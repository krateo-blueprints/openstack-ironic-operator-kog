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

**It did not work until 2026-11-04, and the way it failed is the point.** The
installer fetched its checksum manifest from `<base>/checksums.txt`. That asset
does not exist: hydrophone publishes `hydrophone_<version>_checksums.txt`, with
the leading `v` stripped from the tag, and has done so for all nine releases.
So `curl --fail` returned 404, the script refused to install an unverified
binary — correctly — and exited 1. Being last in `runcmd`, it never gated
bring-up. Measured in an amd64 Debian 13 container, which is what the blades
run:

```
+ curl --fail -sSL -o .../sums .../v0.8.0/checksums.txt
curl: (22) The requested URL returned error: 404
FATAL: no checksums.txt for v0.8.0; refusing to install unverified binary
hydrophone after: ABSENT
/var/lib/conformance: No such file or directory
```

The remedy had acquired the disease it was written to cure: a runner baked in
so a re-image could not silently remove it, which silently installed nothing.
`--fail` and the refusal-to-install were both right; the URL was wrong, so good
error handling converted a typo into a silent no-op. **A correct guard on a
wrong target still fails closed, and failing closed quietly is the shape that
survives review.**

Three things changed. The checksum name is now derived (`${VER#v}`) and held in
a variable, so the URL and the error message cannot drift apart again — the old
message named `checksums.txt` and was perfectly accurate about a file that had
never existed. A failure now leaves `INSTALL-FAILED.txt` in the output
directory, because previously the directory was not created at all and "the
runner failed" was indistinguishable from "the runner was never enabled" at the
one place anyone looks. And `hydrophone --version` prints a multi-line build
block whose first line is the program name, so the `| head -1` that recorded
what got installed recorded `hydrophone` and no version; it now parses the
`version:` row and writes it into `HOW-TO-RUN.txt`.

**Why this was not caught by running it on blade01.** blade01 already has a
hand-installed hydrophone, so the script short-circuits on `command -v` and
reports success without ever reaching the download. The only environment that
can exercise the install path is one where the binary is absent — the arm that
looks redundant is the only one that measures anything.

Six arms, all in one container run: fresh install; re-run with the binary
already present; a bogus version, which must fail **and** leave the marker
(exit 22, `hydrophone: ABSENT`, marker written); and the checksum verification
against both a corrupted tarball (`FAILED`, exit 1) and the real one (`OK`,
exit 0) — because a verification step that cannot reject anything is decoration,
and the refusal-to-install is the whole reason the checksum is fetched.

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
- A **`wg-lab-route` DaemonSet** terminates a WireGuard tunnel to the lab. It
  parses nothing and rewrites nothing, deliberately. It replaced an earlier
  `wg-ironic-proxy` Deployment in the "zero proxies" work; that manifest was
  **deleted on 2026-11-04** because it also declared `Service/ironic`, so
  `make lab-tunnel-up` would have overwritten the working Service with a
  selector aimed at pods that no longer exist — breaking the only route to
  Ironic. The bridge script that used to be a sidecar in that pod now runs as
  its own `ironic-extra-bridge` Deployment.
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

