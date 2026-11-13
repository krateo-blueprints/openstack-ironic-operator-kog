# Generator-level measurement: no cluster required

`ironic_power_spec_test.go` answers one question about **this repo's own document**:

> does the generated spec carry `node_id`, the path parameter that
> `manifests/restdefinition-power.yaml` maps **self-referentially** from `spec.node_id`?

## Why this exists

`scripts/check-restdefinition-shape.py` encodes a version-dependent rule:

```
spec.<param> survives  IFF  the create body declares <param> at top level
                       OR   the mapping is SELF-REFERENCING AND oasgen >= 0.52.0   (#421)
```

The second clause came from the `openstack-nova-operator-kog` session, measured on
**one document on one vendor**. Adopting it here on that basis would have been the
dangerous direction: if it did not transfer, the screen would report a live defect as
resolved. So PR #49 was held until this was measured on an Ironic document.

## Why the GENERATOR and not the cluster

The screen's claim is about what oasgen **generates** at a given version. The live
CRD answers a different question — what the apiserver has **stored**, which can be
stale if the fleet has not regenerated since an upgrade. Fixing what a system renders
says nothing about what it already stored, and vice versa. Both are real questions;
this harness is the right instrument for the screen's one, and it needs no cluster,
no load budget and no credentials.

## Result, 2026-11-12

```
oasgen 0.59.0   spec.node_id = PRESENT    CONTROL spec.target = PRESENT
oasgen 0.50.0   spec.node_id = ABSENT     CONTROL spec.target = PRESENT
```

`spec.target` is declared in the `PowerTarget` request body, so it must be present at
**both** versions. It is — which is what makes the 0.50.0 `ABSENT` a measurement
rather than a broken build. Without that control the two runs would be
indistinguishable from the harness failing to compile against the older API.

So the rule transfers to Ironic's documents, #421's behaviour is not narrower than
its description, and the earlier `ABSENT` finding here was correct **for 0.50.0**.

## Reproducing it

The package is `internal/`, so the test has to run *inside* oasgen's own tree, the
way their own tests do. Worktrees keep each version isolated:

```sh
git clone https://github.com/krateo-platformops/oasgen-provider.git /tmp/ogp
cd /tmp/ogp && git fetch --tags
for v in 0.59.0 0.50.0; do
  git worktree add --detach /tmp/ogp-$v "$v"
  cp tests/oasgen-generator/ironic_power_spec_test.go \
     /tmp/ogp-$v/go/oasgen-provider/internal/tools/oas2jsonschema/
  (cd /tmp/ogp-$v/go/oasgen-provider && \
     go test ./internal/tools/oas2jsonschema/ -run TestIronicPowerNodeIDSpecField -v)
done
```

Note the **hyphen** in `krateo-platformops` and the `go/<module>/` prefix: the
unhyphenated org still resolves and serves a stale snapshot, and the repo became a
monorepo with no root `go.mod`.

The test reads the OAS by absolute path, so adjust it if this repo lives elsewhere.
It is not wired into `make check` — it needs a clone of another repository at two
tags, which is not a standing gate's business.

---

# `ironic_provision_get_routing_test.go` — would a `get` verb fix the provision kind?

After #65 restored the configuration in which the provision PUT fires, the open
question was whether to buy back observability by declaring
`GET /v1/nodes/{node_id}/states` as a `get` verb. Two peers and I all expected that to
be the clean fix: existence would stop depending on findby matching, and
`additionalStatusFields` would populate.

**It is not. It would reintroduce #65's defect structurally, and no mapping avoids it.**

## What decides it

`BuildCallConfig`'s writer order (`builder.go`, numbered in its own comments):

```
3. processFields(specFields)    <- auto-populates path params from spec BY NAME
4. processFields(statusFields)
5. applyRequestFieldMapping     <- wins only where its own source resolves
```

