#!/usr/bin/env python3
"""Every NodeProvision render site must be gated on a LIVE provision-state read.

WHY THIS SCREEN EXISTS. NodeProvision's create is dispatched by Observe reporting the
resource ABSENT, and for this kind findby can never match (`node_id` is absent from
/v1/nodes items and no responseTransform is declared, deliberately -- see
manifests/restdefinition-provision.yaml). So rdc re-dispatches
PUT /v1/nodes/{node_id}/states/provision on EVERY resync for as long as the CR exists.

The only thing that bounds that is the chart: each transition template is gated on
`baremetal-host.provisionState`, which is a `lookup` of the live BaremetalNode's
status.provision_state. When the node leaves the gating state the gate closes, the CR
stops being rendered, and cdc removes it. An empty lookup returns "" and every
`eq ... "<state>"` comparison is then false, so the gates FAIL CLOSED -- the safe
direction.

That bound is invisible from either side on its own: nothing in the RestDefinition says
"a chart limits my blast radius", and nothing in the chart says "without this gate the
action re-fires forever". If a gate is ever changed to a values flag, or a new transition
template is added without one, the bound disappears silently and the only symptom is a
provision transition re-issued every 3 minutes against a live node.

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
            "(none of %s). Without that gate the action re-fires every resync."
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


if __name__ == "__main__":
    sys.exit(self_test() if "--self-test" in sys.argv else run())
