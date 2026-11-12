#!/usr/bin/env python3
"""Standing screen: the release workflow's chart lists agree with each chart's
own `krateo.io/publish` annotation.

WHY THIS EXISTS. `.github/workflows/release-chart.yaml` publishes a HAND-MAINTAINED
list, written out twice -- once for `helm lint`, once for `helm package` (the push
step globs `dist/`, so it follows package):

    for dir in charts/baremetal-discovery charts/baremetal-host \\
               charts/baremetal-lifecycle charts/kubernetes-cluster; do

A chart added to `charts/` and not to that list is **never published, silently**. It
lints clean, `make check` passes, the tag succeeds, and the artifact simply never
appears in the registry -- a failure whose only symptom is an absence, discovered by
whoever tries to install it.

This repo has already paid for the same shape once in a different place:
`make apply-restdef` applied four of five RestDefinitions, omitting `power`, while
NodePower was live in the cluster. Nothing re-applies a RestDefinition in normal
operation, so the broken target was indistinguishable from a working one for weeks.
A hand-maintained list rots, and it rots quietly.

THE FIX IS NOT TO REPLACE THE LIST WITH A GLOB. Publishing is an outward-facing,
irreversible act -- an artifact pushed to GHCR cannot be withdrawn, as tag 0.3.7
proved by shipping a broken `kubernetes-cluster` 0.15.2. A glob would mean a new
directory publishes itself with no review. The list stays explicit in the pipeline,
where changing it is a reviewable diff; each chart DECLARES its intent in its own
Chart.yaml; and this screen makes any disagreement loud and offline.

WHAT IT CHECKS
  1. every chart under charts/ carries a `krateo.io/publish` annotation ("true"/"false")
  2. the set annotated true == the workflow's `helm package` list
  3. the `helm lint` list == the `helm package` list (they are written out separately
     and could drift from each other without either drifting from the annotations)

VOID, never a silent pass, when: no charts are found, the workflow is unreadable, or
no chart list can be parsed out of it. A screen that cannot find its subject must not
report agreement.
"""
import argparse, glob, os, re, sys

try:
    import yaml
except ImportError:
    print("check-publish-set: PyYAML missing -- VOID, not a pass", file=sys.stderr)
    sys.exit(3)

WORKFLOW = ".github/workflows/release-chart.yaml"


class Void(Exception):
    """The screen could not form an opinion. Never a pass."""


def chart_intent(chart_dir):
    """('name', True/False) or ('name', None) when the annotation is absent."""
    p = os.path.join(chart_dir, "Chart.yaml")
    d = yaml.safe_load(open(p)) or {}
    name = d.get("name") or os.path.basename(chart_dir.rstrip("/"))
    raw = (d.get("annotations") or {}).get("krateo.io/publish")
    if raw is None:
        return name, None
    if str(raw).lower() not in ("true", "false"):
        raise Void("%s: krateo.io/publish is %r, expected \"true\" or \"false\"" % (p, raw))
    return name, str(raw).lower() == "true"


def workflow_lists(text):
    """Every `for dir in charts/... ; do` list in the workflow, by the step that holds it.

    Returns {step_name: {chart names}}. Raises Void if none can be parsed, because a
    changed workflow shape must not read as agreement.
    """
    out, current = {}, "(before any named step)"
    for line in text.split("\n"):
        m = re.match(r"\s*-\s+name:\s*(.+?)\s*$", line)
        if m:
            current = m.group(1)
        if "for dir in" in line and "charts/" in line:
            names = set(re.findall(r"charts/([A-Za-z0-9._-]+)", line))
            if names:
                out.setdefault(current, set()).update(names)
    if not out:
        raise Void("no `for dir in charts/...` list found in %s -- the workflow's shape "
                   "changed, so this screen cannot tell agreement from absence" % WORKFLOW)
    return out


def evaluate(intents, lists):
    """Pure core. Returns a list of findings; raises Void when undecidable."""
    if not intents:
        raise Void("no charts found under charts/")
    missing = sorted(n for n, v in intents.items() if v is None)
    if missing:
        raise Void("these charts carry no krateo.io/publish annotation, so the "
                   "published set is undeclared: %s" % ", ".join(missing))

    declared = {n for n, v in intents.items() if v}
    findings = []

    pkg_steps = [s for s in lists if "package" in s.lower()]
    if not pkg_steps:
        raise Void("no `helm package` step with a chart list; found steps: %s"
                   % ", ".join(sorted(lists)))
    packaged = set().union(*(lists[s] for s in pkg_steps))

    for n in sorted(declared - packaged):
        findings.append("%s declares krateo.io/publish=true but the workflow's package "
                        "list omits it -- it would NEVER be published, silently" % n)
    for n in sorted(packaged - declared):
        findings.append("%s is in the workflow's package list but declares "
                        "krateo.io/publish=false (or is not a chart here)" % n)

    lint_steps = [s for s in lists if "lint" in s.lower()]
    if lint_steps:
        linted = set().union(*(lists[s] for s in lint_steps))
        for n in sorted(linted ^ packaged):
            side = "lint only" if n in linted else "package only"
            findings.append("%s appears in %s -- the two hand-written lists have drifted "
                            "from each other" % (n, side))
    return findings, declared, packaged


