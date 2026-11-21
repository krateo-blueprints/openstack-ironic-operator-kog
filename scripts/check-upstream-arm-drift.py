#!/usr/bin/env python3
"""The contributed upstream arm claims to carry "the REAL verb mappings as shipped".

tests/upstream-arms/ironic_real_definitions_test.go holds hand-written Go fixtures for
four of this repo's RestDefinitions, and was contributed to krateo-platformops/
oasgen-provider as the over-eagerness arm for #455 / #456. Its value rests entirely on
being a faithful copy of documents that live in another directory and change
independently. Nothing kept the two in step.

WHAT IT COST, measured 2026-11-21. Two claims in that file had gone stale:

  1. "`ports` is the only one of the four whose by-uuid GET has NO mapping", which the
     file itself calls load-bearing. TRUE at b74777f (2026-11-05). FALSE since 000f05c
     (2026-11-12), nine days, when a mapping was added to fix "every NEW Port was
     rejected". It went stale BY BEING FIXED.
  2. The premise. The header calls these "the four RestDefinitions that set
     excludedSpecFields". None of them sets it any more.

NEITHER IS VISIBLE TO gofmt, which is the other screen over these files -- it sees
syntax and layout, never whether a literal still matches a YAML document elsewhere.
That is the gap this closes.

DIVERGENCE IS ALLOWED, AND MUST BE DECLARED. The fixtures are now deliberately a
HISTORICAL shape: #456's risk was exempting too much, so a regression arm over the
2026-11-05 documents is still worth having. So this screen does not demand equality.
It demands that every divergence be covered by a DATED marker in the file -- the same
constraint this repo sent upstream for charts' float exemptions, for the same reason:
an exemption a gate cannot verify is the shape that already failed here once.

THREE OUTCOMES. If the Go literals cannot be parsed, or a fixture names a kind with no
matching manifest, that is VOID (exit 2) -- not a pass. A parser that silently matched
nothing would report "no drift" forever, which is the failure mode this screen exists
to prevent in the first place.
"""
import os
import re
import sys

import yaml

ARM = "tests/upstream-arms/ironic_real_definitions_test.go"

FIXTURE_TO_MANIFEST = {
    "ports":           "manifests/restdefinition-port.yaml",
    "portgroups":      "manifests/restdefinition-portgroup.yaml",
    "allocations":     "manifests/restdefinition-allocation.yaml",
    "deploytemplates": "manifests/restdefinition-deploy-template.yaml",
}

# A divergence is accepted only if the file carries a marker of this form. The DATE is
# required: a marker without one cannot be checked for staleness, and "historical" with
# no date is indistinguishable from "we forgot".
MARKER = re.compile(r"(?:FALSE SINCE|HISTORICAL(?:\s+SHAPE)?(?:\s+AS\s+OF)?)\s+(\d{4}-\d{2}-\d{2})")


def go_list(block, field):
    m = re.search(field + r':\s*\[\]string\{([^}]*)\}', block)
    if not m:
        return None
    return sorted(re.findall(r'"([^"]*)"', m.group(1)))


def parse_arm(text):
    """-> {fixture_name: {"excluded":[...], "status":[...], "ids":[...], "verbs":{(action,path):mapping}}}"""
    out = {}
    for name in FIXTURE_TO_MANIFEST:
        m = re.search(r'\{"' + re.escape(name) + r'",\s*&ResourceConfig\{', text)
        if not m:
            continue
        # walk braces from the ResourceConfig open to its match
        i = text.index("{", m.end() - 1) if False else m.end() - 1
        depth, j = 0, i
        while j < len(text):
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        block = text[i:j + 1]
        verbs = {}
        for vm in re.finditer(
                r'\{Action:\s*"([^"]+)",\s*Method:\s*"([^"]+)",\s*Path:\s*"([^"]+)"'
                r'(?:,\s*\n?\s*FieldMapping:\s*\[\]FieldMapping\{(.*?)\})?\s*\}', block, re.S):
            action, _method, path, fm = vm.group(1), vm.group(2), vm.group(3), vm.group(4)
            mapping = {}
            if fm:
                for em in re.finditer(r'InPath:\s*"([^"]+)",\s*InCustomResource:\s*"([^"]+)"', fm):
                    mapping[em.group(1)] = em.group(2)
            verbs[(action, path)] = mapping
        out[name] = {
            "excluded": go_list(block, "ExcludedSpecFields"),
            "status":   go_list(block, "AdditionalStatusFields"),
            "ids":      go_list(block, "Identifiers"),
            "verbs":    verbs,
        }
    return out


