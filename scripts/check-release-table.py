#!/usr/bin/env python3
"""docs/release.md's version table must agree with charts/*/Chart.yaml.

WHY THIS EXISTS. The table went stale FOUR times. Twice it was corrected by hand the
same day it drifted; the fourth time it drifted inside the very pull request whose
commit message said it had been updated -- a rebase dropped the hunk and nothing
noticed, because a markdown table is not code and no screen read it.

WHY IT IS CHECKABLE ONLY NOW. The table was headed "Current published versions", which
is a claim about the REGISTRY. Charts here are bumped when their content changes, which
happens BEFORE the tag, so between a bump and a release the table was legitimately
ahead of what was published -- and a screen enforcing "table == Chart.yaml" would have
contradicted its own heading, while one enforcing "table == newest tag" would have been
red for most of the repo's life. Neither is a usable gate.

Retitled to "Versions the next tag will publish", the table states what is in the
working tree, which is exactly what a tag pushed from this commit would publish. That
makes it a mechanical fact with a single source, and this screen enforces it.

VOID IS NOT A PASS. A missing table, an unparsable one, or a chart set that does not
line up is reported as VOID -- the screen formed no opinion -- rather than silently
passing on nothing. An empty table that matched an empty chart set would otherwise be
the quietest possible success.
"""
import glob
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC = os.path.join(REPO, "docs", "release.md")
HEADING = "Versions the next tag will publish"
PUBLISH_ANNOTATION = "krateo.io/publish"

ROW = re.compile(r"^\|\s*`([a-z0-9-]+)`\s*\|\s*`([0-9][^`]*)`\s*\|\s*$", re.M)


class Void(Exception):
    """The screen could not form an opinion. NOT a pass."""


def table_versions(text):
    """chart -> version, read from the table under HEADING."""
    i = text.find("## " + HEADING)
    if i < 0:
        raise Void("docs/release.md has no '## %s' section, so there is no table to "
                   "check. If it was renamed, this screen is now checking nothing"
                   % HEADING)
    # stop at the next heading so a later table cannot be mistaken for this one
    j = text.find("\n## ", i + 1)
    body = text[i: j if j > 0 else len(text)]
    rows = ROW.findall(body)
    if not rows:
        raise Void("the '%s' table parsed to ZERO rows. An empty table trivially "
                   "agrees with everything" % HEADING)
    out = {}
    for name, ver in rows:
        if name in out:
            raise Void("chart %r appears twice in the table, so it cannot be compared "
                       "against one version" % name)
        out[name] = ver.strip()
    return out


def chart_versions():
    """chart -> version, for charts that declare themselves published."""
    out = {}
    for path in sorted(glob.glob(os.path.join(REPO, "charts/*/Chart.yaml"))):
        text = open(path).read()
        published = re.search(r'["\']?%s["\']?\s*:\s*["\']?true["\']?'
                              % re.escape(PUBLISH_ANNOTATION), text)
        if not published:
            continue
        ver = None
        for line in text.split("\n"):
            if line.startswith("version:"):
                ver = line.split(":", 1)[1].strip()
                break
        name = os.path.basename(os.path.dirname(path))
        if ver is None:
            raise Void("charts/%s/Chart.yaml declares no `version:`, so the table "
                       "cannot be compared against it" % name)
        out[name] = ver
    if not out:
        raise Void("no chart declares `%s: true`, so this screen compared the table "
                   "against an empty set" % PUBLISH_ANNOTATION)
    return out


def compare(table, charts):
    """Return a list of (chart, table_version, chart_version, message)."""
    findings = []
    for name in sorted(set(table) | set(charts)):
        t, c = table.get(name), charts.get(name)
        if t is None:
            findings.append((name, "-", c,
                             "published chart is MISSING from the table, so a reader "
                             "cannot see what the next tag will publish for it"))
        elif c is None:
            findings.append((name, t, "-",
                             "table lists a chart that is not a published chart here. "
                             "Either it was unpublished and the row outlived it, or the "
                             "name is wrong"))
        elif t != c:
            findings.append((name, t, c,
                             "table says %s, Chart.yaml says %s. The chart is the "
                             "source of truth" % (t, c)))
    return findings


def run():
    try:
        text = open(DOC).read()
    except OSError as e:
        raise Void("cannot read docs/release.md (%s)" % e)
    table, charts = table_versions(text), chart_versions()
    findings = compare(table, charts)

    print("checked %d table row(s) against %d published chart(s)"
          % (len(table), len(charts)))
    for name in sorted(charts):
        print("  %-24s table=%-10s chart=%s"
              % (name, table.get(name, "(missing)"), charts[name]))
    if findings:
        print("\nACTIONABLE (%d):" % len(findings))
        for name, t, c, msg in findings:
            print("  %-24s %s" % (name, msg))
        print("\nFAIL -- docs/release.md disagrees with the charts.")
        return 1
    print("\nPASS -- the table matches every published chart.")
    return 0


def selftest():
    cases, failures = [], []

    def case(n, ok):
        cases.append((n, ok))
        if not ok:
            failures.append(n)

    doc = ("# x\n\n## " + HEADING + "\n\nblurb\n\n| chart | version |\n|---|---|\n"
           "| `a` | `1.0.0` |\n| `b` | `2.0.0` |\n\n## Something else\n\n"
           "| `c` | `9.9.9` |\n")
    t = table_versions(doc)
    case("reads the rows under the heading", t == {"a": "1.0.0", "b": "2.0.0"})
    case("a table under a LATER heading is not absorbed", "c" not in t)

    case("agreement -> no findings", compare({"a": "1.0.0"}, {"a": "1.0.0"}) == [])

    f = compare({"a": "1.0.0"}, {"a": "1.0.1"})
    case("a version mismatch -> FINDING",
         len(f) == 1 and "source of truth" in f[0][3])

    f = compare({}, {"a": "1.0.0"})
    case("a published chart missing from the table -> FINDING",
         len(f) == 1 and "MISSING from the table" in f[0][3])

    f = compare({"ghost": "1.0.0"}, {"a": "1.0.0"})
    case("a table row for no published chart -> FINDING", len(f) == 2)

    # VOID arms: an absence must not read as a pass
    for name, bad in (("no heading", "# x\n\nnothing here\n"),
                      ("heading but no rows", "## " + HEADING + "\n\nprose only\n")):
        try:
            table_versions(bad)
            case("VOID: %s" % name, False)
        except Void:
            case("VOID: %s" % name, True)
    try:
        table_versions("## " + HEADING + "\n\n| `a` | `1.0.0` |\n| `a` | `2.0.0` |\n")
        case("VOID: a chart listed twice", False)
    except Void:
        case("VOID: a chart listed twice", True)

    # DISCRIMINATOR: compare must be able to return a finding at all. Three arms above
    # expect "clean"; without this they would pass against a compare() that never
    # reports anything.
    case("DISCRIMINATOR: compare CAN return a finding",
         len(compare({"a": "1.0.0"}, {"a": "9.9.9"})) == 1)

    for n, ok in cases:
        print("  %-58s %s" % (n, "ok" if ok else "FAIL"))
    print("selftest: %d cases, %d failure(s)" % (len(cases), len(failures)))
    for x in failures:
        print("  failed: %s" % x)
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
