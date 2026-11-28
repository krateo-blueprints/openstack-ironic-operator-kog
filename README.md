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
  and the `RestDefinition`s in `manifests/`, generating CRDs (`BaremetalNode`, `NodeProvision`,
  `Port`, `PortGroup`, `Allocation`, `DeployTemplate`, `NodePower`) and a
  `rest-dynamic-controller` per definition that talks straight to the Ironic REST API. The
  `BaremetalNode` RestDefinition has no `update` verb on purpose (Ironic PATCH is JSON-Patch-only and
  400s); creating a `NodeProvision` fires `PUT /v1/nodes/{id}/states/provision` once (the
  Ironic `202` sets a Pending condition that guards re-firing).
  (Corrected 2026-11-16: both of those said `Node`. The served kind is
  `BaremetalNode`. `docs/REAL-IRONIC.md` corrected this same error on 2026-11-05 and
  `docs/index.md` says outright that a `Node` kind does not exist -- yet the stale name
  still survived in **ten** live places across seven files, the API reference and the
  docs overview among them. All ten are fixed together, because fixing the copy in
  front of you and reporting it done is how it survived this long.)
- **Compositions** — `core-provider` consumes the `CompositionDefinition`s in `manifests/`
  and, for each, generates a CRD plus a `composition-dynamic-controller` (cdc) that
  helm-installs one of the two charts under `charts/`. Each chart models Ironic's
  provision-state machine as **one custom resource per state**, selected on every reconcile
  with the Helm `lookup` function. Because **cdc re-evaluates `lookup` on every reconcile**,
  the composition walks `enroll → manage → manageable → provide → available → deploy →
  active` one transition at a time — the composition *is* the orchestrator (no CLI, no
  middleware service).

The bundle ships **two charts**, versioned independently:

| chart | Kind | what it does |
|---|---|---|
| `charts/baremetal-host` | `BaremetalHost` | unified single-CRD host lifecycle (metal3-equivalent surface) |
| `charts/kubernetes-cluster` | `KubernetesCluster` | kubeadm cluster over Ironic-provisioned blades |

> 📖 **[Quickstart](docs/quickstart.md)** — install the provider and see a node appear in Ironic.

## Install

Everything runs locally on a laptop for free — no hardware, PXE, VMs, or public cloud. An
isolated `kind` cluster runs Krateo and a standalone Ironic (the official openstack-helm
image `quay.io/airshipit/ironic` with the `fake-hardware` driver, SQLite, noauth). See
[`local/README.md`](local/README.md).

```bash
make local-up        # kind + standalone Ironic + Krateo (KOG + core) + RestDefinitions
make composition-up  # package + host the charts, apply the CompositionDefinitions
make composition-demo # cdc walks a fake-hardware node -> active
make local-down      # tear down (refuses on a cluster named ironic-kog;
                     #            pass I_MEAN_IT=1 if it is yours to delete)
```

All `kubectl`/`helm` calls use an isolated kubeconfig (`local/kubeconfig.ironic-kog`) and
explicit `--context kind-ironic-kog`, so your default `~/.kube/config` is never touched.

To drive a composition with the controller (the real lookup-driven flow — a single
`helm install` only advances one step):

```bash
make composition-up    # package + host both charts, apply the CompositionDefinitions
make composition-demo  # create a fake-hardware BaremetalHost; cdc walks it enroll -> active
kubectl -n openstack get node.baremetal.ogen.krateo.io metal-a \
  -o jsonpath='{.status.provision_state}'
```

For a real cluster, publish the chart (the GHCR OCI artifact the release workflow pushes,
or `helm package` locally) and set `spec.chart.url` in the matching
`manifests/compositiondefinition-*.yaml`. Against a real Ironic API, neither path needs
operator changes — only the endpoint behind the in-cluster `ironic` Service:

- **Standalone Ironic (Bifrost)** — `make bifrost-up BIFROST_URL=http://<host>:6385`
  ([docs/BIFROST.md](docs/BIFROST.md)).
- **Keystone-protected Ironic** — declare `spec.authentication.apiKey.tokenRef` on the Configuration CR (no proxy; the `keystone-token-refresher` CronJob keeps the token current)
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

- [examples/baremetal-host](examples/baremetal-host/README.md) — a `BaremetalHost` CR that
  drives an Ironic node from `enroll` through inspection to `active`, with the BMC password
  taken from a Secret rather than written into `driver_info`.
- `manifests/baremetalhost-fake-demo.yaml` — a `BaremetalHost` that cdc walks from
  `enroll` to `active` against the local standalone Ironic, with no hardware. This is
  what `make composition-demo` applies.
