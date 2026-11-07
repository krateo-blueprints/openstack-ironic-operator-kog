#!/usr/bin/env python3
"""Merge chart packages into the chartrepo ConfigMap, never replacing it.

Prints the merged ConfigMap as JSON on stdout (JSON is valid YAML). The caller
pipes it to `kubectl apply --server-side -f -` -- SERVER-SIDE IS LOAD-BEARING,
see BUDGET_BYTES below.

WHY MERGE RATHER THAN REGENERATE. The first version of `make chart-host` built
the ConfigMap from only the four versions on disk today and piped it through
`kubectl apply`, which prunes keys that are no longer listed. So bumping
baremetal-host to 0.6.4 would DELETE baremetal-host-0.6.3.tgz -- a version
manifests/compositiondefinition-baremetal-host.yaml still pins -- and the next
render would fail with the chart-inspector 500-wrapping-404 that the
ConfigMap-backed chartrepo was introduced to eliminate. Found by /code-review
before it happened.

Accumulating also retires the reason scripts/chart-host-pinned.sh existed: that
script rebuilt pinned versions from git because the pod's container filesystem
was wiped on every restart. ConfigMap content lives in etcd, so it survives pod
restarts, node restarts and `docker stop`. Pinned versions now need adding once,
not restoring on every resume.

A ConfigMap is capped at ~1MiB of serialised data, so this refuses to grow past
a budget rather than failing inside the API server with a message about etcd.
"""
import argparse
import base64
import json
import os
import subprocess
import sys

# TWO ceilings, and the one that binds is not the obvious one.
#
#   object size          ~1 MiB   (etcd/apiserver, the one everyone quotes)
#   annotation size     262,144   kubectl.kubernetes.io/last-applied-configuration
#
# A CLIENT-SIDE `kubectl apply` copies the entire merged object into that
# annotation, so the real ceiling is 262,144 and it arrives at roughly a quarter
# of the object limit. Measured 2026-11-07: four charts = 124,144 bytes of
# base64 and an annotation of 124,413 -- 47% of the annotation cap while this
# script's own budget reported 13%. Two more versions would have failed
# `make chart-host` outright with `metadata.annotations: Too long`, which is
# exactly the opaque API-server error the budget exists to pre-empt. Found by
# /code-review.
#
# Both callers therefore use `kubectl apply --server-side`, which writes no
# last-applied annotation, so the object limit is the only ceiling and this
# budget is the meaningful one again. If you ever revert to a client-side apply,
# drop BUDGET_BYTES to 120_000.
BUDGET_BYTES = 900_000


def kubectl(args, kubeconfig, context, check=True):
    cmd = ["kubectl"]
    if kubeconfig:
        cmd += ["--kubeconfig", kubeconfig]
    if context:
        cmd += ["--context", context]
    cmd += args
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def existing(name, namespace, kubeconfig, context):
    """Current binaryData, or {} when the ConfigMap does not exist yet."""
    r = kubectl(["-n", namespace, "get", "configmap", name, "-o", "json"],
                kubeconfig, context, check=False)
    if r.returncode != 0:
        if "NotFound" in r.stderr or "not found" in r.stderr:
            return {}
        # Any other failure must NOT read as "nothing there": that is how the
        # pruning bug would come back, silently, as an empty merge base.
        raise SystemExit(f"error: reading configmap/{name} failed, refusing to "
                         f"treat that as empty: {r.stderr.strip()}")
    doc = json.loads(r.stdout)
    # A stale last-applied annotation from an earlier CLIENT-SIDE apply keeps the
    # 262,144-byte ceiling in force even after both callers move to
    # --server-side: server-side apply stops WRITING it and does not remove it.
    # Measured 2026-11-07: after switching, the annotation was still 124,392
    # bytes. So say so rather than trusting whoever reads this to remember.
    la = (doc["metadata"].get("annotations") or {}).get(
        "kubectl.kubernetes.io/last-applied-configuration", "")
    if len(la) > 50_000:
        print(f"warning: configmap/{name} still carries a "
              f"{len(la):,}-byte kubectl.kubernetes.io/last-applied-configuration "
              f"annotation from an earlier client-side apply. That annotation is "
              f"capped at 262,144 bytes and is the ceiling that will actually "
              f"bite. Remove it once:\n"
              f"  kubectl -n {namespace} annotate configmap {name} "
              f"kubectl.kubernetes.io/last-applied-configuration-",
              file=sys.stderr)
    return dict(doc.get("binaryData") or {})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tgz", nargs="+", help="chart packages to merge in")
    ap.add_argument("--name", default="chartrepo-charts")
    ap.add_argument("--namespace", required=True)
    ap.add_argument("--kubeconfig", default=os.environ.get("KUBECONFIG"))
    ap.add_argument("--context", default=None)
    ap.add_argument("--budget", type=int, default=BUDGET_BYTES)
    a = ap.parse_args()

    data = existing(a.name, a.namespace, a.kubeconfig, a.context)
    kept = len(data)

    for path in a.tgz:
        if not os.path.isfile(path):
            raise SystemExit(f"error: {path} is not a file")
        key = os.path.basename(path)
        if not key.endswith(".tgz"):
            raise SystemExit(f"error: {key} is not a .tgz")
        with open(path, "rb") as fh:
            data[key] = base64.b64encode(fh.read()).decode("ascii")

    total = sum(len(v) for v in data.values())
    if total > a.budget:
        raise SystemExit(
            f"error: the merged chart set is {total} bytes of base64, over the "
            f"{a.budget} budget (a ConfigMap is capped near 1MiB).\n"
            f"       {len(data)} versions held. Drop versions no "
            f"CompositionDefinition pins, or move the chartrepo to a PVC.")

    doc = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": a.name, "namespace": a.namespace},
        "binaryData": dict(sorted(data.items())),
    }
    # Emitted without PyYAML so this works on a bare runner: JSON is valid YAML.
    print(json.dumps(doc, indent=2))
    added = len(data) - kept
    print(f"# {len(data)} chart version(s), {total} bytes of base64 "
          f"({100*total//a.budget}% of budget); {added} new, {kept} carried over",
          file=sys.stderr)


if __name__ == "__main__":
    main()
