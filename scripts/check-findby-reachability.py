#!/usr/bin/env python3
"""A `findby` that can never fire, carrying configuration that only findby reads.

THE DEFECT CLASS: a declaration the generator accepts and silently never uses.
Observe consults `findby` only when IsResourceKnown is false. IsResourceKnown builds
the GET and asks whether it is addressable -- and it has NO observation counter, so it
is a pure function of the CURRENT spec. There is no "first reconcile" during which
findby gets a turn. If the get's path parameters all resolve at reconcile 1, findby is
structurally unreachable, and its `queries` and `responseTransform` are dead config
that looks load-bearing.

MEASURED, not reasoned: tests/oasgen-generator/ironic_power_get_routing_test.go and
ironic_mapping_source_routing_test.go, at oasgen 0.61.0 AND 0.80.0, identical at both.

    ARM 1      {node_id} <- spec.node_id BY NAME      known=TRUE   (step 3)
    ARM M      {uuid}    <- spec.name via mapping     known=TRUE   (step 5)
    ARM S-PRE  {uuid}    <- status.uuid, no status    known=false
    ARM S-POST {uuid}    <- status.uuid, status SET   known=TRUE
    DISCRIM    a source resolving nowhere             known=false

WHY THIS SCREEN EXISTS AT ALL. NodePower gained a `get` on 2026-11-08. That silently
retired its findby -- including a `responseTransform`, an `include_children=true` query
added after measuring 3 nodes vs 17 on the collection, and an `identifiersMatchPolicy`.
Nothing noticed for FOUR MONTHS, and the manifest's own comments asserted the opposite
until they were corrected on 2026-11-18 and 2026-11-21. A screen is what makes the next
one loud on the PR that causes it.

BY-NAME IS ONE OF TWO ROUTES, and getting that wrong is why this is a screen and not a
one-line grep. A param resolves at reconcile 1 if a spec field shares its NAME (step 3
auto-populate) OR a requestFieldMapping points it at a `spec.*` path (step 5). A mapping
to `status.*` does NOT resolve before a create has published status, which is the single
reason Port's findby is reachable and the other five are not.

THREE OUTCOMES, because two are never enough. A param this screen cannot classify --
no mapping and not an identifier, so its presence in the spec depends on the OAS
*Create* schema -- is VOID (exit 2), not a pass. Guessing in the permissive direction is
how a screen reports clean having measured nothing.
"""
import glob
import os
import re
import sys

import yaml

# Config keys ONLY findby reads. `headers` is excluded deliberately: every verb carries
# the API-version header, so its presence says nothing about findby.
FINDBY_ONLY_SUBSTANTIVE = ("queries", "responseTransform")
FINDBY_ONLY_MINOR = ("identifiersMatchPolicy",)

# --- Accepted findings. Every entry needs the kind, the exact dead keys, and a REASON.
#     An entry that matches nothing is a FAILURE: a dead exemption silently accepts
#     whatever replaces it. This repo has already been bitten by exactly that.
ACCEPTED = [
    {
        "kind": "BaremetalNode",
        "dead": ("queries", "responseTransform"),
        "reason": (
            "Documented in-file 2026-11-21 as DEFENSIVE, not load-bearing. The get "
            "resolves {uuid} from spec.name at step 5, so findby cannot fire today. "
            "Both are kept on purpose: the responseTransform masks a write-only "
            "driver_info password (oasgen #309) and the include_children query exists "
            "because the collection omits child nodes (3 vs 17 measured). Removing "
            "either would be a behaviour change on a path that goes live again the "
            "moment this kind loses its get verb or its spec.name mapping."
        ),
    },
    {
        "kind": "NodePower",
        "dead": ("queries", "responseTransform"),
        "reason": (
            "Documented in-file 2026-11-18 (#121), five comments corrected. Same shape "
            "as BaremetalNode but via step 3: {node_id} auto-populates from "
            "spec.node_id BY NAME. Reachable only below oasgen 0.52.0, where #421 "
            "suppressed the spec property -- which is what evidence/"
            "blade06-rdc-log-2026-11-05.jsonl records. Kept as the pre-0.52.0 path."
        ),
    },
]


def path_params(p):
    return re.findall(r"\{([^}]+)\}", p or "")


def classify_param(param, mapping, identifiers):
    """-> ('spec'|'status'|'unknown', explanation)"""
    src = mapping.get(param)
    if src:
        if src.startswith("spec."):
            return "spec", "mapping -> %s (step 5, resolves at reconcile 1)" % src
        if src.startswith("status."):
            return "status", "mapping -> %s (absent until a create publishes it)" % src
        return "unknown", "mapping -> %s, which is neither spec.* nor status.*" % src
    if param in identifiers:
        return "spec", "identifier %r, so a mandatory spec field of the same name (step 3)" % param
    return "unknown", (
        "no mapping and not an identifier, so whether spec carries %r depends on the "
        "OAS *Create* schema, which this screen does not read" % param
    )


