#!/usr/bin/env python3
"""Wait until the chartrepo pod actually SERVES every version the ConfigMap holds.

Why this is not `kubectl rollout status`. On a re-run of `make chart-host` the
Deployment spec is unchanged, so `apply` is a no-op, the pod is already Ready
against the OLD ConfigMap, and `rollout status` returns in milliseconds.
kubelet propagates the new ConfigMap on its periodic sync, up to a minute
later, and for that window nginx 404s the version just published while the
target reports success -- reproducing the chart-inspector 500-wrapping-404 the
ConfigMap-backed chartrepo exists to prevent. The readiness probe only ever
proved the first run, and /code-review caught the claim that it "IS the
verification".
"""
import argparse
import json
import subprocess
import sys
import time


def kubectl(args, a, check=True):
    cmd = ["kubectl"]
    if a.kubeconfig:
        cmd += ["--kubeconfig", a.kubeconfig]
    if a.context:
        cmd += ["--context", a.context]
    return subprocess.run(cmd + args, capture_output=True, text=True, check=check)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="chartrepo-charts")
    ap.add_argument("--namespace", required=True)
    ap.add_argument("--deployment", default="deploy/chartrepo")
    ap.add_argument("--kubeconfig")
    ap.add_argument("--context")
    ap.add_argument("--timeout", type=int, default=90)
    a = ap.parse_args()

    r = kubectl(["-n", a.namespace, "get", "configmap", a.name, "-o", "json"], a, check=False)
    if r.returncode != 0:
        print(f"::error::cannot read configmap/{a.name}: {r.stderr.strip()}")
        return 1
    want = sorted((json.loads(r.stdout).get("binaryData") or {}).keys())
    if not want:
        print(f"::error::configmap/{a.name} holds no chart versions")
        return 1

    deadline = time.time() + a.timeout
    missing = list(want)
    while time.time() < deadline:
        ls = kubectl(["-n", a.namespace, "exec", a.deployment, "--",
                      "ls", "-1", "/usr/share/nginx/html/"], a, check=False)
        served = set(ls.stdout.split()) if ls.returncode == 0 else set()
        missing = [k for k in want if k not in served]
        if not missing:
            print(f"serving all {len(want)} version(s): {' '.join(want)}")
            return 0
        time.sleep(2)

    print(f"::error::after {a.timeout}s the pod still does not serve: {' '.join(missing)}")
    print(f"           (ConfigMap holds {len(want)}, pod serves {len(want)-len(missing)})")
    return 1


if __name__ == "__main__":
    sys.exit(main())
