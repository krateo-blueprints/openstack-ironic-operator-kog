#!/usr/bin/env python3
"""Screen the Marketplace blueprints: would they publish, and would the form be usable?

Nine checks over charts/*/ for the charts listed in BLUEPRINTS. Three reproduce
the publisher's own channel gate so a rejection is seen here rather than in a
release run; the rest cover failures the gate passes and the Marketplace then
shows -- a card that never appears, a blank icon, a form of unlabelled keys.

Run as `make check-blueprints`, or `--self-test` to prove it still fails.

WHY EACH CHECK EARNS ITS PLACE
------------------------------
1-2. `values.schema.json` and `compositiondefinition.yaml` must be in the
   PACKAGED .tgz. krateo-blueprints/charts publish-chart.yaml rejects a
   blueprint missing either, and it inspects the archive -- a file beside the
   chart does not count. We package with `helm package` and read the archive,
   for the same reason: .helmignore, or a file in the wrong directory, is
   invisible to a source-tree check.

3. `icon` must be an absolute https URL. The gate checks only the SCHEME, so a
   `github.com/.../blob/...` URL passes it and then renders an empty card: that
   URL serves an HTML page, not an image. We also reject `/blob/`, and resolve
   the URL when --online is given.

4. `krateo.io/source-repo` must be present. The Marketplace adds a card for a
   live-index chart only when the NEWEST version of its entry carries this
   annotation (portal's `catalogLiveDefs`, merged 2026-10-05). Without it the
   card waits for the next `krateo-marketplace-catalog` release, which is not
   ours to cut -- the curated ConfigMap is built from this same live index, so
   the blueprint does arrive, just not when the publish lands.

   Checked 2026-11-05: no entry in the live index carries it yet, this product's
   own `baremetal-lifecycle` 0.3.1 included. These two blueprints would be the
   first to use that route, so a missing annotation is also the first thing to
   suspect if a card does not appear.

5. The in-chart CompositionDefinition's `spec.chart.version` must equal the
   chart version, because the chart version IS the served apiVersion (0.14.0 ->
   composition.krateo.io/v0-14-0). Two files, one number, bumped by hand.

6. Its `spec.chart.url` must name this chart, so a copied blueprint cannot
   register a sibling's chart under its own name.

7. Every property in values.schema.json needs a non-empty `title` AND
   `description`. plumbing's crdgen carries both into the CRD
   (crdgen/transpile.go, Description at :243 and Title at :348), and they are
   the label and the help text of the only interface a Marketplace user has.
   Both, not either: `title` is unverified on this cluster -- no CRD here was
   generated from a schema that had one -- so each description must also stand
   on its own.

8. No `default` on a property nested under an OPTIONAL object. This is the one
   check whose passing is not the point of the feature, so read the mechanism:
   crdgen gives such a parent `default: {}` (crdgen/nesteddefault.go), the API
   server materialises defaults on read, and the stanza is then persisted by the
   first unrelated write. It is NOT REVERSIBLE -- pinning the CRD back does not
   remove it, and the field is unowned in managedFields so `kubectl apply` will
   not prune it (measured by the aruba fleet, 2 of 35 instances).

   Measured here 2026-11-05, which is why this check exists at all: with
   `debug.connectorVerbose` defaulted, the served BaremetalHost CRD carried
   `debug: {default: {}}` and all SIX live hosts had a materialised
   `spec.debug: {connectorVerbose: false}` that no manifest ever wrote. The
   defaults for these inputs live in values.yaml, where helm applies them at
   render time and nothing is persisted. A top-level default is a different
   shape and is allowed: it pre-fills the form and it invents no stanza.

9. The root must NOT set `additionalProperties: false`. chart-inspector performs
   a real dry-run install to derive RBAC and helm injects `global`, so the
   rejection surfaces as an opaque HTTP 500 (core-provider#325).
"""
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The charts published to the Marketplace's `blueprints` channel. The other
# charts in charts/ are deliberately NOT here: baremetal-discovery and
# baremetal-lifecycle are the superseded split that baremetal-host absorbed, and
# the two *-probe charts are throwaway measurement rigs. They have no in-chart
# CompositionDefinition and would fail the gate, which is the correct outcome --
# the publisher is called for the blueprint artifact only.
BLUEPRINTS = ("kubernetes-cluster", "baremetal-host")

