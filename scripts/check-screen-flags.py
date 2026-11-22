#!/usr/bin/env python3
"""Every screen's mode flag must accept both spellings and REFUSE anything else.

THE DEFECT THIS PREVENTS, and it is measured rather than imagined. This repo spelled
the self-test flag `--selftest` on 7 screens and `--self-test` on 11. The screens that
tested it with a bare `"--flag" in sys.argv` SILENTLY RAN THE OTHER MODE on a typo and
exited 0:

    drift screen, wrong spelling   -> exit 0, having run its report
    shape screen, wrong spelling   -> exit 2, argparse REFUSED it

That cost two false conclusions in one session on 2026-11-21: a screen was reported as
"not running its controls" when the flag was merely misspelt, and a second was read as
failing when argparse had refused the argument. Neither was a defect in the screen.

WHY THIS IS A SOURCE CHECK AND NOT A RUN CHECK. `make check` already runs all fourteen
self-tests; shelling out to run them again here would double the slowest part of the
suite to re-learn what it already knows. What it does NOT know is whether a NEW screen
follows the convention, which is a property of the source. So this reads the files.

Two live arms are kept, one per mechanism, because a source check proves the text and
not the behaviour -- and the whole lesson above is that those differ.

THOSE LIVE ARMS WERE VACUOUS UNTIL 2026-11-22. They accepted a screen if its output
matched `self.?test|assertion|\d+ cases|^ *ok +` under both spellings. The REFUSAL path
prints `usage: ... [--self-test|--selftest]`, which matches that pattern -- so a screen
that refused one spelling satisfied the arm whose entire purpose is to catch exactly
that. Measured with a one-spelling screen as a positive control: the arm passed it. The
arms now require a positive, EQUAL, REPORTED assertion count under both spellings, which
only a self-test that ran can produce, and that one-spelling screen is kept as a
permanent synthetic arm -- the real instance was fixed in #137/#138, so without the
synthetic one nothing here has to fail.

THREE OUTCOMES. A screen whose mechanism cannot be identified at all is VOID, not a
pass: an unrecognised shape is exactly where the next instance would hide.
"""
import glob
import os
import re
import subprocess
import sys
import tempfile

BOTH = ("--self-test", "--selftest")

# What a self-test REPORTS about itself. Two spellings exist in this repo:
# `SELF-TEST: 11/11 assertions passed` and `selftest: 57 cases, 0 failure(s)`.
_COUNT = re.compile(r"(\d+)\s*/\s*\d+\s+assertions passed|(\d+)\s+cases", re.I)


def selftest_signature(path, flag):
    """(exit code, assertions reported) for one screen under one spelling.

    NOT a regex over free text. The refusal path prints a usage line naming BOTH
    spellings, so a pattern like `self.?test` matches a screen that REFUSED the flag --
    which is how this arm's predecessor passed a screen accepting only `--self-test`.
    A positive, reported count is something only a self-test that ran can produce.
    """
    r = subprocess.run([sys.executable, "-I", path, flag], capture_output=True, text=True)
    m = _COUNT.search(r.stdout + r.stderr)
    if not m:
        return r.returncode, None
    return r.returncode, int(next(g for g in m.groups() if g is not None))


def live_arm(path):
    """Run one PYTHON screen under both spellings plus a bogus flag -> (ok, detail).

    Shared by the real arms and by the self-test's synthetic screens, so the positive
    control exercises the same code path the real arms do rather than a copy of it.
    Python-only: the shell screens are covered by classify() on their source.
    """
    rc_bogus = subprocess.run([sys.executable, "-I", path, "--zz-bogus-flag"],
                              capture_output=True).returncode
    rcs, ns = [], []
    for flag in BOTH:
        rc, n = selftest_signature(path, flag)
        rcs.append(rc)
        ns.append(n)
    ok = (rc_bogus != 0
          and all(rc == 0 for rc in rcs)
          and all(n is not None and n > 0 for n in ns)
          and ns[0] == ns[1])
    detail = ("bogus-exit=%-3s exits=%-8s assertions=%s"
              % (rc_bogus, ",".join(str(x) for x in rcs),
                 ",".join("none" if n is None else str(n) for n in ns)))
    return ok, detail

