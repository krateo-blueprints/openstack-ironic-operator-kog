#!/bin/bash
# Can a NEW USER still get from nothing to a provisioned node?
#
# Seven PRs merged today touched manifests, charts and the Makefile. Every one of them
# sits on this path and none of them exercised it: the last full fresh install was this
# morning, BEFORE the NodePower fix, the discovery deprecation and the docs changes.
#
# Each step asserts the state it produced rather than assuming the previous command
# worked. A step that cannot reach its state makes the run VOID, not a pass.
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
cd /Users/diegobraga/openstack-ironic-operator || exit 1
C=e2e-verify
KC="local/kubeconfig.$C"; NS=openstack
K="kubectl --kubeconfig=$KC --context=kind-$C"
L(){ echo "$(date -u +%H:%M:%SZ) $*"; }
KEEP=0; FAIL=0

for t in kind kubectl helm docker python3 make jq curl; do
  command -v "$t" >/dev/null || { echo "PRECHECK FAILED: $t"; exit 1; }
done
[ "$C" = "ironic-kog" ] && { echo "REFUSING: protected lab"; exit 1; }
lm=$(sysctl -n vm.loadavg | tr -d '{}' | awk '{print $1}' | tr ',' '.')
L "load 1m=$lm"
awk -v l="$lm" 'BEGIN{exit !(l>8)}' && { L "REFUSED: load $lm > 8 (shared host)"; exit 1; }

L "== STEP 1: make local-up on a throwaway cluster, exactly as the README tells a user =="
if ! make local-up KIND_CLUSTER="$C" >"/private/tmp/claude-501/-Users-diegobraga-openstack-ironic-operator/2289beaf-6978-422a-a32a-dd474eba8ce5/scratchpad/e2e-build.log" 2>&1; then
  L "   local-up FAILED -- this alone answers the question. Tail:"
  tail -25 "/private/tmp/claude-501/-Users-diegobraga-openstack-ironic-operator/2289beaf-6978-422a-a32a-dd474eba8ce5/scratchpad/e2e-build.log" | sed 's/^/      /'
  exit 1
fi
L "   local-up succeeded"

L ""
L "== STEP 2: the stack a user needs, each ASSERTED =="
rdn=$($K -n $NS get restdefinitions.ogen.krateo.io -o name 2>/dev/null | wc -l | tr -d ' ')
L "   RestDefinitions: $rdn (want 7)"
[ "$rdn" = "7" ] || { L "   WRONG COUNT"; FAIL=1; }
# POLL, do not read once. The first version read this ONE SECOND after local-up
# returned and saw 4 of 7 still False -- then the node walked to `active` normally.
# That is a reconciliation that had not settled, not a defect, and reporting it as
# FAIL would have been the third too-early read of the day. Gate on the objects, the
# way this script already does for provision states.
notready=""
for i in $(seq 1 30); do
  notready=""
  for r in $($K -n $NS get restdefinitions.ogen.krateo.io -o name 2>/dev/null); do
    st=$($K -n $NS get "$r" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)
    [ "$st" = "True" ] || notready="$notready $(basename $r)=$st"
  done
  [ -z "$notready" ] && { L "   all 7 Ready (poll $i)"; break; }
  sleep 10
done
if [ -n "$notready" ]; then L "   NOT Ready:$notready"; else L "   all 7 RestDefinitions Ready"; fi

# the six Configuration singletons. A missing one is why no fresh install could
# provision before #64, and nothing screens that they are all applied.
L "   Configuration singletons:"
miss=0
for kind in baremetalnodeconfigurations portconfigurations nodepowerconfigurations \
            nodeprovisionconfigurations allocationconfigurations deploytemplateconfigurations \
            portgroupconfigurations; do
  got=$($K -n $NS get "${kind}.v1alpha1.baremetal.ogen.krateo.io" ironic-endpoint -o name 2>/dev/null)
  if [ -n "$got" ]; then printf "     %-32s present\n" "$kind"
  else printf "     %-32s MISSING\n" "$kind"; miss=$((miss+1)); fi
done
[ "$miss" -gt 0 ] && L "   $miss Configuration(s) missing"

L ""
L "== STEP 3: nothing -> enroll -> manageable -> available -> active =="
cat <<EOF | $K apply -f - >/dev/null 2>&1
apiVersion: baremetal.ogen.krateo.io/v1-0-0
kind: BaremetalNode
metadata: { name: e2e01, namespace: $NS }
spec:
  configurationRef: { name: ironic-endpoint, namespace: $NS }
  name: e2e01
  driver: fake-hardware
  driver_info: {}
  instance_info:
    image_source: "http://example.invalid/image.qcow2"
    image_checksum: "0"
EOF
pstate(){ $K -n $NS get baremetalnodes.v1-0-0.baremetal.ogen.krateo.io e2e01 -o jsonpath='{.status.provision_state}' 2>/dev/null; }
await(){ for i in $(seq 1 "$3"); do s=$(pstate); [ "$s" = "$2" ] && { L "   -> $2 (poll $i)"; return 0; }; sleep 6; done
         L "   NEVER reached $2 (last=${s:-<empty>})"; return 1; }
transition(){ cat <<EOF | $K apply -f - >/dev/null 2>&1
apiVersion: baremetal.ogen.krateo.io/v1-0-0
kind: NodeProvision
metadata: { name: e2e01-$2, namespace: $NS }
spec: { configurationRef: { name: ironic-endpoint, namespace: $NS }, node_id: "e2e01", target: $2 }
EOF
}
await e2e01 enroll 30 || FAIL=1
if [ "$FAIL" = "0" ]; then transition e2e01 manage; await e2e01 manageable 30 || FAIL=1; fi
if [ "$FAIL" = "0" ]; then transition e2e01 provide; await e2e01 available 40 || FAIL=1; fi
if [ "$FAIL" = "0" ]; then transition e2e01 active;  await e2e01 active 50 || FAIL=1; fi

L ""
L "== diagnostics BEFORE teardown =="
$K -n $NS get baremetalnodes.v1-0-0.baremetal.ogen.krateo.io e2e01 \
  -o jsonpath='{range .status.conditions[*]}{.type}={.status}/{.reason} {end}' 2>/dev/null | sed 's/^/    node: /'; echo
$K -n $NS get nodeprovisions.v1-0-0.baremetal.ogen.krateo.io \
  -o custom-columns=NAME:.metadata.name,READY:.status.conditions[?\(@.type==\"Ready\"\)].status 2>/dev/null | sed 's/^/    /'
le=$($K -n $NS get baremetalnodes.v1-0-0.baremetal.ogen.krateo.io e2e01 -o jsonpath='{.status.last_error}' 2>/dev/null)
[ -n "$le" ] && L "    node last_error: $le"

L ""
L "########## RESULT"
if [ "$FAIL" = "0" ] && [ "$miss" = "0" ] && [ "$rdn" = "7" ] && [ -z "$notready" ]; then
  L "   PASS -- a fresh install still goes from nothing to an ACTIVE node."
  L "   7/7 RestDefinitions Ready, 7/7 Configurations present, full state walk completed."
else
  L "   FAIL/VOID -- fresh install is NOT clean. rdn=$rdn missingConfigs=$miss notReady='$notready' walk_failed=$FAIL"
  KEEP=1
fi
[ "$KEEP" = "1" ] && L "   cluster KEPT: kind-$C" || { make local-down KIND_CLUSTER="$C" I_MEAN_IT=1 >/dev/null 2>&1 && L "   deleted kind-$C"; }
