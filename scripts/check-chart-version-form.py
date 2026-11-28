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

  * `make check` -- on every pull request (standing-screens.yaml), which is where a bad
    version should be caught: at the commit that writes it, not months later at release
    time. It no longer runs as a step of the release itself, which is release-oci.
  * `hack/release-preflight` -- so the shared rule-12 job is not vacuous for this repo.

THE CHART LIST IS DERIVED BY THE SAME DISCOVERY THE RELEASE USES, NEVER RESTATED. Since
2026-11-28 the release is the org's release-oci workflow, which publishes every
first-class Chart.yaml it finds: pruning directories named examples, example,
__fixtures__, fixtures, testdata, test, tests, e2e, sample, samples, docs and
node_modules, and skipping a vendored subchart (a Chart.yaml whose parent directory is
`charts` and whose grandparent is itself a chart). This screen walks the tree with that
same rule, so a chart added anywhere it would be released is covered without touching
this file. Until then the list was read out of a hand-written `helm package` loop in
.github/workflows/release-chart.yaml; that loop is gone, and the screen that read it
would have VOIDed on every run. A chart under a pruned directory (the probe charts in
tests/charts/) is reported rather than failed -- it never reaches GHCR, so failing the
build on it would be a false positive on a chart nobody publishes.

KEEP THE PRUNE LIST IN STEP with release-oci.yaml's `find` (the pin in release-chart.yaml
names the version). It is copied, not shared: a divergence is silent in one direction
(a directory released that this screen never looks at), which is why the self-test pins
the list.

THE ZERO THIS SCREEN MUST NOT REPORT. "every version is fine" is only a measurement if
the walker found charts at all. If discovery finds no chart at all, or a chart it finds declares no
version, the verdict is VOID (exit 2) -- never a pass. `--self-test`
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

# release-oci.yaml's prune list (its `find ... -prune`), plus .git. See the docstring.
PRUNED = frozenset((
    ".git", "node_modules", "examples", "example", "__fixtures__", "fixtures",
    "testdata", "test", "tests", "e2e", "sample", "samples", "docs"))


class Void(Exception):
    """The screen could not form an opinion. Never a pass."""


def _walk(root, prune):
    """Every directory under root holding a Chart.yaml, relative to root, sorted.

    The root itself is never pruned, only what is below it (find -mindepth 1).
    """
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        if prune:
            dirnames[:] = [d for d in dirnames if d not in PRUNED]
        if "Chart.yaml" in filenames:
            found.append(os.path.relpath(dirpath, root))
    return sorted(found)


def published_chart_dirs(root="."):
    """The chart directories release-oci would package and push.

    Raises Void rather than returning an empty list: "every version is fine" is only a
    measurement if discovery found charts at all, and this is the one coupling to the
    release, so this is where it would silently go blind.
    """
    dirs = []
    for d in _walk(root, prune=True):
        parent = os.path.dirname(d)
        # a vendored subchart: parent dir named `charts` AND grandparent is itself a chart
        if (parent and os.path.basename(parent) == "charts"
                and os.path.isfile(os.path.join(root, os.path.dirname(parent), "Chart.yaml"))):
            continue
        dirs.append(d)
    if not dirs:
        raise Void("discovery (release-oci's rule) found no first-class chart under %s, so "
                   "which charts are published cannot be established" % os.path.abspath(root))
    return dirs


def chart_version(root, cdir):
    """The `version:` of one chart, read without a YAML dependency.

    Raises Void for a chart with no readable Chart.yaml: release-oci would fail to
    package it, and this screen must not report that as a version pass.
    """
    path = os.path.join(root, cdir, "Chart.yaml")
    if not os.path.exists(path):
        raise Void("%s does not exist" % os.path.join(cdir, "Chart.yaml"))
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            m = re.match(r"^version:\s*(\S+)\s*$", line)
            if m:
                return m.group(1).strip("'\"")
    raise Void("%s declares no top-level `version:`" % os.path.join(cdir, "Chart.yaml"))


