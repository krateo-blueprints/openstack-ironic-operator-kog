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

    lines, total = [], 0
    for it in sorted(items, key=lambda i: i["metadata"]["name"]):
        name = it["metadata"]["name"]
        warns = (it.get("status") or {}).get("schemaWarnings") or []
        gen = (it.get("status") or {}).get("generatorBuild", "?")
        if warns:
            lines.append(f"  {name}  ({len(warns)} warning(s), generatorBuild={gen})")
            lines += [f"      - {w}" for w in warns]
            total += len(warns)
        else:
            lines.append(f"  {name}  clean (generatorBuild={gen})")
    return lines, total


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
    cases = [
        ("no RestDefinitions must VOID", [], ok, Void),
        ("no schemaWarnings field must VOID", [_rd("a")], {"conditions": {}}, Void),
        ("no generatorBuild must VOID", [_rd("a", gen=None)], ok, Void),
        ("clean corpus must PASS with 0", [_rd("a"), _rd("b")], ok, 0),
        ("warnings must be COUNTED", [_rd("a", warns=["x", "y"]), _rd("b")], ok, 2),
    ]
    failures = 0
    for label, items, props, expect in cases:
        try:
            _, got = evaluate(items, props)
        except Void:
            got = Void
        bad = got != expect
        failures += bad
        shown = "VOID" if got is Void else got
        want = "VOID" if expect is Void else expect
        print(f"  [{'FAILED' if bad else 'ok'}] {label}  -> {shown} (want {want})")
    if failures:
        raise SystemExit(f"SELFTEST FAILED: {failures} of {len(cases)}")
    print(f"  selftest: {len(cases)}/{len(cases)} -- all three VOID paths can fire, "
          "and a clean corpus still passes")


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
        lines, total = evaluate(items, props)
    except Void as v:
        raise SystemExit(f"CONTROL FAILED: {v}")

    print(f"  controls passed: {len(items)} RestDefinition(s), schemaWarnings in the "
          f"schema, every one rendered\n")
    print("\n".join(lines))
    print()
    if total:
        raise SystemExit(
            f"FAIL: {total} schema warning(s) across the live RestDefinitions.\n"
            "These persist on the object and clear when fixed, so a non-zero count is\n"
            "current state, not history. Each one names a field or path that will not\n"
            "resolve at runtime."
        )
    print("  0 schema warnings, with every control passing.")


if __name__ == "__main__":
    main()
