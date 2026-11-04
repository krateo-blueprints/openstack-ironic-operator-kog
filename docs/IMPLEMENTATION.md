---
type: Architecture
title: Krateo Akmon — what is implemented
description: The complete implementation reference for Krateo Akmon, the CNCF-conformant installer that provisions bare metal through OpenStack Ironic and installs unmodified upstream Kubernetes on it — the two-blueprint architecture, the Ironic state machine, the Kubernetes layer, operations, the failure modes that report success, and the open upstream issues.
resource: oci://ghcr.io/krateo-blueprints/charts/kubernetes-cluster
tags: [ironic, bare-metal, kubernetes, conformance, cncf, krateo, kog, composition, architecture]
timestamp: 2026-11-04T00:00:00Z
---

# Krateo Akmon — what is implemented

**Krateo Akmon** installs unmodified upstream Kubernetes onto bare-metal
servers provisioned through OpenStack Ironic, driven entirely by declarative
Krateo compositions. It is an *installer* in CNCF's taxonomy, not a
distribution: it downloads vanilla Kubernetes from `pkgs.k8s.io` and patches
nothing.

**Status: CNCF conformant.** `hydrophone --conformance` against Kubernetes
**v1.36.6** on a six-node bare-metal cluster: **443 Passed, 0 Failed, 0
Pending, 7136 Skipped**, `SuiteSucceeded = true`. Artefacts and a reproduction
guide are in [`conformance/v1.36/`](../conformance/v1.36/).

This document describes what exists and why it is shaped the way it is. For
how to run it, see [`quickstart.md`](quickstart.md) and
[`USER-GUIDE.md`](USER-GUIDE.md). For how it differs from Metal³, see
[`VS-METAL3.md`](VS-METAL3.md).

---

## 1. The architecture, and the one idea it rests on

The distinguishing idea is **two blueprints, not one**.

```
          ┌──────────────────────────────────────────┐
          │  kubernetes-cluster  (the Kubernetes layer) │
          │  "given machines, make a cluster"          │
          └───────────────────┬──────────────────────┘
                              │ renders N BaremetalHost CRs
          ┌───────────────────▼──────────────────────┐
          │  baremetal-host  (the Ironic layer)       │
          │  "given a machine, drive its state"       │
          └───────────────────┬──────────────────────┘
                              │ renders KOG CRs
          ┌───────────────────▼──────────────────────┐
          │  BaremetalNode · Port · NodePower ·        │
          │  NodeProvision   → rest-dynamic-controller │
          └───────────────────┬──────────────────────┘
                              │ HTTP
                       OpenStack Ironic
```

The lower blueprint knows only about machines: enrol a node, attach its ports,
set power, walk its provision state. It has no concept of Kubernetes. The upper
blueprint knows only about clusters: which machines are control plane, which
are workers, how they find each other. It reaches hardware only by rendering
instances of the lower one.

This is the difference from Metal³, which couples bare-metal management and
cluster lifecycle in one operator. Here the Ironic layer is independently
usable: you can run `baremetal-host` on its own to manage machines with no
Kubernetes anywhere in the picture, and that is a supported use, not an
accident of layering.

### Component inventory

| Component | Version | Role |
|---|---|---|
| `kubernetes-cluster` chart | 0.13.22 | The Kubernetes layer. 10 templates. |
| `baremetal-host` chart | 0.6.2 | One machine's Ironic lifecycle. 9 templates. |
| `baremetal-lifecycle` chart | 0.3.1 | Standalone lifecycle operations. |
| `baremetal-discovery` chart | 0.1.1 | Node discovery/enrolment. |
| Krateo `core-provider` | 1.0.0 | Turns a chart into a CRD + controller (cdc). |
| Krateo `oasgen-provider` | 0.50.0 | Turns an OpenAPI spec into CRDs. |
| `rest-dynamic-controller` | 0.50.0 | Reconciles those CRDs against the HTTP API. |
| Kubernetes | v1.36.6 | Installed unmodified from `pkgs.k8s.io`. |
| Cilium | 1.20.4 | CNI, with kube-proxy replacement. |
| containerd | 2.4.2 | Runtime, from Docker's apt repo. |

