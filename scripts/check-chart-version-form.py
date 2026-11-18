#!/usr/bin/env python3
"""Standing screen: is every PUBLISHED chart's version a plain X.Y.Z?

krateo-blueprints/charts PUBLISHING.md rule 13: the channel index never carries a
pre-release. `publish-chart.yaml` enforces it -- but **at index time, after the GHCR
push**. So a chart whose version is not plain X.Y.Z is pushed to GHCR and then refused
by the indexer: the artifact exists, the index entry does not, and the release reports a
failure having already written to the registry. That half-published state is the thing
this screen exists to make impossible.

WHY A SCREEN AND NOT ONLY THE SHARED PREFLIGHT. charts 1.4.3 ships
`release-preflight.yaml` (rule 12), and this repo calls it -- but it checks the ONE
`version` input it is given, which for this repo is the git tag. **This repo publishes
FOUR charts, each at its own Chart.yaml version, so the tag is not any of the versions
GHCR receives.** The shared preflight also passes outright when a repo declares no
`hack/release-preflight`. Wired that way and nothing else, rule 13 would still be
unchecked here before the push.

So the check lives here, and `hack/release-preflight` DELEGATES to this file. One
implementation, two callers:

  * `make check` -- step 2 of the publish job, and the push is step 7, so this gates the
    push on BOTH a tag and a workflow_dispatch. It also runs on every pull request, which
    is where a bad version should be caught: at the commit that writes it, not months
    later at release time.
  * `hack/release-preflight` -- so the shared rule-12 job is not vacuous for this repo.

THE CHART LIST IS DERIVED FROM THE WORKFLOW, NEVER RESTATED. A hand-copied list is a
second copy that goes stale the moment a chart is added to the release, and this
repository has already been bitten by a hand-kept copy of a value the tooling owns. The
names are read out of `helm package` in .github/workflows/release-chart.yaml, so a chart
added to the release is covered without touching this file, and a chart that is NOT
packaged (the probe charts) is reported rather than failed -- it never reaches GHCR, so
failing the build on it would be a false positive on a chart nobody publishes.

THE ZERO THIS SCREEN MUST NOT REPORT. "every version is fine" is only a measurement if
the walker found charts at all. If the workflow's package list cannot be parsed, or names
a chart that does not exist, the verdict is VOID (exit 2) -- never a pass. `--self-test`
plants a case for every branch and asserts each one fires.

Exit: 0 PASS, 1 FAIL, 2 VOID.
"""
import glob
import os
import re
import sys

# Byte-for-byte the pattern in krateo-blueprints/charts release-preflight.yaml and
# publish-chart.yaml (both define it as PLAIN_VERSION; their own
# test/release-preflight/run-cases.py fails if the two of theirs differ). Plain X.Y.Z:
# no `v`, no leading zeros, no -prerelease, no +build.
PLAIN_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")

WORKFLOW = ".github/workflows/release-chart.yaml"
# `helm package "$dir" --destination dist` inside a for-loop over explicit chart dirs.
PACKAGE_LOOP = re.compile(
    r"for\s+dir\s+in\s+(?P<dirs>[^\n;]*?)\s*;\s*do\s*\n\s*helm\s+package", re.M)


class Void(Exception):
    """The screen could not form an opinion. Never a pass."""


def packaged_chart_dirs(root="."):
    """The chart directories the release workflow actually packages and pushes.

    Raises Void rather than returning a default: a screen that silently checks nothing
    because a regex stopped matching is worse than no screen, and this one has exactly
    one coupling to the workflow, so that coupling is where it would break.
    """
    path = os.path.join(root, WORKFLOW)
    if not os.path.exists(path):
        raise Void("%s does not exist, so the packaged-chart list cannot be derived" % WORKFLOW)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    matches = PACKAGE_LOOP.findall(text)
    if not matches:
        raise Void(
            "no `for dir in ... ; do helm package` loop found in %s. The release workflow's "
            "packaging step changed shape, so this screen can no longer tell which charts "
            "are published. Fix the pattern here rather than hardcoding a list." % WORKFLOW)
    if len(matches) > 1:
        raise Void(
            "%d `helm package` loops in %s; this screen models one. Which charts are "
            "published is now ambiguous." % (len(matches), WORKFLOW))
    dirs = [d for d in matches[0].split() if d]
    if not dirs:
        raise Void("the `helm package` loop in %s lists no chart directories" % WORKFLOW)
    return dirs


