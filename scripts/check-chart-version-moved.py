#!/usr/bin/env python3
"""A published chart whose CONTENT moved must also move its VERSION.

WHY THIS EXISTS. `helm push` to an OCI registry at a version that already exists
OVERWRITES it. A tag re-pushes every published chart at whatever version its
Chart.yaml currently says, so a chart whose content changed while its version did
not silently replaces bytes that consumers already hold -- and a consumer who
pinned that version gets different bytes than they pinned, with no signal anywhere.

docs/release.md already records one instance reached from the other direction
(`kubernetes-cluster` 0.15.2, left immutable rather than overwritten, for exactly
this reason). This screen is the preventive half.

MEASURED 2026-11-13, by hand, preparing 0.3.9: TWO of four published charts had
content changes since 0.3.8 at an unchanged version -- `baremetal-discovery`
(0.1.2) and `baremetal-lifecycle` (0.3.3). Both had gained a
`krateo.io/publish` annotation. The change is semantically inert; the packaged
bytes are not. Nothing in `make check` would have caught it, and the tag was
one command away.

The comparison is against the newest TAG, not against a branch point: the tag is
what was published, and that is the only thing a consumer can be holding.
"""
import glob
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

PUBLISH_ANNOTATION = "krateo.io/publish"


class Void(Exception):
    """The screen could not form an opinion. NOT a pass."""


def sh(*args):
    r = subprocess.run(args, capture_output=True, text=True)
    return r.stdout.strip(), r.returncode


def newest_tag():
    """The newest tag that is NOT the commit being checked.

    EXCLUDING HEAD'S OWN TAGS IS THE WHOLE POINT, and this screen shipped without it.
    Measured in the 0.3.9 release run, which is the only place `make check` runs in
    CI (release-chart.yaml, `on: push: tags`):

        checked 4 published chart(s) against tag 0.3.9: ...
          baremetal-discovery      0.1.3 -> 0.1.3

    It compared tag 0.3.9 against the tree AT tag 0.3.9 -- the tag against itself. No
    chart can show movement against its own commit, so every one passed trivially and
    the screen that exists to guard releases was vacuous in the only place it ran.

    Locally the bug is invisible: HEAD is normally past the newest tag, so the
    comparison is real. It appears exactly when HEAD *is* the tag, which is the
    release. A screen whose correctness depends on not being at the moment it guards
    is not a screen.
    """
    out, rc = sh("git", "for-each-ref", "--sort=-creatordate",
                 "--format=%(refname:short)", "refs/tags")
    if rc != 0:
        raise Void("git could not list tags, so there is nothing to compare against")
    tags = [t for t in out.split("\n") if t.strip()]
    if not tags:
        raise Void("this repository has no tags, so no published bytes exist to protect")

    head, rc = sh("git", "rev-parse", "HEAD")
    if rc != 0:
        raise Void("git could not resolve HEAD, so 'the tag before this commit' is unknown")

    def sha_of(t):
        out, rc = sh("git", "rev-list", "-n", "1", t)
        return out if rc == 0 else None

    return pick_tag(tags, sha_of, head)


def pick_tag(tags, sha_of, head):
    """The newest tag not pointing at `head`. Pure, so the self-test can drive it."""
    usable = [t for t in tags if sha_of(t) not in (None, head)]
    if not usable:
        raise Void("every tag in this repository points at HEAD (%s), so there is no "
                   "EARLIER release to compare against. A shallow CI checkout that "
                   "fetched only the tag being built looks exactly like this -- and "
                   "comparing a tag with itself would pass every chart trivially"
                   % head[:12])
    return usable[0]


def package_members(chart_dir):
    """What `helm package` would PUBLISH for this chart: {member path: bytes}.

    HELM decides the membership, not this script. A published chart is the PACKAGE, and
    `.helmignore` removes files from it (`ci/` values, chart-local docs, the ignore file
    itself). Comparing the raw directory flagged a change confined to ignored files as
    "content moved" and demanded a version bump for bytes nobody can hold -- the false
    positive that blocked #149. `.helmignore` glob semantics (negation, anchoring,
    directory matching) are deliberately NOT reimplemented: a filter slightly too greedy
    would silently stop reporting real changes.

    Both sides are packaged with the same helm, and only paths and contents are compared,
    which is what upstream chart-version-guard compares. Raises Void when it cannot
    measure -- "could not compare" must never read as "nothing moved".
    """
    if shutil.which("helm") is None:
        raise Void("helm is not installed, so the packaged contents cannot be compared")
    out = tempfile.mkdtemp(prefix="pkg-")
    try:
        r = subprocess.run(["helm", "package", chart_dir, "-d", out],
                           capture_output=True, text=True)
        tgzs = glob.glob(os.path.join(out, "*.tgz"))
        if r.returncode != 0 or len(tgzs) != 1:
            raise Void("helm package failed for %s: %s"
                       % (chart_dir, (r.stderr.strip().splitlines() or ["no output"])[-1]))
        members = {}
        with tarfile.open(tgzs[0]) as t:
            for m in t.getmembers():
                if m.isfile():
                    members[m.name] = t.extractfile(m).read()
        return members
    finally:
        shutil.rmtree(out, ignore_errors=True)


