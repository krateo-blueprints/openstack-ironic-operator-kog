#!/usr/bin/env python3
"""The worker's node-ip derivation must respect the subnet mask the blade actually has.

THE DEFECT (issue #128, part 2). Through kubernetes-cluster 0.16.1 the worker join took
the VIP's first three octets as a STRING PREFIX and matched interface addresses that
started with it -- a hard /24 assumption. On a /22 or /16 a worker in the same subnet
with a different third octet matched nothing and fell back to kubelet autodetect, the
exact failure the block exists to prevent. The inverse also held: an address sharing the
first three octets but sitting in a different /26 WAS selected.

WHY THIS SCREEN DOES NOT COPY THE ALGORITHM. The shell runs on a blade, from cloud-init,
and cannot source a repo file; a hand-copied duplicate in a test would drift from the
template and keep passing. So this renders the REAL `kubernetes-cluster.workerUserData`
helper with `helm template`, pulls /etc/kubernetes/join.sh out of the rendered cloud-config,
cuts the node-ip block out of it, and EXECUTES that against a case table with a stubbed
`ip`. What is tested is what ships.

HOW THE REAL HELPER IS RENDERED. The worker BaremetalHosts are lookup-gated, so
`helm template` (offline) emits none of them. The screen therefore copies the chart to a
temp dir and adds ONE probe template that includes the unchanged helper into a ConfigMap.
No chart file is modified; the helper text is the one in templates/_helpers.tpl.

ONE TEXTUAL SUBSTITUTION in the executed block: the kubelet defaults path
/etc/default/kubelet is redirected into a temp file (count asserted == 1), because the
block writes it and a test must not touch the host. Nothing else is altered.

SELF-TEST FIRST (a check that cannot fail reports success): it runs the case table
against the shipped block, then against the same block with the OLD /24 string-prefix
derivation spliced back in, and requires that mutant to FAIL exactly the cases that
motivated the fix while still passing the /24 ones.

EXIT CODES: 0 pass, 1 finding, 2 VOID (cannot measure: helm or bash missing, the block
not found in the render), 3 PyYAML missing. VOID is never a pass.
"""
import ipaddress
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile

try:
    import yaml
except ImportError:
    print("check-worker-node-ip: PyYAML missing -- VOID, not a pass", file=sys.stderr)
    sys.exit(3)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHART = os.path.join(REPO, "charts", "kubernetes-cluster")
PROBE_NAME = "worker-node-ip-probe"
PROBE = """apiVersion: v1
kind: ConfigMap
metadata:
  name: %s
data:
  userdata: |
    {{- include "kubernetes-cluster.workerUserData" . | nindent 4 }}
""" % PROBE_NAME
KUBELET_PATH = "/etc/default/kubelet"

# The shipped derivation up to through 0.16.1, spliced in as the negative control.
OLD_DERIVATION = (
    'NODE_IP_PREFIX=$(echo "$VIP" | awk -F: \'{print $1}\' | awk -F. \'{print $1"."$2"."$3"."}\')\n'
    'NODE_IP=$(ip -o -4 addr show | awk -v p="$NODE_IP_PREFIX" '
    '\'$4 ~ "^"p {sub("/.*","",$4); print $4; exit}\')'
)


class Void(Exception):
    """The screen could not form an opinion. NOT a pass."""


def ipline(idx, ifname, cidr):
    """One line of `ip -o -4 addr show`; field 4 is addr/prefixlen."""
    return "%d: %-6s inet %s brd 0.0.0.0 scope global %s\\       valid_lft forever preferred_lft forever" % (
        idx, ifname, cidr, ifname)


LO = ipline(1, "lo", "127.0.0.1/8")

