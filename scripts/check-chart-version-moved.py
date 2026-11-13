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
import re
import subprocess
import sys
import glob
import os

PUBLISH_ANNOTATION = "krateo.io/publish"


class Void(Exception):
    """The screen could not form an opinion. NOT a pass."""


def sh(*args):
    r = subprocess.run(args, capture_output=True, text=True)
    return r.stdout.strip(), r.returncode


def newest_tag():
    out, rc = sh("git", "for-each-ref", "--sort=-creatordate",
                 "--format=%(refname:short)", "refs/tags")
    if rc != 0:
        raise Void("git could not list tags, so there is nothing to compare against")
    tags = [t for t in out.split("\n") if t.strip()]
    if not tags:
        raise Void("this repository has no tags, so no published bytes exist to protect")
    return tags[0]


def chart_version(text):
    for line in (text or "").split("\n"):
        if line.startswith("version:"):
            return line.split(":", 1)[1].strip()
    return None


def is_published(text):
    """Does this chart declare itself published?

    Read from the chart's OWN annotation rather than from the workflow's list,
    because scripts/check-publish-set.py already asserts the two agree. Two
    screens reading the same fact from two places would disagree the moment that
    other screen is the one that is broken.
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
        out, rc = sh("git", "diff", "--name-only", tag, "HEAD", "--", "charts/%s" % c)
        if rc != 0:
            raise Void("git diff failed for charts/%s against %s" % (c, tag))
        return bool(out)

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

    # THE DISCRIMINATOR. Every arm above that expects "clean" would also pass if
    # audit() simply returned no findings ever. This one must come back non-empty,
    # and the file's whole value rests on it.
    f, _ = audit(["x"], lambda c: True, lambda c: "9.9.9", lambda c: "9.9.9")
    case("DISCRIMINATOR: audit CAN return a finding", len(f) == 1)

    for n, ok in cases:
        print("  %-62s %s" % (n, "ok" if ok else "FAIL"))
    print("selftest: %d cases, %d failure(s)" % (len(cases), len(failures)))
    for f_ in failures:
        print("  failed: %s" % f_)
    return 1 if failures else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    try:
        sys.exit(run())
    except Void as v:
        print("VOID: %s" % v)
        print("NOT a pass.")
        sys.exit(2)