def audit(root="."):
    rows, voids = [], []
    files = sorted(glob.glob(os.path.join(root, "manifests/restdefinition-*.yaml")))
    for f in files:
        try:
            d = yaml.safe_load(open(f, encoding="utf-8"))
        except Exception as e:                      # a malformed document is not a pass
            voids.append((f, "does not parse: %s" % e))
            continue
        r = ((d or {}).get("spec") or {}).get("resource") or {}
        kind = r.get("kind", "?")
        identifiers = r.get("identifiers") or []
        verbs = r.get("verbsDescription") or []
        findbys = [v for v in verbs if v.get("action") == "findby"]
        gets = [v for v in verbs if v.get("action") == "get"]
        if not findbys:
            continue
        fb = findbys[0]
        dead_sub = tuple(k for k in FINDBY_ONLY_SUBSTANTIVE if fb.get(k) is not None)
        dead_min = tuple(k for k in FINDBY_ONLY_MINOR if fb.get(k) is not None)

        if not gets:
            rows.append((f, kind, True, "no get verb at all, so findby is the only observe path",
                         dead_sub, dead_min))
            continue
        g = gets[0]
        mapping = {e.get("inPath"): e.get("inCustomResource")
                   for e in (g.get("requestFieldMapping") or [])}
        params = path_params(g.get("path"))
        if not params:
            voids.append((f, "the get's path %r declares no {param}; cannot classify"
                          % g.get("path")))
            continue
        why, blocked, unknown = [], False, False
        for p in params:
            cls, expl = classify_param(p, mapping, identifiers)
            why.append("{%s}: %s" % (p, expl))
            if cls == "status":
                blocked = True
            elif cls == "unknown":
                unknown = True
        if unknown:
            voids.append((f, "cannot classify every get param: " + "; ".join(why)))
            continue
        rows.append((f, kind, blocked, "; ".join(why), dead_sub, dead_min))
    return files, rows, voids


def report(root="."):
    files, rows, voids = audit(root)

    if not files:
        print("VOID: no manifests/restdefinition-*.yaml found under %r." % root)
        print("VOID: a clean run over zero documents is not a pass.")
        return 2
    if voids:
        print("VOID -- %d document(s) could not be classified. Not a pass, not a failure:" % len(voids))
        for f, why in voids:
            print("  %s\n    %s" % (os.path.relpath(f, root), why))
        return 2

    accepted_hits = {id(a): 0 for a in ACCEPTED}
    findings = []
    print("  %d document(s) with a findby:" % len(rows))
    for f, kind, reachable, why, dead_sub, dead_min in rows:
        rel = os.path.relpath(f, root)
        state = "REACHABLE" if reachable else "unreachable"
        keys = ",".join(dead_sub + dead_min)
        if not keys:
            extra = "no findby-only config"
        elif reachable:
            # NOT dead: a reachable findby reads this config, so say so rather than
            # printing "dead" beside every row and training the reader to skim it.
            extra = "findby-only config (LIVE): " + keys
        else:
            extra = "dead: " + keys
        print("    %-22s %-11s %s" % (kind, state, extra))
        if reachable or not dead_sub:
            continue
        acc = next((a for a in ACCEPTED
                    if a["kind"] == kind and tuple(sorted(a["dead"])) == tuple(sorted(dead_sub))), None)
        if acc:
            accepted_hits[id(acc)] += 1
            continue
        findings.append((rel, kind, dead_sub, why))

    dead_entries = [a for a in ACCEPTED if accepted_hits[id(a)] == 0]
    if dead_entries:
        print()
        print("SCREEN BROKEN -- %d accepted entr(ies) matched NOTHING:" % len(dead_entries))
        for a in dead_entries:
            print("  %s / %s" % (a["kind"], ",".join(a["dead"])))
        print("  An exemption that matches nothing silently accepts whatever replaces it.")
        print("  Either the kind was renamed, the config moved, or the finding is fixed")
        print("  and the entry should be deleted. Refusing to report a pass.")
        return 2

    if findings:
        print()
        print("FAIL -- %d findby verb(s) can never fire, carrying config only findby reads:" % len(findings))
        for rel, kind, dead, why in findings:
            print("  %s (%s)" % (rel, kind))
            print("    dead config: %s" % ", ".join(dead))
            print("    why unreachable: %s" % why)
        print()
        print("  Either remove the dead config, or remove/retarget the get so findby can")
        print("  fire, or add an ACCEPTED entry here stating why it is kept deliberately.")
        return 1

    print()
    print("PASS -- every findby either can fire, or carries no findby-only config, or is")
    print("        an accepted deliberate keep. %d accepted entr(ies), all matched."
          % len(ACCEPTED))
    return 0