Seven RestDefinitions exist on disk (`node`, `port`, `power`, `provision`,
`allocation`, `portgroup`, `deploy-template`); four are deployed in the
reference environment.

---

## 2. The Ironic layer

### Everything is a CR, and every transition is its own CR

`baremetal-host` renders a `BaremetalNode` (the Ironic node), one `Port` per
NIC, and then **one CR per state transition** — `transition-manage`,
`transition-provide`, `transition-deploy`, `transition-clean`,
`transition-inspect`, `transition-undeploy`, `transition-power`. Each renders a
`NodeProvision` or `NodePower` only when the node's current state makes that
transition legal:

```
enroll ──manage──▶ manageable ──provide──▶ available ──deploy──▶ active
                                                ▲                   │
                                                └─────undeploy──────┘
```

The gate is always the node's **observed** state, read from the `BaremetalNode`
CR's `status.provision_state`. When a transition is not legal the template
renders nothing at all, so the chart is a pure function of observed state and
re-rendering is always safe.

A design consequence worth stating: `NodeProvision` has `findby` and `create`
but **no `update` verb**, because Ironic's provision endpoint is a command, not
a resource. A transition fires because a CR with a *new name* appears. This is
why transitions are separate templates rather than one mutable CR — and why an
attempt to collapse them into a single CR (0.6.0) had to be reverted: with no
`update` verb, the single CR could never fire a second time.

### The teardown problem, and what was learned

Deleting a `BaremetalHost` fires `helm uninstall`, which does **not** re-render
the chart. So deletion cannot walk Ironic from `active` down to `available`
first — KOG fires `DELETE /v1/nodes/{id}` against an active node, Ironic
refuses, and the record wedges.

Versions 0.5.6–0.6.1 tried to close that with a `pre-delete` helm hook that
drained the machine before the delete proceeded. **It was removed in 0.6.2
because it could never work**, for three independent reasons — see §8.

Drain-before-delete now lives where it can work: the parent's
`workers.removed[]` sets `undeploy: true` on the `BaremetalHost`, which renders
`transition-undeploy` **through cdc**, waits for `available`, and only then
drops the node from the render.

---

## 3. The Kubernetes layer

### No IP addresses are declared as inputs

This was a hard requirement and it shapes the design. The control-plane
endpoint is a VIP that the bootstrap node **derives at runtime**: it probes its
own subnet with `arping -D` for a free address, claims it with a `kube-vip`
static pod, publishes it into Ironic's `node.extra`, and the joining nodes read
it back from there.

Nothing in `values.yaml` contains an IP. The cluster's own address is
discovered by the cluster.

### Bring-up

1. **Bootstrap CP** — `kubeadm init` with `controlPlaneEndpoint` set to the
   derived VIP, installs Cilium, publishes its join command, certificate key
   and etcd IP into `node.extra`.
2. **CP replicas** — read the join data from Ironic at runtime and
   `kubeadm join --control-plane`, giving stacked etcd across three nodes.
3. **Workers** — read the join command and `kubeadm join`.

Join data travels through Ironic's `node.extra` rather than through the
management cluster, because the blades can reach Ironic but not the management
apiserver.

### Two settings conformance depends on

Both were found by running the suite, and both are required:

- **`cni.kubeProxyReplacement: true`.** Without it nothing implements
  `hostPort`: Cilium provides it only under kube-proxy replacement and is not
  chained with the `portmap` plugin. A *running* kube-proxy does not cover it,
  and `[sig-network] HostPort` fails.
- **containerd 2.x.** Debian trixie ships 1.7.24 and offers nothing newer;
  `[sig-node] ImageVolume` needs runtime support from the 2.x line. The chart
  installs `containerd.io` from Docker's apt repo and then asserts
  `SystemdCgroup = true`.

### The conformance runner is in the chart

`conformance.install: true` bakes `hydrophone` onto the bootstrap node, because
the certified result was originally produced with a hand-installed binary that
a re-image would silently take away. The suite does **not** run on boot by
default — it takes about two hours — and the runcmd entry is last so a failed
download can never be mistaken for a failed cluster.

---

## 4. How the pieces reach each other

### The network, and why join data travels through Ironic

