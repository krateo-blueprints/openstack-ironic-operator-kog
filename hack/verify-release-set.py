#!/usr/bin/env python3
"""Require that the charts release-oci BUILT are exactly the charts this repository holds.

release-oci discovers what to publish from the layout, and its own checks (the collect
step, the index gate) assert only the set DISCOVERY found. A chart discovery misses --
a directory name on its prune list, a layout it does not recognise -- therefore fails
nothing: the set it asserts is the set it built. This reads the other side. The expected
set comes from charts/*/Chart.yaml in the checkout, which does not go through discovery,
and the observed set from the `release-oci-charts` artifact the run uploads in BOTH modes
(a rehearsal and a release), so the check also runs on a dry run.

Usage:  verify-release-set.py --dist <dir of .tgz> [--repo-root <dir>]
        verify-release-set.py --self-test

Exit 0  every expected (name, version) is in the artifact and nothing else is
     1  FAIL: something expected is missing, or something unexpected was built
     2  VOID: no expected chart could be established, so nothing was measured

THE VERDICT IS DERIVED FROM WHAT WAS FOUND. `missing` is seeded with EVERYTHING expected
and an entry leaves it only when a package that actually reads as that chart is seen. The
opposite seeding -- empty, filled from what is absent -- passes an empty artifact having
looked at nothing, which is the hole in the publisher's own availability loop
(krateo-blueprints/charts#53). `--self-test` runs the seeding the wrong way round and
requires the empty-artifact case to then pass, which is how this file shows it can tell.
"""
import glob
import os
import re
import sys
import tarfile
import tempfile

_NAME = re.compile(r"^name:\s*(\S+)\s*$", re.M)
_VERSION = re.compile(r"^version:\s*(\S+)\s*$", re.M)


def _scalar(rx, text):
    m = rx.search(text)
    return m.group(1).strip("'\"") if m else None


def expected_set(repo_root):
    """{(name, version)} from charts/*/Chart.yaml, independent of release-oci's discovery."""
    out = set()
    for p in sorted(glob.glob(os.path.join(repo_root, "charts", "*", "Chart.yaml"))):
        text = open(p, encoding="utf-8").read()
        name, version = _scalar(_NAME, text), _scalar(_VERSION, text)
        if name and version:
            out.add((name, version))
    return out


def observed_set(dist):
    """({(name, version)}, [unreadable file names]) from the packages' OWN Chart.yaml.

    The file name is not trusted: a package is what its Chart.yaml says it is.
    """
    found, bad = set(), []
    for tgz in sorted(glob.glob(os.path.join(dist, "*.tgz"))):
        try:
            with tarfile.open(tgz, "r:gz") as tf:
                names = [m.name for m in tf.getmembers()
                         if re.fullmatch(r"[^/]+/Chart\.yaml", m.name)]
                if len(names) != 1:
                    raise ValueError("%d top-level Chart.yaml" % len(names))
                text = tf.extractfile(names[0]).read().decode("utf-8")
            name, version = _scalar(_NAME, text), _scalar(_VERSION, text)
            if not (name and version):
                raise ValueError("no name/version")
            found.add((name, version))
        except Exception:  # noqa: BLE001 -- any unreadable package is a finding
            bad.append(os.path.basename(tgz))
    return found, bad


def verdict(expected, found, bad, seed_with_everything=True):
    """(exit code, [messages])."""
    if not expected:
        return 2, ["VOID -- no chart under charts/*/Chart.yaml could be read, so there is "
                   "nothing to compare the build against. This is NOT a pass."]
    missing = set(expected) if seed_with_everything else set()
    if seed_with_everything:
        missing -= found
    else:
        # the deliberately WRONG seeding, used only by the self-test's mutation arm
        missing = {e for e in expected if e not in found} if found else set()
    unexpected = found - set(expected)
    msgs = []
    for n, v in sorted(missing):
        msgs.append("MISSING %s %s: charts/ holds it and release-oci did not build it "
                    "(discovery missed it, or it was skipped)" % (n, v))
    for n, v in sorted(unexpected):
        msgs.append("UNEXPECTED %s %s: built by release-oci but not a chart under charts/" % (n, v))
    for f in bad:
        msgs.append("UNREADABLE %s: not a package with exactly one top-level Chart.yaml" % f)
    return (1 if msgs else 0), msgs


def run(dist, repo_root):
    expected = expected_set(repo_root)
    found, bad = observed_set(dist) if os.path.isdir(dist) else (set(), [])
    code, msgs = verdict(expected, found, bad)
    print("expected %d chart(s): %s" % (len(expected), ", ".join("%s %s" % e for e in sorted(expected)) or "none"))
    print("built    %d chart(s): %s" % (len(found), ", ".join("%s %s" % e for e in sorted(found)) or "none"))
    for m in msgs:
        print(("::error::" if code == 1 else "") + m)
    if code == 0:
        print("PASS -- the build is exactly the %d chart(s) under charts/." % len(expected))
    elif code == 1:
        print("FAIL -- the release would not publish what this repository holds.")
    return code


