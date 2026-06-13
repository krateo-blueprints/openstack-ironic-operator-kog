# `kubernetes-cluster` composition

Provision a real Kubernetes cluster on Ironic-managed bare-metal nodes by applying **one** Custom Resource. No Go operator, no extra controllers — a layered Krateo composition that decomposes into well-known primitives and ends up calling Ironic's REST API.

## Table of contents

- [Layered architecture](#layered-architecture)
- [Ironic provision-state FSM](#ironic-provision-state-fsm)
- [Cluster bringup — sequence diagram](#cluster-bringup--sequence-diagram)
- [The rendezvous: how the worker finds the CP](#the-rendezvous-how-the-worker-finds-the-cp)
- [Bridge sidecar — state machine](#bridge-sidecar--state-machine)
- [Workload kubeconfig delivery (v0.10.13)](#workload-kubeconfig-delivery-v01013)
- [What each embedded script does](#what-each-embedded-script-does)
- [Spec reference](#spec-reference)
- [Day-2 operations](#day-2-operations)
- [Accessing the provisioned cluster](#accessing-the-provisioned-cluster)
- [Troubleshooting playbook](#troubleshooting-playbook)
- [Related docs](#related-docs)

---

## Layered architecture

Every box below is a Helm chart shipping its own [`CompositionDefinition`](https://docs.krateo.io/key-concepts/composition-definition/). Krateo's `composition-dynamic-controller` (cdc) turns each CR into a Helm release at the next layer down.

```
┌──────────────────────────────────────────────────────────────────────────┐
│ L4    KubernetesCluster CR             (what the user applies)            │
│       └─ kubernetes-cluster chart                                         │
│          ├── lifecycle-cp.yaml         → BaremetalHost  (bootstrap CP)    │
│          ├── lifecycle-cp-replicas.yaml→ BaremetalHost  (HA replica CPs)  │
│          ├── lifecycle-cp-recovery.yaml→ BaremetalHost  (failed-CP rebuild)│
│          ├── lifecycle-workers.yaml    → BaremetalHost  (workers)         │
│          ├── drain-jobs.yaml           → Job            (worker drain)    │
│          └── rbac.yaml                 → SA + Role + Token Secret         │
├──────────────────────────────────────────────────────────────────────────┤
│ L3    BaremetalHost CR  (one per blade — the full lifecycle)              │
│       └─ baremetal-host chart                                             │
│          ├── node.yaml                 → Node           (KOG primitive)   │
│          ├── ports.yaml                → Port × N       (KOG primitive)   │
│          ├── transition-manage.yaml    → NodeProvision  (manage)          │
│          ├── transition-provide.yaml   → NodeProvision  (provide)         │
│          ├── transition-deploy.yaml    → NodeProvision  (active)          │
│          ├── transition-undeploy.yaml  → NodeProvision  (deleted)         │
│          ├── transition-inspect.yaml   → NodeProvision  (inspect)         │
│          ├── transition-clean.yaml     → NodeProvision  (clean)           │
│          └── transition-power.yaml     → NodePower      (on/off)          │
│                                                                          │
│       siblings (not used by kubernetes-cluster):                          │
│        • BaremetalLifecycle CR — slim enroll→active without ports/rebuild │
│        • BaremetalDiscovery CR — inventory pass, surfaces hw in status    │
├──────────────────────────────────────────────────────────────────────────┤
│ L2    KOG primitives                                                      │
│       Node / Port / NodeProvision / NodePower / Allocation / DeployTemplate│
│       Each CRD is generated from oas/ironic-*.yaml by Krateo's            │
│       oasgen-provider; each has a rest-dynamic-controller (rdc) that      │
│       maps "create/get/patch/delete CR" to a REST call.                   │
│                                                                          │
│       Endpoint singletons (applied once by `make restdef-up`):            │
│        • NodeConfiguration   — Ironic API URL + microversion              │
│        • PortConfiguration   — same, for the Port rdc                     │
├──────────────────────────────────────────────────────────────────────────┤
│ L1    Ironic REST API                                                     │
│       POST /v1/nodes                  PUT /v1/nodes/{id}/states/provision │
│       POST /v1/ports                  PUT /v1/nodes/{id}/states/power     │
│       PATCH /v1/nodes/{id}            DELETE /v1/nodes/{id}               │
└──────────────────────────────────────────────────────────────────────────┘
```

The siblings (`BaremetalLifecycle`, `BaremetalDiscovery`) solve the same general problem at narrower scopes. `kubernetes-cluster` only renders `BaremetalHost` — full state machine, full power.

---

## Ironic provision-state FSM

KOG's `NodeProvision` rdc drives Ironic through this state machine by `PUT`ing `target` on `/v1/nodes/{id}/states/provision`. The `baremetal-host` chart renders one `NodeProvision` CR per transition, gated on the current `provision_state`.

```
            ┌────────┐                                                ┌────────┐
            │ enroll │  (POST /v1/nodes)                              │ error  │
            └────┬───┘                                                └────────┘
                 │ target=manage                                         ▲
                 ▼                                                       │
            ┌─────────────┐                                              │ (any failure)
            │ verifying   │   (Ironic verifies driver_info)              │
            └────┬────────┘                                              │
                 │ ok                                                    │
                 ▼                                                       │
            ┌──────────────┐                                             │
   ┌────────│ manageable   │◄─────┐                                     │
   │        └────┬─────────┘      │                                      │
   │             │ target=provide │ target=manage                        │
   │ target=     ▼                │ (undeploy back to manageable)        │
   │ inspect ┌──────────┐          │                                     │
   │         │ cleaning │          │                                     │
   │         └────┬─────┘          │                                     │
   │              │ ok             │                                     │
   │              ▼                │                                     │
   │         ┌────────────┐        │                                     │
   ▼         │ available  │◄───────┤                                     │
┌───────────┐└────┬───────┘        │                                     │
│inspecting │     │ target=active   │                                     │
│           │     │                │                                     │
└─────┬─────┘     ▼                │                                     │
      │      ┌─────────────┐       │                                     │
      └──►   │ deploying   │       │                                     │
             └────┬────────┘       │                                     │
                  │ image written  │                                     │
                  ▼                │                                     │
             ┌────────────────┐    │                                     │
             │ wait call-back │    │                                     │
             └────┬───────────┘    │                                     │
                  │ ramdisk done   │ target=deleted                      │
                  ▼                │ (undeploy)                          │
             ┌─────────┐           │                                     │
             │ active  │───────────┘                                     │
             └─────────┘                                                 │
                                                                         │
              all transitions emit error → ────────────────────────────►─┘
```

The chart's `lifecycle-cp.yaml` template renders the BH with `spec.image`, `configDrive`, and `online: true`. KOG walks `enroll → manageable → available → deploying → wait call-back → active` automatically; the chart only re-renders to inject the right `NodeProvision` per state.

---

## Cluster bringup — sequence diagram

End-to-end on a fresh apply of `KubernetesCluster ettore`. Real timing measured on the Ettore lab, Debian 13 trixie, k8s 1.36.2:

```
USER                MGMT CLUSTER (kind)             LAB / IRONIC          BLADE 06 (CP)        BLADE 10 (worker)
  │                       │                              │                     │                    │
  │ kubectl apply         │                              │                     │                    │
  │  KubernetesCluster    │                              │                     │                    │
  ├──────────────────────►│                              │                     │                    │
  │                       │ cdc helm-installs            │                     │                    │
  │                       │  kubernetes-cluster chart    │                     │                    │
  │                       │                              │                     │                    │
  │                       │ renders ettore-cp-blade06    │                     │                    │
  │                       │  BaremetalHost CR            │                     │                    │
  │                       │   ↓                          │                     │                    │
  │                       │ cdc-bh helm-installs         │                     │                    │
  │                       │  baremetal-host chart        │                     │                    │
  │                       │   ↓                          │                     │                    │
  │                       │ Node + Port × 2 + NodeProv  │                     │                    │
  │                       │   ↓                          │                     │                    │
  │                       │ KOG rdc                      │                     │                    │
  │                       │ POST /v1/nodes  ─────────────►                     │                    │
  │                       │ POST /v1/ports  ─────────────►                     │                    │
  │                       │                              │ enroll              │                    │
  │                       │ PUT manage ──────────────────► verifying           │                    │
  │                       │                              │ manageable          │                    │
  │                       │ PUT provide ─────────────────► cleaning            │                    │
  │                       │                              │ available           │                    │
  │                       │ PUT active  ─────────────────► deploying           │                    │
  │                       │                              │  ↓ writes qcow2     │                    │
  │                       │                              │ wait call-back      │                    │
  │                       │                              │  ↓ boot complete    │                    │
  │                       │                              │ active ─────────────► T+10 install-k8s.sh│
  │                       │                              │                     │ → cp-init.sh       │
  │                       │                              │                     │   kubeadm init     │
  │                       │                              │                     │   kubectl apply CNI│
  │                       │                              │                     │ → publish-         │
  │                       │                              │                     │   workload-kc.sh   │
  │                       │   ◄──── POST Secret ─────────┼─────────────────────┤                    │
  │                       │ (mgmt-api proxy in wg-pod)   │                     │                    │
  │                       │                              │                     │ → publish-join.sh  │
  │                       │                              │ ◄─ PATCH node.extra ┤                    │
  │                       │                              │                     │   {kubeadm_join,   │
  │                       │                              │                     │    cert_key}       │
  │                       │                              │                     │                    │
  │                       │ bridge sidecar in wg-pod     │                     │                    │
  │                       │ polls Ironic every 10s       │                     │                    │
  │                       │ ◄─ GET /v1/nodes/blade06 ────┤                     │                    │
  │                       │ PATCH Node.spec.extra ←──────┤                     │                    │
  │                       │                              │                     │                    │
  │                       │ cdc re-renders ettore CR     │                     │                    │
  │                       │ (joinCommand lookup matches) │                     │                    │
  │                       │ renders ettore-worker-blade10│                     │                    │
  │                       │   ↓ same baremetal-host path │                     │                    │
  │                       │ POST /v1/nodes  ─────────────►                     │                    │
  │                       │ PUT active  ─────────────────►                     │ enroll → … active ►│
  │                       │                              │                     │                    │ install-k8s.sh
  │                       │                              │                     │                    │ → join.sh
  │                       │                              │                     │                    │   kubeadm join
  │                       │                              │                     │ ◄── ─── ─── ─── ───┤   192.168.0.206
  │                       │                              │                     │   joined           │
  │                       │                              │                     │                    │
  │                       │                              │                     │ blade06 Ready      │
  │                       │                              │                     │ blade10 Ready      │
  │                       │                              │                     │                    │
  │◄──────────────────────┼──────────────────────────────┼─────────────────────┴────────────────────┤
  │  kubectl get nodes via ettore-workload-kubeconfig Secret                                         │
  │   → blade06 Ready  blade10 Ready                                                                 │

Total: ~23-24 min hands-off, 0 pod restarts (since v0.10.12).
```

---

## The rendezvous: how the worker finds the CP

The bare-metal blade can reach Ironic on the lab's OOB network, but it CAN'T reach the management cluster directly (the WG tunnel routes mgmt → lab, not the reverse). So we can't have the CP write to a k8s Secret and let the worker read it — the worker's userData has to embed the join command at chart-render time.

That creates a chicken-and-egg: the worker BH needs the join command in its userData, but the join command doesn't exist until the CP has run `kubeadm init`.

The chart resolves it through **Ironic `node.extra` as a publish-board**, plus a bridge sidecar that mirrors back into the management cluster:

```
┌─────────────┐                                             ┌──────────────────┐
│  blade06    │   1. publish-join.sh PATCHes                │ Ironic           │
│  (CP)       ├─────────────────────────────────────────────►  /v1/nodes/blade06│
└─────────────┘   { extra: { kubeadm_join, cert_key } }     │  .extra = {…}    │
                                                            └────────┬─────────┘
                                                                     │
                                                                     │ 2. bridge sidecar
                                                                     │    polls every 10s
                                                                     │    (GET via keystone proxy)
                                                                     ▼
                                                            ┌──────────────────────┐
                                                            │ kind mgmt cluster    │
                                                            │                      │
                                                            │ Node CR              │
                                                            │  blade06             │
                                                            │  .spec.extra = {…}   │
                                                            └────────┬─────────────┘
                                                                     │
                                                                     │ 3. cdc-kubernetes-cluster
                                                                     │    re-renders ettore CR;
                                                                     │    `joinCommand` lookup
                                                                     │    on Node.spec.extra
                                                                     │    now resolves.
                                                                     ▼
                                                            ┌──────────────────────┐
                                                            │ ettore-worker-blade10│
                                                            │  BaremetalHost CR    │
                                                            │  spec.configDrive    │
                                                            │   .userData =        │
                                                            │   "...kubeadm join   │
                                                            │    192.168.0.206:6443│
                                                            │    --token …"       │
                                                            └──────────────────────┘
```

The publish-board pattern means the worker BH simply doesn't render until the CP has published. The cdc's natural reconcile cadence picks up the bridged value within ~30s of it landing.

For HA the same pattern carries `cert_key` (the encryption key for kubeadm's `kubeadm-certs` Secret, so replica CPs can `kubeadm join --control-plane`).

---

## Bridge sidecar — state machine

`scripts/ironic-node-extra-bridge.py` runs in the `wg-ironic-proxy` pod alongside the keystone proxy. Two responsibilities:

```
                         ┌────────────────────────────┐
              every 10s  │ list Node CRs in openstack │
                         │  (mgmt-api SA list)        │
                         └────────────┬───────────────┘
                                      │
                ┌─────────────────────┼─────────────────────────────────────┐
                │                     │                                     │
                ▼ for each Node       │                                     ▼ for each KOG primitive
        ┌────────────────────┐         │                            ┌────────────────────────────┐
        │ GET /v1/nodes/uuid │         │                            │ has annotation             │
        │  via keystone      │         │                            │  meta.helm.sh/release-name │
        │  proxy (127:8080)  │         │                            └──────────────┬─────────────┘
        └────────┬───────────┘         │                                           │
                 │                     │                                           ▼
                 ▼                     │                                  ┌─────────────────────┐
        ┌────────────────────┐         │                                  │ secret with that    │
        │ provision_state in │         │                                  │ release name in ns? │
        │ {active, deploying,│         │                                  └────────┬────────────┘
        │  wait call-back}?  │         │                                           │
        └─┬──────────────────┘         │                                           │ no, and CR is
          │yes                         │                                           ▼ ≥ 60s old
          ▼                            │                                  ┌─────────────────────┐
   ┌──────────────────┐                │                                  │ strip finalizer +    │
   │ if Ironic.extra  │                │                                  │ DELETE the CR        │
   │  ≠ Node.spec     │                │                                  │ (orphan reaper)      │
   │  → PATCH         │                │                                  └─────────────────────┘
   │ Node.spec.extra  │                │
   └──────────────────┘                │
          ▲                            │
          │no                          │
          ▼                            │
   ┌──────────────────────────────┐    │
   │ clear managed keys from BOTH │    │
   │  • Ironic.extra              │    │
   │    (PATCH /v1/nodes/uuid)    │    │
   │  • Node CR.spec.extra        │    │
   │    (merge-patch)             │    │
   │ stops stale CA hashes from   │    │
   │ leaking into next deploy     │    │
   └──────────────────────────────┘    │
```

The bridge logs every action: `bridged extra keys [...] from Ironic to CR <name>`, `cleared Ironic.extra keys [...] on <name> (state=available)`, `reaped orphan ports/<name> (release=... not tracked)`. Read with `kubectl -n openstack logs <wg-pod> -c ironic-extra-bridge -f`.

---

## Workload kubeconfig delivery (v0.10.13)

The CP's cloud-init publishes `/etc/kubernetes/admin.conf` as a Secret in the management cluster, so external users get the workload kubeconfig with one `kubectl` call instead of SSH-scping it from the blade.

```
┌─────────────┐                                          ┌──────────────────────┐
│  blade06    │  publish-workload-kubeconfig.sh           │ mgmt-api proxy in    │
│  (CP)       │                                           │ wg-ironic-proxy pod  │
│             │  POST                                     │                      │
│ admin.conf  │    /api/v1/namespaces/openstack/secrets  │ kubectl-proxy        │
│  base64─────┼─────────────────────────────────────────► │ on 198.51.100.5:8443 │
│  encoded    │  (Authorization: Bearer <cp-publisher>)   │  ↓ forwards to       │
│             │  Content-Type: application/json           │   in-cluster k8s API │
│             │                                           │  with the wg pod's   │
│ on 409:     │  PATCH                                    │  SA token            │
│ application │    .../secrets/ettore-workload-kubeconfig │                      │
│ /merge-     │  (no resourceVersion needed)              │ K8s API stores       │
│  patch+json │                                           │  Secret              │
│             │                                           └──────────┬───────────┘
│ 5x retry,   │                                                      │
│ /var/log/   │                                                      │
│ publish-    │                                                      │
│ workload-   │                                                      │
│ kubeconfig  │                                                      │
│ .log         │                                                      │
└─────────────┘                                                      │
                                                                     │
USER ($ kubectl -n openstack get secret ettore-workload-kubeconfig)  │
                                                                     │
                ◄────────────────────────────────────────────────────┘
```

The `<clusterName>-cp-publisher` SA is created by the chart's `rbac.yaml` with `secrets: create, get` in the openstack namespace. Token is read at helm-template time via `lookup` and baked into the userData — same machinery used by the join-publish path.

The Secret carries:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: ettore-workload-kubeconfig
  namespace: openstack
  labels:
    kubernetescluster.ogen.krateo.io/cluster: ettore
type: Opaque
data:
  kubeconfig: <base64 admin.conf>
```

The decoded `admin.conf` server URL is `https://192.168.0.206:6443` (the CP's data-network IP that kubeadm pinned via `--apiserver-advertise-address` since v0.10.10).

---

## What each embedded script does

Generated by the chart into the CP's cloud-init `write_files:` and called from `runcmd:`. The CP runcmd order is:

```
install-k8s.sh → cp-init.sh → publish-workload-kubeconfig.sh → publish-join.sh → systemctl enable kubeadm-token-refresh.timer
```

| Script | Logs to | What it does |
|---|---|---|
| `install-k8s.sh` | `/var/log/install-k8s.log` | apt-install `kubeadm`/`kubelet`/`kubectl`/`containerd`; rewrite `/etc/containerd/config.toml` (`bin_dir=/opt/cni/bin`, **`SystemdCgroup=true`** — fixes the etcd-flap, see v0.10.12); restart containerd; switch iptables/ip6tables to `legacy`; `modprobe overlay br_netfilter` + sysctls (`net.ipv4.ip_forward=1`, bridge-nf-call*) |
| `cp-init.sh` | `/var/log/cp-init.log` | detect `ADVERTISE_IP` from `ip route get 8.8.8.8`; `openssl rand -hex 32 > /etc/kubernetes/cert-key`; `kubeadm init --apiserver-advertise-address=$ADVERTISE_IP` (pinned in v0.10.10 to remove dual-NIC autodetect race); copy `admin.conf` to `/root/.kube/config`; `kubectl apply` flannel |
| `publish-workload-kubeconfig.sh` | `/var/log/publish-workload-kubeconfig.log` | base64 `admin.conf` → POST Secret (or PATCH on 409) to mgmt cluster's openstack namespace as `<clusterName>-workload-kubeconfig`; 5× retry on transient failures (v0.10.13) |
| `publish-join.sh` | `/var/log/publish-join.log` | `kubeadm token create --print-join-command` (24h TTL); Keystone v3 password auth to lab Keystone (system_scope); PATCH `/v1/nodes/<uuid>` with JSON-Patch `add` of `/extra/kubeadm_join` and `/extra/cert_key`; 5× retry |
| `kubeadm-token-refresh.timer` (systemd) | journal | Re-runs `publish-join.sh` every 12h (50% margin under the 24h token TTL) so workers joining beyond day 1 get a valid token |

The worker's `runcmd` is simpler — `install-k8s.sh` (same chart helper, sans `kubectl`), then `join.sh`:

| Script | Logs to | What it does |
|---|---|---|
| `join.sh` | `/var/log/join.log` | 30 × 20s retry loop running the join command embedded by the chart at render time. Survives bootstrap apiserver flap windows. Was added in v0.10.9 (before v0.10.12 made the flap go away entirely; still useful for any future transient unreachability). |

---

## Spec reference

### Identity + image

```yaml
spec:
  clusterName: ettore
  k8sVersion: v1.36.2                   # any v1.30+ that pkgs.k8s.io publishes
  image:
    source: http://172.19.74.1:8089/debian-13-genericcloud-amd64.qcow2
    checksum: http://172.19.74.1:8089/CHECKSUM
```

`k8sVersion` is matched to the latest `-X.X` Debian package suffix at install time via `apt-cache madison` (v0.10.11 fix — older minors ship `-1.1`, v1.36+ ships `-2.1`).

### CNI + network

```yaml
spec:
  cni:
    install: flannel
    manifestUrl: https://github.com/flannel-io/flannel/releases/latest/download/kube-flannel.yml
  network:
    podCIDR: 10.244.0.0/16
    serviceCIDR: 10.96.0.0/12
    managementApiReachability: nodeport-dns
```

### Ironic endpoint + auth

```yaml
spec:
  ironicApiUrl: http://ironic.openstack.svc.cluster.local:6385
  ironicApiVersion: "1.81"
  ironicAuth:
    authUrl:    http://172.19.74.1:5000
    ironicUrl:  http://172.19.74.1:6385
    username:   admin
    password:   <…>
    userDomain: Default
  configurationRef:
    name:      ironic-endpoint
    namespace: openstack
```

`ironicApiUrl` + `configurationRef` are what the KOG primitives use. `ironicAuth` is what the CP's `publish-join.sh` uses to PATCH Ironic from the blade itself.

### Management-cluster ingress

```yaml
spec:
  managementCluster:
    apiUrl:                     http://198.51.100.5:8443
    serviceAccountName:         ettore-cp-publisher
    serviceAccountNamespace:    openstack
```

`apiUrl` is the kubectl-proxy sidecar inside `wg-ironic-proxy`, reachable from the lab through the WG tunnel. The SA and its token-Secret are created by the chart's `rbac.yaml` with `secrets: create,get` + `nodes: get,patch` (the latter is for the CP's bridge-mediated extra publish).

### Control plane

```yaml
spec:
  controlPlane:
    endpoint: ""                                # set for HA (LB hostname)
    node:                                       # bootstrap CP (index 0)
      nodeName: blade06
      nodeUuid: 2f05176e-…
      driver: redfish
      driver_info:
        redfish_address: http://172.19.74.11:8000
        redfish_system_id: /redfish/v1/Systems/blade06
        redfish_username: ironic
        redfish_password: baremetal
        redfish_verify_ca: false
      parentNode: 5113ab44-…
      ports:
        - { address: "00:60:2f:36:81:01", pxe_enabled: true }
        - { address: "00:60:2f:36:81:02", pxe_enabled: false }
    # day-2 levers
    upgrade:
      targetNode: ""                            # set to CP nodeName to re-image
    recovery:
      failedNodes: []
```

### Workers

```yaml
spec:
  workers:
    drainTimeout: 5m
    nodes:
      - nodeName: blade10
        nodeUuid: fcae4724-…
        driver: redfish
        driver_info: { … }
        parentNode: 5113ab44-…
        ports: [ … ]
    removed: []                                 # move entry here to drain+undeploy
```

---

## Day-2 operations

| Goal | What to change in the CR |
|---|---|
| Add a worker | append to `spec.workers.nodes[]` |
| Drain + remove a worker | move entry from `workers.nodes[]` to `workers.removed[]` — the chart renders a drain Job mounting `<cluster>-workload-kubeconfig` and then sets `spec.undeploy: true` on the BH once drain succeeds |
| Add an HA replica CP | append to `spec.controlPlane.nodes[]` — gates on bootstrap CP's `cert_key` being present in the Node CR |
| Reimage a CP for a k8s patch upgrade | set `spec.controlPlane.upgrade.targetNode` to the nodeName; chart re-renders BH with `undeploy: true, undeployMode: full`; clear once Ironic walks back to `available` |
| Recover a failed CP | add nodeName to `spec.controlPlane.recovery.failedNodes` |
| Refresh kubeadm join token | nothing — `kubeadm-token-refresh.timer` re-runs `publish-join.sh` every 12h |
| Rotate the workload-kubeconfig Secret | `kubectl -n openstack delete secret <cluster>-workload-kubeconfig` then `kubectl -n openstack annotate kubernetescluster <cluster> reconcile-nudge="$(date +%s)"` to nudge cdc; OR just wait for the next CP reboot |

---

## Accessing the provisioned cluster

### Recommended — workload-kubeconfig Secret (v0.10.13)

```sh
kubectl -n openstack get secret <cluster>-workload-kubeconfig \
  -o jsonpath='{.data.kubeconfig}' | base64 -d > my.kubeconfig
sudo chmod 600 ./my.kubeconfig

KUBECONFIG=./my.kubeconfig kubectl get nodes -o wide
```

The decoded server URL is `https://<cp-data-ip>:6443`. Anyone with network access to the data network (the lab host, in our case) gets a working kubeconfig.

### Fallback — SSH-tunnel from a workstation that only reaches OOB

```sh
ssh -L 6443:192.168.0.206:6443 ironic@<cp-OOB-ip>

# in another shell — rewrite the server URL in the kubeconfig:
kubectl --kubeconfig=./my.kubeconfig config set-cluster <cluster> \
  --server=https://127.0.0.1:6443 --insecure-skip-tls-verify=true
```

The TLS SAN covers the apiserver's actual IP, so the tunnel breaks SAN verification — `--insecure-skip-tls-verify` is required.

### On-blade — when you're already SSHed in for debugging

```sh
ssh ironic@<cp-OOB-ip>
sudo KUBECONFIG=/etc/kubernetes/admin.conf kubectl get nodes
```

`/root/.kube/config` is the same file (cp-init.sh `cp`'d it).

---

## Troubleshooting playbook

Order matters: top of the table = check first.

| Symptom | Where to look | Often-true root cause |
|---|---|---|
| etcd / apiserver restart counter climbing every ~11s | `grep cgroupDriver /var/lib/kubelet/config.yaml` vs `grep SystemdCgroup /etc/containerd/config.toml` on the CP | cgroup-driver mismatch. Aligned to `systemd` since v0.10.12. If you see it again, the chart's containerd config rewrite was overridden by something. |
| `KubernetesCluster` stuck `Ready: False` | `kubectl -n krateo-system logs deploy/kubernetesclusters-v0-X-X-controller` | core-provider regenerated the cdc; the ettore CR may be at an apiVersion no longer served. Re-apply the manifest with the latest `composition.krateo.io/v0-X-X`. |
| `BaremetalHost` stuck `Synced: False` "exists and cannot be imported" | `kubectl -n openstack get port,nodeprovision,nodepower` for orphans | half-failed prior helm install. Bridge reaper deletes them after 60s (v0.10.8). If reaper isn't running, restart wg pod. |
| Worker never joins, kubelet.conf missing on blade10 | `sudo tail /var/log/join.log` on blade10 | apiserver was flapping at join time (pre-v0.10.12) or worker has a stale join command (pre-v0.10.8 bridge clear-on-non-active). |
| Worker has kubelet.conf but says `connect: connection refused` | check the CP's apiserver | same etcd-flap cluster of symptoms. v0.10.12 fix should hold. |
| `<cluster>-workload-kubeconfig` Secret missing | `sudo tail /var/log/publish-workload-kubeconfig.log` on CP | SA token wasn't rendered (rbac.yaml race) — re-render BH by annotating the KubernetesCluster CR. |
| `<cluster>-workload-kubeconfig` exists but points at the wrong IP | decode it: `... | base64 -d \| grep server:` | `--apiserver-advertise-address` resolved to an unexpected NIC. Check `ip route get 8.8.8.8` on CP — should print the data network IP. |
| Bridge log spams `HTTP Error 403` | the SA's Role is missing a verb | patch `manifests/wg-ironic-proxy.yaml` Role; re-`make lab-tunnel-up`. The bridge needs `get,list,patch,delete` on `nodes,ports,nodeprovisions,nodepowers`. |
| `kubectl get bh -o yaml` shows the OLD `kubeadm_join` after a redeploy | check Ironic node.provision_state | bridge clears extra only when state is NOT `{active, deploying, wait call-back}` (v0.10.8). If clearing isn't happening, restart the wg pod. |
| New worker BH never renders even though blade06 is Ready | check Node CR | `Node.spec.extra.kubeadm_join` is the gate. Verify bridge mirrored it; if not, `kubectl -n openstack logs <wg-pod> -c ironic-extra-bridge --tail=30`. |

### Data-driven debug discipline

When a symptom survives one chart cut, the next move is **diagnostics on the live cluster, not the next chart cut**.

Concretely: SSH into the CP, run the relevant `grep`/`journalctl`/`crictl` commands, read the output. The 11-second etcd kill that started this whole journey was visible from `grep cgroupDriver` + `grep SystemdCgroup` in under a minute — we'd burned nine chart cuts speculating before doing that. Don't repeat that mistake.

---

## Related docs

- [USER-GUIDE.md](USER-GUIDE.md) — the `BaremetalHost` spec, transition-by-transition
- [E2E.md](E2E.md) — end-to-end timing breakdown + fixture manifests
- [KUBERNETES-CLUSTER-PLAN.md](KUBERNETES-CLUSTER-PLAN.md) — design rationale, milestones
- [REAL-IRONIC.md](REAL-IRONIC.md) — pointing the operator at a Keystone-protected Ironic
- [ORPHAN-RECOVERY.md](ORPHAN-RECOVERY.md) — the orphan KOG primitive story
- [BIFROST.md](BIFROST.md) — pointing the operator at a remote Bifrost
- [VS-METAL3.md](VS-METAL3.md) — comparison with Metal3
- [quickstart.md](quickstart.md) — bring up the local stack in 5 minutes
