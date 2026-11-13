#!/usr/bin/env python3
"""What a chart RENDERS must be a kind that exists, at a version that is served.

WHY THIS EXISTS. check-example-manifests.py screens manifests, examples and README
fences -- everything a reader COPIES. Nothing screened what a chart PRODUCES. So
`baremetal-discovery` shipped, published and documented, rendering

    apiVersion: baremetal.ogen.krateo.io/v1alpha1
    kind: Node

for its entire life (the helper has carried `v1alpha1` since commit a009c6f). No
RestDefinition in this repo declares a kind called `Node`; the node kind is
`BaremetalNode` at `v1-0-0`. `kubectl apply` of that output fails with "no matches
for kind Node". Worse, the chart's own `lookup` used the same non-existent kind, so
every transition guard failed silently and the chart rendered ONE resource and
nothing else -- it appeared to install and did nothing.

The sibling screen's own docstring records the same defect being fixed in the
quickstart: "the quickstart shipped `kind: Node` at an unserved apiVersion for
months". It was fixed where readers copy it from, and left in the chart that
generates it, because no screen rendered a chart.

VOID IS NOT A PASS. Several charts here are lookup-driven: their interesting output
is gated on a live cluster read, so `helm template` offline shows a subset or
nothing. A chart whose output carries nothing in our API group is reported as VOID
FOR THAT CHART -- this screen cleared nothing there -- rather than counted as clean.
kubernetes-cluster is in that state even when rendered from its own lab CR's spec.
"""
import glob
import os
import subprocess
import sys

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GROUP = "baremetal.ogen.krateo.io"

# Per-chart values. A chart that cannot be rendered is VOID, never a pass, so these
# exist to get as far as `helm template` honestly can -- not to force output.
CHART_VALUES = {
    "baremetal-discovery": ["nodeName=b1", "driver=redfish"],
    "baremetal-host": ["nodeName=b1", "driver=redfish"],
    "baremetal-lifecycle": ["nodeName=b1", "driver=redfish",
                            "instance_info.image_source=http://example.invalid/i.qcow2",
                            "instance_info.image_checksum=0"],
    "kubernetes-cluster": ["clusterName=lab", "managementCluster.caBundle=Zm9v",
                           "controlPlane.nodes[0].nodeName=cp1"],
}


class Void(Exception):
    """The screen could not form an opinion. NOT a pass."""


def served_kinds():
    """kind -> served apiVersion, from the RestDefinitions that generate the CRDs.

    The RestDefinition is the source of truth: it is what oasgen turns into a CRD,
    so it states both the kind and the group. The version comes from the generated
    CRD's naming convention, which this repo pins everywhere as v1-0-0.
    """
    out = {}
    for f in sorted(glob.glob(os.path.join(REPO, "manifests/restdefinition-*.yaml"))):
        try:
            d = yaml.safe_load(open(f))
        except Exception as e:
            raise Void("could not parse %s (%s), so the set of real kinds is unknown"
                       % (os.path.basename(f), e))
        res = (d.get("spec") or {}).get("resource") or {}
        kind, group = res.get("kind"), (d.get("spec") or {}).get("resourceGroup")
        if kind and group:
            out[kind] = "%s/v1-0-0" % group
    if not out:
        raise Void("no RestDefinition yielded a kind, so every chart would be "
                   "measured against an empty set and pass by construction")
    return out


def render(chart_dir):
    name = os.path.basename(chart_dir.rstrip("/"))
    cmd = ["helm", "template", "probe", chart_dir]
    for v in CHART_VALUES.get(name, []):
        cmd += ["--set", v]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return None, (r.stderr.strip().split("\n") or ["helm failed"])[-1][:160]
    return r.stdout, None


def audit(chart, text, kinds):
    """Return (findings, ours) for one chart's rendered output."""
    findings, ours = [], []
    try:
        docs = list(yaml.safe_load_all(text))
    except Exception as e:
        findings.append((chart, "rendered output does not parse as YAML: %s" % e))
        return findings, ours
    for d in docs:
        if not isinstance(d, dict):
            continue
        av, kind = d.get("apiVersion") or "", d.get("kind")
        if not kind or GROUP not in av:
            continue
        ours.append(kind)
        if kind not in kinds:
            findings.append((chart,
                             "renders kind %r, which NO RestDefinition declares. The kinds "
                             "that exist are %s. `kubectl apply` fails with \"no matches for "
                             "kind %s\"" % (kind, ", ".join(sorted(kinds)), kind)))
        elif av != kinds[kind]:
            findings.append((chart,
                             "renders %s at %r, but that kind is served at %r"
                             % (kind, av, kinds[kind])))
    return findings, ours


