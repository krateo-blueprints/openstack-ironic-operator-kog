#!/usr/bin/env python3
"""Every chart must render at least one document the way the release image gate renders it.

THE DEFECT. release-oci's `verify-images` job runs `helm template` on every chart with
its DEFAULT values and inspects the output for container images. In the 2026-11-28 dry
run (run 46110665051) it reported

    checked 0 image reference(s) across 2 chart(s)

because `baremetal-host` is wrapped in `if .Values.nodeName`: from defaults it renders
SUCCESSFULLY and emits nothing. The gate hard-fails only when a render FAILS, so a chart
that renders NOTHING is a silent pass that exercised no template at all -- and it is this
repo's principal chart.

THE SUPPORTED FIX is the gate's own contract: a chart whose defaults are not a valid
configuration supplies the smallest one that is in `<chart dir>/ci/*-values.yaml`, which
the gate passes as `-f`. This screen is the half the gate lacks. It renders each chart
under charts/ exactly as the gate does (every ci/*-values.yaml, sorted, as `-f`; defaults
when there is none) and FAILS when the render emits zero documents.

Documents are counted by parsing the output, not by counting `kind:` lines, so a comment
or a string value that says `kind:` cannot satisfy it.

kubernetes-cluster is deliberately not given a ci/ file: it emits ONE document (a Secret)
from defaults and the same one with a fully populated configuration, because the rest is
lookup-gated and `helm template` runs offline. A file there would add nothing.

EXIT CODES: 0 pass, 1 FAIL (a chart renders nothing, or cannot render), 2 VOID (could not
measure: no helm, no charts), 3 PyYAML missing. VOID is never a pass.
"""
import glob
import os
import shutil
import subprocess
import sys
import tempfile

try:
    import yaml
except ImportError:
    # A missing dependency measured NOTHING; exit 3, not the traceback's 1 which in this
    # screen family means "findings exist".
    print("check-charts-render-something: PyYAML missing -- VOID, not a pass", file=sys.stderr)
    sys.exit(3)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Void(Exception):
    """The screen could not form an opinion. NOT a pass."""


def ci_values(chart_dir):
    """The files the gate passes as -f, in the order it passes them."""
    return sorted(glob.glob(os.path.join(chart_dir, "ci", "*-values.yaml")))


def count_documents(text):
    """Documents with a `kind`, by parsing -- not by grepping for `kind:`."""
    return sum(1 for d in yaml.safe_load_all(text or "") if isinstance(d, dict) and d.get("kind"))


def render(chart_dir):
    """-> (documents, error). Renders as the gate does."""
    if shutil.which("helm") is None:
        raise Void("helm is not installed, so nothing could be rendered")
    cmd = ["helm", "template", "probe", chart_dir]
    for f in ci_values(chart_dir):
        cmd += ["-f", f]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        lines = [l.strip() for l in r.stderr.splitlines() if l.strip().startswith("Error:")]
        return 0, (lines[0] if lines else "helm template failed")[:240]
    try:
        return count_documents(r.stdout), None
    except yaml.YAMLError as e:
        return 0, "output does not parse as YAML: %s" % str(e).splitlines()[0]


def verdict(name, ndocs, err, nci):
    """-> message when this chart FAILS, else None. Pure, so the self-test needs no helm."""
    if err:
        return "%s: cannot be rendered by the gate's command -- %s" % (name, err)
    if ndocs == 0:
        how = "with %d ci values file(s)" % nci if nci else "from its defaults (no ci/*-values.yaml)"
        return ("%s: renders ZERO documents %s, so release-oci's image gate inspects nothing "
                "here and passes. Add ci/render-values.yaml holding the smallest valid "
                "configuration, and list ci/ and .helmignore in a .helmignore so the "
                "package is unchanged." % (name, how))
    return None


def audit(charts):
    """-> (failures, rows). rows are (name, ndocs, nci)."""
    failures, rows = [], []
    for c in charts:
        name = os.path.basename(c.rstrip("/"))
        n, err = render(c)
        nci = len(ci_values(c))
        rows.append((name, n, nci))
        m = verdict(name, n, err, nci)
        if m:
            failures.append(m)
    return failures, rows


def run():
    charts = sorted(glob.glob(os.path.join(REPO, "charts", "*", "Chart.yaml")))
    charts = [os.path.dirname(c) for c in charts]
    if not charts:
        raise Void("no charts found under charts/, so this screen rendered nothing")
    failures, rows = audit(charts)
    for name, n, nci in rows:
        print("  %-22s %2d document(s)  (%d ci values file(s))" % (name, n, nci))
    if failures:
        print("\nFAIL (%d):" % len(failures))
        for f in failures:
            print("  " + f)
        return 1
    print("\nPASS -- every chart renders at least one document the way the image gate renders it.")
    return 0


def _fixture(root, name, template, ci=None):
    d = os.path.join(root, name)
    os.makedirs(os.path.join(d, "templates"))
    with open(os.path.join(d, "Chart.yaml"), "w") as f:
        f.write("apiVersion: v2\nname: %s\nversion: 0.0.1\n" % name)
    with open(os.path.join(d, "values.yaml"), "w") as f:
        f.write("nodeName:\n")
    with open(os.path.join(d, "templates", "t.yaml"), "w") as f:
        f.write(template)
    if ci is not None:
        os.makedirs(os.path.join(d, "ci"))
        with open(os.path.join(d, "ci", "render-values.yaml"), "w") as f:
            f.write(ci)
    return d


