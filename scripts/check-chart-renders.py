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

TWO ARMS, TWO QUESTIONS. The arm above renders with CHART_VALUES, whose job is to
reach interesting output. That is not the consumer's question. A second arm renders
with CHART_GUARD_VALUES -- the minimum that makes a chart emit anything -- and asks
whether the chart renders FROM ITS OWN values.yaml DEFAULTS. The difference is not
academic: CHART_VALUES sets `instance_info.image_source`, which turns a null default
into a map, so `baremetal-lifecycle` rendered cleanly here for months while

    helm template baremetal-lifecycle ./charts/baremetal-lifecycle --set nodeName=x

exited 1 at node.yaml:6, `deepCopy .Values.instance_info | default dict` -- deepCopy
runs BEFORE default, so `default dict` can never rescue a null. The sibling live chart
spells it correctly: `deepCopy (.Values.extra | default dict)`. Measured 2026-11-22:
1 of 3 deepCopy uses in this repo has the broken form, and it is the deprecated one.
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

# The MINIMUM --set values that make each chart emit any document at all. Guards
# only -- never an image source, a checksum or a CA bundle. Measured per chart on
# 2026-11-22 by counting `kind:` lines: the three probe/cluster charts emit from
# their own defaults alone, so their guard is empty; the three node charts are
# wrapped in `if .Values.nodeName` and emit nothing without it.
#
# A guard value MUST also appear in that chart's CHART_VALUES, so the two tables
# cannot describe different worlds; asserted in selftest(). A chart whose guard
# render emits zero documents is VOID for this arm, never a pass -- that is what
# keeps this table honest if someone trims it too far.
CHART_GUARD_VALUES = {
    "baremetal-discovery": ["nodeName=b1"],
    "baremetal-host": ["nodeName=b1"],
    "baremetal-lifecycle": ["nodeName=b1"],
    "hook-rbac-probe": [],
    "kubernetes-cluster": [],
    "release-adopt-probe": [],
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


def helm_error(stderr):
    """The line a reader can act on.

    helm prints `Error: <what went wrong>` and THEN a `Use --debug flag ...` hint, so
    the last line of stderr is the hint. This screen reported that hint as the reason
    a chart did not render until 2026-11-22 -- a message that names no file, no line
    and no cause. Prefer the `Error:` line; fall back to the last line only when
    there is none.
    """
    lines = [l.strip() for l in (stderr or "").strip().split("\n") if l.strip()]
    for l in lines:
        if l.startswith("Error:"):
            return l[:240]
    return (lines[-1] if lines else "helm failed")[:240]


def render(chart_dir, values=None):
    """Render a chart. values=None means CHART_VALUES; pass a list to override."""
    name = os.path.basename(chart_dir.rstrip("/"))
    cmd = ["helm", "template", "probe", chart_dir]
    for v in (CHART_VALUES.get(name, []) if values is None else values):
        cmd += ["--set", v]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return None, helm_error(r.stderr)
    return r.stdout, None


def count_resources(text):
    """How many documents the render actually emitted, by `kind:` line."""
    return len([l for l in (text or "").split("\n") if l.strip().startswith("kind:")])


def defaults_verdict(name, err, ndocs, guard):
    """Verdict for the defaults arm. -> (kind, message), kind in finding|void|None.

    Pure, so selftest() can exercise all three outcomes without helm. THREE outcomes,
    deliberately: a render that emits nothing is not a pass, it is an arm that looked
    at nothing.
    """
    shown = " ".join("--set %s" % v for v in guard) or "(no values)"
    if err:
        return ("finding",
                "cannot be rendered from its own values.yaml defaults: `helm template "
                "%s` exits non-zero -- %s. A consumer running `helm install` without "
                "setting the optional values gets exactly this." % (shown, err))
    if ndocs == 0:
        return ("void",
                "guard render `%s` emitted NO documents, so this arm screened nothing "
                "-- the guard set is insufficient, not the chart clean" % shown)
    return (None, "")


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
        guard = CHART_GUARD_VALUES.get(name, [])
        gtext, gerr = render(c, values=guard)
        verdict, gmsg = defaults_verdict(name, gerr, count_resources(gtext), guard)
        if verdict == "finding":
            findings.append((name, gmsg, deprecated))
        elif verdict == "void":
            voids.append((name, gmsg))

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
        print("\nPASS -- every kind rendered by a live chart exists and is served, and\n       every live chart renders from its own values.yaml defaults.")
        if voids:
            print("       %d chart(s) were NOT screened, listed above." % len(voids))
    else:
        print("\nFAIL -- a live chart renders something that cannot be applied, or cannot\n       be rendered from its own defaults at all.")
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

    # ---- the defaults arm -------------------------------------------------
    # helm's real stderr for the defect this arm exists to catch, verbatim.
    real = ("WARNING: This chart is deprecated\n"
            "Error: template: baremetal-lifecycle/templates/node.yaml:6:21: executing "
            "\"baremetal-lifecycle/templates/node.yaml\" at <deepCopy .Values.instance_info>: "
            "error calling deepCopy: reflect: call of reflect.Value.Type on zero Value\n"
            "\n"
            "Use --debug flag to render out invalid YAML\n")
    case("helm_error picks the Error: line, NOT the trailing --debug hint",
         helm_error(real).startswith("Error: template:") and "--debug" not in helm_error(real))
    case("helm_error falls back to the last line when there is no Error:",
         helm_error("just a warning\nand another") == "and another")
    case("helm_error on empty stderr does not crash", helm_error("") == "helm failed")

    k, m = defaults_verdict("c", helm_error(real), 0, ["nodeName=b1"])
    case("a chart that errors from its defaults -> FINDING",
         k == "finding" and "own values.yaml defaults" in m and "deepCopy" in m)
    case("that finding shows the exact command a consumer would run",
         "--set nodeName=b1" in m)

    k, m = defaults_verdict("c", None, 0, [])
    case("renders but emits NOTHING -> VOID, not a pass",
         k == "void" and "screened nothing" in m)

    k, m = defaults_verdict("c", None, 3, ["nodeName=b1"])
    case("renders with documents -> no verdict", k is None)

    case("DISCRIMINATOR: defaults_verdict CAN return each of its three outcomes",
         defaults_verdict("c", "e", 1, [])[0] == "finding"
         and defaults_verdict("c", None, 0, [])[0] == "void"
         and defaults_verdict("c", None, 1, [])[0] is None)

    case("count_resources counts indented kinds too",
         count_resources("kind: A\n  kind: B\nnotkind: C\n") == 2)

    # TABLE INVARIANT: a guard value that is not in CHART_VALUES would mean the two
    # arms render different worlds, and the defaults arm could then report a defect
    # the other arm never sees -- or miss one it does.
    stray = [(c, v) for c, vs in CHART_GUARD_VALUES.items() for v in vs
             if c in CHART_VALUES and v not in CHART_VALUES[c]]
    case("every guard value also appears in CHART_VALUES", not stray)

    # Every chart on disk must have a guard entry, or the arm silently skips it with
    # an empty guard and calls whatever renders a pass.
    on_disk = {os.path.basename(x.rstrip("/"))
               for x in glob.glob(os.path.join(REPO, "charts/*/"))}
    missing = sorted(on_disk - set(CHART_GUARD_VALUES))
    case("every chart on disk has a guard entry (missing: %s)" % (missing or "none"),
         not missing and len(on_disk) > 0)

    for n, ok in cases:
        print("  %-62s %s" % (n, "ok" if ok else "FAIL"))
    print("selftest: %d cases, %d failure(s)" % (len(cases), len(failures)))
    for x in failures:
        print("  failed: %s" % x)
    return 1 if failures else 0


_WANT_SELFTEST = False

if __name__ == "__main__":
    # MODE FLAGS: both spellings accepted, anything else REFUSED.
    # This repo spells it `--selftest` on some screens and `--self-test` on others, and a
    # bare `in sys.argv` test SILENTLY RUNS THE OTHER MODE on a typo, exiting 0. That
    # produced two false conclusions in one session: a screen was reported as "not running
    # its controls" when the flag was merely misspelt, and a second was read as failing
    # when argparse had refused the argument. The argparse-based screens caught it; these
    # could not.
    #
    # Inlined rather than shared because the Makefile runs these under `python3 -I`, which
    # removes the script's directory from sys.path -- verified, not assumed -- so a sibling
    # import fails. Six duplicated lines are the price of that isolation.
    _ALLOWED = {"--self-test", "--selftest"}
    _unknown = [a for a in sys.argv[1:] if a not in _ALLOWED]
    if _unknown:
        sys.stderr.write("usage: %s [%s]\n" % (sys.argv[0], "|".join(sorted(_ALLOWED))))
        sys.stderr.write("refusing unrecognised argument(s): %s\n" % " ".join(_unknown))
        sys.stderr.write("a mistyped mode flag would otherwise run the OTHER mode and exit 0.\n")
        sys.exit(64)
    _WANT_SELFTEST = bool({"--self-test", "--selftest"} & set(sys.argv[1:]))
    if _WANT_SELFTEST:
        sys.exit(selftest())
    try:
        sys.exit(run())
    except Void as v:
        print("VOID: %s" % v)
        print("NOT a pass.")
        sys.exit(2)
