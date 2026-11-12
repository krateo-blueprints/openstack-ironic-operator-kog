#!/usr/bin/env python3
"""Standing screen: is each RestDefinition's declared shape ACTUALLY addressable?

Four kinds in this repo are exercised daily by the lab; three ship and are
exercised by nothing. The two most expensive defects this repo has had were both
invisible to review and to every other screen, and both were in that second class
of thing -- something declared that cannot work:

  * `restdefinition-power`'s `get` verb addressed /v1/nodes/{node_id}, which the
    OAS did not declare. The verb was INERT, so the repair path for a wedge was
    silent. Cost: a day of diagnosis. Fixed in PR #30.
  * `restdefinition-provision`'s `findby` matched on `node_id`, which the Ironic
    collection response does not carry. findby could never match. Fixed in PR #34
    with a responseTransform that manufactures it.

Neither is a syntax error. Both are a reference from one document to another that
does not resolve. This screen reads both sides and checks that they meet.

WHAT IT CHECKS (A-D are actionable; E is reported, never fatal)

  A  every verb's path AND method are declared in that kind's OAS
  B  every `identifiers` entry is a real spec field (spec is built from the
     CREATE body schema), or a findby responseTransform manufactures it
  C  every requestFieldMapping source resolves:
       spec.X   <- the create body schema
       status.X <- additionalStatusFields, or the get/findby response
  D  findby's identifier appears in the COLLECTION item schema, or a
     responseTransform supplies it
  E  a path param with NO mapping is kept as a spec property. If it is absent
     from the create body, the user must set a field the API never returns.

WHY E IS NOT FATAL. Three live kinds are in exactly that state today, by a
generation bug measured on 2026-11-04 against live CRDs at oasgen 0.50.0: a
`requestFieldMapping` SUPPRESSES the path parameter's own spec property, and only
an independent request-BODY declaration rescues it. So NodePower and
NodeProvision have no `spec.node_id` at all, and the charts render it on every
transition CR where it is silently pruned. That is upstream's to fix, it is
documented in docs/IMPLEMENTATION.md, and failing the build on it would make this
screen red on main -- and a gate that is red on main is not a gate. It is printed
on EVERY run instead, because the day it changes is the day someone needs to know.

THE ZERO THIS SCREEN MUST NOT REPORT. "0 findings" is only a measurement if the
walker could have found one. --selftest plants a case for every branch above and
asserts each fires; the run also asserts the three KNOWN rows below are still
matched, so a refactor that stops reading the file cannot pass silently.
"""
import argparse, glob, os, re, sys

try:
    import yaml
except ImportError:
    print("check-restdefinition-shape: PyYAML missing -- VOID, not a pass", file=sys.stderr)
    sys.exit(3)


class Void(Exception):
    """The screen could not form an opinion. Never a pass."""


# WHETHER A MAPPED PATH PARAM KEEPS ITS SPEC PROPERTY IS VERSION-DEPENDENT.
#
#     spec.<param> survives  IFF  the create body declares <param> at top level
#                            OR   the mapping is SELF-REFERENCING
#                                 AND oasgen >= 0.52.0   (issue #421)
#
# That second clause cost this repo a retraction and nearly a second one. I measured
# four Ironic kinds at oasgen 0.50.0 and concluded "a requestFieldMapping suppresses
# the property; only a body declaration rescues it", explicitly RETRACTING a
# same-name rule because my rows moved two factors together. The
# openstack-nova-operator-kog session then reported the same-name rule as true, which
# looked like a contradiction. It is not: #421 -- "a requestFieldMapping no longer
# deletes the spec property it reads from" -- first shipped in 0.52.0, BELOW which my
# measurement was taken. Their before/after on one document, on a cluster:
#
#     0.50.0   self-referencing mapping, param not in body  ->  spec prop ABSENT
#     0.52.0   same document                                ->  PRESENT and required
#
# So neither table was confounded the way I guessed. The factor NEITHER of us varied
# was the oasgen version -- mine because I only ever ran 0.50.0, theirs because every
# Nova create body wraps in a single envelope key, so a mapped param is never
# body-declared and that clause is a CONSTANT they read as an absence.
#
# AN EXEMPTION IS A CLAIM ABOUT A VERSION, NOT FOREVER. Each entry below therefore
# carries the range it applies to. One that does not apply at the configured version
# is reported as RETIRED BY VERSION rather than counted as a calibration failure --
# otherwise upgrading past a fix turns this screen red for the fix.
# oasgen #421. MEASURED from the tag graph, not read off the PR -- and that
# distinction is load-bearing, because #421's own description says it targets
# **0.51.1, A VERSION THAT DOES NOT EXIST**:
#
#     git tag | grep ^0.51          ->  0.51.0          (one line, no 0.51.1)
#     git tag --contains 8461460    ->  0.52.0 first, 11 tags total
#     CONTROL: git tag | grep ^0.52 ->  0.52.0          (so the grep works)
#
# 8461460 is the #421 merge commit. Had this constant been "corrected" against the
# PR body it would encode a release nobody can install, and every verdict below the
# real boundary would have been wrong in the permissive direction -- the screen would
# have said a property survives at 0.51.1 when there is no 0.51.1 to survive at.
#
# A version a fix CLAIMS to target is a plan; the tag graph is what happened.
MIN_SELFREF_SURVIVES = (0, 52, 0)

