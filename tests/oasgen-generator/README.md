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

# `ironic_power_get_routing_test.go` — is NodePower's `findby` ever consulted?

`manifests/restdefinition-power.yaml` carries a `findby` with a `responseTransform`, an
`include_children=true` query that took a measurement to find (3 nodes vs 17 on the
collection), and `identifiersMatchPolicy: OR`. It also carried a comment asserting that
when that findby does **not** match, "`create` drives the state, which is the only
outcome that actually powers the node."

That is testable. Observe reaches `findby` only when `IsResourceKnown` is false, and that
function asks exactly one question — do the `get` verb's path parameters resolve from the
CR as it stands? There is no "has this been observed before" input.

## The version coordinate, which is easy to get wrong

`rest-dynamic-controller` lives **inside** the oasgen-provider tree at
`go/rest-dynamic-controller/`. The numbers this repo pins — `KRATEO_RDC_VERSION` and
`KRATEO_OASGEN_VERSION`, both `0.61.0` — are **oasgen** tags. The standalone
`krateo-platformops/rest-dynamic-controller` repo stops at **0.20.0** and has no 0.61.0 or
0.80.0 tag at all, so a run that cloned it looking for those would find nothing, and that
absence would say nothing about this code. Confirmed against both org spellings
(`krateo-platformops` has oasgen 0.83.1; `krateoplatformops` is the stale one and stops at
0.10.0).

## Result, 2026-11-18 — identical at oasgen 0.61.0 and 0.80.0

```
ARM 1  get present, spec.node_id="blade01"  param="blade01"  known=TRUE
ARM 2  get ABSENT,  spec.node_id="blade01"                   known=false
ARM 3  get present, spec.node_id ABSENT     param=""         known=false
DISCRIM  a param nothing can supply         param=""         known=false
```

`known=TRUE` on **ARM 1** is on a CR with no status at all, so `findby` is bypassed from
the first reconcile. The cause is `BuildCallConfig`'s writer order — step 3
`processFields(specFields)` auto-populates path params from the spec **by name**, before
step 5 applies any mapping — the same mechanism
`ironic_provision_get_routing_test.go` measured from the other direction.

## The two arms that make it a measurement rather than a claim

`findby` is reachable only if one of two conditions holds, and **neither holds today**:

