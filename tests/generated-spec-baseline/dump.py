#!/usr/bin/env python3
"""Dump the spec-schema facts that oasgen bugs #419 / #455 turn on, in a form
that can be diffed against a recorded baseline.

Why a dump-and-diff rather than assertions with expected values baked in: the
expected values DEPEND ON WHICH FIXES THE BINARY CARRIES, and there are three
independent ones in flight (#419/#421 restores node_id, #455 restores the uuid
exclusion, #453 governs whether a regeneration happens at all). A script with
one hard-coded expectation would have to be edited for every combination, and
whoever edits it will make it agree with whatever it just printed.

So: this prints facts. BASELINE.md records what each tagged version produced.
The reader does the comparison.

Usage:
  KUBECONFIG=... CONTEXT=... python3 -I dump.py            # dump
  KUBECONFIG=... CONTEXT=... python3 -I dump.py --self-test
"""
import json
import os
import subprocess
import sys

# The kinds the two bugs touch, and WHY each is in the list.
KINDS = {
    # #455: excludedSpecFields:[uuid] vs a path-level required:true uuid.
    # Expected on 0.50.0 and on a fixed 0.51.x: uuid ABSENT entirely.
    # Expected on unfixed 0.51.0 regenerated: uuid PRESENT and REQUIRED.
    "ports":          ("baremetal.ogen.krateo.io", "uuid"),
    "portgroups":     ("baremetal.ogen.krateo.io", "uuid"),
    "allocations":    ("baremetal.ogen.krateo.io", "uuid"),
    "deploytemplates": ("baremetal.ogen.krateo.io", "uuid"),
    # #419/#421: a requestFieldMapping suppresses the property it reads from.
    # Expected before the fix: node_id ABSENT. After: PRESENT and REQUIRED.
    "nodeprovisions": ("baremetal.ogen.krateo.io", "node_id"),
    "nodepowers":     ("baremetal.ogen.krateo.io", "node_id"),
}


def kubectl(*args):
    base = ["kubectl"]
    if os.environ.get("KUBECONFIG_FILE"):
        base += ["--kubeconfig", os.environ["KUBECONFIG_FILE"]]
    if os.environ.get("CONTEXT"):
        base += ["--context", os.environ["CONTEXT"]]
    return subprocess.run(base + list(args) + ["--request-timeout=30s"],
                          capture_output=True, text=True)


def facts(plural, group, key):
    r = kubectl("get", "crd", f"{plural}.{group}", "-o", "json")
    if r.returncode != 0:
        return {"crd": "ABSENT"}
    d = json.loads(r.stdout)
    out = {"uid": d["metadata"]["uid"],
           "creationTimestamp": d["metadata"]["creationTimestamp"],
           "versions": {}}
    for v in d["spec"]["versions"]:
        sch = ((v.get("schema") or {}).get("openAPIV3Schema") or {}) \
            .get("properties", {}).get("spec", {})
        props = sorted((sch.get("properties") or {}).keys())
        req = sorted(sch.get("required") or [])
        out["versions"][v["name"]] = {
            "served": v["served"],
            "properties": props,
            "required": req,
            f"{key}_in_properties": key in props,
            f"{key}_in_required": key in req,
        }
    return out


def self_test():
    """Prove the dump can distinguish present from absent, so a run that reports
    ABSENT everywhere is not mistaken for a result. Three of my own extractor
    bugs produced exactly that -- a tool that had read no files, reporting
    confident zeros."""
    cases, fails = 0, []
    cases += 1
    r = kubectl("get", "crd", "-o", "name")
    if r.returncode != 0:
        fails.append("cannot reach the cluster at all -- every ABSENT below would be meaningless")
    cases += 1
    # a CRD that certainly exists on any cluster with oasgen installed
    probe = kubectl("get", "crd", "restdefinitions.ogen.krateo.io", "-o", "name")
    if probe.returncode != 0:
        fails.append("restdefinitions CRD not found -- is oasgen installed? "
                     "without it the kind lookups cannot be trusted")
    # stderr, not stdout: stdout is the JSON payload and a consumer must be
    # able to pipe it. Printing diagnostics to stdout made the first version's
    # output unparseable -- a tool whose result you cannot read is a tool that
    # reported nothing.
    e = sys.stderr
    print(f"  self-test: {cases} cases", file=e)
    for f in fails:
        print(f"    FAIL {f}", file=e)
    if fails:
        print("  SELF-TEST FAILED -- the dump below would be meaningless", file=e)
        return False
    print("  self-test: OK (the cluster answers, and oasgen's own CRD is visible)", file=e)
    return True


def main():
    if "--self-test" in sys.argv:
        return 0 if self_test() else 2
    if not self_test():
        return 2
    out = {}
    for plural, (group, key) in KINDS.items():
        out[plural] = {"watched_field": key, **facts(plural, group, key)}
    print(json.dumps(out, indent=2, sort_keys=True))
    absent = [k for k, v in out.items() if v.get("crd") == "ABSENT"]
    if len(absent) == len(KINDS):
        print("\nALL CRDs ABSENT -- this is not a result, it is an empty cluster.",
              file=sys.stderr)
        return 2
    if absent:
        print(f"\nnote: absent kinds (not applied on this cluster): {absent}",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
