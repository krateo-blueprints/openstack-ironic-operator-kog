#!/usr/bin/env bash
# FALSIFICATION ARM for oasgen #561: does a Configuration-sourced header reach the
# wire at oasgen/rdc 0.43.0, the last release BEFORE the #304 fix?
#
# WHY THIS ARM EXISTS. On 2026-11-11 this corpus measured, at rdc 0.54.1, that a
# PortConfiguration set to a distinctive 1.95 puts that value on the wire twice with
# zero 406s, with the static per-verb headers removed. That settled the PRACTICAL
# question. It did NOT test oasgen-provider's EXPLANATION of why it used to be
# broken -- that the generator emitted `header` (singular, the OpenAPI location)
# while rest-dynamic-controller read `headers` (plural) in builder.go
# applyConfigSpec, fixed in 0.44.0 (#304) and 0.50.0 (#305/#392).
#
# This arm can FALSIFY that account rather than confirm it:
#
#   at 0.43.0, run EXACTLY the 0.54.1 experiment
#   EXPECT:  the distinctive value does NOT reach the wire
#   IF IT DOES: the plural-key mechanism was not the cause, and oasgen's account
#               on #561 is wrong
#
# One tag, one variable. A version diff varies everything between two tags; holding
# the experiment constant and varying only the tag is the stronger design, and it
# was oasgen's own suggestion that it be able to refute them.
#
# WHY A THROWAWAY CLUSTER AND NOT A DOWNGRADE. oasgen does not regenerate an
# existing CRD on upgrade or downgrade (#453). A 0.57.0 install downgraded to
# 0.43.0 therefore runs a 0.43.0 controller against 0.57.0-shaped CRDs -- a state no
# real install has ever been in, so the arm would measure an artifact of the method.
# A fresh install at 0.43.0 is the only honest subject.
#
# RUN WITH BASH, NOT ZSH. Every command below is a bash ARRAY. A string variable
# holding a command with spaces is not word-split by zsh, so `$K get ...` fails with
# "command not found" -- or worse, returns nothing inside a $(...) that the caller
# reads as "no results". That produced a reading saying this lab was EMPTY earlier
# today: 0 CRDs, 0 RestDefinitions, 0 CRs, from a run in which not one command
# executed.
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
for b in kind kubectl helm docker python3; do
  command -v "$b" >/dev/null || { echo "PRECHECK FAILED: $b not on PATH"; exit 1; }
done

# --preflight-only runs every guard and STOPS before `kind create`. Added after I
# tested the guards by running the script and piping to `head`, which stopped it only
# because the closed pipe raised SIGPIPE -- and once it did NOT stop in time and
# created a real cluster I then had to delete, while a peer had a prior claim on the
# box. A script that creates infrastructure needs a way to exercise its refusals that
# does not depend on a signal arriving fast enough.
PREFLIGHT_ONLY=0
[ "${1:-}" = "--preflight-only" ] && { PREFLIGHT_ONLY=1; shift; }

# POLICY GATE, 2026-11-11. Diego's directive, scope all sessions: stop using kind
# clusters, use disposable GKE created per run and deleted-and-verified after; for
# heavy local suites, a disposable Compute instance (snowplow is building a runner
# that ships the repo at a SHA as a git BUNDLE, so no credentials reach the VM).
#
# The reason is measured, not stylistic: the shared laptop reached load 148, and that
# contention crashlooped one cluster's kube-controller-manager 143 TIMES while every
# object read stayed correct -- so it corrupted durations and garbage collection
# invisibly, and two latency figures had already been handed to another session before
# it was found. See docs/IMPLEMENTATION.md and the kcm-crashloop note.
#
# This script creates a kind cluster, so it now refuses by default. `I_HAVE_NO_GKE=1`
# exists to make a violation EXPLICIT rather than to permit it casually: if you set it,
# you are asserting you have no disposable GKE available and have accepted the cost to
# everyone else sharing the host.
if [ "${I_HAVE_NO_GKE:-0}" != "1" ]; then
  printf '%s\n' \
    "REFUSED: this probe creates a KIND cluster, and kind is no longer the sanctioned" \
    "         path (Diego, 2026-11-11, all sessions). Use a disposable GKE cluster," \
    "         created per run and deleted-and-verified after; for a heavy local suite" \
    "         ask snowplow for a disposable Compute instance." \
    "" \
    "         The arm itself is portable: it needs a Kubernetes cluster, oasgen at the" \
    "         tag under test, one RestDefinition and the header-recording mock. Nothing" \
    "         in it depends on kind." \
    "" \
    "         To run it on kind anyway: I_HAVE_NO_GKE=1 $0" \
    "         That flag makes the violation explicit. It does not make it fine." >&2
  exit 4
