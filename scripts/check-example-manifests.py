#!/usr/bin/env python3
"""Check that every manifest a reader is meant to APPLY would actually apply.

Why this exists. Shipped example manifests are the artifacts least likely to be
exercised and most likely to be copied verbatim, and the two failure modes they
carry are both invisible to `helm template`, `helm lint` and `kubeconform` --
they are rejected by the API server and nowhere earlier. Audited 2026-11-04
after a sibling KOG provider reported four defects out of four in hand-written
artifact sets that had never been applied; this repo had two of the same kinds:

  * `manifests/nodeconfiguration-example.yaml` declared kind
    `NodeConfiguration`. No RestDefinition declares a `Node` kind, so that kind
    has never existed -- the real one is `BaremetalNodeConfiguration`.
  * Four example manifests declared composition apiVersions from long-pruned
    chart versions (`v0-1-0` against a chart at 0.13.x).

Both would be caught instantly by applying them and never by reading them,
which is exactly the class a check earns its keep on.

Scope is deliberately narrow: files a reader is told to apply. Historical test
fixtures under manifests/ (blade02-e2e and friends) are records of past runs,
pinned to the version they ran against on purpose, and are skipped.

--self-test proves this script can still FAIL, by running the real check
against manifests built to be broken. It ships here, inside the thing it
calibrates, for the reason a peer's own repo puts best: a check that lives in
a previous commit message is a check nobody runs. The first calibration of
this script was a shell one-liner that injected a bad version and then reverted
it -- which proved the checker worked that afternoon and nothing afterwards.
`make check-examples` runs the self-test first; if it fails, the real result is
void rather than passing.

Exit 1 on any finding, 2 if the self-test fails. Run: make check-examples
"""
import argparse
import glob
import os
import re
import sys
import tempfile

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# composition Kind -> the chart that generates it
KIND_TO_CHART = {
    "BaremetalHost": "baremetal-host",
    "BaremetalLifecycle": "baremetal-lifecycle",
    "BaremetalDiscovery": "baremetal-discovery",
    "KubernetesCluster": "kubernetes-cluster",
}


def chart_versions():
    """chart dir name -> the composition apiVersion a fresh install would serve"""
    out = {}
    for c in glob.glob(os.path.join(REPO, "charts/*/Chart.yaml")):
        name = os.path.basename(os.path.dirname(c))
        with open(c) as fh:
            v = yaml.safe_load(fh)["version"]
        out[name] = "composition.krateo.io/v" + v.replace(".", "-")
    return out


def resource_kinds():
    """kind -> the apiVersion its CRD serves, from each RestDefinition and its OAS.

    The resource version comes from the OAS `info.version` (1.0.0 -> v1-0-0);
    Configuration kinds are always v1alpha1. Those being DIFFERENT is the trap
    the old quickstart fell into -- it used the Configuration version on a
    resource.
    """
    out = {}
    for f in glob.glob(os.path.join(REPO, "manifests/restdefinition-*.yaml")):
        with open(f) as fh:
            d = yaml.safe_load(fh) or {}
        spec = d.get("spec") or {}
        kind = (spec.get("resource") or {}).get("kind")
        if not kind:
            continue
        stem = os.path.basename(f)[len("restdefinition-"):-len(".yaml")]
        oas = os.path.join(REPO, f"oas/ironic-{stem}.yaml")
        ver = "v1-0-0"
        if os.path.exists(oas):
            with open(oas) as fh:
                iv = ((yaml.safe_load(fh) or {}).get("info") or {}).get("version")
            if iv:
                ver = "v" + str(iv).replace(".", "-")
        out[kind] = f"baremetal.ogen.krateo.io/{ver}"
    return out


def config_kinds():
    """every <Kind>Configuration the RestDefinitions make real"""
    kinds = set()
    for f in glob.glob(os.path.join(REPO, "manifests/restdefinition-*.yaml")):
        with open(f) as fh:
            d = yaml.safe_load(fh) or {}
        k = ((d.get("spec") or {}).get("resource") or {}).get("kind")
        if k:
            kinds.add(k)
    return {k + "Configuration" for k in kinds}