def evaluate(root="."):
    """(failures, reported, checked_names). Raises Void when it cannot form an opinion."""
    published = published_chart_dirs(root)
    failures, reported, checked = [], [], []

    for cdir in published:
        version = chart_version(root, cdir)
        name = os.path.basename(cdir.rstrip("/")) or os.path.basename(os.path.abspath(root))
        checked.append(name)
        if not PLAIN_VERSION.match(version):
            failures.append(
                "%s %s: not a plain X.Y.Z release version. The index would refuse it "
                "after it is already in GHCR, leaving the artifact published and the "
                "index entry missing (PUBLISHING.md rules 12 and 13)"
                % (name, version))

    # Charts that exist but are NOT released (under a pruned directory): reported, never
    # fatal. They do not reach GHCR, so rule 13 cannot bite them -- but a reader should
    # know they were seen.
    released = set(published)
    for cdir in _walk(root, prune=False):
        if cdir in released or cdir.split(os.sep)[0] == ".git":
            continue
        try:
            version = chart_version(root, cdir)
        except Void as exc:
            reported.append("%s: %s" % (cdir, exc))
            continue
        if not PLAIN_VERSION.match(version):
            reported.append(
                "%s %s: not a plain X.Y.Z, but release-oci does not release this chart "
                "(pruned directory or vendored subchart), so it never reaches GHCR"
                % (cdir, version))
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
    import contextlib
    import io
    import shutil
    import tempfile

    fails = 0

    def chart(tmp, rel, version, name=None):
        d = os.path.join(tmp, rel)
        os.makedirs(d, exist_ok=True)
        body = "apiVersion: v2\nname: %s\n" % (name or os.path.basename(rel))
        if version is not None:
            body += "version: %s\n" % version
        with open(os.path.join(d, "Chart.yaml"), "w", encoding="utf-8") as fh:
            fh.write(body)

    def two(t, a="1.2.3", b="0.16.1"):
        chart(t, "charts/a", a)
        chart(t, "charts/b", b)

    cases = [
        # (label, builder, expected exit)
        ("two clean X.Y.Z versions pass", lambda t: two(t), 0),
        ("a -prerelease is REFUSED", lambda t: two(t, a="1.2.3-rc1"), 1),
        ("a v-prefix is REFUSED", lambda t: two(t, a="v1.2.3"), 1),
        ("a two-component version is REFUSED", lambda t: two(t, a="1.2"), 1),
        ("a leading zero is REFUSED", lambda t: two(t, a="01.2.3"), 1),
        ("+build metadata is REFUSED", lambda t: two(t, a="1.2.3+build"), 1),
        ("0.0.0 is a legitimate plain version and PASSES", lambda t: two(t, a="0.0.0"), 0),
        # Discovery. A chart ADDED to charts/ with no edit to this file must be covered:
        # that is the property the old workflow-derived list had and this one must keep.
        ("a chart added under charts/ is discovered and refused",
         lambda t: (two(t), chart(t, "charts/c", "1.0.0-rc1")), 1),
        ("a chart outside charts/ is discovered too (release-oci takes any layout)",
         lambda t: (two(t), chart(t, "deploy/d", "1.0")), 1),
        ("an EMPTY tree is VOID, not a pass", lambda t: None, 2),
        ("a tree whose only charts are all pruned is VOID",
         lambda t: chart(t, "tests/charts/p", "0.0.1"), 2),
        ("a chart with no version: is VOID",
         lambda t: (two(t), chart(t, "charts/c", None)), 2),
    ]
    # every directory release-oci prunes must be pruned HERE, with a bad version beside a
    # good release: a name missing from PRUNED would turn a fixture into a failure.
    for pruned in sorted(PRUNED - {".git"}):
        cases.append((
            "a bad-version chart under %s/ is NOT released, so it is not fatal" % pruned,
            (lambda pr: lambda t: (two(t), chart(t, pr + "/p", "0.0.1-rc1")))(pruned), 0))
    cases.append((
        "a vendored subchart (charts/<dep> inside a chart) is not released",
        lambda t: (two(t), chart(t, "charts/a/charts/dep", "9.9")), 0))

    for label, builder, want in cases:
        tmp = tempfile.mkdtemp()
        try:
            builder(tmp)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                got = main(tmp)
            if got != want:
                fails += 1
                print("  %-72s FAILED (wanted exit %d, got %d)" % (label, want, got))
                print("    output: %s" % buf.getvalue().strip().replace("\n", " | ")[:200])
            else:
                print("  %-72s ok" % label)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # An UNRELEASED chart with a bad version must be REPORTED, not merely tolerated: a
    # pruned directory that silently stops being looked at is the failure this guards.
    tmp = tempfile.mkdtemp()
    try:
        two(tmp)
        chart(tmp, "tests/charts/probe", "0.0.1-rc1")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            got = main(tmp)
        out = buf.getvalue()
        if got != 0:
            fails += 1
            print("  %-72s FAILED (exit %d)" % ("an UNRELEASED bad version is not fatal", got))
        elif "reported, not fatal" not in out or "probe" not in out:
            fails += 1
            print("  %-72s FAILED (not reported)" % "an UNRELEASED bad version is REPORTED")
        else:
            print("  %-72s ok" % "an UNRELEASED bad version is reported, not fatal")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # The pattern must be the org's, byte for byte. A drift here would make this screen
    # pass something the index then refuses, which is the whole failure mode.
    org = r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
    if PLAIN_VERSION.pattern != org:
        fails += 1
        print("  %-72s FAILED (%r != %r)"
              % ("the pattern matches the org's byte for byte", PLAIN_VERSION.pattern, org))
    else:
        print("  %-72s ok" % "the pattern matches the org's byte for byte")

    # And the prune list must be release-oci's, name for name.
    want_pruned = {".git", "node_modules", "examples", "example", "__fixtures__", "fixtures",
                   "testdata", "test", "tests", "e2e", "sample", "samples", "docs"}
    if set(PRUNED) != want_pruned:
        fails += 1
        print("  %-72s FAILED (%s)" % ("the prune list matches release-oci's",
                                       sorted(set(PRUNED) ^ want_pruned)))
    else:
        print("  %-72s ok" % "the prune list matches release-oci's")

    print("selftest: %d case(s), %d failure(s)" % (len(cases) + 3, fails))
    if not fails:
        print("  -> the screen refuses a pre-release, a v-prefix, a short version, a "
              "leading zero and +build;")
        print("     covers a chart added anywhere release-oci would release it; VOIDs when "
              "discovery finds nothing; and does not fail on a chart nobody pushes.")
    return 1 if fails else 0


