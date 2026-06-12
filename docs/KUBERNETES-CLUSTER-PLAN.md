# `kubernetes-cluster` blueprint — implementation plan

Source-of-truth roadmap for hardening and extending `charts/kubernetes-cluster/`
(the MVP Layer-2 blueprint that turns Ironic-provisioned blades into a
kubeadm-bootstrapped Kubernetes cluster).

Authored against scaffold at commit `fc88ff1`. Every milestone reuses one or
more of the three existing Krateo charts/compositions wherever possible:

- `charts/baremetal-host/` — unified single-CRD BaremetalHost composition
  (`spec.undeploy`, `spec.undeployMode`, `spec.online`, `spec.image`,
  `spec.maintenance`, `spec.detached`, `spec.cleanSteps`, `spec.configDrive`).
- `charts/baremetal-discovery/` — discovery-only flow
  (`enroll → manageable → inspect → manageable`).
- `charts/baremetal-lifecycle/` — lifecycle-only flow
  (`available → deploy → active` and undeploy back).

All five MVP gaps fill via chart templates + values schema changes + cloud-init
extensions; no new KOG primitives are needed.

## Landed status

| # | Milestone | Chart version | Commit |
|---|---|---|---|
| 2 | Network plumbing (endpoint + reachability hint) | 0.2.0 | `20205be` |
| 3 | CA bundle hardening (`kube-root-ca.crt` lookup) | 0.2.0 | `20205be` |
| 4 | Token rotation (systemd timer, 12h cadence) | 0.3.0 | `e5bfb02` |
| 5b | Drain & delete worker (BL→BH migration, drain Job) | 0.4.0 | `2bacc7d` |
| 1 | HA control plane (stacked etcd, cert-key rendezvous) | 0.5.0 | `45f4fe0` |
| 5a | k8s upgrade by-reimage (`upgrade.targetNode`) | 0.6.0 | `a133577` |
| 5c | Failed CP recovery (`recovery.failedNodes[]`) | 0.6.0 | `a133577` |

Recommended landing order (`2 → 3 → 4 → 5b → 1 → 5a → 5c`) was followed
end-to-end. Chart at `0.6.0`, CompositionDefinition repointed.

Real Ettore-lab deployable manifest is at
[`manifests/kubernetescluster-ettore-lab.yaml`](../manifests/kubernetescluster-ettore-lab.yaml)
— CP on blade06 + worker on blade10, real Redfish credentials and
UUIDs. Two pre-deploy operator steps documented in
[`docs/USER-GUIDE.md`](USER-GUIDE.md): plumb the management apiserver
to the blade network, and delete the existing `blade06` + `blade10`
BHs so the new chart's BHs can take ownership.

---

## 1. HA control plane (stacked etcd)

**Design.** Promote `controlPlane.node` to `controlPlane.nodes` (array;
index 0 = bootstrap CP). Add `controlPlane.replicas` (`{1,3,5}` only,
schema-enforced). New helper `kubernetes-cluster.certKey` `lookup`s the
bootstrap CP's `Node.spec.extra.cert_key`. Bootstrap CP's `cpUserData`
runs `kubeadm init --upload-certs --control-plane-endpoint=<Values.controlPlane.endpoint> --certificate-key=$(openssl rand -hex 32)`;
the publish script PATCHes both `kubeadm_join` and `cert_key` onto its
own Node CR. Additional CPs render via `lifecycle-cp-replicas.yaml`
gated on `cert_key` AND a freshness stamp (`extra.cert_key_at`); they
run `kubeadm join … --control-plane --certificate-key <key>`. The
existing `cpToken` SA's Role widens to `patch` two `extra` keys.

**Reuses:** `charts/baremetal-lifecycle/` — one BL CR per CP replica,
identical surface to the MVP CP path. No new lifecycle pattern.

**Files.**

- `charts/kubernetes-cluster/values.yaml`, `values.schema.json` —
  promote to `controlPlane.nodes[]` + `controlPlane.endpoint`.
- `charts/kubernetes-cluster/templates/_helpers.tpl` — add `certKey`,
  `bootstrapCpNodeName`, `controlPlaneEndpoint`, and the
  `--upload-certs` init userData branch.
- `charts/kubernetes-cluster/templates/lifecycle-cp.yaml` — render
  bootstrap CP only.
- `charts/kubernetes-cluster/templates/lifecycle-cp-replicas.yaml`
  (new) — N–1 BL CRs gated on `cert_key`.
