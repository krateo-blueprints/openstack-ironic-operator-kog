#!/usr/bin/env bash
# DELETE-path arm for oasgen-provider#444.
#
# Question: when OR adopts the WRONG object, does deleting the CR delete that
# wrong object? status carries the adopted object's identifier, and the delete
# verb's path is /v1/nodes/{uuid}, so the hypothesis is yes.
#
# A `delete` verb had to be ADDED to the RestDefinition for this to be testable
# at all -- without one, deleting the CR fires no external call and the test
# would "pass" by doing nothing. That is the vacuous version of this arm.
set -uo pipefail
: "${KC:?}" "${CTX:?}" "${IRONIC_API:?}"
k() { kubectl --kubeconfig "$KC" --context "$CTX" --request-timeout=40s "$@"; }
MV="X-OpenStack-Ironic-API-Version: 1.109"

nodes() {
  curl --fail -sS "$IRONIC_API/v1/nodes?include_children=true" -H "$MV" \
  | python3 -I -c 'import json,sys; print(" ".join(sorted(n["name"] for n in json.load(sys.stdin)["nodes"])))'
}

echo "nodes before anything: $(nodes)"

mk() {  # kind, crname, name-value, maintenance|''
  { echo "apiVersion: arms.ogen.krateo.io/v1-0-0"
    echo "kind: $1"
    echo "metadata: {name: $2, namespace: openstack}"
    echo "spec:"
    echo "  name: \"$3\""
    echo "  driver: fake-hardware"
    [ -n "$4" ] && echo "  maintenance: $4"
  } | k apply -f - >/dev/null 2>&1
}
res() { printf '%ss' "$(echo "$1" | tr '[:upper:]' '[:lower:]')"; }
adopted() { k get "$(res "$1").v1-0-0.arms.ogen.krateo.io" -n openstack "$2" -o jsonpath='{.status.uuid}' 2>/dev/null; }
adoptedname() { k get "$(res "$1").v1-0-0.arms.ogen.krateo.io" -n openstack "$2" -o jsonpath='{.status.name}' 2>/dev/null; }

echo
echo "########## POSITIVE CONTROL: does the delete verb fire AT ALL?"
echo '## ArmBase (single identifier name) asking for child-a -- a CORRECT match.'
mk ArmBase del-ctrl child-a ""
sleep 18
CTRL_AD=$(adoptedname ArmBase del-ctrl)
echo "   adopted: ${CTRL_AD:-<nothing>}  (expect child-a)"
if [ -z "$CTRL_AD" ]; then
  echo "   CONTROL FAILED: the control CR adopted NOTHING, so it cannot show the"
  echo "   delete verb firing. Conditions:"
  k get armbases.v1-0-0.arms.ogen.krateo.io -n openstack del-ctrl -o jsonpath='{range .status.conditions[*]}      {.type}={.status} {.reason} {.message}{"\n"}{end}' 2>/dev/null
  echo "   STOPPING rather than reading the arm below."
  exit 1
fi
BEFORE=$(nodes)
k delete armbases.v1-0-0.arms.ogen.krateo.io -n openstack del-ctrl --timeout=90s >/dev/null 2>&1
sleep 12
AFTER=$(nodes)
echo "   nodes: $BEFORE"
echo "       -> $AFTER"
if [ "$BEFORE" = "$AFTER" ]; then
  echo "   CONTROL FAILED: nothing was deleted, so the delete verb does NOT fire."
  echo "   -> the arm below cannot distinguish 'wrong object deleted' from 'nothing deleted'. STOPPING."
  exit 1
fi
echo "   CONTROL OK: the delete verb fires and removed the correctly-adopted node."

echo
echo "########## THE ARM: ArmOr asking for child-b, which OR mis-adopts"
mk ArmOr del-test child-b false
sleep 18
AD=$(adoptedname ArmOr del-test)
echo "   CR asked for : child-b"
echo "   adopted      : $AD"
if [ "$AD" = "child-b" ]; then
  echo "   NOTE: this run adopted the CORRECT node, so the arm measures nothing about"
  echo "         mis-adoption. Ordering is item-order dependent; re-seed and retry."
fi
BEFORE=$(nodes)
k delete armors.v1-0-0.arms.ogen.krateo.io -n openstack del-test --timeout=90s >/dev/null 2>&1
sleep 12
AFTER=$(nodes)
echo "   nodes: $BEFORE"
echo "       -> $AFTER"
echo
echo "   VERDICT: the CR named 'child-b'. The node that disappeared is whatever is"
echo "            missing above. If it is '$AD' and not 'child-b', then a wrong"
echo "            adoption DESTROYS an unrelated resource."