# ---------------------------------------------------------------- calibration
# A screen that cannot fail is worth less than no screen, because it reports success.
# These fixtures are built here rather than listed, so they cannot rot.
SELFTEST = {
    # findby + a get whose param maps to spec.* -> UNREACHABLE, dead queries -> must FAIL
    "unreachable_with_dead_config": ({
        "spec": {"resource": {"kind": "Widget", "identifiers": ["name"], "verbsDescription": [
            {"action": "findby", "method": "GET", "path": "/v1/widgets",
             "queries": [{"name": "deep", "value": "true"}]},
            {"action": "get", "method": "GET", "path": "/v1/widgets/{uuid}",
             "requestFieldMapping": [{"inPath": "uuid", "inCustomResource": "spec.name"}]},
        ]}}}, 1),
    # the SAME shape but the mapping reads status -> REACHABLE -> must PASS.
    # This is the discriminating pair: only the mapping SOURCE differs.
    "reachable_via_status": ({
        "spec": {"resource": {"kind": "Widget", "identifiers": ["name"], "verbsDescription": [
            {"action": "findby", "method": "GET", "path": "/v1/widgets",
             "queries": [{"name": "deep", "value": "true"}]},
            {"action": "get", "method": "GET", "path": "/v1/widgets/{uuid}",
             "requestFieldMapping": [{"inPath": "uuid", "inCustomResource": "status.uuid"}]},
        ]}}}, 0),
    # unreachable but carrying NO findby-only config -> nothing is dying -> PASS
    "unreachable_but_nothing_dies": ({
        "spec": {"resource": {"kind": "Widget", "identifiers": ["name"], "verbsDescription": [
            {"action": "findby", "method": "GET", "path": "/v1/widgets"},
            {"action": "get", "method": "GET", "path": "/v1/widgets/{uuid}",
             "requestFieldMapping": [{"inPath": "uuid", "inCustomResource": "spec.name"}]},
        ]}}}, 0),
    # no get at all -> findby IS the observe path -> PASS even with queries
    "no_get_verb": ({
        "spec": {"resource": {"kind": "Widget", "identifiers": ["node_id"], "verbsDescription": [
            {"action": "findby", "method": "GET", "path": "/v1/widgets",
             "queries": [{"name": "deep", "value": "true"}]},
            {"action": "create", "method": "PUT", "path": "/v1/widgets/{node_id}/x"},
        ]}}}, 0),
    # a param neither mapped nor an identifier -> UNCLASSIFIABLE -> VOID, not a pass
    "unclassifiable_param": ({
        "spec": {"resource": {"kind": "Widget", "identifiers": ["name"], "verbsDescription": [
            {"action": "findby", "method": "GET", "path": "/v1/widgets",
             "queries": [{"name": "deep", "value": "true"}]},
            {"action": "get", "method": "GET", "path": "/v1/widgets/{mystery}"},
        ]}}}, 2),
}


def self_test():
    import tempfile
    fails = 0
    base = tempfile.mkdtemp()
    print("SELF-TEST (fixtures under %s)" % base)
    for name, (doc, want) in sorted(SELFTEST.items()):
        root = os.path.join(base, name)
        os.makedirs(os.path.join(root, "manifests"), exist_ok=True)
        with open(os.path.join(root, "manifests", "restdefinition-w.yaml"), "w",
                  encoding="utf-8") as fh:
            yaml.safe_dump(doc, fh)
        # the fixture kinds are "Widget", which no ACCEPTED entry names, so a finding
        # here is a real finding; but ACCEPTED would then match nothing and return 2.
        # Run the audit directly so the dead-exemption guard does not mask the arm.
        files, rows, voids = audit(root)
        if voids:
            got = 2
        elif not files:
            got = 2
        else:
            got = 0
            for f, kind, reachable, why, dead_sub, dead_min in rows:
                if not reachable and dead_sub:
                    got = 1
        mark = "ok   " if got == want else "FAIL "
        if got != want:
            fails = 1
        print("  %s %-34s expected %d, got %d" % (mark, name, want, got))

    # the real tree must classify cleanly -- no VOIDs
    files, rows, voids = audit(".")
    ok = files and not voids
    print("  %s %-34s %d doc(s), %d void(s)"
          % ("ok   " if ok else "FAIL ", "the real tree classifies", len(rows), len(voids)))
    if not ok:
        fails = 1

    # and the ACCEPTED list must not be vacuous
    nonempty = len(ACCEPTED) > 0
    print("  %s %-34s %d entr(ies)"
          % ("ok   " if nonempty else "FAIL ", "ACCEPTED is not empty", len(ACCEPTED)))
    if not nonempty:
        fails = 1

    if fails:
        print("SELF-TEST FAILED")
        return 2
    print("SELF-TEST: %d/%d assertions passed" % (len(SELFTEST) + 2, len(SELFTEST) + 2))
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    sys.exit(report(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))
