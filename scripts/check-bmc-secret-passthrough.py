#!/usr/bin/env python3
"""Assert the BMC-secret pointer survives the whole parent -> child path.

WHY A SCREEN AND NOT A RENDER. The obvious verification is `helm template` on
kubernetes-cluster with bmcSecret set on a node, reading the BaremetalHost specs
it produces. That cannot work: every BaremetalHost render is gated on
`kubernetes-cluster.kubeadmPkiReady`, which is a `lookup` of a live Secret, and
`lookup` returns nothing under `helm template`. So the parent emits its PKI
Secret and NOTHING ELSE offline -- measured 2026-11-14, one resource rendered
from a three-node values file. There is no flag that fakes a lookup.

That leaves two honest options: a cluster, or an invariant checked on the source.
This is the second. It does NOT prove Ironic receives the password -- only a
cluster run does that, and the chart-path arm has not been run. What it proves is
the thing that would silently rot: that a node variable which passes driver_info
through ALSO passes bmcSecret through, at every site.

THE FAILURE IT EXISTS FOR. kubernetes-cluster renders a child spec at six sites
across four templates, each with its own node variable ($bootstrap, $f, $cp, $w
x3). Adding a seventh site, or a new node shape, means adding both blocks. Miss
the bmcSecret one and the node silently falls back to whatever driver_info says
-- which, for a node migrated OFF plaintext, is no password at all. The symptom
is an Ironic 400 at registration time for one node shape only, and nothing in the
chart or the CR says why.
"""
import pathlib
import re
import sys
from collections import Counter

PARENT = pathlib.Path("charts/kubernetes-cluster/templates")
CHILD = pathlib.Path("charts/baremetal-host")

DRIVER_INFO = re.compile(
    r"  \{\{- with (\$[A-Za-z_][A-Za-z0-9_]*)\.driver_info \}\}\n"
    r"  driver_info:\n"
)
BMC = re.compile(r"  \{\{- with (\$[A-Za-z_][A-Za-z0-9_]*)\.bmcSecret \}\}\n  bmcSecret:\n")


def audit(read):
    """read(path) -> text. Returns (findings, site_count)."""
    findings = []
    sites = 0
    for name in ("lifecycle-cp", "lifecycle-cp-recovery", "lifecycle-cp-replicas", "lifecycle-workers"):
        p = PARENT / f"{name}.yaml"
        text = read(p)
        if text is None:
            findings.append(f"{p}: missing")
            continue
        di = Counter(DRIVER_INFO.findall(text))
        bs = Counter(BMC.findall(text))
        sites += sum(di.values())
        # COUNTS, NOT NAMES. lifecycle-workers.yaml passes $w through at THREE
        # sites; a set-membership test calls two-of-three a pass, and the first
        # positive control for this screen proved exactly that on 2026-11-14.
        for var in set(di) | set(bs):
            if di[var] != bs[var]:
                findings.append(
                    f"{p}: {var} has {di[var]} driver_info site(s) but {bs[var]} bmcSecret "
                    f"site(s) -- a node of this shape silently loses its Secret pointer"
                )

    # The child must actually consume it, or the pass-through lands nowhere.
    node = read(CHILD / "templates" / "node.yaml")
    helpers = read(CHILD / "templates" / "_helpers.tpl")
    if node is None or helpers is None:
        findings.append(f"{CHILD}: node.yaml or _helpers.tpl missing")
    else:
        if 'include "baremetal-host.nodeExtra"' not in node:
            findings.append(f"{CHILD}/templates/node.yaml: does not render spec.extra via nodeExtra")
        if 'define "baremetal-host.nodeExtra"' not in helpers:
            findings.append(f"{CHILD}/templates/_helpers.tpl: nodeExtra is not defined")
        if "bmc_secret" not in helpers:
            findings.append(f"{CHILD}/templates/_helpers.tpl: nodeExtra does not set bmc_secret")
        # The guard is the only thing that stops a plaintext password sitting in the
        # CR doing nothing. A nodeExtra without it is worse than no feature.
        if "{{- fail " not in helpers:
            findings.append(
                f"{CHILD}/templates/_helpers.tpl: nodeExtra has no `fail` guard -- "
                f"declaring bmcSecret AND driver_info.redfish_password would render silently"
            )

    # The pointer path must match what the RestDefinition's resolver reads. If either
    # side is renamed the chart keeps rendering and the resolver stops resolving.
    rd = read(pathlib.Path("manifests/restdefinition-node.yaml"))
    if rd is None:
        findings.append("manifests/restdefinition-node.yaml: missing")
    else:
        for path in ("spec.extra.bmc_secret.name", "spec.extra.bmc_secret.key"):
            if path not in rd:
                findings.append(
                    f"manifests/restdefinition-node.yaml: resolver does not reference {path}, "
                    f"but the chart renders it -- the pointer leads nowhere"
                )
    return findings, sites