def check(files, charts, valid_config_kinds, root, resource_kinds_map=None):
    """the whole rule, over an arbitrary file list. Returns (problems, resources_seen)."""
    resource_kinds_map = resource_kinds_map or {}
    problems, seen = [], 0
    for f in files:
        base = os.path.basename(f)
        if base.startswith("docs__"):
            md, fence = base[len("docs__"):-len(".yaml")].split("__fence")
            rel = f"docs/{md} (```yaml fence {fence})"
        else:
            rel = os.path.relpath(f, root)
        with open(f) as fh:
            try:
                docs = list(yaml.safe_load_all(fh))
            except yaml.YAMLError as e:
                problems.append(f"{rel}: not parseable as YAML -- {e}")
                continue
        for d in docs:
            if not isinstance(d, dict):
                continue
            kind, av = d.get("kind", ""), d.get("apiVersion", "")
            if not kind:
                continue
            seen += 1
            if kind.endswith("Configuration") and kind not in valid_config_kinds:
                problems.append(
                    f"{rel}: kind {kind!r} does not exist. "
                    f"Valid: {', '.join(sorted(valid_config_kinds))}"
                )
            if kind in resource_kinds_map:
                want = resource_kinds_map[kind]
                if av != want:
                    problems.append(
                        f"{rel}: {kind} declares {av}, but that kind is served at {want}"
                    )
            elif av.startswith("baremetal.ogen.krateo.io/") and not kind.endswith("Configuration"):
                problems.append(
                    f"{rel}: kind {kind!r} is not generated by any RestDefinition. "
                    f"Generated kinds: {', '.join(sorted(resource_kinds_map))}"
                )

            # A RestDefinition declaring MORE THAN ONE identifier without an
            # explicit AND policy gets OR by default, and OR returns on the
            # FIRST identifier that matches -- so a non-unique identifier can
            # bind the CR to an arbitrary other object. Measured 2026-11-04 on
            # an isolated fixture: a CR naming `child-b` adopted `enclosure-a`,
            # reported Synced=True, and DELETING the CR destroyed enclosure-a
            # while child-b was never touched. See tests/identifier-arms/.
            #
            # Every production RestDefinition here declares exactly ONE
            # identifier, so this repo is not exposed today. This rule exists so
            # that adding a second one cannot introduce the hazard silently.
            if kind == "RestDefinition":
                res = (d.get("spec") or {}).get("resource") or {}
                ids = res.get("identifiers") or []
                if len(ids) > 1:
                    for v in res.get("verbsDescription") or []:
                        if v.get("action") != "findby":
                            continue
                        pol = str(v.get("identifiersMatchPolicy", "")).strip().upper()
                        if pol != "AND":
                            problems.append(
                                f"{rel}: {res.get('kind')} declares {len(ids)} identifiers "
                                f"{ids} with findby policy "
                                f"{v.get('identifiersMatchPolicy', '(omitted -> OR)')!r}. "
                                f"OR matches on the FIRST identifier that matches, so a "
                                f"non-unique one can bind -- and delete -- an unrelated "
                                f"object. Set identifiersMatchPolicy: AND, or reduce to one "
                                f"identifier."
                            )

            if kind in KIND_TO_CHART and av.startswith("composition.krateo.io/"):
                want = charts.get(KIND_TO_CHART[kind])
                if want and av != want:
                    problems.append(
                        f"{rel}: {kind} declares {av}, but "
                        f"charts/{KIND_TO_CHART[kind]} is at {want}"
                    )
    return problems, seen


FENCE = re.compile(r"```ya?ml\n(.*?)```", re.S)


def doc_snippets(tmpdir):
    """Write each ```yaml fence in docs/ to a file so the same rules screen it.

    Embedded YAML is the one place readers copy from that nothing checked. The
    quickstart shipped `kind: Node` at an unserved apiVersion for months because
    it lives in a fence and no screen reads fences.
    """
    out = []
    for md in sorted(glob.glob(os.path.join(REPO, "docs/*.md"))):
        with open(md) as fh:
            body = fh.read()
        for i, block in enumerate(FENCE.findall(body)):
            try:
                docs = list(yaml.safe_load_all(block))
            except yaml.YAMLError:
                continue  # prose or templated YAML; not ours to judge
            if not any(isinstance(d, dict) and d.get("kind") for d in docs):
                continue
            name = f"docs__{os.path.basename(md)}__fence{i + 1}.yaml"
            path = os.path.join(tmpdir, name)
            with open(path, "w") as fh:
                fh.write(block)
            out.append(path)
    return out


def targets():
    """manifests a reader is meant to apply, plus every production RestDefinition"""
    out = sorted(glob.glob(os.path.join(REPO, "examples/**/*.yaml"), recursive=True))
    out += sorted(
        f for f in glob.glob(os.path.join(REPO, "manifests/*.yaml"))
        if "example" in os.path.basename(f) or "quickstart" in os.path.basename(f)
    )
    # The RestDefinitions themselves, for the multi-identifier rule. They are
    # not "manifests a reader applies" but they are where the identifier hazard
    # would be introduced, and a rule over a file list that excludes its own
    # subject screens nothing -- which is what happened when this rule was
    # first added.
    #
    # tests/ is deliberately NOT included: tests/identifier-arms/ contains an
    # intentionally-exposed ArmOr fixture whose whole purpose is to exhibit the
    # hazard, and screening it would make the check fail by design. That
    # exclusion is asserted in self_test() rather than assumed, so moving or
    # deleting the fixture cannot quietly empty it.
    out += sorted(glob.glob(os.path.join(REPO, "manifests/restdefinition-*.yaml")))
    return out


