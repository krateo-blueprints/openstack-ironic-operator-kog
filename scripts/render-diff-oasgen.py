"""Render the four uuid-excluding RestDefinitions under 0.50.0 and main, and diff.

Tests FRESH generation, which is what #456's narrowing affects, across all four
subjects -- not just the one that happens to be installed. Touches no cluster.
"""
import glob, json, os, re, sys, urllib.request, difflib
import yaml

REPO = "/Users/diegobraga/openstack-ironic-operator"
PORTS = {"0.50.0": 18050, "main@57573a5": 18057}
# the four whose excludedSpecFields names uuid
WANT = ("allocation", "deploy-template", "port", "portgroup")


def oas_for(oaspath):
    m = re.match(r"configmap://[^/]+/([^/]+)/", oaspath)
    cm = m.group(1) if m else ""
    base = cm[:-4] if cm.endswith("-oas") else cm
    p = os.path.join(REPO, f"oas/{base}.yaml")
    return p if os.path.exists(p) else None


def build_request():
    defs, oas = [], {}
    for name in WANT:
        f = os.path.join(REPO, f"manifests/restdefinition-{name}.yaml")
        for d in [x for x in yaml.safe_load_all(open(f)) if x and x.get("kind") == "RestDefinition"]:
            p = oas_for(d["spec"]["oasPath"])
            if not p:
                print(f"  SKIP {name}: OAS not resolved from {d['spec']['oasPath']}")
                continue
            defs.append(d)
            oas[d["spec"]["oasPath"]] = open(p).read()
    return {"restDefinitions": defs, "oas": oas}


def render(port, req):
    body = json.dumps(req).encode()
    r = urllib.request.Request(f"http://127.0.0.1:{port}/render", data=body,
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=60) as resp:
        return json.load(resp)


def spec_facts(crds):
    """kind -> (sorted spec properties, sorted required)"""
    out = {}
    for c in crds or []:
        kind = c["spec"]["names"]["kind"]
        for v in c["spec"]["versions"]:
            sp = v["schema"]["openAPIV3Schema"]["properties"]["spec"]
            out[f"{kind}@{v['name']}"] = (sorted((sp.get("properties") or {}).keys()),
                                          sorted(sp.get("required") or []))
    return out


def main():
    req = build_request()
    print(f"  rendering {len(req['restDefinitions'])} RestDefinition(s) "
          f"with {len(req['oas'])} OAS document(s)\n")
    results = {}
    for label, port in PORTS.items():
        try:
            results[label] = render(port, req)
        except Exception as e:
            print(f"  {label}: RENDER FAILED -- {e}")
            return 2
        r = results[label]
        print(f"  {label}: {len(r.get('crds') or [])} CRD(s), "
              f"{len(r.get('errors') or [])} error(s)")
        for p in (r.get("errors") or []):
            print(f"      ERROR {p.get('restDefinition')} {p.get('field')}: {p.get('message')[:110]}")

    a, b = spec_facts(results["0.50.0"]["crds"]), spec_facts(results["main@57573a5"]["crds"])
    print("\n  SPEC SCHEMA, per kind (uuid is the field #456 concerns):")
    keys = sorted(set(a) | set(b))
    diffs = 0
    for k in keys:
        pa, ra = a.get(k, (None, None))
        pb, rb = b.get(k, (None, None))
        same = (pa, ra) == (pb, rb)
        uu = ("uuid" in (pa or []), "uuid" in (pb or []))
        rq = ("uuid" in (ra or []), "uuid" in (rb or []))
        print(f"    {k:28} uuid in props 0.50.0={uu[0]} main={uu[1]}  "
              f"required 0.50.0={rq[0]} main={rq[1]}  {'IDENTICAL' if same else '*** DIFFERS ***'}")
        if not same:
            diffs += 1
            for ln in difflib.unified_diff([str(pa), str(ra)], [str(pb), str(rb)],
                                           "0.50.0", "main", lineterm="", n=0):
                if ln.startswith(("+", "-")) and not ln.startswith(("+++", "---")):
                    print(f"        {ln[:160]}")
    print(f"\n  KINDS DIFFERING: {diffs} of {len(keys)}")
    if diffs == 0:
        print("  #456's narrowing changed NOTHING for these four definitions -- which is")
        print("  the expected result, since every one supplies uuid via a mapping and the")
        print("  pre-existing exemption already covered them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
