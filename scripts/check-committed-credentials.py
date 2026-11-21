#!/usr/bin/env python3
"""No plaintext credential value may sit in a tracked file.

WHY A REPO-LOCAL SCREEN, WHEN THE ORG ALREADY SCANS FOR SECRETS. It does scan --
`security / filesystem` runs Trivy with `scanners: vuln,secret,misconfig` AND
gitleaks. Both missed a jump host's password and a password-login example that sat
in this repo's docs until 2026-11-17 (#115), and the reason is written in the org
scanner's own config:

    Shared allowlist: documentation, generated API specs, vendored subcharts,
    sample manifests, Helm template partials and JSON schemas are not secret
    stores ... Real secrets in source (values.yaml, code, config) are still caught.

That allowlist is correct about entropy false positives and wrong about this repo:
`docs/` and `manifests/` ARE where our credentials lived, because this is a
blueprint repo whose product is runbooks and sample manifests. So the classes the
org scanner excludes are exactly the classes nothing was checking.

It is also worth naming what I got wrong on the way here, since the mistake is the
reusable part: I first reported `security / detect` as the gate that failed. That
job is a STACK DETECTOR -- it emits go/node/chart booleans to decide which scans
run -- and never looked for a secret in its life. A green check next to a
plausible name is not evidence about what was checked.

WHAT THIS CATCHES. A credential-shaped key assigned a short literal value, prose
telling a reader to log in with a named password, and inline `user:pass@` URLs.
Entropy is deliberately NOT used: the values that actually leaked here were
`baremetal` and `ops`, which no entropy rule will ever flag, and that is the point.

WHAT IT DOES NOT CATCH, stated so a pass is not read as more than it is: a secret
in git HISTORY (this reads the worktree only -- #115's password was public for
months and a clean run here says nothing about that), a high-entropy token the
org scanner already covers, or a credential that never appears as a literal.

usage:  check-committed-credentials.py [--self-test] [--list]
"""
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# A credential-shaped key with a LITERAL value.
KEY_ASSIGN = re.compile(
    r"^[^\S\n]*#?[^\S\n]*[\"']?([A-Za-z_]*(?:password|passwd|pw))[\"']?[^\S\n]*:"
    r"[^\S\n]*([\"']?)([^\s\"'{<#$][^\s\"'#]*)\2[^\S\n]*(?:#.*)?$",
    re.I,
)
# Prose that hands a reader a password.
PROSE = re.compile(
    r"(?:\bpasswords?\b|\bpw\b)(?:\s+(?:is|was|=))?\s*[:=]?\s*"
    r"[`\"']([A-Za-z0-9!@#._/-]{2,40})[`\"']",
    re.I,
)
PROSE_BARE = re.compile(r"\bwith (?:the )?password\s+([A-Za-z0-9!@#._/-]{2,40})\b", re.I)
# Bare prose after "with password" is usually a sentence, not a value.
PROSE_STOPWORDS = frozenset(
    "a an and the this that these those it its you your yours our their his her "
    "above below here there off on set unset empty enabled disabled authentication "
    "auth authn login logins access interpolated means being chosen choose rotate "
    "rotated required optional for from into over under when if then else".split()
)
URL_CREDS = re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@]+:([^/\s@]{2,})@")

# Values that are not credentials. A placeholder is the FIX, so it must pass.
BENIGN_VALUE = re.compile(
    r"^(?:"
    r"<.*>|\{\{.*|\$.*|\(.*|true|false|null|none|empty|required|optional|string|object|"
    r"integer|boolean|yes|no|n/?a|tbd|"
    r"replace-?me|change-?me|changeit|your-.*|my-.*|example.*|sample.*|placeholder|"
    r"redacted|removed|scrubbed|secret|password|passwd|hunter2|xxx+|\.\.\.|-+|\*+|"
    r"wrong|wrong-.*|.*-intentional|bogus.*|invalid.*|deliberately-.*|"
    r"read\b.*|\[.*|.*\|.*"
    r")$",
    re.I,
)
# Lines that are talking ABOUT credentials rather than containing one.
BENIGN_LINE = re.compile(
    r"valueFrom|secretKeyRef|tokenRef|existingSecret|bmcSecret|passwordKey|"
    r"\bread -rs\b|getenv|environ|os\.environ|\$\{|\{\{|<\?|"
    r"\"methods\"|'methods'|--arg\b",
)