# (name, VIP, [interface cidrs in `ip` order], expected address or None)
# Expectations are written by hand from the network arithmetic, not computed by the
# algorithm under test.
CASES = [
    ("/24 lab (today): data NIC beside an OOB NIC",
     "10.20.30.100", ["192.168.1.5/24", "10.20.30.7/24"], "10.20.30.7"),
    ("/22: third octet differs (29 vs 30), same subnet -> MUST select",
     "10.20.30.100", ["192.168.1.5/24", "10.20.29.7/22"], "10.20.29.7"),
    ("/22: top of the block (10.20.31.250 shares 10.20.28.0/22)",
     "10.20.30.100", ["10.20.31.250/22"], "10.20.31.250"),
    ("/22: just outside (10.20.32.7 is the NEXT /22) -> must NOT select",
     "10.20.30.100", ["10.20.32.7/22"], None),
    ("/16: third octet differs widely -> MUST select",
     "10.20.30.100", ["192.168.1.5/24", "10.20.200.9/16"], "10.20.200.9"),
    ("/16: different second octet -> must NOT select",
     "10.20.30.100", ["10.21.30.9/16"], None),
    ("different /24 entirely -> must NOT select",
     "10.20.30.100", ["10.20.40.7/24"], None),
    ("same first three octets, different /26 (old code selected it) -> must NOT select",
     "10.20.30.100", ["10.20.30.200/26"], None),
    ("same /26 as the VIP (.64-.127) -> MUST select",
     "10.20.30.100", ["10.20.30.70/26"], "10.20.30.70"),
    ("/32 on another address is not the VIP's subnet",
     "10.20.30.100", ["10.20.30.101/32"], None),
    ("order: decoy first, correct second",
     "10.20.30.100", ["172.16.5.5/12", "10.20.31.9/22", "192.168.0.2/24"], "10.20.31.9"),
    ("order: correct first, decoy second",
     "10.20.30.100", ["10.20.31.9/22", "172.16.5.5/12", "192.168.0.2/24"], "10.20.31.9"),
    ("order: correct last of four",
     "10.20.30.100", ["192.168.0.2/24", "172.16.5.5/12", "10.99.0.1/16", "10.20.28.1/22"], "10.20.28.1"),
    ("two NICs in the VIP's subnet -> the first wins (unchanged behaviour)",
     "10.20.30.100", ["10.20.30.7/24", "10.20.30.8/24"], "10.20.30.7"),
    ("VIP given with a port",
     "10.20.30.100:6443", ["10.20.29.7/22"], "10.20.29.7"),
    ("no match: warning, node-ip left to kubelet",
     "10.20.30.100", ["192.168.1.5/24", "172.16.5.5/16"], None),
    ("no interfaces at all: warning, node-ip left to kubelet",
     "10.20.30.100", [], None),
    ("unparseable prefix length is skipped, not matched",
     "10.20.30.100", ["10.20.30.7/xx", "10.20.29.7/22"], "10.20.29.7"),
    ("unparseable VIP: no selection, warning",
     "not-an-ip", ["10.20.30.7/24"], None),
    ("leading-zero octets are decimal, not octal",
     "010.020.030.100", ["10.20.30.7/24"], "10.20.30.7"),
]


def render(chart_dir=CHART):
    """The rendered join.sh of the REAL workerUserData helper, as text."""
    if shutil.which("helm") is None:
        raise Void("helm is not on PATH; the chart cannot be rendered")
    if shutil.which("bash") is None:
        raise Void("bash is not on PATH; the block cannot be executed")
    with tempfile.TemporaryDirectory() as td:
        dst = os.path.join(td, "chart")
        shutil.copytree(chart_dir, dst)
        with open(os.path.join(dst, "templates", "zz-worker-node-ip-probe.yaml"), "w") as f:
            f.write(PROBE)
        r = subprocess.run(["helm", "template", "probe", dst], capture_output=True, text=True)
    if r.returncode != 0:
        raise Void("helm template failed: %s" % r.stderr.strip()[:300])
    for d in yaml.safe_load_all(r.stdout):
        if isinstance(d, dict) and d.get("kind") == "ConfigMap" \
                and (d.get("metadata") or {}).get("name") == PROBE_NAME:
            cc = yaml.safe_load(d["data"]["userdata"])
            for wf in cc.get("write_files") or []:
                if wf.get("path") == "/etc/kubernetes/join.sh":
                    return wf["content"]
            raise Void("rendered cloud-config has no /etc/kubernetes/join.sh")
    raise Void("the probe ConfigMap is not in the render")


