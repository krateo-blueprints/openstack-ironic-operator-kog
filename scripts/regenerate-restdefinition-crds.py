#!/usr/bin/env python3
"""Force oasgen to regenerate the CRDs for the live RestDefinitions, and verify it.

WHY THIS EXISTS: oasgen-provider does not regenerate an existing CRD when the
provider is upgraded. Its Observe compares four hashes -- status.digest,
status.resourceHash, status.oasHash, status.authSecretDigest -- and none of them
covers the GENERATOR VERSION (oasgen #453). So installing a version that fixes a
schema defect leaves every already-generated CRD exactly as the broken version
wrote it. Upgrading is not the procedure; upgrading and then regenerating is.

`oasHash` IS hashed, so changing the referenced OAS ConfigMap is the supported
nudge. This edits the OAS files in oas/ (a marker comment -- semantically inert,
it does not touch the parsed schema), reapplies the ConfigMaps through the normal
path, and then CHECKS that each definition's oasHash actually moved.

That check is the point. If a comment-only change does not move the hash, this
whole procedure is void, and the honest outcome is to say so loudly rather than
report success because the commands ran. A nudge that silently fails looks
identical to a nudge that worked and a generator that ignored it.

Usage:
    scripts/regenerate-restdefinition-crds.py --kubectl "kubectl --kubeconfig X --context Y"
    scripts/regenerate-restdefinition-crds.py ... --check-only
"""
import argparse
import datetime
import json
import os
import re
import shlex
import subprocess
import sys
import time

NS = os.environ.get("IRONIC_NS", "openstack")
MARKER = "# regeneration-nudge:"


def sh(cmd, check=True):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise SystemExit(f"FAILED: {cmd}\n{r.stderr.strip()[:500]}")
    return r.stdout


def definitions(kubectl):
    out = sh(f"{kubectl} -n {NS} get restdefinitions.v1alpha1.ogen.krateo.io -o json")
    d = json.loads(out)
    res = {}
    for i in d.get("items", []):
        st = i.get("status") or {}
        res[i["metadata"]["name"]] = {
            "oasHash": st.get("oasHash"),
            "oasPath": st.get("oasPath"),
        }
    return res


def oas_files(defs):
    """Map each definition to the oas/ file its oasPath resolves to."""
    out = {}
    for name, st in defs.items():
        m = re.match(r"configmap://[^/]+/([^/]+)/", st.get("oasPath") or "")
        if not m:
            out[name] = None
            continue
        cm = m.group(1)
        base = cm[:-4] if cm.endswith("-oas") else cm
        p = f"oas/{base}.yaml"
        out[name] = p if os.path.exists(p) else None
    return out


def nudge(paths):
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    touched = []
    for p in sorted({x for x in paths if x}):
        with open(p) as fh:
            body = fh.read()
        line = f"{MARKER} {stamp}\n"
        if MARKER in body:
            body = re.sub(rf"^{re.escape(MARKER)}.*\n", line, body, count=1, flags=re.M)
        else:
            body = body.rstrip("\n") + "\n" + line
        with open(p, "w") as fh:
            fh.write(body)
        touched.append(p)
    return touched, stamp


def crd_has(kubectl, crd, field):
    """(in_properties, in_required) for the served version, or (None, None)."""
    out = sh(f"{kubectl} get crd {crd} -o json", check=False)
    if not out:
        return None, None
    d = json.loads(out)
    for v in d["spec"]["versions"]:
        if v.get("served"):
            sp = v["schema"]["openAPIV3Schema"]["properties"]["spec"]
            props = sp.get("properties") or {}
            req = sp.get("required") or []
            return field in props, field in req
    return None, None


# (crd, field, want_present, why) -- `want_present` is what a CORRECT generator
# produces, so a row that disagrees is the finding.
STORED_CHECKS = (
    ("nodepowers.baremetal.ogen.krateo.io", "node_id", True,
     "#421: a fieldMapping must not delete the spec property it reads from"),
    ("nodeprovisions.baremetal.ogen.krateo.io", "node_id", True,
     "#421: same, on the provision verb"),
    ("ports.baremetal.ogen.krateo.io", "uuid", False,
     "#456: uuid is excluded AND mapped, so the narrowing must NOT expose it"),
)


def report_stored(kubectl):
    print("\n  STORED SIDE -- what the generated CRDs actually carry:")
    verdicts = []
    for crd, field, want, why in STORED_CHECKS:
        inprops, inreq = crd_has(kubectl, crd, field)
        if inprops is None:
            print(f"    {crd.split('.')[0]:16} spec.{field:8} CRD NOT FOUND -- not checked")
            verdicts.append((crd, field, None))
            continue
        state = "PRESENT" if inprops else "ABSENT"
        agree = (inprops == want)
        print(f"    {crd.split('.')[0]:16} spec.{field:8} {state:8} "
              f"required={inreq}  {'as expected' if agree else '*** UNEXPECTED ***'}")
        print(f"        {why}")
        verdicts.append((crd, field, agree))
    return verdicts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kubectl", required=True, help='e.g. "kubectl --kubeconfig X --context Y"')
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--check-only", action="store_true",
                    help="report the stored side and exit; change nothing")
    args = ap.parse_args()
    k = args.kubectl

    before = definitions(k)
    if not before:
        raise SystemExit("  no RestDefinitions found -- wrong cluster or namespace?")
    print(f"  {len(before)} live RestDefinition(s):")
    for n, st in sorted(before.items()):
        print(f"    {n:24} oasHash={str(st['oasHash'])[:16]}")

    report_stored(k)
    if args.check_only:
        return 0

    files = oas_files(before)
    unresolved = [n for n, p in files.items() if not p]
    if unresolved:
        raise SystemExit(f"\n  REFUSING: no oas/ file resolved for {unresolved}. "
                         f"Nudging the rest would regenerate SOME definitions and "
                         f"leave others on the old generator, which is worse than "
                         f"not starting.")

    touched, stamp = nudge(files.values())
    print(f"\n  nudged {len(touched)} OAS file(s) with marker {stamp}")
    sh(f'KUBECTL={shlex.quote(k)} ./scripts/create-ironic-oas-configmap.sh {NS}')
    print("  OAS ConfigMaps reapplied")

    deadline = time.time() + args.timeout
    pending = set(before)
    while pending and time.time() < deadline:
        time.sleep(10)
        now = definitions(k)
        for n in sorted(pending):
            if now.get(n, {}).get("oasHash") != before[n]["oasHash"]:
                print(f"    {n}: oasHash moved -> {str(now[n]['oasHash'])[:16]}")
                pending.discard(n)

    if pending:
        print(f"\n  THE NUDGE DID NOT TAKE on: {sorted(pending)}")
        print("  oasHash did not move, so oasgen has not re-read the OAS and the")
        print("  CRDs were NOT regenerated. Do not read the stored side below as a")
        print("  verdict on the generator -- the procedure itself failed. A")
        print("  comment-only edit may not reach the hash; try a semantically inert")
        print("  but structural change (e.g. a description field) before concluding")
        print("  anything about #421.")
        report_stored(k)
        return 2

    print("\n  all definitions re-read their OAS")
    report_stored(k)
    return 0


if __name__ == "__main__":
    sys.exit(main())
