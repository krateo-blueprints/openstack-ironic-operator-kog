<p align="center">
  <img src="docs/krateo-loves-ironic.png" alt="Krateo loves OpenStack Ironic" width="900"/>
</p>

# OpenStack Ironic + Krateo (KOG)

Provision bare-metal servers with **OpenStack Ironic** using Krateo's dynamic controllers
instead of a hand-written operator.

## What is this

There is **no Go operator** here. Ironic's bare-metal lifecycle is driven by two Krateo
layers:

- **KOG (Krateo Operator Generator)** — `oasgen-provider` reads the OpenAPI specs in `oas/`
  and the `RestDefinition`s in `manifests/`, generating CRDs (`Node`, `NodeProvision`,
  `Port`, `PortGroup`, `Allocation`, `DeployTemplate`, `NodePower`) and a
  `rest-dynamic-controller` per definition that talks straight to the Ironic REST API. The
  `Node` RestDefinition has no `update` verb on purpose (Ironic PATCH is JSON-Patch-only and
  400s); creating a `NodeProvision` fires `PUT /v1/nodes/{id}/states/provision` once (the
  Ironic `202` sets a Pending condition that guards re-firing).
- **Compositions** — `core-provider` consumes the `CompositionDefinition`s in `manifests/`
  and, for each, generates a CRD plus a `composition-dynamic-controller` (cdc) that
  helm-installs one of the four charts under `charts/`. Each chart models Ironic's
  provision-state machine as **one custom resource per state**, selected on every reconcile
  with the Helm `lookup` function. Because **cdc re-evaluates `lookup` on every reconcile**,
  the composition walks `enroll → manage → manageable → provide → available → deploy →
  active` one transition at a time — the composition *is* the orchestrator (no CLI, no
  middleware service).

The bundle ships **four charts**, versioned independently:

| chart | Kind | what it does |
|---|---|---|
| `charts/baremetal-host` | `BaremetalHost` | unified single-CRD host lifecycle (metal3-equivalent surface) |
| `charts/baremetal-lifecycle` | `BaremetalLifecycle` | provisioning-only walk `enroll → active` |
| `charts/baremetal-discovery` | `BaremetalDiscovery` | discovery-only walk `enroll → manage → inspect`, surfaces inventory |
| `charts/kubernetes-cluster` | `KubernetesCluster` | kubeadm cluster over Ironic-provisioned blades |

> 📖 **[Quickstart](docs/quickstart.md)** — install the provider and see a node appear in Ironic.

## Install

Everything runs locally on a laptop for free — no hardware, PXE, VMs, or public cloud. An
isolated `kind` cluster runs Krateo and a standalone Ironic (the official openstack-helm
image `quay.io/airshipit/ironic` with the `fake-hardware` driver, SQLite, noauth). See
[`local/README.md`](local/README.md).

```bash
make local-up        # kind + standalone Ironic + Krateo (KOG + core) + RestDefinitions
make provision-demo  # composition provisions a sample fake node -> active
make local-down      # tear down
```

All `kubectl`/`helm` calls use an isolated kubeconfig (`local/kubeconfig.ironic-kog`) and
explicit `--context kind-ironic-kog`, so your default `~/.kube/config` is never touched.

To drive a composition with the controller (the real lookup-driven flow — a single
`helm install` only advances one step):

```bash
make composition-up    # package + host all four charts, apply the CompositionDefinitions
make composition-demo  # create a BaremetalLifecycle instance; cdc walks it enroll -> active
kubectl -n openstack get node.baremetal.ogen.krateo.io metal-a \
  -o jsonpath='{.status.provision_state}'
```

For a real cluster, publish the chart (`make package-chart`, or the GHCR OCI artifact the
release workflow pushes) and set `spec.chart.url` in the matching
`manifests/compositiondefinition-*.yaml`. Against a real Ironic API, neither path needs
operator changes — only the endpoint behind the in-cluster `ironic` Service:

- **Standalone Ironic (Bifrost)** — `make bifrost-up BIFROST_URL=http://<host>:6385`
  ([docs/BIFROST.md](docs/BIFROST.md)).
- **Keystone-protected Ironic** — `make keystone-up CLOUDS_FILE=clouds.yaml OS_CLOUD=<name>`
  ([docs/REAL-IRONIC.md](docs/REAL-IRONIC.md)).

> The standalone Ironic pod includes an nginx sidecar that injects a default
> `X-OpenStack-Ironic-API-Version` header — Ironic rejects write requests without a
> microversion (HTTP 406), and the rest-dynamic-controller doesn't send one.

## Configure