def chart_version(root, cdir):
    """The `version:` of one chart, read without a YAML dependency.

    Raises Void for a chart the workflow names but that is not here: the workflow would
    fail at `helm package`, and this screen must not report that as a version pass.
    """
    path = os.path.join(root, cdir, "Chart.yaml")
    if not os.path.exists(path):
        raise Void(
            "%s names %s in its `helm package` loop, but %s does not exist"
            % (WORKFLOW, cdir, os.path.join(cdir, "Chart.yaml")))
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            m = re.match(r"^version:\s*(\S+)\s*$", line)
            if m:
                return m.group(1).strip("'\"")
    raise Void("%s declares no top-level `version:`" % os.path.join(cdir, "Chart.yaml"))


def evaluate(root="."):
    """(failures, reported, checked_names). Raises Void when it cannot form an opinion."""
    published = packaged_chart_dirs(root)
    failures, reported, checked = [], [], []

    for cdir in published:
        version = chart_version(root, cdir)
        name = os.path.basename(cdir.rstrip("/"))
        checked.append(name)
        if not PLAIN_VERSION.match(version):
            failures.append(
                "%s %s: not a plain X.Y.Z release version. publish-chart.yaml would push "
                "it to GHCR and then REFUSE to index it, leaving the artifact published "
                "and the index entry missing (PUBLISHING.md rules 12 and 13)"
                % (name, version))

    # Charts that exist but are not packaged: reported, never fatal. They do not reach
    # GHCR, so rule 13 cannot bite them -- but a reader should know they were seen.
    pub_names = {os.path.basename(d.rstrip("/")) for d in published}
    for chart_yaml in sorted(glob.glob(os.path.join(root, "charts", "*", "Chart.yaml"))):
        cdir = os.path.dirname(chart_yaml)
        name = os.path.basename(cdir)
        if name in pub_names:
            continue
        rel = os.path.relpath(cdir, root)
        try:
            version = chart_version(root, rel)
        except Void as exc:
            reported.append("%s: %s" % (name, exc))
            continue
        if not PLAIN_VERSION.match(version):
            reported.append(
                "%s %s: not a plain X.Y.Z, but this chart is NOT in the release "
                "workflow's package list, so it never reaches GHCR" % (name, version))
    return failures, reported, checked


def main(root="."):
    try:
        failures, reported, checked = evaluate(root)
    except Void as exc:
        print("VOID -- %s" % exc)
        print("       This is NOT a pass: nothing about chart versions was established.")
        return 2

    print("checked %d published chart(s) against rule 13: %s"
          % (len(checked), ", ".join(checked)))
    for r in reported:
        print("  reported, not fatal: %s" % r)
    if failures:
        for f in failures:
            print("::error::%s" % f)
        print("FAIL -- %d published chart version(s) would be pushed and then refused "
              "by the indexer." % len(failures))
        return 1
    print("PASS -- every published chart version is a plain X.Y.Z, so none can be pushed "
          "to GHCR and then refused at index time.")
    return 0


