# release-adopt-probe

Throwaway chart for measuring **core-provider#303**: when a composition CR is
recreated while its previous helm release still exists, does cdc *upgrade* that
release or *install a second one beside it*?

Deliberately inert — one ConfigMap, no hooks, no Jobs, no external calls, no
cluster-scoped objects. Nothing here can touch the Ironic fleet. Measured
2026-11-04 on the `ironic-kog` kind cluster.

## Why this chart exists

The natural experiment (finding a cluster already in the broken state) cannot
distinguish *adopted* from *never broken*. This chart lets you **construct** the
precondition: a composition CR that is gone while its helm release survives.

## Forcing the precondition

cdc runs **one deployment per CompositionDefinition version**, so scaling the
probe's own controller leaves every production composition reconciling.

```
kubectl scale deploy/releaseadoptprobes-v0-1-1-controller -n krateo-system --replicas=0
kubectl delete releaseadoptprobes.v0-1-1.composition.krateo.io -n probe-303 adopt --wait=false
kubectl patch  releaseadoptprobes.v0-1-1.composition.krateo.io -n probe-303 adopt \
  --type=merge -p '{"metadata":{"finalizers":[]}}'
kubectl scale deploy/releaseadoptprobes-v0-1-1-controller -n krateo-system --replicas=1
# then re-apply the CR with a DIFFERENT spec.marker
```

**Assert the precondition before believing the arm.** CR absent AND release
present. Without the surviving release the arm measures nothing and still
produces a number.

The release name lives in the CR label `krateo.io/release-name`. A recreated CR
has no such label, which is the whole mechanism: cdc must either mint a new
name (second release) or discover the existing one.

## Results

| | 2.13.58 (digest `9ad88f4a`) | 2.13.59 (digest `8f70ac94`) |
|---|---|---|
| releases after recreate | **2**, both `deployed`, rev 1 each | **1** |
| orphan | permanent — a later clean delete did **not** remove it | n/a |
| converges to desired state | yes, into the *new* release | **no** |
| CR conditions | `Synced=True Ready=True` | `Synced=False ReconcileError` |

**2.13.58** leaks a release that still owns a live ConfigMap with pre-delete
content, and reports healthy throughout.

**2.13.59** adopts correctly — `adopted=... would_have_minted=...` in the log —
then fails every reconcile with chart-inspector HTTP 500,
`install failed: release name "": no name provided`. The adopted name reaches
the helm operation but an empty `krateo.io/release-name` label reaches
chart-inspector. Setting that label by hand, changing nothing else, took the
release from rev 1 / `marker=cycle-1` to rev 3 / `marker=cycle-2` and both
conditions to True.

Only the **adoption** path is affected: a fresh CR with no pre-existing release
installs normally on 2.13.59, and every composition carrying a `release-name`
label skips the new code entirely.

## v0.1.2: the fixture itself could never converge

`templates/cm.yaml` rendered the release revision into its own output. That
makes the chart **non-idempotent**: every render differs from the live object,
so Observe sees drift, the self-heal upgrades, the revision changes, and the
next render differs again. Forever.

Measured on 2026-11-04 while taking a reading on cdc 2.13.62: the release had
climbed to **revision 154 and was still rising at about 2 per minute**, with
every condition reporting healthy — `Synced=True`, `Ready=True`,
`ChartRenderable=True`, and zero errors in the controller log.

**A composition that never settles while reporting perfectly healthy is
indistinguishable from a working one unless you check revision STABILITY over
time.** A conditions check passes it. I was composing a message reporting the
churn as a cdc defect; reading my own template first is what caught it.

Fixed in 0.1.2 by removing the field. Asserted: the render is byte-identical
across two invocations, and on the convergent chart the release holds at
revision 1 over 90 seconds.

Do not add anything to the rendered output that changes per render.

## Two traps worth inheriting

- **`values.schema.json` is mandatory.** Without it the CD fails with
  `error getting JSON schema: ... file does not exist`. But it must **not** set
  `additionalProperties: false` — helm injects `global`, chart-inspector does a
  real dry-run install, and the rejection arrives as an opaque 500
  (core-provider#325).
- **"One release" is not a pass.** One release at revision 1 means *replaced*,
  not *adopted*, and looks identical in a count. Always pair the count with
  `helm history` on the survivor and with the rendered content.

## Digests

`imageID` on a kind node is the **per-platform** manifest digest, so on Apple
Silicon it will not equal the multi-arch index digest published in release
notes. Use the *relative* control — assert the digest **moved** across the
upgrade — rather than matching a published value.