# a screen with no self-test at all is out of scope, and so is one that takes
# positional arguments rather than a mode flag.
def classify(src):
    # --- shell shapes. A `case` (or `if`) accepting BOTH spellings AND carrying a `--*)`
    # arm that refuses the rest. The `--*)` arm is the load-bearing half: without it,
    # tests/provision-arms/arm7.sh fell through a mistyped flag into the REAL arm and
    # CREATED A KIND CLUSTER, and scripts/verify-publisher-scope.sh let the typo become
    # the NODE and probed a live Ironic for a node by that name.
    # GATED ON A BASH SHEBANG. Without this gate the shell branch also matched PYTHON
    # sources that mention both spellings, and it ran first -- so it reclassified the
    # guard-positional python fixture as an unguarded shell script. The self-test caught
    # that immediately, which is the arm earning its place twice in one sitting.
    if re.search(r'^#!.*\b(bash|sh)\b', src, re.M) and "python" not in src.split("\n")[0]:
        if re.search(r'--self-test\|--selftest\)|--selftest\|--self-test\)', src) \
                or ('"--self-test"' in src and '"--selftest"' in src) \
                or ('= "--self-test"' in src and '= "--selftest"' in src):
            return "shell-guarded" if re.search(r'--\*\)', src) else "shell-one-spelling-or-unguarded"
        if re.search(r'--self-?test', src):
            return "shell-one-spelling-or-unguarded"
    if "_WANT_SELFTEST" in src and "_ALLOWED" in src:
        return "guard"
    # a screen taking a POSITIONAL argument cannot refuse unknown args wholesale, so it
    # refuses an unrecognised `--`-prefixed first argument instead. Both spellings still
    # required. check-schema-warnings.py takes a kubectl invocation this way.
    if ('("--selftest", "--self-test")' in src or '("--self-test", "--selftest")' in src) \
            and 'startswith("--")' in src:
        return "guard-positional"
    if "argparse" in src and re.search(r'add_argument\(\s*"--self-?test"\s*,\s*"--self-?test"', src):
        return "argparse-both"
    if "argparse" in src and re.search(r'add_argument\(\s*"--self-?test"', src):
        return "argparse-one"
    if re.search(r'"--self-?test" in sys\.argv', src):
        return "bare"
    if re.search(r'self.?test', src, re.I):
        return "unknown"
    return None


