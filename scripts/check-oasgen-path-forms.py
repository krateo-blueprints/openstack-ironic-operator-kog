#!/usr/bin/env python3
"""oasgen #544 pre-upgrade screen: 0.57.0 drops the dotted-index path form.

PARSES the RestDefinitions (never greps) and reports every JSONPath-into-the-CR
that uses `foo.0.bar` or `foo[0]`.

Two defects were found in earlier versions of THIS script, both by the positive
control, and both are the reason it is structured the way it is:

  1. Exclusions were substring-matched: `script` matches `verbsDescription`
     ("De-script-ion"), which silently excluded the entire subtree holding every
     `requestFieldMapping` in the corpus. The screen printed a clean it had not
     measured. Exclusions are now ANCHORED.

  2. The key surface was oasgen's first list -- fieldMapping / identifiers /
     additionalStatusFields / requestFieldMapping. That list omits
     `dependentLookups`, which is its own field on spec.resource (types.go:940),
     so a walker told to visit those four never reaches it, nor the
     fieldMapping[].resolver.* leaves.

A control hit is planted inside EVERY container, not at the top level, because a
control proves the instrument fires where it is planted and nowhere else.
"""
import re
import sys

import yaml

# Bare string lists: the path IS the list element, there is no leaf key.
LIST_KEYS = {"identifiers", "additionalStatusFields"}

# Leaf keys whose VALUE is a JSONPath into the custom resource. Collected wherever
# they appear, so the walk never depends on knowing every container above them --
# which is exactly how `dependentLookups` was missed.
LEAF_KEYS = {
    "inCustomResource",            # fieldMapping[], requestFieldMapping[] (deprecated, live)
    "nameFromCustomResource",      # fieldMapping[].resolver.secretRef
    "keyFromCustomResource",       # fieldMapping[].resolver.secretRef
    "customResourceFieldPresent",  # fieldMapping[].resolver.when
}

# ANCHORED, not substring. jq bodies are false positives: `[0]` inside jq is legal.
SKIP = re.compile(r"^(.*transform|jq|script|.*body)$", re.I)

# SCOPE, corrected by oasgen-provider 2026-11-11. Only two forms are decidably
# refused by #544:
#
#   foo.0.bar     the dotted index
#   foo['0']      the quoted bracket index
#
# The full set, from #544's author:
#
#   REFUSED     a.0       against an ARRAY
#   REFUSED     a['0']    against an ARRAY
#   REFUSED     a[0]      against a MAP
#   UNCHANGED   a[0]      against an ARRAY        <- the normal, correct form
#   UNCHANGED   a['0']    against a MAP
#   UNCHANGED   plain map traversal, [?key=value] predicates
#
# A RestDefinition does not carry the schema, so array-vs-map is not decidable from
# path text. A bare `foo[0]` is therefore NOT a finding -- it needs a schema check,
# which is a different job. My first version flagged every bare bracket and would
# have raised false alarms on five corpora, including `spec.credentials[0]`, which
# is in #544's own agree list.
DOTTED = re.compile(r"(^|\.)\d+(\.|$)")              # foo.0.bar  foo.0  0.bar
QUOTED = re.compile(r"\[\s*['\"]\d+['\"]\s*\]")      # foo['0']  foo["0"]


def is_version(s):
    """A version is numeric in EVERY dot-separated segment; a path has non-numeric
    ones. Without this, DOTTED flags `2.72` and `1.95` -- microversions, which both
    this corpus and nova's carry."""
    parts = s.split(".")
    return len(parts) > 1 and all(p.isdigit() for p in parts)


def offending(s):
    if not isinstance(s, str) or is_version(s):
        return False
    return bool(DOTTED.search(s) or QUOTED.search(s))


def walk(node, path, hits):
    """Collect (full_path, value). Keyed on full_path, so the same leaf reached
    by two routes is one hit, not two."""
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(k, str) and SKIP.search(k):
                continue
            here = f"{path}.{k}" if path else k
            if k in LEAF_KEYS and offending(v):
                hits[here] = v
            if k in LIST_KEYS and isinstance(v, list):
                for i, item in enumerate(v):
                    if offending(item):
                        hits[f"{here}[{i}]"] = item
            walk(v, here, hits)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            walk(v, f"{path}[{i}]", hits)


def scan(docs):
    hits = {}
    for d in docs:
        if d:
            walk(d, "", hits)
    return hits


