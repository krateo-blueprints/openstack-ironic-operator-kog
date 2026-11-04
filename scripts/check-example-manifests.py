#!/usr/bin/env python3
"""Check that every manifest a reader is meant to APPLY would actually apply.

Why this exists. Shipped example manifests are the artifacts least likely to be
exercised and most likely to be copied verbatim, and the two failure modes they
carry are both invisible to `helm template`, `helm lint` and `kubeconform` --
they are rejected by the API server and nowhere earlier. Audited 2026-11-04
after a sibling KOG provider reported four defects out of four in hand-written
artifact sets that had never been applied; this repo had two of the same kinds:

  * `manifests/nodeconfiguration-example.yaml` declared kind
    `NodeConfiguration`. No RestDefinition declares a `Node` kind, so that kind
    has never existed -- the real one is `BaremetalNodeConfiguration`.
  * Four example manifests declared composition apiVersions from long-pruned
    chart versions (`v0-1-0` against a chart at 0.13.x).

Both would be caught instantly by applying them and never by reading them,
which is exactly the class a check earns its keep on.

Scope is deliberately narrow: files a reader is told to apply. Historical test
fixtures under manifests/ (blade02-e2e and friends) are records of past runs,
pinned to the version they ran against on purpose, and are skipped.

Exit 1 on any finding. Run: make check-examples
"""
import glob
import os
import re
import sys

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def chart_versions():
    """chart dir name -> the composition apiVersion a fresh install would serve"""
    out = {}
    for c in glob.glob(os.path.join(REPO, "charts/*/Chart.yaml")):
        name = os.path.basename(os.path.dirname(c))
        with open(c) as fh:
            v = yaml.safe_load(fh)["version"]
        out[name] = "composition.krateo.io/v" + v.replace(".", "-")
    return out


def declared_kinds():
    """every <Kind>Configuration the RestDefinitions make real"""
    kinds = set()
    for f in glob.glob(os.path.join(REPO, "manifests/restdefinition-*.yaml")):
        with open(f) as fh:
            d = yaml.safe_load(fh) or {}
        k = ((d.get("spec") or {}).get("resource") or {}).get("kind")
        if k:
            kinds.add(k)
    return kinds, {k + "Configuration" for k in kinds}


# composition Kind -> the chart that generates it
KIND_TO_CHART = {
    "BaremetalHost": "baremetal-host",
    "BaremetalLifecycle": "baremetal-lifecycle",
    "BaremetalDiscovery": "baremetal-discovery",
    "KubernetesCluster": "kubernetes-cluster",
}


def targets():
    """manifests a reader is meant to apply"""
    out = sorted(glob.glob(os.path.join(REPO, "examples/**/*.yaml"), recursive=True))
    out += sorted(
        f for f in glob.glob(os.path.join(REPO, "manifests/*.yaml"))
        if "example" in os.path.basename(f)
    )
    return out


def main():
    charts = chart_versions()
    kinds, config_kinds = declared_kinds()
    problems = []
    checked = 0

    for f in targets():
        rel = os.path.relpath(f, REPO)
        with open(f) as fh:
            try:
                docs = list(yaml.safe_load_all(fh))
            except yaml.YAMLError as e:
                problems.append(f"{rel}: not parseable as YAML -- {e}")
                continue
        for d in docs:
            if not isinstance(d, dict):
                continue
            kind, av = d.get("kind", ""), d.get("apiVersion", "")
            if not kind:
                continue
            checked += 1

            # 1. a Configuration kind that no RestDefinition makes real
            if kind.endswith("Configuration") and kind not in config_kinds:
                problems.append(
                    f"{rel}: kind {kind!r} does not exist. "
                    f"Valid: {', '.join(sorted(config_kinds))}"
                )

            # 2. a composition apiVersion that no longer matches its chart
            if kind in KIND_TO_CHART and av.startswith("composition.krateo.io/"):
                want = charts.get(KIND_TO_CHART[kind])
                if want and av != want:
                    problems.append(
                        f"{rel}: {kind} declares {av}, but "
                        f"charts/{KIND_TO_CHART[kind]} is at {want}"
                    )

    print(f"  checked {checked} resources across {len(targets())} files")
    if problems:
        print(f"  {len(problems)} problem(s):")
        for p in problems:
            print(f"    - {p}")
        return 1
    # A clean result is only meaningful if the check could see anything at all.
    if checked == 0:
        print("  NOTHING CHECKED -- the glob matched no resources; treat as a failure")
        return 1
    print("  OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