def extract_block(joinsh):
    """The node-ip block: from `ip4_to_int() {` to the `fi` closing the NODE_IP branch."""
    start = re.search(r"^ip4_to_int\(\) \{$", joinsh, re.M)
    if not start:
        raise Void("`ip4_to_int() {` not found in the rendered join.sh")
    use = re.search(r"^NODE_IP=\$\(derive_node_ip ", joinsh[start.start():], re.M)
    if not use:
        raise Void("the `NODE_IP=$(derive_node_ip ...)` call is not in the rendered join.sh")
    tail = joinsh[start.start() + use.start():]
    end = re.search(r"^fi$", tail, re.M)
    if not end:
        raise Void("the `fi` closing the node-ip branch is not in the rendered join.sh")
    block = joinsh[start.start(): start.start() + use.start() + end.end()]
    if block.count(KUBELET_PATH) != 1:
        raise Void("expected exactly one %s in the block, found %d"
                   % (KUBELET_PATH, block.count(KUBELET_PATH)))
    return block


def run_case(block, vip, cidrs):
    """-> (selected address or None, warning printed?). Executes the block under bash."""
    lines = [LO] + [ipline(i + 2, "eth%d" % i, c) for i, c in enumerate(cidrs)]
    with tempfile.TemporaryDirectory() as td:
        kubelet = os.path.join(td, "kubelet")
        script = os.path.join(td, "run.sh")
        fixture = os.path.join(td, "ip.out")
        with open(fixture, "w") as f:
            f.write("\n".join(lines) + "\n")
        body = block.replace(KUBELET_PATH, kubelet)
        with open(script, "w") as f:
            # The stub refuses any invocation other than the one the block should make,
            # so a changed command line is a finding rather than a silently-served fixture.
            f.write('ip() { [ "$*" = "-o -4 addr show" ] || { echo "unexpected ip args: $*" >&2; exit 99; }\n'
                    '  cat "%s"; }\nVIP=%s\n%s\n' % (fixture, shell_quote(vip), body))
        r = subprocess.run(["bash", script], capture_output=True, text=True)
        if r.returncode != 0:
            raise AssertionError("block exited %d: %s" % (r.returncode, r.stderr.strip()[:200]))
        chosen = None
        if os.path.exists(kubelet):
            m = re.fullmatch(r"KUBELET_EXTRA_ARGS=--node-ip=(\S+)\n", open(kubelet).read())
            if not m:
                raise AssertionError("unexpected kubelet defaults content")
            chosen = m.group(1)
        warned = "WARNING: no address in the same subnet" in r.stderr
        return chosen, warned


def shell_quote(s):
    return "'" + s.replace("'", "'\"'\"'") + "'"


def evaluate(block):
    """Run the case table -> list of (name, ok, detail)."""
    out = []
    for name, vip, cidrs, want in CASES:
        try:
            got, warned = run_case(block, vip, cidrs)
        except AssertionError as e:
            out.append((name, False, str(e)))
            continue
        ok = got == want and warned == (want is None)
        out.append((name, ok, "want %s got %s warned=%s" % (want, got, warned)))
    return out


def oracle_sweep(block, n=120):
    """Random single-interface cases judged by Python's ipaddress module."""
    rnd = random.Random(128)
    res = []
    for i in range(n):
        plen = rnd.randint(9, 30)
        # 10/8 only: a random 127.x VIP legitimately matches the stub's loopback line.
        vip = ipaddress.IPv4Address(0x0A000000 | rnd.getrandbits(24))
        net = ipaddress.IPv4Network("%s/%d" % (vip, plen), strict=False)
        if rnd.random() < 0.5:
            addr = ipaddress.IPv4Address(int(net.network_address) + rnd.randrange(net.num_addresses))
        else:
            addr = ipaddress.IPv4Address(int(vip) ^ (1 << rnd.randint(0, 31)))
        want = str(addr) if addr in net else None
        try:
            got, warned = run_case(block, str(vip), ["%s/%d" % (addr, plen)])
            ok = got == want and warned == (want is None)
            detail = "vip=%s iface=%s/%d want=%s got=%s" % (vip, addr, plen, want, got)
        except AssertionError as e:
            ok, detail = False, str(e)
        res.append(("oracle #%d" % i, ok, detail))
    return res


def mutate_to_old(block):
    """The shipped block with the 0.16.1 /24 string-prefix derivation spliced back in."""
    call = 'NODE_IP=$(derive_node_ip "$VIP" || true)'
    if block.count(call) != 1:
        raise Void("cannot build the negative control: call line count != 1")
    return block.replace(call, OLD_DERIVATION)


