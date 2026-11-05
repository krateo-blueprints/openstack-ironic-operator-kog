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

    # Arm: the child-apiVersion fallback check must fire on a stale fallback, and
    # must also fire when the define it reads has been reshaped away -- a check
    # that silently verifies nothing is the failure mode this suite exists for.
    with tempfile.TemporaryDirectory() as td:
        hp = os.path.join(td, "charts/kubernetes-cluster/templates")
        os.makedirs(hp)
        stale = ('{{- define "kubernetes-cluster.lifecycleApiVersion" -}}\n'
                 '{{- default "composition.krateo.io/v0-5-6" .Values.lifecycleApiVersion -}}\n'
                 '{{- end -}}\n')
        with open(os.path.join(hp, "_helpers.tpl"), "w") as fh:
            fh.write(stale)
        cases += 1
        if not check_lifecycle_fallback({"baremetal-host": "composition.krateo.io/v0-6-2"}, td):
            failures.append("a fallback of v0-5-6 against a child shipping v0-6-2 "
                            "did not trip check_lifecycle_fallback")
        cases += 1
        if check_lifecycle_fallback({"baremetal-host": "composition.krateo.io/v0-5-6"}, td):
            failures.append("a fallback MATCHING the child version tripped "
                            "check_lifecycle_fallback -- it fires on agreement")
        with open(os.path.join(hp, "_helpers.tpl"), "w") as fh:
            fh.write('{{- define "something.else" -}}x{{- end -}}\n')
        cases += 1
        if not check_lifecycle_fallback({"baremetal-host": "composition.krateo.io/v0-6-2"}, td):
            failures.append("the define being gone did not trip "
                            "check_lifecycle_fallback -- it would verify nothing silently")

    # Arm: the configdrive-hash interlock guard must fire when the key is added
    # (the obvious "fix" that arms a fleet reimage), stay quiet when it is absent,
    # and fire when EXTRA_KEYS has been reshaped so it can no longer check.
    with tempfile.TemporaryDirectory() as td:
        sp = os.path.join(td, "scripts")
        os.makedirs(sp)
        bp = os.path.join(sp, "ironic-node-extra-bridge.py")
        safe = 'EXTRA_KEYS = ("kubeadm_join", "cert_key", "drain_kubeconfig")\n'
        armed = ('EXTRA_KEYS = ("kubeadm_join", "cert_key",\n'
                 '              "deployed_configdrive_hash")\n')
        with open(bp, "w") as fh:
            fh.write(safe)
        cases += 1
        if check_configdrive_hash_interlock(td):
            failures.append("the interlock guard fired on a bridge that correctly "
                            "EXCLUDES deployed_configdrive_hash")
        with open(bp, "w") as fh:
            fh.write(armed)
        cases += 1
        if not check_configdrive_hash_interlock(td):
            failures.append("deployed_configdrive_hash present in EXTRA_KEYS did "
                            "NOT trip the interlock guard -- a fleet reimage would "
                            "be armed with nothing objecting")
        with open(bp, "w") as fh:
            fh.write("EXTRA_KEY_SET = {'kubeadm_join'}\n")
        cases += 1
        if not check_configdrive_hash_interlock(td):
            failures.append("EXTRA_KEYS being reshaped away did not trip the "
                            "interlock guard -- it would verify nothing silently")

    # Arm: the isolation check must catch each of the three real shapes found, and
    # must NOT fire on a bare `helm package`, which never contacts a cluster.
    with tempfile.TemporaryDirectory() as td:
        cases_mk = [
            ("\tkubectl apply -f x.yaml\n", True, "bare kubectl apply"),
            ("\thelm upgrade --install foo ./chart\n", True, "bare helm upgrade"),
            ("\t./scripts/create-ironic-oas-configmap.sh ns\n", True,
             "OAS script without KUBECTL="),
            ("\thelm package charts/foo -d dist/\n", False, "bare helm package"),
            ("\t$(KUBECTL) apply -f x.yaml\n", False, "$(KUBECTL) apply"),
            ("\t# kubectl apply -f x.yaml\n", False, "a commented-out kubectl"),
        ]
        for recipe, should_fire, label in cases_mk:
            with open(os.path.join(td, "Makefile"), "w") as fh:
                fh.write("target:\n" + recipe)
            cases += 1
            fired = bool(check_kubeconfig_isolation(td))
            if fired != should_fire:
                failures.append(f"isolation check {'missed' if should_fire else 'falsely fired on'} {label}")

    print(f"  self-test: {cases} cases")
    for f in failures:
        print(f"    FAIL {f}")
    if failures:
        print("  SELF-TEST FAILED -- the real result below would be meaningless")
        return False
    print("  self-test: OK (the check demonstrably still fails on bad input)")
    return True