fi

CLUSTER=${CLUSTER:-oasgen043-arm}
KC="local/kubeconfig.${CLUSTER}"      # isolated: ~/.kube/config is never touched
CTX="kind-${CLUSTER}"
NS=probe
OASGEN_VER=${OASGEN_VER:-0.43.0}
OCI=oci://ghcr.io/krateo-platformops/charts
DISTINCT_CONFIG=1.95                  # only the Configuration can produce this
DISTINCT_STATIC=1.77                  # only a static per-verb header can produce this
LOAD_CEILING=${LOAD_CEILING:-25}

K=(kubectl --kubeconfig "$KC" --context "$CTX")
H=(helm --kubeconfig "$KC" --kube-context "$CTX")
say () { printf '%s %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }

# ---------------------------------------------------------------- preflight ----
say "== preflight =="
# Split on WHITESPACE ONLY, then convert the decimal comma. Splitting on `,` as
# well -- which is what this did first -- discards the decimals under an it_IT
# locale: `39,41` parsed as `39`. Harmless far from the ceiling and a silent
# misread next to it. `sysctl -n vm.loadavg` is no help: it prints `{ 39,41 ... }`
# with the same locale comma.
# GATE ON THE 15-MINUTE AVERAGE, not the 1-minute one. Caught by the installer
# session 2026-11-11: my first version read the 1-min figure, which is the noisiest
# of the three and the one most likely to be a TROUGH BETWEEN BURSTS. It reported
# 21.2 -- below the ceiling -- while two other control planes sat at 97.7% and
# 92.96% and the 15-min figure was 24.4, i.e. AT the ceiling.
#
# This run lasts 15-20 minutes, so the 15-minute average is the matching timescale:
# the question is not "is the box quiet right now" but "has the box been quiet long
# enough that it will probably stay quiet for the length of my run". Gating on the
# 1-min figure answers the wrong question and answers it optimistically.
#
# Same shape as the defect this probe was written to avoid: the instrument chosen
# determines the answer, and the convenient instrument said go.
read -r L1 L5 L15 <<< "$(uptime | sed 's/.*averages*: *//' | awk '{print $1, $2, $3}')"
norm () { local v="${1%,}"; printf '%s' "${v/,/.}"; }   # strip LIST sep, then DECIMAL comma
L1=$(norm "$L1"); L5=$(norm "$L5"); L15=$(norm "$L15")
# ...and gate on the MAXIMUM of the three, not on any single one. Gating on the
# 15-min alone has the mirror-image defect of gating on the 1-min: it permits a
# RISING load. My own test table caught it --
#     1m=40.00  5m=30.00  15m=20.00  ->  PROCEED
# which is a box that is busy NOW and was quiet an hour ago, i.e. exactly when not
# to start. The max refuses both shapes: a trough between bursts (1m low, 15m high)
# and a spike on a recently-quiet box (1m high, 15m low).
# LC_ALL=C on BOTH the max and the comparison. Without it awk PRINTS the max with
# a locale decimal comma ("25,27") and the comparison then re-parses that string --
# which happens to work under it_IT, because its awk reads "25,27" as 25.27, and
# TRUNCATES to 25 under C. A guard that is correct only because of the ambient
# locale is the third locale defect in this one function; pin it instead of relying
# on it.
LOAD=$(LC_ALL=C awk -v a="$L1" -v b="$L5" -v c="$L15" 'BEGIN{m=a+0; if(b+0>m)m=b+0; if(c+0>m)m=c+0; printf "%.2f", m}')
# Both must happen and in this order. Converting first turns "2.50," into "2.50."
# which awk still reads as 2.50, so the comparison survives while the figure PRINTED
# in the abort message is wrong -- and an abort message is exactly what someone
# reasons from later.
say "   load 1-min=$L1  5-min=$L5  15-min=$L15   gating on MAX=$LOAD, ceiling $LOAD_CEILING"
LC_ALL=C awk -v l="$LOAD" -v c="$LOAD_CEILING" 'BEGIN{exit !(l+0>c+0)}' && {
  say "   ABORT: max load above the ceiling. Several sessions share this box and a"
  say "          kind create here would hurt them. Re-run when it drops, or pass"
  say "          LOAD_CEILING=<n> if you have decided otherwise."
  exit 2
}
FREE=$(docker run --rm --privileged alpine sh -c 'df -BG /etc/hosts' 2>/dev/null | tail -1 | awk '{gsub("G","",$4); print $4}')
say "   docker VM free: ${FREE}G"
[ "${FREE:-0}" -lt 15 ] && { say "   ABORT: under 15G free on the shared Docker disk"; exit 2; }
kind get clusters 2>/dev/null | grep -qx "$CLUSTER" && {
  say "   ABORT: cluster $CLUSTER already exists. Delete it deliberately first."; exit 2; }

if [ "$PREFLIGHT_ONLY" = "1" ]; then
  say "== --preflight-only: every guard passed, stopping before kind create =="
  exit 0
fi

# ---------------------------------------------------------------- cluster ------
say "== create the throwaway cluster (isolated kubeconfig) =="
mkdir -p local
kind create cluster --name "$CLUSTER" --kubeconfig "$KC" --wait 180s 2>&1 | tail -3 | sed 's/^/   /'
"${K[@]}" get nodes 2>&1 | sed 's/^/   /'
"${K[@]}" create namespace "$NS" 2>&1 | sed 's/^/   /'

# ---------------------------------------------------------------- the mock -----
# THE INSTRUMENT. Records every request's microversion header to stdout, so the
# measurement is "which values reached the wire", not "did anything break".
say "== deploy the header-recording mock =="
cat > /tmp/${CLUSTER}-mock.py <<'PY'
import json, os
from http.server import BaseHTTPRequestHandler, HTTPServer
MAC = "aa:bb:cc:dd:ee:01"
PORT = {"uuid": "11111111-1111-1111-1111-111111111111", "address": MAC,
        "node_uuid": "22222222-2222-2222-2222-222222222222", "pxe_enabled": True,
        "extra": {}, "local_link_connection": {}, "physical_network": None,
        "portgroup_uuid": None}
class H(BaseHTTPRequestHandler):
    def _go(self, verb):
        mv = self.headers.get("X-Openstack-Ironic-Api-Version")
        # ONE line per request, with the header value or the literal NONE. This is
        # the entire measurement; everything else is scaffolding.
        print(f"HDR verb={verb} path={self.path} microversion={mv if mv else 'NONE'}", flush=True)
        body = {"ports": [PORT]} if (verb == "GET" and self.path.rstrip("/").endswith("/v1/ports")) else PORT
        code = 201 if verb == "POST" else 200
        if verb == "DELETE":
            self.send_response(204); self.end_headers(); return
        self.send_response(code)
        self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(json.dumps(body).encode())
    def do_GET(self): self._go("GET")
    def do_POST(self): self._go("POST")
    def do_DELETE(self): self._go("DELETE")
    def do_PATCH(self): self._go("PATCH")
    def do_PUT(self): self._go("PUT")
    def log_message(self, *a): pass
print("headermock up on :8080", flush=True)
HTTPServer(("0.0.0.0", 8080), H).serve_forever()
PY
"${K[@]}" create configmap headermock-src -n "$NS" --from-file=mock.py=/tmp/${CLUSTER}-mock.py 2>&1 | sed 's/^/   /'
"${K[@]}" apply -n "$NS" -f - <<'EOF' 2>&1 | sed 's/^/   /'
apiVersion: apps/v1
kind: Deployment
metadata: {name: headermock}
spec:
  replicas: 1
  selector: {matchLabels: {app: headermock}}
  template:
    metadata: {labels: {app: headermock}}
    spec:
      containers:
        - name: mock
          image: python:3.12-alpine
          command: ["python3", "/src/mock.py"]
          ports: [{containerPort: 8080}]
          volumeMounts: [{name: src, mountPath: /src}]
      volumes: [{name: src, configMap: {name: headermock-src}}]
---
apiVersion: v1
kind: Service
metadata: {name: headermock}
spec:
  selector: {app: headermock}
  ports: [{port: 8080, targetPort: 8080}]
EOF
"${K[@]}" -n "$NS" rollout status deploy/headermock --timeout=240s 2>&1 | tail -1 | sed 's/^/   /'

say "== MOCK CONTROL: it must record a header it is sent, and NONE when it is not =="
"${K[@]}" -n "$NS" run mockcheck --rm -i --restart=Never --image=curlimages/curl:8.11.0 --quiet -- sh -c \
  'curl -s -o /dev/null -H "X-OpenStack-Ironic-API-Version: 9.99" http://headermock.probe.svc.cluster.local:8080/v1/ports;
   curl -s -o /dev/null http://headermock.probe.svc.cluster.local:8080/v1/ports' >/dev/null 2>&1
sleep 3
MOCKLOG=$("${K[@]}" logs -n "$NS" deploy/headermock --tail=-1 2>/dev/null)
echo "$MOCKLOG" | grep '^HDR' | tail -4 | sed 's/^/   /'
echo "$MOCKLOG" | grep -q 'microversion=9.99' || { say "   ABORT: the mock did not record a header it was sent -- the instrument is broken"; exit 1; }
echo "$MOCKLOG" | grep -q 'microversion=NONE' || { say "   ABORT: the mock did not record NONE for a header-less request"; exit 1; }
say "   instrument verified: records a sent header AND records NONE. Both directions."

# ---------------------------------------------------------------- the arms -----
say "== install oasgen $OASGEN_VER =="
# ONLY the tag. The 0.43.0 chart's defaults are registry=ghcr.io and
# repository=krateo-platformops/rest-dynamic-controller -- a DIFFERENT split from
# 0.54.1, where the registry carries the org. Passing the 0.54.1 --set flags here
# would build ghcr.io/krateo-platformops/krateo-platformops/rest-dynamic-controller.
"${H[@]}" upgrade --install krateo-oasgen-provider-crds "$OCI/oasgen-provider-crds" \
    -n krateo-system --create-namespace --version "$OASGEN_VER" --wait --timeout 5m 2>&1 | grep -E "^STATUS|Error" | sed 's/^/   /'
"${H[@]}" upgrade --install krateo-oasgen-provider "$OCI/oasgen-provider" \
    -n krateo-system --version "$OASGEN_VER" --set "rdc.image.tag=$OASGEN_VER" \
    --wait --timeout 8m 2>&1 | grep -E "^STATUS|Error" | sed 's/^/   /'
say "   provider image: $("${K[@]}" get deploy -n krateo-system -o jsonpath='{.items[0].spec.template.spec.containers[0].image}' 2>/dev/null)"

say "== OAS configmap, servers -> the mock =="
python3 - <<'PY'
import yaml, pathlib
d = yaml.safe_load(pathlib.Path("oas/ironic-port.yaml").read_text())
d["servers"] = [{"url": "http://headermock.probe.svc.cluster.local:8080",
                 "description": "header-recording mock"}]
pathlib.Path("/tmp/arm043-port.yaml").write_text(yaml.safe_dump(d, sort_keys=False))
PY
"${K[@]}" create configmap port-oas -n "$NS" --from-file=port.yaml=/tmp/arm043-port.yaml \
    --dry-run=client -o yaml | "${K[@]}" apply -f - >/dev/null 2>&1 && say "   configmap ok"

say "== RestDefinition with ALL static per-verb headers REMOVED =="
python3 - <<'PY'
import yaml, pathlib
d = yaml.safe_load(pathlib.Path("manifests/restdefinition-port.yaml").read_text())
d["metadata"].update(name="arm-port", namespace="probe")
d["spec"]["oasPath"] = "configmap://probe/port-oas/port.yaml"
d["spec"]["resourceGroup"] = "arm.ogen.krateo.io"
removed = sum(1 for v in d["spec"]["resource"]["verbsDescription"] if v.pop("headers", None) is not None)
cf = [c["fromOpenAPI"]["name"] for c in d["spec"]["resource"]["configurationFields"]]
# ASSERT THE SETUP, because a setup that cannot produce the condition reports a pass.
assert removed == 4, f"expected 4 static header blocks removed, got {removed}"
assert "X-OpenStack-Ironic-API-Version" in cf, cf
pathlib.Path("/tmp/arm043-rd.yaml").write_text(yaml.safe_dump(d, sort_keys=False))
print(f"   removed {removed} static header blocks; configurationFields={cf}")
PY
[ $? -eq 0 ] || { say "   ABORT: the RestDefinition setup assertions failed"; exit 1; }
"${K[@]}" apply -f /tmp/arm043-rd.yaml >/dev/null 2>&1
for i in $(seq 1 40); do
  r=$("${K[@]}" get restdefinitions.ogen.krateo.io -n "$NS" arm-port -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)
  [ "$r" = "True" ] && { say "   RestDefinition Ready after $i polls"; break; }
  sleep 8
done
[ "${r:-}" = "True" ] || { say "   ABORT: RestDefinition never became Ready"; exit 1; }

say "== READ the generated Configuration schema and ASSERT its shape =="
# Never guess this. At 0.54.1 `spec.configuration` does not exist at all, and the
# 0.43.0 shape IS the subject of the experiment. A guessed shape configures nothing,
# puts nothing on the wire, and that reads as CONFIRMING the hypothesis -- the setup
# manufacturing the expected answer. So assert the shape, and abort if it moved.
"${K[@]}" get crd portconfigurations.arm.ogen.krateo.io -o json 2>/dev/null > /tmp/arm043-cfgcrd.json
# `||` must sit on the heredoc's COMMAND line, not after the terminator. Putting it
# after `PY` is a syntax error -- and bash executes incrementally, so the script runs
# normally until it REACHES the error. A late syntax error in a script that creates a
# cluster means it creates the cluster and then dies. `bash -n` is the gate; a
# successful --preflight-only run is not.
python3 - /tmp/arm043-cfgcrd.json <<'PY' || { say "   ABORT: the Configuration schema is not the #304 shape; the arm would be vacuous"; exit 1; }
import sys, json
v = json.load(open(sys.argv[1]))["spec"]["versions"][0]
sp = v["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]
assert "configuration" in sp, "no spec.configuration at this version: %s" % sorted(sp)
h = sp["configuration"]["properties"]
assert "header" in h, "expected the SINGULAR header key (the #304 shape), got %s" % sorted(h)
verbs = sorted(h["header"]["properties"])
print("   spec.configuration.header per-verb keys: %s" % verbs)
assert "findby" in verbs, verbs
PY

say "== PortConfiguration: #304 shape, 'header' SINGULAR, per verb, = $DISTINCT_CONFIG =="
"${K[@]}" create secret generic ironic-token -n "$NS" --from-literal=token=dummy \
    --dry-run=client -o yaml | "${K[@]}" apply -f - >/dev/null 2>&1
"${K[@]}" apply -f - >/dev/null 2>&1 <<EOF
apiVersion: arm.ogen.krateo.io/v1alpha1
kind: PortConfiguration
metadata: {name: arm-endpoint, namespace: $NS}
spec:
  authentication:
    apiKey:
      header: X-Auth-Token
      tokenRef: {name: ironic-token, namespace: $NS, key: token}
  configuration:
    header:
      findby: {X-OpenStack-Ironic-API-Version: "$DISTINCT_CONFIG"}
      get:    {X-OpenStack-Ironic-API-Version: "$DISTINCT_CONFIG"}
      create: {X-OpenStack-Ironic-API-Version: "$DISTINCT_CONFIG"}
      delete: {X-OpenStack-Ironic-API-Version: "$DISTINCT_CONFIG"}
EOF
STORED=$("${K[@]}" get portconfigurations.arm.ogen.krateo.io -n "$NS" arm-endpoint -o jsonpath='{.spec.configuration}' 2>/dev/null)
say "   stored back: ${STORED:0:120}..."
case "$STORED" in *"$DISTINCT_CONFIG"*) ;; *) say "   ABORT: the Configuration did not store $DISTINCT_CONFIG -- it was pruned"; exit 1;; esac