def run():
    dirs = sorted(d for d in glob.glob("charts/*") if os.path.isfile(os.path.join(d, "Chart.yaml")))
    intents = {}
    for d in dirs:
        n, v = chart_intent(d)
        intents[n] = v
    try:
        text = open(WORKFLOW).read()
    except OSError as e:
        raise Void("cannot read %s (%s)" % (WORKFLOW, e))
    lists = workflow_lists(text)

    findings, declared, packaged = evaluate(intents, lists)
    print("checked %d chart(s): %s" % (len(intents), ", ".join(sorted(intents))))
    print("  declared publish=true : %s" % ", ".join(sorted(declared)))
    print("  workflow package list : %s" % ", ".join(sorted(packaged)))
    print("  not published, by declaration: %s"
          % (", ".join(sorted(n for n, v in intents.items() if v is False)) or "(none)"))
    if findings:
        print("\nACTIONABLE (%d):" % len(findings))
        for f in findings:
            print("  " + f)
        return 1
    print("\nPASS -- every chart declares its intent and the workflow agrees.")
    return 0


# ------------------------------------------------------------------ selftest
def selftest():
    WF = """
      - name: helm lint (all charts)
        run: |
          for dir in charts/a charts/b; do helm lint "$dir"; done
      - name: helm package (all charts)
        run: |
          for dir in charts/a charts/b; do helm package "$dir"; done
    """
    cases, failures = [], []

    def case(name, fn, want):
        try:
            got = fn()
        except Void as v:
            got = ("VOID", str(v))
        ok = (got == want) if not isinstance(want, str) else (got[0] == "VOID")
        cases.append((name, ok))
        if not ok:
            failures.append("%s: got %r, wanted %r" % (name, got, want))

    # agreement -> no findings
    case("clean: annotations match the workflow",
         lambda: evaluate({"a": True, "b": True}, workflow_lists(WF))[0], [])
    # a chart declared true but absent from the package list -- THE defect
    # ONE finding, not two: `c` is declared-but-omitted, and the lint/package lists
    # still agree with each other, so the drift check stays silent. My first
    # expectation here was 2 and the arm caught my arithmetic rather than the walker.
    case("declared true, omitted from package -> ACTIONABLE",
         lambda: len(evaluate({"a": True, "b": True, "c": True}, workflow_lists(WF))[0]), 1)
    # a chart in the list but declared false
    case("in the list but declared false -> ACTIONABLE",
         lambda: len(evaluate({"a": True, "b": False}, workflow_lists(WF))[0]), 1)
    # the two hand-written lists drift from EACH OTHER
    WF_DRIFT = WF.replace('for dir in charts/a charts/b; do helm lint', 'for dir in charts/a; do helm lint')
    case("lint and package lists drift -> ACTIONABLE",
         lambda: len(evaluate({"a": True, "b": True}, workflow_lists(WF_DRIFT))[0]), 1)
    # VOIDs
    case("VOID: a chart with no annotation",
         lambda: evaluate({"a": True, "b": None}, workflow_lists(WF)), "VOID")
    case("VOID: no charts at all",
         lambda: evaluate({}, workflow_lists(WF)), "VOID")
    case("VOID: the workflow has no chart list",
         lambda: workflow_lists("- name: nothing\n  run: echo hi"), "VOID")
    case("VOID: no package step",
         lambda: evaluate({"a": True}, {"helm lint (all charts)": {"a"}}), "VOID")

    # The real repo's cleanliness is NOT asserted here -- `make check` runs the real
    # pass right after this one, with `&&`, so a red repo fails there. An earlier
    # draft of this function had a `real = run.__wrapped__ ...` block that computed
    # nothing and was never read: a dead value that looked like a control.

    for n, ok in cases:
        print("  %-52s %s" % (n, "ok" if ok else "FAIL"))
    print("  selftest: %d cases, %d failure(s)" % (len(cases), len(failures)))
    for f in failures:
        print("    " + f)
    return 1 if failures else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    try:
        sys.exit(run())
    except Void as v:
        print("VOID: %s" % v)
        print("NOT a pass.")
        sys.exit(2)
