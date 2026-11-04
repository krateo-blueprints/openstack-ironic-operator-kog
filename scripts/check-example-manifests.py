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

--self-test proves this script can still FAIL, by running the real check
against manifests built to be broken. It ships here, inside the thing it
calibrates, for the reason a peer's own repo puts best: a check that lives in
a previous commit message is a check nobody runs. The first calibration of
this script was a shell one-liner that injected a bad version and then reverted
it -- which proved the checker worked that afternoon and nothing afterwards.
`make check-examples` runs the self-test first; if it fails, the real result is
void rather than passing.

Exit 1 on any finding, 2 if the self-test fails. Run: make check-examples
"""
import argparse
import glob
import os
import sys
import tempfile

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# composition Kind -> the chart that generates it
KIND_TO_CHART = {
    "BaremetalHost": "baremetal-host",
    "BaremetalLifecycle": "baremetal-lifecycle",
    "BaremetalDiscovery": "baremetal-discovery",
    "KubernetesCluster": "kubernetes-cluster",
}


def chart_versions():
    """chart dir name -> the composition apiVersion a fresh install would serve"""
    out = {}
    for c in glob.glob(os.path.join(REPO, "charts/*/Chart.yaml")):
        name = os.path.basename(os.path.dirname(c))
        with open(c) as fh:
            v = yaml.safe_load(fh)["version"]
        out[name] = "composition.krateo.io/v" + v.replace(".", "-")
    return out


def config_kinds():
    """every <Kind>Configuration the RestDefinitions make real"""
    kinds = set()
    for f in glob.glob(os.path.join(REPO, "manifests/restdefinition-*.yaml")):
        with open(f) as fh:
            d = yaml.safe_load(fh) or {}
        k = ((d.get("spec") or {}).get("resource") or {}).get("kind")
        if k:
            kinds.add(k)
    return {k + "Configuration" for k in kinds}


def check(files, charts, valid_config_kinds, root):
    """the whole rule, over an arbitrary file list. Returns (problems, resources_seen)."""
    problems, seen = [], 0
    for f in files:
        rel = os.path.relpath(f, root)
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
            seen += 1
            if kind.endswith("Configuration") and kind not in valid_config_kinds:
                problems.append(
                    f"{rel}: kind {kind!r} does not exist. "
                    f"Valid: {', '.join(sorted(valid_config_kinds))}"
                )
            if kind in KIND_TO_CHART and av.startswith("composition.krateo.io/"):
                want = charts.get(KIND_TO_CHART[kind])
                if want and av != want:
                    problems.append(
                        f"{rel}: {kind} declares {av}, but "
                        f"charts/{KIND_TO_CHART[kind]} is at {want}"
                    )
    return problems, seen


def targets():
    """manifests a reader is meant to apply"""
    out = sorted(glob.glob(os.path.join(REPO, "examples/**/*.yaml"), recursive=True))
    out += sorted(
        f for f in glob.glob(os.path.join(REPO, "manifests/*.yaml"))
        if "example" in os.path.basename(f)
    )
    return out


def self_test():
    """Prove the check can still fail, by running it against built-to-be-broken input.

    The defect goes into the SUBJECT -- real files fed to the real check() -- not
    into a reconstruction of the logic. A peer had three screens defeated by their
    own documentation, each one correct and matching the wrong thing; the lesson is
    that proving a checker can return non-zero is not the same as proving it
    returns non-zero FOR THIS.
    """
    charts = {"kubernetes-cluster": "composition.krateo.io/v9-9-9"}
    valid = {"BaremetalNodeConfiguration"}
    cases, failures = 0, []

    with tempfile.TemporaryDirectory() as td:
        def write(name, body):
            p = os.path.join(td, name)
            with open(p, "w") as fh:
                fh.write(body)
            return p

        bad_version = write("bad-version.yaml",
            "apiVersion: composition.krateo.io/v0-1-0\nkind: KubernetesCluster\n"
            "metadata:\n  name: x\n")
        bad_kind = write("bad-kind.yaml",
            "apiVersion: baremetal.ogen.krateo.io/v1alpha1\nkind: NodeConfiguration\n"
            "metadata:\n  name: x\n")
        good = write("good.yaml",
            "apiVersion: composition.krateo.io/v9-9-9\nkind: KubernetesCluster\n"
            "metadata:\n  name: x\n")
        broken_yaml = write("broken.yaml", "apiVersion: [unclosed\n")

        for label, files, want_hit in (
            ("a stale apiVersion is caught", [bad_version], True),
            ("a nonexistent kind is caught", [bad_kind], True),
            ("unparseable YAML is caught", [broken_yaml], True),
            ("a correct manifest passes", [good], False),
        ):
            cases += 1
            problems, seen = check(files, charts, valid, td)
            if bool(problems) != want_hit:
                failures.append(
                    f"{label}: expected {'a finding' if want_hit else 'no finding'}, "
                    f"got {problems or 'none'}"
                )

        # The check must also notice it examined nothing -- otherwise a glob that
        # stops matching prints OK forever.
        cases += 1
        _, seen = check([good], charts, valid, td)
        if seen != 1:
            failures.append(f"resource counting is wrong: saw {seen}, expected 1")

    print(f"  self-test: {cases} cases")
    for f in failures:
        print(f"    FAIL {f}")
    if failures:
        print("  SELF-TEST FAILED -- the real result below would be meaningless")
        return False
    print("  self-test: OK (the check demonstrably still fails on bad input)")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true",
                    help="prove the check can still fail, then exit")
    args = ap.parse_args()

    if args.self_test:
        return 0 if self_test() else 2

    files = targets()
    problems, seen = check(files, chart_versions(), config_kinds(), REPO)
    print(f"  checked {seen} resources across {len(files)} files")
    if problems:
        print(f"  {len(problems)} problem(s):")
        for p in problems:
            print(f"    - {p}")
        return 1
    if seen == 0:
        print("  NOTHING CHECKED -- the glob matched no resources; treat as a failure")
        return 1
    print("  OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