KNOWN = {
    ("power", "E", "node_id"): {
        "why": ("a SELF-REFERENCING mapping (node_id <- spec.node_id) with no body "
                "declaration. Measured ABSENT on live CRDs 2026-11-04 at oasgen "
                "0.50.0 -- BELOW #421's 0.52.0. Predicted RESOLVED at the configured "
                "version; that prediction is NOT yet confirmed on this cluster."),
        "upto": MIN_SELFREF_SURVIVES,
    },
    ("provision", "E", "node_id"): {
        "why": "same mechanism and same version boundary as power.",
        "upto": MIN_SELFREF_SURVIVES,
    },
    ("port", "E", "uuid"): {
        "why": ("NOT self-referencing -- Port's delete maps uuid <- status.uuid, and "
                "its get leaves {uuid} unmapped. Neither survival clause can apply at "
                "ANY version, so this one does not retire. Port is addressed by "
                "address, so it is inert rather than broken."),
        "upto": None,   # applies at every version
    },
}


def parse_version(text):
    """'0.59.0' -> (0, 59, 0). Raises Void on anything else, because a version this
    screen cannot parse must not silently become a default that changes verdicts."""
    parts = (text or "").strip().lstrip("v").split(".")
    if len(parts) != 3 or not all(x.isdigit() for x in parts):
        raise Void("cannot parse oasgen version %r -- refusing to guess, since the "
                   "version decides how check C and E classify" % text)
    return tuple(int(x) for x in parts)


def known_applies(entry, version):
    """Is this exemption still in force at `version`?"""
    upto = entry.get("upto")
    return upto is None or version < upto


def repo_pinned_version(path="Makefile"):
    """The repo's own pin, so the default is a single source of truth rather than a
    literal in this file that would drift from the one the install actually uses."""
    try:
        for line in open(path):
            if line.startswith("KRATEO_OASGEN_VERSION"):
                return line.split("=", 1)[1].strip().lstrip("?= ").strip()
    except OSError:
        pass
    return None

LIVE = {"node", "port", "power", "provision"}


def resolve(schema, oas, depth=0):
    """Property names of a schema, following $ref and the composition keywords."""
    if schema is None or depth > 8:
        return set()
    if "$ref" in schema:
        name = schema["$ref"].split("/")[-1]
        return resolve((oas.get("components") or {}).get("schemas", {}).get(name), oas, depth + 1)
    out = set(schema.get("properties") or {})
    for key in ("allOf", "anyOf", "oneOf"):
        for sub in schema.get(key) or []:
            out |= resolve(sub, oas, depth + 1)
    if schema.get("type") == "array":
        out |= resolve(schema.get("items"), oas, depth + 1)
    return out


def body_props(oas, path, method):
    op = (oas.get("paths") or {}).get(path, {}).get(method, {})
    for _ct, media in ((op.get("requestBody") or {}).get("content") or {}).items():
        return resolve(media.get("schema"), oas)
    return set()


def response_item_props(oas, path, method):
    """For a collection endpoint, the properties of ONE item."""
    op = (oas.get("paths") or {}).get(path, {}).get(method, {})
    for code, resp in (op.get("responses") or {}).items():
        if not str(code).startswith("2"):
            continue
        for _ct, media in (resp.get("content") or {}).items():
            schema = media.get("schema") or {}
            if "$ref" in schema:
                schema = (oas.get("components") or {})["schemas"][schema["$ref"].split("/")[-1]]
            for wrapper, sub in (schema.get("properties") or {}).items():
                if (sub or {}).get("type") == "array":
                    return wrapper, resolve(sub.get("items"), oas)
            return "(unwrapped)", resolve(schema, oas)
    return None, set()


def find_oas(rd, slug, oas_dir):
    key = rd["spec"]["oasPath"].rsplit("/", 1)[-1]
    for cand in (os.path.join(oas_dir, key.replace("_", "-")),
                 os.path.join(oas_dir, "ironic-%s.yaml" % slug),
                 os.path.join(oas_dir, "%s.yaml" % slug)):
        if os.path.exists(cand):
            return cand
    return None


