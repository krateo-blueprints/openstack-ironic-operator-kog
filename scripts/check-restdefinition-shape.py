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


# Conditions that are real, measured, upstream's, and documented. Matched exactly
# so a NEW instance of the same shape is still actionable.
KNOWN = {
    ("power", "E", "node_id"):
        "oasgen prunes a mapped path param's spec property; no NodeCreate-style "
        "body declares node_id. Measured 2026-11-04 on live CRDs (oasgen 0.50.0).",
    ("provision", "E", "node_id"):
        "same mechanism as power.",
    ("port", "E", "uuid"):
        "Port's delete maps uuid<-status.uuid, which suppresses spec.uuid; its get "
        "leaves {uuid} unmapped. Net: absent. Port is addressed by address, so this "
        "is inert rather than broken.",
}

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


def evaluate(rd, oas, slug):
    """Return a list of (check, subject, message, actionable). Raises Void."""
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
    asf = set(res.get("additionalStatusFields") or [])
    reader = by_action.get("get") or by_action.get("findby")
    status = set(asf)
    if reader:
        status |= response_item_props(oas, reader["path"], reader["method"].lower())[1]

    findby = by_action.get("findby")
    transform = (findby or {}).get("responseTransform")
    xf_text = str(transform) if transform else ""

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
        if ident in xf_text:
            out.append(("B", ident, "not a spec field, but the findby responseTransform supplies it", False))
        else:
            out.append(("B", ident, "identifier is not a spec field and no transform supplies it", True))

    # C -- mapping sources must resolve
    for v in verbs:
        for m in v.get("requestFieldMapping") or []:
            src = m.get("inCustomResource", "")
            root, leaf = (src.split(".") + [""])[0], src.split(".")[-1]
            pool, pname = (spec, "the create body") if root == "spec" else (status, "status")
            if leaf not in pool:
                # The suppression IS the cause of the unresolvable source, so where
                # the suppression is KNOWN this row is a consequence, not a finding.
                known = (slug, "E", leaf) in KNOWN
                out.append(("C", "%s %s<-%s" % (v.get("action"), m.get("inPath"), src),
                            "source does not resolve in %s" % pname, not known))

    # D -- findby must be able to match
    if findby:
        _w, item = response_item_props(oas, findby["path"], (findby.get("method") or "get").lower())
        for ident in res.get("identifiers") or []:
            if ident in item:
                continue
            if ident in xf_text:
                out.append(("D", ident, "absent from the collection item; the responseTransform manufactures it", False))
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
    # a mapped path param is suppressed from spec; report when nothing rescues it
    for v in verbs:
        for m in v.get("requestFieldMapping") or []:
            pp = m.get("inPath")
            if pp and pp not in spec:
                out.append(("E", pp, "mapped path param is suppressed from spec and no body schema "
                                     "declares it, so spec.%s does not exist" % pp, False))
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


def run(manifest_glob, oas_dir):
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
            rows = evaluate(rd, oas, slug)
        except Void as v:
            voids.append(str(v)); continue
        tag = "live" if slug in LIVE else "NOT EXERCISED"
        for check, subject, msg, act in rows:
            k = (slug, check, subject)
            if k in KNOWN:
                known_hit.add(k)
                reported.append("  %-16s [%s] %-24s %s\n        known: %s" % (slug, check, subject, msg, KNOWN[k]))
            elif act:
                actionable.append("  %-16s (%s) [%s] %-24s %s" % (slug, tag, check, subject, msg))
            else:
                reported.append("  %-16s [%s] %-24s %s" % (slug, check, subject, msg))

    print("checked %d RestDefinition(s): %s" % (len(files),
          ", ".join(os.path.basename(f)[len("restdefinition-"):-len(".yaml")] for f in files)))
    if voids:
        print("\nVOID -- the screen could not form an opinion on these. NOT a pass:")
        for v in voids: print("  " + v)
    if reported:
        print("\nreported, not fatal (%d):" % len(reported))
        for r in reported: print(r)
    if actionable:
        print("\nACTIONABLE (%d):" % len(actionable))
        for a in actionable: print(a)

    # The walker must still be reading the file: the three KNOWN rows must match.
    missing = set(KNOWN) - known_hit
    if missing and not voids:
        print("\nCALIBRATION FAILED: these KNOWN conditions were not matched, so this "
              "screen is no longer reading what it thinks it reads:")
        for m in sorted(missing): print("  %s" % (m,))
        return 2

    print("\nactionable=%d  reported=%d  void=%d  known-matched=%d/%d"
          % (len(actionable), len(reported), len(voids), len(known_hit), len(KNOWN)))
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

    def case(name, rdoc, oas, want_check, want_actionable):
        try:
            rows = evaluate(rdoc, oas, "thing")
        except Void as v:
            got = [("VOID", str(v), "", True)]
        else:
            got = rows
        hit = [r for r in got if r[0] == want_check]
        ok = bool(hit) and (not want_actionable or any(r[3] for r in hit))
        cases.append((name, ok))
        if not ok:
            failures.append("%s: wanted check %s (actionable=%s), got %s"
                            % (name, want_check, want_actionable, [(r[0], r[1], r[3]) for r in got]))

    # A: a path the OAS does not declare
    bad = rd(); bad["spec"]["resource"]["verbsDescription"][0]["path"] = "/v1/nope"
    case("A inert path", bad, BASE_OAS, "A", True)
    # A: right path, wrong method
    bad = rd(); bad["spec"]["resource"]["verbsDescription"][0]["method"] = "PUT"
    case("A inert method", bad, BASE_OAS, "A", True)
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
    case("E unmapped path param", e, BASE_OAS, "E", False)
    # VOID: no verbs at all
    v = rd(verbsDescription=[])
    case("VOID no verbs", v, BASE_OAS, "VOID", True)
    # VOID: an OAS with no paths
    case("VOID empty OAS", rd(), {"paths": {}}, "VOID", True)

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
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    try:
        sys.exit(run(a.manifests, a.oas_dir))
    except Void as v:
        print("VOID: %s" % v); print("NOT a pass."); sys.exit(2)