- `charts/kubernetes-cluster/templates/rbac.yaml` — extend patch path
  to `extra.cert_key`.

**Citations.**

- [kubeadm HA topology](https://kubernetes.io/docs/setup/production-environment/tools/kubeadm/high-availability/) —
  > "Please note that the certificate-key gives access to cluster sensitive data, keep it secret! As a safeguard, uploaded-certs will be deleted in two hours; If necessary, you can use kubeadm init phase upload-certs to reload certs afterward."
- [`kubeadm init`](https://kubernetes.io/docs/reference/setup-tools/kubeadm/kubeadm-init/) —
  > "Key used to encrypt the control-plane certificates in the kubeadm-certs Secret. The certificate key is a hex encoded string that is an AES key of size 32 bytes."
- [`kubeadm join`](https://kubernetes.io/docs/reference/setup-tools/kubeadm/kubeadm-join/) —
  > "Use this key to decrypt the certificate secrets uploaded by init."

**Risks.**

- **2h cert-key TTL** — if a replica BL stalls in Ironic for >2h after
  the bootstrap CP publishes, the join fails. Mitigation: gate replica
  render on `extra.cert_key_at` age; if stale, re-run
  `kubeadm init phase upload-certs --upload-certs` on the bootstrap CP
  via Milestone 4's maintenance window mechanism.
- `--control-plane-endpoint` must exist before bootstrap CP runs
  `kubeadm init` — hard dependency on Milestone 2.
- Race: cdc may render replica CRs against a stale `cert_key`. Stamp
  `extra.cert_key_at` and gate on age, not just presence.

---

## 2. Network plumbing (management API reachable from blades)

**Design.** No chart code change — a network design decision documented
in the chart and enforced via schema. Add `controlPlane.endpoint` (DNS
+ port) as required when `controlPlane.replicas>1`. Add
`network.managementApiReachability` enum so future ops tooling can
branch on the chosen option.

| Option | Cost | When |
|---|---|---|
| kube-vip (DaemonSet on CPs) | one extra static-pod manifest in CP userData | air-gapped lab, no external LB |
| MetalLB L2/BGP | requires MetalLB on management cluster | only if blades share L2 with mgmt |
| External LB (haproxy/F5) | infra dependency | enterprise prod default |
| NodePort + DNS RR | zero infra, no HA failover | single-CP dev only |

The MVP `managementCluster.apiUrl` already pins the address; this
milestone formalises the requirement that whatever you choose, the LB
IP equals `controlPlaneEndpoint` — kubeadm enforces this.

**Reuses:** none directly — pure network design. Justification: there
is no Ironic-side abstraction for cluster ingress; this is
upstream-cluster plumbing.

**Files.**

- `docs/USER-GUIDE.md` — extend with a "Cluster ingress" section
  containing the option matrix.
- `charts/kubernetes-cluster/values.schema.json` — make
  `controlPlane.endpoint` required when `controlPlane.replicas>1`.
- `manifests/kubernetescluster-example.yaml` — annotate the chosen
  option.

**Citations.**

- [Service `type: LoadBalancer`](https://kubernetes.io/docs/concepts/services-networking/service/#type-loadbalancer) —
  > "exposes the Service externally using a cloud provider's load balancer."
- [kubeadm HA — software LB options](https://kubernetes.io/docs/setup/production-environment/tools/kubeadm/high-availability/#options-for-software-load-balancing) —
  > "Make sure the address of the load balancer always matches the address of kubeadm's `ControlPlaneEndpoint`."

**Risks.**

- kube-vip in CP userData is tempting but couples CP bootstrap to a
  moving target (kube-vip release cadence).
- NodePort+DNS leaks 6443 mismatch (advertised vs reachable); forbid
  explicitly for `replicas>1`.

---

## 3. CA bundle hardening

**Design.** Remove `managementCluster.caBundle` from values (deprecate
to optional override). Add helper `kubernetes-cluster.mgmtCaBundle`
that `lookup`s the `kube-root-ca.crt` ConfigMap in
`.Values.managementCluster.serviceAccountNamespace` (auto-projected
into every namespace by the root-ca-cert-publisher controller in
k8s 1.20+). CP `publish-join.sh` writes the embedded CA from the
lookup, eliminating the operator burden of pasting PEM and the risk of
stale/wrong CA. If the ConfigMap is absent (skew or detached
namespace), the helper returns empty and the CP lifecycle skips render
— same guard pattern as `cpToken`.

**Reuses:** none — management-cluster-side ConfigMap read, no overlap
with the three bare-metal charts.

**Files.**

- `charts/kubernetes-cluster/templates/_helpers.tpl` — new
  `mgmtCaBundle` helper; rewire `cpUserData` to use it.
- `charts/kubernetes-cluster/values.yaml`, `values.schema.json` —
  deprecate `managementCluster.caBundle`, keep as optional override for
  non-kubeadm management clusters.
- `charts/kubernetes-cluster/templates/lifecycle-cp.yaml` — gate render
  on `mgmtCaBundle` non-empty.

**Citations.**

- [Cluster certificates](https://kubernetes.io/docs/concepts/cluster-administration/certificates/) —
  > "To learn how to generate certificates for your cluster, see Certificates."
- [Administer certificates](https://kubernetes.io/docs/tasks/administer-cluster/certificates/) —
  > "A client node may refuse to recognize a self-signed CA certificate as valid. For a non-production deployment, or for a deployment that runs behind a company firewall, you can distribute a self-signed CA certificate to all clients and refresh the local list for valid certificates."

**Risks.**

- `kube-root-ca.crt` is auto-projected from k8s 1.20+; verify
  management cluster meets minimum.
- Some hardened clusters disable root-ca-cert-publisher; keep
  `caBundle` value as escape hatch.
- The SA-namespace ConfigMap projects the *management* cluster's CA —
  exactly what `publish-join.sh` needs. Do not confuse with the
  *workload* (bare-metal-bootstrapped) cluster's CA.

---

## 4. Token rotation

**Design.** MVP mints a 24h-TTL token once at `kubeadm init`. After
24h, the published `kubeadm_join` is invalid; new workers can't join.
The cheapest path that respects the no-code constraint: extend the CP
cloud-init to install a systemd timer (`kubeadm-token-refresh.timer`
firing every 12h) whose service script runs
`kubeadm token create --print-join-command --ttl 24h` and PATCHes the
new value to its own `Node.spec.extra.kubeadm_join`. cdc reconcile
picks up the new token on next pass; the worker lifecycle template's
gate (`joinCommand` non-empty) keeps firing fresh joins for any
not-yet-deployed worker.

**Reuses:** `charts/baremetal-host/` for failure recovery only — if
the refresher dies, the operator drives the CP through
`spec.undeploy: true` → re-deploy (the image carries the refresher
unit). For the steady-state refresh, the systemd-timer-in-cloud-init
pattern is the minimal-invasion option and avoids a new k8s controller.

**Files.**

- `charts/kubernetes-cluster/templates/_helpers.tpl` — extend
  `cpUserData` with `/etc/systemd/system/kubeadm-token-refresh.{service,timer}`.
- `charts/kubernetes-cluster/templates/rbac.yaml` — already grants
  `patch` on the CP Node CR; reused unchanged.
- `docs/USER-GUIDE.md` — document the 24h boundary and the refresher.

**Citations.**

- [`kubeadm token`](https://kubernetes.io/docs/reference/setup-tools/kubeadm/kubeadm-token/) —
  > "kubeadm init creates an initial token with a 24-hour TTL."
- [Bootstrap tokens](https://kubernetes.io/docs/reference/access-authn-authz/bootstrap-tokens/) —
  > "The expiration field controls the expiry of the token. Expired tokens are rejected when used for authentication and ignored during ConfigMap signing."

**Risks.**

- Timer drift: if the blade's clock skews past TTL before the refresher
  runs, mid-window joins fail. Mitigate with 12h cadence (50% margin).
- Patch failure (mgmt API unreachable): refresher must retry with
  backoff and log to journal.
- Token leak in Node CR: `spec.extra.kubeadm_join` is world-readable
  inside the operator namespace; document RBAC scoping.

---

## 5a. k8s patch / minor upgrade

**Design.** Upgrade-by-reimage, not in-place. Bumping `.Values.k8sVersion`
causes the chart to render new cloud-init for any blade that walks
`available → deploy`. For an existing CP/worker, the operator sets
`BaremetalHost.spec.undeploy: true` on the target blade's BH CR (or the
BL CR), waits for `available`, then clears `undeploy` — the chart
re-deploys with the new version. Sequence: drain in k8s (a pre-undeploy
Job — same pattern as 5b) → BH undeploy → image re-deploy with the new
kubeadm version → new node joins via the live token. Add
`controlPlane.upgrade.strategy: reimage` and `k8sVersionPerNode`
override so the operator can roll one CP at a time per upstream
guidance. In-place upgrade (`kubeadm upgrade apply` over SSH) would
require a new mechanism (SSH controller, or a Job that mounts a
kubeconfig and execs into the blade) — rejected for the no-code
constraint.

**Reuses:** `charts/baremetal-host/` via `spec.undeploy: true`
(widened gate in v0.3.4:
`{active, deploy failed, clean failed, wait call-back, deploying}`)
plus the existing image-swap recipe in `docs/USER-GUIDE.md`. Highest
leverage reuse in the plan.

**Files.**

- `charts/kubernetes-cluster/values.yaml`, `values.schema.json` —
  `controlPlane.upgrade`, per-node `k8sVersion` override.
- `charts/kubernetes-cluster/templates/_helpers.tpl` — `nodeK8sVersion`
  resolver.
- `charts/kubernetes-cluster/templates/upgrade-drain-job.yaml` (new) —
  pre-undeploy drain Job (shared with 5b).
- `docs/USER-GUIDE.md` — upgrade runbook (bump version, undeploy CPs
  sequentially).

**Citations.**

- [`kubeadm upgrade`](https://kubernetes.io/docs/tasks/administer-cluster/kubeadm/kubeadm-upgrade/) —
  > "The upgrade workflow at high level is the following: 1. Upgrade a primary control plane node. 2. Upgrade additional control plane nodes. 3. Upgrade worker nodes."
- [Upgrade control plane nodes](https://kubernetes.io/docs/tasks/administer-cluster/kubeadm/kubeadm-upgrade/#upgrade-control-plane-nodes) —
  > "The upgrade procedure on control plane nodes should be executed one node at a time."

**Risks.**

- Reimage CPs drops local etcd member every time — requires HA
  (Milestone 1) and etcd-aware sequencing (drain →
  `etcdctl member remove` → undeploy → re-deploy → auto-rejoin as new
  member). Single-CP clusters cannot use this strategy.
- Skew rule: kubelet may lag apiserver by 3 minor versions; the
  runbook must enforce strict CP-before-worker ordering.

---

## 5b. Drain and delete a worker

**Design.** Removing an entry from `.Values.workers.nodes` triggers
two-phase orchestration in-chart. **Phase 1**: render a drain Job
(`kubectl drain <node> --ignore-daemonsets --delete-emptydir-data`)
that runs against the *workload* cluster's apiserver using the CP's
kubeconfig (delivered via Milestone 3's CA plumbing, projected into the
management cluster as a Secret). **Phase 2**: chart patches
`BaremetalHost.spec.undeploy: true` (the worker stays in
`.Values.workers.removed[]` until BH reaches `available`). **Phase 3**:
chart stops rendering the worker BL CR; cdc's helm upgrade deletes it;
KOG fires `DELETE /v1/nodes/{id}` cleanly.

**Reuses:** `charts/baremetal-host/` via `spec.undeploy: true` — the
widened v0.3.4 gate. The "Don't `kubectl delete bh` while blade is at
`active`" warning in USER-GUIDE.md is exactly what we automate.

**Files.**

- `charts/kubernetes-cluster/templates/drain-jobs.yaml` (new) — Job per
  `removed` worker.
- `charts/kubernetes-cluster/templates/lifecycle-workers.yaml` — read
  `removed` list, render `spec.undeploy: true` instead of deploy.
- `charts/kubernetes-cluster/templates/_helpers.tpl` —
  `workerDrainComplete` lookup gate (`.status.succeeded == 1`).
- `charts/kubernetes-cluster/values.yaml`, `values.schema.json` —
  `workers.removed[]` array.

**Citations.**

- [Safely drain node](https://kubernetes.io/docs/tasks/administer-cluster/safely-drain-node/) —
  > "Safe evictions allow the pod's containers to gracefully terminate and will respect the PodDisruptionBudgets you have specified."
- [`kubectl drain`](https://kubernetes.io/docs/reference/generated/kubectl/kubectl-commands#drain) —
  > "Evicts all pods from a node" (Cluster Management section).

**Risks.**

- PDB blocks drain indefinitely → BH never undeploys. Add `drainTimeout`
  value and `--disable-eviction` fallback after threshold.
- Workload-cluster kubeconfig: requires a publish step (CP writes its
  `admin.conf` to a Secret patched into the management cluster). Lifts
  cleanly off Milestone 3's CA bundle plumbing.

---

## 5c. Recover a failed CP

**Design.** Two scopes:

- **Single-CP**: data loss without etcd backup — explicitly out of
  scope until Milestone 1 lands. Document with a hard banner in
  USER-GUIDE.md.
- **HA (post-Milestone-1)**: failed CP recovery is (a) drain still-
  reachable workloads off it, (b) `etcdctl member remove <id>` from a
  healthy CP, (c) set the failed CP's `BaremetalHost.spec.undeploy: true`
  (widened gate covers `deploy failed`, `wait call-back` — perfect fit),
  (d) clear `undeploy`, chart re-renders deploy with the same
  `kubeadm join --control-plane --certificate-key` userData,
  (e) blade re-joins as fresh etcd member. Add
  `controlPlane.recovery.failedNodes[]` list to bypass the
  bootstrap-CP-only `kubeadm init`; nodes there get the join userData.

**Reuses:** `charts/baremetal-host/` via `spec.undeploy: true` from
`deploy failed` / `wait call-back`. Mirrors 5a but driven by health
rather than version.

**Files.**

- `charts/kubernetes-cluster/templates/_helpers.tpl` —
  `failedCpUserData` (join only, no init).
- `charts/kubernetes-cluster/templates/lifecycle-cp-replicas.yaml` —
  extend to include nodes listed under `recovery.failedNodes` with
  rotated cert-key (re-uploaded via Milestone 1's maintenance window).
- `docs/USER-GUIDE.md` — recovery runbook with `etcdctl` prerequisite
  step (operator-driven, out-of-chart).
- `charts/kubernetes-cluster/values.schema.json` —
  `controlPlane.recovery.failedNodes[]`.

**Citations.**

- [kubeadm HA — manual cert distribution](https://kubernetes.io/docs/setup/production-environment/tools/kubeadm/high-availability/#manual-certificate-distribution) —
  > "If instead, you prefer to copy certs across control-plane nodes manually or using automation tools, please remove this flag and refer to Manual certificate distribution section below."
- [etcd remove member](https://etcd.io/docs/v3.5/op-guide/runtime-configuration/#remove-a-member) —
  > "Suppose the member ID to remove is a8266ecf031671f3. Use the `remove` command to perform the removal: `etcdctl member remove a8266ecf031671f3`."

**Risks.**

- `etcdctl member remove` is operator-run, outside the chart — runbook
  discipline matters.
- Cert-key has been deleted (2h TTL passed since original `init`);
  rotation step is mandatory before re-deploy.
- Re-using same `nodeUuid` keeps Ironic identity stable across
  recovery — encourage the operator to pin it.

---

## Sequencing

Milestone 2 (network plumbing) is a prerequisite for everything beyond
MVP: Milestone 1 (HA CP) cannot succeed without a stable
`--control-plane-endpoint`, and Milestones 5a / 5c assume CPs share a
load-balanced address. Land Milestone 2's doc/schema work first (no
chart logic, low cost). Milestone 3 (CA hardening) is independent of
all others — land in parallel with 2 to clean up the existing
`caBundle` paste-in burden before users multiply. Milestone 1 (HA CP)
requires 2 + 3 landed and is the gate for 5a (reimage CP upgrade) and
5c (CP recovery) — both depend on having more than one CP. Milestone 4
(token rotation) is independent of 1 and should land second after
2 / 3 because the 24h ceiling is the most user-visible MVP defect.
Milestone 5b (drain & delete worker) only needs Milestone 3's CA
plumbing (for the workload kubeconfig Secret) and can land before 1 —
it is the cleanest "reuse `baremetal-host`" win and a good shakedown of
the drain-Job pattern that 5a will inherit.

**Recommended order: 2 → 3 → 4 → 5b → 1 → 5a → 5c.**

(2 and 3 can run in parallel.)

### Critical files for implementation

- `charts/kubernetes-cluster/templates/_helpers.tpl`
- `charts/kubernetes-cluster/templates/lifecycle-cp.yaml`
- `charts/kubernetes-cluster/templates/lifecycle-workers.yaml`
- `charts/kubernetes-cluster/values.schema.json`
- `charts/baremetal-host/templates/transition-undeploy.yaml`
  (integration point for milestones 4, 5a, 5b, 5c)