def self_test():
    """Prove the check can still fail, by running it against built-to-be-broken input.

    The defect goes into the SUBJECT -- real files fed to the real check() -- not
    into a reconstruction of the logic. A peer had three screens defeated by their
    own documentation, each one correct and matching the wrong thing; the lesson is
    that proving a checker can return non-zero is not the same as proving it
    returns non-zero FOR THIS.
    """
    charts = {"kubernetes-cluster": "composition.krateo.io/v9-9-9"}
    valid = {"BaremetalNodeConfiguration"}
    rmap = {"BaremetalNode": "baremetal.ogen.krateo.io/v1-0-0"}
    cases, failures = 0, []

    with tempfile.TemporaryDirectory() as td:
        def write(name, body):
            p = os.path.join(td, name)
            with open(p, "w") as fh:
                fh.write(body)
            return p

        bad_version = write("bad-version.yaml",
            "apiVersion: composition.krateo.io/v0-1-0\nkind: KubernetesCluster\n"
            "metadata:\n  name: x\n")
        bad_kind = write("bad-kind.yaml",
            "apiVersion: baremetal.ogen.krateo.io/v1alpha1\nkind: NodeConfiguration\n"
            "metadata:\n  name: x\n")
        good = write("good.yaml",
            "apiVersion: composition.krateo.io/v9-9-9\nkind: KubernetesCluster\n"
            "metadata:\n  name: x\n")
        broken_yaml = write("broken.yaml", "apiVersion: [unclosed\n")

        # OVER-EAGERNESS ARMS. Four of the original five cases tested rules I
        # was thinking about while writing them; only "a correct manifest
        # passes" guarded the other direction. Two peers independently made
        # the same point today: the risk is not in the rule you just added, it
        # is in what the addition sits in front of. A screen that starts
        # rejecting valid manifests is as bad as one that stops rejecting
        # invalid ones, and nobody writes that arm because the over-eagerness
        # would have been obvious the day the rule was written -- not two
        # months later when someone tightens it.
        other_kind = write("other-kind.yaml",
            # Outside our API group on purpose. The original fixture used
            # BaremetalNode at v1alpha1, which became ambiguous the moment the
            # resource-version rule existed: the arm then failed for a correct
            # reason and said nothing about Configuration over-eagerness.
            "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: x\n")
        unmanaged = write("unmanaged.yaml",
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n")
        no_kind = write("no-kind.yaml", "apiVersion: v1\nmetadata:\n  name: x\n")
        bad_res_version = write("bad-res-version.yaml",
            "apiVersion: baremetal.ogen.krateo.io/v1alpha1\nkind: BaremetalNode\n"
            "metadata:\n  name: x\n")
        ghost_kind = write("ghost-kind.yaml",
            "apiVersion: baremetal.ogen.krateo.io/v1-0-0\nkind: Node\n"
            "metadata:\n  name: x\n")
        good_res = write("good-res.yaml",
            "apiVersion: baremetal.ogen.krateo.io/v1-0-0\nkind: BaremetalNode\n"
            "metadata:\n  name: x\n")
        other_group = write("other-group.yaml",
            "apiVersion: composition.krateo.io/v0-0-1\nkind: SomethingElse\n"
            "metadata:\n  name: x\n")

        # MULTI-IDENTIFIER ARMS (oasgen-provider#444). OR returns on the FIRST
        # identifier that matches, so a non-unique one can bind the CR to an
        # arbitrary other object -- and deleting the CR then DELETES that
        # object. Measured 2026-11-04: a CR naming `child-b` destroyed
        # `enclosure-a`. These arms are what make the rule's pass over this
        # repo's seven single-identifier RestDefinitions mean something.
        def restdef(name, ids, policy_line):
            return write(name,
                "apiVersion: ogen.krateo.io/v1alpha1\nkind: RestDefinition\n"
                "metadata:\n  name: x\n  namespace: openstack\n"
                "spec:\n  oasPath: configmap://x/y/z.yaml\n"
                "  resourceGroup: g.ogen.krateo.io\n  resource:\n    kind: K\n"
                "    identifiers:\n" + "".join(f"      - {i}\n" for i in ids) +
                "    verbsDescription:\n      - action: findby\n"
                "        method: GET\n        path: /v1/things\n" + policy_line)

        multi_or_omitted = restdef("rd-multi-omitted.yaml", ["a", "b"], "")
        multi_or_explicit = restdef("rd-multi-or.yaml", ["a", "b"],
                                    "        identifiersMatchPolicy: OR\n")
        multi_and = restdef("rd-multi-and.yaml", ["a", "b"],
                            "        identifiersMatchPolicy: AND\n")
        single_or = restdef("rd-single-or.yaml", ["a"],
                            "        identifiersMatchPolicy: OR\n")
        single_omitted = restdef("rd-single-omitted.yaml", ["a"], "")

        for label, files, want_hit in (
            # the rules fire on what they should
            ("a stale apiVersion is caught", [bad_version], True),
            ("a nonexistent kind is caught", [bad_kind], True),
            ("unparseable YAML is caught", [broken_yaml], True),
            ("a correct manifest passes", [good], False),
            # ...and do NOT fire on what they should leave alone
            ("a non-Configuration kind is left alone", [other_kind], False),
            ("an unmanaged kind (ConfigMap) is left alone", [unmanaged], False),
            ("a document with no kind is skipped", [no_kind], False),
            ("a composition Kind we do not own is left alone", [other_group], False),
            # the quickstart's two defect classes
            ("a resource on the Configuration version is caught", [bad_res_version], True),
            ("a kind no RestDefinition generates is caught", [ghost_kind], True),
            ("a correct generated resource passes", [good_res], False),
            # multi-identifier policy: fires on the hazard...
            ("2 identifiers with the policy OMITTED is caught", [multi_or_omitted], True),
            ("2 identifiers with an explicit OR is caught", [multi_or_explicit], True),
            # ...and leaves the safe shapes alone. Without these three the rule
            # could be tightened to "any OR policy" and nothing would notice,
            # which would flag all seven of this repo's RestDefinitions.
            ("2 identifiers with AND passes", [multi_and], False),
            ("ONE identifier with OR passes", [single_or], False),
            ("ONE identifier with the policy omitted passes", [single_omitted], False),
        ):
            cases += 1
            problems, seen = check(files, charts, valid, td, rmap)
            if bool(problems) != want_hit:
                failures.append(
                    f"{label}: expected {'a finding' if want_hit else 'no finding'}, "
                    f"got {problems or 'none'}"
                )

        # The check must also notice it examined nothing -- otherwise a glob that
        # stops matching prints OK forever.
        cases += 1
        _, seen = check([good], charts, valid, td, rmap)
        if seen != 1:
            failures.append(f"resource counting is wrong: saw {seen}, expected 1")

        # THE EXCLUSION IS ITSELF TESTED. targets() deliberately omits tests/,
        # because tests/identifier-arms/ holds an intentionally-exposed ArmOr
        # fixture. If that fixture is moved or deleted, the exclusion silently
        # protects nothing and this rule loses the only known-positive it has
        # in the tree. Borrowed from a peer whose test made them justify an
        # exclusion instead of assuming it.
        cases += 1
        fixture = os.path.join(REPO, "tests/identifier-arms/restdefinitions.yaml")
        if not os.path.exists(fixture):
            failures.append(
                "tests/identifier-arms/restdefinitions.yaml is gone -- targets() excludes "
                "tests/ for its sake, so the exclusion now covers nothing")
        else:
            probs, _ = check([fixture], charts, valid, td, rmap)
            if not probs:
                failures.append(
                    "the deliberately-exposed ArmOr fixture no longer trips the "
                    "multi-identifier rule -- either the fixture or the rule changed")

    print(f"  self-test: {cases} cases")
    for f in failures:
        print(f"    FAIL {f}")
    if failures:
        print("  SELF-TEST FAILED -- the real result below would be meaningless")
        return False
    print("  self-test: OK (the check demonstrably still fails on bad input)")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true",
                    help="prove the check can still fail, then exit")
    args = ap.parse_args()

    if args.self_test:
        return 0 if self_test() else 2

    rmap = resource_kinds()
    with tempfile.TemporaryDirectory() as td:
        snippets = doc_snippets(td)
        files = targets() + snippets
        problems, seen = check(files, chart_versions(), config_kinds(), REPO, rmap)
    print(f"  checked {seen} resources across {len(files)} files "
          f"({len(snippets)} of them ```yaml fences in docs/)")
    if problems:
        print(f"  {len(problems)} problem(s):")
        for p in problems:
            print(f"    - {p}")
        return 1
    if seen == 0:
        print("  NOTHING CHECKED -- the glob matched no resources; treat as a failure")
        return 1
    print("  OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