def main():
    # DERIVED FROM THE MAKEFILE, not from a scripts/ glob. The glob version missed
    # tests/provision-arms/arm7.sh, which `make check` runs and whose mistyped flag
    # created a kind cluster -- the worst instance of this defect, in the one
    # mode-flagged file that lives outside scripts/. Deriving means the next one is
    # caught wherever it lives.
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    mk = os.path.join(root, "Makefile")
    if not os.path.isfile(mk):
        print("VOID: no Makefile, so the screen list cannot be derived.")
        return 2
    mktext = open(mk, encoding="utf-8").read()
    m = re.search(r"^check:([^\n]*)", mktext, re.M)
    if not m:
        print("VOID: no `check:` target found; cannot derive the screen list.")
        return 2
    targets = [t for t in m.group(1).split() if t.startswith("check-")]
    bodies = {b.group(1): b.group(3) for b in
              re.finditer(r"^([a-zA-Z0-9_.-]+):([^\n]*)\n((?:(?:\t|#)[^\n]*\n|\n)*)", mktext, re.M)}
    paths = set()
    for t in targets:
        for rel in re.findall(r"((?:scripts|tests|hack|local)/[A-Za-z0-9_./-]+\.(?:py|sh))",
                              bodies.get(t, "")):
            paths.add(rel)
    files = sorted(os.path.join(root, r) for r in paths if os.path.isfile(os.path.join(root, r)))
    me = os.path.basename(os.path.abspath(__file__))
    if not files:
        print("VOID: derived zero screens from the Makefile. A clean run over zero is not a pass.")
        return 2
    if len(files) < 10:
        print("VOID: derived only %d screen(s) from `check:`, which is implausibly few --"
              % len(files))
        print("      the Makefile shape probably changed. Refusing to report a pass.")
        return 2

    good, bad, void, skipped = [], [], [], []
    for f in files:
        name = os.path.basename(f)
        if name == me:
            continue
        kind = classify(open(f, encoding="utf-8").read())
        if kind is None:
            skipped.append((name, "no self-test mode"))
        elif kind in ("guard", "guard-positional", "argparse-both", "shell-guarded"):
            good.append((name, kind))
        elif kind == "argparse-one":
            bad.append((name, "argparse accepts only ONE spelling; add the other as an alias"))
        elif kind == "bare":
            bad.append((name, 'bare `"--flag" in sys.argv`: a typo silently runs the other mode and exits 0'))
        elif kind == "shell-one-spelling-or-unguarded":
            bad.append((name, "shell: needs BOTH spellings and a `--*)` arm refusing the rest; "
                              "without it a typo falls through to the real work"))
        else:
            void.append((name, "a self-test exists but its flag mechanism was not recognised"))

    print("  %d screen(s) with a mode flag, %d without" % (len(good) + len(bad) + len(void), len(skipped)))
    for n, k in good:
        print("    ok    %-44s %s" % (n, k))

    if void:
        print()
        print("VOID -- %d screen(s) have a self-test whose flag handling is unrecognised." % len(void))
        for n, why in void:
            print("    %-44s %s" % (n, why))
        print("  An unrecognised shape is where the next silent-wrong-mode screen would hide.")
        return 2
    if bad:
        print()
        print("FAIL -- %d screen(s) do not accept both spellings and refuse the rest:" % len(bad))
        for n, why in bad:
            print("    %-44s %s" % (n, why))
        return 1
    if not good:
        print("VOID: zero screens matched a known mechanism, so this measured nothing.")
        return 2
    return 0


