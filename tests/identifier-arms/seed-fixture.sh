#!/usr/bin/env bash
# Seed the VENDOR-SIDE state the identifier arms measure against.
#
# Four nodes on the local standalone Ironic (fake drivers, noauth):
#
#   enclosure-a                                 parent
#   enclosure-b                                 parent
#   child-a   parent_node=<enclosure-a>  resource_class=shared-class
#   child-b   parent_node=<enclosure-b>  resource_class=shared-class
#
# child-a and child-b share an identifier VALUE under DIFFERENT parents. That
# is the collision the arms need, and it is constructed rather than found: the
# real six-blade fleet has six distinct names and zero values under more than
# one parent, so it cannot produce this state.
#
# WHY THIS TALKS TO IRONIC DIRECTLY instead of creating the nodes through KOG:
# the fixture must be established INDEPENDENTLY of the component under test.
# The arms measure rest-dynamic-controller's matcher; seeding through that same
# matcher would make the setup depend on the behaviour being measured, so a
# misbehaving matcher could silently produce a different fixture than intended
# and every arm would still report a number. This is a throwaway kind cluster
# with a fake-driver Ironic -- not the lab.
#
# Requires: a port-forward to the local Ironic on $IRONIC_API.
set -euo pipefail
IRONIC_API="${IRONIC_API:-http://localhost:6385}"
MV="1.109"   # parent_node needs >= 1.83

api() {
  local method="$1" path="$2" data="${3:-}"
  if [ -n "$data" ]; then
    curl --fail -sS -X "$method" "$IRONIC_API$path" \
      -H "Content-Type: application/json" \
      -H "X-OpenStack-Ironic-API-Version: $MV" \
      --data "$data"
  else
    curl --fail -sS -X "$method" "$IRONIC_API$path" \
      -H "X-OpenStack-Ironic-API-Version: $MV"
  fi
}

uuid_of() { python3 -I -c 'import json,sys; print(json.load(sys.stdin)["uuid"])'; }

echo "### Ironic reachable? (fail loudly rather than seeding half a fixture)"
api GET /v1 > /dev/null
echo "    yes"

mk_parent() {
  local name="$1"
  api POST /v1/nodes "{\"name\":\"$name\",\"driver\":\"fake-hardware\"}" | uuid_of
}
mk_child() {
  local name="$1" parent="$2" rc="$3"
  api POST /v1/nodes \
    "{\"name\":\"$name\",\"driver\":\"fake-hardware\",\"parent_node\":\"$parent\",\"resource_class\":\"$rc\"}" \
    | uuid_of
}

PA=$(mk_parent enclosure-a); echo "enclosure-a $PA"
PB=$(mk_parent enclosure-b); echo "enclosure-b $PB"
CA=$(mk_child child-a "$PA" shared-class); echo "child-a     $CA  parent=$PA"
CB=$(mk_child child-b "$PB" shared-class); echo "child-b     $CB  parent=$PB"

echo
echo "### ASSERT the fixture is what the arms assume -- a seeded fixture nobody"
echo "### checked is the same failure as an uncalibrated instrument."
api GET "/v1/nodes?include_children=true" | python3 -I -c '
import json,sys
ns=json.load(sys.stdin)["nodes"]
print("    nodes visible with include_children=true:", len(ns))
assert len(ns) == 4, "expected 4 nodes, got %d" % len(ns)
print("    CONTROL: collection WITHOUT include_children is checked separately below")
'
echo "### and the control that makes that number mean something:"
api GET "/v1/nodes" | python3 -I -c '
import json,sys
ns=json.load(sys.stdin)["nodes"]
print("    nodes visible WITHOUT include_children:", len(ns))
assert len(ns) == 2, "expected only the 2 parents, got %d" % len(ns)
print("    -> children are genuinely hidden without the query, so include_children is load-bearing")
'
echo
echo "PARENT_A=$PA"
echo "PARENT_B=$PB"
echo "CHILD_A=$CA"
echo "CHILD_B=$CB"