The management cluster and the blades are not on one flat network. The blades
can reach Ironic; they **cannot** reach the management cluster's apiserver.
That single asymmetry explains several design choices that otherwise look
roundabout:

- Join commands, the certificate key, the etcd IP and the control-plane
  endpoint are published into **Ironic's `node.extra`** and read back from
  there by joining nodes. Ironic is the only rendezvous both sides can see.
- A `wg-ironic-proxy` Deployment terminates a WireGuard tunnel to the lab and
  `socat`-forwards TCP to the real Ironic. It parses nothing and rewrites
  nothing, deliberately.
- In the reference environment the in-cluster `ironic` Service has **no
  selector**; an EndpointSlice points it at the lab endpoint. The `ironic`
  Deployment that also exists there is a leftover from local testing and holds
  zero nodes. Check the EndpointSlice before reasoning about which Ironic is
  in play — the Deployment is a decoy.

### The extra-bridge sidecar

`ironic-extra-bridge` mirrors Ironic's `node.extra` onto the generated
`BaremetalNode` CRs so the charts can read published values with helm
`lookup`. Two things about it are load-bearing and non-obvious:

- It mirrors an **explicit allow-list** of keys. Mirroring a partial view was
  what caused rdc to replay a truncated map back to Ironic and delete the
  rest — see §7.
- It deliberately does **not** mirror `deployed_configdrive_hash`, because
  that key is the observed half of the auto-redeploy predicate. Mirroring it
  would arm a predicate that re-images live machines.

Clearing keys is done with explicit `null`s, never by omission, because
RFC 7386 merge-patch leaves an omitted key unchanged.

### Credentials

Ironic is reached through a Keystone-protected endpoint; a token refresher
keeps the credential current, and the generated CRs carry a
`configurationRef` naming a single `BaremetalNodeConfiguration` that holds the
auth reference rather than the secret itself. The cluster-admin kubeconfig is
never copied off the machine that owns it — conformance is run *on* the
bootstrap node for exactly that reason, and diagnostics that need `kubectl`
run there too.

## 5. What is deliberately not implemented

Stating these saves the next person from assuming they were overlooked.

- **No hand-written Go operator.** Every behaviour is a chart plus a generated
  controller. The cost is that bugs land in generated code paths that this
  repo does not own, which is why §9 is as long as it is.
- **No `update` verb on `NodeProvision`.** Ironic's provision endpoint is a
  command, not a resource. Transitions fire by CR creation.
- **No userData drift detection, and nothing says so.** `shouldAutoRedeploy`
  is the most heavily reasoned predicate in the chart — roughly 25 lines on
  race-safety, suppression windows and two revisions of history. It fires when
  the observed configdrive hash is non-empty and differs from the current one.
  The observed hash comes from the CR's `spec.extra.deployed_configdrive_hash`,
  mirrored by the bridge — and the bridge's allow-list **deliberately excludes
  that key** (`DELIBERATELY ABSENT` in `scripts/ironic-node-extra-bridge.py`),
  because mirroring it would arm the predicate. Measured: the precondition
  holds on **0 of 6** nodes, so the predicate cannot fire anywhere.

  The exclusion is intentional. The silence is not. Change the image or the
  cloud-init and **nothing redeploys and nothing reports that nothing
  redeployed** — the blade publishes its hash to Ironic, the bridge drops it,
  and no part of the chart observes the resulting gap.

  This was found by deliberately looking for a shape the oasgen-provider
  maintainers named from a defect of their own: *the instrumentation ends up
  on the path that cannot break*. The mechanism you reason about hardest is
  the one you instrument, and it is usually also the one you then make unable
  to fail; the repair path gets written second, quickly, because the hard
  thinking is done. Worth re-running that search against anything here that
  looks carefully argued.

- **No stuck-deploy auto-recovery.** A predicate existed and was removed: it
  anchored on the `BaremetalNode` CR's `creationTimestamp` — the age of the
  *record*, not of the current attempt — so fifteen minutes after enrolment it
  was permanently true, and every healthy deploy transiting `wait call-back`
  fired a fresh undeploy. One blade lost that race nineteen times. A correct
  predicate needs Ironic's `provision_updated_at`, which the CRD does not yet
  expose. **No predicate is better than one that fires on every healthy
  deploy.**