_WANT_SELFTEST = False

if __name__ == "__main__":
    # MODE FLAGS: both spellings accepted, anything else REFUSED.
    # This repo spells it `--selftest` on some screens and `--self-test` on others, and a
    # bare `in sys.argv` test SILENTLY RUNS THE OTHER MODE on a typo, exiting 0. That
    # produced two false conclusions in one session: a screen was reported as "not running
    # its controls" when the flag was merely misspelt, and a second was read as failing
    # when argparse had refused the argument. The argparse-based screens caught it; these
    # could not.
    #
    # Inlined rather than shared because the Makefile runs these under `python3 -I`, which
    # removes the script's directory from sys.path -- verified, not assumed -- so a sibling
    # import fails. Six duplicated lines are the price of that isolation.
    _ALLOWED = {"--self-test", "--selftest"}
    _unknown = [a for a in sys.argv[1:] if a not in _ALLOWED]
    if _unknown:
        sys.stderr.write("usage: %s [%s]\n" % (sys.argv[0], "|".join(sorted(_ALLOWED))))
        sys.stderr.write("refusing unrecognised argument(s): %s\n" % " ".join(_unknown))
        sys.stderr.write("a mistyped mode flag would otherwise run the OTHER mode and exit 0.\n")
        sys.exit(64)
    _WANT_SELFTEST = bool({"--self-test", "--selftest"} & set(sys.argv[1:]))
    if _WANT_SELFTEST:
        sys.exit(self_test())
    sys.exit(main())
