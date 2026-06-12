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

        # Are any of the bridge-managed keys present in Ironic but
        # missing/different in the CR?
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


def main():
    namespace = read_file(K8S_NS_PATH)
    log(f"starting; namespace={namespace} interval={INTERVAL}s")
    while True:
        try:
            reconcile_once(namespace)
        except Exception as e:
            log(f"reconcile loop error: {e}")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