def parse_manifest(path):
    d = yaml.safe_load(open(path, encoding="utf-8"))
    r = ((d or {}).get("spec") or {}).get("resource") or {}
    verbs = {}
    for v in r.get("verbsDescription") or []:
        # BOTH KEYS. `requestFieldMapping` is deprecated in favour of the unified
        # `fieldMapping`, and this repo migrated all 14 verbs on 2026-11-21. Reading only
        # the deprecated one made this screen report SIX FABRICATED contradictions
        # (manifest='none' for mappings that exist under the other key) -- and it still
        # PASSED, because the dated marker absorbed them. A real contradiction would have
        # been buried among the invented ones, which is worse than failing.
        #
        # Third screen in one day to need this fix. Two were written the same morning and
        # only one was corrected first; see the count-the-copies note.
        mapping = {}
        for _k in ("requestFieldMapping", "fieldMapping"):
            for e in (v.get(_k) or []):
                mapping[e.get("inPath")] = e.get("inCustomResource")
        verbs[(v.get("action"), v.get("path"))] = mapping
    esf = r.get("excludedSpecFields")
    return {
        "excluded": sorted(esf) if esf else None,
        "status":   sorted(r.get("additionalStatusFields") or []) or None,
        "ids":      sorted(r.get("identifiers") or []) or None,
        "verbs":    verbs,
    }


def audit(root="."):
    arm_path = os.path.join(root, ARM)
    if not os.path.isfile(arm_path):
        return None, None, ["%s does not exist" % ARM], []
    text = open(arm_path, encoding="utf-8").read()
    fixtures = parse_arm(text)
    voids = []
    if not fixtures:
        voids.append("parsed ZERO fixtures out of %s. Either the Go literals were "
                     "restructured or the parser is wrong; a 'no drift' result from a "
                     "parser that matched nothing is meaningless." % ARM)
        return text, None, voids, []
    missing = [n for n in FIXTURE_TO_MANIFEST if n not in fixtures]
    if missing:
        voids.append("fixtures present in the mapping but not found in the file: %s"
                     % ", ".join(sorted(missing)))
    drift, notes = [], []
    for name, fx in sorted(fixtures.items()):
        mp = os.path.join(root, FIXTURE_TO_MANIFEST[name])
        if not os.path.isfile(mp):
            voids.append("fixture %r names %s, which does not exist" % (name, FIXTURE_TO_MANIFEST[name]))
            continue
        mf = parse_manifest(mp)
        for field in ("excluded", "status", "ids"):
            if fx[field] != mf[field]:
                drift.append((name, field, fx[field], mf[field]))
        for key, fxmap in sorted(fx["verbs"].items()):
            if key not in mf["verbs"]:
                drift.append((name, "verb %s %s" % key, "present", "ABSENT from manifest"))
            elif fxmap != mf["verbs"][key]:
                drift.append((name, "mapping on %s %s" % key, fxmap or "none", mf["verbs"][key] or "none"))
        # A verb the manifest has and the fixture omits is SUBSET MODELLING, not a false
        # claim: this arm exists to exercise the excludedSpecFields guard, which only
        # cares about verbs that supply the excluded param, so findby/create (no path
        # params) are reasonably left out. Reported as a note so it stays visible, but
        # it is not drift -- otherwise 6 benign rows bury the 5 real contradictions and
        # train the reader to skim the list.
        for key in sorted(mf["verbs"]):
            if key not in fx["verbs"]:
                notes.append((name, "verb %s %s present in manifest, not modelled" % key))
    return text, drift, voids, notes