# ------------------------------------------------------------------ self-test
def self_test():
    """Prove this screen can FAIL, and for the stated reason.

    A screen whose positive case is never exercised reports success for free. Every
    branch above gets a case, and the accept case is here so a future tightening that
    rejects a legitimate version is caught too.
    """
    import shutil
    import tempfile

    fails = 0
    workflow_ok = (
        "jobs:\n  publish:\n    steps:\n"
        "      - name: helm package (all charts)\n"
        "        run: |\n"
        "          mkdir -p dist\n"
        "          for dir in charts/a charts/b; do\n"
        "            helm package \"$dir\" --destination dist\n"
        "          done\n")

    def build(tmp, workflow=workflow_ok, charts=None):
        os.makedirs(os.path.join(tmp, ".github", "workflows"), exist_ok=True)
        with open(os.path.join(tmp, WORKFLOW), "w", encoding="utf-8") as fh:
            fh.write(workflow)
        for name, version in (charts or {"a": "1.2.3", "b": "0.16.1"}).items():
            d = os.path.join(tmp, "charts", name)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "Chart.yaml"), "w", encoding="utf-8") as fh:
                fh.write("apiVersion: v2\nname: %s\nversion: %s\n" % (name, version))

    cases = [
        # (label, builder, expected exit)
        ("two clean X.Y.Z versions pass", lambda t: build(t), 0),
        ("a -prerelease is REFUSED",
         lambda t: build(t, charts={"a": "1.2.3-rc1", "b": "0.16.1"}), 1),
        ("a v-prefix is REFUSED",
         lambda t: build(t, charts={"a": "v1.2.3", "b": "0.16.1"}), 1),
        ("a two-component version is REFUSED",
         lambda t: build(t, charts={"a": "1.2", "b": "0.16.1"}), 1),
        ("a leading zero is REFUSED",
         lambda t: build(t, charts={"a": "01.2.3", "b": "0.16.1"}), 1),
        ("+build metadata is REFUSED",
         lambda t: build(t, charts={"a": "1.2.3+build", "b": "0.16.1"}), 1),
        ("0.0.0 is a legitimate plain version and PASSES",
         lambda t: build(t, charts={"a": "0.0.0", "b": "0.16.1"}), 0),
        ("a missing workflow is VOID, not a pass",
         lambda t: build(t, charts={"a": "1.2.3", "b": "1.0.0"}) or os.remove(
             os.path.join(t, WORKFLOW)), 2),
        ("a workflow with no package loop is VOID",
         lambda t: build(t, workflow="jobs:\n  publish:\n    steps: []\n"), 2),
        ("TWO package loops are VOID (ambiguous)",
         lambda t: build(t, workflow=workflow_ok + workflow_ok), 2),
        ("a packaged chart with no Chart.yaml is VOID",
         lambda t: (build(t), shutil.rmtree(os.path.join(t, "charts", "b"))), 2),
        ("a Chart.yaml with no version: is VOID",
         lambda t: (build(t), open(os.path.join(t, "charts", "b", "Chart.yaml"), "w",
                                   encoding="utf-8").write("apiVersion: v2\nname: b\n")), 2),
    ]

    for label, builder, want in cases:
        tmp = tempfile.mkdtemp()
        try:
            builder(tmp)
            import io
            import contextlib
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                got = main(tmp)
            if got != want:
                fails += 1
                print("  %-52s FAILED (wanted exit %d, got %d)" % (label, want, got))
                print("    output: %s" % buf.getvalue().strip().replace("\n", " | ")[:200])
            else:
                print("  %-52s ok" % label)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # An UNPACKAGED chart with a bad version must be reported, never fatal.
    tmp = tempfile.mkdtemp()
    try:
        build(tmp)
        probe = os.path.join(tmp, "charts", "probe")
        os.makedirs(probe, exist_ok=True)
        with open(os.path.join(probe, "Chart.yaml"), "w", encoding="utf-8") as fh:
            fh.write("apiVersion: v2\nname: probe\nversion: 0.0.1-rc1\n")
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            got = main(tmp)
        out = buf.getvalue()
        if got != 0:
            fails += 1
            print("  %-52s FAILED (exit %d)" % ("an UNPACKAGED bad version is not fatal", got))
        elif "reported, not fatal" not in out or "probe" not in out:
            fails += 1
            print("  %-52s FAILED (not reported)" % "an UNPACKAGED bad version is REPORTED")
        else:
            print("  %-52s ok" % "an UNPACKAGED bad version is reported, not fatal")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # The pattern must be the org's, byte for byte. A drift here would make this screen
    # pass something publish-chart.yaml then refuses, which is the whole failure mode.
    org = r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
    if PLAIN_VERSION.pattern != org:
        fails += 1
        print("  %-52s FAILED (%r != %r)"
              % ("the pattern matches the org's byte for byte", PLAIN_VERSION.pattern, org))
    else:
        print("  %-52s ok" % "the pattern matches the org's byte for byte")

    print("selftest: %d case(s), %d failure(s)" % (len(cases) + 2, fails))
    if not fails:
        print("  -> the screen refuses a pre-release, a v-prefix, a short version, a "
              "leading zero and +build;")
        print("     VOIDs when it cannot tell which charts are published; and does not "
              "fail on a chart nobody pushes.")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv or "--selftest" in sys.argv:
        sys.exit(self_test())
    sys.exit(main())
