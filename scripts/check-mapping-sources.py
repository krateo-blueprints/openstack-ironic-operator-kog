#!/usr/bin/env python3
"""Every request-direction fieldMapping must have a source that EXISTS.

A mapping declares `inPath: X <- inCustomResource: spec.Y`. Declaring it has a
side effect: oasgen removes X's spec property (mappingSuppliedParams). So if
Y == X, the mapping's own declaration deletes the field it reads from, and the
parameter resolves to nothing at request time -- oasgen-provider #419.

This screen reads the DOCUMENTS, so it answers before a single API call. It is
the document-level form of the check oasgen is building as a generation warning
(#445), and it is the one thing that would have caught #419 here in October
instead of after a 406 went unnoticed for five days.

Calibration: `--self-test`, like the repo's three other screens. This was the
only one without one -- it used a KNOWN_BROKEN set as a substitute, declaring
that "a run reporting zero problems means the screen broke". That substitute was
falsified on 2026-11-07 and is gone; see below.

THE CALIBRATION IT USED TO HAVE, AND WHY IT WENT. It listed two mappings as
KNOWN_BROKEN -- power and provision, `node_id <- spec.node_id` -- on the grounds
that oasgen #419 suppressed the spec property, #421 was merged but unreleased,
the deployed generator was 0.50.0, and an upgrade does not regenerate an
existing CRD (#453). Every rung was true when written and three are now false:
#421 shipped in 0.52.0, this cluster runs 0.54.1, and the served CRDs did
regenerate. Measured 2026-11-07 with the one-command check below:

    nodepowers       v1-0-0  node_id present=True  required=True
    nodeprovisions   v1-0-0  node_id present=True  required=True

This docstring said "when that prints True for both, and only then, empty
KNOWN_BROKEN". It prints True for both, so KNOWN_BROKEN is empty and a clean run
is good news rather than an instrument failure. /code-review caught that leaving
it populated would make `make check` exit 2 with "SCREEN BROKEN" on a healthy
repo.

The lesson kept: a clean run must still be distinguishable from a broken screen,
and a hand-maintained list of expected failures is a poor way to do it -- it
rots silently, and it rots into a FALSE ALARM rather than a miss. A self-test is
the honest form, because it proves the instrument fires on input it builds
itself.

Regenerating is a separate action: the referenced OAS ConfigMap IS hashed
(`status.oasHash`), so editing it is the supported nudge. See docs/
IMPLEMENTATION.md §9.
"""
import glob
import os
import re
import sys

import yaml

# EMPTY as of 2026-11-07, intentionally -- see the docstring. Both mappings it
# used to list are fine on the stored side now: oasgen 0.54.1 contains #421 and
# the served CRDs carry node_id, present and required. Anything reported is
# therefore NEW, and `--self-test` is what makes a clean run meaningful.
KNOWN_BROKEN = set()


def oas_for(oaspath, root="."):
    m = re.match(r"configmap://[^/]+/([^/]+)/", oaspath)
    cm = m.group(1) if m else ""
    base = cm[:-4] if cm.endswith("-oas") else cm
    p = os.path.join(root, f"oas/{base}.yaml")
    return p if os.path.exists(p) else None