def self_test():
    """Two LIVE arms, one per mechanism, plus the classifier's own cases."""
    here = os.path.dirname(os.path.abspath(__file__))
    fails = 0
    print("SELF-TEST")

    cases = [
        ("a guard screen is accepted", '_WANT_SELFTEST = False\n_ALLOWED = {"--self-test"}', "guard"),
        ("argparse with both spellings is accepted",
         'import argparse\nap.add_argument("--selftest", "--self-test", action="store_true")', "argparse-both"),
        ("argparse with one spelling FAILS",
         'import argparse\nap.add_argument("--selftest", action="store_true")', "argparse-one"),
        ("a bare sys.argv test FAILS", 'if "--self-test" in sys.argv:\n    pass', "bare"),
        ("a screen with no self-test is skipped", 'print("hello")', None),
        ("an unrecognised self-test shape is VOID", 'def self_test():\n    pass', "unknown"),
        ("a shell case with both spellings and a refusing arm is accepted",
         '#!/usr/bin/env bash\ncase "${1:-}" in\n  --self-test|--selftest) x ;;\n  --*) exit 64 ;;\nesac',
         "shell-guarded"),
        ("a shell case with ONE spelling FAILS",
         '#!/usr/bin/env bash\ncase "${1:-}" in\n  --self-test) x ;;\nesac',
         "shell-one-spelling-or-unguarded"),
        ("a positional-taking screen with both spellings is accepted",
         'if _first in ("--selftest", "--self-test"):\n    pass\nif _first.startswith("--"):\n    pass',
         "guard-positional"),
    ]
    for label, src, want in cases:
        got = classify(src)
        ok = got == want
        print("  %s %-46s expected %-14s got %s" % ("ok   " if ok else "FAIL ", label, want, got))
        fails |= 0 if ok else 1

    # LIVE ARMS: the source check proves the text, not the behaviour. These prove it.
    for name, expect_nonzero in (("check-findby-reachability.py", True),   # guard mechanism
                                 ("check-restdefinition-shape.py", True)): # argparse mechanism
        p = os.path.join(here, name)
        if not os.path.isfile(p):
            print("  VOID  %-46s not present, so this arm measured nothing" % name)
            fails = 1
            continue
        ok, detail = live_arm(p)
        print("  %s %-46s %s"
              % ("ok   " if ok else "FAIL ", "LIVE: " + name, detail))
        fails |= 0 if ok else 1

    # ---- the live arm's discriminator, and the arm's own controls ----------
    def c(label, ok):
        nonlocal fails
        print("  %s %s" % ("ok   " if ok else "FAIL ", label))
        fails |= 0 if ok else 1

    # verbatim from `check-findby-reachability.py --zz-bogus`
    refusal = ("usage: scripts/check-findby-reachability.py [--self-test|--selftest]\n"
               "refusing unrecognised argument(s): --zz-bogus\n"
               "a mistyped mode flag would otherwise run the OTHER mode and exit 0.\n")
    old_pat = r"self.?test|assertion|\d+ cases|^ *ok +"
    c("the OLD pattern matched the REFUSAL text (this is why the arm was vacuous)",
      bool(re.search(old_pat, refusal, re.I | re.M)))
    c("the count parser finds nothing in that same refusal text",
      _COUNT.search(refusal) is None)
    c("count parser reads `SELF-TEST: 11/11 assertions passed` as 11",
      int(next(g for g in _COUNT.search("SELF-TEST: 11/11 assertions passed").groups()
               if g is not None)) == 11)
    c("count parser reads `selftest: 57 cases, 0 failure(s)` as 57",
      int(next(g for g in _COUNT.search("  selftest: 57 cases, 0 failure(s)").groups()
               if g is not None)) == 57)

    # PERMANENT SYNTHETIC ARMS. The real one-spelling screens were fixed in #137/#138,
    # so without these the live arm has no input that must fail.
    d = tempfile.mkdtemp()

    def screen(fname, allowed, count):
        q = os.path.join(d, fname)
        io_lines = [
            "import sys",
            "_ALLOWED = {%s}" % ", ".join('"%s"' % a for a in allowed),
            "_unknown = [a for a in sys.argv[1:] if a not in _ALLOWED]",
            "if _unknown:",
            '    sys.stderr.write("usage: %s [--self-test|--selftest]\\n" % sys.argv[0])',
            "    sys.exit(64)",
            'print("selftest: %d cases, 0 failure(s)")' % count,
        ]
        with open(q, "w") as fh:
            fh.write("\n".join(io_lines) + "\n")
        return q

    one = screen("one-spelling.py", ["--self-test"], 3)
    both = screen("both-spellings.py", ["--self-test", "--selftest"], 3)
    zero = screen("zero-cases.py", ["--self-test", "--selftest"], 0)

    c("LIVE ARM positive control: refusing ONE spelling is REJECTED", not live_arm(one)[0])
    c("LIVE ARM negative control: accepting BOTH is accepted", live_arm(both)[0])
    c("LIVE ARM: a self-test reporting ZERO assertions is REJECTED", not live_arm(zero)[0])
    c("DISCRIMINATOR: live_arm CAN return both verdicts",
      live_arm(both)[0] and not live_arm(one)[0])

    if fails:
        print("SELF-TEST FAILED")
        return 2
    total = len(cases) + 2 + 8   # classifier cases + 2 live arms + 8 arms added above
    print("SELF-TEST: %d/%d assertions passed" % (total, total))
    return 0


_WANT_SELFTEST = False

if __name__ == "__main__":
    _ALLOWED = {"--self-test", "--selftest"}
    _unknown = [a for a in sys.argv[1:] if a not in _ALLOWED]
    if _unknown:
        sys.stderr.write("usage: %s [%s]\n" % (sys.argv[0], "|".join(sorted(_ALLOWED))))
        sys.stderr.write("refusing unrecognised argument(s): %s\n" % " ".join(_unknown))
        sys.exit(64)
    _WANT_SELFTEST = bool(_ALLOWED & set(sys.argv[1:]))
    sys.exit(self_test() if _WANT_SELFTEST else main())