GATED = ("{{- if .Values.nodeName }}\napiVersion: v1\nkind: ConfigMap\nmetadata:\n"
         "  name: {{ .Values.nodeName }}\n{{- end }}\n")


def selftest():
    cases, failures = [], []

    def case(n, ok):
        cases.append((n, ok))
        if not ok:
            failures.append(n)

    # ---- pure parts, no helm ----
    case("zero documents, no ci file -> FAIL naming the defaults",
         "ZERO" in (verdict("c", 0, None, 0) or "") and "defaults" in verdict("c", 0, None, 0))
    case("zero documents even WITH a ci file -> FAIL (the file did not help)",
         "1 ci values" in (verdict("c", 0, None, 1) or ""))
    case("a render error -> FAIL, distinct from zero documents",
         "cannot be rendered" in (verdict("c", 0, "Error: x", 0) or ""))
    case("documents emitted -> no failure", verdict("c", 1, None, 0) is None)
    case("count_documents ignores a `kind:` inside a comment and a string",
         count_documents("# kind: Fake\nkey: 'kind: Fake'\n") == 0)
    case("count_documents counts every document in a multi-document render",
         count_documents("kind: A\n---\nkind: B\n") == 2)
    case("an empty render counts zero", count_documents("") == 0)

    # ---- fixtures through the real helm path ----
    tmp = tempfile.mkdtemp(prefix="render-something-")
    try:
        empty = _fixture(tmp, "gated-empty", GATED)
        rescued = _fixture(tmp, "gated-rescued", GATED, ci="nodeName: probe\n")
        plain = _fixture(tmp, "plain", "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: a\n")
        broken = _fixture(tmp, "broken", "{{ fail \"no\" }}\n")

        # NEGATIVE CONTROL: the exact shape of baremetal-host. It renders successfully
        # (helm exits 0) and emits nothing; the screen must reject it.
        f, rows = audit([empty])
        case("NEGATIVE CONTROL: a chart gated on a value, rendered from defaults -> FAIL",
             len(f) == 1 and rows[0][1] == 0)
        case("the control renders successfully, so only the ZERO check can catch it",
             subprocess.run(["helm", "template", "p", empty], capture_output=True).returncode == 0)
        f, rows = audit([rescued])
        case("the same gated chart with ci/render-values.yaml -> PASS and emits 1 document",
             not f and rows[0][1] == 1 and rows[0][2] == 1)
        f, _ = audit([plain])
        case("clean corpus: a chart that renders from defaults -> PASS", not f)
        f, _ = audit([broken])
        case("a chart that cannot render -> FAIL", len(f) == 1 and "cannot be rendered" in f[0])
        f, _ = audit([plain, empty, rescued])
        case("one bad chart among good ones is still reported, exactly once", len(f) == 1)

        # VOID is distinct from FAIL: with helm unreachable, render() must raise Void.
        saved = os.environ.get("PATH", "")
        os.environ["PATH"] = os.path.join(tmp, "nowhere")
        try:
            try:
                render(plain)
                void = False
            except Void:
                void = True
        finally:
            os.environ["PATH"] = saved
        case("helm missing -> VOID (an exception), not a FAIL and not a pass", void)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- the real repo ----
    real = [os.path.dirname(c) for c in glob.glob(os.path.join(REPO, "charts", "*", "Chart.yaml"))]
    case("the real corpus is non-empty, so a pass is not vacuous", len(real) > 0)
    bh = os.path.join(REPO, "charts", "baremetal-host")
    case("baremetal-host carries a ci values file (the defect this screen guards)",
         len(ci_values(bh)) > 0)

    for n, ok in cases:
        print("  %-76s %s" % (n, "ok" if ok else "FAIL"))
    print("selftest: %d cases, %d failure(s)" % (len(cases), len(failures)))
    for x in failures:
        print("  failed: %s" % x)
    return 1 if failures else 0


_WANT_SELFTEST = False

if __name__ == "__main__":
    # MODE FLAGS: both spellings accepted, anything else REFUSED (see check-screen-flags.py).
    _ALLOWED = {"--self-test", "--selftest"}
    _unknown = [a for a in sys.argv[1:] if a not in _ALLOWED]
    if _unknown:
        sys.stderr.write("usage: %s [%s]\n" % (sys.argv[0], "|".join(sorted(_ALLOWED))))
        sys.stderr.write("refusing unrecognised argument(s): %s\n" % " ".join(_unknown))
        sys.stderr.write("a mistyped mode flag would otherwise run the OTHER mode and exit 0.\n")
        sys.exit(64)
    _WANT_SELFTEST = bool({"--self-test", "--selftest"} & set(sys.argv[1:]))
    try:
        if _WANT_SELFTEST:
            sys.exit(selftest())
        sys.exit(run())
    except Void as v:
        print("VOID: %s" % v)
        print("NOT a pass.")
        sys.exit(2)
