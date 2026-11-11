#!/usr/bin/env python3
"""Read `status.schemaWarnings` off every live RestDefinition and fail if any is set.

WHY THIS EXISTS, AND WHY IT TOOK SO LONG TO EXIST
-------------------------------------------------
`docs/IMPLEMENTATION.md` said twice that oasgen reports generation warnings "as a
Kubernetes Warning Event and NOWHERE ELSE", and that "no condition carries them" --
so the diagnosis expired after the apiserver's 1h event TTL and the only way to see
one was to be looking within the hour after a render.

**Both statements were wrong.** oasgen writes the same warnings to
`status.schemaWarnings`, a real shipped status field, on EVERY render and
unconditionally -- so it persists indefinitely and it CLEARS when the cause is
fixed. Confirmed on the live cluster at 0.54.1: the field is in the CRD schema
alongside `conditions`, `digest` and `generatorBuild`, and it was holding NINE
warnings across three RestDefinitions that nothing in this repo had ever read --
including one saying a `get` verb addressed a path absent from its own OAS.

That is the whole lesson. The first telling was "it is silent"; the correction was
"it is an event, and nowhere else"; the truth is "it is a durable status field".
Each version was more specific than the last and two of the three were wrong.

THREE WAYS AN EMPTY RESULT CAN BE A LIE, each of which VOIDS rather than reporting
zero. The third is the nova session's, and it is the one I had missed:

    no RestDefinitions found        -> wrong cluster or wrong GVR
    no status.schemaWarnings field  -> an oasgen that does not populate it
    no status.generatorBuild        -> never rendered, so empty means UNKNOWN

`--selftest` proves all three can fire, and that a clean corpus still passes. A
control that cannot fail is worth less than no check, because it reports success.

Needs a live cluster, like regenerate-crds-check. Not a CI gate.
"""
import json
import shlex
import subprocess
import sys


class Void(Exception):
    """An empty result that is not evidence."""


# Warnings this repo CANNOT clear, because the cause is an upstream defect rather
# than a misconfiguration here. Each entry must name the issue and why.
#
# Why this list exists at all, from the nova session's gate design: a gate that
# merely FAILS forever on a known upstream cause trains people to ignore it, in the
# same way a gate that is red on main does. Theirs announces its own blind spot on
# every run -- "SKIPPED, and this is not a pass: oasgen 0.52.0 predates
# status.schemaWarnings, which first appears in 0.54.0" -- so the blind spot
# survives whoever knew about it. This is the same idea applied to a FAIL.
#
# The signature must be narrow enough that a DIFFERENT warning mentioning the same
# field still fails; `--selftest` has a case proving exactly that.
BLOCKED_UPSTREAM = [
    {
        "signature": "is absent from the items the findby response carries",
        "issue": "oasgen #575",
        "why": ("the check is computed from the declared schema and is blind to "
                "`responseTransform`, which is what actually supplies the identifier "
                "before matching (restclient.go:711, normalizeItems). Both "
                "ironic-node-power and ironic-node-provision carry the transform, so "
                "this warning is a FALSE POSITIVE on both and cannot be cleared here."),
    },
    {
        "signature": "status field 'node_id' not found in response",
        "issue": "oasgen #575",
        "why": ("same cause: /v1/nodes returns `name` and `uuid`, and the findby "
                "responseTransform manufactures `node_id` from `.name`. The generator "
                "does not evaluate the transform."),
    },
    {
        "signature": "set in configurationFields not found in schema",
        "issue": "immutable configurationFields (see docs/IMPLEMENTATION.md §9.13b)",
        "why": ("the dead X-Ironic-Endpoint entry is removed in the manifests, but "
                "`configurationFields` is IMMUTABLE once the CRD has been generated -- "
                "the apply is refused. It clears only on a fresh install."),
    },
]


def classify(warning):
    """Return the BLOCKED_UPSTREAM entry this warning matches, or None."""
    for e in BLOCKED_UPSTREAM:
        if e["signature"] in warning:
            return e
    return None


def evaluate(items, status_props):
    """Pure core, so --selftest can drive it. Returns (lines, total_warnings).
    Raises Void when an empty result would not be evidence."""
    if not items:
        raise Void("zero RestDefinitions found. Either the cluster is wrong or the "
                   "GVR is -- a 'no warnings' result from an empty list is not a "
                   "measurement.")
    if "schemaWarnings" not in status_props:
        raise Void("this oasgen's RestDefinition CRD has no status.schemaWarnings "
                   "field, so an empty result says nothing. Upgrade oasgen or drop "
                   "this check.")
    unrendered = [i["metadata"]["name"] for i in items
                  if not (i.get("status") or {}).get("generatorBuild")]
    if unrendered:
        raise Void("these RestDefinitions carry no status.generatorBuild, so their "
                   "empty warning list means UNKNOWN rather than clean:\n    "
                   + "\n    ".join(unrendered)
                   + "\nForce a render (touch the OAS or the spec) before trusting "
                     "a zero.")

    lines, actionable, blocked = [], 0, 0
    for it in sorted(items, key=lambda i: i["metadata"]["name"]):
        name = it["metadata"]["name"]
        warns = (it.get("status") or {}).get("schemaWarnings") or []
        gen = (it.get("status") or {}).get("generatorBuild", "?")
        if warns:
            lines.append(f"  {name}  ({len(warns)} warning(s), generatorBuild={gen})")
            for w in warns:
                e = classify(w)
                if e:
                    blocked += 1
                    lines.append(f"      - BLOCKED-UPSTREAM [{e['issue']}] {w}")
                else:
                    actionable += 1
                    lines.append(f"      - ACTIONABLE {w}")
        else:
            lines.append(f"  {name}  clean (generatorBuild={gen})")
    return lines, actionable, blocked


