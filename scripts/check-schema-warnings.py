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
        "signature": "is omitted from the generated spec because verb",
        "issue": "oasgen path-param suppression (measured, not filed)",
        "why": ("MEASURED THREE WAYS and benign for every kind in this repo. A "
                "requestFieldMapping suppresses the path parameter's own spec property; "
                "only an independent request-BODY declaration rescues it. For "
                "Allocation, DeployTemplate and PortGroup the suppressed property is "
                "`uuid`, which is supplied from `spec.name` by the mapping and surfaced "
                "through additionalStatusFields -- so no author ever sets it and nothing "
                "is lost. The warning's own suggested remedies are both WRONG here: "
                "there is nothing to remove from the CR, and removing the fieldMapping "
                "would break get/delete. A warning on a correct configuration.\n"
                "Three independent channels agree it is this mechanism: live CRD dumps "
                "(2026-11-04), static OAS reading (scripts/check-restdefinition-shape.py "
                "check E), and this generator warning itself (2026-11-12)."),
    },
    {
        "signature": "is absent from the items the findby response carries",
        "issue": "oasgen #575 (power) / BY DESIGN (provision)",
        "why": ("STALE UNTIL 2026-11-13, in the dangerous direction. This entry read "
                "\"Both ironic-node-power and ironic-node-provision carry the transform, "
                "so this warning is a FALSE POSITIVE on both\" -- which stopped being "
                "true when #65 REMOVED provision's findby responseTransform. It was "
                "classifying a literally-true warning as benign on a basis that no "
                "longer held.\n"
                "The two kinds need OPPOSITE reasons:\n"
                "  POWER -- false positive. It carries `if (.name and .power_state) then "
                ".node_id = .name else . end`, and the generator does not evaluate "
                "responseTransform (restclient.go:711, normalizeItems applies it before "
                "matching).\n"
                "  PROVISION -- LITERALLY TRUE, AND INTENDED. No findby transform, "
                "deliberately: with no `get` and no `update`, its create is dispatched "
                "only when Observe reports the resource ABSENT, so a findby that never "
                "matches is what fires the provision PUT. Making findby match disabled "
                "every transition for 24 hours (#65). The warning's own advice IS the "
                "outage.\n"
                "Accepted for both kinds for opposite reasons, which is why one "
                "signature cannot carry one justification."),
    },
    {
        "signature": "status field 'node_id' not found in response",
        "issue": "oasgen #575 (power) / BY DESIGN (provision)",
        "why": ("POWER: false positive -- /v1/nodes returns `name` and `uuid`, and its "
                "findby responseTransform manufactures `node_id` from `.name`; the "
                "generator does not evaluate the transform.\n"
                "PROVISION: true and intended, same reason as the entry above -- no "
                "transform, so `node_id` really is absent from the findby response. "
                "Nothing reads NodeProvision status; the charts read provision_state off "
                "the BaremetalNode CR."),
    },
    {
        "signature": "status field 'target_provision_state' not found in response",
        "issue": "BY DESIGN -- the declaration is kept ON PURPOSE so this warning survives",
        "why": ("THIS WARNING EXISTS BECAUSE THE DECLARATION WAS KEPT, and that was a "
                "decision, not an oversight.\n"
                "`target_provision_state` is in NodeProvision's additionalStatusFields and "
                "can never populate: the kind's only reader is findby, which never matches "
                "by design, and since 2026-11-13 the OAS no longer falsely claims "
                "/v1/nodes carries it (MEASURED on two clusters: the collection returns 7 "
                "keys, the single-node GET 69). Correcting that document is what made the "
                "generator able to say so -- before, the false schema suppressed it.\n"
                "REMOVING the entry would make the manifest fully honest AND SILENCE THIS "
                "WARNING, and a silent document is indistinguishable from a tool that is "
                "not looking. Keeping it preserves a claim for the generator to contradict "
                "-- the only standing, machine-checked record that this kind's status "
                "cannot populate. The manifest comment says the same thing, but nothing "
                "checks a comment.\n"
                "SO THIS ENTRY IS TWO-WAY. If the warning ever DISAPPEARS, something "
                "changed worth knowing: either somebody removed the declaration (fine, but "
                "deliberately) or Ironic began returning the field in the collection, which "
                "would mean the provision kind could finally be made observable. Framing "
                "owed to openstack-nova-operator-kog, who named the tension between an "
                "honest document and a surviving diagnostic."),
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
        # EVERY REGISTRY ENTRY MUST BE REACHABLE. An entry whose signature has a typo
        # never matches, so its warning silently reads as ACTIONABLE forever -- or, worse,
        # a future entry meant to accept something quietly accepts nothing. The
        # target_provision_state entry is the one that exists to be a STANDING two-way
        # diagnostic, so it is the one that most needs to be shown to fire.
        ("the kept-on-purpose target_provision_state warning is classified",
         [_rd("a", warns=["generation error at additionalStatusFields."
                          "target_provision_state: status field "
                          "'target_provision_state' not found in response, "
                          "defaulting to string"])], ok, (0, 1)),
    ]

    # ... and that the real generator text matches EVERY registry signature, not just
    # the one above. A signature that matches nothing is a dead exemption.
    for _e in BLOCKED_UPSTREAM:
        if not _e["signature"].strip():
            raise SystemExit("a BLOCKED_UPSTREAM entry has an empty signature, which "
                             "would classify every warning as accepted")
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