- **No pre-delete hook.** See §8.
- **No operator-settable Ironic microversion.** `ironicApiVersion` exists in
  two charts' values, in four manifests and on the live cluster CR, and is
  read by **nothing** — verified by checking every top-level values key
  against the whole template tree; it and `ironicApiUrl` were the only two
  with zero mentions, while the control (`ironicAuth`) resolves to
  `_helpers.tpl:714`. The microversion is declared in the OAS documents
  instead, `required` with a default the apiserver stamps onto every generated
  CR.

  The inert key is worth more attention than a dead value usually deserves,
  because **it is dangerous precisely because it is inert**. It reads exactly
  like the knob that controls the microversion, and it is set to `1.81` —
  below the 1.83 floor for the findby's `include_children=true`. Anyone who
  closes the apparent disconnect by wiring it through reintroduces the 406
  described in §9. Both keys are annotated in place and are to be deleted at
  the next parent chart bump; they are not deleted yet because removing them
  from `values.schema.json` regenerates the CRD and would prune the field from
  a live conformant cluster's CR.

## 6. Operating it

| Task | How |
|---|---|
| Declare a cluster | One manifest, six nodes, no IPs. See `manifests/kubernetescluster-lab-ha.yaml`. |
| Add a worker | Add to `workers.nodes[]`. |
| Remove a worker | Move to `workers.removed[]`; drains, undeploys, then drops. |
| Recover a failed CP | `controlPlane.recovery.failedNodes[]` — ungated, since a drain gate can never open for a machine that never joined. |
| Pause the control plane | `docker stop` the kind node, then `make chart-host` on resume. See [`../local/README.md`](../local/README.md). |
| Restore etcd | [`RUNBOOK-ETCD-RESTORE.md`](RUNBOOK-ETCD-RESTORE.md) |
| Recover orphans | [`ORPHAN-RECOVERY.md`](ORPHAN-RECOVERY.md) |

**Pausing is safe; an empty Ironic is not.** Both destructive transitions are
gated off while machines sit at `active`: `transition-deploy` requires
`provisionState == "available"`, and that helper reads the *CR's* status via
helm `lookup` rather than Ironic, so it holds its last value while nothing
reconciles. `transition-undeploy` requires `undeploy: true` or
`shouldAutoRedeploy`, and the latter needs a non-empty
`deployed_configdrive_hash`, which the bridge deliberately does not mirror. The
dangerous case is an Ironic that returns **empty**: the CRs re-enrol the nodes
(Ironic honours a client-supplied uuid), the chart walks them to `available`,
and deploy fires and re-images a live machine.

---

## 7. Things that report success while failing

These cost real time and are the most transferable part of the work.

**Render-time versus runtime.** Any value derived from a provisioned machine is
empty when Helm renders. `{{ if }}` and `{{ with }}` then silently omit the
block — so a template that looks correct produces a cluster missing a critical
field. `controlPlaneEndpoint` was omitted this way at `kubeadm init`, and the
cluster could never have been HA. The trap within the trap: `curl -k` returned
200 against the apiserver, which looked like proof it worked. It was not — the
certificate SANs did not contain the VIP. The fix is placeholders substituted
at runtime, plus assertions that fail loudly.

**Whole-map replay.** `rest-dynamic-controller` sends a `spec` field holding a
map **entire** on every update. Where the CR models only part of what the
vendor holds, the rest is deleted, with no warning and no condition. A sidecar
mirroring 2 of 4 keys in `node.extra` caused Ironic to lose the other two
within a measured **82 seconds**, taking the control-plane VIP with them.

**RFC 7386 merge-patch does the opposite.** An omitted key is left *unchanged*;
only an explicit `null` deletes. A "clear" written by omitting keys deleted
nothing for its entire existence while logging `cleared ...` every ten seconds.

**Ironic's collection is abridged twice.** `GET /v1/nodes` omits child nodes
*and* all but seven fields. `parent_node=None` from a collection means
**absent**, not null. `include_children=true` is now on every `findby`.