# --- Accepted findings. Every entry needs a path glob, the exact value, and a REASON.
#     An entry that matches nothing is a FAILURE: a dead exemption silently accepts
#     whatever replaces it (see reference: an exemption that outlived its reason).
ACCEPTED = [
    {
        "glob": "manifests/baremetal*.yaml",
        "value": "baremetal",
        "reason": (
            "Lab fixture for the sushy-emulator, which enforces NO Redfish auth at all -- "
            "docs/TEST-PLAN.md gap 7 proved it: a node given a deliberately wrong password "
            "walked enroll->manageable unimpeded. Several of these files state in-file that "
            "the plaintext is deliberate so the fixture stays applyable with one command and "
            "no prerequisites. MUST BE REVISITED the moment the lab gets physical BMCs, which "
            "TEST-PLAN's own re-evaluation criterion anticipates."
        ),
    },
    {
        "glob": "manifests/kubernetescluster-example.yaml",
        "value": "baremetal",
        "reason": (
            "Same emulator credential. This file's own header says it carries the BMC password "
            "in plaintext deliberately, because its job is to be applyable with one command and "
            "no prerequisites; kubernetescluster-ettore-ha.example.yaml is the Secret-based shape."
        ),
    },
    {
        "glob": "docs/KUBERNETES-CLUSTER.md",
        "value": "baremetal",
        "reason": (
            "A WARNING, not a leak: the paragraph records that until chart 0.14.0 every node "
            "was provisioned with an unlocked account and this hardcoded password, and that "
            "0.15.0 made nodeAccess default-safe. Naming the old value is what makes the "
            "warning actionable for anyone still running a pre-0.15.0 node. Kept on purpose "
            "in #115; removing it would erase the record of a fixed defect."
        ),
    },
    {
        "glob": "charts/*/values.yaml",
        "value": "baremetal",
        "reason": (
            "A COMMENTED-OUT example, and the surrounding comment documents that the chart "
            "hardcoded this until 2026-11-08 and now defaults to a locked account. Removing it "
            "would erase the record of a fixed defect."
        ),
    },
]


# This screen's own self-test needs credential-SHAPED fixtures, so it flags itself.
# Exempting the whole file would be a blind spot: a real credential pasted into it
# would then be invisible. So the skip is bounded to a marked block, in this ONE
# file, and the self-test asserts BOTH halves -- inside is skipped, outside is still
# caught. If the markers go missing or unbalanced the screen returns VOID rather
# than quietly skipping nothing (or everything).
SELF_PATH = "scripts/check-committed-credentials.py"
FIXTURE_OPEN = "# >>> credential-fixtures"
FIXTURE_CLOSE = "# <<< credential-fixtures"


def fixture_skip_lines(relpath, text):
    """Line numbers to ignore. Non-empty only for this screen's own marked block."""
    if relpath != SELF_PATH:
        return frozenset()
    skip, depth = set(), 0
    for lineno, line in enumerate(text.splitlines(), 1):
        if FIXTURE_OPEN in line:
            depth += 1
            skip.add(lineno)
            continue
        if FIXTURE_CLOSE in line:
            depth -= 1
            skip.add(lineno)
            continue
        if depth > 0:
            skip.add(lineno)
    if depth != 0:
        return None          # unbalanced -> caller must VOID
    return frozenset(skip)


def tracked_files():
    out = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, text=True
    )
    return [f for f in out.stdout.split("\0") if f]


def is_text(path):
    try:
        with open(path, "rb") as fh:
            chunk = fh.read(4096)
    except OSError:
        return False
    if b"\0" in chunk:
        return False
    return True


def findings_in_text(relpath, text, skip=frozenset()):
    """Every (line, value, kind) that looks like a committed credential value."""
    out = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if lineno in skip:
            continue
        if len(line) > 400:
            continue
        if BENIGN_LINE.search(line):
            continue
        for kind, pat, group in (
            ("key", KEY_ASSIGN, 3),
            ("prose", PROSE, 1),
            ("prose-bare", PROSE_BARE, 1),
            ("url", URL_CREDS, 1),
        ):
            m = pat.search(line)
            if not m:
                continue
            val = m.group(group)
            if not val or BENIGN_VALUE.match(val):
                continue
            if kind == "prose-bare" and val.lower() in PROSE_STOPWORDS:
                continue
            out.append((lineno, val, kind, line.strip()[:120]))
            break
    return out


def accepted_for(relpath, value):
    for entry in ACCEPTED:
        if pathlib.PurePath(relpath).match(entry["glob"]) and value == entry["value"]:
            return entry
    return None