**A credential audit, and the two boundaries that make its result readable.**
`--debug` is on for *any* oasgen chart install — the deployment hardcodes
`args: ["--debug"]`, and the chart's own `OASGEN_PROVIDER_DEBUG: "false"` is
inert config beside a working `MAX_RECONCILE_RATE` (oasgen **#454**). Since this
project's `values.yaml` records that "oasgen < 0.26.1 printed credential headers
under this flag", that was worth measuring rather than assuming.

Searched every pod in both namespaces, completed Jobs included,
`--all-containers`, for the **live Keystone token** and the **non-rotating admin
password** — the second being the one that matters, because a leak of it does
not self-heal. **62 pods, zero hits**, with both greps calibrated against known
positives so the zero is a result rather than a pattern that never matched. Also
zero instances of `x-auth-token`/`authorization` followed by a 20+ character
value; header *names* appear, and a name is not a secret.

**What the clean result does NOT cover**, because both boundaries are
configuration rather than code:

1. **rdc runs at INFO here, not DEBUG.** It is passed no debug flag and logs
   300/300 INFO with zero DEBUG lines — the opposite of oasgen in the same
   monorepo, which the upstream maintainer confirmed is a genuine asymmetry
   between the two components. So the audit says nothing about what rdc would
   emit at DEBUG.
2. **`debug.connectorVerbose` is off**, and that — not oasgen's `--debug` — is
   the flag that dumps the full HTTP exchange with Ironic. Verified absent on
   all six `BaremetalNode` CRs. It is the actual disclosure path in this system.

And the scope is the **retained** log window (last 3000 lines per pod), so this
is "not present on a live fleet", never "never".

The transferable half, which cost an upstream maintainer a wrong safety
assumption: **the audience of a log level is a deployment property, not a code
property.** "We log it at Debug so it is not in production logs" is false for
one component and true for the other, in one repository, with nothing in either
codebase telling a reader which side a given statement is on.

**And the same error in a different costume, twice in one evening:** I read
`ALREADY RUNNING` from a `pgrep` in a pod log and briefly believed a two-hour
conformance suite was running — it was a completed Job from 36 hours earlier.
Earlier the same night I read the extra-bridge's DNS errors as current when they
were a window that had closed nine hours before. **A log line, a process listing
and an issue body are all snapshots; the timestamp is part of the datum.** Find
out when it was written before deciding what it says about now.

**And the inverse: a healthy fleet that reports failure forever.** Every
`BaremetalHost` sits at `Ready=False`, reason `Creating`, message *"1 of 4
managed children are not ready: nodepowers/..."* — permanently, on a cluster
where all six machines are `active` and powered on. That is correct. A
`NodePower` CR exists to fire one `PUT .../states/power`; Ironic answers `202`,
which sets `Ready=False/Pending`, and that Pending condition is precisely the
guard that stops the PUT re-firing on the next Observe. A transition CR that
reached `Ready=True` would be one that had lost its only protection against
repeating a power operation. So **`Synced`, not `Ready`, is the health column
for anything owning a transition CR** — and `kubectl get baremetalhosts` shows
`Ready`. Checked here on 2026-11-04 rather than assumed: `Synced=True
ReconcileSuccess` on all six, `provision_state=active` and `power_state=power
on` on all six `BaremetalNode`s.

**A verb granted but never used, beside a verb used but never granted.** The
CP publisher Role carried `create, get` on **all** secrets in the lifecycle
namespace. The script never issues a `get` — it POSTs and, on `409`, PATCHes —
and `patch` was not granted. So the over-broad grant was dead, and the path
that needed a grant 403'd. It had never run: a first boot POSTs `201` and
exits, so only a **re-provision** reaches the `409` branch. Found by Trivy
KSV-0113 and then confirmed, not argued, with `kubectl auth can-i` against the
live ServiceAccount: `patch secrets` → **no**, `get secrets` → **yes**,
including `<cluster>-kubeadm-pki`. Now three rules — `create` unrestricted
(authorization cannot see an object's name on a create), `get`/`patch`
restricted by `resourceNames` to the one Secret the chart owns — verified by
applying the rendered Role to a scratch namespace and asserting both
directions, including that the worker CRs and the PKI Secret come back `no`.

The transferable part is where the lie lived: a comment at the **top** of the
file said the Role "grants ONLY the patch verb on a single BaremetalNode CR".
That was true when written. A second rule was appended at the **bottom** months
later, and nothing connects the two — **a file-level comment describing
rules that are edited far away from it will drift, and review reads the comment
as the specification.**

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

**`curl` without `--fail` turns an HTTP error into a success.** `curl -sS`
exits **0** on a 4xx or 5xx — `-s` silences the progress meter, not the
outcome. Send the output to `/dev/null` as well and a rejected request is
indistinguishable from an accepted one. This repo's `pre-delete` hook POSTed a
CR that was missing a required field, got a 422 every time, and logged
`creating NodeProvision` on a twenty-minute loop while **zero** such CRs
existed anywhere in the cluster.

Generalised, because it will recur for anyone doing the same thing: **a hook,
sidecar or cloud-init script that POSTs a generated CR directly must use
`curl --fail` and must supply `configurationRef`**, which every generated kind
requires and which nothing in the request reminds you of. The failure is not
that the write is rejected — that is recoverable. It is that the script then
reports having done it. See §8 for the full post-mortem.

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

**What decides whether a path parameter keeps a spec property** — corrected
after a fourth measurement, because the obvious rule fit the data and
discriminated nothing:

| kind | mapped param | same name? | in a body schema? | property in CRD |
|---|---|---|---|---|
| `baremetalnodes` | `uuid` | no (`spec.name`) | **yes** (`NodeCreate`) | **present** |
| `ports` | `uuid` | no (`status.uuid`) | no | **absent** |
| `nodepowers` | `node_id` | yes | no | **absent** |
| `nodeprovisions` | `node_id` | yes | no | **absent** |

**`ports`' exemption is an accident of a mapping on a different verb.** Asked
to verify the four transcriptions, 2026-11-05, and it found an asymmetry in this
very table:

```
ports            GET /v1/ports/{uuid}             (no mapping)
portgroups       GET /v1/portgroups/{uuid}        uuid <- spec.name
allocations      GET /v1/allocations/{uuid}       uuid <- spec.name
deploytemplates  GET /v1/deploy_templates/{uuid}  uuid <- spec.name
```

`ports` is the only one whose **by-uuid GET has no mapping**, so that verb's
required `uuid` is satisfied **only by status** — `additionalStatusFields`
carries it. That is exactly the oasgen #455 shape. It escapes #410's guard today
only because `mappingSuppliedParams` is computed across **all** verbs, and the
DELETE verb's `uuid <- status.uuid` marks `uuid` as supplied globally.

**Remove the DELETE verb or its mapping and `Port` becomes uncreatable** —
`uuid` stops being mapping-supplied, enters `requiredParameters`, the guard
refuses the exclusion, and all 12 live `Port` CRs with an empty `spec.uuid` are
rejected. oasgen #456 is what makes this safe by design rather than by side
effect.

**And the check caught an error of mine.** The arm I contributed upstream
transcribed `additionalStatusFields` as empty for portgroups, allocations and
deploytemplates. They are not: `[uuid, node_uuid, address]`,
`[uuid, state, node_uuid]` and `[uuid]`. #456's new exemption is *precisely*
about status-carried parameters, so omitting them under-specified the exact
thing under test — three of four fixtures would have exercised only the mapping
exemption and not the status one. The maintainer declined to take the file into
their repo partly because they could not verify these transcriptions; they were
right, and the thing they could not check was wrong.

`ports` is the row that separates the two candidate explanations: its mapping
is **not** self-referential and the property is absent anyway. So the
discriminator is not "the mapping names its own parameter" — it is **whether
the property is independently declared in a request body schema.** A
`requestFieldMapping` suppresses; a body declaration rescues. That also
reconciles with oasgen's `TestPathItemParameters_BecomeSpecFields`, where a
path-only parameter with *no* mapping becomes a spec field: three cells of one
table rather than two competing rules.

**But "absent" and "defective" are different questions, and they come apart on
that same row.** The table above answers *what determines absence*. Whether an
absence is a **bug** turns on something else — whether the author was supposed
to supply the value:

| row | absent | is the absence a defect? |
|---|---|---|
| `ports`: `uuid <- status.uuid` | yes | **No — correct.** The vendor assigns the uuid; an author cannot supply it, and requiring it would make the kind uncreatable. |
| `nodepowers` / `nodeprovisions`: `node_id <- spec.node_id` | yes | **Yes.** The mapping reads a property the author *is* meant to supply, and the suppression deletes its own source. |

So self-reference is the right key for the upstream **exemption** even though
it is not the discriminator of the observed outcome, and `ports` must stay
suppressed. Do not "fix" it by analogy with the other two.

This document previously asserted the same-name rule, citing `BaremetalNode`
escaping "because its mapping is `uuid <- spec.name`, two different names".
The different names are incidental; it escapes because `NodeCreate` declares
`uuid` in its body.

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

**0.51.1 unparks this, and the body is not a second problem.** The obvious
worry — that restoring `node_id` to the spec would serialise it into a PUT
body Ironic does not expect — does not apply. `processFields` gates every body
write on the document's declared body schema (`ReqParams.Body.Contains`), and
the provision body declares only `target`, `configdrive` and `clean_steps`.
Serialisation follows the body schema, not the spec, so Ironic keeps seeing
`{target: ...}`. A sibling KOG provider *is* parked on exactly that problem,
because its OAS declares the path parameter in the body schema deliberately —
the two cases come apart on that one line.

### How to adopt an oasgen generation fix without de-enrolling the fleet

**An oasgen upgrade does not regenerate existing CRDs.** Observe compares four
stored hashes — `status.digest`, `status.resourceHash`, `status.oasHash`,
`status.authSecretDigest` — and **none covers the generator's own version**. So
a new binary changes nothing Observe looks at, Observe reports up-to-date, and
generation never runs again. A CRD built by 0.50.0 keeps its 0.50.0 schema
indefinitely under 0.51.0. Filed upstream as oasgen **#453**.

That matters here because delete-and-recreate — the obvious way to force a
re-render — **fires the external DELETE on every live CR of the kind**. With
six `BaremetalNode`s and twelve `Port`s on the fleet, that means de-enrolling
machines from Ironic. It is not available in production.

> **An earlier version of this section said the nudge was UNSAFE on `ports`,
> `portgroups`, `allocations` and `deploytemplates`. That was wrong and is
> retracted.** The reasoning was that each sets `excludedSpecFields: [uuid]`
> while its OAS declares `uuid` as a path-level `required: true` parameter, and
> that oasgen 0.51.0 (#410/#414) refuses such an exclusion.
>
> **Measured instead of reasoned:** the `ports` CRD generated *by 0.51.0* on an
> isolated cluster (uid `3fc83f7d…`, created `22:25:30Z`, after the upgrade)
> has `uuid` absent from both `properties` and `required`. The exclusion was
> applied.
>
> The predicate I tested was `excluded ∩ required-parameters`. The guard's
> actual condition is `excluded ∩ (required-parameters NOT supplied by a
> mapping)` — and all four supply `uuid` through a request-direction
> `requestFieldMapping` (`uuid <- status.uuid` for ports, `uuid <- spec.name`
> for the other three), which oasgen deliberately exempts. **I had the
> exemption in front of me and modelled the predicate without it.**
>
> Three successive static answers, each more careful than the last, were all
> wrong; the first thing actually run settled it. A correct answer to one half
> of a conjunction reads as confirmation of the whole.
>
> oasgen **#455** remains a real breaking change for a definition whose
> excluded required parameter is *not* mapped. This fleet is simply not one.

**The safe path is to edit the referenced OAS document**, because `oasHash`
*is* hashed:

```
1. append a comment to the OAS document (that alone moves oasHash)
2. re-apply the ConfigMap
3. WAIT for the CRD's resourceVersion to move
```

Measured on an isolated cluster, `nodepowers`, both directions:

| step | oasHash | CRD rv | result |
|---|---|---|---|
| inert comment | `4e5c1ba2` → `e11a30b8` | unchanged | reconcile ran; re-render identical, so correctly nothing written |
| semantic edit (new body field) | `e11a30b8` → `fdf88f53` | `22874` → `23895` | the field **appears** in the generated spec |
| ConfigMap restored | — | `23895` → `24204` | the field **disappears** again |

No DELETE dispatched, no RestDefinition recreated, no CR touched.

**Step 3 is not optional, and it is where this nearly went wrong.** A first run
polled 96 seconds, saw neither the CRD nor `oasHash` move, and was one message
away from reporting that `oasHash` detection is inert — with a passing control
(the live ConfigMap did contain the new field), so the reading looked solid.

The cause is a **3-minute default poll interval**
(`OASGEN_PROVIDER_POLL_INTERVAL`, `main.go:46`), which matched the observed
gaps: `22:22, 22:25, 22:26, 22:27, 22:28, 22:31, 22:34`. The 96-second window
fell inside one gap. **Before trusting that a trigger did not fire, find out how
often the thing could have fired at all** — a correct control proves your edit
landed; it cannot prove your observation window was longer than the mechanism's
period.

**And the wait is settable, which removes the caveat rather than warning about
it:**

```
helm upgrade krateo-oasgen-provider ... --set env.OASGEN_PROVIDER_POLL_INTERVAL=20s
```

Verified on the isolated cluster rather than taken on trust — measured gaps
after the override: `20, 22, 20, 21, 20, 21` seconds, median **21s**, against a
120–180s baseline.

**Why that channel works here and not everywhere:** the oasgen-provider
container takes `envFrom` a ConfigMap built from `.Values.env`, and the
deployment hardcodes only `args: ["--debug"]` — **no `--poll`** — so the env
var governs. Where the chart *does* pass the flag, the flag wins and the env
var is dead config. `OASGEN_PROVIDER_DEBUG` is exactly that: both this cluster
and production carry `OASGEN_PROVIDER_DEBUG: "false"` while `--debug` is
passed, and the process emits 190 DEBUG lines to 10 INFO. A value that reads as
load-bearing and governs nothing.

**On production the interval is unset**, i.e. the 3-minute default, on oasgen
0.50.0. Deliberately left alone: a one-off nudge is not worth permanently
tripling the fleet's reconcile rate.

### Baseline measured 2026-11-04 on 0.50.0 AND 0.51.0 — the defect is live on a tag

Taken on an **isolated** kind cluster (own kubeconfig, fake-driver Ironic,
nothing near the fleet) with the real `restdefinition-provision`, `-power` and
`-port` manifests, so the eventual 0.51.1 run is a **difference** rather than a
prediction read on its own.

| kind | 0.50.0 | 0.51.0 | the key |
|---|---|---|---|
| `nodeprovisions` | `node_id` **absent** | `node_id` **absent** | — |
| `nodepowers` | `node_id` **absent** | `node_id` **absent** | — |
| `ports` | `uuid` **absent** | `uuid` **absent** | control arm |

Property sets are **identical** across the two versions, not merely
similar — `['X-OpenStack-Ironic-API-Version','clean_steps','configdrive','configurationRef','target']`
for provision, `[...,'target','timeout']` for power, and for ports
`['address','configurationRef','extra','local_link_connection','node_uuid','physical_network','portgroup_uuid','pxe_enabled']`.
Note `ports` keeps `node_uuid` (a body field) while losing `uuid` (the mapped
path parameter), which is the suppression and its boundary in one object.

**0.51.0 does not carry #419's fix**, confirmed independently by the
maintainer: #421 is the fix and it is an **unmerged draft**, so it is in no
tag. The two adjacent 0.51.0 log lines are #410 (`excludedSpecFields`, a
different suppression path) and #409 (a *warning* about an omitted parameter).
0.51.0 unblocks *cutting* 0.51.1; it does not unblock this probe.

**The change control matters more than the result here.** A first attempt read
the CRDs after upgrading and found them unchanged — but the resourceVersions
had not moved (`22053/22069/22086` before and after), because re-applying an
unchanged RestDefinition is a no-op and nothing regenerated. That "no change"
would have been a stale schema, not a measurement. Deleting and recreating the
three RestDefinitions forced regeneration by the 0.51.0 binary
(`22863/22874/22887`), and only then is "identical" a fact. Checked first that
all three kinds had **zero** live CRs, because deleting a RestDefinition fires
the external DELETE on any that exist.

**So prediction 3 now has the prior state it needs.** "`ports`' `uuid` stays
absent after the fix" was a weak claim while nobody had established it was
absent before; it is established on both tagged versions now.

**What the 0.51.1 run should show — written down before running it**, so the
result is read against a prediction rather than rationalised after:

1. `spec.node_id` returns to the `nodeprovisions` and `nodepowers` CRDs, and
   returns **required**, not optional. The parameter is `in: path,
   required: true` in both OAS documents, and under the fix a non-suppressed
   parameter carries its `Required` through to the property. **An optional
   property would be a difference between this repo's documents and the
   upstream model, and is worth chasing before reading anything into runtime
   behaviour.**
2. A throwaway CR naming a node that does not exist produces **Ironic's own
   404**, not the guard. The guard message would mean the fix is wrong rather
   than late; a 400 about an unexpected body field would mean serialisation
   does not follow the body schema after all.
3. **`ports`' `uuid` stays ABSENT.** This is the control arm on the upstream
   fix, and it is the one most likely to be skipped. `ports` maps
   `uuid <- status.uuid`: the vendor assigns the uuid, an author cannot
   supply it, and requiring it would make the kind uncreatable — so its
   absence is *correct*, not a defect, and #419 must leave it alone. If
   `uuid` appears in the `ports` CRD after the fix, **the fix over-reached**,
   and that is a worse outcome than it not landing. Same shape as the
   over-eagerness arms in `scripts/check-example-manifests.py --self-test`:
   the risk is not only that a rule stops firing, it is that it starts firing
   on what it should leave alone.
4. Nothing breaks at admission. Checked ahead of the tag: **all 13 templates
   across the three charts that render a `NodeProvision` or `NodePower`
   already set `node_id`**, so a property that becomes required finds a value
   waiting. This is the concrete reason the instruction below matters — had
   those fields been tidied away as dead weight, every transition in all
   three charts would have started failing admission on the day the fix
   shipped.

**When the run happens, tell the nova KOG session.** They are running the
mirror of this against Nova's quota kind — same upstream fix, a different
vendor, a different verb set (`create`/`get`/`delete`, no `findby`), and
their own four arms including the same over-reach control on their four
`status.*`-sourced mappings. They stopped watching for the tag, so the ping
has to come from here. Report this run as **one vendor, one verb set** and
name which prediction falls, including when none does — that is where the
temptation to generalise is strongest.

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

**The CNCF submission is parked**, by the owner's decision on 2026-11-04. Not
blocked, not waiting on anyone — parked. What is parked is the *submission*,
not the result: the run stands on its own, the artefacts and checksums are in
this repo, the procedure above reproduces it, and the runner is now in the
chart so a re-image cannot lose it.

Resuming needs two non-engineering things: a decision about which legal entity
certifies, and one email to CNCF asking what that costs.
[`conformance/v1.36/PRODUCT.yaml`](../conformance/v1.36/PRODUCT.yaml) records
both, along with every field that *is* settled and verified.

The one thing that ages: CNCF's certifiable window is the current Kubernetes
release plus the two before it, so v1.36 stays submittable while 1.37 and 1.38
are current. After that the suite must be re-run against a newer release —
which is a chart flag now rather than a hand-install, so resuming late costs a
bring-up and two hours rather than a rediscovery.

### §9.7 — a document-level screen for #419, and the #457 audit that produced it

oasgen #457 asks a question we had not asked: `mappingSuppliedParams` is built
once over **every** verb into one flat map keyed by parameter name, with no verb
context, so a mapping declared on one verb removes the spec property for **all**
verbs. A verb with neither a mapping of its own nor a status source then has
nowhere left to read the value from, and nothing warns.

Audited all seven RestDefinitions, per verb, per required parameter:

```
UNRESOLVABLE combinations: NONE
```

with two rows worth naming:

| resource | verb | resolvable by |
|---|---|---|
| `ports` | `GET /v1/ports/{uuid}` | `status` **only** |
| `NodePower`, `NodeProvision` | `PUT` | `mapped-here` **only** |

`ports` is fine via status, exactly as #457 predicted. The second row is the
interesting one, because it made the audit's own weakness visible: **"mapped-here"
is necessary, not sufficient.** A mapping is only usable if its *source* exists —
and #419 is precisely a mapping whose source is the property the mapping itself
suppressed. The first audit would have cleared it.

Refining it to check each mapping's `inCustomResource` actually resolves:

```
restdefinition-port.yaml       DELETE  uuid     <- status.uuid    OK
restdefinition-power.yaml      PUT     node_id  <- spec.node_id   BROKEN (mapping-suppressed)
restdefinition-provision.yaml  PUT     node_id  <- spec.node_id   BROKEN (mapping-suppressed)
```

That is **#419, found from the documents alone** — no cluster, no API call. It is
shipped as `scripts/check-mapping-sources.py` / `make check-mapping-sources`.

Its calibration is the part worth keeping. #421 is an unmerged draft, so the two
broken mappings **cannot** legitimately be clean, which means a zero is not good
news — it is a broken instrument. The screen therefore carries the two known rows
explicitly and **exits non-zero when it finds none**. Verified three ways: it
finds the two known rows (exit 0), refuses a run where the defect was removed
from a copy (exit 2), and flags a planted third mapping with a non-existent
status source (exit 1).

This is [[calibrate-before-trusting-a-zero]] applied to a static check rather
than to a measurement, and it is the first screen here that answers a wire-level
defect without touching the wire.

### §9.8 — promoting 0.13.25, and what the pre-flight found

0.13.25 is live: CompositionDefinition moved 0.13.23 → 0.13.25, cdc upgraded the
release to revision 81, all six blades stayed `active` / `power on`, and no
`NodeProvision` CR exists — so nothing reimaged. baremetal-host 0.6.2 needed no
move; it had been live on all six since 2026-11-04 13:07, so this was a
single-chart promotion and the parent/child lockstep hazard never applied.

**The pre-flight found a defect review had not.** See §9.7's commit: the parent's
`lifecycleApiVersion` fallback named `v0-5-6`, a version no longer served, and
the field is optional with no schema default — so a fresh cluster CR would have
wedged. Folded into 0.13.25 before promoting.

**Two readings I had to discard before trusting anything.** `helm upgrade
--dry-run` does not run `lookup`, so it rendered 147 lines, no BaremetalHosts,
and a diff against it "showed" the six hosts being deleted. Only
`--dry-run=server` renders the real thing (4088 lines). Separately a jsonpath on
`status.provisionState` returned empty for all six blades; the field is
`status.provision_state`, and reading the empty as data would have inverted the
next conclusion.

**The destructive path, and why it did not fire.** baremetal-host's drift
detection compares the rendered configdrive hash — which is literally the parent
chart version — against `node.extra.deployed_configdrive_hash`, and on a
mismatch at `active` it auto-fires transition-undeploy: Ironic wipes the blade
and redeploys. The hash moved 0.13.23 → 0.13.25 on all six, and the userData
genuinely changed. So on the face of it, promoting reimages the whole HA cluster
on one apply.

It does not, because `deployed_configdrive_hash` is **absent** from every node's
`spec.extra`, and v0.4.3 fires only when observed is non-empty. Verified by
dry-running the child itself against the live cluster at both hashes and with the
real new configdrive: identical inventories all three times (BaremetalNode,
NodePower, 2 Ports), no `NodeProvision` rendered, and the BaremetalNode body
unchanged but for the hash annotation. The new configdrive reaches Ironic only
through a deploy transition, which an already-active node does not render.

**Correction to the first version of this section: that safety is NOT accidental,
it is an interlock.** I wrote that the publish step had failed and the fleet got
lucky. It had not. `deployed_configdrive_hash` is absent from the CR because
`scripts/ironic-node-extra-bridge.py` mirrors an explicit allow-list and that key
is **deliberately excluded**, with the reason stated where it is excluded:

> DELIBERATELY ABSENT: deployed_configdrive_hash. [...] that rendered value is
> .Chart.Version rather than a content hash. Mirroring it would arm a re-image of
> every blade on every chart bump. Make it a real content hash first; until then
> leaving it unmirrored is what keeps the gate shut.

So the blade does publish the hash to Ironic; the bridge declines to carry it to
Kubernetes, and `observedConfigDriveHash` reads the CR. One deliberately omitted
tuple entry is the only thing standing between a chart bump and a six-blade
reimage — and it is load-bearing exactly as designed.

Two things follow that the "accidental" reading got backwards. The promotion was
never one interlock away from disaster by luck; it was held by a control someone
put there on purpose. And the real defect is not the inert detection — it is that
**the hash is a chart version rather than a hash of the configdrive**, which is
why the interlock has to exist at all. Fix that and drift detection can be armed
as designed: a bump that changes nothing in the configdrive would stop looking
like drift, and a genuine content change would redeploy. Until then the feature
ships inert, deliberately, and the comment is the only thing that says so —
nothing in the chart or the test suite asserts the exclusion is still there.

The way I got this wrong is worth keeping: I read an absent field, found a code
path that explains absence as failure, and stopped. The file that owns the field
explains it as policy, in a comment addressed to exactly the person I was being.

**The version-stripping window is real but short.** Both `v0-13-23` and
`v0-13-25` were served at t+12s; by the next read `v0-13-23` was pruned and only
`v0-13-25` served. The hazard in [[krateo-crd-version-stripping]] exists during
that window, not after it.

**Credential effect, measured on the rendered userData rather than reasoned.**
0.13.25 removes the inline never-expiring management-cluster SA bearer token
(1 → 0) that the dead `publish-workload-kubeconfig.sh` carried, and adds a third
copy of the Keystone **admin** password inline (2 → 3), because every Ironic
publisher authenticates the same way. Net clearly better — a scoped drain SA
replaces an admin.conf publish — but three plaintext copies of the Keystone admin
password ride in each blade's config drive. The right fix is a Keystone user
scoped to the Ironic writes these scripts perform, not `admin`; that is a design
change, not folded in here. **Open, unfixed, and not introduced by 0.13.25.**

### §9.9 — 0.13.26: the configdrive hash is a content hash, and why arming still waits

The defect §9.8 landed on was that `deployedConfigDriveHash` was `.Chart.Version`,
not a hash of the configdrive. That is why the bridge had to refuse to mirror it:
with a version as the hash, *every* bump looks like drift, and drift at `active`
means Ironic wipes the blade.

**Fixed properly.** `kubernetes-cluster.deployedConfigDriveHash` now returns
sha256 of the rendered userData, with its own value replaced by
`__CONFIGDRIVE_HASH_SENTINEL__` during the hashing pass. That sentinel is what
breaks the "hash includes hash" recursion which v0.4.2 had sidestepped by
reaching for the chart version. The builder is passed explicitly — the three node
classes render different userData and must hash their own — which also means the
three classes get three different hashes, as they should.

Calibrated three ways against the live cluster, because a hash that cannot move
and a hash that always moves are both useless:

| test | expected | result |
|---|---|---|
| render twice, same chart | identical | identical (3 values, 1/2/3 nodes) |
| bump chart 0.13.25 → 0.99.99, userData untouched | **unchanged** | unchanged |
| genuine one-line userData edit | **changed** | all three changed |

**And then the measurement that stopped me arming it.** Removing the bridge
exclusion looked like the natural last step. It is not, and the reason is on the
other side of the wire: what each blade has *already published* to Ironic is a
chart-version string from whenever it was last deployed. Read from Ironic
2026-11-05, via `include_children=true` because the node collection is abridged:

```
blade01  None        blade04  "0.13.18"
blade02  "0.13.19"   blade05  "0.13.18"
blade03  "0.13.19"   blade06  "0.13.18"
```

Three different stale values across six blades, and the bootstrap CP never
published at all. None of them can equal a content hash. So mirroring the key
today would set observed to a stale version string and fire auto-undeploy on the
**five** blades whose value is non-empty — including blade02 and blade03, two of
three control planes, which takes etcd quorum with it. blade01 is spared only
because v0.4.3 suppresses on empty-observed.

So the precondition for arming is not "the rendered hash is a content hash" — it
is **"every blade has republished under a chart that stamps one"**, which happens
on each blade's next deploy and cannot be made to happen any sooner without
deploying them. The interlock and its guard both stay, with that precondition
written into the guard alongside the measured values. This is the ordering of a
two-sided migration, not a hedge: the publisher has to move before the consumer.

Generalising, because it is the same shape as §9.8 one turn later: fixing the
value a system *renders* says nothing about the values it has already *stored*.
I had the first half measured and was one command from arming on the strength of
it.

### §9.10 — replacing the inline Keystone admin password with a scoped user

§9.8 recorded that each blade's config drive carries **three plaintext copies of
the Keystone `admin` password**, because every Ironic publisher
(`publish-join.sh`, `publish-stage.sh`, `publish-deployed-hash.sh`,
`publish-drain-kubeconfig.sh`, `resolve-node-extra.sh`) authenticates the same
way. A config drive is readable from `node.instance_info`, so this is a cloud
admin credential sitting where any Ironic reader can take it.

**The expected blocker is not there.** I assumed Ironic's default policy would
require admin for a node write, which would have meant shipping a custom
`policy.yaml` before anything could be scoped. It does not. The live Ironic runs
the default policy — no policy file, no `policy` setting in `ironic.conf` — and
the two rules these scripts need are:

```
baremetal:node:update_extra
  (role:member and system_scope:all) or rule:service_role
  or (role:member and (project_id:%(node.owner)s or project_id:%(node.lessee)s))
  or (role:service and system_scope:all)

baremetal:node:get
  (role:reader and system_scope:all) or ... or (role:reader and (project_id:%(node.owner)s ...))
```

**`member`, not `admin`.** So a correctly scoped user needs no policy file at
all, and `update_extra` is its own rule — distinct from `baremetal:node:update` —
so the publisher can be allowed to write `extra` without being able to change a
node's driver, power, provision target or instance_info.

#### The commands (yours to run — they create a credential on the live deployment)

```bash
# 1. a dedicated project the publisher lives in
openstack project create --description 'blade-side Ironic publishers' ironic-publishers

# 2. the user. Generate the password; do not reuse anything.
openstack user create --project ironic-publishers \
  --password-prompt ironic-publisher

# 3. member at SYSTEM scope -- satisfies both rules above for every node
openstack role add --user ironic-publisher --system all member

# 4. verify BEFORE changing the chart: it can read and write extra ...
openstack --os-system-scope all --os-username ironic-publisher \
  baremetal node show blade01 -f value -c extra

# ... and verify it CANNOT do the things it should not
openstack --os-system-scope all --os-username ironic-publisher \
  baremetal node set --driver fake-hardware blade01     # expect 403
openstack --os-system-scope all --os-username ironic-publisher \
  baremetal node power off blade01                      # expect 403
```

Step 4's second half is the part worth not skipping: a scoped credential is only
scoped if the negative case is checked. `member` at system scope is broader than
`update_extra` alone, so **measure** what it can reach rather than assuming the
rule list above is exhaustive.

Then set `ironicAuth.username` / `ironicAuth.password` on the KubernetesCluster CR
to the new user. No chart change is needed — the chart already parameterises
these — which is also why this was never a code problem, only a credential one.

**It takes effect per blade on its next deploy**, exactly like §9.9's hash: the
credential is baked into cloud-init, so existing blades keep the admin password in
their config drive until they are redeployed. Same publisher-before-consumer
ordering, same reason.

**Tighter option, if you want it later.** Setting each node's `owner` to the
`ironic-publishers` project lets the user hold `member` on that project only, with
no system scope — then it can write `extra` on *its own* nodes and is invisible to
every other node in the Ironic. That is strictly better and costs one field per
node, but it changes node ownership semantics, so it is a separate decision rather
than something to fold in here.

**Status: unblocked but not applied.** Nothing here runs without creating a live
credential, which is yours.

### §9.11 — six Makefile recipes reached a cluster without the isolated kubeconfig

Found while building regeneration tooling, not by looking for it. Six recipe
lines called a cluster through bare `kubectl` / `helm`, so they targeted whatever
`~/.kube/config` pointed at rather than `local/kubeconfig.<cluster>` with an
explicit `--context`:

```
Makefile:44  ./scripts/create-ironic-oas-configmap.sh $(IRONIC_NS)
Makefile:47  kubectl apply -f manifests/restdefinition-node.yaml
Makefile:48  kubectl apply -f manifests/restdefinition-port.yaml
Makefile:49  kubectl apply -f manifests/restdefinition-provision.yaml
Makefile:52  helm upgrade --install baremetal-lifecycle ./charts/baremetal-lifecycle
Makefile:77  helm upgrade --install ironic openstack-helm/ironic
```

Two of them install charts and one installs **Ironic itself**. Line 44 is the
subtle one: it names no `kubectl` at all, but the script it calls does
`KUBECTL="${KUBECTL:-kubectl}"` and the Makefile never passed it — so a recipe
can leak isolation without mentioning either binary. That is why the screen
checks the script invocation and not only the two command names.

The failure mode is what makes this worth a guard rather than a fix: a bare
command does not error. It succeeds, against the wrong cluster, and reports
success. Nothing distinguishes that from working.

`make check-examples` now fails on any recipe reaching a cluster without
`$(KUBECTL)` / `$(HELMK)`, with six self-test arms (24 → 30 cases) covering each
real shape plus two that must NOT fire — `helm package`, which never contacts a
cluster, and a commented-out `kubectl`. Calibrated the way that actually counts:
run against the **real pre-fix Makefile from git**, it reports all six.

**Separately, `apply-restdef` omitted `restdefinition-power.yaml`** while the
NodePower definition is live in the cluster — so the target left it at whatever
an earlier apply had put there. Added, with a note that allocation,
deploy-template and portgroup exist in `manifests/` but are not installed. Only
four of the seven RestDefinitions are live: node, port, provision, power.

### §9.12 — the regeneration nudge, validated as a positive control before it was needed

§9's regeneration procedure was prose. The one thing it asserted and nothing had
tested is that changing the referenced OAS ConfigMap moves `status.oasHash` and
makes oasgen re-read. If a comment-only edit did not reach the hash, the whole
procedure was void — and I would have found that out at the moment I needed it,
with the measurement blocked and a freshly-upgraded provider as a second
variable.

So I ran it on the **current** generator, 0.50.0, which is the safest possible
moment: the same generator in means the regenerated schema should come out
byte-identical, so a successful run is a no-op and a failed one is still
diagnostic.

Run 2026-11-05T06:06:28Z. All four live definitions re-read their OAS:

```
ironic-node            8c19ee6b58ec46c6 -> 7921236d3d55d2f7
ironic-node-power      aaa9ae89d35b1202 -> e3c6b4f35c5a9c7b
ironic-node-provision  1f3bec857df432e8 -> 8976e271d3b3d62e
ironic-port            7a61f5fa80e184c4 -> 48f47cc0996df35d
```

**Collateral: I reported "none" and that was wrong.** What I checked was
byte-identical CRD schemas, unchanged CR counts (6 BaremetalNode, 6 NodePower,
0 NodeProvision, 12 Port) and all six blades still `active`. All true, and all
of it missed what actually happened.

Re-rendering the CRDs also updates the generated **rest-dynamic-controller
Deployments** — oasgen compares the rendered digest and applies — so **all four
rdc controllers restarted at 06:08:21Z**, the same instant the `SchemaWarnings`
events fired. Those are not two events; they are one re-render seen through two
channels. The restart drove a re-reconcile, cdc upgraded blade06's
baremetal-host release three times in ten minutes (revisions 27, 28, 29 at
06:15, 06:23, 06:25), and at 06:28:55 its NodePower CR failed to create and
wedged:

```
krateo.io/external-create-failed  = 2026-11-05T06:28:55Z
krateo.io/external-create-pending = 2026-11-05T06:29:15Z

cannot determine creation result - remove the krateo.io/external-create-pending
annotation if it is safe to proceed: existence check: findby listed successfully
but no item matched the resource's identifiers, which is not proof of absence:
unexpected status: 404: item not found
```

preceded at 06:29:37 by `Cannot update managed resource: the object has been
modified` — an optimistic-concurrency loss. It has retried every ~3 minutes
since. The other five blades' NodePower CRs sit at `Ready=False/Pending,
Synced=True`, so this one is genuinely different.

**The regeneration caused it.** Not a pre-existing specimen I happened to find.

What I got wrong is narrower than "I missed a side effect": I chose three
verification axes — schema bytes, CR counts, blade power state — and all three
are *object existence and content* checks. None of them looks at whether a
controller is **healthy**, and the CR count is the metric most likely to stay
flat while a CR is broken, because a wedged CR is still a CR. I verified the
nouns and never the verbs.

The consequence for the step after this one: **upgrading oasgen will re-render
again and restart all four rdc controllers again**, with the same exposure. That
is not a reason to skip it, but it is a known cost rather than a surprise, and
the pre-state is captured in `evidence/` first.

Corrected axes for any future regeneration: CR conditions per CR, controller pod
restart counts and ages, and helm release revisions — alongside the three above.

**And the result that makes the measurement worth taking.** After a genuine
regeneration, `node_id` is **still ABSENT** from both `nodepowers` and
`nodeprovisions`. That is correct — 0.50.0 does not contain #421 — and it is the
part that matters: the readout did *not* move when only the nudge happened. So
when it flips to PRESENT it will be attributable to the generator and to nothing
else. A measurement that could have been moved by the nudge alone would not have
distinguished "#421 works" from "regeneration happened".

That is the positive control inverted: usually you plant the defect and check the
screen fires. Here the screen must **not** fire on the intervention it is paired
with, or it cannot attribute what it sees.

`make regenerate-crds` performs it; `make regenerate-crds-check` reads the stored
side and changes nothing. The script refuses to start if any definition's OAS
file cannot be resolved, because regenerating some and leaving others on the old
generator is worse than not starting.

### §9.13 — the #456 arm is VACUOUS on this fleet, and why that is the finding

oasgen asked for the #456 over-eagerness arm against real documents: `uuid` must
stay absent from `properties` and `required`, because the narrowing must not
*change* a case the pre-existing mapping exemption already handled. They pointed
at `oasgen-render`, which generates CRDs with the same `render.CRDs` the
controller calls, so all four `uuid`-excluding definitions could be rendered
under both versions with no cluster touched.

Ran it. **0 of 4 kinds differ** between 0.50.0 and `main@57573a5`:

```
Allocation@v1-0-0      uuid in props 0.50.0=False main=False  required False/False  IDENTICAL
DeployTemplate@v1-0-0  uuid in props 0.50.0=False main=False  required False/False  IDENTICAL
Port@v1-0-0            uuid in props 0.50.0=False main=False  required False/False  IDENTICAL
PortGroup@v1-0-0       uuid in props 0.50.0=False main=False  required False/False  IDENTICAL
```

**That is not evidence the narrowing is safe. It is evidence these definitions
cannot test it.** The render output carried eight errors, identical on both
versions, and they are the actual result:

```
generation error: field 'uuid' set in excludedSpecFields not found in schema
generation error: field 'X-Ironic-Endpoint' set in configurationFields not found in schema
```

Every one of the four OAS documents has two schemas — a full one and a create
one — and oasgen builds the spec from the **create** body:

```
ironic-allocation.yaml       Allocation:uuid=True      AllocationCreate:uuid=False
ironic-deploy-template.yaml  DeployTemplate:uuid=True   DeployTemplateCreate:uuid=False
ironic-portgroup.yaml        Portgroup:uuid=True        PortgroupCreate:uuid=False
ironic-port.yaml             Port:uuid=True             PortCreate:uuid=False
```

So `excludedSpecFields: [uuid]` excludes a field that was **never in the spec**.
All four exclusions are no-ops, all four `configurationFields: [X-Ironic-Endpoint]`
likewise. `uuid` is absent because nothing put it there — not because an
exclusion removed it and not because a mapping exempted it. An arm asking "does
the narrowing expose `uuid`" cannot discriminate when there is no `uuid` to
expose.

**This is the fourth explanation I have given for the same observation.** The
first three — the guard refuses it, the mapping exempts it, the exemption is an
accident of a mapping on a different verb — all fit the data and all were wrong
about the mechanism. The operative fact is one line of OAS. Each previous
explanation was more sophisticated than the last, which is the warning sign I
keep not reading: when the explanation gets more intricate and the observation
does not change, the explanation is probably decoration on an unexamined
assumption. See [[reference_calibrate-before-trusting-a-zero]].

**And the errors ship silently.** `ironic-port` is live, and its RestDefinition
reports:

```
Ready=True   reason=Available
Synced=True  reason=ReconcileSuccess
```

with no mention of either generation error.

**Correction: they ARE reported, and the true version is worse.** oasgen emits
them as a Kubernetes Warning Event (`reason=SchemaWarnings`,
`restdefinition.go:1031`) and nowhere else. No condition carries them, so the
status reading above is right -- but "nothing in the cluster can be asked about
them" was wrong. I checked conditions and never checked events.

The defect is a channel-lifetime mismatch, not silence. The facts are permanent;
`--event-ttl` defaults to one hour (confirmed unset on this apiserver), and the
emitting path runs only on create or on an OAS/spec change. So for `ironic-port`
they fired once at install and were collected an hour later.

Confirmed by accident: the §9.12 regeneration re-emitted all three at
06:08:21Z, which is the only reason a definition I had called silent had events
when I finally looked. `ironic-node` has none, consistent with it being the one
installed definition declaring no `excludedSpecFields`.

And the events carry **more** than my render diff found. `ironic-node-power` and
`ironic-node-provision` each hold two warnings I had never seen, because my
render covered only the four `uuid`-excluding definitions:

```
generation error at identifiers.node_id: status field 'node_id' not found in
    response, defaulting to string
validation error: could not extract schema for action 'create' to compare:
    no suitable response
```

The first is the same `node_id` that `check-mapping-sources` found statically,
approached from the other end: my screen reasoned from the documents that
`spec.node_id` is mapping-suppressed, while the generator observed that
`node_id` is not in the response either. **That warning had been sitting in a
one-hour event since the day the definition was installed, while I derived half
of it from first principles over two days.** Had it been a condition, that screen
would not have needed to exist. Captured in `evidence/` before expiry.

`scripts/render-diff-oasgen.py` performs the comparison. The eight errors are
real and unfixed: removing the dead `excludedSpecFields` and `configurationFields`
entries is correct but changes generated output, so it is a separate change.

### §9.14 — the blade06 NodePower wedge, measured end to end

My regeneration (§9.12) restarted the four rdc controllers, which re-entered the
create path for `lab-ha-worker-blade06-vzx5x9vd-power` and wedged it. The chain
is fully measured, and every step of it is a known defect behaving correctly.

**1. `spec.node_id` is absent from all six NodePower CRs.** The chart renders it
(`transition-power.yaml:44`), but the generated CRD has no such property, so the
apiserver **prunes it on write**. That is oasgen #419's real operational cost in
this fleet: not a cosmetic schema gap, a silently dropped field.

**2. rdc refused to send the request — 54 times, 06:14:26 → 06:29:32:**

```
refusing to request /v1/nodes/{node_id}/states/power: unresolved path
parameter(s) [node_id] -- the value is absent, so this request would address
the literal template rather than a resource
```

The 0.48+ path-param guard working exactly as designed. **No request reached
Ironic, so the blade was never touched.**

**3. The existence check then could not prove absence — 12 times, 06:24:39 →
06:56:47:**

```
existence check: findby listed successfully but no item matched the resource's
identifiers, which is not proof of absence: unexpected status: 404: item not found
```

rdc refusing to decide, on purpose, because deciding wrongly duplicates a live
resource.

**4. Ironic, asked directly:**

```
blade06  uuid=2f05176e-...  provision_state=active  power_state=power on
         target_power_state=None  last_error=None
```

**The resource reported as `404: item not found` exists and is in the desired
state.** The 404 is not Ironic's answer about blade06 — no request was made to
its power endpoint at all — and it is wrong about the world.

That is the dangerous part, and it is a message defect rather than a logic one.
The prose says *"not proof of absence"*; the most legible token in the same
string says *"404: item not found"*. The message gates a hard-to-reverse manual
intervention — clearing `krateo.io/external-create-pending` — and its most
quotable fragment argues for the opposite of its own prose. An operator who
trusts it duplicates a live resource on physical hardware.

**Why only blade06.** All six CRs carry `external-create-pending`. The five
healthy ones *also* carry `external-create-succeeded` from an earlier create;
blade06 carries `external-create-failed` instead. So the differentiator is not
the pending annotation — it is whether a create was ever confirmed. The pruned
`node_id` is universal; needing the create path again is what blade06 had and
the others did not. **This falsified my first hypothesis**, which was that the
pruned identifier caused the wedge directly: five CRs with the identical pruned
spec are fine.

So #419 does not cause the wedge. It makes the wedge **unrecoverable**, which is
worse: the retry path can never succeed, because the value it needs was pruned
before it ever reached the CR.

**Prediction, recorded before the upgrade so it can be wrong.** Restoring
`spec.node_id` via #421 should let the chart's value land, the guard resolve, the
create proceed, and the wedge clear — with exactly one PUT of `target: power on`
against a blade already powered on. If it clears, that is attributable to #421,
**not** #442: `findItemInList` propagating a comparison error would change the
message, not unblock a request that was never sent.

Evidence captured in `evidence/` first, because the upgrade destroys the
pre-state: the wedged CR, the 69 rdc log lines, and the three `SchemaWarnings`
events.

### §9.15 — #421 confirmed, the serving window is sub-quarter-second, and my prediction was wrong

Upgraded oasgen to a locally built `main@57573a5` image (Deployment image swap,
`--debug` preserved, rollback `:0.50.0` recorded), then ran `make regenerate-crds`.

**#421 is confirmed.** Stored side, before and after, same nudge:

| CRD | field | 0.50.0 | main@57573a5 |
|---|---|---|---|
| `nodepowers` | `spec.node_id` | ABSENT | **PRESENT, required=True** |
| `nodeprovisions` | `spec.node_id` | ABSENT | **PRESENT, required=True** |
| `ports` | `spec.uuid` | ABSENT | ABSENT (unchanged) |

Attributable, because §9.12 ran the identical nudge against 0.50.0 and `node_id`
stayed ABSENT. The flip is the generator and nothing else. `ports.uuid`
unchanged is the #456 arm — though per §9.13 that arm is vacuous here anyway.

**Serving window (oasgen #446): 0.0s, meaning below resolution.** `SCHEMA` (the
CRD says so) and `HONOURED` (a server-side dry-run apply is accepted) flipped in
the **same sample**, at t+176.28s after the nudge. 786 samples over 176s is
~0.22s per sample, so on this cluster the gap between a generated CRD being
updated and the apiserver honouring the new schema is **under a quarter second**.
The 176s is time-to-regenerate, not the window.

#### The prediction, and why it failed

Recorded in §9.14: #421 restores `spec.node_id`, the guard resolves, the create
proceeds, blade06 clears. **It did not clear.** All six NodePower CRs still read
`node_id=None`, and blade06 still holds `external-create-failed` with
`Synced=False/ReconcileError`.

The schema now accepts the field. The CRs do not carry it:

```
helm get manifest lab-ha-worker-blade06-vzx5x9vd  ->  node_id: "blade06"
live CR spec.node_id                              ->  None
```

**The value was pruned at write time, and fixing the schema does not retroactively
repopulate it.** Helm's stored manifest was always correct; the apiserver dropped
the field on the way in, and nothing has rewritten the CR since. The schema fix
makes a *future* write succeed; it does nothing for a write that already
happened.

This is [[rendered-vs-stored]] for the third time in a day — the renderer is
fixed and the store disagrees — and I walked into it hours after writing that
note. The note says "read the far side before arming a comparison". I read the
far side for the blades' configdrive hash and then failed to ask the same
question of the CR I was predicting about. **Knowing the shape of an error does
not make you check for it; only checking does.**

It also corrects something I would otherwise have let a peer publish: a CR wedged
before #421 does **not** become recoverable merely by upgrading the generator and
re-rendering the kind. The schema becomes correct and the CR stays empty. Recovery
needs something to rewrite the CR.

Which is cdc's job, not mine — the release manifest already holds the right
content, so a re-apply fixes it. A monitor is watching for `node_id` to land
rather than choreographing it by hand, because reconciliation arriving on its own
is the property worth observing; if it never arrives, that absence is the finding
and the next question is why cdc does not re-apply a release whose rendered
manifest differs from the live object.

### §9.16 — the disk incident, and the NodePower non-movement result

#### An incident I caused, and nearly misdiagnosed as a regression

Minutes after putting all four rdc controllers on `main@57573a5`, every one of
the six NodePower CRs went `Synced=ReconcileError` — five of which had been
`ReconcileSuccess`. The obvious reading was a regression in the build I had just
deployed.

It was not. The condition message said:

```
observe failed: building the client for action "dispatch" against
"configmap://openstack/ironic-power-oas/power.yaml": failed to create
directory: mkdir /tmp/rest-dynamic-controller-2102883379:
no space left on device
```

**The node disk was at 100%, 0 bytes free.** `docker system df`:

```
Images         13.92GB   ( 9.46GB reclaimable)
Local Volumes  32.34GB   ( 7.13GB reclaimable)
Build Cache     8.14GB   ( 7.35GB reclaimable, 0 ACTIVE)
```

Two `go build -a` Docker builds — mine, for the oasgen and rdc images — left
8.14GB of build cache with nothing referencing it. `docker builder prune` freed
7.35GB without touching a tagged image or a volume, and the node went to 85%
with 8.9G free. etcd recovered (`readyz check passed`), all six blades stayed
`active/power on` throughout, and the five healthy CRs returned to
`ReconcileSuccess` on their own.

**The near-miss is the lesson.** A fleet-wide error appearing minutes after a
fleet-wide change reads as caused by it, and the reading is cheap, available and
wrong. What saved it was opening the condition *message* rather than acting on
the condition *state* — the state said "everything I just touched is broken", the
message said "the disk is full". A symptom's timing is not evidence about its
cause, and the more plausible the coincidence, the less it is examined.

Also worth recording: the thing I filled was shared. The kind node's own tree is
only ~7.5G; the 58G was the Docker VM disk, which every kind cluster and
container on this machine shares. A build in one workspace can take out a
cluster in another.

#### The NodePower non-movement check (#442's measurement)

With the disk healthy and rdc on `main@57573a5`, blade06's existence-check
message was **unchanged**, word for word. That does not falsify #442: the create
path and the existence-check path are different code, and #442 touches the
matcher.

Then, with the user's explicit approval and on measured grounds — Ironic
reporting `active / power on / target_power_state=None / last_error=None`, and
the CR's `target` already `power on`, so a retried create is idempotent —
`krateo.io/external-create-pending` was cleared.

**Result, within 20 seconds:**

```
external-create-pending   = 2026-11-05T07:54:53Z   (re-set by the retry)
external-create-succeeded = 2026-11-05T07:54:55Z   (2s later)
Ready  = False / Pending          at 07:54:54Z
Synced = True  / ReconcileSuccess at 07:55:25Z
```

And the hardware, against blade01 as control:

```
blade01  provision=active  power=power on  target=None  last_error=None
blade06  provision=active  power=power on  target=None  last_error=None
```

**No movement.** No power transition, no target power state, no error, on either.
blade06 now matches the other five exactly, including the `Ready=False/Pending`
steady state all six share.

**Attribution, stated narrowly.** The wedge cleared because the annotation was
cleared. That was only *safe to clear* because #421 had restored `spec.node_id`
and cdc had rewritten the CRs — without it the retried create would have hit the
path-param guard and re-failed, re-wedging immediately. So #421 made the retry
able to succeed; the manual clear was still required, because the annotation gate
is deliberate. **#442's contribution is not observable in this result** and I am
not claiming it: the message it governs did not change, and the create that
succeeded is not the code path it touches.

### §9.17 — core-provider #303/#329 adoption probe on 2.13.62: PASS, plus two unreachable preconditions

Run for core-provider and openstack-as-a-service, who both had this gap open.
Throwaway `release-adopt-probe` 0.1.2 — one ConfigMap, nothing cluster-scoped —
on core-provider **2.13.62**, against the live `ironic-kog` cluster. cdc runs one
Deployment per CompositionDefinition version, so the probe could not disturb the
four live compositions, and it did not.

#### The headline: adoption works and completes

| | |
|---|---|
| (a) releases before | `adopt-probe-5v6764b8` rev **1**, deployed |
| (a) releases after | `adopt-probe-5v6764b8` rev **2**, deployed — **one release throughout** |
| (b) adopted revision | **2**, description **"Upgrade complete"** (not "Install complete") |
| (c) reconcile completed | **yes** — `Ready=True/Available`, `Synced=True/ReconcileSuccess` |

Timing: spec applied 08:58:39Z, helm upgrade recorded 08:58:41Z, CR fully
reconciled by 08:58:54Z. The label was re-stamped to the **existing** release
name rather than a new one, and the ConfigMap content updated to the new marker,
so the release was genuinely adopted and upgraded — not reinstalled, and not
duplicated. That is materially different from 2.13.59, where a recreated
composition CR wedged on a chart-inspector 500.

#### Two preconditions that make the stated repro unreachable

Core-provider's recipe was *"a recreated CR with no `krateo.io/release-name`
label"*. **Recreation does not produce that state**, for two measured reasons:

**1. Deleting the CR uninstalls the release cleanly.** The finalizer
`composition.krateo.io/finalizer` fires, cdc uninstalls, and within 10s there is
no release left to adopt.

**2. The release name is not deterministic.** Baseline run produced
`adopt-probe-5dw9xx5f`; deleting and recreating the identical CR produced
`adopt-probe-5v6764b8`. A random suffix, so even an orphaned release could not
be matched by a recreated CR — it would compute a different name.

So the reachable entry is a **stripped label on a live CR**, not a recreate. And
there is a third precondition:

**3. Removing the label does not trigger a reconcile.** Four minutes after
stripping it, `generation` was still 1 and nothing had happened — a label change
does not bump generation. Adoption was only entered after a subsequent **spec**
change, which is what the probe chart's `marker` field exists to supply.

An e2e that deletes and recreates a CR, or that strips the label and waits, will
observe nothing and may read that as a pass. The sequence that actually exercises
adoption is: **strip the label, then change the spec.**

Teardown was finalizer-safe — CR first so cdc could still uninstall, then the
CompositionDefinition. The generated CRD deleted cleanly with no `Terminating`
hang, which is the hazard in [[krateo-cd-stuck-crd-finalizer]].

### §9.18 — 0.13.26 promoted, verified on the axes §9.12 got wrong

CompositionDefinition 0.13.25 → 0.13.26, applied 11:08:42Z, cdc at revision 82
within 15s, `v0-13-26` served and the old version already pruned.

Pre-flight was a server-side dry run: 6 BaremetalHost + 1 Secret, **no
`NodeProvision`**, and exactly 12 changed spec fields — the hash and the userData
line carrying it, on each of six. Per node class, matching the §9.9 calibration
exactly.

**Verified on the corrected axes, not the ones that produced a false clean:**

| axis | before | after |
|---|---|---|
| per-CR conditions | 6× `ReconcileSuccess` | 6× `ReconcileSuccess` |
| controller restarts | all 0 | all 0 |
| helm revisions (children) | 62/71/75/52/62/31 | 63/72/76/53/63/32 (+1 each) |
| blades | 6× `active/power on` | 6× `active/power on` |
| `NodeProvision` CRs | none | none |

The child revisions incrementing is the expected consequence — cdc re-applied
each baremetal-host release with the new hash. **Controller restarts staying at
zero is the distinction worth naming:** §9.12's restarts came from an *oasgen*
re-render re-applying the generated rdc Deployments, which a composition chart
promotion does not do. Two superficially similar operations, different blast
radii, and the difference is only visible on an axis I added after being wrong.

**The feature is live and the interlock is holding.** The live BaremetalHost CRs
now carry real content hashes:

```
lab-ha-cp-blade01      dd8c9c77b71f7299    (bootstrap CP)
lab-ha-cp-blade02/03   ae655ff2f0d7b4ac    (CP replicas)
lab-ha-worker-04/05/06 e62df77bd404edbc    (workers)
```

Three distinct values for three node classes, as designed. And
`deployed_configdrive_hash` is on **0 of 6** BaremetalNode CRs, so nothing can
auto-undeploy: the bridge still refuses to mirror it, the guard still asserts
that refusal, and `make check-examples` passes.

So drift detection now has an honest input and remains deliberately disarmed.
The remaining step is unchanged and is not a code change: every blade must
republish under 0.13.26+ before the key can be mirrored, which happens on each
blade's next deploy. [[rendered-vs-stored]].

### §9.19 — `observedGeneration` is absent on every KOG-generated resource

Noticed while trying to tell "reconciled, this is the steady state" from "not
looked at yet", and initially reported with the wrong controller attached. The
split is by **generator**:

| | declares it | populated |
|---|---|---|
| `kubernetesclusters` (core-provider) | yes | 1/1 |
| `baremetalhosts` (core-provider) | yes | 6/6 |
| `baremetalnodes` (oasgen) | **no** | 0/6 |
| `nodepowers` (oasgen) | **no** | 0/6 |
| `ports` (oasgen) | **no** | 0/12 |

**Nothing removes it — it is never declared.** The oasgen-generated status schema
carries `conditions` plus the OAS response fields (`nodepowers`:
`conditions, node_id, power_state, target_power_state, uuid`) and no synthetic
ones. core-provider had just filed a lost-update mechanism where a stale
concurrent status write wipes fields, which would have been a good explanation for
a field a stale write never carried; it is impossible here.

**Why it matters operationally.** `observedGeneration` is the standard answer to
"has the controller caught up with my spec change?", and on KOG resources there
is none — so there is no generic way to separate *reconciled-and-steady* from
*not-yet-seen*. All six NodePower CRs sit permanently at `Ready=False/Pending`
with `Synced=True/ReconcileSuccess`, so the conditions do not answer it either. It
compounds with a second gap: a **label** change does not bump `generation`, so a
consumer watching for convergence after a metadata-only edit has neither a
generation delta nor an `observedGeneration`. That pair is exactly why §9.17's
strip-then-wait observed nothing for four minutes and could not distinguish
"nothing happened" from "happened, changed nothing".

**The reporting error is the part to keep.** I said "absent on all six" having
looked only at NodePower, then answered a peer's "which cdc version?" without
noticing those resources are not cdc-managed at all. They went looking in their
own code for a fact that was never about them. What caught it was their asking
*which resources* — the question I should have answered before offering the
observation. **A measurement with the wrong subject attached is worse than no
measurement, because it is actionable.** Same family as
[[reference_setup-cannot-produce-the-state]]: the finding was real and the frame
around it was not.

**Addendum — the write path, traced by oasgen (filed as #466).** My reading
established that nothing *removes* the field. They asked what *writes* it, since
a writer plus an undeclared schema would mean the apiserver prunes it — the
silent-loss mechanism from §9.14. It is unwired in **three** independent places:

1. **Nothing writes it.** `unstructured-runtime` v1.7.4 ships
   `statusprojection.SetObservedGeneration` with **zero call sites** — not in
   rest-dynamic-controller, not in the reconciler that ships it.
2. **Nothing declares it.** The status builder mirrors the OAS response plus
   `conditions`, no synthetic fields (what §9.19 measured).
3. **Something already protects it.** `observe_restaction.go:177` skips
   `conditions` and `observedGeneration` when composing a delegated observe
   result — *"Never let the composed result clobber runtime-managed status
   fields."* A guard on a field nothing manages, which the schema would prune
   anyway.

A writer waiting for a schema, a schema mirroring the response, and a guard
protecting the writer's output. Each part is defensible alone; together they do
nothing. [[reference_what-ships-that-nothing-exercises]] three times over in one
feature.

**The constraint for anyone fixing it: both halves are required and neither
works alone.** Declaring the field alone adds a permanently empty one; calling
the setter alone gets pruned by the schema. **Shipping either half reproduces
today's symptom while looking fixed** — which is the failure mode most likely to
close the issue.

### §9.20 — core-provider 2.13.65: both adoption variants pass, with the grace measured

Upgraded `core-provider` + `chart-inspector` 2.13.62 → 2.13.65 after the image
became pullable. Verified on the four axes: compositions 4× `ReconcileSuccess`,
per-CR conditions 6× `ReconcileSuccess`, controller restarts 0, helm revisions
unchanged (63/72/76/82/53/63/32), six blades `active/power on`.

The per-version cdc pods were **replaced** (new names, 0 restarts, ready).

**The explanation this section gave for that is FALSE, retracted 2026-11-09.**
It said the replacement was 2.13.65 adding a `readinessProbe` on `/readyz` to
each generated cdc Deployment. Measured against every ReplicaSet in
`krateo-system`:

```
31 ReplicaSets for the per-version controllers, created 2026-11-04 .. 2026-11-07
readinessProbe present: 0 of 31
of which rendered AFTER the 2.13.65 upgrade (12:03Z on 11-05): 11, all without
the 5 live Deployments: ready=1/1, no probes of any kind
```

Two of those Deployments (`baremetalhosts-v0-6-3`, `kubernetesclusters-v0-14-0`)
were created fresh on 2026-11-07, i.e. rendered BY core-provider 2.13.65, and
they carry no probe either. So the claim is not merely stale — 2.13.65 does not
render a probe here at all, and the pod replacement at upgrade had some other
cause that was never established.

The claim was invented to explain an observation (new pod names with 0
restarts), it sounded mechanistic, and nothing checked it. Worse, it was passed
to the core-provider session as fact on 2026-11-09, which reasoned from it and
predicted the 2.13.62 controllers would show `0/1 READY` failing a probe they
could not answer. They asked for the measurement rather than taking it, which is
the only reason it was caught. See
[[reference_escalating-mechanisms-read-as-progress]] and
[[reference_a-correction-can-be-the-error]]: the restart-count axis reading zero
with new pod names is still the observation, and it still has no explanation.

#### Probe 1 — recreated CR, empty status (the 2.13.62 defect)

The case my earlier route structurally could not reach: a recreated CR has no
`status.digest`, and `plumbing`'s `maps.NestedString` reported the missing key as
an error, so Observe failed every pass.

```
t+0s   label=None  digest=NO   (the defective precondition, captured)
t+10s  label=adopt-probe-cwv9c7v6  digest=yes  Ready=Available  Synced=ReconcileSuccess
       release rev1 -> rev2 "Upgrade complete"
```

**Completed in 10s.** The orphan was adopted and upgraded, not reinstalled, and
one release existed throughout. The `digest=NO` sample matters: it shows the
precondition was genuinely present rather than assumed.

#### Probe 2 — `pending-install` orphan, and a void first attempt

**My first attempt was void and I nearly reported it as a pass.** I constructed
`pending-install` by setting the Secret's `status` **label**, then ran the probe
and saw it complete in 15s. Two faults: helm reads `info.status` from the release
**data** (base64 of base64 of gzipped JSON), so the label is only a query index;
and I had labelled revision `.v2` while an adoption had already created `.v3`. So
the probe tested a *deployed* orphan twice. That is
[[reference_setup-cannot-produce-the-state]] — a fourth instance, committed hours
after writing the note, in a script whose own header warns about constructed
state.

`scripts/helm-set-release-status.py` does it properly — patches the release data
**and** the label, on the current revision, and optionally stamps
`info.last_deployed` to now, because the grace is measured from `last_deployed`
and a stale one is already past its grace. **Verified with `helm list` before
proceeding**, which is the step the first attempt skipped.

Against a real `pending-install` orphan:

```
t+0s     label=None                        release rev3 pending-install
t+15s    Ready=PendingHelmOperation        "pending for 26s"
t+61s    Ready=PendingHelmOperation        "pending for 1m13s"
t+123s   Ready=PendingHelmOperation        "pending for 2m13s"
t+170s   Ready=PendingHelmOperation        "pending for 3m13s"
t+234s   Ready=PendingHelmOperation        "pending for 4m14s"
t+297s   label=adopt-probe-cwv9c7v6  Ready=Available  rev5 deployed
```

```
rev3  12:07:51  pending-install  Upgrade complete   <- constructed
rev4  12:13:05  superseded       Rollback to 3      <- grace expired, rollback
rev5  12:13:06  deployed         Upgrade complete   <- then the upgrade
```

**Adopt → helm lock → grace → rollback → upgrade, exactly as predicted.**
`Synced` stayed `True/ReconcileSuccess` throughout, so `PendingHelmOperation`
on `Ready` is the only waiting signal — and it carries `pendingFor`, so it is
self-describing.

**Grace, measured:** `last_deployed` 12:07:51 → rollback 12:13:05 = **5m14s**,
against a derived 5m default plus reconcile timing. Total to completion
**4m57s**, inside the ~7m expected ceiling and well inside the 10m threshold at
which core-provider says to call it wedged.

So the derived figures hold against a live cluster with external state behind the
compositions. The six blades were untouched throughout both probes.

### §9.21 — the plumbing v1.26.x defaulting predicate: zero, and a nearly vacuous zero

oasgen is holding a `plumbing` v1.14.2 → v1.26.x bump and asked both available
fleets to screen for its predicate: an **optional** object property (not in its
parent's `required`) with a **reachable defaulted descendant** and **no required
properties of its own**. At v1.26.1 such a parent gains `default: {}`; the
apiserver applies defaults on *read* from etcd, and rdc sends top-level spec
values whole — so after a re-render an existing CR could put a stanza on the wire
that nobody authored, separated from the upgrade by the #453 regeneration gap.

`scripts/check-defaulted-optional-objects.py`, 6-case control (3 must trip, 3
must not), refuses to scan if the control fails. Result: **0 hits across all 8
OAS documents.**

**Then the question that mattered more: is the zero informative?**

| document | defaults | nested objects | optional objects |
|---|---|---|---|
| all 8 combined | **4** | **26** | **26** |

The optional-object half is satisfied **26 times over** — every nested object in
this fleet is optional, so the zero is not "our objects are required". And all
four `default:` occurrences turn out to be **the same thing**:

```
components.parameters.X-OpenStack-Ironic-API-Version.schema.default = '1.109'
```

A **header parameter** default, in four documents. **There is not one default in
any request-body schema anywhere in this fleet.**

**So the zero is real for us and nearly worthless as evidence.** The predicate
needs two ingredients and one of them is entirely absent from our documents — we
could not trip it whatever our object nesting looked like. We are clear by a
general property rather than by an accident of shape, which is stronger than the
other fleet's reason, but it also means **neither available fleet is a witness**:
theirs is clear because its nested defaults sit under a required parent, ours
because it has no body defaults at all.

That is worth more to the person holding the bump than a clean report would be:
it says the change is **untested in the field**, not screened safe.

Secondary checks, both 0 across all 8 documents: `maxProperties` (the bound
silently dropped at v1.14.2 and kept at v1.26.1) and `type: [integer, string]`
nodes (which become `x-kubernetes-int-or-string`). Same as the other fleet.

**Addendum — a real witness was found, and the caveat on my own screen was the
useful part.** I had reported that my walker does not follow
`allOf`/`anyOf`/`oneOf`. oasgen checked theirs against that and found it walked
only `properties`, `items` and `additionalProperties` — so it reported **0 on the
only shape anyone has found in the field**. Their two controls had both passed,
and both used inline `properties`, because both came from their own model of the
shape. *A control drawn from your own model tests the implementation against the
model, not against the world.*

The field instance, from the aruba fleet — **2 of 35** — is
`allOf: [{$ref: .../PropertiesDto}]` with `nullable: true`, and the defaulted
payload is a property literally named `default`, documented *"Only one default vpc
for region is admissible."* So the injection would make every Vpc CR whose author
omitted `properties` assert a region-unique role.

**So the bump is no longer untested — the field evidence exists and is positive**,
which supersedes my "neither fleet is a witness" conclusion. It was right when
written and is not now.

I closed the same hole here: the screen resolves local `$ref` with cycle
detection and merges composition members for **both** `properties` and
`required`. Three new controls are drawn from the field shape rather than my
model, and **one of them immediately caught a bug in my own fix** — `own_req`
was still reading the node's own `required`, so a parent whose `required` arrives
through `allOf` would have tripped falsely. 6 controls → 9, all passing, and the
0 survives the stricter walker.

**And the risk is not reversible, which is why this is now a standing check
rather than a reading.** aruba measured that once a default is materialised, one
unrelated write — an annotation, a label, any controller touch — persists it to
etcd; **pinning the CRD back does not remove it**, and the field is unowned in
`managedFields`, so `kubectl apply` will not prune it. An object created fresh at
the same moment comes back clean while one that lived through the window keeps
the value.

For this fleet that means the exposure would arrive with a **new Ironic OAS
document**, at the next re-render, and could not be undone by pinning. A one-off
reading cannot catch that; `make check-oas-defaults` can, and `make check` now
runs all three standing screens together.

### §9.22 — two Marketplace blueprints, and the defaulting predicate found on our own side

The ask was a proper blueprint for Kubernetes and for bare metal, fit for the
Krateo Marketplace. `charts/kubernetes-cluster` → **0.14.0**,
`charts/baremetal-host` → **0.6.3**, published to the `blueprints` channel by
`release-chart.yaml` on the next tag. `docs/MARKETPLACE.md` is the runbook.

#### The gate was read, not guessed

`krateo-blueprints/charts/.github/workflows/publish-chart.yaml` is the authority,
so it was read rather than inferred. Three hard requirements, all on the
**packaged** `.tgz`: `values.schema.json`, `compositiondefinition.yaml`, and an
`icon` beginning `https://`. Both charts were packaged and the archives listed to
confirm the two files are in them — a chart-root file is packaged, but
`.helmignore` is invisible to a source-tree check, so the archive is what the
screen reads too.

The gate's own comments were worth the read for a second reason: they record that
`tar tzf … | grep -q` made a PRESENT file fail the test, because `grep -q` exits
on its first match, `tar` dies of SIGPIPE, and under `pipefail` the pipeline
status becomes 141. It bit only the largest charts, in a run where 196 smaller
ones passed. `check-blueprint-metadata.py` reads the listing whole for the same
reason.

#### What the gate passes, and the Marketplace then shows

Six failures live on the far side of a green gate. The one that is not guessable:

**`krateo.io/source-repo` decides whether a card appears at all.** The portal's
live-index selector is literally
`(.value[0].annotations["krateo.io/source-repo"] // "") != ""`. I first wrote
this up as "without it the blueprint publishes and is never shown", citing our
own `baremetal-lifecycle` 0.3.1 as the worked example — **and that was wrong.**
`krateo-blueprints/marketplace`'s `hack/build-index.py` builds the curated
ConfigMap *from this same live index*, removing only 18 named platform charts. So
the annotation buys **immediacy, not existence**: without it the card waits for a
`krateo-marketplace-catalog` release that is not ours to cut. Corrected in five
places before the commit.

Checked against the real index: **no entry carries that annotation yet** —
`baremetal-lifecycle` included, and the `aws-*-stack` blueprints too. portal#292's
own validation table says it in its third row. These two blueprints would be the
first to use that route, which is a caveat worth carrying rather than hiding.

Also measured, because the icon is the one field whose failure is purely visual:
the gate tests only the *scheme*, so a `github.com/.../blob/...` URL passes it and
renders a blank card — that URL serves an HTML page. Ours is the Akmon mark
pinned to a commit SHA, verified HTTP 200 `image/svg+xml`.

#### The form is the only interface a Marketplace user has

Between them the two schemas had **119 property paths and not one `title`**, and
most had no `description` either — a form of bare keys like `driver_info` and
`undeployMode` with no help text. plumbing's `crdgen` carries both into the CRD
(`transpile.go`: Description at :243, Title at :348), so both were authored, 119
of them, through an injector that asserts the structural schema is unchanged once
`title`/`description` are stripped from both sides. A typo in the mapping could
otherwise have moved a type, a default or a `required` — and the chart version is
the served apiVersion, so that is not a cosmetic risk.

`title` is propagated by the generator but is **unverified on this cluster**: no
CRD here was generated from a schema that had one. So every description was
written to stand on its own, and the screen requires both rather than either.

#### Two inputs the templates read and no Composition could set

Found by reading the served CRDs rather than the chart:

- **`conformance` was absent from the served `KubernetesCluster` CRD entirely.**
  The templates read `.Values.conformance.install`; the schema never declared it.
  So the conformance runner was reachable by `helm install` and unreachable
  through a Composition — which is the only way a Marketplace user installs
  anything.
- **`cni.kubeProxyReplacement` was silently PRUNED.** `cni` declares properties,
  so it is a structural schema with no `x-kubernetes-preserve-unknown-fields`, and
  the API server drops an undeclared key without complaint. The chart default
  (`true`) therefore always won, and the knob that `values.yaml` documents as
  *required for conformance* could not be turned off.

Both are now in the contract. Neither got a schema `default`, for the reason
below.

#### `ironicApiUrl` / `ironicApiVersion` are gone

`values.yaml` had scheduled this since 2026-11-04 — *"Delete both at the next
parent chart bump, manifests included"* — and this is that bump. Re-verified
first: zero reads in either chart's templates. Removed from the schema,
`values.yaml`, four manifests and two docs tables.

`ironicApiVersion` is the one that had to go rather than being left alone. It
reads exactly like the knob that sets Ironic's microversion, it was pinned to
`1.81` — below the `1.83` floor the node look-up needs — and it was inert, so
anyone "fixing" the apparent disconnect by wiring it through would reintroduce a
`406` on every transition. In `values.yaml` that is a trap for a maintainer. In a
Marketplace form it is a labelled, editable field that does nothing.

#### The aruba predicate, on our own side of the house

§9.21 screened the **OAS** documents for the plumbing defaulting shape and found
zero. `values.schema.json` was never screened — and it has the shape.

Measured 2026-11-05 on the live cluster. `debug` is an optional object whose child
`connectorVerbose` carried `default: false`, so `crdgen` gave the parent
`default: {}`, and **all six live `BaremetalHost` resources carried a
`spec.debug: {connectorVerbose: false}` that no manifest ever wrote.** Exactly the
irreversible materialisation aruba measured at 2 of 35, found here by looking at
the one document class the earlier screen did not cover.

The harm is small — the value it asserts is the one the chart would have used —
but the mechanism is not: one unrelated write persists it, pinning the CRD back
does not remove it, and `kubectl apply` will not prune it because the field is
unowned in `managedFields`. The default now lives only in `values.yaml`, where
Helm applies it at render time and nothing is persisted. **The six existing hosts
keep the stanza, and nothing can remove it.** That is the finding, not a
side-note.

A top-level default is deliberately still allowed: it pre-fills the form and
invents no stanza. The screen has three arms that must NOT fire, so it
discriminates the shape with the consequence rather than the word "default".

#### The screen, and the silence that hid my own defect

`make check-blueprints` — 9 checks, 13 self-test cases, 3 of which must not fire.
`make check` now runs four standing screens.

While wiring it up, `check-examples` was extended to read ```yaml fences in
`charts/*/README.md` as well as `docs/`, because a blueprint's chart README is the
first thing a new user copies and its examples pin an apiVersion. The file count
went 19 → 20 for **two** new READMEs, which is how I found that the
`kubernetes-cluster` example used `…` elisions, parsed nowhere, and was therefore
**skipped in silence** — unchecked, uncopyable, and the run still said OK. The
example is now a complete manifest that parses, and the collector **counts** the
fences it could not parse, with a self-test arm proving the count fires. 30 fences
today: 7 screened, 23 carry no `kind`, 0 unparseable.

A screen that skips what it cannot read, without saying so, is the failure mode
this suite exists to catch — one level up, again.

#### §9.22 addendum — oasgen's one authored default runs on every document we have

The generalisation above ("any document that crdgen or oasgen turns into a CRD")
came back with a result, on an axis I had not considered: not a file class, but a
**scheme type** inside files already in scope.

oasgen authors exactly one schema default of its own —
`configuration_builder.go:258`, the `header` property of the **apiKey**
authentication shape, defaulted from the scheme's `ParamName`. In oasgen's repo
that branch is unrendered: 12 documents declare `bearer`, **zero** declare
`apiKey`. Here it is the only branch there is. Keystone's `X-Auth-Token` *is* an
apiKey-in-header scheme, and all seven Ironic documents declare it.

Measured on the served Configuration CRDs:

```
authentication.apiKey.header   default: "X-Auth-Token"   present on 4 of 5
default: {} stamped on authentication or apiKey           0
apiKey.required                                           ['header', 'tokenRef']
```

**No stanza is invented**, and for the documented reason: the parent declares
required properties, so plumbing #45's clause suppresses `default: {}`. The
irreversible hazard does not arise here.

Two things the measurement adds that the structural argument does not:

- **The default is reachable, and it does real work.** CRD defaulting runs
  *before* validation, so omitting the required `header` yields `X-Auth-Token`
  rather than a rejection — it turns a required field into an effectively
  optional one.
- **On this fleet it never fires.** `manifests/nodeconfiguration-ironic.yaml`
  supplies the value explicitly, so the live CR's `header` is **authored, not
  defaulted**. "No materialisation observed" is therefore a property of our
  manifest, not of oasgen's code — a distinction that matters if the manifest
  ever stops setting it.

**A loose end, recorded as one rather than dressed up.** `nodeconfigurations`
carries no such default while the other four do. I proposed "generated before the
feature shipped and never regenerated under #453" and **falsified it
immediately**: `portconfigurations` is the oldest of the five (19:09 against
19:23) and does carry it. The odd one out is an orphaned kind from the `Node` →
`BaremetalNode` rename with zero live resources, so the cause is most likely the
document it was generated from at the time. Unused, so not a hazard — but it is a
case of one document class producing two different auth shapes, and I do not know
why.

The transferable part is about **scope, not mechanism**: my screen's blind spot
was a file class, oasgen's was a scheme type, and neither of us could see our own
from inside our own code.

#### §9.22 addendum 2 — the `nodeconfigurations` loose end, closed; and a falsification that was itself unsound

oasgen proposed a falsifiable hypothesis for the odd kind out: its document
declared **more than one** apiKey scheme at generation time, which suppresses the
authored default while leaving the shape intact. Cheap to kill, and it is dead —
the document declared **zero**, not several:

```
restdefinition-node.yaml @129e84a    resource.kind: Node          -> NodeConfiguration
restdefinition-node.yaml @HEAD       resource.kind: BaremetalNode -> BaremetalNodeConfiguration

oas/ironic-node.yaml  f09f65a 2026-02-16   apiKey schemes: 0
                      a24a833 2026-05-29   apiKey schemes: 0
                      7501fe0 2026-06-10   apiKey schemes: 0   <- in effect at generation
                      fe9fdfb 2026-10-31   apiKey schemes: 1   <- X-Auth-Token added here
```

All five Configuration CRDs were created on **2026-10-29**. The document gained
its single apiKey scheme on **2026-10-31**, two days later. Four of the five
nevertheless carry `authentication.apiKey.header: "X-Auth-Token"` today — a
default that can only come from a scheme the document did not have when they were
created. **So those four must have had their schemas rewritten after 10-31**, which
is what the regeneration nudges of 11-04 and 11-05 did (an `oasHash` change forces
the re-read; under #453 an upgrade alone does not).

`nodeconfigurations` is the one kind no RestDefinition has pointed at since the
`Node` → `BaremetalNode` rename. Nothing would re-read a document for it, so its
schema still reflects the June document with no security schemes at all. Zero live
resources. Closed, and not a hazard.

**The part worth keeping is about my own falsification.** I proposed exactly this
regeneration explanation in the previous addendum and then rejected it, because
`portconfigurations` is the *oldest* of the five by `creationTimestamp` and does
carry the default. That falsification was unsound — and unsound in the same way
the hypothesis was. **A CRD's `creationTimestamp` says nothing about when its
schema was last written.** Creation time and generation-of-current-content time
are different quantities, and I compared the wrong one, so the test could not have
addressed the claim either way. I abandoned a correct explanation on the strength
of an instrument that was never pointed at the question.

`managedFields` would have been the obvious instrument and it carries no `time` on
these objects, so it gives nothing. What actually dated the schemas was their
**content**: a default that can only originate in a scheme the document lacked
until 10-31 dates the schema to after 10-31, with no clock involved.

This repo has a standing note that a null result is a claim about the instrument
first. The inverse holds and had not been written down: **a falsification is a
claim about the instrument too.** Rejecting your own hypothesis feels like rigour,
which is exactly why it gets less scrutiny than accepting one.

#### §9.22 addendum 3 — three mechanisms for a non-event, and the one rule worth keeping

A thread that ended with nothing to fix, recorded because the *shape* of it cost
more than any error inside it.

A peer reported a release body asserting `Commits in this tag:` and listing 2 of
19. Three causes were then proposed in sequence — their time-window mechanism, my
`creationTimestamp` falsification earlier in the day, and my `--no-merges`
mechanism. The last was worth something to this repo on its face, because
`.github/workflows/release-notes.yaml` here delegates to the *same* org-wide
reusable workflow, so a defect in its fallback body would be ours on the next tag.

I read that workflow rather than guessing, and two suspicions died in it:
`fetch-depth: 0` is set and commented, and `PREV` is resolved by commit topology
(`git describe --tags --abbrev=0 --exclude=$TAG $TAG^`) rather than by version
sort. That left `git log --no-merges` as the only filter between 19 and 2, and I
sent the one-command kill test rather than the explanation.

**It died, and it died irrelevant. The observation never happened.** The body
listed all 19. The "2" was an artifact of a sweep step that ended in `| head -6`,
read as the whole body — six lines being a marker, two blanks, the fallback
notice, and two bullets.

So: **three sound mechanisms for a number that did not exist.** Also measured
here, which is why the hypothesis was plausible enough to send and useless anyway:

```
this repo, 0.1.0..HEAD:   all 126   --no-merges 126   merges 0
```

Main here is linear, so `--no-merges` drops nothing. It drops nothing in their
monorepo either. No release body in either repo is affected, and nothing in this
repo needed changing.

**The rule worth keeping is theirs, and it is about provenance rather than
quantities: when one row of a table is hand-typed among measured ones, the typed
row is the finding.** Their comparison table had three rows from
`grep -cE '^- '` and one typed by hand from the truncated reading — and the typed
row was the only one that mattered. The measured rows lend the typed one their
credibility; that is the entire mechanism. It would have caught four of the nine
instrument errors the two sessions produced today.

A second rule fell out of the sequence: **when a third mechanism arrives for one
unmoving observation, the thing nobody has checked is whether the observation
happened.** The countervailing evidence was in the same output that produced the
artifact — `of which merges: 0` — quoted and unused.

Corollary, narrow and mechanical: `| head -N` and `| tail -N` make a sample look
like a total. Do not conclude about a whole from a pipeline ending in one.

This repo's standing note says a null result is a claim about the instrument
first. Two more belong beside it now: so is a falsification, and so is the
*existence* of the thing being explained.