# Every planted value is labelled. MUST-FIRE sits inside every container that can
# hold a CR path -- never at the top level, because a control proves the instrument
# fires where it is planted and nowhere else. MUST-NOT-FIRE covers the two false
# alarms the scope correction removed (bare brackets, versions) plus jq bodies.
CONTROL = """
apiVersion: ogen.krateo.io/v1alpha1
kind: RestDefinition
metadata:
  name: control
spec:
  resource:
    identifiers:
      - spec.nested.0.address          # MUST FIRE   dotted
      - spec.nested['1'].address       # MUST FIRE   quoted bracket
      - status.items[0].uuid           # must NOT    bare bracket, undecidable
      - spec.plain.name                # must NOT    no index
      - spec.credentials[0].valueSecretRef.name
      #   ^ must NOT. This exact path is in #544's own AGREE list and appears across
      #     five corpora. Flagging it is what turns a screen into a source of false
      #     work, so it is pinned here as a regression row rather than left implied.
    additionalStatusFields:
      - status.a.2.b                   # MUST FIRE
      - "1.109"                        # must NOT    a microversion, not a path
      - "2.72"                         # must NOT    a microversion
      - "2.1"                          # must NOT    a microversion (nova's)
      #   ^ QUOTED deliberately. Unquoted, YAML types these as floats and they never
      #     reach is_version() at all -- the negative case would pass because the
      #     value was not a string, not because the guard works. A control that
      #     passes for the wrong reason is not a control.
    dependentLookups:
      - name: domain
        parameters:
          - inCustomResource: spec.domains.0.name       # MUST FIRE
      - name: project
        parameters:
          - inCustomResource: status.projects[0].id     # must NOT  bare bracket
    fieldMapping:
      - inCustomResource: spec.plain.0.name             # MUST FIRE
        resolver:
          secretRef:
            nameFromCustomResource: spec.secrets.0.name # MUST FIRE
            keyFromCustomResource: spec.secrets[0].key  # must NOT  bare bracket
          when:
            customResourceFieldPresent: spec.flags.0.enabled   # MUST FIRE
    verbsDescription:
      - action: get
        path: /v2.1/flavors            # must NOT (and not a walked key anyway)
        requestFieldMapping:
          - inPath: uuid
            inCustomResource: status.ports.0.uuid       # MUST FIRE
        responseTransform: '.items[0].uuid'             # must NOT  jq body
        requestBody: '{"n": .items[0].n}'               # must NOT  jq body
"""

# 8 MUST-FIRE plants, enumerated rather than read off the output:
#   identifiers: spec.nested.0.address, spec.nested['1'].address
#   additionalStatusFields: status.a.2.b
#   dependentLookups[0].parameters[0].inCustomResource
#   fieldMapping[0].inCustomResource
#   resolver.secretRef.nameFromCustomResource
#   resolver.when.customResourceFieldPresent
#   verbsDescription[0].requestFieldMapping[0].inCustomResource
EXPECTED = 8

if __name__ == "__main__":
    print(f"=== POSITIVE CONTROL (must be exactly {EXPECTED}; the two jq bodies must NOT appear) ===")
    hits = scan(yaml.safe_load_all(CONTROL))
    for where in sorted(hits):
        print(f"  HIT  {where} = {hits[where]}")
    print(f"  total = {len(hits)}  (expected {EXPECTED})")
    assert len(hits) == EXPECTED, f"SCREEN IS BROKEN: control gave {len(hits)}, not {EXPECTED}"
    # Assert what must NOT have fired. A control that only checks the planted hits
    # cannot catch an over-broad pattern -- which is exactly what the bare-bracket
    # rule was.
    MUST_STAY_SILENT = ["status.items[0].uuid", "status.projects[0].id",
                        "spec.secrets[0].key", "items[0].n", "1.109", "2.72", "2.1",
                        "/v2.1/flavors", "spec.plain.name",
                        "spec.credentials[0].valueSecretRef.name"]
    for forbidden in MUST_STAY_SILENT:
        assert not any(forbidden == v for v in hits.values()), \
            f"FALSE ALARM: pattern fired on {forbidden!r}, which #544 does not refuse"
    # Both counts must be NON-ZERO (oasgen's idea). A suite of silent rows passes
    # trivially against a checker that never fires, and a suite of firing rows passes
    # against one that fires on everything. Asserting both directions are exercised is
    # what makes the pair meaningful.
    assert len(hits) > 0, "no must-fire row fired: the checker may never fire at all"
    assert len(MUST_STAY_SILENT) > 0, "no must-stay-silent rows: over-breadth untested"
    print("  control PASSES -- the walk reaches every container and skips jq\n")

    print("=== REAL CORPUS ===")
    total = 0
    for p in sys.argv[1:]:
        with open(p) as fh:
            docs = list(yaml.safe_load_all(fh))
        hits = scan(docs)
        names = [d.get("metadata", {}).get("name") for d in docs if d]
        print(f"  {p}\n      docs={len(docs)} names={names} hits={len(hits)}")
        for where in sorted(hits):
            print(f"      HIT  {where} = {hits[where]}")
        total += len(hits)
    print(f"\n  corpus total hits = {total}")
    if total:
        # A check that reports findings and exits 0 is worse than no check, because
        # `make check` then goes green with real findings in the output. This script
        # DID exit 0 on a known-bad input until the exit code was tested explicitly --
        # the findings were printed and the gate passed.
        raise SystemExit(f"FAIL: {total} dotted-index path form(s); oasgen 0.57.0 (#544) "
                         f"will refuse them. Rewrite each as a non-indexed JSONPath.")
    print(f"  (0 with the control at {EXPECTED}/{EXPECTED} = genuinely clean for #544)")