def package_diff(old_dir, new_dir):
    """Member paths whose presence or contents differ between two chart directories."""
    a, b = package_members(old_dir), package_members(new_dir)
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))


def chart_at_tag(tag, chart, dest):
    """Extract charts/<chart> as of `tag` into dest -> its directory, or None if absent."""
    r = subprocess.run(["git", "archive", tag, "--", "charts/%s" % chart], capture_output=True)
    if r.returncode != 0:
        return None
    with tarfile.open(fileobj=io.BytesIO(r.stdout)) as t:
        t.extractall(dest, filter="data")
    return os.path.join(dest, "charts", chart)


def chart_version(text):
    for line in (text or "").split("\n"):
        if line.startswith("version:"):
            return line.split(":", 1)[1].strip()
    return None


def is_published(text):
    """Does this chart declare itself published?

    Read from the chart's OWN annotation. (Until 2026-11-28 the alternative was the
    release workflow's hand-written chart list, and scripts/check-publish-set.py
    asserted the two agreed; that list and that screen are gone with the move to
    release-oci, so the annotation is now the only declaration this screen reads.)
    """
    if not text:
        return False
    return re.search(r'["\']?%s["\']?\s*:\s*["\']?true["\']?' % re.escape(PUBLISH_ANNOTATION),
                     text) is not None


def audit(charts, moved_fn, old_version_fn, new_version_fn):
    """Pure core, so the self-test can drive it without a git history.

    Returns (findings, checked). A finding is (chart, old, new, message).
    """
    findings, checked = [], []
    for c in sorted(charts):
        old_v = old_version_fn(c)
        new_v = new_version_fn(c)
        if new_v is None:
            findings.append((c, old_v, new_v,
                             "Chart.yaml declares no `version:` -- helm cannot package it, "
                             "and this screen cannot tell whether it moved"))
            continue
        moved = moved_fn(c)
        checked.append(c)
        if old_v is None:
            # New since the tag. Nothing published to overwrite.
            continue
        if moved and old_v == new_v:
            findings.append((c, old_v, new_v,
                             "content changed since the tag but `version:` did NOT. A release "
                             "re-pushes this chart at %s, OVERWRITING bytes consumers already "
                             "hold at that version. Bump the version" % new_v))
    return findings, checked


def run():
    tag = newest_tag()
    charts = []
    for path in sorted(glob.glob("charts/*/Chart.yaml")):
        text = open(path).read()
        if is_published(text):
            charts.append(os.path.basename(os.path.dirname(path)))
    if not charts:
        raise Void("no chart declares `%s: true`, so this screen compared nothing. "
                   "That is not the same as every chart being safe" % PUBLISH_ANNOTATION)

    def moved(c):
        # THE PACKAGE AT THE TAG vs THE PACKAGE FROM THE WORKING TREE (see
        # package_members: what is published is the package, not the directory).
        #
        # TAG vs THE WORKING TREE, not tag..HEAD.
        #
        # This read `tag HEAD` until 2026-11-13 and was INCONSISTENT with itself: it
        # took "has the content moved" from COMMITTED state while taking the new
        # version from the working tree two lines below. So an uncommitted Chart.yaml
        # edit was invisible, and `make check` -- which you run BEFORE committing,
        # which is the whole point -- passed on exactly the change it exists to catch.
        #
        # Found by using it: an uncommitted one-line edit to baremetal-lifecycle's
        # description went unreported while a committed edit to baremetal-discovery
        # was flagged, in the same run. Omitting the second ref diffs the tag against
        # the working tree, which is the state a human is about to commit and tag.
        tmp = tempfile.mkdtemp(prefix="at-tag-")
        try:
            old_dir = chart_at_tag(tag, c, tmp)
            if old_dir is None:
                return False   # absent at the tag: audit() treats that as new
            return bool(package_diff(old_dir, "charts/%s" % c))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def old_v(c):
        out, rc = sh("git", "show", "%s:charts/%s/Chart.yaml" % (tag, c))
        return chart_version(out) if rc == 0 else None

    def new_v(c):
        return chart_version(open("charts/%s/Chart.yaml" % c).read())

    findings, checked = audit(charts, moved, old_v, new_v)

    print("checked %d published chart(s) against tag %s: %s"
          % (len(checked), tag, ", ".join(checked)))
    for c in checked:
        print("  %-24s %s -> %s%s"
              % (c, old_v(c) or "(new)", new_v(c), "   content moved" if moved(c) else ""))
    if findings:
        print("\nACTIONABLE (%d):" % len(findings))
        for c, o, n, m in findings:
            print("  %-24s %s" % (c, m))
        print("\nFAIL -- tagging now would overwrite published bytes.")
        return 1
    print("\nPASS -- every published chart whose content moved also moved its version.")
    return 0


