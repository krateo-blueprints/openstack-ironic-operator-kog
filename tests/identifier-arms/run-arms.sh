#!/usr/bin/env bash
# Identifier-matching arms for oasgen-provider#439.
#
# Needs, from the environment:
#   KC, CTX      kubeconfig + context for the ISOLATED arms cluster
#   IRONIC_API   a port-forward to the local standalone Ironic
#   CHILD_A, CHILD_B, PARENT_A, PARENT_B   uuids from seed-fixture.sh
set -uo pipefail
: "${KC:?}" "${CTX:?}" "${IRONIC_API:?}" "${PARENT_A:?}" "${PARENT_B:?}" "${CHILD_A:?}" "${CHILD_B:?}"
k() { kubectl --kubeconfig "$KC" --context "$CTX" --request-timeout=30s "$@"; }
MV="X-OpenStack-Ironic-API-Version: 1.109"

node_count() {
  curl --fail -sS "$IRONIC_API/v1/nodes?include_children=true" -H "$MV" \
  | python3 -I -c 'import json,sys; print(len(json.load(sys.stdin)["nodes"]))'
}

label_for() {
  case "$1" in
    "$CHILD_A")  echo "child-a";;
    "$CHILD_B")  echo "child-b";;
    "$PARENT_A") echo "enclosure-a";;
    "$PARENT_B") echo "enclosure-b";;
    "")          echo "(nothing selected)";;
    *)           echo "UNKNOWN:$1";;
  esac
}

plural() { printf '%ss' "$(echo "$1" | tr '[:upper:]' '[:lower:]')"; }

# GUARD, permanent (NOT scaffolding). oasgen withdrew the #439 change that
# would have made this self-reporting, so this stays the only channel that
# distinguishes "the policy is unusable" from "the fixture could not exercise
# it". Two ways the comparison becomes unevaluable, and this catches both:
#   a) identifier absent from the CR   (oasgen#419 spec-property suppression)
#   b) identifier absent from the COLLECTION response (Ironic returns 7 fields)
guard() {
  local kind="$1" name="$2" ok=1 res; res=$(plural "$kind")
  for f in "${@:3}"; do
    v=$(k get "$res.v1-0-0.arms.ogen.krateo.io" -n openstack "$name" -o jsonpath="{.spec.$f}" 2>/dev/null)
    if [ -z "$v" ]; then echo "      GUARD FAIL: spec.$f absent on $kind/$name"; ok=0
    else echo "      guard: spec.$f = $v"; fi
  done
  [ "$ok" = 1 ] || { echo "      -> this arm measures NOTHING; do not read its result"; return 1; }
}

arm() {   # label, kind, crname, name-value, maintenance-value|'', expectation
  local label="$1" kind="$2" cr="$3" nameval="$4" maint="$5" expect="$6"
  local res; res=$(plural "$kind")
  echo; echo "=== $label"
  local before; before=$(node_count)
  {
    echo "apiVersion: arms.ogen.krateo.io/v1-0-0"
    echo "kind: $kind"
    echo "metadata: {name: $cr, namespace: openstack}"
    echo "spec:"
    echo "  name: \"$nameval\""
    # driver is REQUIRED by Ironic on create. Including it means a no-match
    # produces a real extra node, so the count below is a genuine second
    # channel instead of failing for an unrelated reason.
    echo "  driver: fake-hardware"
    [ -n "$maint" ] && echo "  maintenance: $maint"
  } | k apply -f - >/dev/null 2>&1
  sleep 18
  local fields="name"; [ -n "$maint" ] && fields="name maintenance"
  guard "$kind" "$cr" $fields || return 0
  local uuid synced msg after
  uuid=$(k get "$res.v1-0-0.arms.ogen.krateo.io" -n openstack "$cr" -o jsonpath='{.status.uuid}' 2>/dev/null)
  synced=$(k get "$res.v1-0-0.arms.ogen.krateo.io" -n openstack "$cr" -o jsonpath='{.status.conditions[?(@.type=="Synced")].status}' 2>/dev/null)
  msg=$(k get "$res.v1-0-0.arms.ogen.krateo.io" -n openstack "$cr" -o jsonpath='{.status.conditions[?(@.type=="Synced")].message}' 2>/dev/null)
  after=$(node_count)
  echo "      expectation : $expect"
  echo "      selected    : $(label_for "$uuid")    Synced=${synced:-none}"
  [ -n "$msg" ] && echo "      message     : ${msg:0:140}"
  # The two channels, reported SEPARATELY and never collapsed into one claim.
  # An unchanged count does NOT imply a match: it is equally consistent with a
  # reconcile that errored before reaching create. That exact inference was
  # wrong in the first version of this script.
  if [ "$before" = "$after" ]; then
    echo "      node count  : $before -> $after (unchanged: a match OR an error before create -- read 'selected' and Synced to tell which)"
  else
    echo "      node count  : $before -> $after (CREATE SUCCEEDED => findby found NO match: this is the duplication hazard)"
  fi
}

echo "fixture: enclosure-a=$PARENT_A enclosure-b=$PARENT_B child-a=$CHILD_A child-b=$CHILD_B"
echo "nodes at start: $(node_count)"
echo
echo "### Ironic's node COLLECTION carries only these fields -- findby can compare nothing else:"
curl --fail -sS "$IRONIC_API/v1/nodes?include_children=true" -H "$MV" \
 | python3 -I -c 'import json,sys; ns=json.load(sys.stdin)["nodes"]; print("   ", sorted(set().union(*[set(n) for n in ns])))'

arm "ARM 0  POSITIVE CONTROL: one identifier (name), ask for child-b" \
    ArmBase base-b child-b "" \
    "child-b -- proves microversion, include_children, comparison and status all work"

arm "ARM 1  policy OMITTED (OR default): name=child-b AND maintenance=false" \
    ArmOr or-b child-b false \
    "HAZARD: OR matches on EITHER identifier, so the first node with maintenance=false can win"

arm "ARM 2  policy AND: name=child-b AND maintenance=false" \
    ArmAnd and-b child-b false \
    "child-b only -- both identifiers must match"

arm "ARM 3  policy AND: name=child-a AND maintenance=false (control that AND is USABLE)" \
    ArmAnd and-a child-a false \
    "child-a -- proves AND is not strict to the point of matching nothing"
