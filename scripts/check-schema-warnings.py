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
warnings across three RestDefinitions that nothing in this repo had ever read.

That is the whole lesson. The first telling was "it is silent"; the correction was
"it is an event, and nowhere else"; the truth is "it is a durable status field".
Each version was more specific than the last and two of the three were wrong --
see the `a-correction-can-be-the-error` note.

Needs a live cluster, like `regenerate-crds-check`. Not a CI gate.
"""
import json
import shlex
import subprocess
import sys


def kubectl(cmd, args):
    out = subprocess.run(shlex.split(cmd) + args, capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"kubectl failed: {out.stderr.strip()[:400]}")
    return out.stdout


def main():
    # The caller passes the full kubectl invocation, so --kubeconfig/--context
    # isolation is preserved and ~/.kube/config is never consulted.
    cmd = sys.argv[1] if len(sys.argv) > 1 else "kubectl"

    raw = kubectl(cmd, ["get", "restdefinitions.v1alpha1.ogen.krateo.io",
                        "-A", "-o", "json"])
    items = json.loads(raw).get("items", [])

    # CONTROL: an empty list here would otherwise read as "no warnings anywhere".
    if not items:
        raise SystemExit("CONTROL FAILED: zero RestDefinitions found. Either the "
                         "cluster is wrong or the GVR is -- a 'no warnings' result "
                         "from an empty list is not a measurement.")

    # CONTROL: prove the field is in the schema, so an all-empty result distinguishes
    # "nothing is wrong" from "this oasgen does not populate the field".
    crd = json.loads(kubectl(cmd, ["get", "crd", "restdefinitions.ogen.krateo.io",
                                   "-o", "json"]))
    props = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"][
        "properties"]["status"]["properties"]
    if "schemaWarnings" not in props:
        raise SystemExit("CONTROL FAILED: this oasgen's RestDefinition CRD has no "
                         "status.schemaWarnings field, so an empty result says "
                         "nothing. Upgrade oasgen or drop this check.")

    print(f"  control: {len(items)} RestDefinition(s); status.schemaWarnings is in the schema\n")

    total = 0
    for it in sorted(items, key=lambda i: i["metadata"]["name"]):
        name = it["metadata"]["name"]
        warns = (it.get("status") or {}).get("schemaWarnings") or []
        gen = (it.get("status") or {}).get("generatorBuild", "?")
        if warns:
            print(f"  {name}  ({len(warns)} warning(s), generatorBuild={gen})")
            for w in warns:
                print(f"      - {w}")
            total += len(warns)
        else:
            print(f"  {name}  clean (generatorBuild={gen})")

    print()
    if total:
        raise SystemExit(
            f"FAIL: {total} schema warning(s) across the live RestDefinitions.\n"
            "These persist on the object and clear when fixed, so a non-zero count is\n"
            "current state, not history. Each one names a field or path that will not\n"
            "resolve at runtime."
        )
    print("  0 schema warnings, with both controls passing.")


if __name__ == "__main__":
    main()