def evaluate(rd, oas, slug, version=MIN_SELFREF_SURVIVES):
    """Return a list of (check, subject, message, actionable). Raises Void.

    `version` is the oasgen version to answer FOR. It decides whether a
    self-referencing mapping keeps its spec property (#421, 0.52.0)."""
    res = (rd.get("spec") or {}).get("resource") or {}
    verbs = res.get("verbsDescription") or []
    if not verbs:
        raise Void("%s declares no verbsDescription" % slug)
    paths = oas.get("paths") or {}
    if not paths:
        raise Void("%s: its OAS declares no paths at all" % slug)

    by_action = {v.get("action"): v for v in verbs}
    create = by_action.get("create")
    spec = body_props(oas, create["path"], create["method"].lower()) if create else set()

    # A MAPPED PATH PARAM THAT SURVIVES IS A SPEC FIELD, so it belongs in `spec`
    # before anything reads it. Putting this in check C's actionability instead was
    # wrong and produced 4 ACTIONABLE rows about kinds that have driven 8 blades for
    # weeks: C asks "does the mapping's source resolve", and if the property survives
    # then it plainly does. The version rule belongs in ONE place -- here.
    suppressed_params = {}
    for v in verbs:
        for m in v.get("requestFieldMapping") or []:
            pp = m.get("inPath")
            if not pp or pp in spec:
                continue                       # clause 1: declared in the create body
            src = m.get("inCustomResource", "")
            selfref = src.startswith("spec.") and src.split(".")[-1] == pp
            if selfref and version >= MIN_SELFREF_SURVIVES:
                spec.add(pp)                   # clause 2: #421, oasgen >= 0.52.0
            else:
                suppressed_params[pp] = (src, selfref)
    asf = set(res.get("additionalStatusFields") or [])
    reader = by_action.get("get") or by_action.get("findby")
    status = set(asf)
    if reader:
        status |= response_item_props(oas, reader["path"], reader["method"].lower())[1]

    findby = by_action.get("findby")
    transform = (findby or {}).get("responseTransform")
    xf_text = str(transform) if transform else ""

    def transform_supplies(ident):
        """Does the findby responseTransform plausibly supply `ident`?

        A HEURISTIC, and labelled as one in every row that uses it, because it
        DOWNGRADES: a true answer turns an actionable finding into an informational
        one. Getting it wrong in the permissive direction hides a findby that can
        never match -- which is exactly the defect this screen exists to catch (#34).

        It was a bare substring test (`ident in xf_text`) until 2026-11-12. That
        works here only by luck of Ironic's payload shape: every identifier is flat
        (`name`, `address`, `node_id`), so a substring cannot collide. The
        openstack-nova-operator-kog session ported this screen and found the test
        UNREACHABLE against Nova, whose payloads are enveloped -- a transform for a
        dotted identifier references the LEAF (`{name: .flavors[0].name}`), so
        `"status.server_group.id" in xf_text` is never true. Their D-rescued case
        could not fire at all.

        So: match the identifier's LEAF on a word boundary. Still a heuristic -- it
        does not evaluate the jq -- but it fails in the direction that keeps a
        finding actionable rather than silently informational.
        """
        if not xf_text:
            return False
        leaf = ident.split(".")[-1]
        # `(?<!\w)`, NOT `(?<![\w.])`. A jq field reference is written `.node_id`, so
        # excluding a preceding dot excludes the only form that actually occurs --
        # which it did: the first version of this turned all four live transforms
        # into "no transform supplies it" and made the screen RED ON MAIN with 4
        # actionable rows. A word char before the leaf is the real collision case
        # (`name` inside `hostname_suffix`), and `(?<!\w)` rejects exactly that.
        return re.search(r"(?<!\w)%s(?!\w)" % re.escape(leaf), xf_text) is not None

    out = []
    # A -- the path and method must be declared
    for v in verbs:
        p, m, a = v.get("path"), (v.get("method") or "").lower(), v.get("action")
        if p not in paths:
            out.append(("A", "%s %s" % (a, p), "path not declared in the OAS -- the verb is INERT", True))
        elif m not in {k.lower() for k in paths[p]}:
            out.append(("A", "%s %s %s" % (a, m.upper(), p),
                        "path is declared but method %s is not -- the verb is INERT" % m.upper(), True))

    # B -- identifiers must be readable
    for ident in res.get("identifiers") or []:
        if ident in spec:
            continue
        if transform_supplies(ident):
            out.append(("B", ident, "not a spec field, but the findby responseTransform appears to "
                                    "supply it (HEURISTIC: the jq is not evaluated)", False))
        else:
            out.append(("B", ident, "identifier is not a spec field and no transform supplies it", True))

    # C -- mapping sources must resolve
    for v in verbs:
        for m in v.get("requestFieldMapping") or []:
            src = m.get("inCustomResource", "")
            root, leaf = (src.split(".") + [""])[0], src.split(".")[-1]
            pool, pname = (spec, "the create body") if root == "spec" else (status, "status")
            if leaf not in pool:
                out.append(("C", "%s %s<-%s" % (v.get("action"), m.get("inPath"), src),
                            "source does not resolve in %s" % pname, True))

    # D -- findby must be able to match
    if findby:
        _w, item = response_item_props(oas, findby["path"], (findby.get("method") or "get").lower())
        for ident in res.get("identifiers") or []:
            if ident in item:
                continue
            if transform_supplies(ident):
                out.append(("D", ident, "absent from the collection item; the responseTransform appears "
                                        "to manufacture it (HEURISTIC: the jq is not evaluated)", False))
            else:
                out.append(("D", ident, "absent from the collection item and no responseTransform -- findby can NEVER match", True))

    # E -- unmapped path params become spec properties
    for v in verbs:
        mapped = {m.get("inPath") for m in (v.get("requestFieldMapping") or [])}
        for pp in re.findall(r"\{(\w+)\}", v.get("path") or ""):
            if pp in mapped or pp in spec:
                continue
            out.append(("E", pp, "path param is unmapped and absent from the create body, so it is "
                                 "kept as spec.%s -- a field the API never returns" % pp, False))
    # E simply reports what the survival pass above classified as suppressed.
    for pp, (src, selfref) in sorted(suppressed_params.items()):
        if selfref:
            msg = ("mapped path param is self-referencing but oasgen %s is below %s, "
                   "where #421 stopped the mapping deleting the property, so spec.%s "
                   "does not exist"
                   % (".".join(map(str, version)),
                      ".".join(map(str, MIN_SELFREF_SURVIVES)), pp))
        else:
            msg = ("mapped path param is suppressed from spec: the mapping is not "
                   "self-referencing (reads %s) and no body schema declares it, so "
                   "spec.%s does not exist at any version" % (src or "?", pp))
        out.append(("E", pp, msg, False))
    # dedupe E
    seen, deduped = set(), []
    for row in out:
        key = (row[0], row[1], row[2])
        if row[0] == "E":
            if (row[0], row[1]) in seen:
                continue
            seen.add((row[0], row[1]))
        deduped.append(row)
    return deduped