- `manifests/` carries example instances of every kind: `BaremetalHost`
  (`baremetalhost-blade*.yaml`, addressing real lab blades) and `KubernetesCluster`
  (`kubernetescluster-*.yaml`).

## Marketplace blueprints

Both charts here are this repository's Krateo Marketplace blueprints, and
**both are live in the
[`blueprints` channel](https://krateo-blueprints.github.io/charts/blueprints)**
as of release `0.3.2` on 2026-11-07 — measured at `http=200` from the channel's
own index, which had not moved since 2026-10-09
([docs/MARKETPLACE.md](docs/MARKETPLACE.md) records the before/after and the two
failed tags it took):

| Chart | Card | What it does |
|---|---|---|
| [`kubernetes-cluster`](charts/kubernetes-cluster/README.md) | Kubernetes on Bare Metal | turns a set of blades into a conformant upstream Kubernetes cluster |
| [`baremetal-host`](charts/baremetal-host/README.md) | Bare-Metal Host (OpenStack Ironic) | manages one physical server through Ironic as a single resource |

Register `baremetal-host` **first** — `kubernetes-cluster` renders its resources.

`baremetal-discovery` and `baremetal-lifecycle`, the superseded split that
`baremetal-host` absorbed, were **retired on 2026-11-25**. Both were
`deprecated: true`; both rendered a `kind: Node` that no RestDefinition here declares,
so their output could never be applied at all; and neither carried the in-chart
`compositiondefinition.yaml` the channel gate requires. Use `baremetal-host` with
`enableInspection=true`, which does what the split did.

One caveat a reader needs: a published chart version is immutable and an index entry is
never removed, so retiring a chart does not withdraw what it already shipped.
**`baremetal-lifecycle` 0.3.1 is still served in the `blueprints` channel** — published
from here on or before 2026-08-19, carrying no `home` and no `sources` — and nothing can
take it out. Treat that entry as abandoned, not current.

Measured against the live index on **2026-11-25** (this supersedes a 2026-11-07 reading
that said 0.3.1 was the only entry from this repo — true then, false now, which is why
it carries its date): `kubernetes-cluster` is served at 0.14.0, 0.15.0, 0.15.1, 0.15.3,
0.15.5, 0.16.0 and 0.16.1; `baremetal-host` at 0.6.3, 0.6.5 and 0.7.0; `baremetal-lifecycle`
at 0.3.1 only; `baremetal-discovery` is absent entirely. So the two cards are there now,
beside the abandoned 0.3.1.

See [docs/MARKETPLACE.md](docs/MARKETPLACE.md) for how a chart becomes a card,
what the channel gate checks, and the one-time token setup the publish needs.
`make check-blueprints` reproduces the gate locally.

## Docs

- [docs/index.md](docs/index.md) — the map of the whole bundle.
- [docs/overview.md](docs/overview.md) — the KOG + composition architecture.
- [docs/usage.md](docs/usage.md) — local env, driving compositions, real Ironic.
- [docs/configuration.md](docs/configuration.md) — the value surface of each chart.
- [docs/api.md](docs/api.md) — the CompositionDefinition CRD and the generated CRDs.
- [docs/examples.md](docs/examples.md) — the runnable examples.
- [docs/release.md](docs/release.md) — how the two charts are published.
- [docs/MARKETPLACE.md](docs/MARKETPLACE.md) — the two Marketplace blueprints.
- [docs/log.md](docs/log.md) — curated history.

Deep-dive references: [KUBERNETES-CLUSTER.md](docs/KUBERNETES-CLUSTER.md),
[USER-GUIDE.md](docs/USER-GUIDE.md), [VS-METAL3.md](docs/VS-METAL3.md),
[E2E.md](docs/E2E.md), [ORPHAN-RECOVERY.md](docs/ORPHAN-RECOVERY.md),
[RUNBOOK-ETCD-RESTORE.md](docs/RUNBOOK-ETCD-RESTORE.md),
[DEMO.md](docs/DEMO.md).

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
`composition-up`, `composition-demo`, `local-down` (run `make help`).
Chart: `validate-chart`. (`provision-demo`, `deploy-chart`, `package-chart` and
`template-chart` were removed on 2026-11-25 with the charts they drove.)

The two charts version independently (each carries its own `version` in `Chart.yaml`) but
are published together by `.github/workflows/release-chart.yaml` on any plain-SemVer tag
(`X.Y.Z`, **no** `v` prefix): it lints, packages and pushes both to
`oci://ghcr.io/krateo-blueprints/charts/<name>`. Full flow in [docs/release.md](docs/release.md).

```bash
git tag X.Y.Z && git push origin X.Y.Z
```

CI: `security.yml` runs the shared `krateo-platformops/.github` security workflow, and
`lint.yaml` runs the shared docs-standard linter on every PR.