| | condition | when it last held | arm |
|---|---|---|---|
| (i) | no `get` verb | before 2026-11-08 | ARM 2 |
| (ii) | no `spec.node_id` | below oasgen 0.52.0 (#421) | ARM 3 |

**(ii) is why the dated measurements in the manifest are correct as written and must not
be "fixed".** On 2026-11-07 and 2026-11-08 the cluster ran an oasgen below 0.52.0, where
`spec.node_id` was suppressed — measured ABSENT at 0.50.0 and PRESENT at 0.59.0 with
`spec.target` present at both as the control, in `ironic_power_spec_test.go` above. So the
get was unbuildable, Observe really did fall to `findby`, and `create` really did dispatch,
which is what `evidence/blade06-rdc-log-2026-11-05.jsonl` records (`op=Create`, refusing
the unresolved `{node_id}`). Nothing in this file contradicts those readings; the pinned
version moved out from under them.

Without ARM 3 the finding would have been attributed to the `get` verb alone, which is
wrong in a way that matters: it would have made a *downgrade* look safe.

## What it does NOT measure

That `create` is unreachable too. `GET /v1/nodes/{node_id}` answers 200 for any node that
exists, which would make `ResourceExists=true` and leave `get`+`update` driving the kind —
but that is an inference from Ironic's endpoint behaviour, and this harness never performs
the GET. The arms must not be cited for the create half.

## The discriminator is not optional, for the same reason as provision's

Three of four arms answer `false`, so a broken instrument would look like a finding here
rather than like agreement. `TestIronicPowerGetVerbRoutingDiscriminator` exists to come
back `false` because the path param **cannot** be supplied — `spec.node_id` is present but
differently named, so step 3 has nothing to match. If it ever reads `true`,
`ValidateRequest` is ignoring params and every arm in the file is vacuous. It uses its own
`powerParamCheckingClient` rather than sharing provision's helper, so each file can be run
alone.

## Reproducing it

```
git clone https://github.com/krateo-platformops/oasgen-provider.git /tmp/ogp
cd /tmp/ogp
for v in 0.61.0 0.80.0; do
  git worktree add --detach /tmp/ogp-$v "$v"
  cp <this repo>/tests/oasgen-generator/ironic_power_get_routing_test.go \
     /tmp/ogp-$v/go/rest-dynamic-controller/internal/tools/client/builder/
  (cd /tmp/ogp-$v/go/rest-dynamic-controller && \
     go test ./internal/tools/client/builder/ -run TestIronicPowerGetVerbRouting -count=1 -v)
done
```

`-count=1` matters: these tests read nothing at runtime, but the suite next door does, and
a cached pass there has already been mistaken for a fresh one.

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

---

# `ironic_secretref_crd_stability_test.go` — does the `secretRef` resolver regenerate the node CRD?

Moving the BMC password out of plaintext needs a `secretRef` resolver on the node
definition's create and update verbs. The question that decided **how** to do it:

> does adding that resolver change what oasgen GENERATES?

## Why it decides the shape of the change

Regenerating the node CRD unserves its previous version, and that wedges every
`kubernetes-cluster` render here until the parent chart's pin moves — measured twice in
this repo. So the answer is the difference between a manifest-only edit and a coordinated
four-chart release.

## Result, 2026-11-14, oasgen 0.61.0 (the pinned version)

```
ARM A  with the resolver      spec schema 2264 bytes
ARM B  without the resolver   spec schema 2264 bytes   IDENTICAL
CONTROL  ExcludedSpecFields=["extra"]   2264 -> 2183   the comparison CAN see a change
```

So the resolver is manifest-only: no regeneration, no chart lockstep.

It is identical because the resolver needed **no OAS change**. `NodeCreate.required` is
`[driver]`, so `dropResolverSuppliedRequired` has nothing to drop, and both `driver_info`
and `extra` are `additionalProperties: true`, so the pointer carrier
`spec.extra.bmc_secret` is expressible without declaring a property.

## The control is not optional here

Byte equality is exactly what a collapsed instrument produces — two renders agreeing to
the byte is a tell, not a finding (this repo has one of those on record). So the control
excludes `extra` and demands the bytes **move**. It also pins this file's premise: if
excluding `extra` changed nothing, `extra` would not be a generated spec property and the
carrier path could not resolve at all. One assertion, two failure modes caught.

`loadNodeForSecretRef` also VOIDs if the shipped manifest declares no resolver entries,
because then both arms are the same input and byte equality is a tautology.

## The result EXPIRED at 0.65.0, exactly as predicted — and the suite did not notice

`2264 == 2264` was measured at **0.61.0**. The note below was written as a prediction before
0.65.0 existed; it is now a measurement, and the prediction's second half — *the suite will
not notice* — was the part that mattered.

```
oasgen 0.61.0   SPEC identical (2264)   STATUS identical (1860), digest absent from BOTH arms
oasgen 0.65.0   SPEC identical (2264)   STATUS MOVED 1860 -> 2447 (+587)
                                        resolvedResolversDigest present WITH the resolver,
                                        absent WITHOUT it
```

oasgen-provider **#646** declares `status.resolvedResolversDigest` gated on the update verb
carrying a `secretRef` — a **status-side** change. **This test compared only `SpecSchema`**,
so at 0.65.0 it reported PASS, logged "spec schema IDENTICAL", and said nothing about a node
CRD that now regenerates. The warning predicted that and the code still had the hole; writing
the prediction down is not the same as closing it.

It now reads both schemas, and asserts on the MECHANISM rather than on a version, so one
assertion holds either side of #646:

- the digest field must **never** appear in the without-resolver arm (if it does, #646's gate
  is not holding and every consumer gets the field — that is a finding for oasgen, not for us)
- if it appears in the with-resolver arm, the status schemas **must** differ
- status schemas differing with the field in neither arm is an error, because then something
  other than #646 moved and this test cannot say what

The 0.65.0 run is therefore also generation-level confirmation that #646's gate works — the
field is absent from the arm whose update verb carries no resolver. Worth noting because
oasgen's own tag message states that no part of #598 has been exercised on a cluster by them.

### What it costs us

Adopting the `secretRef` means the node CRD regenerates on the upgrade to 0.65.0, which
unserves its previous version and stalls every `kubernetes-cluster` render until the parent
chart's pin moves. The provider's `OASGEN_PROVIDER_REGENERATE_ON_UPGRADE=false` defers a
regeneration caused by the *upgrade*; it does **not** defer this one, because the gate is an
OR and adopting the resolver is itself a `spec.resource` change that moves `resourceHash`. So
the wedge is owed — the only choice is when to pay it, with the parent pin move planned.

### The general lesson, which is why this section stays

A comparison between two arms **at one version** keeps reporting PASS after its conclusion
stops holding, because both arms move together. Pin the mechanism, not the number: the
assertion that survived the version change is the one about *which field appears where*, not
the one about 2264 bytes.

#### And it expired exactly that way on 2026-11-15 — from THIS side, not upstream

`2264` no longer reproduces: adding `power_interface` to `oas/ironic-node.yaml` moved the
node spec schema to **2413**. The rows above are still correct as measurements — both arms
were identical, and they still are — but their absolute figure is now historical.

Note which direction it expired from. The note above anticipated an oasgen version change
moving the number. What actually moved it was a change in **this repo's own OAS**, which no
upstream version pin protects against and no screen here watches. The mechanism assertion
survived untouched, as predicted; the number died of something the prediction did not name.

Re-measured with `-count=1` and the file state checked at each arm:

```
without power_interface   2264 bytes   (0 occurrences in the OAS)
with it, terse description 2413 bytes  (3 occurrences)   +149
with it, verbose description 3188 bytes               +924
```

**The third row is a finding in its own right.** oasgen copies an OAS `description` into the
generated CRD, so prose there is stored in etcd and printed by `kubectl explain`. My first
draft of the field put ~15 lines of repo rationale in its `description` and cost 924 bytes --
six times the field itself -- to ship repo history as API documentation. Descriptions stay
terse; rationale goes in a YAML `#` comment, which oasgen does not read.

**And the first attempt to measure this read a cached result.** `go test` caches passing
runs, and this test reads the OAS **at runtime**, so the file is not a build input and
changing it does not invalidate the cache. Two arms that genuinely differed both printed
2264 until `-count=1` was added. Any arm here that varies a file the test reads rather than
compiles MUST pass `-count=1`, and should print something that proves which input it saw --
the occurrence count above is there for that reason.
