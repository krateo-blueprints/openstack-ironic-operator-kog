#!/usr/bin/env python3
"""Measure the gap between a generated CRD being UPDATED and its new schema being SERVED.

oasgen#446: a RestDefinition reporting Ready=True means oasgen finished its
reconcile, NOT that the apiserver is answering with the new schema. A consumer
that acts on that readiness -- creating a CR with a newly-added field -- can get
the old contract and a rejection it cannot explain.

Two clocks are sampled, as fast as the apiserver will answer:

  SCHEMA   the CRD object's own spec: is `field` in the served version's
           spec.properties? This is what oasgen wrote.
  HONOURED a server-side dry-run apply of a CR carrying that field: does the
           apiserver ACCEPT it? This is what the apiserver enforces.

           Measured, not assumed: `kubectl apply` uses STRICT decoding, so a
           field absent from the served schema comes back as an explicit
           `strict decoding error: unknown field "spec.<field>"` rather than
           being pruned in silence. I designed this around pruning and the
           calibration run corrected me. The explicit error is the better
           instrument -- "rejected for this exact field" is unambiguous where a
           pruned field has to be inferred from a diff.

The interesting number is HONOURED minus SCHEMA. If it is non-zero there is a
window in which the CRD says one thing and the apiserver does another, which is
exactly what #446 describes. If SCHEMA never flips, the generator did not change
the schema and the run says nothing about the window.

Deliberately does NOT create anything: dry-run only, so it is safe to point at a
cluster holding live state.
"""
import argparse
import json
import subprocess
import sys
import time

TEMPLATE = {
    "apiVersion": None,          # filled from the CRD's served version
    "kind": None,
    "metadata": {"name": "serving-window-probe", "namespace": None},
    "spec": {},
}


def sh(cmd, inp=None):
    r = subprocess.run(cmd, shell=True, input=inp, capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def crd_state(kubectl, crd, field):
    """(served_version, field_in_schema, resourceVersion) or (None, None, None)."""
    rc, out, _ = sh(f"{kubectl} get crd {crd} -o json")
    if rc != 0 or not out:
        return None, None, None
    d = json.loads(out)
    rv = d["metadata"]["resourceVersion"]
    for v in d["spec"]["versions"]:
        if v.get("served"):
            props = (v["schema"]["openAPIV3Schema"]["properties"]["spec"]
                     .get("properties") or {})
            return v["name"], field in props, rv
    return None, None, rv


def honoured(kubectl, group, version, kind, ns, field, extra):
    """True when the apiserver ACCEPTS a CR carrying `field`.

    Returns (True | False | None, note). False means the apiserver explicitly
    rejected THIS field as unknown -- the served schema does not have it yet.
    None means something else went wrong and the sample is not evidence either
    way, which is kept distinct from False on purpose: a probe that cannot tell
    "the field is not served" from "my request was malformed" would report the
    window closing the moment I fixed my own body.
    """
    body = dict(TEMPLATE)
    body["apiVersion"] = f"{group}/{version}"
    body["kind"] = kind
    body["metadata"] = dict(TEMPLATE["metadata"], namespace=ns)
    body["spec"] = dict(extra)
    body["spec"][field] = "serving-window-probe-value"
    rc, out, err = sh(f"{kubectl} apply --dry-run=server -o json -f -",
                      inp=json.dumps(body))
    if rc == 0:
        return True, ""
    blob = (err or "") + (out or "")
    if f'unknown field "spec.{field}"' in blob:
        return False, ""
    return None, blob.strip().replace("\n", " ")[:140]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kubectl", required=True)
    ap.add_argument("--crd", required=True, help="e.g. nodepowers.baremetal.ogen.krateo.io")
    ap.add_argument("--kind", required=True, help="e.g. NodePower")
    ap.add_argument("--field", required=True, help="the field only the NEW schema declares")
    ap.add_argument("--namespace", default="openstack")
    ap.add_argument("--required", default="{}",
                    help="JSON of other required spec fields, so the probe validates")
    ap.add_argument("--seconds", type=float, default=300)
    # Default 0: the two kubectl calls already cost ~1.4-2.1s per sample pair
    # against a local kind cluster (measured 2026-11-05), so that is the
    # instrument's resolution and an added sleep only makes it coarser.
    #
    # The first sample can cost ~28s, and the cause is narrower than I first
    # wrote: it is the `apply --dry-run=server` leg fetching OpenAPI v3 schemas,
    # NOT the discovery cache in general. Re-measured: a `get crd -o jsonpath`
    # costs 1.25-2.51s on a first call, so a polling-only probe never pays it.
    # It matters because a one-off ~28s first sample looks exactly like a long
    # serving window, and because it is the leg that only THIS probe uses.
    ap.add_argument("--interval", type=float, default=0.0)
    args = ap.parse_args()

    group = args.crd.split(".", 1)[1]
    extra = json.loads(args.required)
    t0 = time.time()
    first_schema = first_honoured = None
    last = None
    samples = 0

    print(f"  watching {args.crd} for spec.{args.field}")
    print(f"  SCHEMA = the CRD says so | HONOURED = a dry-run apply keeps the field\n")
    while time.time() - t0 < args.seconds:
        ver, in_schema, rv = crd_state(args.kubectl, args.crd, args.field)
        hon, note = (None, "")
        if ver:
            hon, note = honoured(args.kubectl, group, ver, args.kind,
                                 args.namespace, args.field, extra)
        samples += 1
        now = round(time.time() - t0, 2)
        state = (ver, in_schema, hon)
        if state != last:
            print(f"  t+{now:7.2f}s  version={ver}  SCHEMA={in_schema}  "
                  f"HONOURED={hon}  rv={rv}" + (f"  [{note}]" if note else ""))
            last = state
        if in_schema and first_schema is None:
            first_schema = now
        if hon is None and note:
            # Surface a malformed probe immediately rather than letting it look
            # like a long window. This fired on the first calibration run.
            print(f"  t+{now:7.2f}s  PROBE ERROR (not evidence): {note}")
        if hon is True and first_honoured is None:
            first_honoured = now
        if first_schema is not None and first_honoured is not None:
            break
        time.sleep(args.interval)

    print(f"\n  samples: {samples}")
    if first_schema is None:
        print("  SCHEMA never flipped -- the generator did not add the field, so this")
        print("  run says NOTHING about the serving window. Not a zero; a non-result.")
        return 2
    if first_honoured is None:
        print(f"  SCHEMA flipped at t+{first_schema}s but a dry-run apply still prunes")
        print(f"  the field after {args.seconds}s -- the window is larger than the watch.")
        return 1
    gap = round(first_honoured - first_schema, 2)
    print(f"  SCHEMA at t+{first_schema}s | HONOURED at t+{first_honoured}s")
    print(f"  SERVING WINDOW: {gap}s")
    print(f"  (sampled every {args.interval}s, so a gap below that is reported as 0.0")
    print(f"   and means 'smaller than the resolution', not 'absent')")
    return 0


if __name__ == "__main__":
    sys.exit(main())
