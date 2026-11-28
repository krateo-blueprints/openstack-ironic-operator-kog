#!/usr/bin/env python3
"""tests/properties-inspection/*.yaml pins what `status.properties` really looks like.

`properties` is listed WHOLESALE in additionalStatusFields of
manifests/restdefinition-node.yaml, so the CR mirrors whatever Ironic holds. Nothing
pinned the shape, so a consumer could assume one real output does not have. The fixture
is measured, and this screen holds it to the three facts it exists for:

  1. `properties` is a FLAT map of string -> string.
  2. `inspection_finished_at` is RFC3339 with a numeric offset (`+00:00`), NOT `Z`.
  3. An inspected node at rest reads Ready=True/Available (the KOG CR, not the parent).

It also requires the fixture to carry its measurement date and provenance and to DECLARE
that it cannot exercise multi-key properties (emulated BMC), and requires that
`properties` is still surfaced wholesale -- if the manifest stops listing it, the fixture
describes a field the CR no longer carries.

THREE OUTCOMES. Exit 0 pass, 1 finding, 2 VOID (cannot measure: fixture or manifest
missing or unparseable). Missing PyYAML exits 3, never 1: an unavailable instrument must
not read as a finding.
"""
import copy
import glob
import os
import re
import sys
import tempfile

try:
    import yaml
except ImportError:
    print("check-properties-fixture: PyYAML missing -- VOID, not a pass", file=sys.stderr)
    sys.exit(3)

MANIFEST = "manifests/restdefinition-node.yaml"
FIXTURES = "tests/properties-inspection/*.yaml"
OFFSET_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?[+-]\d{2}:\d{2}$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def findings_for(doc):
    """Findings for one parsed fixture; [] means it holds. ValueError if unreadable."""
    if not isinstance(doc, dict) or not isinstance(doc.get("status"), dict):
        raise ValueError("no `status` mapping")
    f = []
    st = doc["status"]
    props = st.get("properties")
    # fact 1
    if not isinstance(props, dict) or not props:
        f.append("properties is not a non-empty mapping")
    else:
        for k, v in props.items():
            if not isinstance(k, str) or not isinstance(v, str):
                f.append("properties is not flat string->string at %r: %r" % (k, v))
    # fact 2 (a trailing Z cannot match the numeric-offset pattern)
    ts = st.get("inspection_finished_at")
    if not isinstance(ts, str) or not OFFSET_TS.match(ts):
        f.append("inspection_finished_at %r is not RFC3339 with a numeric offset" % (ts,))
    # fact 3
    ready = [c for c in st.get("conditions") or []
             if isinstance(c, dict) and c.get("type") == "Ready"]
    if len(ready) != 1 or ready[0].get("status") != "True" or ready[0].get("reason") != "Available":
        f.append("Ready condition is not exactly one True/Available")
    # provenance + scope limit
    m = doc.get("measurement") or {}
    if not DATE.match(str(m.get("date", ""))) or not DATE.match(str(m.get("inspected_on", ""))):
        f.append("measurement.date / inspected_on missing or not YYYY-MM-DD")
    for k in ("lab", "bmc", "node"):
        if not m.get(k):
            f.append("measurement.%s missing" % k)
    sl = doc.get("scope_limit") or {}
    if sl.get("exercises_multi_key_properties") is not False:
        f.append("scope_limit.exercises_multi_key_properties must be false")
    if not re.search(r"cannot exercise multi-key", str(sl.get("statement", ""))):
        f.append("scope_limit.statement does not say it cannot exercise multi-key properties")
    if isinstance(props, dict) and len(props) > 1:
        f.append("multi-key properties in an emulator-sourced fixture: provenance mismatch")
    return f


def surfaced_wholesale(manifest_text):
    d = yaml.safe_load(manifest_text)
    return "properties" in d["spec"]["resource"]["additionalStatusFields"]