def run(list_only=False):
    # GUARD: an entry with no reason accepts things for no stated cause.
    for entry in ACCEPTED:
        if not entry.get("reason", "").strip():
            print("GUARD: an ACCEPTED entry has an empty reason: %r" % entry.get("glob"))
            return 2
        if not entry.get("value", "").strip() or not entry.get("glob", "").strip():
            print("GUARD: an ACCEPTED entry has an empty glob or value")
            return 2

    files = tracked_files()
    if not files:
        print("GUARD: git ls-files returned nothing -- the screen would pass vacuously")
        return 2

    findings, used = [], set()
    scanned = 0
    for rel in files:
        p = ROOT / rel
        if not p.is_file() or not is_text(p):
            continue
        scanned += 1
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        skip = fixture_skip_lines(rel, text)
        if skip is None:
            print("GUARD: %s has unbalanced credential-fixture markers -- not a pass" % rel)
            return 2
        if rel == SELF_PATH and not skip:
            print("GUARD: %s has no credential-fixture markers; its own self-test "
                  "fixtures would fail the screen -- not a pass" % rel)
            return 2
        for lineno, val, kind, line in findings_in_text(rel, text, skip):
            entry = accepted_for(rel, val)
            if entry:
                used.add(entry["glob"] + "\0" + entry["value"])
                if list_only:
                    print("  accepted %s:%d [%s]" % (rel, lineno, kind))
                continue
            findings.append((rel, lineno, val, kind, line))

    if scanned == 0:
        print("GUARD: 0 text files scanned out of %d tracked -- not a pass" % len(files))
        return 2
    print("  scanned %d tracked text files" % scanned)

    # A dead exemption is a failure, not a convenience.
    dead = [e for e in ACCEPTED if (e["glob"] + "\0" + e["value"]) not in used]
    if dead and not list_only:
        for e in dead:
            print("  FINDING: ACCEPTED entry matches nothing any more: %s / %r"
                  % (e["glob"], e["value"]))
        print("FAIL -- a dead exemption would silently accept whatever replaces it.")
        return 1

    if findings:
        for rel, lineno, val, kind, line in findings:
            print("  FINDING: %s:%d [%s] committed credential value" % (rel, lineno, kind))
            print("           %s" % line)
        print("FAIL -- %d plaintext credential value(s) in tracked files." % len(findings))
        print("        Fix: replace with a placeholder such as \"<choose one>\", or add an")
        print("        ACCEPTED entry with a written reason. And if it was ever pushed,")
        print("        ROTATE it -- scrubbing the worktree does not un-publish it.")
        return 1

    print("PASS -- no unaccepted plaintext credential value in any tracked file.")
    return 0