**cloud-init discards unparseable user-data silently** while `network_data`, a
separate document, still runs — so the machine boots and looks alive with none
of its configuration applied.

**An instrument reporting zero is a claim about the instrument.** Every
negative in this repo is now paired with a known positive. This caught, among
others: a `grep -c` matching a comment rather than the flag; `pgrep -f`
matching its own command line; and a search for a string that did not exist
anywhere, whose "hits" came from a different branch of the same alternation.

---

## 8. Why the pre-delete hook was removed

Worth its own section, because the conclusion generalises beyond this repo.

1. **No RBAC.** core-provider derives a composition's permissions from a helm
   dry-run **install**, and helm keeps hooks in `release.Hooks`, not in
   `Manifest` — so no hook of any phase gets a grant. The clean demonstration:
   one template rendered a ServiceAccount, a Role, a RoleBinding and a Job;
   only the Job carried `helm.sh/hook`; the generated Role granted the first
   three and nothing for `batch/jobs`. helm's `before-hook-creation` then could
   not delete the hook either, turning every uninstall into a permanent
   failure.
2. **An unobservable failure.** The hook POSTed with `curl -sS` and no
   `--fail`, output to `/dev/null`, so an HTTP 422 was indistinguishable from
   success. It logged "creating NodeProvision" on a twenty-minute loop while
   zero such CRs existed.
3. ~~**The one that kills the idea.**~~ **RETRACTED — this was wrong.** I
   claimed rdc's `krateo.io/oas-version` informer selector made hook-created
   CRs permanently invisible, because cdc injected that label and no chart
   could set it. The selector is real (shipped in **0.28.0**, not 0.50.0), but
   **cdc does not inject the label** — a `MutatingAdmissionPolicy` that
   oasgen-provider creates per API group does, on CREATE and UPDATE, for
   `resources: '*'` with empty namespace and object selectors and **no client
   filtering**. Measured with both arms:

   ```
   kubectl, no labels in the manifest, --dry-run=server : krateo.io/oas-version=v1-0-0
   same object,                        --dry-run=client : none        <- CONTROL
   ```

   So a hook-created CR **is** labelled and **would** be reconciled. I never
   observed otherwise — the hook's CR was never created at all (defect 1), so
   the question never arose. I saw the label sitting beside the
   `krateo.io/composition-*` labels that cdc really does set, and read a
   correlation as a mechanism.

   I initially kept a caveat here that this is version-dependent, since
   `MutatingAdmissionPolicy` is GA at Kubernetes 1.36 and that is exactly the
   generated CRD chart's floor. **That is not a coincidence and not a
   hazard**: per the oasgen-provider maintainers, GA of that policy is
   *why* the floor is `>=1.36.0-0` — CRD schema ratcheting, the other
   candidate, has been GA since 1.33 and cannot be what sets it. The floor is
   enforced at render time, so below 1.36 the chart is refused rather than
   silently installed. There is therefore no supported cluster where the
   policy is inert while selector-scoped watching is active, and the
   unlabelled-CR case is unreachable wherever the chart can be installed.

   There is no migration window either. An unlabelled CR predating the policy
   is **backfilled** on the next RestDefinition reconcile, by a pass that
   selects on label *absence* (`!krateo.io/oas-version`) and is therefore
   self-limiting — once it has run the list comes back empty, so it runs every
   reconcile rather than being a migration step someone could skip. Three
   independent mechanisms, then: admission stamping with no client filtering
   (measured above), a chart floor that refuses to install below 1.36, and the
   backfill.

   The version-dependent behaviour did exist, for exactly one release: at
   0.27.0 the label was written by the policy *or*, below 1.36 where that API
   does not exist, by the controller. 0.28.0 raised the floor and made the
   policy required, closing it by removing the second code path rather than
   maintaining both.

---

## 9. Open upstream issues

