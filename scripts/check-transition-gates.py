#!/usr/bin/env python3
"""Every NodeProvision render site must be gated on a LIVE provision-state read.

WHY THIS SCREEN EXISTS. NodeProvision's create is dispatched by Observe reporting the
resource ABSENT, and for this kind findby can never match (`node_id` is absent from
/v1/nodes items and no responseTransform is declared, deliberately -- see
manifests/restdefinition-provision.yaml).

CORRECTED 2026-11-16, AND THE CLAIM THIS REPLACES WAS MINE, HERE AND IN TWO OTHER
PLACES. This said rdc "re-dispatches PUT /v1/nodes/{node_id}/states/provision on EVERY
resync for as long as the CR exists". MEASURED TWICE -- arm7 at 0.61.0 and 0.73.0 -- that
is FALSE for a create that lands: the 202 sets Pending, the Pending guard then returns
exists-and-up-to-date, and the dispatch count stays at 1 across 6.4 resync periods.

The gate is still justified, on two narrower mechanisms the overstatement was hiding:
  * the absence counters are keyed on `gen=<generation>;<kind>`, so a SPEC EDIT resets
    them and re-enables dispatch -- the bound matters per generation, not once per CR;
  * a create that ERRORS leaves Pending unset, so there is no latch, and THAT case does
    re-dispatch every resync.

An overstated justification is worse than a narrow one: it invites a reader to check it,
find it false, and remove the screen it was defending. Which is also why this correction
had to be made in three places -- the Makefile comment, this docstring, and the FINDING
TEXT below, the last being the one an operator actually reads.

The only thing that bounds that is the chart: each transition template is gated on
`baremetal-host.provisionState`, which is a `lookup` of the live BaremetalNode's
status.provision_state. When the node leaves the gating state the gate closes, the CR
stops being rendered, and cdc removes it. An empty lookup returns "" and every
`eq ... "<state>"` comparison is then false, so the gates FAIL CLOSED -- the safe
direction.

That bound is invisible from either side on its own: nothing in the RestDefinition says
"a chart limits my blast radius", and nothing in the chart says "without this gate this
CR is never removed". If a gate is ever changed to a values flag, or a new transition
template is added without one, the bound disappears silently -- and the symptom is not a
steady stream of writes but something quieter: a CR that outlives its transition, so the
next spec edit or failed create dispatches into a node that has long since moved on.

WHAT IT DOES NOT CHECK, stated so the pass is not read as more than it is: that the
window is short, or that Ironic rejects the repeat. Both are runtime facts and belong to
the arm7 measurement, not to a static screen.

usage:  check-transition-gates.py [--self-test]
"""
import re
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "charts" / "baremetal-host" / "templates"

# A gate counts if it reads the LIVE node state. These are the accepted forms.
STATE_READS = (
    'include "baremetal-host.provisionState"',
    'include "baremetal-host.inspectionFinishedAt"',
)
KIND_RE = re.compile(r"^kind:\s*NodeProvision\s*$", re.M)


def findings_for(text, name):
    """Return a list of findings for one template's text."""
    out = []
    if not KIND_RE.search(text):
        return out
    if not any(s in text for s in STATE_READS):
        out.append(
            "%s renders NodeProvision but never reads the live provision state "
            "(none of %s). Without that gate the CR is never removed, so every "
            "generation bump and every failed create re-dispatches the transition."
            % (name, ", ".join(repr(s) for s in STATE_READS))
        )
        return out
    # The read must actually gate something: it has to appear inside an `if`.
    gated = any(
        any(s in line for s in STATE_READS)
        for line in text.splitlines()
        if "{{- if" in line or "{{ if" in line or "$state" in line or "$okTo" in line
    )
    if not gated:
        out.append(
            "%s reads the live provision state but not inside a conditional, so it "
            "does not gate the render." % name
        )
    return out


def run():
    if not TEMPLATES.is_dir():
        print("GUARD: %s is not a directory" % TEMPLATES)
        return 2
    files = sorted(TEMPLATES.glob("*.yaml"))
    if not files:
        print("GUARD: no templates found -- the screen would pass vacuously")
        return 2
    renderers, findings = [], []
    for f in files:
        text = f.read_text()
        if KIND_RE.search(text):
            renderers.append(f.name)
            findings.extend(findings_for(text, f.name))
    # A screen that finds nothing to inspect is not a pass.
    if not renderers:
        print("GUARD: no template renders NodeProvision -- either the kind is gone or this")
        print("       screen stopped matching. Not a pass.")
        return 2
    print("  inspected %d NodeProvision render site(s): %s" % (len(renderers), ", ".join(renderers)))
    if findings:
        for x in findings:
            print("  FINDING: %s" % x)
        print("FAIL -- a NodeProvision render is not bounded by a live provision-state gate.")
        return 1
    print("PASS -- every NodeProvision render site is gated on a live provision-state read.")
    return 0


def self_test():
    cases = [
        ("a gated transition (the real shape)",
         '{{- if eq (include "baremetal-host.provisionState" .) "enroll" }}\nkind: NodeProvision\n{{- end }}',
         0),
        ("ungated: renders the kind with no state read",
         'kind: NodeProvision\nmetadata:\n  name: x\n', 1),
        ("state read present but not in a conditional",
         '# {{ include "baremetal-host.provisionState" . }}\nkind: NodeProvision\n', 1),
        ("gated on a values flag instead of the live state",
         '{{- if .Values.deploy }}\nkind: NodeProvision\n{{- end }}', 1),
        ("not a NodeProvision template at all -- must be ignored",
         '{{- if .Values.x }}\nkind: NodePower\n{{- end }}', 0),
        ("indented kind: must NOT be treated as a render site",
         '  kind: NodeProvision\n', 0),
    ]
    fails = 0
    for name, text, want in cases:
        got = 1 if findings_for(text, "fixture") else 0
        ok = got == want
        print("  %-58s %s" % (name, "ok" if ok else "FAIL (want %d got %d)" % (want, got)))
        if not ok:
            fails += 1
    print("selftest: %d cases, %d failure(s)" % (len(cases), fails))
    if fails:
        return 1
    print("  -> the screen demonstrably fails on an ungated render and on a values-flag gate,")
    print("     and ignores templates that do not render the kind.")
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
    _ALLOWED = {"--self-test", "--selftest"}
    _unknown = [a for a in sys.argv[1:] if a not in _ALLOWED]
    if _unknown:
        sys.stderr.write("usage: %s [%s]\n" % (sys.argv[0], "|".join(sorted(_ALLOWED))))
        sys.stderr.write("refusing unrecognised argument(s): %s\n" % " ".join(_unknown))
        sys.stderr.write("a mistyped mode flag would otherwise run the OTHER mode and exit 0.\n")
        sys.exit(64)
    _WANT_SELFTEST = bool({"--self-test", "--selftest"} & set(sys.argv[1:]))
    sys.exit(self_test() if _WANT_SELFTEST else run())