SOURCE_REPO = "krateo-blueprints/openstack-ironic-operator-kog"

DOC_EXEMPT_ROOT = True  # the root carries the chart title, not a property label


# --------------------------------------------------------------------- helpers
def package(chart_dir, dest):
    """helm package, returning the .tgz path. The archive is what gets gated."""
    out = subprocess.run(
        ["helm", "package", chart_dir, "--destination", dest],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        return None, f"helm package failed: {out.stderr.strip()}"
    tgz = glob.glob(os.path.join(dest, "*.tgz"))
    if len(tgz) != 1:
        return None, f"expected one .tgz, got {len(tgz)}"
    return tgz[0], None


def members(tgz):
    """Every path in the archive. Read ONCE into a list, never piped to `grep -q`.

    The gate's own comment records why: grep -q exits on its first match, tar
    dies of SIGPIPE, and under `set -o pipefail` the pipeline status becomes
    141 -- so the file being PRESENT made the test fail, and only for the
    largest charts. Reading the listing whole has no such failure mode.
    """
    out = subprocess.run(["tar", "tzf", tgz], capture_output=True, text=True)
    return out.stdout.splitlines()


def schema_paths(node, path=""):
    """Yield (path, node) for the schema and every schema descendant."""
    yield path, node
    for name, sub in (node.get("properties") or {}).items():
        child = f"{path}.{name}" if path else name
        yield from schema_paths(sub, child)
    items = node.get("items")
    if isinstance(items, dict):
        yield from schema_paths(items, path + "[]")


def defaulted_under_optional(schema):
    """Paths whose nearest object ancestor is optional and has no required of its own.

    Mirrors the predicate in crdgen/nesteddefault.go: the parent gains
    `default: {}` and the API server then materialises the whole stanza.
    """
    hits = []

    def walk(node, path, parent, parent_path, parent_required_here):
        if "default" in node and parent is not None and not parent_required_here:
            if not (parent.get("required") or []):
                hits.append((path, parent_path or "<root>"))
        for name, sub in (node.get("properties") or {}).items():
            child = f"{path}.{name}" if path else name
            walk(sub, child, node, path, name in (node.get("required") or []))
        items = node.get("items")
        if isinstance(items, dict):
            walk(items, path + "[]", node, path, True)

    for name, sub in (schema.get("properties") or {}).items():
        # A top-level property's parent is the root, which is not an optional
        # object property: a default there invents no stanza.
        for n2, s2 in (sub.get("properties") or {}).items():
            walk(s2, f"{name}.{n2}", sub, name, n2 in (sub.get("required") or []))
        it = sub.get("items")
        if isinstance(it, dict):
            walk(it, name + "[]", sub, name, True)
    return hits


def resolves_to_image(url, timeout=10):
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
            if resp.status != 200:
                return f"HTTP {resp.status}"
            if not ctype.startswith("image/"):
                return f"content-type {ctype!r} is not an image"
            return None
    except Exception as exc:  # noqa: BLE001 -- any failure is a failure to serve
        return f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------- checks
def check_chart(root, name, online=False):
    problems = []
    chart_dir = os.path.join(root, "charts", name)
    rel = f"charts/{name}"

    chart_yaml_path = os.path.join(chart_dir, "Chart.yaml")
    if not os.path.exists(chart_yaml_path):
        return [f"{rel}: no Chart.yaml"]
    with open(chart_yaml_path) as fh:
        chart = yaml.safe_load(fh) or {}

    version = str(chart.get("version") or "")
    annotations = chart.get("annotations") or {}

    # 1 + 2 -- the two files the publisher's gate requires, in the ARCHIVE
    tmp = tempfile.mkdtemp()
    try:
        tgz, err = package(chart_dir, tmp)
        if err:
            problems.append(f"{rel}: {err}")
            names = []
        else:
            names = members(tgz)
        if names:
            if not any(n.endswith("/values.schema.json") for n in names):
                problems.append(
                    f"{rel}: packaged chart has no values.schema.json -- the blueprints "
                    f"channel gate rejects it (publish to `operators` instead, or add one)")
            if not any(n.endswith("/compositiondefinition.yaml")
                       or n.endswith("/compositiondefinition.yml") for n in names):
                problems.append(
                    f"{rel}: packaged chart has no compositiondefinition.yaml -- the gate "
                    f"inspects the .tgz, so a copy beside the chart does not count")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 3 -- icon
    icon = chart.get("icon") or ""
    if not icon.startswith("https://"):
        problems.append(f"{rel}: icon must be an absolute https URL, got {icon!r}")
    elif "/blob/" in icon:
        problems.append(
            f"{rel}: icon is a github /blob/ URL, which serves an HTML page rather than "
            f"the image -- it passes the gate and renders a blank card: {icon}")
    elif online:
        why = resolves_to_image(icon)
        if why:
            problems.append(f"{rel}: icon does not serve an image ({why}): {icon}")

    # 4 -- the annotation that decides whether a card appears at all
    src = annotations.get("krateo.io/source-repo")
    if not src:
        problems.append(
            f"{rel}: Chart.yaml has no `krateo.io/source-repo` annotation. The Marketplace "
            f"only adds a card for a live-index chart whose newest version carries it, so "
            f"without it the card waits on a catalog release nobody here controls")
    elif src != SOURCE_REPO:
        problems.append(f"{rel}: krateo.io/source-repo is {src!r}, expected {SOURCE_REPO!r}")

    for required_annotation in ("krateo.io/category", "krateo.io/maturity"):
        if not annotations.get(required_annotation):
            problems.append(f"{rel}: Chart.yaml has no `{required_annotation}` annotation")

    # 5 + 6 -- the in-chart CompositionDefinition
    cd_path = None
    for candidate in ("compositiondefinition.yaml", "compositiondefinition.yml"):
        if os.path.exists(os.path.join(chart_dir, candidate)):
            cd_path = os.path.join(chart_dir, candidate)
            break
    if cd_path:
        docs = [d for d in yaml.safe_load_all(open(cd_path)) if d]
        cds = [d for d in docs if d.get("kind") == "CompositionDefinition"]
        if len(cds) != 1:
            problems.append(f"{rel}: expected exactly one CompositionDefinition, found {len(cds)}")
        else:
            spec_chart = (cds[0].get("spec") or {}).get("chart") or {}
            cd_version = str(spec_chart.get("version") or "")
            if cd_version != version:
                problems.append(
                    f"{rel}: compositiondefinition.yaml pins version {cd_version!r} but "
                    f"Chart.yaml is {version!r} -- the chart version IS the served "
                    f"apiVersion, so these cannot differ")
            url = str(spec_chart.get("url") or "")
            if url and not url.rstrip("/").endswith(name) and name not in url:
                problems.append(
                    f"{rel}: compositiondefinition.yaml chart url {url!r} does not name "
                    f"this chart")

    # 7 + 8 + 9 -- the schema, which is the Marketplace form
    schema_path = os.path.join(chart_dir, "values.schema.json")
    if os.path.exists(schema_path):
        with open(schema_path) as fh:
            schema = json.load(fh)

        # NESTED occurrences too, not just the root. The root rule exists for
        # chart-inspector's dry-run (core-provider#325); this one is harder: the
        # API SERVER forbids `additionalProperties` ALONGSIDE `properties`
        # anywhere in a CRD schema, and crdgen copies values.schema.json nodes
        # through verbatim. Proven on the live cluster with a server-side dry-run:
        #   spec.validation.openAPIV3Schema.properties[spec].properties[nodeAccess]
        #   .additionalProperties: Forbidden: additionalProperties and properties
        #   are mutual exclusive
        # So a nested one makes the generated CRD unregisterable: the new version
        # is never served while registering it still stops the previous one, and
        # a PUBLISHED blueprint becomes uninstallable. That happened here with
        # kubernetes-cluster 0.15.0 on 2026-11-08, and this rule tested only the
        # root, so it passed.
        def _ap_paths(node, path="(root)"):
            if not isinstance(node, dict):
                return
            if node.get("additionalProperties") is False and "properties" in node:
                yield path
            for k, v in (node.get("properties") or {}).items():
                yield from _ap_paths(v, f"{path}.{k}" if path != "(root)" else k)
            it = node.get("items")
            if isinstance(it, dict):
                yield from _ap_paths(it, f"{path}[]")

        for where in _ap_paths(schema):
            if where == "(root)":
                continue  # the root case is reported by the rule below
            problems.append(
                f"{rel}: values.schema.json sets additionalProperties:false "
                f"alongside properties at `{where}`. The API server forbids that "
                f"combination in a CRD and crdgen copies this node through, so "
                f"the generated CRD will not register and the published version "
                f"becomes uninstallable. Remove additionalProperties there.")

        if schema.get("additionalProperties") is False:
            problems.append(
                f"{rel}: values.schema.json root sets additionalProperties:false. helm "
                f"injects `global` and chart-inspector runs a real dry-run install, so this "
                f"surfaces as an opaque HTTP 500 rather than a validation message "
                f"(core-provider#325)")

        undocumented = [
            p for p, n in schema_paths(schema)
            if p and not (n.get("title") and n.get("description"))
        ]
        if undocumented:
            shown = ", ".join(undocumented[:6])
            more = f" (+{len(undocumented) - 6} more)" if len(undocumented) > 6 else ""
            problems.append(
                f"{rel}: {len(undocumented)} schema path(s) lack title+description, so the "
                f"Marketplace form shows a bare key with no help: {shown}{more}")

        for path, parent in defaulted_under_optional(schema):
            problems.append(
                f"{rel}: `{path}` has a schema default under the optional object "
                f"`{parent}`. crdgen gives that parent `default: {{}}`, the API server "
                f"materialises the stanza, and it is NOT reversible -- put the default in "
                f"values.yaml instead")

    return problems


# -------------------------------------------------------------------- self-test
def self_test():
    """Build a chart that breaks each check, and require the check to catch it.

    Calibration, not ceremony: a screen whose detection quietly stopped working
    reports OK forever, which is the failure it exists to catch, one level up.
    """
    cases = []
    root = tempfile.mkdtemp()
    try:
        def build(label, chart_extra=None, schema=None, cd=None, drop_schema=False):
            name = "probe-" + label
            d = os.path.join(root, "charts", name)
            os.makedirs(os.path.join(d, "templates"), exist_ok=True)
            chart = {
                "apiVersion": "v2", "name": name, "version": "1.0.0",
                "description": "probe", "type": "application",
                "icon": "https://example.invalid/i.svg",
                "annotations": {
                    "krateo.io/source-repo": SOURCE_REPO,
                    "krateo.io/category": "compute",
                    "krateo.io/maturity": "beta",
                },
            }
            chart.update(chart_extra or {})
            with open(os.path.join(d, "Chart.yaml"), "w") as fh:
                yaml.safe_dump(chart, fh)
            with open(os.path.join(d, "templates", "cm.yaml"), "w") as fh:
                fh.write("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: probe\n")
            if not drop_schema:
                with open(os.path.join(d, "values.schema.json"), "w") as fh:
                    json.dump(schema if schema is not None else {
                        "type": "object",
                        "properties": {"a": {"type": "string", "title": "A",
                                             "description": "The a."}},
                    }, fh)
            if cd is not False:
                with open(os.path.join(d, "compositiondefinition.yaml"), "w") as fh:
                    yaml.safe_dump(cd or {
                        "apiVersion": "core.krateo.io/v1alpha1",
                        "kind": "CompositionDefinition",
                        "metadata": {"name": name, "namespace": "krateo-system"},
                        "spec": {"chart": {
                            "url": f"oci://ghcr.io/krateo-blueprints/charts/{name}",
                            "version": "1.0.0"}},
                    }, fh)
            return name

        # The control: a correct blueprint must produce NO problem. Without it a
        # check that fired on everything would look like a working screen.
        cases.append(("a correct blueprint passes", build("ok"), False))

        cases.append(("a missing values.schema.json is caught",
                      build("noschema", drop_schema=True), True))
        cases.append(("a missing compositiondefinition.yaml is caught",
                      build("nocd", cd=False), True))
        cases.append(("a non-https icon is caught",
                      build("httpicon", chart_extra={"icon": "http://example.invalid/i.svg"}), True))
        cases.append(("a github /blob/ icon is caught -- it passes the real gate",
                      build("blobicon", chart_extra={
                          "icon": "https://github.com/o/r/blob/main/i.svg"}), True))
        cases.append(("a missing krateo.io/source-repo is caught",
                      build("nosrc", chart_extra={"annotations": {
                          "krateo.io/category": "compute",
                          "krateo.io/maturity": "beta"}}), True))
        cases.append(("a CD version that drifts from Chart.yaml is caught",
                      build("drift", cd={
                          "apiVersion": "core.krateo.io/v1alpha1",
                          "kind": "CompositionDefinition",
                          "metadata": {"name": "probe-drift"},
                          "spec": {"chart": {
                              "url": "oci://ghcr.io/krateo-blueprints/charts/probe-drift",
                              "version": "0.9.0"}}}), True))
        cases.append(("an undocumented property is caught", build("nodocs", schema={
            "type": "object", "properties": {"a": {"type": "string"}}}), True))
        cases.append(("a title with no description is caught", build("titleonly", schema={
            "type": "object",
            "properties": {"a": {"type": "string", "title": "A"}}}), True))
        cases.append(("NESTED additionalProperties:false beside properties is caught",
                      build("apnested", schema={
            "type": "object",
            "properties": {"nodeAccess": {
                "type": "object", "title": "t", "description": "d",
                "additionalProperties": False,
                "properties": {"password": {"type": "string", "title": "t", "description": "d"}}}},
        }), True))
        cases.append(("a nested object WITHOUT additionalProperties is accepted",
                      build("apnestedok", schema={
            "type": "object",
            "properties": {"nodeAccess": {
                "type": "object", "title": "t", "description": "d",
                "properties": {"password": {"type": "string", "title": "t", "description": "d"}}}},
        }), False))
        cases.append(("additionalProperties:false is caught", build("apfalse", schema={
            "type": "object", "additionalProperties": False,
            "properties": {"a": {"type": "string", "title": "A", "description": "d"}}}), True))
        cases.append(("a default under an optional object is caught", build("nested", schema={
            "type": "object",
            "properties": {"p": {
                "type": "object", "title": "P", "description": "d",
                "properties": {"c": {"type": "boolean", "default": False,
                                     "title": "C", "description": "d"}}}}}), True))
        # The discriminating arm: the SAME default one level up is allowed, so
        # the check tests the shape with the irreversible consequence and not
        # merely the presence of the word "default".
        cases.append(("a top-level default is allowed", build("toplevel", schema={
            "type": "object",
            "properties": {"c": {"type": "boolean", "default": False,
                                 "title": "C", "description": "d"}}}), False))
        # ... and a default under a REQUIRED nested property is allowed too: the
        # parent then has `required`, so crdgen adds no `default: {}`.
        cases.append(("a default under a required property is allowed", build("req", schema={
            "type": "object",
            "properties": {"p": {
                "type": "object", "title": "P", "description": "d",
                "required": ["c"],
                "properties": {"c": {"type": "boolean", "default": False,
                                     "title": "C", "description": "d"}}}}}), False))

        failures = []
        for label, name, should_fail in cases:
            found = check_chart(root, name, online=False)
            if bool(found) != should_fail:
                failures.append(
                    f"{label}: expected {'a problem' if should_fail else 'no problem'}, "
                    f"got {found or 'none'}")
        print(f"  self-test: {len(cases)} cases, {len(failures)} failure(s)")
        for f in failures:
            print("   ", f)
        return 1 if failures else 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main():
    args = sys.argv[1:]
    if "--self-test" in args:
        return self_test()
    online = "--online" in args

    problems = []
    for name in BLUEPRINTS:
        problems += check_chart(REPO, name, online=online)

    print(f"  checked {len(BLUEPRINTS)} blueprint chart(s): {', '.join(BLUEPRINTS)}"
          f"{' (icon resolved over the network)' if online else ''}")
    if problems:
        print(f"  {len(problems)} problem(s):")
        for p in problems:
            print("    -", p)
        return 1
    print("  OK -- both would pass the blueprints channel gate and render a usable form")
    return 0


if __name__ == "__main__":
    sys.exit(main())