def lifecycle_fallback(root=None):
    """The literal apiVersion `kubernetes-cluster.lifecycleApiVersion` falls back to.

    Returns (fallback_string, source_path) or (None, path) if the define is gone.
    """
    root = root or REPO
    path = os.path.join(root, "charts/kubernetes-cluster/templates/_helpers.tpl")
    with open(path) as fh:
        body = fh.read()
    m = re.search(
        r'define\s+"kubernetes-cluster\.lifecycleApiVersion".*?'
        r'default\s+"([^"]+)"\s+\.Values\.lifecycleApiVersion',
        body, re.S)
    return (m.group(1) if m else None), path


def check_lifecycle_fallback(charts, root=None):
    """The parent's child-apiVersion fallback must name the child it ships against.

    `lifecycleApiVersion` is OPTIONAL in the generated KubernetesCluster CRD and
    has no schema default, so the chart's fallback is REACHABLE: a cluster CR that
    omits the field renders BaremetalHosts at whatever this string says. It was
    stale at v0-5-6 while the served child version was v0-6-2, which would have
    wedged any fresh cluster CR -- invisibly, because the one live CR sets the
    field explicitly and never took the fallback path.

    Nothing else catches this. `helm lint` sees a valid string; `helm template`
    renders no BaremetalHost at all offline (they gate on a live lookup), so even
    a full render is silent on it.
    """
    want = charts.get("baremetal-host")
    got, path = lifecycle_fallback(root)
    rel = os.path.relpath(path, root or REPO)
    if got is None:
        return [f"{rel}: the lifecycleApiVersion define is gone or reshaped -- "
                f"this check now verifies nothing and must be updated, not deleted"]
    if want is None:
        return [f"charts/baremetal-host/Chart.yaml: no version found, so {rel}'s "
                f"fallback {got} cannot be checked against anything"]
    if got != want:
        return [f"{rel}: lifecycleApiVersion falls back to {got}, but "
                f"charts/baremetal-host ships {want}. A KubernetesCluster CR that "
                f"omits the field would render BaremetalHosts at an apiVersion the "
                f"apiserver does not serve. Bump the fallback with the child chart."]
    return []


def check_configdrive_hash_interlock(root=None):
    """`deployed_configdrive_hash` must stay OUT of the bridge's EXTRA_KEYS.

    baremetal-host auto-fires transition-undeploy -- Ironic wipes the blade --
    when the observed configdrive hash is non-empty and differs from the rendered
    one. The rendered one is the PARENT CHART VERSION, not a hash of the
    configdrive. So the moment this key is mirrored into the CR, every chart bump
    looks like drift and reimages all six blades.

    The blade does publish the hash to Ironic. The bridge declining to carry it is
    the whole interlock, and a comment was once its only protection: adding one
    tuple entry -- the obvious way to "fix" drift detection not working -- arms a
    fleet reimage with nothing objecting.

    v0.13.26 made the rendered value a real content hash, which removes the
    ORIGINAL reason for the exclusion: a chart bump no longer looks like drift.
    It does NOT make arming safe, and this is the part worth reading before
    deleting this guard. What each blade has already published to Ironic is a
    chart-version STRING from whenever it was last deployed. Measured 2026-11-05:

        blade01  None       (bootstrap CP never published)
        blade02  "0.13.19"      blade04  "0.13.18"
        blade03  "0.13.19"      blade05  "0.13.18"
                                blade06  "0.13.18"

    None of those can equal a content hash. So mirroring the key today sets
    observed to a stale version string and fires auto-undeploy on the five blades
    whose value is non-empty -- including two of three control planes, which takes
    etcd quorum with it. blade01 is spared only because v0.4.3 suppresses on
    empty-observed.

    The precondition for arming is therefore not "the hash is a content hash" but
    "every blade has republished under a chart that stamps one", which happens on
    each blade's next deploy. Verify by reading Ironic's node.extra and seeing
    content hashes rather than version strings -- then add the key and delete this
    guard in the same commit.
    """
    root = root or REPO
    path = os.path.join(root, "scripts/ironic-node-extra-bridge.py")
    rel = os.path.relpath(path, root)
    with open(path) as fh:
        body = fh.read()
    m = re.search(r"^EXTRA_KEYS\s*=\s*\((.*?)\)", body, re.S | re.M)
    if not m:
        return [f"{rel}: EXTRA_KEYS not found or reshaped -- this guard now "
                f"verifies nothing and must be updated, not deleted"]
    keys = set(re.findall(r'"([^"]+)"', m.group(1)))
    if not keys:
        return [f"{rel}: EXTRA_KEYS parsed as empty -- the guard cannot confirm "
                f"the exclusion, so treat it as unverified"]
    if "deployed_configdrive_hash" in keys:
        return [f"{rel}: deployed_configdrive_hash is in EXTRA_KEYS. Mirroring it "
                f"arms baremetal-host's auto-undeploy on every chart bump, because "
                f"the rendered hash is the parent CHART VERSION, not a content "
                f"hash. As of v0.13.26 the RENDERED value is a content hash, but "
                f"what the blades have PUBLISHED is still a stale chart-version "
                f"string, so mirroring now fires auto-undeploy on every blade whose "
                f"published value is non-empty -- measured 2026-11-05 as five of "
                f"six, including two of three control planes. Arm only once every "
                f"blade has republished under v0.13.26+; see this function's "
                f"docstring for how to check."]
    return []