Each chart's values are typed by its `values.schema.json`, and `core-provider` derives the
generated composition CRD's schema from that file. The full per-chart value surface is in
[docs/configuration.md](docs/configuration.md). The load-bearing fields:

- **Identity & BMC** — `nodeName`, `driver`, `driver_info` (ipmi/redfish keys), `ports`.
- **Provision** — `image` / `instance_info` (set to deploy `available → active`; omit to
  stop at `available`), `enableInspection`.
- **Power** — `online` (`true`/`false`/unset); each flip renames the NodePower CR so KOG
  fires the PUT once.
- **Release** — `undeploy: true` drives `active → available` (a spec field, **not** a
  deletion side-effect — `kubectl delete` runs `helm uninstall` with no re-render).
  `undeployMode` (`full`/`none`) controls the cleaning pass.

Every generated controller reads a namespace-scoped singleton `NodeConfiguration`
(`manifests/nodeconfiguration-ironic.yaml`) for the Ironic endpoint, microversion and auth;
charts point `configurationRef.name` at it.

**Apply at the right `apiVersion`.** `core-provider` keeps every prior chart version served
on the generated CRD (`served: true`, `vacuum` storage, conversion webhook on read) so
consumers can pin different chart versions. On `kubectl apply`, set `apiVersion` to a version
whose schema actually has the fields you use — an older `apiVersion` schema-prunes unknown
fields **silently at write time**. A `Warning: unknown field "spec.X"` means the targeted
version lacks field X; switch to a newer one (see [docs/api.md](docs/api.md)).

## Examples

- [examples/baremetal-lifecycle](examples/baremetal-lifecycle/README.md) — a
  `BaremetalLifecycle` CR that drives an Ironic node from `enroll` to `active`.
- `manifests/` carries example instances of every kind: `BaremetalHost`
  (`baremetalhost-blade*.yaml`), `BaremetalDiscovery` (`baremetaldiscovery-blade03.yaml`),
  `KubernetesCluster` (`kubernetescluster-*.yaml`).

## Docs

- [docs/index.md](docs/index.md) — the map of the whole bundle.
- [docs/overview.md](docs/overview.md) — the KOG + composition architecture.
- [docs/usage.md](docs/usage.md) — local env, driving compositions, real Ironic.
- [docs/configuration.md](docs/configuration.md) — the value surface of each chart.
- [docs/api.md](docs/api.md) — the CompositionDefinition CRD and the generated CRDs.
- [docs/examples.md](docs/examples.md) — the runnable examples.
- [docs/release.md](docs/release.md) — how the four charts are published.
- [docs/log.md](docs/log.md) — curated history.

Deep-dive references: [KUBERNETES-CLUSTER.md](docs/KUBERNETES-CLUSTER.md),
[USER-GUIDE.md](docs/USER-GUIDE.md), [VS-METAL3.md](docs/VS-METAL3.md),
[E2E.md](docs/E2E.md), [ORPHAN-RECOVERY.md](docs/ORPHAN-RECOVERY.md),
[RUNBOOK-ETCD-RESTORE.md](docs/RUNBOOK-ETCD-RESTORE.md).

## Develop & release

Project layout:

| Path | Description |
|------|-------------|
| `oas/` | OpenAPI specs KOG consumes (Node, Provision, Port, Portgroup, Allocation, Deploy Template) |
| `manifests/` | `RestDefinition`s, `CompositionDefinition`s, example CRs |
| `charts/` | the four composition Helm charts |
| `local/` | free local env (kind config, standalone Ironic, kubeconfig isolation) |
| `deploy/` | openstack-helm Ironic deployment (full stack, for real clusters) |
| `kagent/` | the Ironic-KOG expert agent (kagent) |
| `scripts/` | OAS ConfigMap creation, proxies, smoke test |

Makefile targets — local env: `local-up`, `krateo-up`, `restdef-up`, `ironic-up`,
`provision-demo`, `composition-up`, `composition-demo`, `local-down` (run `make help`).
Chart/packaging: `package-chart`, `template-chart`, `validate-chart`.

The four charts version independently (each carries its own `version` in `Chart.yaml`) but
are published together by `.github/workflows/release-chart.yaml` on any plain-SemVer tag
(`X.Y.Z`, **no** `v` prefix): it lints, packages and pushes all four to
`oci://ghcr.io/krateo-blueprints/charts/<name>`. Full flow in [docs/release.md](docs/release.md).

```bash
git tag X.Y.Z && git push origin X.Y.Z
```

CI: `security.yml` runs the shared `krateo-platformops/.github` security workflow, and
`lint.yaml` runs the shared docs-standard linter on every PR.
