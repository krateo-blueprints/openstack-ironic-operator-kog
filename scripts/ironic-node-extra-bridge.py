#!/usr/bin/env python3
"""
Ironic -> Kubernetes bridge for `node.extra`.

The kubernetes-cluster blueprint's bootstrap CP publishes its
`kubeadm join` command into Ironic's `node.extra.kubeadm_join` (and, for
HA, `extra.cert_key`) because the bare-metal blade can reach Ironic
over its OOB network but CAN'T reach the management cluster's API
(the lab's wg tunnel only routes management-cluster -> lab, not the
reverse). KOG doesn't natively propagate Ironic's `node.extra` back
into the Node CR's spec.extra, so this sidecar does it.

Loop:
  - List Node CRs (KOG primitives) in our namespace
  - For each, GET /v1/nodes/<uuid> from the local keystone-ironic-proxy
    (the existing container in the same pod, no extra auth needed)
  - If Ironic's `extra.kubeadm_join` differs from the CR's
    spec.extra.kubeadm_join: PATCH the CR with the new extra

Pure stdlib (urllib + ssl + json) — no openstacksdk dependency.
"""

import datetime
import json
import os
import ssl
import sys
import time
import urllib.request
import urllib.error

K8S_CA = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
K8S_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
K8S_NS_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"
IRONIC_URL = "http://127.0.0.1:8080"
IRONIC_VER = os.environ.get("IRONIC_API_VERSION", "1.109")
KOG_API = "baremetal.ogen.krateo.io/v1alpha1"
INTERVAL = int(os.environ.get("BRIDGE_INTERVAL", "10"))

EXTRA_KEYS = ("kubeadm_join", "cert_key")

HELM_RELEASE_SECRET_PREFIX = "sh.helm.release.v1."
# Don't reap a KOG primitive whose helm-release-name doesn't match a
# tracked release until it's at least this old. Avoids racing a helm
# install that just created child resources but hasn't yet recorded its
# release secret.
ORPHAN_MIN_AGE_SECONDS = 60
ORPHAN_RESOURCE_PATHS = ("ports", "nodes", "nodeprovisions", "nodepowers")


def log(msg):
    sys.stdout.write(f"[ironic-extra-bridge] {msg}\n")
    sys.stdout.flush()


def read_file(p):
    with open(p) as f:
        return f.read().strip()


def k8s_request(method, path, body=None):
    url = "https://kubernetes.default.svc" + path
    headers = {
        "Authorization": f"Bearer {read_file(K8S_TOKEN_PATH)}",
        "Accept": "application/json",
    }
    if body is not None:
        headers["Content-Type"] = "application/merge-patch+json"
    ctx = ssl.create_default_context(cafile=K8S_CA)
    req = urllib.request.Request(url, method=method, headers=headers, data=body)
    with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
        return r.read()


def ironic_get_node(uuid):
    # The keystone-ironic-proxy in the same pod sometimes drops the next
    # request when called back-to-back during a tight reconcile loop —
    # appears as ECONNREFUSED or timeout. One quick retry + a small
    # inter-request sleep absorbs that without changing reconcile cadence.
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                f"{IRONIC_URL}/v1/nodes/{uuid}",
                headers={"X-OpenStack-Ironic-API-Version": IRONIC_VER},
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError:
            raise
        except Exception as e:
            last = e
            time.sleep(0.5)
    raise last if last else RuntimeError("ironic_get_node failed")


# Ironic provision states during which a published kubeadm_join can be
# trusted to belong to the CURRENT deploy. Outside these, the value is
# stale data left over from a previous run — Ironic preserves node.extra
# across undeploys — and propagating it lets the worker render gate open
# on a CA hash that no longer matches the (next) bootstrap CP.
ACTIVE_LIKE_STATES = {"active", "deploying", "wait call-back"}


