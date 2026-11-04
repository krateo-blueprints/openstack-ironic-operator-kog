# Identifier-matching arms (oasgen-provider#439)

Measures what `rest-dynamic-controller`'s `isItemMatch` does when a kind
declares two identifiers under each `identifiersMatchPolicy`. Runs entirely on
an **isolated** kind cluster with a fake-driver standalone Ironic — nothing here
can reach the lab or the production management cluster.

## Result, 2026-11-04, rdc 0.50.0

| arm | policy | CR asked for | matcher selected | Synced |
|---|---|---|---|---|
| 0 *control* | one identifier (`name`) | `child-b` | **child-b** | True |
| 1 | **omitted → OR** | `name=child-b`, `maintenance=false` | **enclosure-a** | **True** |
| 2 | `AND` | `name=child-b`, `maintenance=false` | **child-b** | True |
| 3 | `AND` | `name=child-a`, `maintenance=false` | **child-a** | True |

**Arm 1 is the finding.** The CR named `child-b` explicitly. The matcher bound
it to `enclosure-a` — not a sibling but a *parent* node — because `OR` matches
on **either** identifier and `maintenance=false` is true of every node, so the
first item in the collection won. The reconcile then reported `Synced=True`.
Nothing anywhere says the CR is managing the wrong object.

**`OR` is the default**, including on omission. `AND` behaves correctly and is
usable (arms 2 and 3). So the hazard is reached by leaving a field out, not by
setting it wrong.

## Two findings about the fixture that outrank the arms

**1. Ironic's node COLLECTION is abridged to seven fields.**

```
GET /v1/nodes?include_children=true   (microversion 1.109)
  -> ['instance_uuid','links','maintenance','name','power_state','provision_state','uuid']
  resource_class present anywhere?  False
  parent_node present anywhere?     False

CONTROL: GET /v1/nodes/child-b
  -> resource_class='shared-class'  parent_node='6d6db1d5-...'
```

The fields exist on the resource; the collection omits them. findby reads the
collection, so **on Ironic, per-parent uniqueness via `AND` is not strict — it
is impossible.** `parent_node` never reaches the matcher.

That is a *third* way the comparison becomes unevaluable, and all three are
indistinguishable from a real negative at the matcher:

| | value missing from | example |
|---|---|---|
| 1 | the CR (`spec` **and** `status`) | `NodePower.node_id` (oasgen#419 suppression) |
| 2 | the **collection response item** | `parent_node`, `resource_class` on Ironic |
| 3 | nothing — values differ | a genuine negative |

**2. The first version of this fixture measured nothing and looked like a
finding.** Identifiers `[resource_class, parent_node]` → all three arms
"nothing selected", and it read as *"AND is unusable"*. Two of my own errors
made that look credible:

- **The CR-side guard passed.** `spec.resource_class` and `spec.parent_node`
  were both present and populated. The guard was a *necessary* condition being
  used as a sufficient one. It now also asserts the identifier appears in the
  **collection response**.
- **The runner printed a conclusion its data could not support:**
  `"no create fired => a match happened"`. Create *had* fired and failed on
  Ironic's required `driver`, and an unchanged node count is equally consistent
  with "matched" and "errored before reaching create". The two channels now
  print separately and the script never infers a match from the count.

What actually caught it was `op=Create` in the rdc log — proof findby had
definitively not matched — which sent me to read the raw collection response.

**Arm 0 exists because of this.** A single-identifier positive control proves
microversion stamping, `include_children`, comparison and status population all
work *before* any OR/AND claim is made. Without it, a no-match is
indistinguishable from a broken fixture.

## Running it

```sh
make KIND_CLUSTER=ironic-arms kind-up
helm ... oasgen-provider 0.50.0            # see the session notes
make KIND_CLUSTER=ironic-arms ironic-up    # needs `ironic-image` on arm64

kubectl -n openstack port-forward svc/ironic 6395:6385 &
IRONIC_API=http://localhost:6395 bash tests/identifier-arms/seed-fixture.sh

kubectl create configmap arm-identifier-oas -n openstack \
  --from-file=arm.yaml=oas/arm-identifier-match.yaml
kubectl apply -f tests/identifier-arms/restdefinitions.yaml

KC=... CTX=... IRONIC_API=... PARENT_A=... PARENT_B=... CHILD_A=... CHILD_B=... \
  bash tests/identifier-arms/run-arms.sh
```

`seed-fixture.sh` talks to Ironic directly rather than through KOG on purpose:
the fixture must be established **independently of the component under test**,
or a misbehaving matcher silently produces a different fixture than intended
and every arm still reports a number.
