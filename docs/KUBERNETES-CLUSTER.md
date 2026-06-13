# `kubernetes-cluster` composition

Provision a real Kubernetes cluster on Ironic-managed bare-metal nodes by applying **one** Custom Resource. No Go operator, no extra controllers — just a layered Krateo composition that decomposes into well-known primitives.

```
KubernetesCluster CR        ← what the user applies
        │
        │ kubernetes-cluster chart renders
        ▼
BaremetalHost CR  (one per blade)
        │
        │ baremetal-host chart renders
        ▼
Node + Port + NodeProvision + NodePower  (KOG primitives)
        │
        │ KOG rest-dynamic-controller calls
        ▼
Ironic REST API
```

## The four-layer composition stack

Every box in the diagram above is a Helm chart that ships its own [`CompositionDefinition`](https://docs.krateo.io/key-concepts/composition-definition/). Krateo's `composition-dynamic-controller` (cdc) turns each CR into a Helm release at the next layer down.

| Layer | Chart | CRD | What 1 CR represents |
|---|---|---|---|
| L4 (top) | `kubernetes-cluster` | `KubernetesCluster` | a whole k8s cluster (1 bootstrap CP + N replicas + M workers) |
| L3 | `baremetal-host` | `BaremetalHost` | one bare-metal blade managed by Ironic |
| L3 (sibling) | `baremetal-lifecycle` | `BaremetalLifecycle` | one blade driven `enroll → active` (no ports, no rebuild lifecycle) |
| L3 (sibling) | `baremetal-discovery` | `BaremetalDiscovery` | inventory pass — `enroll → manageable → inspecting` then read CPUs / RAM / NICs into status |
| L2 | (KOG primitives) | `Node` / `Port` / `NodeProvision` / `NodePower` | one Ironic-API REST call (`POST /v1/nodes`, `PUT /v1/nodes/{id}/states/provision`, …) |
| L1 | (Ironic) | — | the actual hardware |

`kubernetes-cluster` only renders `BaremetalHost` directly. `baremetal-lifecycle` and `baremetal-discovery` are sibling compositions that solve the same problem at different scopes — useful when you want a one-off lifecycle or a discovery pass without committing to a full cluster.

### L2 — KOG primitives (the "ironic-operator-kog" layer)

The lowest layer is generated from Ironic's OpenAPI spec by Krateo's `oasgen-provider`. From the spec we declare `RestDefinition` CRs (`manifests/restdefinition-*.yaml`); KOG generates one CRD + one `rest-dynamic-controller` (rdc) per resource:

| CRD | Backed by | Maps to |
|---|---|---|
| `nodes.baremetal.ogen.krateo.io` | `ironic-node-controller` | CRUD on `/v1/nodes` |
| `ports.baremetal.ogen.krateo.io` | `ironic-port-controller` | CRUD on `/v1/ports` |
| `nodeprovisions.baremetal.ogen.krateo.io` | `ironic-node-provision-controller` | one `PUT /v1/nodes/{id}/states/provision` (fires once per CR) |
| `nodepowers.baremetal.ogen.krateo.io` | `ironic-node-power-controller` | one `PUT /v1/nodes/{id}/states/power` (fires once per CR) |
| `portgroups`, `allocations`, `deploytemplates` | (generated) | the corresponding Ironic resources |

Two endpoint singletons resolve **how** to call Ironic:

- `NodeConfiguration` — Ironic API URL + microversion the `Node` rdc uses
- `PortConfiguration` — same, for the `Port` rdc

Both are applied once per cluster by `make restdef-up` and referenced from the higher-layer charts as `configurationRef`.

### L3 — the three blade compositions

These three Krateo blueprints all sit on top of the same KOG primitives but cover different lifecycles. Each is a stateless template — the `composition-dynamic-controller` re-renders on every reconcile and the right transition CR is rendered when its gates match live Ironic state.

#### `baremetal-host`

The most complete blade composition. One `BaremetalHost` CR → a `Node` + `Port`(s) + a per-state `NodeProvision`/`NodePower`/transition CR. Covers the full state machine:

```
enroll → verifying → manageable → cleaning → available
                                              │
                                              │ deploy
                                              ▼
                         deploying → wait call-back → active
                                                       │
                                                       │ undeploy
                                                       ▼
                                                  available
```

Spec covers: identity + BMC (`driver`, `driver_info`), `ports`, optional inspection, `image`, `configDrive` (metadata + userData + networkData), `undeploy`, `online`, `maintenance`, `cleanSteps`. See [USER-GUIDE.md](USER-GUIDE.md) for the full spec walk-through.

This is what `kubernetes-cluster` renders for every blade.

#### `baremetal-lifecycle`

Smaller, focused on the `enroll → active` happy path. No ports management, no rebuild, no clean steps. Use it when you want a one-shot provisioning step without the full BaremetalHost surface area. Not used by `kubernetes-cluster`.

#### `baremetal-discovery`

A discovery pass — drives `enroll → manageable → inspecting → manageable` and surfaces inventory (`cpus`, `memory_mb`, `disks`, `nics`, capabilities) in the `Node` CR's status. The output is meant to be **input** for `baremetal-lifecycle` or `baremetal-host`: a higher-level operator reads inventory and decides which blade gets a lifecycle spec. No deploy, no ports.

### L4 — `kubernetes-cluster`

Takes a logical cluster spec and emits one `BaremetalHost` per node, gated correctly so the state machine plays out in the right order:

| Template | Renders when |
|---|---|
| `lifecycle-cp.yaml` | always (bootstrap CP — index 0 of `controlPlane.nodes[]`) |
| `lifecycle-cp-replicas.yaml` | join command and cert-key both present in bootstrap CP's `Node.spec.extra` |
| `lifecycle-cp-recovery.yaml` | nodeName listed in `spec.controlPlane.recovery.failedNodes` |
| `lifecycle-workers.yaml` | join command present in bootstrap CP's `Node.spec.extra` |
| `drain-jobs.yaml` | worker entry moved from `workers.nodes[]` to `workers.removed[]` |
| `rbac.yaml` | always — minimal SA + Role used by the CP's cloud-init to patch its own Node CR and create the workload-kubeconfig Secret |

Bootstrap behaviour is encoded directly in the rendered `BaremetalHost.spec.configDrive.userData` (the chart's `cpUserData` / `workerUserData` / `cpReplicaUserData` helpers):

- `install-k8s.sh` — apt install of `kubeadm`/`kubelet`/`kubectl`/`containerd`, containerd config rewrite (`bin_dir=/opt/cni/bin`, `SystemdCgroup=true`), `iptables` switched to legacy, sysctls + br_netfilter loaded
- `cp-init.sh` — `openssl rand` cert-key, `kubeadm init --apiserver-advertise-address=$(ip route get 8.8.8.8 | awk '/src/ {print $7}')`, `kubectl apply` flannel
- `publish-join.sh` — kubeadm token create + `PATCH` Ironic `node.extra.kubeadm_join` + `cert_key`, retried on transient Keystone / Ironic failures, refreshed every 12h via systemd timer
- `join.sh` — workers and replica CPs run `kubeadm join` with 30 × 20s retry

The rendezvous between CP and workers/replicas relies on two side channels:

- **Ironic `node.extra` as a publish-board** — the CP's cloud-init `PATCH`es its own join command into Ironic; this is OOB-network-reachable from the blade even when the management API isn't.
- **`ironic-extra-bridge` sidecar** in the `wg-ironic-proxy` pod — polls Ironic and mirrors `extra.kubeadm_join` + `extra.cert_key` into the Node CR's `spec.extra` so the chart's `lookup` resolves and the worker BH renders. The same sidecar also reaps orphan KOG primitives whose `meta.helm.sh/release-name` annotation no longer matches any active helm release secret.

## Bringing it up

```sh
# 1. kind + Krateo + WG/proxy + RestDefinitions
make KIND_CLUSTER=ironic-lab lab-up

# 2. all 4 charts → in-cluster chartrepo + all 4 CompositionDefinitions
make KIND_CLUSTER=ironic-lab composition-up

# 3. one CR creates the whole cluster
kubectl apply -f manifests/kubernetescluster-ettore-lab.yaml
```

Expected duration on a 1-CP + 1-worker setup against the Ettore lab (Debian 13 trixie, k8s 1.36.2): **≈ 24 minutes** end-to-end, zero pod restarts. See [E2E.md](E2E.md) for the timing breakdown.

## `KubernetesCluster` spec — one section at a time

### Identity + image

```yaml
spec:
  clusterName: ettore
  k8sVersion: v1.36.2          # any v1.30+ that pkgs.k8s.io publishes
  image:
    source: http://172.19.74.1:8089/debian-13-genericcloud-amd64.qcow2
    checksum: http://172.19.74.1:8089/CHECKSUM
```

### CNI + network

```yaml
spec:
  cni:
    install: flannel
    manifestUrl: https://github.com/flannel-io/flannel/releases/latest/download/kube-flannel.yml
  network:
    podCIDR: 10.244.0.0/16
    serviceCIDR: 10.96.0.0/12
    managementApiReachability: nodeport-dns   # how blades reach the mgmt cluster API
```

### Ironic endpoint + auth

`ironicAuth` is what the CP's `publish-join.sh` uses to `PATCH` Ironic over its OOB network. `configurationRef` is the singleton `NodeConfiguration` applied by `make restdef-up` — it tells the KOG `Node` controller which Ironic to talk to.

```yaml
spec:
  ironicApiUrl: http://ironic.openstack.svc.cluster.local:6385
  ironicApiVersion: "1.81"
  ironicAuth:
    authUrl: http://172.19.74.1:5000
    ironicUrl: http://172.19.74.1:6385
    username: admin
    password: <password>
    userDomain: Default
  configurationRef:
    name: ironic-endpoint
    namespace: openstack
```

### Management-cluster ingress

The CP's cloud-init must reach **back** into the management cluster to (a) patch its own Node CR and (b) publish the workload kubeconfig as a Secret. `managementCluster.apiUrl` is what the blade calls; the SA that signs those calls is created by the chart's `rbac.yaml`.

```yaml
spec:
  managementCluster:
    apiUrl: http://198.51.100.5:8443          # the wg-pod's kubectl-proxy sidecar
    serviceAccountName: ettore-cp-publisher
    serviceAccountNamespace: openstack
```

### Control plane

```yaml
spec:
  controlPlane:
    endpoint: ""                               # set for HA (load-balancer hostname)
    node:                                      # bootstrap CP — index 0
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
      targetNode: ""                           # set to a CP nodeName to re-image it
    recovery:
      failedNodes: []                          # nodeNames to mark for chart-side recovery
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
    removed: []                                # move an entry here to drain + undeploy
```

## Day-2 operations

| Goal | What to change |
|---|---|
| Add a worker | append to `spec.workers.nodes[]` |
| Drain + remove a worker | move entry from `workers.nodes[]` to `workers.removed[]` (`drain-jobs.yaml` runs `kubectl drain` against the workload kubeconfig, then the BH gets `undeploy: true`) |
| Add an HA replica CP | append to `spec.controlPlane.nodes[]` |
| Reimage a CP for a k8s patch upgrade | set `spec.controlPlane.upgrade.targetNode` to the CP's nodeName; clear once Ironic walks it back to available |
| Recover a failed CP | add nodeName to `spec.controlPlane.recovery.failedNodes` |
| Refresh kubeadm join token | nothing — the CP's `kubeadm-token-refresh.timer` re-runs `publish-join.sh` every 12h |

## Accessing the provisioned cluster

The cluster's apiserver lives on the **bootstrap CP**, listening on its default-route source IP (the data network) at port 6443. Three ways to reach it:

### Direct, from anywhere that routes to the data network (recommended)

The blade is named exactly like the Ironic node (e.g. `blade06`). Pull its kubeconfig and use it:

```sh
# from a workstation that can route to 192.168.0.0/24
scp ironic@<blade06-OOB-ip>:/etc/kubernetes/admin.conf ./ettore.kubeconfig
# fix the user account perm (Debian writes 0600 owned by root) — adjust:
sudo chmod 600 ./ettore.kubeconfig
KUBECONFIG=./ettore.kubeconfig kubectl get nodes -o wide
```

`admin.conf` already points at `https://<data-IP>:6443` (e.g. `192.168.0.206:6443`) which kubeadm pinned via `--apiserver-advertise-address`. **For Ettore on the lab this is the simplest path** — both `172.19.74.0/24` (OOB) and `192.168.0.0/24` (data) are reachable from the lab host.

### SSH-tunnelled, when you can only reach the OOB network

```sh
# tunnel local 6443 → blade06's apiserver
ssh -L 6443:192.168.0.206:6443 ironic@<blade06-OOB-ip>

# rewrite the kubeconfig server URL once, in another shell
kubectl --kubeconfig=./ettore.kubeconfig config set-cluster <cluster> \
  --server=https://127.0.0.1:6443 --insecure-skip-tls-verify=true
```

The TLS SAN on the apiserver cert covers the IP it was advertised on; the tunnel breaks SAN verification so `--insecure-skip-tls-verify` is required for this shortcut.

### From inside the lab cluster itself, by SSH

```sh
ssh ironic@<blade06-OOB-ip>
sudo KUBECONFIG=/etc/kubernetes/admin.conf kubectl get nodes
```

`/root/.kube/config` is the same file — `cp-init.sh` mints it from `/etc/kubernetes/admin.conf` so `sudo kubectl` Just Works on the CP.

## Where to look when something breaks

| Symptom | First place to read |
|---|---|
| `KubernetesCluster` stuck `Ready: False` | `kubectl -n krateo-system logs deploy/kubernetesclusters-vX-XX-XX-controller` |
| `BaremetalHost` stuck `Synced: False` with "exists and cannot be imported" | orphan KOG primitives — the bridge reaper handles this within 60s, see [ORPHAN-RECOVERY.md](ORPHAN-RECOVERY.md) |
| Worker never joins | SSH into blade06 (`172.19.74.X`), `sudo tail /var/log/cp-init.log` and `/var/log/publish-join.log`; SSH into the worker (`172.19.74.Y`) and `sudo tail /var/log/join.log` (30 × 20s retry log) |
| etcd / apiserver flapping every ~11s | cgroup-driver mismatch — `grep cgroupDriver /var/lib/kubelet/config.yaml` and `grep SystemdCgroup /etc/containerd/config.toml` MUST agree. The chart aligns both to `systemd` since v0.10.12 |
| Bridge log floods `HTTP Error 403` | the SA's Role is missing a verb — patch `manifests/wg-ironic-proxy.yaml` |
| `kubectl get bh -o yaml` shows the OLD `kubeadm_join` after a redeploy | the chart's bridge clears `extra.kubeadm_join` and `extra.cert_key` from both Ironic and the Node CR while the node is NOT in `{active, deploying, wait call-back}` (since v0.10.8) — verify the bridge sidecar is reporting `cleared Ironic.extra keys` |

## Related docs

- [USER-GUIDE.md](USER-GUIDE.md) — the `BaremetalHost` spec, transition-by-transition
- [E2E.md](E2E.md) — end-to-end timing, fixture manifests
- [KUBERNETES-CLUSTER-PLAN.md](KUBERNETES-CLUSTER-PLAN.md) — design rationale and milestone breakdown
- [REAL-IRONIC.md](REAL-IRONIC.md) — how to point the operator at a real, Keystone-protected Ironic
- [ORPHAN-RECOVERY.md](ORPHAN-RECOVERY.md) — the orphan KOG primitive cleanup story