def self_test():
    # >>> credential-fixtures
    cases = [
        # (name, relpath, text, expect_findings)
        ("a doc handing out a password", "docs/X.md",
         "SSH as `ops` with password `ops`.\n", True),
        ("a yaml literal password", "docs/X.md",
         "          plain_text_passwd: ops\n", True),
        ("jump host with inline pw", "docs/X.md",
         "- SSH through dome (10.0.0.1, pw `baremetal`) ->\n", True),
        ("url with inline credentials", "docs/X.md",
         "    endpoint: https://admin:s3cret@keystone.example/v3\n", True),
        # the FIXES must pass
        ("placeholder in angle brackets", "docs/X.md",
         "          plain_text_passwd: \"<choose one>\"\n", False),
        ("replace-me placeholder", "docs/X.md",
         "    redfish_password: replace-me\n", False),
        ("helm template value", "charts/x/templates/y.yaml",
         "    plain_text_passwd: {{ $pw | quote }}\n", False),
        ("a Secret reference, not a value", "manifests/x.yaml",
         "    passwordKey: redfish_password\n", False),
        ("prose discussing passwords", "docs/X.md",
         "Rotating the password means updating the Secret -- no CR edit.\n", False),
        ("keystone auth method name", "charts/x/templates/y.tpl",
         "-d \"{\\\"methods\\\":[\\\"password\\\"],\\\"password\\\":{\\\"user\\\"\n", False),
        ("read -rs prompt", "scripts/x.sh",
         "printf 'password for %s: ' \"$PUB_USER\"\n", False),
        # the empty-value and shell-var shapes
        ("empty value", "docs/X.md", "  password:\n", False),
        ("shell variable", "scripts/x.sh", "  password: $PASSWORD\n", False),
        # --- shapes the first live run flagged WRONGLY; locked in as regressions
        ("password for the `x` user", "charts/x/values.yaml",
         "# Plaintext password for the `ironic` user. Ignored unless\n", False),
        ("password interpolated into a var", "docs/X.md",
         "Three of the five are inline `AUTH_BODY` strings with the password interpolated\n", False),
        ("with the password you chose", "docs/X.md",
         "network. SSH as `ops` with the password you chose above.\n", False),
        ("password authentication off", "docs/X.md",
         "> defaults to a locked account with password authentication off. Prefer an SSH\n", False),
        ("json key then object", "manifests/x.yaml",
         '        "password": {"user": {\n', False),
        ("json schema fixture", "scripts/x.py",
         '  {"properties": {"password": {"type": "string", "title": "t"}}},\n', False),
        ("deliberately wrong credential", "manifests/x.yaml",
         "    redfish_password: wrong-password-intentional   # gap 7: intentionally bogus\n", False),
        # and the true positive must STILL fire
        ("adjacent quoted password still fires", "docs/X.md",
         "the password `baremetal` and `ssh_pwauth: true`, hardcoded\n", True),
    ]
    # <<< credential-fixtures
    fails = 0
    for name, rel, text, expect in cases:
        got = bool(findings_in_text(rel, text))
        ok = got == expect
        print("  %-42s %s" % (name, "ok" if ok else "FAIL (want %s got %s)" % (expect, got)))
        if not ok:
            fails += 1

    # the accept path must actually accept, and only for the right path+value
    acc = accepted_for("manifests/baremetalhost-blade03.yaml", "baremetal")
    print("  %-42s %s" % ("fixture+value is accepted", "ok" if acc else "FAIL"))
    fails += 0 if acc else 1
    wrong_path = accepted_for("docs/USER-GUIDE.md", "baremetal")
    print("  %-42s %s" % ("same value in docs is NOT accepted", "ok" if not wrong_path else "FAIL"))
    fails += 0 if not wrong_path else 1
    wrong_val = accepted_for("manifests/baremetalhost-blade03.yaml", "ops")
    print("  %-42s %s" % ("different value is NOT accepted", "ok" if not wrong_val else "FAIL"))
    fails += 0 if not wrong_val else 1

    # The self-exemption must be BOUNDED: inside the markers skipped, outside caught.
    own = pathlib.Path(__file__).read_text()
    own_skip = fixture_skip_lines(SELF_PATH, own)
    bounded_in = own_skip is not None and bool(own_skip)
    print("  %-42s %s" % ("own fixture block is skipped", "ok" if bounded_in else "FAIL"))
    fails += 0 if bounded_in else 1

    # >>> credential-fixtures
    planted = "x = 1\n" + FIXTURE_OPEN + "\npw `baremetal`\n" + FIXTURE_CLOSE + \
              "\nSSH as `ops` with password `ops`.\n"
    # <<< credential-fixtures
    psk = fixture_skip_lines(SELF_PATH, planted)
    outside = findings_in_text(SELF_PATH, planted, psk)
    bounded_out = len(outside) == 1 and outside[0][0] == 5
    print("  %-42s %s" % ("a credential OUTSIDE the block is caught",
                          "ok" if bounded_out else "FAIL (%r)" % (outside,)))
    fails += 0 if bounded_out else 1

    # >>> credential-fixtures
    unbalanced = fixture_skip_lines(SELF_PATH, "x\n" + FIXTURE_OPEN + "\npw `x1`\n")
    # <<< credential-fixtures
    print("  %-42s %s" % ("unbalanced markers return VOID",
                          "ok" if unbalanced is None else "FAIL"))
    fails += 0 if unbalanced is None else 1

    print("selftest: %d assertions, %d failure(s)" % (len(cases) + 6, fails))
    if fails:
        return 1
    print("  -> the screen fires on a password in prose, in YAML and in a URL, and stays")
    print("     quiet on placeholders, template expressions and Secret references.")
    return 0


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
    _ALLOWED = {"--self-test", "--selftest", "--list"}
    _unknown = [a for a in sys.argv[1:] if a not in _ALLOWED]
    if _unknown:
        sys.stderr.write("usage: %s [%s]\n" % (sys.argv[0], "|".join(sorted(_ALLOWED))))
        sys.stderr.write("refusing unrecognised argument(s): %s\n" % " ".join(_unknown))
        sys.stderr.write("a mistyped mode flag would otherwise run the OTHER mode and exit 0.\n")
        sys.exit(64)
    _WANT_SELFTEST = bool({"--self-test", "--selftest"} & set(sys.argv[1:]))
    if _WANT_SELFTEST:
        sys.exit(self_test())
    sys.exit(run(list_only="--list" in sys.argv))
