---
type: API
title: openstack-ironic-operator-kog — API
description: The CRD contracts of this blueprint — the core-provider CompositionDefinition that registers each chart, the four generated composition CRDs (BaremetalHost, BaremetalLifecycle, BaremetalDiscovery, KubernetesCluster), and the KOG-generated Ironic primitives (Node, NodeProvision, Port, NodePower) they drive.
resource: oci://ghcr.io/krateo-blueprints/charts/baremetal-host
tags: [ironic, api, crd, compositiondefinition, kog]
timestamp: 2026-08-11T00:00:00Z
---

# API

Two CRD families matter here: the `CompositionDefinition` that registers a chart, the four
composition CRDs it generates, and the KOG-generated Ironic primitives those compositions
drive.

## CompositionDefinition (`core.krateo.io/v1alpha1`)

This is the input `core-provider` consumes. One per chart, in `manifests/`. It names a
published chart; `core-provider` generates the composition CRD from the chart's
`values.schema.json` and runs a `composition-dynamic-controller` that helm-installs the
chart per instance.

```yaml
apiVersion: core.krateo.io/v1alpha1
kind: CompositionDefinition
metadata:
  name: baremetal-lifecycle          # -> Kind BaremetalLifecycle (dashes dropped, CamelCased)
  namespace: krateo-system
spec:
  chart:
    url: oci://ghcr.io/krateo-blueprints/charts/baremetal-lifecycle
    version: "0.3.1"                  # -> apiVersion composition.krateo.io/v0-3-1
```

| field | meaning |
|---|---|
| `metadata.name` | the chart/composition name. The generated **Kind** is its PascalCase (`baremetal-lifecycle` → `BaremetalLifecycle`). |
| `spec.chart.url` | where the chart lives. GHCR OCI for released charts; an in-cluster HTTP server (`http://chartrepo.openstack.svc.cluster.local/<name>-<ver>.tgz`) for local dev. |
| `spec.chart.version` | the chart's published version. The generated **apiVersion** is `composition.krateo.io/v0-X-Y` derived from it (`0.3.1` → `v0-3-1`). |

The four definitions:

| file | name | published chart URL (released) |
|---|---|---|
| `compositiondefinition-baremetal-host.yaml` | `baremetal-host` | `oci://ghcr.io/krateo-blueprints/charts/baremetal-host` |
| `compositiondefinition-baremetal-lifecycle.yaml` | `baremetal-lifecycle` | `oci://ghcr.io/krateo-blueprints/charts/baremetal-lifecycle` |
| `compositiondefinition-baremetal-discovery.yaml` | `baremetal-discovery` | `oci://ghcr.io/krateo-blueprints/charts/baremetal-discovery` |
| `compositiondefinition-kubernetes-cluster.yaml` | `kubernetes-cluster` | `oci://ghcr.io/krateo-blueprints/charts/kubernetes-cluster` |

## The generated composition CRDs (`composition.krateo.io`)

Each `CompositionDefinition` produces one CRD. Its spec schema **is** the chart's
`values.schema.json`, so the CR fields are exactly the values documented in
[configuration](./configuration.md).

| Kind | drives | key spec fields |
|---|---|---|
| `BaremetalHost` | the whole `registering → available → provisioned` machine behind one CRD | `nodeName`, `driver`, `driver_info`, `ports`, `image`, `online`, `maintenance`, `detached`, `cleanSteps`, `undeploy`, `undeployMode`, `enableInspection`, `configDrive` |
| `BaremetalLifecycle` | provisioning-only `enroll → active` | `nodeName`, `driver`, `driver_info`, `ports`, `instance_info`, `online` |
| `BaremetalDiscovery` | discovery-only `enroll → manage → inspect` | `nodeName`, `driver`, `driver_info`, `ports` |
| `KubernetesCluster` | one `BaremetalLifecycle` per member + kubeadm bootstrap | `clusterName`, `k8sVersion`, `cni`, `image`, `controlPlane`, `network`, `ironicAuth`, `managementCluster` |

### Multi-version served CRDs

`core-provider` keeps **every prior chart version** served on the generated CRD
(`served: true`), with a `vacuum` storage version
(`x-kubernetes-preserve-unknown-fields: true`) and a conversion webhook translating on read.
This is intentional — it lets consumers pin different chart versions of the same composition.

Consequences to know:

- **Apply at a version whose schema has your fields.** Applying at an older `apiVersion`
  makes the kube-apiserver schema-prune the unknown fields **silently at write time**. The
  apply succeeds but the stored spec is incomplete, and cdc (which reads its own precise GVR)
  sees the trimmed body. A `Warning: unknown field "spec.X"` on apply/patch means the
  targeted `apiVersion` doesn't have field X — switch to a newer one.
- **Reads default to the oldest served version** (cosmetic). Plain
  `kubectl get <cr> -o yaml` returns the body translated to the first entry in
  `spec.versions[]`. To see the latest schema's view, hit the precise endpoint:
  `kubectl get --raw /apis/composition.krateo.io/v0-X-Y/namespaces/<ns>/<plural>/<name>`.
- **`helm get values <release>` showing defaults for fields you set** has the same root
  cause: the CR was written at an `apiVersion` lacking those fields. Re-apply at the right
  version; `vacuum` storage preserves what you write.

## The KOG-generated Ironic primitives (`baremetal.ogen.krateo.io/v1alpha1`)

The compositions render these; `oasgen-provider` generates them from `oas/` + the
`RestDefinition`s. They are the actual Ironic API clients.

| Kind | RestDefinition | Ironic call | notes |
|---|---|---|---|
| `Node` | `restdefinition-node.yaml` | CRUD on `/v1/nodes` | identifier is `spec.name` (Ironic resolves `/v1/nodes/{name}`). **No `update` verb** — Ironic PATCH is JSON-Patch-only and 400s a plain body. Status surfaces `uuid`, `provision_state`, `power_state`, `properties` (inspection inventory), `instance_info`, `automated_clean`. |
| `NodeProvision` | `restdefinition-provision.yaml` | `PUT /v1/nodes/{id}/states/provision` | action-only. **No `get` verb** — Observe uses findby. Creating one fires the PUT once; Ironic's `202` sets a Pending condition guarding re-firing. |
| `NodePower` | `restdefinition-power.yaml` | `PUT /v1/nodes/{id}/states/power` | each `spec.online` flip renames the CR so KOG fires exactly one PUT. |
| `Port` / `PortGroup` | `restdefinition-port.yaml` / `-portgroup.yaml` | CRUD on `/v1/ports` / `/v1/portgroups` | NICs and bonds. |
| `Allocation` / `DeployTemplate` | `restdefinition-allocation.yaml` / `-deploy-template.yaml` | node matching/binding; trait → deploy steps | supporting primitives. |

Every generated controller reads a namespace-scoped singleton `NodeConfiguration`
(`manifests/nodeconfiguration-ironic.yaml`) for the Ironic endpoint, microversion header and
auth; charts point `configurationRef.name` at it.

The state machine walks by re-rendering: cdc re-evaluates the chart's `lookup` of the Node
CR's `status.provision_state` on every reconcile and renders the single `NodeProvision` /
`NodePower` matching the current delta, pruning the previous one. See
[overview](./overview.md).
