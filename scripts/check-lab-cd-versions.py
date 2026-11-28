#!/usr/bin/env python3
"""Standing screen: do the LAB-path CompositionDefinitions pin their chart's CURRENT version?

THE TWO PATHS, AND WHICH ONE THIS SCREENS. A chart has two CompositionDefinitions and
they are deliberately separate (see the NOTE at the top of each manifest):

  * MARKETPLACE -- charts/<name>/compositiondefinition.yaml, pointing at the published
    OCI artifact. check-blueprint-metadata.py already asserts it matches Chart.yaml.
  * LAB         -- manifests/compositiondefinition-<name>.yaml, pointing at the in-cluster
    chart server that `make composition-up` packages into. Read only by
    hack/chartrepo-sync.py, and until this screen by NOTHING that compared it with the
    chart.

This screen covers the second and does not touch the first. Measured when it was added:
kubernetes-cluster pinned 0.15.1 against a chart at 0.17.0, baremetal-host 0.6.3
against 0.7.0 -- so a fresh lab validated charts older than the ones the repo publishes,
and #128's worker node-ip fix (0.17.0) would never have been exercised by it.

WHAT IS ASSERTED, per chart under charts/ that HAS a lab manifest: `spec.chart.version`
equals Chart.yaml's version AND the filename in `spec.chart.url` is exactly
<chart-name>-<version>.tgz. Both, because the two drifting apart is the likelier real
bug: a bump that edits `version:` and leaves the url (or the reverse) makes
chartrepo-sync serve one thing and core-provider look for another.

ABSENCE IS NOT A FINDING. A chart legitimately may have no lab manifest. A manifest that
is PRESENT and disagrees is. A manifest with no chart behind it is reported, not fatal.

Exit: 0 PASS, 1 FINDING, 2 VOID (cannot measure: no charts found, an unparseable
Chart.yaml or manifest, a chart with no `version:`, a manifest with no
CompositionDefinition or no chart.version/url), 3 PyYAML missing.
"""
import os
import sys

try:
    import yaml
except ImportError:
    # A missing dependency measured NOTHING. Exit 3 rather than letting the traceback
    # exit 1, which in this screen family means "findings exist".
    print("check-lab-cd-versions: PyYAML missing -- VOID, not a pass", file=sys.stderr)
    sys.exit(3)


class Void(Exception):
    """The screen could not form an opinion. Never a pass."""


def _load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return [d for d in yaml.safe_load_all(fh) if d]
    except (OSError, yaml.YAMLError) as exc:
        raise Void("%s is unreadable or not valid YAML: %s" % (path, exc))


def chart_identity(root, cdir):
    """(name, version) from charts/<cdir>/Chart.yaml."""
    path = os.path.join(root, "charts", cdir, "Chart.yaml")
    docs = _load(path)
    if len(docs) != 1 or not isinstance(docs[0], dict):
        raise Void("%s is not a single YAML mapping" % path)
    version = docs[0].get("version")
    if version is None or str(version).strip() == "":
        raise Void("%s declares no `version:`" % path)
    return str(docs[0].get("name") or cdir), str(version)


def evaluate(root="."):
    """(failures, checked, absent, orphans). Raises Void when it cannot form an opinion."""
    cdir_root = os.path.join(root, "charts")
    charts = sorted(d for d in (os.listdir(cdir_root) if os.path.isdir(cdir_root) else [])
                    if os.path.isfile(os.path.join(cdir_root, d, "Chart.yaml")))
    if not charts:
        raise Void("no chart found under %s, so there is nothing to compare a manifest with"
                   % os.path.join(os.path.abspath(root), "charts"))

    failures, checked, absent = [], [], []
    for cdir in charts:
        name, version = chart_identity(root, cdir)
        rel = "manifests/compositiondefinition-%s.yaml" % cdir
        path = os.path.join(root, rel)
        if not os.path.isfile(path):
            absent.append(cdir)
            continue
        cds = [d for d in _load(path)
               if isinstance(d, dict) and d.get("kind") == "CompositionDefinition"]
        if len(cds) != 1:
            raise Void("%s: expected exactly one CompositionDefinition, found %d"
                       % (rel, len(cds)))
        chart = (cds[0].get("spec") or {}).get("chart") or {}
        pinned, url = chart.get("version"), chart.get("url")
        if not pinned or not url:
            raise Void("%s: spec.chart.version or spec.chart.url is missing" % rel)
        checked.append(cdir)
        if str(pinned) != version:
            failures.append(
                "%s pins version %r but charts/%s/Chart.yaml is %r -- a fresh lab "
                "(`make composition-up`) would validate an older chart than the repo "
                "publishes" % (rel, str(pinned), cdir, version))
        want = "%s-%s.tgz" % (name, version)
        got = str(url).rstrip("/").rsplit("/", 1)[-1]
        if got != want:
            failures.append(
                "%s url names %r but the chart is %r -- version and url have drifted "
                "apart, so chartrepo-sync serves one tarball and core-provider asks for "
                "another" % (rel, got, want))

    orphans = []
    mdir = os.path.join(root, "manifests")
    if os.path.isdir(mdir):
        for f in sorted(os.listdir(mdir)):
            if f.startswith("compositiondefinition-") and f.endswith(".yaml"):
                if f[len("compositiondefinition-"):-len(".yaml")] not in charts:
                    orphans.append(f)
    return failures, checked, absent, orphans