def ironic_patch_node(uuid, ops):
    body = json.dumps(ops).encode()
    req = urllib.request.Request(
        f"{IRONIC_URL}/v1/nodes/{uuid}",
        method="PATCH",
        headers={
            "X-OpenStack-Ironic-API-Version": IRONIC_VER,
            "Content-Type": "application/json-patch+json",
        },
        data=body,
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read()


def reconcile_once(namespace):
    body = k8s_request("GET", f"/apis/{KOG_API}/namespaces/{namespace}/nodes")
    nodes = json.loads(body).get("items", [])
    for idx, n in enumerate(nodes):
        if idx > 0:
            time.sleep(0.2)  # pace requests to the keystone-ironic-proxy
        name = n["metadata"]["name"]
        uuid = (n.get("spec") or {}).get("uuid")
        if not uuid:
            continue
        try:
            ir = ironic_get_node(uuid)
        except urllib.error.HTTPError as e:
            if e.code != 404:
                log(f"GET ironic node {name}/{uuid}: HTTP {e.code}")
            continue
        except Exception as e:
            log(f"GET ironic node {name}/{uuid}: {e}")
            continue

        ir_extra = ir.get("extra") or {}
        cr_extra = ((n.get("spec") or {}).get("extra")) or {}
        ir_state = ir.get("provision_state", "")

        if ir_state not in ACTIVE_LIKE_STATES:
            # Between deploys: clear stale managed keys from BOTH sides so
            # the next deploy's publish-join.sh starts from a clean slate
            # and the chart's joinCommand lookup gate stays closed until
            # the FRESH PATCH lands.
            ironic_to_clear = [k for k in EXTRA_KEYS if k in ir_extra]
            if ironic_to_clear:
                try:
                    ironic_patch_node(
                        uuid,
                        [{"op": "remove", "path": f"/extra/{k}"} for k in ironic_to_clear],
                    )
                    log(f"cleared Ironic.extra keys {ironic_to_clear} on {name} (state={ir_state})")
                except Exception as e:
                    log(f"clear Ironic.extra {name}: {e}")
            cr_to_clear = [k for k in EXTRA_KEYS if k in cr_extra]
            if cr_to_clear:
                merged = {k: v for k, v in cr_extra.items() if k not in cr_to_clear}
                patch = json.dumps({"spec": {"extra": merged}}).encode()
                try:
                    k8s_request(
                        "PATCH",
                        f"/apis/{KOG_API}/namespaces/{namespace}/nodes/{name}",
                        body=patch,
                    )
                    log(f"cleared CR.spec.extra keys {cr_to_clear} on {name}")
                except Exception as e:
                    log(f"clear CR.spec.extra {name}: {e}")
            continue

        # Active-like state: mirror Ironic.extra -> CR.spec.extra.
        deltas = {}
        for k in EXTRA_KEYS:
            ir_v = ir_extra.get(k)
            cr_v = cr_extra.get(k)
            if ir_v and ir_v != cr_v:
                deltas[k] = ir_v
        if not deltas:
            continue

        merged = dict(cr_extra)
        merged.update(deltas)
        patch = json.dumps({"spec": {"extra": merged}}).encode()
        try:
            k8s_request(
                "PATCH",
                f"/apis/{KOG_API}/namespaces/{namespace}/nodes/{name}",
                body=patch,
            )
            log(f"bridged extra keys {list(deltas)} from Ironic to CR {name}")
        except Exception as e:
            log(f"PATCH CR {name}: {e}")


def _parse_iso_timestamp(s):
    # k8s timestamps look like 2026-06-12T20:00:00Z. Strip the trailing Z
    # and any fractional seconds so plain fromisoformat works on 3.11+.
    s = s.rstrip("Z").split(".")[0]
    return datetime.datetime.fromisoformat(s)


def list_tracked_helm_releases(namespace):
    """Return the set of currently-tracked helm release names in the ns.
    Returns None if listing fails so callers can skip the reap step
    instead of mis-treating every annotated resource as orphan."""
    try:
        body = k8s_request("GET", f"/api/v1/namespaces/{namespace}/secrets")
    except Exception as e:
        log(f"list helm release secrets: {e}")
        return None
    names = set()
    for s in json.loads(body).get("items", []):
        n = s["metadata"]["name"]
        if n.startswith(HELM_RELEASE_SECRET_PREFIX):
            rest = n[len(HELM_RELEASE_SECRET_PREFIX):]
            # strip the trailing ".v<rev>"
            names.add(rest.rsplit(".v", 1)[0])
    return names


def reap_orphan_helm_artifacts(namespace, active_releases):
    """Clean up KOG primitives left behind by a half-failed helm install.

    cdc-baremetal-host generates a fresh random helm-release-name on
    every retry. If the original install errored after creating the
    child Port/Node/NodeProvision/NodePower CRs but BEFORE recording the
    release secret, the next retry hits:

        Port "<name>" exists and cannot be imported into the current
        release: ... must equal "<newrelease>": current value is
        "<oldrelease>"

    and stays in ReconcileError forever. This sweep deletes any KOG
    primitive whose helm-release-name annotation points at a release
    that no longer has a secret in the namespace, after the resource has
    been around at least ORPHAN_MIN_AGE_SECONDS (so we don't race a
    just-completing install)."""
    if active_releases is None:
        return
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    for resource_path in ORPHAN_RESOURCE_PATHS:
        try:
            body = k8s_request(
                "GET",
                f"/apis/{KOG_API}/namespaces/{namespace}/{resource_path}",
            )
            items = json.loads(body).get("items", [])
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            log(f"list {resource_path}: HTTP {e.code}")
            continue
        except Exception as e:
            log(f"list {resource_path}: {e}")
            continue
        for item in items:
            md = item.get("metadata", {})
            ann = md.get("annotations") or {}
            release = ann.get("meta.helm.sh/release-name", "")
            if not release or release in active_releases:
                continue
            created_at = md.get("creationTimestamp")
            if created_at:
                try:
                    age = (now - _parse_iso_timestamp(created_at)).total_seconds()
                except Exception:
                    age = ORPHAN_MIN_AGE_SECONDS + 1
                if age < ORPHAN_MIN_AGE_SECONDS:
                    continue
            name = md["name"]
            # Strip the finalizer so KOG's own controller doesn't block
            # the delete by retrying the Ironic-side cleanup forever.
            try:
                k8s_request(
                    "PATCH",
                    f"/apis/{KOG_API}/namespaces/{namespace}/{resource_path}/{name}",
                    body=json.dumps({"metadata": {"finalizers": []}}).encode(),
                )
            except Exception as e:
                log(f"strip finalizer {resource_path}/{name}: {e}")
            try:
                k8s_request(
                    "DELETE",
                    f"/apis/{KOG_API}/namespaces/{namespace}/{resource_path}/{name}",
                )
                log(f"reaped orphan {resource_path}/{name} (release={release} not tracked)")
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    continue
                log(f"delete {resource_path}/{name}: HTTP {e.code}")
            except Exception as e:
                log(f"delete {resource_path}/{name}: {e}")


def main():
    namespace = read_file(K8S_NS_PATH)
    log(f"starting; namespace={namespace} interval={INTERVAL}s")
    while True:
        try:
            reconcile_once(namespace)
            reap_orphan_helm_artifacts(namespace, list_tracked_helm_releases(namespace))
        except Exception as e:
            log(f"reconcile loop error: {e}")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
