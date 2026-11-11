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

DOTTED = re.compile(r"(^|\.)\d+(\.|$)")  # foo.0.bar  foo.0  0.bar
BRACKET = re.compile(r"\[\s*\d+\s*\]")   # foo[0]


def offending(s):
    return isinstance(s, str) and bool(DOTTED.search(s) or BRACKET.search(s))


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


# Eight planted hits, one in every container that can hold a CR path, plus two jq
# bodies that must NOT be flagged.
CONTROL = """
apiVersion: ogen.krateo.io/v1alpha1
kind: RestDefinition
metadata:
  name: control
spec:
  resource:
    identifiers: ["spec.nested.0.address"]
    additionalStatusFields: ["status.items[0].uuid"]
    dependentLookups:
      - name: domain
        parameters:
          - inCustomResource: spec.domains.0.name
      - name: project
        parameters:
          - inCustomResource: status.projects[0].id
    fieldMapping:
      - inCustomResource: spec.plain.0.name
        resolver:
          secretRef:
            nameFromCustomResource: spec.secrets.0.name
            keyFromCustomResource: spec.secrets[0].key
          when:
            customResourceFieldPresent: spec.flags.0.enabled
    verbsDescription:
      - action: get
        requestFieldMapping:
          - inPath: uuid
            inCustomResource: status.ports.0.uuid
        responseTransform: '.items[0].uuid'
        requestBody: '{"n": .items[0].n}'
"""

# 9, counted off the plants one by one after the screen reported 9 and this said 8.
# The screen was right and the expectation was wrong -- `spec.plain.0.name` gained an
# index when the control was rewritten. Enumerated, not adjusted to match the output:
#   identifiers[0]  additionalStatusFields[0]
#   dependentLookups[0] dependentLookups[1]
#   fieldMapping[0].inCustomResource
#   resolver.secretRef.nameFromCustomResource  .keyFromCustomResource
#   resolver.when.customResourceFieldPresent
#   verbsDescription[0].requestFieldMapping[0].inCustomResource
EXPECTED = 9

if __name__ == "__main__":
    print(f"=== POSITIVE CONTROL (must be exactly {EXPECTED}; the two jq bodies must NOT appear) ===")
    hits = scan(yaml.safe_load_all(CONTROL))
    for where in sorted(hits):
        print(f"  HIT  {where} = {hits[where]}")
    print(f"  total = {len(hits)}  (expected {EXPECTED})")
    assert len(hits) == EXPECTED, f"SCREEN IS BROKEN: control gave {len(hits)}, not {EXPECTED}"
    assert not any("items[0].n" in v for v in hits.values()), "jq body leaked into results"
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