def audit(root="."):
    found, unresolved = [], []
    for f in sorted(glob.glob(os.path.join(root, "manifests/restdefinition-*.yaml"))):
        for d in [x for x in yaml.safe_load_all(open(f)) if x and x.get("kind") == "RestDefinition"]:
            r = d["spec"]["resource"]
            if not oas_for(d["spec"]["oasPath"], root):
                unresolved.append(os.path.basename(f))
                continue
            excluded = set(r.get("excludedSpecFields") or [])
            status = set(r.get("additionalStatusFields") or []) | set(r.get("identifiers") or [])
            verbs = r.get("verbsDescription") or []
            # A mapping on ANY verb removes the name from the spec for ALL verbs
            # (oasgen #457), so suppression is computed across the whole
            # resource. Still true for the non-path-parameter case; #421 only
            # restores path parameters.
            mapped_any = {m.get("inPath") for vv in verbs
                          for k in ("fieldMapping", "requestFieldMapping")
                          for m in (vv.get(k) or []) if m.get("inCustomResource")}
            for v in verbs:
                for k in ("fieldMapping", "requestFieldMapping"):
                    for m in (v.get(k) or []):
                        src, name = m.get("inCustomResource") or "", m.get("inPath")
                        # IS THIS A PATH PARAMETER? Decidable from the document:
                        # it is one iff `{name}` appears in this verb's own path.
                        # That matters because #421 restores a suppressed PATH
                        # parameter's spec property and nothing else -- so the
                        # suppression rule still applies to a mapping whose
                        # inPath names a request-BODY field.
                        #
                        # The previous commit dropped the rule for EVERY mapping
                        # on the strength of measuring exactly two, both path
                        # params, and /code-review caught the over-generalisation:
                        # with KNOWN_BROKEN emptied in the same change, a
                        # reintroduced body-field suppression would have read as
                        # a clean run.
                        in_path = ("{" + str(name) + "}") in (v.get("path") or "")
                        if src.startswith("spec."):
                            fld = src[5:]
                            # THE MAPPING-SUPPRESSION RULE IS GONE, and that is a
                            # measurement, not an assumption. It held under oasgen
                            # #419: declaring `inPath X <- spec.X` removed X's spec
                            # property, so the mapping deleted its own source. #421
                            # restores a path parameter's property -- as `required`
                            # -- and shipped in 0.52.0. Every mapping this screen
                            # examines is an `inPath` mapping, i.e. a path
                            # parameter, so #421 covers all of them and the rule has
                            # no remaining case.
                            #
                            # Measured 2026-11-07 on the served CRDs, which is the
                            # only rung that decides it:
                            #   nodepowers      node_id present=True required=True
                            #   nodeprovisions  node_id present=True required=True
                            #
                            # `excludedSpecFields` suppression is NOT affected and
                            # is still checked: that removes the property outright
                            # and no generator fix restores it.
                            if fld in excluded:
                                ok, why = False, "excluded"
                            elif not in_path and fld in mapped_any:
                                # Suppressed by its own declaration and NOT
                                # restored by #421, because it is not a path
                                # parameter of this verb.
                                ok, why = False, "mapping-suppressed (not a path parameter)"
                            else:
                                ok, why = True, ""
                        elif src.startswith("status."):
                            fld = src[7:]
                            ok = fld in status
                            why = "not in additionalStatusFields or identifiers"
                        else:
                            ok, why = False, "unrecognised source"
                        if not ok:
                            found.append((os.path.basename(f), name, src, why))
    return found, unresolved


SELF_TEST_CASES = [
    # (name, restdefinition spec dict, must_be_reported)
    # NOTE both of these carry an explicit `path`. They did not, and the
    # sharpened rule rightly reported them: without `{node_id}` in the path a
    # mapping is not a path parameter, so #421 does not restore it. The fixtures
    # were underspecified, not the rule -- the real verbs are
    # `/v1/nodes/{node_id}/states/power`.
    ("path mapping from its own name is FINE under #421 (the old false alarm)",
     {"identifiers": ["node_id"],
      "verbsDescription": [{"action": "create", "method": "PUT",
                            "path": "/v1/nodes/{node_id}/states/power",
                            "requestFieldMapping":
                            [{"inPath": "node_id", "inCustomResource": "spec.node_id"}]}]},
     False),
    ("the same PATH mapping on two verbs is still fine, and fires nothing",
     {"identifiers": ["node_id"],
      "verbsDescription": [
          {"action": "create", "method": "PUT",
           "path": "/v1/nodes/{node_id}/states/power", "requestFieldMapping":
           [{"inPath": "node_id", "inCustomResource": "spec.node_id"}]},
          {"action": "update", "method": "PUT",
           "path": "/v1/nodes/{node_id}/states/power", "requestFieldMapping":
           [{"inPath": "node_id", "inCustomResource": "spec.node_id"}]}]},
     False),
    ("a BODY-field mapping suppressed by its own declaration -- MUST fire "
     "(#421 restores path params only, and `target` is not in this path)",
     {"verbsDescription": [{"action": "create", "method": "PUT",
                            "path": "/v1/nodes/{node_id}/states/power",
                            "requestFieldMapping": [
                                {"inPath": "node_id", "inCustomResource": "spec.node_id"},
                                {"inPath": "target", "inCustomResource": "spec.target"}]}]},
     True),
    ("the SAME excluded mapping on two verbs fires once, not twice -- covers the "
     "dedup display added for the power kind",
     {"excludedSpecFields": ["uuid"],
      "verbsDescription": [
          {"action": "get", "method": "GET", "path": "/v1/nodes/{uuid}",
           "requestFieldMapping": [{"inPath": "uuid", "inCustomResource": "spec.uuid"}]},
          {"action": "delete", "method": "DELETE", "path": "/v1/nodes/{uuid}",
           "requestFieldMapping": [{"inPath": "uuid", "inCustomResource": "spec.uuid"}]}]},
     True),
    ("excludedSpecFields really does remove the source -- MUST fire",
     {"excludedSpecFields": ["uuid"],
      "verbsDescription": [{"action": "get", "requestFieldMapping":
                            [{"inPath": "uuid", "inCustomResource": "spec.uuid"}]}]},
     True),
    ("status source present in additionalStatusFields is FINE",
     {"additionalStatusFields": ["uuid"],
      "verbsDescription": [{"action": "get", "requestFieldMapping":
                            [{"inPath": "uuid", "inCustomResource": "status.uuid"}]}]},
     False),
    ("status source absent from additionalStatusFields -- MUST fire",
     {"additionalStatusFields": ["power_state"],
      "verbsDescription": [{"action": "get", "requestFieldMapping":
                            [{"inPath": "uuid", "inCustomResource": "status.uuid"}]}]},
     True),
    ("status source satisfied by identifiers is FINE",
     {"identifiers": ["name"],
      "verbsDescription": [{"action": "get", "requestFieldMapping":
                            [{"inPath": "ident", "inCustomResource": "status.name"}]}]},
     False),
    ("an unrecognised source -- MUST fire",
     {"verbsDescription": [{"action": "get", "requestFieldMapping":
                            [{"inPath": "x", "inCustomResource": "metadata.name"}]}]},
     True),
]