def run():
    block = extract_block(render())
    results = evaluate(block) + oracle_sweep(block)
    bad = [r for r in results if not r[1]]
    print("check-worker-node-ip: %d case(s) against the RENDERED worker join.sh" % len(results))
    for name, ok, detail in results[:len(CASES)]:
        print("  %-4s %s  [%s]" % ("ok" if ok else "FAIL", name, detail))
    print("  oracle sweep: %d random case(s), %d failing"
          % (len(results) - len(CASES), sum(1 for r in bad if r[0].startswith("oracle"))))
    for name, _, detail in bad:
        if name.startswith("oracle"):
            print("  FAIL %s  [%s]" % (name, detail))
    if bad:
        print("FINDING: %d case(s) failed" % len(bad))
        return 1
    print("OK")
    return 0


def selftest():
    block = extract_block(render())
    passed = total = 0

    def assertion(label, cond):
        nonlocal passed, total
        total += 1
        passed += bool(cond)
        print("  %s %s" % ("ok  " if cond else "FAIL", label))

    # 1. the shipped block passes its own table
    shipped = evaluate(block)
    assertion("shipped block passes all %d table cases" % len(shipped), all(r[1] for r in shipped))
    sweep = oracle_sweep(block, 60)
    assertion("shipped block agrees with the ipaddress oracle on %d random cases" % len(sweep),
              all(r[1] for r in sweep))

    # 2. NEGATIVE CONTROL: the 0.16.1 derivation must FAIL the cases that motivated the fix
    old = {r[0]: r[1] for r in evaluate(mutate_to_old(block))}
    must_fail = [c[0] for c in CASES if c[0].startswith(("/22: third octet", "/22: top of the block",
                                                         "/16: third octet", "same first three octets, different /26",
                                                         "VIP given with a port"))]
    # "VIP given with a port" on a /22 address is a /22 case; the old code fails it too
    assertion("negative control: old /24 code FAILS the %d widened-mask cases" % len(must_fail),
              len(must_fail) >= 4 and all(not old[n] for n in must_fail))
    assertion("negative control: old /24 code still passes the /24 lab case (the table is not just failing everything)",
              old["/24 lab (today): data NIC beside an OOB NIC"])
    assertion("negative control: old /24 code fails the oracle sweep",
              not all(r[1] for r in oracle_sweep(mutate_to_old(block), 60)))

    # 3. the harness can see a wrong selection, not only a missing one
    wrong = block.replace('echo "$addr"\n', 'echo "9.9.9.9"\n', 1) if 'echo "$addr"\n' in block else None
    assertion("a block that selects the wrong address is caught",
              wrong is not None and not all(r[1] for r in evaluate(wrong)))

    # 4. VOID paths: an absent block must not read as a pass
    for label, text in [("no ip4_to_int", "echo hi\n"),
                        ("no call", "ip4_to_int() {\n  :\n}\n"),
                        ("no closing fi", "ip4_to_int() {\n:\n}\nNODE_IP=$(derive_node_ip x)\n")]:
        try:
            extract_block(text)
            assertion("VOID when the render has %s" % label, False)
        except Void:
            assertion("VOID when the render has %s" % label, True)

    print("selftest: %d cases, %d failure(s)" % (total, total - passed))
    print("SELF-TEST: %d/%d assertions passed" % (passed, total))
    return 0 if passed == total else 1


_WANT_SELFTEST = False

if __name__ == "__main__":
    # MODE FLAGS: both spellings accepted, anything else REFUSED (see check-screen-flags.py).
    _ALLOWED = {"--self-test", "--selftest"}
    _unknown = [a for a in sys.argv[1:] if a not in _ALLOWED]
    if _unknown:
        sys.stderr.write("usage: %s [%s]\n" % (sys.argv[0], "|".join(sorted(_ALLOWED))))
        sys.stderr.write("refusing unrecognised argument(s): %s\n" % " ".join(_unknown))
        sys.stderr.write("a mistyped mode flag would otherwise run the OTHER mode and exit 0.\n")
        sys.exit(64)
    _WANT_SELFTEST = bool({"--self-test", "--selftest"} & set(sys.argv[1:]))
    try:
        if _WANT_SELFTEST:
            sys.exit(selftest())
        sys.exit(run())
    except Void as v:
        print("VOID: %s" % v)
        print("NOT a pass.")
        sys.exit(2)