reqs () { "${K[@]}" logs -n "$NS" deploy/headermock --tail=-1 2>/dev/null | grep -c '^HDR'; }
hdrs () { "${K[@]}" logs -n "$NS" deploy/headermock --tail=-1 2>/dev/null | grep -c "microversion=$1"; }
PV=$("${K[@]}" get crd ports.arm.ogen.krateo.io -o jsonpath='{.spec.versions[0].name}' 2>/dev/null)

say "== ARM 1: one Port CR, gated on the MOCK BEING CALLED, never on elapsed time =="
B1=$(reqs)
"${K[@]}" apply -f - >/dev/null 2>&1 <<EOF
apiVersion: arm.ogen.krateo.io/$PV
kind: Port
metadata: {name: arm-one, namespace: $NS}
spec:
  address: "aa:bb:cc:dd:ee:01"
  node_uuid: "22222222-2222-2222-2222-222222222222"
  pxe_enabled: true
  configurationRef: {name: arm-endpoint, namespace: $NS}
EOF
for i in $(seq 1 45); do [ "$(reqs)" -gt "$B1" ] && break; sleep 4; done
[ "$(reqs)" -gt "$B1" ] || { say "   ARM VOID: the controller never called the mock"; exit 3; }
sleep 15
say "   arm 1 microversions:"; "${K[@]}" logs -n "$NS" deploy/headermock --tail=-1 2>/dev/null | grep '^HDR' | tail -n "$(( $(reqs) - B1 ))" | awk '{print $NF}' | sort | uniq -c | sed 's/^/      /'