def kubectl(cmd, args):
    out = subprocess.run(shlex.split(cmd) + args, capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"kubectl failed: {out.stderr.strip()[:400]}")
    return out.stdout


def _rd(name, gen="0.54.1-abc", warns=None):
    st = {}
    if gen is not None:
        st["generatorBuild"] = gen
    if warns is not None:
        st["schemaWarnings"] = warns
    return {"metadata": {"name": name}, "status": st}


def selftest():
    ok = {"schemaWarnings": {}, "generatorBuild": {}}
    BLOCKED = "identifier \"node_id\" is absent from the items the findby response carries"
    # Mentions node_id and findby but is NOT the blocked signature -- must stay ACTIONABLE.
    # Without this case the signature could be widened until it swallows real warnings,
    # and every other arm would still pass.
    NEAR_MISS = "identifier \"node_id\" is absent from the get response, so get cannot resolve"
    cases = [
        ("no RestDefinitions must VOID", [], ok, Void),
        ("no schemaWarnings field must VOID", [_rd("a")], {"conditions": {}}, Void),
        ("no generatorBuild must VOID", [_rd("a", gen=None)], ok, Void),
        ("clean corpus must PASS 0 actionable / 0 blocked", [_rd("a"), _rd("b")], ok, (0, 0)),
        ("unknown warnings are ACTIONABLE", [_rd("a", warns=["x", "y"])], ok, (2, 0)),
        ("the #575 signature is BLOCKED-UPSTREAM", [_rd("a", warns=[BLOCKED])], ok, (0, 1)),
        ("a NEAR MISS must stay ACTIONABLE", [_rd("a", warns=[NEAR_MISS])], ok, (1, 0)),
        ("a blocked warning must not mask an actionable one",
         [_rd("a", warns=[BLOCKED, "something real"])], ok, (1, 1)),
    ]
    failures = 0
    for label, items, props, expect in cases:
        try:
            _, a, b = evaluate(items, props)
            got = (a, b)
        except Void:
            got = Void
        bad = got != expect
        failures += bad
        shown = "VOID" if got is Void else got
        want = "VOID" if expect is Void else expect
        print(f"  [{'FAILED' if bad else 'ok'}] {label}  -> {shown} (want {want})")
    if failures:
        raise SystemExit(f"SELFTEST FAILED: {failures} of {len(cases)}")
    print(f"  selftest: {len(cases)}/{len(cases)} -- three VOID paths fire, a clean corpus "
          "passes, blocked-upstream is classified, a near miss stays actionable, and a "
          "blocked warning cannot mask a real one")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        return selftest()

    # The caller passes the full kubectl invocation, so --kubeconfig/--context
    # isolation is preserved and ~/.kube/config is never consulted.
    cmd = sys.argv[1] if len(sys.argv) > 1 else "kubectl"

    items = json.loads(kubectl(cmd, ["get", "restdefinitions.v1alpha1.ogen.krateo.io",
                                     "-A", "-o", "json"])).get("items", [])
    crd = json.loads(kubectl(cmd, ["get", "crd", "restdefinitions.ogen.krateo.io",
                                   "-o", "json"]))
    props = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"][
        "properties"]["status"]["properties"]

    try:
        lines, actionable, blocked = evaluate(items, props)
    except Void as v:
        raise SystemExit(f"CONTROL FAILED: {v}")

    print(f"  controls passed: {len(items)} RestDefinition(s), schemaWarnings in the "
          f"schema, every one rendered\n")
    print("\n".join(lines))

    # THE BLIND SPOT, announced on EVERY run including a passing one, so it survives
    # whoever knew about it rather than living in a PR description nobody re-reads.
    print("\n  BLIND SPOT -- this gate cannot reach zero while these are open:")
    for e in BLOCKED_UPSTREAM:
        print(f"      [{e['issue']}] {e['why']}")
    print(f"\n  actionable={actionable}  blocked-upstream={blocked}")

    if actionable:
        raise SystemExit(
            f"FAIL: {actionable} ACTIONABLE schema warning(s).\n"
            "These persist on the object and clear when fixed, so a non-zero count is\n"
            "current state, not history. Each names a field or path that will not\n"
            "resolve at runtime. The blocked-upstream ones above are NOT counted here\n"
            "and are NOT a pass -- they are a defect someone else has to fix."
        )
    if blocked:
        print(f"  PASS with {blocked} blocked-upstream warning(s). NOT a clean sheet:\n"
              "  nothing here can clear them, and they will reappear on any fresh render.")
    else:
        print("  0 schema warnings, with every control passing.")


if __name__ == "__main__":
    main()