Step 3 fills `{node_id}` from `spec.node_id` **before any mapping runs**. `spec.node_id`
is required on every NodeProvision CR, so the get call is always buildable;
`IsResourceKnown` returns true on the first reconcile, Observe routes to `get`,
`GET /v1/nodes/{id}/states` answers **200 because the node exists**, and
`ResourceExists: true` means the create — the PUT — is never dispatched.

A mapping reading `status.uuid` does not help: step 3 has already written the param, and
the mapping only overrides it when `status.uuid` resolves. Measured, not reasoned:

```
src=spec.node_id   param="probe01"   known=true    the defect, made structural
src=status.uuid    param="probe01"   known=true    THE FINDING -- auto-populated from spec
src=status.uuid    param="abc-123"   known=true    CONTROL: mapping overrides when it resolves
discriminator      param=""          known=false   a param nothing can supply
```

## The discriminator is not optional

The first version of this test used `mockUnstructuredClient`, whose `ValidateRequest`
returns `m.validateError` and **never looks at `params`**. So `IsResourceKnown` through
it is a constant, and the test reported `known=true` for both arms *and* the control —
three agreeing readings from an instrument that could not disagree. Only the control
said so. The file now uses a client that really checks the params, and
`TestIronicProvisionGetVerbRoutingDiscriminator` exists solely to come back `false`: if
it ever passes as `true`, every other arm here is vacuous.

## So what is the answer for provision?

That a fire-once action modelled as a CR has no observable "exists" state. Ironic
exposes no endpoint that 404s before the transition and 200s after it, so there is
nothing for Observe to key on that is not simply "the node is there". The kind stays
unobservable by construction, and the charts read `provision_state` off the
BaremetalNode CR, which has a working `responseTransform`. This is a property of the
model, not a defect in the manifest.

---

# `ironic_action_only_warning_test.go` — the arms oasgen asked for (#625 / #629)

oasgen-provider asked this repo for a consumer-side regression arm and said they would
treat a break in it as a break in their repo. This is it.

## What it pins

**#625**: a generation warning told authors to make findby match on a definition where a
never-matching findby is the *operating mechanism*. Acting on it disabled every
bare-metal provision transition here for 24 hours.
**#629**: the inverse and worse half — when such a definition's findby *can* match, its
create is unreachable and nothing warns at all.

Two arms over **one variable, the verb set**, read from the real shipped manifests:

| kind | verbs | expectation |
|---|---|---|
| `ironic-node-provision` | findby + create | must **not** be advised to make findby match |
| `ironic-node-power` | findby + get + create + update | must **still** be advised |

**The control is the point.** "The warning is suppressed" and "the warning stopped
working" are indistinguishable from the first arm alone, and a fix that silenced the
diagnostic everywhere would pass it.

## Measured, both sides of the fix

```
pre-#633   oasgen 05546c0   provision warnings: 2, power warnings: 2   FAIL
post-#633  oasgen 4b5191d   provision warnings: 1, power warnings: 2   PASS
```

Provision drops the harmful warning; **power keeps both**. So the fix is targeted rather
than a blanket silencing — which is exactly what the control is there to distinguish.

## Two things this file had to be rescued from

1. **It called the wrong stage.** The first version used `BuildSpecSchema()` and both
   arms reported **zero** warnings — the matchability warning is appended by `Generate()`
   one stage further out (`generator.go:103`). The arm I cared about was passing because
   nothing had been measured. **The control is what said so**, by demanding the warning
   be present on the non-action-only kind and failing.
2. **It reads the manifests rather than transcribing them.** A sibling consumer's port of
   a different screen was defeated by exactly that: their fixture drifted from the shipped
   shape, so the arm passed while testing nothing.

Assertions are on **what the author is told**, not on internal codes — oasgen's own
request. A reworded message still carrying the harmful advice fails these arms.

`TestIronicCorpusGenerates` is the smoke arm: all seven shipped RestDefinitions must
generate against their own documents. It asserts nothing about warnings (several are
expected and documented by this repo's own screen), only that generation does not break
for a real seven-kind corpus.