def run():
    kinds = served_kinds()
    charts = sorted(glob.glob(os.path.join(REPO, "charts/*/")))
    if not charts:
        raise Void("no charts found, so this screen rendered nothing")

    findings, voids, checked = [], [], []
    print("kinds that exist, from the RestDefinitions:")
    for k, v in sorted(kinds.items()):
        print("  %-16s %s" % (k, v))
    print()

    for c in charts:
        name = os.path.basename(c.rstrip("/"))
        deprecated = False
        try:
            ch = yaml.safe_load(open(os.path.join(c, "Chart.yaml")))
            deprecated = bool((ch or {}).get("deprecated"))
        except Exception:
            pass
        text, err = render(c)
        if err:
            voids.append((name, "does not render: %s" % err))
            continue
        f, ours = audit(name, text, kinds)
        if not ours:
            voids.append((name, "rendered no %s resource offline -- lookup-gated output is "
                                "invisible to `helm template`, so NOTHING here was screened"
                          % GROUP))
            continue
        checked.append((name, ours, deprecated))
        for chart, msg in f:
            findings.append((chart, msg, deprecated))

    for name, ours, dep in checked:
        print("  %-22s %-12s renders: %s"
              % (name, "(deprecated)" if dep else "", ", ".join(sorted(set(ours)))))

    rc = 0
    live = [x for x in findings if not x[2]]
    known = [x for x in findings if x[2]]
    if live:
        print("\nACTIONABLE (%d):" % len(live))
        for chart, msg, _ in live:
            print("  %-22s %s" % (chart, msg))
        rc = 1
    if known:
        print("\nreported, on DEPRECATED charts (documented, not fixed) (%d):" % len(known))
        for chart, msg, _ in known:
            print("  %-22s %s" % (chart, msg))
    if voids:
        print("\nVOID -- this screen formed no opinion on these. NOT a pass (%d):" % len(voids))
        for name, why in voids:
            print("  %-22s %s" % (name, why))

    if rc == 0:
        print("\nPASS -- every kind rendered by a live chart exists and is served.")
        if voids:
            print("       %d chart(s) were NOT screened, listed above." % len(voids))
    else:
        print("\nFAIL -- a live chart renders something that cannot be applied.")
    return rc


def selftest():
    cases, failures = [], []

    def case(n, ok):
        cases.append((n, ok))
        if not ok:
            failures.append(n)

    kinds = {"BaremetalNode": "baremetal.ogen.krateo.io/v1-0-0"}

    good = "apiVersion: baremetal.ogen.krateo.io/v1-0-0\nkind: BaremetalNode\nmetadata:\n  name: a\n"
    f, ours = audit("c", good, kinds)
    case("a real kind at the served version -> clean", not f and ours == ["BaremetalNode"])

    # THE DISCOVERY DEFECT, verbatim
    bad_kind = "apiVersion: baremetal.ogen.krateo.io/v1alpha1\nkind: Node\nmetadata:\n  name: a\n"
    f, ours = audit("c", bad_kind, kinds)
    case("kind that no RestDefinition declares -> FINDING",
         len(f) == 1 and "NO RestDefinition declares" in f[0][1] and ours == ["Node"])

    bad_ver = "apiVersion: baremetal.ogen.krateo.io/v1alpha1\nkind: BaremetalNode\nmetadata:\n  name: a\n"
    f, _ = audit("c", bad_ver, kinds)
    case("real kind at an unserved version -> FINDING",
         len(f) == 1 and "served at" in f[0][1])

    # OVER-EAGERNESS: resources outside our group are none of this screen's business
    other = "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: a\n"
    f, ours = audit("c", other, kinds)
    case("a resource outside our API group is IGNORED", not f and ours == [])

    # a chart rendering nothing of ours yields no findings AND no `ours` -- which the
    # caller must turn into a VOID rather than a pass. Asserted here so the two halves
    # cannot drift apart.
    f, ours = audit("c", "apiVersion: v1\nkind: Secret\nmetadata:\n  name: a\n", kinds)
    case("nothing of ours rendered -> no findings AND empty `ours` (caller VOIDs)",
         not f and ours == [])

    f, _ = audit("c", "this: [is: not: yaml", kinds)
    case("unparsable render -> FINDING, not a silent skip",
         len(f) == 1 and "does not parse" in f[0][1])

    # DISCRIMINATOR: audit must be capable of returning a finding at all. Four arms
    # above expect "clean"; without this one they would all pass against an audit()
    # that never reports anything.
    f, _ = audit("c", bad_kind, kinds)
    case("DISCRIMINATOR: audit CAN return a finding", len(f) == 1)

    for n, ok in cases:
        print("  %-62s %s" % (n, "ok" if ok else "FAIL"))
    print("selftest: %d cases, %d failure(s)" % (len(cases), len(failures)))
    for x in failures:
        print("  failed: %s" % x)
    return 1 if failures else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    try:
        sys.exit(run())
    except Void as v:
        print("VOID: %s" % v)
        print("NOT a pass.")
        sys.exit(2)