def self_test():
    real = {}

    def read_real(p):
        if p not in real:
            real[p] = p.read_text() if p.exists() else None
        return real[p]

    cases = 0
    failures = 0

    def case(label, reader, want_findings):
        nonlocal cases, failures
        cases += 1
        got, _ = audit(reader)
        ok = (len(got) > 0) == want_findings
        print(f"  {label:<62} {'ok' if ok else 'FAILED'}")
        if not ok:
            failures += 1
            print(f"      findings: {got}")

    # NEGATIVE CONTROL: the tree as committed must be clean.
    case("the repo as it stands -> no findings", read_real, False)

    # POSITIVE CONTROLS. Each must FAIL, and for its own reason.
    def drop_one_bmc(p):
        t = read_real(p)
        if t and p.name == "lifecycle-workers.yaml":
            return BMC.sub("", t, count=1)
        return t

    case("a parent site loses its bmcSecret block -> FINDING", drop_one_bmc, True)

    # A MISSING parent template must be a FINDING, not a silent skip. Without
    # this arm, deleting the `findings.append(f"{p}: missing")` line survives the
    # self-test: `continue` then skips that file's audit and the screen passes
    # having inspected one site fewer. Mutation-tested 2026-11-17 -- the mutant
    # survived until this arm existed, which is how the gap was found.
    def hide_one_parent(p):
        if p.name == "lifecycle-cp-replicas.yaml":
            return None
        return read_real(p)

    case("a parent template is MISSING -> FINDING, not a silent skip",
         hide_one_parent, True)

    def drop_guard(p):
        t = read_real(p)
        if t and p.name == "_helpers.tpl" and "baremetal-host" in str(p):
            return t.replace("{{- fail ", "{{- /* no guard */ -}}{{- print ")
        return t

    case("the child's fail guard is removed -> FINDING", drop_guard, True)

    def rename_pointer(p):
        t = read_real(p)
        if t and p.name == "restdefinition-node.yaml":
            return t.replace("spec.extra.bmc_secret.name", "spec.extra.bmc_pointer.name")
        return t

    case("the resolver's pointer path is renamed -> FINDING", rename_pointer, True)

    def drop_child_include(p):
        t = read_real(p)
        if t and p.name == "node.yaml" and "baremetal-host" in str(p):
            return t.replace('include "baremetal-host.nodeExtra"', 'include "baremetal-host.nothing"')
        return t

    case("the child stops rendering spec.extra -> FINDING", drop_child_include, True)

    print(f"selftest: {cases} cases, {failures} failure(s)")
    return failures


def main():
    if "--self-test" in sys.argv:
        failures = self_test()
        if failures:
            return 1
        print("  self-test: OK (the check demonstrably still fails on bad input)")
        return 0

    findings, sites = audit(lambda p: p.read_text() if p.exists() else None)
    print(f"  checked {sites} parent pass-through site(s) + the child's consumer + the resolver's path")
    if findings:
        print(f"  {len(findings)} problem(s):")
        for f in findings:
            print(f"    - {f}")
        return 1
    if sites == 0:
        print("  VOID -- found 0 driver_info pass-through sites, so nothing was checked")
        return 1
    print("PASS -- every node shape that passes driver_info also passes bmcSecret.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
