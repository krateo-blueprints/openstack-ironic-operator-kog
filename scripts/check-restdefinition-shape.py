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
        "why": ("NOT self-referencing -- Port's delete AND get both map "
                "uuid <- status.uuid, so clause 2 cannot apply, clause 3 has no "
                "unmapped verb to fire on, and uuid is not in the create body. Absent "
                "at every version, which is CORRECT: Ironic generates a Port's uuid "
                "on POST, so no author could supply it.\n"
                "THIS TEXT WAS WRONG UNTIL 2026-11-12. It read 'its get leaves "
                "{uuid} unmapped. Neither survival clause can apply at ANY version' "
                "-- while the live CRD said PRESENT and REQUIRED, because an unmapped "
                "path param becomes a spec field (clause 3), which this model did not "
                "have. Every NEW Port was being rejected. And it was the entry I had "
                "declared could never retire. An exemption table is where an "
                "incomplete model goes to hide: all three entries here were papering "
                "over a missing clause."),
        "upto": None,   # applies at every version, now that get is mapped
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
    # CLAUSE 3 FIRST, because it WINS over the mapped-verb suppression. A path
    # parameter that ANY verb leaves unmapped becomes a spec property -- and a
    # required one, since path params are required. Measured on Port at 0.59.0, which
    # had BOTH an unmapped get and a status-mapped delete, and whose spec.uuid was
    # PRESENT and required. Discriminator in the same run: mapping the get too made
    # it ABSENT. tests/oasgen-generator/ironic_port_uuid_test.go.
    unmapped_params = {}
    for v in verbs:
        mapped = {m.get("inPath") for m in (v.get("requestFieldMapping") or [])}
        for pp in re.findall(r"\{(\w+)\}", v.get("path") or ""):
            if pp not in mapped:
                unmapped_params.setdefault(pp, v.get("action"))
    for pp in unmapped_params:
        spec.add(pp)

    suppressed_params = {}
    for v in verbs:
        for m in v.get("requestFieldMapping") or []:
            pp = m.get("inPath")
            if not pp or pp in spec:
                continue                       # clause 1 or 3 already rescued it
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

    def findby_policy():
        """The EFFECTIVE identifiersMatchPolicy. OR IS THE DEFAULT.

        restclient.go:1208-1211, verbatim:

            policy := strings.ToLower(u.IdentifiersMatchPolicy)
            if policy == "" || (policy != "and" && policy != "or") {
                policy = "or" // Default to "or" if not specified or invalid
            }

        This screen DEFAULTED TO "AND" until 2026-11-12, which was wrong in the
        dangerous direction: AND demands every identifier be readable before
        findby "can match", so check F under-reported -- a kind whose findby
        really can match, and whose create is therefore unreachable, passed.
        Caught by openstack-nova-operator-kog reviewing the port (their #148).

        The normalise-invalid-to-or half is NOT modelled, deliberately. A
        kubebuilder Enum=AND;OR reaches the generated CRD, so the apiserver
        refuses any other value at admission and the manifest never applies.
        restclient.go says so at 1197-1207 and notes that two readers took the
        opposite conclusion from that code in one day. So: absent -> OR,
        AND -> AND, anything else is a manifest the cluster will reject."""
        # READ FROM THE VERB, NOT THE RESOURCE. identifiersMatchPolicy is declared in
        # `type VerbsDescription struct` (oasgen apis/restdefinitions/v1alpha1/types.go
        # :633, struct at :549), so it lives at
        #     spec.resource.verbsDescription[].identifiersMatchPolicy
        # and NOT at spec.resource.identifiersMatchPolicy. A screen reading the resource
        # level can never see a declared policy: it reads None, defaults to OR, and a
        # manifest that genuinely says AND is analysed as OR. That is the mirror of the
        # bug this function fixes -- openstack-nova-operator-kog#148 had exactly it, and
        # their fixture put the key at the resource level too, so the arm passed while
        # testing nothing. Guarded by a self-test arm below.
        #
        # EXACT MATCH, no .upper(). The marker is `+kubebuilder:validation:Enum=AND;OR`,
        # so the apiserver refuses `and`, `Or`, `And` and anything else at admission.
        # Upper-casing would give a manifest that CANNOT APPLY the semantics of one that
        # can -- screening `and` as AND while the cluster rejects it outright.
        raw = findby.get("identifiersMatchPolicy")
        if raw is None or raw == "":
            return "OR"
        if raw in ("AND", "OR"):
            return raw
        return "INVALID:%s" % raw

    # The identifiers split by HOW they are readable, computed once because both
    # D and F need it and they must not disagree about it.
    #   in_item  -- genuinely declared in the findby response's item schema
    #   by_xf    -- only reachable because the responseTransform appears to supply
    #               it, which is a LEAF-MATCH HEURISTIC and not an evaluated jq
    idents = res.get("identifiers") or []
    if findby:
        _w, _fitem = response_item_props(oas, findby["path"],
                                         (findby.get("method") or "get").lower())
    else:
        _fitem = set()
    in_item = {i for i in idents if i in _fitem}
    by_xf = {i for i in idents if i not in in_item and transform_supplies(i)}
    readable = in_item | by_xf

    def findby_can_match():
        """Can findby match an item at all, under its effective policy?

        The inverse of check D, and NOT redundant with it: D asks the question
        per identifier, to report one that cannot be read. This asks it of the
        VERB -- whether a match is possible -- which is what decides whether
        Observe can ever report the resource absent."""
        if not findby or not idents:
            return False
        pol = findby_policy()
        # An INVALID policy cannot apply, so there is no behaviour to predict. Answer
        # the permissive side (OR) so F still reports rather than going quiet, and let
        # the dedicated row below say the manifest is refused.
        if pol != "AND":
            return bool(readable)
        return len(readable) == len(idents)

    # AN ACTION-ONLY KIND: `create` is its sole effect, and Observe decides
    # whether to dispatch it. No `get`, so existence is decided by findby
    # (IsResourceKnown cannot build a get call, so Observe takes the findby
    # path). No `update`, so a reported difference cannot drive the endpoint
    # either. For such a kind the create is reached EXACTLY ONE WAY: Observe
    # reporting the resource ABSENT.
    action_only = bool(create) and "get" not in by_action and "update" not in by_action

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

    # D/F SUPPRESSION. A non-conforming policy means the manifest is refused at
    # admission, so every conclusion D and F would draw is advice about a document that
    # cannot exist -- printing it is noise that competes with the one row that matters.
    # Adopted from openstack-nova-operator-kog#149. The control arm matters as much as
    # the suppression: "G suppresses D and F" and "F stopped working" look identical
    # from the outside, so the self-test asserts the same kind with a VALID policy
    # still reports F.
    policy_refused = bool(findby) and findby_policy().startswith("INVALID:")

    # D -- findby must be able to match
    if findby and not policy_refused:
        _w, item = response_item_props(oas, findby["path"], (findby.get("method") or "get").lower())
        for ident in res.get("identifiers") or []:
            if ident in item:
                continue
            if transform_supplies(ident):
                out.append(("D", ident, "absent from the collection item; the responseTransform appears "
                                        "to manufacture it (HEURISTIC: the jq is not evaluated)", False))
            elif action_only:
                # NOT ACTIONABLE, AND THIS CHECK CAUSED A REAL REGRESSION BY
                # BEING SO. For an action-only kind a never-matching findby is
                # the OPERATING MECHANISM, not a defect: not-found is what
                # dispatches the create, and the create is the whole effect.
                #
                # Measured: ironic-node-provision (findby + create, no get, no
                # update) had this transform added on 2026-11-11 in 52ebe5d to
                # satisfy this row and oasgen's identically-worded
                # schemaWarning. It made findby match, so Observe reported
                # exists-and-up-to-date and the PUT was never issued -- every
                # provision transition a silent no-op, nodes stuck at `enroll`.
                # The same transform had been reverted four days earlier in
                # 40bf2ed, whose subject reads "the responseTransform I added
                # today would have silently stopped every transition".
                #
                # So the row stays -- the identifier really is unreadable, and
                # that really does cost observability (additionalStatusFields
                # never populate) -- but it must not read as a defect to fix,
                # because fixing it disables the kind. Check F is the finding
                # for the other direction.
                out.append(("D", ident, "absent from the collection item and no responseTransform, so findby "
                                        "never matches -- which for this ACTION-ONLY kind is what DISPATCHES "
                                        "the create. Not a defect: supplying it would make the create "
                                        "unreachable (see F). The cost is observability, not function: "
                                        "additionalStatusFields never populate", False))
            elif findby_policy() == "OR" and (readable - {ident}):
                # UNDER OR -- THE DEFAULT -- ONE UNREADABLE IDENTIFIER DOES NOT STOP
                # A MATCH. restclient.go adopts on the FIRST declared identifier whose
                # value agrees and never looks at the rest, so with another identifier
                # readable this costs DISCRIMINATION, not function: findby can still
                # match, possibly on a weaker identifier than the author intended.
                # Saying "findby can NEVER match" here would be false.
                out.append(("D", ident, "absent from the collection item, but policy is OR (the "
                                        "DEFAULT) and %s is readable, so findby still matches -- on "
                                        "that one. The cost is DISCRIMINATION, not function: a match "
                                        "may adopt the wrong object. Declare identifiersMatchPolicy: "
                                        "AND to require all of them"
                            % ", ".join(sorted(readable - {ident})), False))
            else:
                out.append(("D", ident, "absent from the collection item and no responseTransform -- "
                                        "and no other identifier is readable either, so findby can "
                                        "NEVER match under policy %s" % findby_policy(), True))

    # G -- identifiersMatchPolicy misdeclared. TWO CAUSES, AND THEY ARE DIFFERENT NEWS.
    # "your policy is doing nothing" and "your manifest will be rejected" lead a reader
    # to different actions, so they must not share a message. Distinction and the
    # pruning mechanism from openstack-nova-operator-kog#149; the CRD was then read
    # here independently, which is the better pair of sources -- they read the CRD the
    # apiserver enforces, I read the struct and the marker.
    if "identifiersMatchPolicy" in res:
        out.append(("G", "resource.identifiersMatchPolicy=%r" % res["identifiersMatchPolicy"],
                    "declared at the RESOURCE level, where the field does not exist -- it "
                    "belongs on the findby VERB (oasgen types.go:633 declares it in "
                    "VerbsDescription). It is PRUNED, not merely unread: spec.resource is a "
                    "structural schema and the CRD's five "
                    "x-kubernetes-preserve-unknown-fields are all on *ApiRef.extras and "
                    "fieldMapping.defaultIfAbsent, none on spec.resource -- verified against "
                    "crds/ogen.krateo.io_restdefinitions.yaml. So the apiserver DROPS the key, "
                    "`kubectl apply` returns 0, and the policy silently defaults to OR: a "
                    "manifest meaning AND is stored, analysed and MATCHED as OR",
                    True))

    # G -- a policy value the apiserver will refuse
    if findby:
        _pol = findby_policy()
        if _pol.startswith("INVALID:"):
            out.append(("G", "findby identifiersMatchPolicy=%r" % _pol.split(":", 1)[1],
                        "not one of AND/OR, so this manifest is REFUSED AT ADMISSION -- a "
                        "different outcome from the pruned resource-level case, and the reader "
                        "acts differently on it. The field carries "
                        "`+kubebuilder:validation:Enum=AND;OR`, so it is not a policy with "
                        "weaker semantics, it is a definition that cannot apply. Case matters: "
                        "`and` is refused too. D and F are SUPPRESSED for this kind below, "
                        "because every conclusion they would draw rests on a policy the "
                        "cluster will never accept",
                        True))

    # F -- an action-only kind whose findby CAN match has an UNREACHABLE create
    #
    # unstructured-runtime pkg/controller/worker.go:964-997, which is the whole
    # argument and the reason this precondition needs no qualifier:
    #
    #     if !observation.ResourceExists {        -> queues ctrlevent.Create
    #     } else if !observation.ResourceUpToDate { -> queues ctrlevent.Update
    #
    # Create is queued ONLY on !ResourceExists. Nothing about ResourceUpToDate
    # can ever produce one. So a matching findby -- which makes Observe return
    # exists=true -- puts the create out of reach regardless of what the drift
    # comparison then decides.
    #
    # WHY THAT MATTERS, because a peer review raised it and the answer changes
    # the SYMPTOM but not the verdict (openstack-nova-operator-kog, 2026-11-12).
    # They confirmed the mechanism on the `compareScope: updatable` path, where
    # scopeFields is empty with no update verb so nothing is compared, and
    # correctly flagged that no manifest here SETS compareScope -- we are on the
    # default scope, where scopeFields = identifiers + additionalStatusFields and
    # is non-empty, so drift IS compared. Their open question was whether F's
    # precondition should therefore be "action-only AND updatable scope".
    #
    # It should not, and the two paths differ only in how the failure LOOKS:
    #   comparison agrees   -> {exists: true, upToDate: true}, logged
    #                          "External resource up-to-date". Silent no-op.
    #                          This is what the lab actually showed, and it is
    #                          expected: the status fields were just populated
    #                          FROM the same response body being compared.
    #   comparison disagrees -> upToDate=false queues an UPDATE, and with no
    #                          update verb restResources.go:1414 logs
    #                          "API action update not found". Still no PUT.
    # Either way the create is unreachable, so the check keys on the verb set.
    if action_only and not policy_refused and findby_can_match():
        subject = "create %s %s" % ((create.get("method") or "").upper(), create.get("path"))
        base = ("UNREACHABLE: this kind has no `get` and no `update`, so its create is dispatched "
                "only when Observe reports the resource ABSENT -- but its findby CAN match "
                "(policy %s; readable: %s), so Observe reports exists-and-up-to-date and the "
                "request is never issued. The kind reports Ready and performs NOTHING."
                % (findby_policy(), ", ".join(sorted(readable)) or "-"))
        # TWO MESSAGES, because the two cases deserve different confidence and a reader
        # must be told which one they have. Adopted from openstack-nova-operator-kog#148.
        # When matchability rests ONLY on the responseTransform, F is standing on a
        # leaf-name heuristic that does not evaluate the jq -- so it says so, and says
        # which way it errs. The self-test asserts the two strings differ.
        if in_item:
            msg = base + (" Drop whatever makes the identifier readable, or give the kind a verb "
                          "that acts on drift.")
        else:
            msg = base + (" NOTE: matchability here rests ONLY on the findby responseTransform "
                          "appearing to supply %s -- a leaf-name HEURISTIC, the jq is not "
                          "evaluated. This errs toward reporting: over-reporting costs a review, "
                          "under-reporting ships a kind that silently does nothing. Removing that "
                          "transform is the fix."
                          % ", ".join(sorted(by_xf)))
        out.append(("F", subject, msg, True))

    # E -- unmapped path params become spec properties
    # CLAUSE 3 IS A FINDING, NOT A NOTE, when nothing can supply the value. An
    # unmapped path param becomes a REQUIRED spec property; if the create body does
    # not declare it, no author can set it and the apiserver REJECTS every new CR.
    # Measured on Port before this was fixed: "spec.uuid: Required value" on a
    # chart-shaped Port, with the same CR carrying spec.uuid accepted as the control.
    # 16 live Ports survived only because updates to an already-violating object pass.
    body_declared = body_props(oas, create["path"], create["method"].lower()) if create else set()
    for pp, action in sorted(unmapped_params.items()):
        if pp in body_declared:
            continue                           # clause 1: the author can set it
        out.append(("E", pp, "path param is unmapped on verb %r, so it becomes a "
                             "REQUIRED spec property -- and the create body does not "
                             "declare it, so no author can supply it and the apiserver "
                             "REJECTS every new CR. Map it, or declare it in the body"
                             % action, True))

    # E also reports what the survival pass above classified as suppressed.
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
    case("E unmapped path param is ACTIONABLE", e, BASE_OAS, "E", True, "REQUIRED spec property")
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

    # F -- AN ACTION-ONLY KIND WHOSE FINDBY CAN MATCH PERFORMS NOTHING.
    # Three arms over one variable: the presence of a verb that can act. Only
    # the kind with neither `get` nor `update` is at risk, because only there
    # does findby's verdict decide whether the create is dispatched at all.
    _v = rd()["spec"]["resource"]["verbsDescription"]          # [findby, create, get]
    _upd = {"action": "update", "method": "DELETE", "path": "/v1/things/{uuid}"}
    for _name, _verbs, _wantF in (
            ("F silent when an `update` verb exists", [_v[0], _v[1], _upd], 0),
            ("F silent when a `get` verb exists",     [_v[0], _v[1], _v[2]], 0),
            ("F fires when there is NEITHER",         [_v[0], _v[1]],        1)):
        _rows = [r for r in evaluate(rd(verbsDescription=_verbs), BASE_OAS, "thing")
                 if r[0] == "F"]
        _ok = len(_rows) == _wantF and all(r[3] for r in _rows)
        if _ok and _wantF:
            _ok = "UNREACHABLE" in _rows[0][2]
        cases.append((_name, _ok))
        if not _ok:
            failures.append("%s: expected %d actionable F row(s) saying UNREACHABLE, got %s"
                            % (_name, _wantF, [(r[1], r[3]) for r in _rows]))

    # D MUST NOT BE ACTIONABLE FOR AN ACTION-ONLY KIND, and the control for this
    # is the "D findby cannot match" case above: the SAME thin OAS, a kind that
    # merely also has a `get`, and there the row IS actionable. One variable.
    _d = [r for r in evaluate(rd(verbsDescription=[_v[0], _v[1]]), thin, "thing")
          if r[0] == "D"]
    _ok = len(_d) == 1 and not _d[0][3] and "ACTION-ONLY" in _d[0][2]
    cases.append(("D downgraded to non-actionable for an action-only kind", _ok))
    if not _ok:
        failures.append("action-only D: expected 1 NON-actionable row saying ACTION-ONLY, got %s"
                        % [(r[1], r[3], r[2][:60]) for r in _d])

    # OR IS THE DEFAULT, AND THIS ARM IS THE REGRESSION TEST FOR GETTING IT WRONG.
    #
    # Two identifiers, only ONE readable in the collection item, action-only, no
    # policy declared. Under the real default (OR, restclient.go:1208-1211) findby
    # CAN match on the readable one, so the create is unreachable and F must fire.
    # Under the "AND" default this screen shipped with until 2026-11-12, readable
    # (1) != declared (2), findby_can_match() was False, and F stayed SILENT on a
    # kind that silently does nothing. That is the direction that ships outages.
    _ao2 = rd(identifiers=["name", "serial"],
              verbsDescription=[_v[0], _v[1]])          # findby + create, no get/update
    _f_or = [r for r in evaluate(_ao2, BASE_OAS, "thing") if r[0] == "F"]
    _ok = len(_f_or) == 1 and _f_or[0][3] and "policy OR" in _f_or[0][2]
    cases.append(("OR is the default: 1-of-2 identifiers readable -> F FIRES", _ok))
    if not _ok:
        failures.append("OR default: expected 1 actionable F row naming policy OR, got %s"
                        % [(r[1], r[3], r[2][:70]) for r in _f_or])

    # ...and the discriminator for that arm: declaring AND explicitly must SILENCE F,
    # because then the unreadable identifier really does block every match. Without
    # this, the arm above could pass on a check that ignores the policy entirely.
    _ao2_and = rd(identifiers=["name", "serial"],
                  verbsDescription=[dict(_v[0], identifiersMatchPolicy="AND"), _v[1]])
    _f_and = [r for r in evaluate(_ao2_and, BASE_OAS, "thing") if r[0] == "F"]
    _ok = len(_f_and) == 0
    cases.append(("...and declaring AND silences F (one variable: the policy)", _ok))
    if not _ok:
        failures.append("AND declared: expected 0 F rows, got %s" % [(r[1], r[3]) for r in _f_and])

    # THE POLICY IS A PER-VERB FIELD. Three arms over one variable: WHERE the key sits.
    #
    # oasgen declares IdentifiersMatchPolicy in `type VerbsDescription struct`
    # (types.go:633, struct at :549), so it lives at
    # spec.resource.verbsDescription[].identifiersMatchPolicy. A screen reading the
    # RESOURCE level can never see a declared policy -- it reads None, defaults to OR,
    # and a manifest that genuinely says AND is analysed as OR. nova's #148 had exactly
    # that, AND their fixture placed the key at the resource level too, so the arm
    # passed while testing nothing. These arms exist so a future "fix" that moves the
    # read cannot pass.
    _ao2v = rd(identifiers=["name", "serial"],
               verbsDescription=[dict(_v[0], identifiersMatchPolicy="AND"), _v[1]])
    _ao2r = rd(identifiers=["name", "serial"], verbsDescription=[_v[0], _v[1]])
    _ao2r["spec"]["resource"]["identifiersMatchPolicy"] = "AND"   # the WRONG level

    _f_verb = [r for r in evaluate(_ao2v, BASE_OAS, "thing") if r[0] == "F"]
    _ok = len(_f_verb) == 0
    cases.append(("policy on the VERB is SEEN (AND -> F silent)", _ok))
    if not _ok:
        failures.append("verb-level AND: expected 0 F rows, got %s" % [(r[1], r[3]) for r in _f_verb])

    # At the resource level the policy must NOT be honoured -- the effective policy is
    # still OR, so F fires -- AND the misplacement must be REPORTED, because silently
    # analysing it as OR is how the real bug hid.
    _rows_r = evaluate(_ao2r, BASE_OAS, "thing")
    _f_res = [r for r in _rows_r if r[0] == "F"]
    _g_res = [r for r in _rows_r if r[0] == "G"]
    _ok = len(_f_res) == 1 and _f_res[0][3]
    cases.append(("policy at the RESOURCE level is NOT honoured (F still fires)", _ok))
    if not _ok:
        failures.append("resource-level AND: expected 1 actionable F row, got %s"
                        % [(r[1], r[3]) for r in _f_res])
    _ok = len(_g_res) == 1 and _g_res[0][3] and "RESOURCE level" in _g_res[0][2]
    cases.append(("...and the misplacement is REPORTED as a G finding", _ok))
    if not _ok:
        failures.append("resource-level AND: expected 1 actionable G row naming the RESOURCE "
                        "level, got %s" % [(r[1], r[3], r[2][:60]) for r in _g_res])

    # CASE MATTERS: the Enum is exactly AND;OR, so `and` is refused at admission. It
    # must be a G finding, not silently given AND's semantics by an .upper().
    _ao2bad = rd(identifiers=["name", "serial"],
                 verbsDescription=[dict(_v[0], identifiersMatchPolicy="and"), _v[1]])
    _g_bad = [r for r in evaluate(_ao2bad, BASE_OAS, "thing") if r[0] == "G"]
    _ok = len(_g_bad) == 1 and _g_bad[0][3] and "Case matters" in _g_bad[0][2]
    cases.append(("lowercase `and` is a REFUSED manifest, not a policy", _ok))
    if not _ok:
        failures.append("lowercase and: expected 1 actionable G row mentioning case, got %s"
                        % [(r[1], r[3], r[2][:60]) for r in _g_bad])

    # G'S TWO CAUSES MUST DIFFER BY CAUSE, not just by subject. A reader acts
    # differently on "your policy was silently dropped and apply returned 0" than on
    # "your manifest will be rejected", so a shared message is a defect even though
    # both rows are actionable. Distinction from openstack-nova-operator-kog#149.
    _g_pruned = [r for r in evaluate(_ao2r, BASE_OAS, "thing") if r[0] == "G"]
    _g_refused = [r for r in evaluate(_ao2bad, BASE_OAS, "thing") if r[0] == "G"]
    _ok = (len(_g_pruned) == 1 and len(_g_refused) == 1
           and _g_pruned[0][2] != _g_refused[0][2]
           and "PRUNED" in _g_pruned[0][2] and "REFUSED AT ADMISSION" in _g_refused[0][2]
           and "PRUNED" not in _g_refused[0][2])
    cases.append(("G's PRUNED and REFUSED messages differ BY CAUSE", _ok))
    if not _ok:
        failures.append("G causes: pruned=%r refused=%r"
                        % ([r[2][:70] for r in _g_pruned], [r[2][:70] for r in _g_refused]))

    # A REFUSED POLICY SUPPRESSES D AND F, because their conclusions would be advice
    # about a document that cannot exist. The CONTROL is the whole point: "G suppresses
    # D and F" and "F stopped working" look identical from outside, so the same kind
    # with a VALID policy must still report F.
    _rows_bad = evaluate(_ao2bad, BASE_OAS, "thing")
    _ok = not [r for r in _rows_bad if r[0] in ("D", "F")]
    cases.append(("a refused policy SUPPRESSES D and F", _ok))
    if not _ok:
        failures.append("refused policy: expected no D/F rows, got %s"
                        % [(r[0], r[1]) for r in _rows_bad if r[0] in ("D", "F")])
    _ao2valid = rd(identifiers=["name", "serial"],
                   verbsDescription=[dict(_v[0], identifiersMatchPolicy="OR"), _v[1]])
    _f_valid = [r for r in evaluate(_ao2valid, BASE_OAS, "thing") if r[0] == "F"]
    _ok = len(_f_valid) == 1 and _f_valid[0][3]
    cases.append(("...CONTROL: a VALID policy on the same kind still reports F", _ok))
    if not _ok:
        failures.append("valid-policy control: expected 1 actionable F row, got %s"
                        % [(r[1], r[3]) for r in _f_valid])

    # F'S TWO MESSAGES MUST BE DIFFERENT STRINGS, and the heuristic-only one must say
    # so. Adopted from openstack-nova-operator-kog#148. A single message would let a
    # reader act on a leaf-match guess as though it were a read schema.
    _f_decl = [r for r in evaluate(rd(verbsDescription=[_v[0], _v[1]]), BASE_OAS, "thing")
               if r[0] == "F"]                                  # `name` IS in the item
    _f_heur = [r for r in evaluate(
        rd(verbsDescription=[dict(_v[0], responseTransform={"inline": ".name = .uuid | .name"}),
                             _v[1]]), thin, "thing") if r[0] == "F"]   # only the transform
    _ok = (len(_f_decl) == 1 and len(_f_heur) == 1
           and _f_decl[0][2] != _f_heur[0][2]
           and "HEURISTIC" in _f_heur[0][2] and "HEURISTIC" not in _f_decl[0][2])
    cases.append(("F's declared-identifier and heuristic-only messages DIFFER", _ok))
    if not _ok:
        failures.append("F message split: declared=%r heuristic=%r"
                        % ([r[2][:80] for r in _f_decl], [r[2][:80] for r in _f_heur]))

    # D UNDER OR WITH ANOTHER IDENTIFIER READABLE IS NOT A FUNCTION PROBLEM.
    # restclient.go adopts on the FIRST agreeing identifier and never looks at the
    # rest, so "findby can NEVER match" would be a false claim here. Non-actionable,
    # and the message must say DISCRIMINATION. Control: the existing "D findby cannot
    # match" case, where NOTHING is readable and the row stays actionable.
    _d_or = [r for r in evaluate(rd(identifiers=["name", "serial"]), BASE_OAS, "thing")
             if r[0] == "D"]                       # has a `get`, so not action-only
    _ok = (len(_d_or) == 1 and _d_or[0][1] == "serial" and not _d_or[0][3]
           and "DISCRIMINATION" in _d_or[0][2])
    cases.append(("D under OR with another identifier readable -> NOT actionable", _ok))
    if not _ok:
        failures.append("D under OR: expected 1 non-actionable `serial` row saying "
                        "DISCRIMINATION, got %s" % [(r[1], r[3], r[2][:70]) for r in _d_or])

    # THE TRANSFORM ITSELF AS THE ONE VARIABLE, on an action-only kind.
    #
    # This is the arm a peer's port needed and did not have
    # (openstack-nova-operator-kog, 2026-11-12). Their check-D fixture is itself
    # action-only -- create + findby, no get, no update -- so their arm 1 asserts
    # D IS actionable on the shape where that verdict is backwards, and their arm
    # 2 asserts that adding a responseTransform is the REMEDY. That is the change
    # that left eight blades at `enroll` for a day: the test would have defended
    # the defect against a correct patch, and it reads as coverage.
    #
    # This repo escaped that only because rd()'s default verb set happens to
    # include a `get`, which makes its D fixtures non-action-only. Luck, not
    # design -- so assert the pair explicitly, on the thin OAS where the
    # identifier is genuinely absent from the item:
    #     no transform -> findby cannot match -> create reachable  -> no F
    #        transform -> findby CAN match    -> create unreachable -> F
    _ao = [_v[0], _v[1]]                                    # findby + create only
    _no_xf = rd(verbsDescription=_ao)
    _xf = rd(verbsDescription=[dict(_v[0], responseTransform={"inline": ".name = .uuid | .name"}),
                               _v[1]])
    for _n, _doc, _wantF in (("action-only, NO transform -> create reachable, no F", _no_xf, 0),
                             ("action-only, WITH a transform -> create UNREACHABLE, F", _xf, 1)):
        _r = [r for r in evaluate(_doc, thin, "thing") if r[0] == "F"]
        _o = len(_r) == _wantF and all(x[3] for x in _r)
        cases.append(("the transform is the variable: %s" % _n, _o))
        if not _o:
            failures.append("%s: expected %d F row(s), got %s" % (_n, _wantF, [(x[1], x[3]) for x in _r]))

    # THE REGRESSION, ON THE REAL DOCUMENTS, BOTH DIRECTIONS.
    #
    # 52ebe5d (2026-11-11) added a findby responseTransform to the provision kind
    # to satisfy check D and oasgen's identically-worded schemaWarning. Because
    # that kind has no `get` and no `update`, a matching findby made Observe
    # report exists-and-up-to-date and the PUT was never issued: every provision
    # transition became a silent no-op and nodes stayed at `enroll`. The same
    # transform had been reverted four days earlier in 40bf2ed, whose subject
    # reads "the responseTransform I added today would have silently stopped
    # every transition" -- and the comment recording that revert was still in the
    # file, 59 lines BELOW the re-added transform, inside the same verb block.
    #
    # So: inject 52ebe5d's exact transform into the SHIPPED document and F must
    # fire; the shipped document itself must be clean; and power -- which carries
    # the equivalent transform but also a `get` and an `update` -- must be clean
    # too. The last one is what stops this check being "no transforms allowed".
    try:
        import copy as _c, yaml as _y3
        _prd = _y3.safe_load(open("manifests/restdefinition-provision.yaml"))
        _poas = _y3.safe_load(open("oas/ironic-provision.yaml"))
        _wrd = _y3.safe_load(open("manifests/restdefinition-power.yaml"))
        _woas = _y3.safe_load(open("oas/ironic-power.yaml"))
    except Exception as _e:
        cases.append(("F on the real provision/power documents", False))
        failures.append("could not load the provision/power documents (%s) -- VOID, not a pass" % _e)
    else:
        _hist = _c.deepcopy(_prd)
        for _vv in _hist["spec"]["resource"]["verbsDescription"]:
            if _vv.get("action") == "findby":
                _vv["responseTransform"] = {
                    "inline": "if (.name and .provision_state) then .node_id = .name else . end"}
        _hf = [r for r in evaluate(_hist, _poas, "provision") if r[0] == "F"]
        _sf = [r for r in evaluate(_prd, _poas, "provision") if r[0] == "F"]
        _wf = [r for r in evaluate(_wrd, _woas, "power") if r[0] == "F"]
        for _n, _rows, _want in (("52ebe5d's transform re-injected -> F fires", _hf, 1),
                                 ("the shipped provision document -> no F",     _sf, 0),
                                 ("power (has get AND update) -> no F",          _wf, 0)):
            _o = len(_rows) == _want and all(r[3] for r in _rows)
            cases.append(("F regression, real documents: %s" % _n, _o))
            if not _o:
                failures.append("%s: expected %d F row(s), got %s"
                                % (_n, _want, [(r[1], r[3]) for r in _rows]))

    # CROSS-ASSERTIONS: each pair of branches must be DISTINGUISHABLE by message.
    # This is what kills the mutants; the per-case checks alone do not.
    for a, b in (("A inert path", "A inert method"),
                 ("E unmapped path param is ACTIONABLE", "E mapped param suppressed")):
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

    # REGRESSION ARM for the live defect this check was added for. The real Port
    # document with `get` UNMAPPED is the shape that shipped, and the apiserver
    # rejected every new Port under it ("spec.uuid: Required value", with the same CR
    # carrying spec.uuid accepted as the control). It must be ACTIONABLE here, and
    # the fixed shape -- `get` mapped from status.uuid, as `delete` already was --
    # must NOT be. Both arms over the real document, so neither can pass by fixture.
    try:
        import yaml as _y
        _prd = _y.safe_load(open("manifests/restdefinition-port.yaml"))
        _poas = _y.safe_load(open("oas/ironic-port.yaml"))
    except Exception as _e:
        cases.append(("Port regression arm", False))
        failures.append("could not load the port documents (%s) -- VOID, not a pass" % _e)
    else:
        import copy as _c
        # the fixed shape, as shipped
        _fixed = [r for r in evaluate(_prd, _poas, "port", (0, 59, 0))
                  if r[0] == "E" and r[1] == "uuid" and r[3]]
        cases.append(("Port as shipped (get mapped): uuid NOT actionable", not _fixed))
        if _fixed:
            failures.append("the shipped Port shape produced an actionable uuid row: %s" % _fixed)
        # the pre-fix shape: strip the get mapping back off
        _broken = _c.deepcopy(_prd)
        for _v in _broken["spec"]["resource"]["verbsDescription"]:
            if _v.get("action") == "get":
                _v.pop("requestFieldMapping", None)
        _rows = [r for r in evaluate(_broken, _poas, "port", (0, 59, 0))
                 if r[0] == "E" and r[1] == "uuid" and r[3]]
        cases.append(("Port pre-fix (get unmapped): uuid IS actionable", bool(_rows)))
        if not _rows:
            failures.append("the PRE-FIX Port shape was NOT flagged -- this check cannot "
                            "catch the defect it was written for")

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