def selftest():
    cases, failures = [], []

    def case(name, ok):
        cases.append((name, ok))
        if not ok:
            failures.append(name)

    V = {"a": "1.0.0", "b": "2.0.0", "c": "3.0.0"}

    # the defect this screen exists for
    f, ch = audit(["a"], lambda c: True, lambda c: "1.0.0", lambda c: "1.0.0")
    case("content moved, version did NOT -> FINDING",
         len(f) == 1 and "OVERWRITING" in f[0][3] and ch == ["a"])

    # the same chart, bumped
    f, ch = audit(["a"], lambda c: True, lambda c: "1.0.0", lambda c: "1.0.1")
    case("content moved AND version moved -> clean", len(f) == 0 and ch == ["a"])

    # untouched chart at an unchanged version is NOT a finding -- the common case,
    # and a screen that flagged it would be unusable
    f, _ = audit(["a"], lambda c: False, lambda c: "1.0.0", lambda c: "1.0.0")
    case("content UNCHANGED at the same version -> clean", len(f) == 0)

    # a chart that did not exist at the tag has nothing to overwrite
    f, _ = audit(["new"], lambda c: True, lambda c: None, lambda c: "0.1.0")
    case("new chart since the tag -> clean (nothing published to overwrite)", len(f) == 0)

    # a Chart.yaml with no version at all is a finding, not a silent skip
    f, ch = audit(["a"], lambda c: True, lambda c: "1.0.0", lambda c: None)
    case("no `version:` key -> FINDING, and NOT counted as checked",
         len(f) == 1 and "declares no" in f[0][3] and ch == [])

    # several charts, only the offender reported
    f, ch = audit(["a", "b", "c"],
                  lambda c: c in ("a", "b"),
                  lambda c: V[c],
                  lambda c: {"a": "1.0.0", "b": "2.0.1", "c": "3.0.0"}[c])
    case("mixed set: only the unbumped-but-moved chart is reported",
         len(f) == 1 and f[0][0] == "a" and set(ch) == {"a", "b", "c"})

    # VOID arms -- an absence must not read as a pass
    try:
        chart_version("")
        case("chart_version('') returns None rather than raising", chart_version("") is None)
    except Exception:
        case("chart_version('') returns None rather than raising", False)

    case("is_published sees a true annotation",
         is_published('annotations:\n  "krateo.io/publish": "true"\n'))
    case("is_published rejects a false annotation",
         not is_published('annotations:\n  "krateo.io/publish": "false"\n'))
    case("is_published rejects a chart with no annotation",
         not is_published("name: x\nversion: 1.0.0\n"))

    # THE TAG MUST NOT BE HEAD'S OWN. This screen shipped without that check and was
    # VACUOUS in the only place it runs: release-chart.yaml fires `on: push: tags`, so
    # at 0.3.9 it compared tag 0.3.9 against the tree AT 0.3.9 and printed
    #   baremetal-discovery  0.1.3 -> 0.1.3
    # for every chart, with no "content moved" anywhere. Locally the bug is invisible,
    # because HEAD is normally past the newest tag.
    SHAS = {"0.3.9": "aaa", "0.3.8": "bbb", "0.3.7": "ccc"}
    case("at a tagged HEAD, the tag itself is SKIPPED",
         pick_tag(["0.3.9", "0.3.8", "0.3.7"], SHAS.get, "aaa") == "0.3.8")
    case("off a tag, the newest tag is used",
         pick_tag(["0.3.9", "0.3.8"], SHAS.get, "zzz") == "0.3.9")
    case("several tags on HEAD are all skipped",
         pick_tag(["0.3.9", "0.3.8", "0.3.7"], {"0.3.9": "aaa", "0.3.8": "aaa",
                                                "0.3.7": "ccc"}.get, "aaa") == "0.3.7")
    try:
        pick_tag(["0.3.9"], SHAS.get, "aaa")
        case("VOID when the ONLY tag is HEAD's (the shallow-CI shape)", False)
    except Void:
        case("VOID when the ONLY tag is HEAD's (the shallow-CI shape)", True)

    # THE DISCRIMINATOR. Every arm above that expects "clean" would also pass if
    # audit() simply returned no findings ever. This one must come back non-empty,
    # and the file's whole value rests on it.
    f, _ = audit(["x"], lambda c: True, lambda c: "9.9.9", lambda c: "9.9.9")
    case("DISCRIMINATOR: audit CAN return a finding", len(f) == 1)

    # ---- the PACKAGE comparison, through real helm on fixtures ----
    def fixture(root, name, files):
        d = os.path.join(root, name)
        for rel, body in files.items():
            os.makedirs(os.path.dirname(os.path.join(d, rel)), exist_ok=True)
            with open(os.path.join(d, rel), "w") as fh:
                fh.write(body)
        return d

    BASE = {"Chart.yaml": "apiVersion: v2\nname: c\nversion: 1.0.0\n",
            "values.yaml": "a: 1\n",
            "templates/t.yaml": "kind: ConfigMap\napiVersion: v1\nmetadata:\n  name: x\n",
            ".helmignore": "ci/\n.helmignore\n"}
    tmp = tempfile.mkdtemp(prefix="vm-selftest-")
    try:
        def pair(tag_files, tree_files):
            return package_diff(fixture(tmp, "old%d" % len(os.listdir(tmp)), tag_files),
                                fixture(tmp, "new%d" % len(os.listdir(tmp)), tree_files))

        def with_(extra, drop=()):
            d = dict(BASE); d.update(extra)
            for k in drop:
                d.pop(k)
            return d

        case("PACKAGE: identical trees -> nothing moved", pair(BASE, BASE) == [])
        # NEGATIVE CONTROL: if either of these stops firing the fix has disabled the screen.
        case("NEGATIVE CONTROL: a templates/ change at an unchanged version -> MOVED",
             pair(BASE, with_({"templates/t.yaml": "kind: Secret\n"})) == ["c/templates/t.yaml"])
        case("NEGATIVE CONTROL: a values.yaml change -> MOVED",
             pair(BASE, with_({"values.yaml": "a: 2\n"})) == ["c/values.yaml"])
        case("NEGATIVE CONTROL: a NEW published file -> MOVED",
             pair(BASE, with_({"templates/u.yaml": "kind: Secret\n"})) == ["c/templates/u.yaml"])
        case("a change confined to ignored paths (ci/) -> NOT moved",
             pair(BASE, with_({"ci/render-values.yaml": "a: 9\n"})) == [])
        case("a change to ignored .helmignore-listed docs only -> NOT moved",
             pair(with_({"ci/v.yaml": "a: 1\n"}), with_({"ci/v.yaml": "a: 2\n"})) == [])
        # THE THIRD CASE: editing .helmignore so a previously-ignored file ships changes
        # the package, and must be reported even though the file existed before.
        old = with_({"notes.txt": "hello\n"}, ())
        old[".helmignore"] = "ci/\n.helmignore\nnotes.txt\n"
        new = dict(old); new[".helmignore"] = "ci/\n.helmignore\n"
        case("a .helmignore edit that makes an ignored file PUBLISHED -> MOVED",
             pair(old, new) == ["c/notes.txt"])
        # And the same: dropping `.helmignore` from its own list ships the ignore file.
        new2 = dict(BASE); new2[".helmignore"] = "ci/\n"
        case("an edit that makes .helmignore itself published -> MOVED",
             pair(BASE, new2) == ["c/.helmignore"])
        try:
            package_members(os.path.join(tmp, "does-not-exist"))
            case("a chart that cannot be packaged -> VOID", False)
        except Void:
            case("a chart that cannot be packaged -> VOID", True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    for n, ok in cases:
        print("  %-62s %s" % (n, "ok" if ok else "FAIL"))
    print("selftest: %d cases, %d failure(s)" % (len(cases), len(failures)))
    for f_ in failures:
        print("  failed: %s" % f_)
    return 1 if failures else 0


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
    try:
        if _WANT_SELFTEST:
            sys.exit(selftest())
        sys.exit(run())
    except Void as v:
        print("VOID: %s" % v)
        print("NOT a pass.")
        sys.exit(2)
