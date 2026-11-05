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

Calibration: it must report the TWO KNOWN cases below and nothing else. A run
reporting zero problems means the screen broke, not that the repo is clean.

That holds because of what is DEPLOYED, not what is merged. oasgen #421 fixes
these two rows and was merged to main as 8461460 on 2026-11-04, but no released
tag contains it yet -- every tag from 0.46.0 to 0.51.0 compares `behind` that
commit -- and the provider running here is 0.50.0. The rows clear when a tag
containing 8461460 is DEPLOYED, which is checkable in one call:

    gh api repos/krateo-platformops/oasgen-provider/compare/8461460...<tag> \
      --jq .status      # "identical" or "ahead" => the fix is in that tag

Until then a clean run is an instrument failure. "Merged upstream" is not the
gate; the generator that reads these documents is.
"""
import glob
import os
import re
import sys

import yaml

# The two mappings that ARE broken by upstream defect #419. #421 fixes them and
# is merged to main (8461460) but UNRELEASED; the deployed generator is 0.50.0.
# Listed so a clean run is recognised as a BROKEN SCREEN rather than good news.
KNOWN_BROKEN = {
    ("restdefinition-power.yaml", "node_id", "spec.node_id"),
    ("restdefinition-provision.yaml", "node_id", "spec.node_id"),
}


def oas_for(oaspath):
    m = re.match(r"configmap://[^/]+/([^/]+)/", oaspath)
    cm = m.group(1) if m else ""
    base = cm[:-4] if cm.endswith("-oas") else cm
    p = f"oas/{base}.yaml"
    return p if os.path.exists(p) else None


def audit():
    found, unresolved = [], []
    for f in sorted(glob.glob("manifests/restdefinition-*.yaml")):
        for d in [x for x in yaml.safe_load_all(open(f)) if x and x.get("kind") == "RestDefinition"]:
            r = d["spec"]["resource"]
            if not oas_for(d["spec"]["oasPath"]):
                unresolved.append(os.path.basename(f))
                continue
            excluded = set(r.get("excludedSpecFields") or [])
            status = set(r.get("additionalStatusFields") or []) | set(r.get("identifiers") or [])
            verbs = r.get("verbsDescription") or []
            # A mapping on ANY verb removes the name from the spec for ALL verbs
            # (oasgen #457), so suppression is computed across the whole resource.
            mapped_any = {m.get("inPath") for v in verbs
                          for k in ("fieldMapping", "requestFieldMapping")
                          for m in (v.get(k) or []) if m.get("inCustomResource")}
            for v in verbs:
                for k in ("fieldMapping", "requestFieldMapping"):
                    for m in (v.get(k) or []):
                        src, name = m.get("inCustomResource") or "", m.get("inPath")
                        if src.startswith("spec."):
                            fld = src[5:]
                            ok = fld not in excluded and fld not in mapped_any
                            why = ("excluded" if fld in excluded else "mapping-suppressed")
                        elif src.startswith("status."):
                            fld = src[7:]
                            ok = fld in status
                            why = "not in additionalStatusFields or identifiers"
                        else:
                            ok, why = False, "unrecognised source"
                        if not ok:
                            found.append((os.path.basename(f), name, src, why))
    return found, unresolved


def main():
    found, unresolved = audit()
    for f in unresolved:
        print(f"  WARN {f}: OAS document not resolved -- not audited, not cleared")
    for f, name, src, why in found:
        tag = "known (#419)" if (f, name, src) in KNOWN_BROKEN else "*** NEW ***"
        print(f"  {tag:14} {f}: {name} <- {src}  ({why})")

    got = {(f, n, s) for f, n, s, _ in found}
    new = got - KNOWN_BROKEN
    missing = KNOWN_BROKEN - got

    if missing:
        print(f"\n  SCREEN BROKEN: expected to find {sorted(missing)} and did not.")
        print("  #421 (8461460) is merged upstream but in no released tag, and the")
        print("  deployed generator is 0.50.0 -- so these cannot legitimately be clean.")
        print("  A zero here means the audit stopped working, not that the repo did.")
        return 2
    if new:
        print(f"\n  {len(new)} NEW broken mapping(s) -- a mapping whose source does not exist")
        print("  resolves to nothing at request time and the call goes out malformed.")
        return 1
    print(f"\n  OK: exactly the {len(KNOWN_BROKEN)} known #419 mappings, nothing new.")
    print("  When a tag containing 8461460 is DEPLOYED, this should reach zero")
    print("  and KNOWN_BROKEN should be emptied in the same commit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