# Helm subcommands that never contact a cluster, so a bare `helm` is fine.
_HELM_LOCAL = ("package", "template", "lint", "repo", "show", "dependency", "create")


def check_kubeconfig_isolation(root=None):
    """No Makefile recipe may reach a cluster through bare `kubectl` or `helm`.

    Every cluster call must go through $(KUBECTL) / $(HELMK), which carry
    --kubeconfig local/kubeconfig.<cluster> and an explicit --context. A bare
    command silently targets whatever ~/.kube/config points at -- so the failure
    is not an error, it is a correct-looking apply against the wrong cluster, and
    nothing reports it.

    Found 2026-11-05 in four recipes, two of them `helm upgrade --install`
    (baremetal-lifecycle and ironic itself), plus apply-oas invoking a script that
    defaults KUBECTL to bare `kubectl` when the Makefile does not pass it. That
    last one is the reason this checks for the script invocation too and not just
    for the two binaries: a recipe can leak isolation without naming kubectl.
    """
    root = root or REPO
    path = os.path.join(root, "Makefile")
    rel = os.path.relpath(path, root)
    problems = []
    with open(path) as fh:
        for n, line in enumerate(fh, 1):
            if not line.startswith("\t"):
                continue
            body = line[1:].lstrip("-@").strip()
            if body.startswith("#") or not body:
                continue
            first = body.split()[0] if body.split() else ""
            if first == "kubectl":
                problems.append(f"{rel}:{n}: bare `kubectl` -- use $(KUBECTL), which "
                                f"pins --kubeconfig and --context: {body[:60]}")
            elif first == "helm":
                sub = body.split()[1] if len(body.split()) > 1 else ""
                if sub not in _HELM_LOCAL:
                    problems.append(f"{rel}:{n}: bare `helm {sub}` reaches a cluster "
                                    f"-- use $(HELMK): {body[:60]}")
            elif "create-ironic-oas-configmap.sh" in body and "KUBECTL=" not in body:
                problems.append(f"{rel}:{n}: the OAS script defaults KUBECTL to bare "
                                f"`kubectl`; pass KUBECTL=\"$(KUBECTL)\": {body[:60]}")
    return problems


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
        charts = chart_versions()
        problems, seen = check(files, charts, config_kinds(), REPO, rmap)
    problems += check_lifecycle_fallback(charts)
    problems += check_configdrive_hash_interlock()
    problems += check_kubeconfig_isolation()
    print(f"  checked {seen} resources across {len(files)} files "
          f"({len(snippets)} of them ```yaml fences in docs/) "
          f"+ the parent's child-apiVersion fallback "
          f"+ the configdrive-hash interlock "
          f"+ Makefile kubeconfig isolation")
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