def run(manifest_glob, oas_dir, version, version_src):
    files = sorted(glob.glob(manifest_glob))
    if not files:
        raise Void("no RestDefinition manifests matched %r" % manifest_glob)

    actionable, reported, voids, known_hit = [], [], [], set()
    for f in files:
        slug = os.path.basename(f)[len("restdefinition-"):-len(".yaml")]
        try:
            rd = yaml.safe_load(open(f))
        except Exception as e:
            voids.append("%s: unreadable (%s)" % (slug, e)); continue
        oasp = find_oas(rd, slug, oas_dir)
        if not oasp:
            voids.append("%s: no OAS document found for %s" % (slug, rd["spec"]["oasPath"])); continue
        try:
            oas = yaml.safe_load(open(oasp))
        except Exception as e:
            voids.append("%s: OAS unreadable (%s)" % (slug, e)); continue
        try:
            rows = evaluate(rd, oas, slug, version)
        except Void as v:
            voids.append(str(v)); continue
        tag = "live" if slug in LIVE else "NOT EXERCISED"
        for check, subject, msg, act in rows:
            k = (slug, check, subject)
            if k in KNOWN and known_applies(KNOWN[k], version):
                known_hit.add(k)
                reported.append("  %-16s [%s] %-24s %s\n        known: %s"
                                % (slug, check, subject, msg, KNOWN[k]["why"]))
            elif act:
                actionable.append("  %-16s (%s) [%s] %-24s %s" % (slug, tag, check, subject, msg))
            else:
                reported.append("  %-16s [%s] %-24s %s" % (slug, check, subject, msg))

    print("checked %d RestDefinition(s): %s" % (len(files),
          ", ".join(os.path.basename(f)[len("restdefinition-"):-len(".yaml")] for f in files)))
    print("answered FOR oasgen %s (%s). Checks C and E depend on it: #421 (0.52.0)\n"
          "  stopped a requestFieldMapping deleting the spec property it reads from."
          % (".".join(map(str, version)), version_src))
    if voids:
        print("\nVOID -- the screen could not form an opinion on these. NOT a pass:")
        for v in voids: print("  " + v)
    if reported:
        print("\nreported, not fatal (%d):" % len(reported))
        for r in reported: print(r)
    if actionable:
        print("\nACTIONABLE (%d):" % len(actionable))
        for a in actionable: print(a)

    # Only exemptions still IN FORCE at this version must match. One retired by a
    # version bump is announced, not failed -- otherwise upgrading past a fix turns
    # this screen red for the fix.
    retired = {k for k, e in KNOWN.items() if not known_applies(e, version)}
    if retired:
        print("\nRETIRED BY VERSION at oasgen %s (%d) -- these exemptions no longer apply:"
              % (".".join(map(str, version)), len(retired)))
        for k in sorted(retired):
            print("  %-16s [%s] %-12s %s" % (k[0], k[1], k[2], KNOWN[k]["why"]))
    missing = {k for k in set(KNOWN) - known_hit if known_applies(KNOWN[k], version)}
    if missing and not voids:
        print("\nCALIBRATION FAILED: these KNOWN conditions were not matched, so this "
              "screen is no longer reading what it thinks it reads:")
        for m in sorted(missing): print("  %s" % (m,))
        return 2

    in_force = sum(1 for e in KNOWN.values() if known_applies(e, version))
    print("\nactionable=%d  reported=%d  void=%d  known-matched=%d/%d in force (%d retired by version)"
          % (len(actionable), len(reported), len(voids), len(known_hit), in_force,
             len(KNOWN) - in_force))
    if voids:   return 2
    if actionable: return 1
    print("PASS -- every declared verb, identifier and mapping resolves against its OAS.")
    print("       %d non-fatal condition(s) stand, listed above. NOT a clean sheet." % len(reported))
    return 0


