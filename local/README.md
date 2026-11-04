# Free local test environment

A self-contained, no-cost Ironic environment for developing and testing the operator
on a laptop — no physical hardware, no PXE, no VMs, no public cloud.

## What it is

- A **kind** cluster (`ironic-kog`) where Krateo (KOG + composition) and the operator run.
- **Standalone Ironic** in namespace `openstack`, reachable in-cluster at
  `http://ironic.openstack.svc.cluster.local:6385`.
  - Image: `quay.io/airshipit/ironic:2026.1-ubuntu_noble` — the **official openstack-helm
    image** (built by OpenStack-Helm/Airship via LOCI), running the combined `ironic`
    binary (API + conductor in one process).
  - Config (`ironic.conf`): `auth_strategy=noauth`, SQLite, `rpc_transport=json-rpc`,
    `fake-hardware` drivers (all interfaces `fake`/`no-*`/`noop`), `automated_clean=false`.
    No MariaDB / RabbitMQ / Keystone.

With the fake drivers the full provision state machine runs without any real hardware:
`enroll → manage → manageable → inspect → manageable → provide → available → (set
instance_info) → active`.

An **nginx sidecar** fronts the Ironic API and injects a default
`X-OpenStack-Ironic-API-Version` header when the client doesn't send one. Standalone Ironic
returns HTTP 406 on write requests that omit the microversion, and KOG's
rest-dynamic-controller doesn't send it; the sidecar supplies a default while preserving any
client-provided version (e.g. from the provisioner Job's `openstack` CLI). The `ironic`
Service points at the sidecar.

> Note: the image is amd64-only; on Apple Silicon it runs under Docker Desktop's shared
> qemu emulation inside the kind node (validated working).

## kubeconfig isolation

The cluster lives in an **isolated kubeconfig** (`local/kubeconfig.ironic-kog`, gitignored).
Every `kubectl`/`helm` call uses `--kubeconfig local/kubeconfig.ironic-kog --context
kind-ironic-kog`, so your default `~/.kube/config` is never modified.

## Usage

```bash
make local-up          # kind + standalone Ironic + Krateo (KOG + core) + both RestDefinitions
make composition-up    # host the chart + install the CompositionDefinition (real cdc path)
make composition-demo  # create a BaremetalLifecycle instance; cdc walks node 'metal-a' -> active
make provision-demo    # alt: provision 'server01' via simulated reconciles (repeated helm upgrade)
make smoke-test        # alt: drive a fake node enroll -> active directly (no Krateo)
make ironic-forward    # expose the API at http://localhost:6385 (separate shell)
make local-down        # delete the kind cluster
```

Manual API access (after `make ironic-forward`):

```bash
curl -s -H "X-OpenStack-Ironic-API-Version: 1.81" http://localhost:6385/v1/nodes | jq
```

## Files

| File | Purpose |
|------|---------|
| `ironic.conf` | Standalone Ironic config (noauth, sqlite, fake drivers) |
| `ironic-standalone.yaml` | Namespace + Ironic Deployment (with nginx version-proxy sidecar) + Service |
| `kubeconfig.ironic-kog` | Isolated kubeconfig (generated, gitignored) |

## Pausing and resuming the `ironic-kog` management cluster

The kind cluster holds only the **control plane** (Krateo, cdc, rdc, the
RestDefinitions, the `lab-ha` composition). It does **not** hold the blades'
Ironic records: the in-cluster `ironic` Deployment is a leftover from local
testing — its config says so, it has 0 nodes, and the `ironic` Service has no
selector. `EndpointSlice/ironic-lab` points the Service at **172.19.74.1:6385**,
the real Ironic in the lab, reached over the WireGuard proxy. So pausing the
cluster cannot lose node records, and the Kubernetes cluster on the blades is
self-contained (its own VIP) and keeps running untouched.

**Pause** — stop the container, do not delete the cluster. `kind delete` would
destroy the CRDs, the CompositionDefinitions and the `lab-ha` CR, which is
hours of rebuild, not a pause:

```bash
docker stop ironic-kog-control-plane
```

Other kind clusters on this machine belong to other sessions. Only ever name
`ironic-kog-control-plane`.

**Resume:**

```bash
docker start ironic-kog-control-plane
# wait for the API, then RE-PUBLISH THE CHARTS — BOTH of these:
make chart-host          # the versions currently in Chart.yaml
make chart-host-pinned   # the versions the live CompositionDefinitions PIN
```

Both are **mandatory**, and the second is the one that is easy to miss. The
`chartrepo` nginx Deployment has **no volumes**, so everything it serves lives
in its container filesystem and is gone the moment the pod is recreated.

`make chart-host` only republishes the versions each chart has on disk *right
now*. The live CompositionDefinitions pin **older** versions — before a restart
the chartrepo has accumulated every version ever published; afterwards it has
only today's. Measured on 2026-11-04: after a restart, all four CDs pinned
versions the chartrepo no longer had, and running `chart-host` alone left every
BaremetalHost `Synced=False` with

```
observe failed: generating RBAC using chart-inspector: ... status code: 500
```

which reads as a chart-inspector fault rather than a missing file, and costs
real time to diagnose. `make chart-host-pinned` reads each CD's pinned version,
finds the commit where that chart last carried it, packages it from git and
uploads it. It skips charts served from OCI, is idempotent, and exits non-zero
if a pinned version cannot be found in history — which is itself worth knowing:
the `baremetal-discovery` CD pins 0.2.0, a version that has never existed in
this repo, so it was applied from some other source and cannot be satisfied
here. Nothing currently uses it.

### Why a paused control plane is not dangerous

Both destructive transitions are gated off while the blades sit at `active`,
verified 2026-11-04:

- `transition-deploy` requires `provisionState == "available"`. That helper
  reads the **BaremetalNode CR's** `status.provision_state` via helm `lookup`,
  not Ironic, so it keeps its last value (`active`) while nothing reconciles.
- `transition-undeploy` requires `spec.undeploy=true` **or**
  `shouldAutoRedeploy`. All six BHs have `undeploy=False`, and
  `shouldAutoRedeploy` needs a non-empty `spec.extra.deployed_configdrive_hash`
  — empty on all six, because the bridge deliberately does not mirror that key.

The dangerous case is not a pause; it is an Ironic that comes back **empty**.
Then the CRs re-enrol the nodes (Ironic honours a client-supplied uuid), the
chart walks them to `available`, and `transition-deploy` fires and re-images a
blade. That cannot happen from pausing this cluster, because the records are not
in it.
