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

## The DELETE arm: a CR naming `child-b` destroyed `enclosure-a`

`run-delete-arm.sh`, 2026-11-04, rdc 0.50.0. oasgen-provider#444.

```
positive control  ArmBase, single identifier `name`, asked child-a
                  -> adopted child-a -> CR deleted -> child-a REMOVED
                  proves the delete verb fires, so the arm can distinguish
                  "wrong object deleted" from "nothing deleted"

the arm           ArmOr, policy omitted (OR), asked child-b
                  -> adopted enclosure-a -> CR deleted
                  before: child-b enclosure-a enclosure-b
                  after:  child-b           enclosure-b
```

Verified on a second channel, not the script's string diff:

```
GET /v1/nodes/enclosure-a  -> 404     the node OR adopted:   DESTROYED
GET /v1/nodes/child-b      -> 200     the node the CR NAMED: untouched
enclosure-b                -> present never referenced:      correct
```

So the mis-adoption does not merely mismanage an unrelated object — **it
deletes one**, with `Synced=True` throughout and no event. `enclosure-a` was a
*parent*, so the blast radius is the enclosure its children hang off.

### Adding the delete verb surfaced two more things

**A delete verb with a path parameter makes that parameter a REQUIRED spec
field.** `DELETE /v1/nodes/{uuid}` gave `spec.uuid: Required value` on every CR,
making the kind **uncreatable** — a CR whose identity is *discovered* by findby
cannot state a uuid it does not yet have. A `requestFieldMapping`
(`inPath: uuid` → `inCustomResource: status.uuid`) suppresses the property and
resolves from the adopted object. The oasgen#419 suppression rule acting as the
remedy rather than the defect.

**`RestDefinition Ready=True` is not a statement about the generated CRD.**
After applying that mapping the RestDefinitions reported `Ready=True` while the
served CRD still carried `required: [uuid]`. One step from reporting "the
mapping does not suppress". Forcing a clean regeneration to **disambiguate
stale-schema from no-suppression** showed `uuid` gone. A readiness condition on
the generator is not a readiness condition on the artifact.

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