# ---------------------------------------------------------------- selftest
def selftest():
    BASE_OAS = {
        "paths": {
            "/v1/things": {
                "get": {"responses": {"200": {"content": {"application/json": {"schema": {
                    "type": "object", "properties": {"things": {"type": "array", "items": {
                        "type": "object", "properties": {"name": {}, "uuid": {}}}}}}}}}}},
                "post": {"requestBody": {"content": {"application/json": {"schema": {
                    "type": "object", "properties": {"name": {}, "extra": {}}}}}}},
            },
            "/v1/things/{uuid}": {"get": {}, "delete": {}},
        }
    }

    def rd(**over):
        res = {"kind": "Thing", "identifiers": ["name"], "additionalStatusFields": ["uuid"],
               "verbsDescription": [
                   {"action": "findby", "method": "GET", "path": "/v1/things"},
                   {"action": "create", "method": "POST", "path": "/v1/things"},
                   {"action": "get", "method": "GET", "path": "/v1/things/{uuid}",
                    "requestFieldMapping": [{"inPath": "uuid", "inCustomResource": "spec.name"}]},
               ]}
        res.update(over)
        return {"spec": {"oasPath": "configmap://x/y/thing.yaml", "resource": res}}

    cases, failures = [], []

    msgs = {}

    def case(name, rdoc, oas, want_check, want_actionable, want_msg=None):
        """want_msg: a substring the row's MESSAGE must contain.

        ASSERTING ONLY THE LETTER IS NOT ENOUGH, and a surviving mutant proved it
        (found by openstack-nova-operator-kog porting this screen, their #137).
        Deleting check A's "path not declared" branch leaves the METHOD branch to
        catch an absent path -- because a path that does not exist has no methods
        -- so an A row still appears, actionable, and all ten cases passed. The
        message it printed was false in the worst way available: "the path is
        declared but the method is not", about a path that is not declared.
        Same hazard in E, whose two branches make OPPOSITE claims (a property
        KEPT in spec versus one SUPPRESSED from it), so either branch can cover
        for the other's removal while inverting the meaning.
        """
        try:
            rows = evaluate(rdoc, oas, "thing")
        except Void as v:
            got = [("VOID", str(v), "", True)]
        else:
            got = rows
        hit = [r for r in got if r[0] == want_check]
        ok = bool(hit) and (not want_actionable or any(r[3] for r in hit))
        if ok and want_msg is not None:
            ok = any(want_msg in r[2] for r in hit)
            if not ok:
                failures.append("%s: an %s row appeared but no message contained %r -- got %r"
                                % (name, want_check, want_msg, [r[2] for r in hit]))
        msgs[name] = [r[2] for r in hit]
        cases.append((name, ok))
        if not ok and not any(name in f for f in failures):
            # EVERY false case must reach `failures`. It did not: this used to be
            # `if not ok and want_msg is None`, so a case carrying a want_msg that
            # failed the letter/actionability check was recorded False and counted
            # zero. A mutant reverting the check-D guard then reported
            # "15 cases, 0 failure(s)" with a case printed as FAIL above it.
            failures.append("%s: wanted check %s (actionable=%s), got %s"
                            % (name, want_check, want_actionable, [(r[0], r[1], r[3]) for r in got]))

    # A: a path the OAS does not declare
    bad = rd(); bad["spec"]["resource"]["verbsDescription"][0]["path"] = "/v1/nope"
    case("A inert path", bad, BASE_OAS, "A", True, "path not declared in the OAS")
    # A: right path, wrong method
    bad = rd(); bad["spec"]["resource"]["verbsDescription"][0]["method"] = "PUT"
    case("A inert method", bad, BASE_OAS, "A", True, "is declared but method")
    # B: identifier not in the create body
    bad = rd(identifiers=["serial"],
             verbsDescription=rd()["spec"]["resource"]["verbsDescription"])
    case("B unreadable identifier", bad, BASE_OAS, "B", True)
    # C: mapping source that resolves nowhere
    bad = rd(); bad["spec"]["resource"]["verbsDescription"][2]["requestFieldMapping"] = [
        {"inPath": "uuid", "inCustomResource": "spec.nonesuch"}]
    case("C unresolvable mapping", bad, BASE_OAS, "C", True)
    # D: findby identifier absent from the collection item, no transform
    thin = {"paths": dict(BASE_OAS["paths"])}
    thin["paths"]["/v1/things"] = {
        "get": {"responses": {"200": {"content": {"application/json": {"schema": {
            "type": "object", "properties": {"things": {"type": "array", "items": {
                "type": "object", "properties": {"uuid": {}}}}}}}}}}},
        "post": BASE_OAS["paths"]["/v1/things"]["post"]}
    case("D findby cannot match", rd(), thin, "D", True)
    # D: ... rescued by a transform
    saved = rd(); saved["spec"]["resource"]["verbsDescription"][0]["responseTransform"] = \
        {"inline": "if .uuid then .name = .uuid else . end"}
    case("D rescued by transform", saved, thin, "D", False)
    # E: unmapped path param absent from the create body
    e = rd(); e["spec"]["resource"]["verbsDescription"][2].pop("requestFieldMapping")
    case("E unmapped path param", e, BASE_OAS, "E", False, "is unmapped and absent")
    # VOID: no verbs at all
    v = rd(verbsDescription=[])
    case("VOID no verbs", v, BASE_OAS, "VOID", True)
    # VOID: an OAS with no paths
    case("VOID empty OAS", rd(), {"paths": {}}, "VOID", True)

    # E, the OTHER branch: a MAPPED path param suppressed from spec. Its message
    # must be the suppression one, not the "kept as a spec property" one.
    case("E mapped param suppressed", rd(), BASE_OAS, "E", False, "is suppressed from spec")

    # The heuristic must fire on a LEAF reference, which is how a transform for a
    # dotted identifier is actually written. nova's port proved the old substring
    # test could not fire at all against enveloped payloads.
    dotted = rd(identifiers=["thing.name"])
    dotted["spec"]["resource"]["verbsDescription"][0]["responseTransform"] = \
        {"inline": "{name: .things[0].name}"}
    case("D dotted identifier rescued by leaf match", dotted, thin, "D", False, "HEURISTIC")

    # ... and must NOT fire on a coincidental substring, which is the direction that
    # would hide a real finding.
    colliding = rd(identifiers=["name"])
    colliding["spec"]["resource"]["verbsDescription"][0]["responseTransform"] = \
        {"inline": "{hostname_suffix: .x}"}
    case("D substring collision stays ACTIONABLE", colliding, thin, "D", True,
         "no responseTransform")

    # CROSS-ASSERTIONS: each pair of branches must be DISTINGUISHABLE by message.
    # This is what kills the mutants; the per-case checks alone do not.
    for a, b in (("A inert path", "A inert method"),
                 ("E unmapped path param", "E mapped param suppressed")):
        ma, mb = set(msgs.get(a) or []), set(msgs.get(b) or [])
        ok = bool(ma) and bool(mb) and not (ma & mb)
        cases.append(("messages differ: %s vs %s" % (a, b), ok))
        if not ok:
            failures.append("%s and %s are not distinguishable by message: %r vs %r"
                            % (a, b, sorted(ma), sorted(mb)))

    # THE #421 BOUNDARY, ASSERTED ON THE REAL DOCUMENTS, BOTH DIRECTIONS.
    # This is the arm that turns a retracted note into a regression test: below
    # 0.52.0 a self-referencing mapping deletes its spec property and power's three
    # mappings stop resolving; at or above it they resolve. If upstream ever reverts
    # #421, or if someone "simplifies" the version rule away, one of these flips.
    try:
        import yaml as _y
        _rd = _y.safe_load(open("manifests/restdefinition-power.yaml"))
        _oas = _y.safe_load(open("oas/ironic-power.yaml"))
    except Exception as _e:
        cases.append(("#421 boundary on real documents", False))
        failures.append("could not load the power documents (%s) -- VOID, not a pass" % _e)
    else:
        _below = [r for r in evaluate(_rd, _oas, "power", (0, 51, 9)) if r[0] == "C"]
        _at = [r for r in evaluate(_rd, _oas, "power", MIN_SELFREF_SURVIVES) if r[0] == "C"]
        ok_below = len(_below) == 3 and all(r[3] for r in _below)
        ok_at = len(_at) == 0
        cases.append(("#421 boundary: 0.51.9 suppresses -> 3 actionable C rows", ok_below))
        cases.append(("#421 boundary: 0.52.0 survives   -> 0 C rows", ok_at))
        if not ok_below:
            failures.append("below 0.52.0 expected 3 actionable C rows, got %s"
                            % [(r[1], r[3]) for r in _below])
        if not ok_at:
            failures.append("at 0.52.0 expected 0 C rows, got %s" % [r[1] for r in _at])

    # THE C/E INVARIANT, asserted across every real document at three versions.
    #
    # Not an expectation per case -- an invariant, because the bug it catches passes
    # every per-case arm. The openstack-nova-operator-kog session had the version
    # rule in their E rows only, so below 0.52.0 their screen said the property was
    # deleted (E, actionable) AND that the mapping reading it resolved fine (C,
    # silent). Two checks, one fact, opposite answers in the same run -- and the
    # SILENT one was the reassuring one.
    #
    # Direction matters. `C-leaves subset of E` would be WRONG: a C row also fires
    # when an author names a field that simply does not exist, which E has no reason
    # to report. The sound direction is the one that catches the divergence:
    #
    #     a SELF-REFERENCING param that E reports suppressed MUST produce a C row
    #
    # It holds structurally here because survival is computed once and B, C and E all
    # consume it -- which is exactly the property a later refactor could remove
    # without failing anything else.
    try:
        import yaml as _y, glob as _g, os as _o
        _slugs = [_o.path.basename(f)[len("restdefinition-"):-len(".yaml")]
                  for f in sorted(_g.glob("manifests/restdefinition-*.yaml"))]
        assert _slugs, "no manifests found"
    except Exception as _e:
        cases.append(("C/E invariant across real documents", False))
        failures.append("could not enumerate manifests (%s) -- VOID, not a pass" % _e)
    else:
        violations, checked, saw_discriminator = [], 0, False

        # THE REAL CORPUS CANNOT DISCRIMINATE THE TWO DIRECTIONS, so it is not enough.
        # Every shipped manifest names a field that exists, so `E - C` and `E ^ C` are
        # identical over all 21 document-versions and a symmetric invariant SURVIVES.
        # Measured: mutating `-` to `^` left 19 cases passing. The corpus therefore
        # carries a synthetic document whose mapping names a field that does not
        # exist, which the sound direction allows and the symmetric one flags.
        _typo = ({"spec": {"oasPath": "configmap://x/y/thing.yaml", "resource": {
                    "kind": "Thing", "identifiers": ["name"],
                    "additionalStatusFields": ["uuid"],
                    "verbsDescription": [
                        {"action": "findby", "method": "GET", "path": "/v1/things"},
                        {"action": "create", "method": "POST", "path": "/v1/things"},
                        {"action": "get", "method": "GET", "path": "/v1/things/{uuid}",
                         "requestFieldMapping": [
                             {"inPath": "uuid", "inCustomResource": "spec.nonesuch"}]}]}}},
                 BASE_OAS)

        _corpus = [(s_, None) for s_ in _slugs] + [("<synthetic typo>", _typo)]
        for _ver in ((0, 50, 0), (0, 52, 0), (0, 59, 0)):
            for _slug, _inline in _corpus:
                try:
                    if _inline is not None:
                        _rd, _oas = _inline
                    else:
                        _rd = _y.safe_load(open("manifests/restdefinition-%s.yaml" % _slug))
                        _key = _rd["spec"]["oasPath"].rsplit("/", 1)[-1]
                        _oasp = next(c for c in ("oas/" + _key.replace("_", "-"),
                                                 "oas/ironic-%s.yaml" % _slug) if _o.path.exists(c))
                        _oas = _y.safe_load(open(_oasp))
                    _rows = evaluate(_rd, _oas, _slug, _ver)
                except Exception:
                    continue
                # which suppressed params were self-referencing?
                _selfref = set()
                for _v in (_rd["spec"]["resource"].get("verbsDescription") or []):
                    for _m in (_v.get("requestFieldMapping") or []):
                        _src = _m.get("inCustomResource", "")
                        if _src.startswith("spec.") and _src.split(".")[-1] == _m.get("inPath"):
                            _selfref.add(_m["inPath"])
                _e_sup = {r[1] for r in _rows if r[0] == "E"} & _selfref
                _c_leaves = {r[1].split("<-")[1].split(".")[-1]
                             for r in _rows if r[0] == "C" and "<-spec." in r[1]}
                checked += 1
                if _c_leaves - _e_sup:
                    # a C row with no matching suppressed-E row: the shape that tells
                    # the sound invariant apart from the symmetric one
                    saw_discriminator = True
                for _pp in _e_sup - _c_leaves:
                    violations.append("%s@%s: E says %s suppressed but C is SILENT"
                                      % (_slug, ".".join(map(str, _ver)), _pp))
        ok = not violations and checked > 0 and saw_discriminator
        cases.append(("C/E invariant: %d document-versions, discriminator present=%s"
                      % (checked, saw_discriminator), ok))
        if checked == 0:
            failures.append("C/E invariant checked NOTHING -- VOID, not a pass")
        elif not saw_discriminator:
            # Without this the arm passes whether the invariant is sound OR symmetric.
            failures.append("C/E invariant never saw a C-row-without-E-row case, so it "
                            "cannot tell the sound direction from the symmetric one -- "
                            "VOID, not a pass")
        for _v in violations:
            failures.append("C/E invariant: " + _v)

    # THE ARM THAT PROVES THE UNSOUND DIRECTION IS NOT IMPLEMENTED.
    #
    # The invariant above is one-directional on purpose. Nothing yet proves the
    # SYMMETRIC version (`E-suppressed == C-fired`) is not in force, and a future
    # "simplification" to an equality would pass every other arm here. nova wrote
    # this arm after I flagged the direction; one of their two had the symmetric
    # form, and it held only because their fixture contained no typo.
    #
    # The ordinary case it would break on: a mapping naming a field that does not
    # exist. That is the most mundane real finding a screen can make -- and under the
    # symmetric rule it is reported as the screen CONTRADICTING ITSELF. A false
    # positive about the instrument is worse than a missed finding, because the next
    # person mutes the check.
    typo = rd()
    typo["spec"]["resource"]["verbsDescription"][2]["requestFieldMapping"] = [
        {"inPath": "uuid", "inCustomResource": "spec.nonesuch"}]
    typo_rows = evaluate(typo, BASE_OAS, "thing", (0, 59, 0))
    c_rows = [r for r in typo_rows if r[0] == "C"]
    e_sup = [r for r in typo_rows if r[0] == "E" and r[1] == "nonesuch"]
    ok = bool(c_rows) and any(r[3] for r in c_rows) and not e_sup
    cases.append(("a typo'd mapping source is a C FINDING, not a self-contradiction", ok))
    if not ok:
        failures.append("a mapping reading spec.nonesuch must yield an actionable C row "
                        "and NO suppressed-E row for 'nonesuch'; got C=%s E=%s"
                        % ([(r[1], r[3]) for r in c_rows], [r[1] for r in e_sup]))

    # the negative control: a clean document must produce NO actionable row
    clean = evaluate(rd(), BASE_OAS, "thing")
    clean_actionable = [r for r in clean if r[3]]
    cases.append(("negative control: clean doc has no actionable row", not clean_actionable))
    if clean_actionable:
        failures.append("clean document produced actionable rows: %s" % clean_actionable)

    for name, ok in cases:
        print("  %-48s %s" % (name, "ok" if ok else "FAIL"))
    print("  selftest: %d cases, %d failure(s)" % (len(cases), len(failures)))
    for f in failures: print("    " + f)
    return 1 if failures else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifests", default="manifests/restdefinition-*.yaml")
    ap.add_argument("--oas-dir", default="oas")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--oasgen-version", default=None,
                    help="oasgen version to answer for; defaults to the repo's own "
                         "KRATEO_OASGEN_VERSION pin in the Makefile")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    try:
        if a.oasgen_version:
            version, src = parse_version(a.oasgen_version), "--oasgen-version"
        else:
            pinned = repo_pinned_version()
            if not pinned:
                raise Void("no --oasgen-version given and KRATEO_OASGEN_VERSION is not "
                           "in the Makefile, so there is no version to answer for")
            version, src = parse_version(pinned), "Makefile KRATEO_OASGEN_VERSION"
        sys.exit(run(a.manifests, a.oas_dir, version, src))
    except Void as v:
        print("VOID: %s" % v); print("NOT a pass."); sys.exit(2)
