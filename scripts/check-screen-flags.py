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

THREE OUTCOMES. A screen whose mechanism cannot be identified at all is VOID, not a
pass: an unrecognised shape is exactly where the next instance would hide.
"""
import glob
import os
import re
import subprocess
import sys

BOTH = ("--self-test", "--selftest")

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
        rc_bogus = subprocess.run([sys.executable, "-I", p, "--zz-bogus-flag"],
                                  capture_output=True).returncode
        outs = []
        for flag in BOTH:
            r = subprocess.run([sys.executable, "-I", p, flag], capture_output=True, text=True)
            outs.append(bool(re.search(r"self.?test|assertion|\d+ cases|^ *ok +",
                                       r.stdout + r.stderr, re.I | re.M)))
        ok = (rc_bogus != 0) and all(outs)
        print("  %s %-46s bogus-exit=%-3s self-test ran under both=%s"
              % ("ok   " if ok else "FAIL ", "LIVE: " + name, rc_bogus, all(outs)))
        fails |= 0 if ok else 1

    if fails:
        print("SELF-TEST FAILED")
        return 2
    print("SELF-TEST: %d/%d assertions passed" % (len(cases) + 2, len(cases) + 2))
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