| Issue | Status |
|---|---|
| core-provider: hook RBAC omitted from generated permissions | Filed as **#311**, fix identified — `rel.Hooks` is already populated before the dry-run returns, and hook collection is phase-agnostic. |
| oasgen: `requestFieldMapping` deletes the spec property it reads from | Confirmed by maintainers. `node_id` is absent from `NodeProvision`/`NodePower` specs; `BaremetalNode` escapes only because its mapping is `uuid <- spec.name`, two different names. |
| oasgen: silent removal of a suppressed property | **#409**, fixed on `main`, unreleased. |
| rdc: a CR ignored for a selector mismatch reports nothing | Open. May be structural — a selector-scoped informer never sees the object, so there is nothing to attach a condition to. |
| core-provider **#304** ("cdc never prunes") | **Withdrawn — it was wrong.** Removal from spec does fire `deleted:1`. The earlier `deleted:0` readings were reconciles in which nothing had been removed. |

### The transitions are refused on 0.50.0 — and that is the guard working

This is the most important open item and it is operational, not academic.

A path parameter has exactly four writers, precedence highest first:

```
fieldMapping > requestFieldMapping > Configuration CR > spec > status
```

Ours is a `requestFieldMapping` reading `spec.node_id`, and **all four sources
are empty**: the CRD has no `node_id` property (upstream generation bug,
**#419**); status is never populated for it, because `populateStatusFields`
reads only a response body while our findby items carry no `node_id` and the
PUT returns 202 with no body — the generator already warns `status field
'node_id' not found in response`; and `NodeProvisionConfiguration` has no
`path` property at all, because the RestDefinition declares no
`configurationFields`. The mapping then finds nothing and **silently skips**
(`builder.go:215-217`: `if err != nil || !found { continue }`).

**The history, stated carefully, because the obvious reading is backwards.**

| | |
|---|---|
| rdc **0.47.0** | No guard. The **unsubstituted template went on the wire**: `PUT /v1/nodes/%7Bnode_id%7D/states/provision`. A real API answers that plausibly, which is worse than a failure — it is exactly what upstream **#363** was filed for. |
| rdc **0.48.0** | Added detection and the guard. The request is now **refused**: `unresolved path parameter(s) [node_id]`. |
| rdc **0.50.0** | **#388** fixed the population half for path-item-declared parameters. |

The cluster was provisioned on 2026-11-03 under 0.47.0;
`KRATEO_RDC_VERSION` moved to 0.50.0 on 2026-11-04. **So the upgrade did not
break a working system — it made a pre-existing silent failure visible.**
Expect a refusal on the next deploy, undeploy, power-cycle or clean, and read
it as the guard doing its job.

**#388 does not apply here.** `oas/ironic-provision.yaml` and
`oas/ironic-power.yaml` declare `node_id` at **operation** level, not path-item
level, so it always reached `ReqParams`. (The two kinds that *do* use path-item
parameters, `node` and `port`, are also the two whose identifiers survive in
the spec — but that is coincidence: `uuid` and `node_uuid` survive because they
are request-**body** fields.) **#419 is the sole remaining blocker**: fix it and
these transitions work on 0.50.0 with no chart change.

**Two things not to do.** Do not remove `spec.node_id` from the
`transition-*.yaml` templates — it is the field the mapping reads, so they are
forward-compatible, and removing it is the one edit that would stop the
upstream fix from reaching this repo. And do not pin rdc back to 0.47.0: that
only restores the silently malformed URL.

**What remains genuinely unknown.** Something walked six blades to `active` on
2026-11-03, and given the URL that was actually on the wire it was **not** a
successful PUT through these kinds. How those machines were provisioned is an
open question in this repo, and §10's description of the procedure should be
read with that in mind rather than taken as established.

## 10. Reproducing the conformance result

See [`conformance/v1.36/README.md`](../conformance/v1.36/README.md) for the
full procedure. In short: apply the four RestDefinitions and the two
CompositionDefinitions, apply one `KubernetesCluster` manifest naming six
machines and no IP addresses, wait for six `Ready` nodes (about 25 minutes,
most of it `apt` and image pulls), then run `hydrophone` **on the bootstrap
node** so the cluster-admin kubeconfig never leaves the machine that owns it.

The submission metadata is in
[`conformance/v1.36/PRODUCT.yaml`](../conformance/v1.36/PRODUCT.yaml). Every
field is filled and verified except the vendor identity, which is a question
about which legal entity signs CNCF's participation form rather than anything
technical; the file records the options and what each costs.