def self_test():
    """Prove the screen fires on input it builds itself.

    The three other screens in this repo have one of these; this was the only
    one without, and it used a hand-maintained KNOWN_BROKEN list instead. That
    list rotted into a false alarm -- see the module docstring.
    """
    import shutil
    import tempfile
    fails = 0
    for name, resource, must_fire in SELF_TEST_CASES:
        root = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(root, "manifests"))
            os.makedirs(os.path.join(root, "oas"))
            # oas_for() maps configmap://ns/<base>-oas/ -> oas/<base>.yaml
            open(os.path.join(root, "oas", "selftest.yaml"), "w").write("openapi: 3.0.0\n")
            doc = {"apiVersion": "ogen.krateo.io/v1alpha1", "kind": "RestDefinition",
                   "spec": {"oasPath": "configmap://ns/selftest-oas/x.yaml",
                            "resource": dict({"kind": "SelfTest"}, **resource)}}
            with open(os.path.join(root, "manifests", "restdefinition-selftest.yaml"), "w") as fh:
                yaml.safe_dump(doc, fh)
            found, unresolved = audit(root)
            fired = bool(found)
            if fired != must_fire:
                print(f"  FAIL  {name}: expected fire={must_fire}, got {fired} {found}")
                fails += 1
            # the dedupe case additionally asserts the COUNT
            if "fires once, not twice" in name:
                raw = len(found)
                uniq = len({(f, n, s) for f, n, s, _ in found})
                if not (raw == 2 and uniq == 1):
                    print(f"  FAIL  {name}: expected 2 raw findings deduping to 1, "
                          f"got raw={raw} uniq={uniq} {found}")
                    fails += 1
        finally:
            shutil.rmtree(root, ignore_errors=True)
    print(f"  self-test: {len(SELF_TEST_CASES)} cases, {fails} failure(s)")
    return 1 if fails else 0


def main():
    if "--self-test" in sys.argv:
        return self_test()
    found, unresolved = audit()
    for f in unresolved:
        print(f"  WARN {f}: OAS document not resolved -- not audited, not cleared")
    # Deduped for display. It used to print one line per VERB while the summary
    # counted a deduped SET, so power -- which carries the same mapping on create
    # and update -- printed twice under a summary saying "exactly the 2 known".
    seen = {}
    for f, name, src, why in found:
        seen.setdefault((f, name, src), [why, 0])[1] += 1
    for (f, name, src), (why, n) in sorted(seen.items()):
        tag = "known" if (f, name, src) in KNOWN_BROKEN else "*** NEW ***"
        on = f"  (on {n} verbs)" if n > 1 else ""
        print(f"  {tag:14} {f}: {name} <- {src}  ({why}){on}")

    got = {(f, n, s) for f, n, s, _ in found}
    new = got - KNOWN_BROKEN
    missing = KNOWN_BROKEN - got

    if missing:
        print(f"\n  SCREEN BROKEN: expected to find {sorted(missing)} and did not.")
        print("  KNOWN_BROKEN is non-empty, so something listed there stopped being")
        print("  reported. Either it was genuinely fixed -- remove it, having")
        print("  MEASURED the served CRD rather than inferring from a version -- or")
        print("  the audit stopped working. Run --self-test to tell which.")
        return 2
    if new:
        print(f"\n  {len(new)} NEW broken mapping(s) -- a mapping whose source does not exist")
        print("  resolves to nothing at request time and the call goes out malformed.")
        return 1
    if KNOWN_BROKEN:
        print(f"\n  OK: exactly the {len(KNOWN_BROKEN)} known mappings, nothing new.")
    else:
        print(f"\n  OK: no mapping reads from a field that does not exist "
              f"({len(seen)} reported, 0 unexpected).")
        print("  KNOWN_BROKEN is empty on purpose: --self-test is what makes this")
        print("  clean run meaningful. See the docstring.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