def main(root="."):
    try:
        failures, checked, absent, orphans = evaluate(root)
    except Void as exc:
        print("VOID -- %s" % exc)
        print("       This is NOT a pass: nothing about lab manifest versions was established.")
        return 2
    print("checked %d lab manifest(s): %s" % (len(checked), ", ".join(checked) or "none"))
    for a in absent:
        print("  no lab manifest for chart %s (not a finding)" % a)
    for o in orphans:
        print("  reported, not fatal: manifests/%s has no chart behind it" % o)
    if failures:
        for f in failures:
            print("::error::%s" % f)
        print("FAIL -- %d lab manifest disagreement(s)." % len(failures))
        return 1
    print("PASS -- every present lab manifest pins its chart's current version in both "
          "`version:` and the `url:` filename.")
    return 0


# ------------------------------------------------------------------ self-test
def self_test():
    """Prove the screen can FAIL, for the stated reason, and VOID distinctly from FAIL."""
    import contextlib
    import io
    import shutil
    import tempfile

    passed = total = 0

    def mk(tmp, chart_ver="0.7.0", pin="0.7.0", url_ver="0.7.0", manifest=True,
           chart_yaml=None, manifest_text=None, charts=True):
        if charts:
            os.makedirs(os.path.join(tmp, "charts", "demo"))
            with open(os.path.join(tmp, "charts", "demo", "Chart.yaml"), "w") as fh:
                fh.write(chart_yaml if chart_yaml is not None else
                         "apiVersion: v2\nname: demo\nversion: %s\n" % chart_ver)
        if manifest:
            os.makedirs(os.path.join(tmp, "manifests"), exist_ok=True)
            with open(os.path.join(tmp, "manifests", "compositiondefinition-demo.yaml"), "w") as fh:
                fh.write(manifest_text if manifest_text is not None else
                         "---\napiVersion: core.krateo.io/v1alpha1\nkind: CompositionDefinition\n"
                         "metadata:\n  name: demo\nspec:\n  chart:\n"
                         "    url: http://chartrepo/demo-%s.tgz\n    version: \"%s\"\n"
                         % (url_ver, pin))

    def case(label, want_rc, want_in, **kw):
        nonlocal passed, total
        total += 1
        tmp = tempfile.mkdtemp()
        try:
            mk(tmp, **kw)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = main(tmp)
            out = buf.getvalue()
            ok = rc == want_rc and all(s in out for s in want_in)
            print("  %-4s %-58s exit=%s" % ("ok" if ok else "FAIL", label, rc))
            if not ok:
                print("       wanted exit %s containing %r; got:\n%s" % (want_rc, want_in, out))
            else:
                passed += 1
        finally:
            shutil.rmtree(tmp)

    case("clean corpus passes", 0, ["PASS"])
    case("chart bumped, version: lags (NEGATIVE CONTROL)", 1,
         ["pins version '0.6.3'"], chart_ver="0.7.0", pin="0.6.3", url_ver="0.7.0")
    case("version: matches, url filename lags (NEGATIVE CONTROL)", 1,
         ["url names 'demo-0.6.3.tgz'"], pin="0.7.0", url_ver="0.6.3")
    case("both lag: two findings", 1, ["2 lab manifest disagreement"], pin="0.6.3", url_ver="0.6.3")
    case("no lab manifest is not a finding", 0, ["not a finding", "PASS"], manifest=False)
    case("VOID: no charts at all (NEGATIVE CONTROL)", 2, ["VOID", "no chart found"],
         charts=False, manifest=False)
    case("VOID: chart declares no version", 2, ["VOID", "no `version:`"],
         chart_yaml="apiVersion: v2\nname: demo\n")
    case("VOID: manifest unparseable", 2, ["VOID", "not valid YAML"],
         manifest_text="spec: [unclosed\n")
    case("VOID: manifest has no CompositionDefinition", 2, ["VOID", "found 0"],
         manifest_text="apiVersion: v1\nkind: ConfigMap\n")

    print("SELF-TEST: %d/%d assertions passed" % (passed, total))
    return 0 if passed == total else 1


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--self-test", "--selftest", action="store_true", dest="self_test",
                    help="prove the screen can fail, then exit")
    args = ap.parse_args()
    if args.self_test:
        sys.exit(self_test())
    sys.exit(main())