say "== ARM 2 (POSITIVE CONTROL): static per-verb header = $DISTINCT_STATIC on the SAME CR =="
B2=$(reqs)
python3 - "$DISTINCT_STATIC" <<'PY'
import yaml, pathlib, sys
d = yaml.safe_load(pathlib.Path("/tmp/arm043-rd.yaml").read_text())
for v in d["spec"]["resource"]["verbsDescription"]:
    v["headers"] = [{"name": "X-OpenStack-Ironic-API-Version", "value": sys.argv[1]}]
pathlib.Path("/tmp/arm043-rd-static.yaml").write_text(yaml.safe_dump(d, sort_keys=False))
PY
"${K[@]}" apply -f /tmp/arm043-rd-static.yaml >/dev/null 2>&1
"${K[@]}" patch ports."$PV".arm.ogen.krateo.io -n "$NS" arm-one --type=merge -p '{"spec":{"extra":{"arm":"static"}}}' >/dev/null 2>&1
for i in $(seq 1 45); do [ "$(reqs)" -gt "$B2" ] && break; sleep 4; done
A2=$(reqs); sleep 10
say "   arm 2 microversions:"; "${K[@]}" logs -n "$NS" deploy/headermock --tail=-1 2>/dev/null | grep '^HDR' | tail -n "$(( $(reqs) - B2 ))" | awk '{print $NF}' | sort | uniq -c | sed 's/^/      /'

say "============== VERDICT =============="
C=$(hdrs "$DISTINCT_CONFIG"); T=$(hdrs "$DISTINCT_STATIC")
echo "   Configuration-sourced $DISTINCT_CONFIG on the wire: $C"
echo "   static per-verb       $DISTINCT_STATIC on the wire: $T"
echo "   NONE: $(hdrs NONE)    total requests: $(reqs)"
if [ "$A2" -le "$B2" ]; then
  echo "   ARM 2 VOID: no request during the static arm, so 'absent' means nothing."
elif [ "$T" -eq 0 ]; then
  echo "   ARM VOID: the STATIC header never reached the wire either, so the pipeline"
  echo "             was never live and arm 1 proves nothing."
elif [ "$C" -gt 0 ]; then
  echo "   *** oasgen's #561 ACCOUNT IS WRONG: $DISTINCT_CONFIG reached the wire at $OASGEN_VER. ***"
else
  echo "   oasgen's #561 account SURVIVES: $DISTINCT_CONFIG absent while the static"
  echo "   $DISTINCT_STATIC reached the wire ($T times), so the Configuration route was"
  echo "   genuinely inert at $OASGEN_VER."
fi

say "== cleanup is deliberate, not automatic: kind delete cluster --name $CLUSTER =="