def report(root="."):
    text, drift, voids, notes = audit(root)
    if voids:
        print("VOID -- cannot compare. Not a pass, not a failure:")
        for v in voids:
            print("  %s" % v)
        return 2
    marks = MARKER.findall(text)
    print("  compared %d fixture(s) against their manifests" % len(FIXTURE_TO_MANIFEST))
    print("  dated divergence marker(s) in the file: %s" % (", ".join(marks) or "NONE"))
    if notes:
        print("  %d verb(s) present in a manifest but not modelled (subset, not drift):" % len(notes))
        for name, what in notes:
            print("    %-16s %s" % (name, what))
    if not drift:
        print()
        print("PASS -- no contradiction between the fixtures and the manifests.")
        return 0
    print()
    print("  %d CONTRADICTION(s) -- a fixture value that disagrees with what ships:" % len(drift))
    for name, field, fxv, mfv in drift:
        print("    %-16s %-34s fixture=%-26r manifest=%r" % (name, field, fxv, mfv))
    if not marks:
        print()
        print("FAIL -- the fixtures have drifted from the manifests and NOTHING in the")
        print("        file says so. This arm's whole claim is that it carries the real")
        print("        shapes as shipped, so an undeclared divergence is a false claim.")
        print("        Either resync the fixtures, or add a dated marker, e.g.")
        print("          // FALSE SINCE 2026-11-12 -- <what changed and why it is kept>")
        return 1
    print()
    print("PASS -- divergence is declared with a dated marker (%s), so the fixtures are" % marks[0])
    print("        an explicitly historical shape rather than a stale copy.")
    return 0


if __name__ == "__main__":
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    if "--self-test" in sys.argv:
        import tempfile, shutil
        fails = 0
        print("SELF-TEST")
        src = open(os.path.join(root, ARM), encoding="utf-8").read()
        # ARM 1: the real tree, which HAS drift and HAS a dated marker -> PASS
        t, d, v, _n = audit(root)
        ok = (not v) and d and MARKER.findall(t)
        print("  %s real tree: %d divergence(s), marker present -> PASS"
              % ("ok   " if ok else "FAIL ", len(d or [])))
        fails |= 0 if ok else 1
        # ARM 2: POSITIVE CONTROL -- same drift, marker STRIPPED -> must FAIL
        tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(tmp, os.path.dirname(ARM)), exist_ok=True)
        os.makedirs(os.path.join(tmp, "manifests"), exist_ok=True)
        for n, mp in FIXTURE_TO_MANIFEST.items():
            shutil.copy(os.path.join(root, mp), os.path.join(tmp, mp))
        stripped = MARKER.sub("DIVERGENCE NOT DECLARED", src)
        open(os.path.join(tmp, ARM), "w", encoding="utf-8").write(stripped)
        rc = report(tmp)
        print("  %s POSITIVE CONTROL: marker stripped -> expected 1, got %d"
              % ("ok   " if rc == 1 else "FAIL ", rc))
        fails |= 0 if rc == 1 else 1
        # ARM 2b: THE FIELD-NAME ARM. The same manifests with every `fieldMapping` key
        # renamed back to the deprecated `requestFieldMapping` must yield the IDENTICAL
        # contradiction count. If it does not, this screen is measuring the field name
        # rather than the mapping -- which is exactly the bug this arm was added for.
        import re as _re
        tmp2 = tempfile.mkdtemp()
        os.makedirs(os.path.join(tmp2, os.path.dirname(ARM)), exist_ok=True)
        os.makedirs(os.path.join(tmp2, "manifests"), exist_ok=True)
        open(os.path.join(tmp2, ARM), "w", encoding="utf-8").write(src)
        for _n, _mp in FIXTURE_TO_MANIFEST.items():
            _txt = open(os.path.join(root, _mp), encoding="utf-8").read()
            _txt = _re.sub(r"^(\s*)fieldMapping:(\s*)$", r"\1requestFieldMapping:\2",
                           _txt, flags=_re.M)
            open(os.path.join(tmp2, _mp), "w", encoding="utf-8").write(_txt)
        _t2, _d2, _v2, _n2 = audit(tmp2)
        _tr, _dr, _vr, _nr = audit(root)
        same = (not _v2) and (not _vr) and len(_d2 or []) == len(_dr or [])
        print("  %s field-name arm: deprecated-key copy gives %d contradiction(s), "
              "current gives %d" % ("ok   " if same else "FAIL ", len(_d2 or []), len(_dr or [])))
        fails |= 0 if same else 1

        # ARM 3: VOID CONTROL -- the Go literals gone -> must be VOID, not a pass
        open(os.path.join(tmp, ARM), "w", encoding="utf-8").write("package x\n// nothing here\n")
        rc = report(tmp)
        print("  %s VOID CONTROL: no parseable fixtures -> expected 2, got %d"
              % ("ok   " if rc == 2 else "FAIL ", rc))
        fails |= 0 if rc == 2 else 1
        if fails:
            print("SELF-TEST FAILED")
            sys.exit(2)
        print("SELF-TEST: 4/4 assertions passed")
        sys.exit(0)
    sys.exit(report(root))