def run(root):
    paths = sorted(glob.glob(os.path.join(root, FIXTURES)))
    if not paths:
        print("VOID: no fixture matches %s" % FIXTURES)
        return 2
    try:
        with open(os.path.join(root, MANIFEST), encoding="utf-8") as fh:
            wholesale = surfaced_wholesale(fh.read())
    except (OSError, KeyError, TypeError, yaml.YAMLError) as e:
        print("VOID: cannot read additionalStatusFields from %s (%s)" % (MANIFEST, e))
        return 2
    bad = []
    for p in paths:
        try:
            with open(p, encoding="utf-8") as fh:
                doc = yaml.safe_load(fh)
            bad += ["%s: %s" % (os.path.basename(p), x) for x in findings_for(doc)]
        except (OSError, ValueError, yaml.YAMLError) as e:
            print("VOID: %s unreadable (%s)" % (p, e))
            return 2
    if not wholesale:
        bad.append("%s no longer lists `properties` in additionalStatusFields" % MANIFEST)
    for b in bad:
        print("FINDING: " + b)
    print("check-properties-fixture: %d fixture(s), %d finding(s)" % (len(paths), len(bad)))
    return 1 if bad else 0


def self_test(root):
    with open(sorted(glob.glob(os.path.join(root, FIXTURES)))[0], encoding="utf-8") as fh:
        good = yaml.safe_load(fh)
    n = 0
    fails = 0

    def check(label, ok):
        nonlocal n, fails
        n += 1
        fails += 0 if ok else 1
        print("  %s %s" % ("ok  " if ok else "FAIL", label))

    def case(label, fn, want_finding):
        d = copy.deepcopy(good)
        fn(d)
        got = bool(findings_for(d))
        check("%s -> %s" % (label, "finding" if got else "clean"), got == want_finding)

    case("measured fixture (negative control)", lambda d: None, False)
    case("nested properties value", lambda d: d["status"]["properties"].update(cpus={"n": 4}), True)
    case("non-string properties value", lambda d: d["status"]["properties"].update(cpus=4), True)
    case("empty properties", lambda d: d["status"].update(properties={}), True)
    case("Z-suffixed timestamp",
         lambda d: d["status"].update(inspection_finished_at="2026-11-10T16:15:34Z"), True)
    case("Ready=False/Creating (the parent's reading)",
         lambda d: d["status"].update(
             conditions=[{"type": "Ready", "status": "False", "reason": "Creating"}]), True)
    case("scope limit claims multi-key coverage",
         lambda d: d["scope_limit"].update(exercises_multi_key_properties=True), True)
    case("scope statement removed", lambda d: d["scope_limit"].update(statement=""), True)
    case("measurement date removed", lambda d: d["measurement"].pop("date"), True)
    case("second key smuggled into an emulator fixture",
         lambda d: d["status"]["properties"].update(memory_mb="4096"), True)
    check("manifest arm: properties absent -> not wholesale",
          not surfaced_wholesale("spec:\n  resource:\n    additionalStatusFields: [uuid]\n")
          and surfaced_wholesale(
              "spec:\n  resource:\n    additionalStatusFields: [uuid, properties]\n"))
    rc = run(tempfile.mkdtemp())
    check("VOID control: no fixture -> expected 2, got %d" % rc, rc == 2)
    if fails:
        print("SELF-TEST FAILED")
        sys.exit(2)
    print("SELF-TEST: %d/%d assertions passed" % (n, n))
    sys.exit(0)


_WANT_SELFTEST = False

if __name__ == "__main__":
    # Both spellings accepted, anything else REFUSED (see check-screen-flags.py).
    _ALLOWED = {"--self-test", "--selftest"}
    _unknown = [a for a in sys.argv[1:] if a not in _ALLOWED]
    if _unknown:
        sys.stderr.write("usage: %s [%s]\n" % (sys.argv[0], "|".join(sorted(_ALLOWED))))
        sys.stderr.write("refusing unrecognised argument(s): %s\n" % " ".join(_unknown))
        sys.exit(64)
    _WANT_SELFTEST = bool(_ALLOWED & set(sys.argv[1:]))
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    if _WANT_SELFTEST:
        self_test(root)
    sys.exit(run(root))