# ----------------------------------------------------------------- self-test
def _pkg(dist, name, version, chart_yaml=None):
    """Write a real .tgz holding <name>/Chart.yaml."""
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, name))
    with open(os.path.join(d, name, "Chart.yaml"), "w") as fh:
        fh.write(chart_yaml if chart_yaml is not None
                 else "apiVersion: v2\nname: %s\nversion: %s\n" % (name, version))
    with tarfile.open(os.path.join(dist, "%s-%s.tgz" % (name, version)), "w:gz") as tf:
        tf.add(os.path.join(d, name), arcname=name)


def _repo(root, charts):
    for name, version in charts:
        os.makedirs(os.path.join(root, "charts", name), exist_ok=True)
        with open(os.path.join(root, "charts", name, "Chart.yaml"), "w") as fh:
            fh.write("apiVersion: v2\nname: %s\nversion: %s\n" % (name, version))


def selftest():
    import contextlib
    import io
    import shutil
    two = [("baremetal-host", "0.7.0"), ("kubernetes-cluster", "0.16.1")]
    cases = [
        ("exactly the two charts pass", two, two, 0),
        ("an EMPTY artifact FAILS (the vacuous-pass case)", two, [], 1),
        ("one chart missing FAILS", two, two[:1], 1),
        ("a version that differs FAILS (missing AND unexpected)",
         two, [two[0], ("kubernetes-cluster", "0.16.0")], 1),
        ("an extra chart FAILS", two, two + [("probe", "0.0.1")], 1),
        ("no chart under charts/ is VOID, not a pass", [], two, 2),
        ("no chart under charts/ AND an empty artifact is still VOID", [], [], 2),
    ]
    failures = []
    for label, exp, built, want in cases:
        root, dist = tempfile.mkdtemp(), tempfile.mkdtemp()
        try:
            _repo(root, exp)
            for n, v in built:
                _pkg(dist, n, v)
            with contextlib.redirect_stdout(io.StringIO()):
                got = run(dist, root)
            if got != want:
                failures.append("%s: wanted exit %d, got %d" % (label, want, got))
        finally:
            shutil.rmtree(root, ignore_errors=True)
            shutil.rmtree(dist, ignore_errors=True)

    # the file name is not trusted: a package named like chart A holding chart B's
    # Chart.yaml reads as B, so a renamed file cannot satisfy the expectation
    root, dist = tempfile.mkdtemp(), tempfile.mkdtemp()
    try:
        _repo(root, two)
        _pkg(dist, "baremetal-host", "0.7.0")
        _pkg(dist, "kubernetes-cluster", "0.16.1", chart_yaml="apiVersion: v2\nname: other\nversion: 1.0.0\n")
        with contextlib.redirect_stdout(io.StringIO()):
            got = run(dist, root)
        if got != 1:
            failures.append("a package whose Chart.yaml disagrees with its file name: wanted 1, got %d" % got)
        # a corrupt package is reported, not skipped
        with open(os.path.join(dist, "junk-1.0.0.tgz"), "wb") as fh:
            fh.write(b"not a tarball")
        _, bad = observed_set(dist)
        if bad != ["junk-1.0.0.tgz"]:
            failures.append("a corrupt .tgz was not reported as unreadable: %r" % bad)
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(dist, ignore_errors=True)

    # MUTATION: the wrong seeding (empty, filled from what is absent) must make the
    # empty-artifact case PASS. If it does not, the empty-artifact arm above was not
    # what was keeping the verdict honest and proves nothing.
    two_set = set(two)
    code, _ = verdict(two_set, set(), [], seed_with_everything=False)
    if code != 0:
        failures.append("mutation: seeding `missing` from absence should let an empty artifact "
                        "pass, got exit %d -- the empty-artifact arm does not discriminate" % code)
    code, _ = verdict(two_set, set(), [], seed_with_everything=True)
    if code != 1:
        failures.append("the real seeding must fail an empty artifact, got exit %d" % code)

    n = len(cases) + 4
    print("  self-test: %d cases, %d failure(s)" % (n, len(failures)))
    for f in failures:
        print("   ", f)
    return 1 if failures else 0


_SELFTEST = ("--self-test", "--selftest")
_OPTIONS = ("--dist", "--repo-root")

if __name__ == "__main__":
    argv = sys.argv[1:]
    # MODE FLAGS: both spellings accepted, an unrecognised `--`-prefixed first argument
    # REFUSED (exit 64), so a mistyped `--self-tet` is not read as a caller error or run
    # as the real check. scripts/check-screen-flags.py classifies this shape.
    if argv and argv[0].startswith("--") and argv[0] not in _SELFTEST + _OPTIONS:
        sys.stderr.write("refusing unrecognised first argument: %s\n" % argv[0])
        sys.exit(64)
    if argv and set(argv) <= set(_SELFTEST):
        sys.exit(selftest())
    dist = root = None
    rest = list(argv)
    ok = True
    while rest:
        a = rest.pop(0)
        if a in _OPTIONS and rest:
            v = rest.pop(0)
            if a == "--dist":
                dist = v
            else:
                root = v
        else:
            sys.stderr.write("refusing unrecognised argument: %s\n" % a)
            ok = False
            break
    if not ok or dist is None:
        sys.stderr.write("usage: %s [%s] | --dist <dir> [--repo-root <dir>]\n"
                         % (sys.argv[0], "|".join(_SELFTEST)))
        sys.exit(64)
    sys.exit(run(dist, root or os.getcwd()))
