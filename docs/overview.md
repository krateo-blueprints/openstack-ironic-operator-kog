---
type: Architecture
title: openstack-ironic-operator-kog — overview
description: How the blueprint is built — the KOG-generated Ironic primitives, the lookup-driven per-state composition that walks Ironic's provision FSM, and how the four charts (host, lifecycle, discovery, cluster) layer on those primitives.
resource: oci://ghcr.io/krateo-blueprints/charts/baremetal-host
tags: [ironic, kog, composition, state-machine, architecture]
timestamp: 2026-08-11T00:00:00Z
---

# Overview

There is **no Go operator** in this repository. Ironic's bare-metal lifecycle is driven by
two Krateo layers stacked on top of each other.

## Layer 1 — the KOG primitives

`oasgen-provider` reads the OpenAPI specs in `oas/` and the `RestDefinition`s in
`manifests/`, and for each one generates a CRD plus a `rest-dynamic-controller` (RDC) that
performs CRUD against the Ironic REST API. Two are load-bearing for the state machine:

- **`Node`** → CRUD on `/v1/nodes` (`manifests/restdefinition-node.yaml`). It has no
  `update` verb on purpose: Ironic's PATCH is JSON-Patch-only and 400s a plain body, so
  changes flow through the JSON-Patch translator rather than a naive PUT/PATCH.
- **`NodeProvision`** → the provision action `PUT /v1/nodes/{id}/states/provision`
  (`manifests/restdefinition-provision.yaml`). Creating a `NodeProvision` fires the PUT
  once; the Ironic `202` sets a Pending condition that guards against re-firing.

Supporting primitives — `Port` / `PortGroup` (NICs and bonds), `Allocation` (node
matching/binding), `DeployTemplate` (trait → deploy steps) and `NodePower`
(`PUT /states/power`) — are generated the same way from the sibling specs and definitions.
A namespace-scoped singleton `NodeConfiguration` (`manifests/nodeconfiguration-ironic.yaml`)
holds the Ironic endpoint and auth that every generated controller reads.

## Layer 2 — the composition is the orchestrator

`core-provider` consumes a `CompositionDefinition` (`core.krateo.io/v1alpha1`) and generates
a composition CRD plus a `composition-dynamic-controller` (cdc) that helm-installs the
referenced chart per instance. The clever part is in the chart: the Ironic provision-state
machine is modeled as **one custom resource per state**. The chart's templates use the Helm
`lookup` function to read the live `Node` CR's `status.provision_state`, and render the
single `NodeProvision` (or `NodePower`) that matches the current desire-vs-observed delta,
pruning the previous one.

Because **cdc re-evaluates `lookup` on every reconcile**, the composition walks the machine
one transition at a time:

```
enroll → manage → manageable → (inspect)? → provide → available → deploy → active
```

and, on release, `active → deleted → cleaning → available`. A single `helm install` only
advances one step; the walk needs the controller's repeated reconciles. Progression is
gated by KOG's Node-controller status resync (~tens of seconds per state), so a full
enroll→active walk against the fake driver takes a few minutes.

## The four charts

The primitives are shared; the charts differ only in which slice of the machine they drive.

| chart | Kind | slice of the machine |
|---|---|---|
| `baremetal-host` | `BaremetalHost` | the whole thing behind one CRD: `registering → (inspecting → ready)? → available → (provisioning → provisioned)`, plus power (`spec.online`), maintenance, detached, clean steps and undeploy |
| `baremetal-lifecycle` | `BaremetalLifecycle` | provisioning-only: `enroll → active`. Inspection is out of scope. |
| `baremetal-discovery` | `BaremetalDiscovery` | discovery-only: `enroll → manage → inspect`, surfacing inventory (cpus/memory/disks/NICs) in the Node CR's status as **input** to a lifecycle spec. No ports/deploy. |
| `kubernetes-cluster` | `KubernetesCluster` | renders one `BaremetalLifecycle` per cluster member and gates the `kubeadm join` sequence on the control plane publishing its join command into its own Node CR's `spec.extra` |

`baremetal-host` is the unified successor that absorbs the discovery + lifecycle split into
a single metal3-shaped `BaremetalHost`; the split charts remain for callers that want the
narrower surface. See [VS-METAL3.md](./VS-METAL3.md) for the metal3 comparison and
[USER-GUIDE.md](./USER-GUIDE.md) for the BaremetalHost user guide.

## Delete vs undeploy — the one asymmetry to know

cdc's reconcile loop has two paths and they are not symmetric:

- **Install / upgrade** (CR spec changes): cdc calls `helm upgrade`, the chart re-renders
  against live `lookup` state, and the composition drives the next transition. Every
  `transition-*.yaml` works this way.
- **Delete** (the CR has a `deletionTimestamp`): cdc calls `helm uninstall` directly, with
  **no** `helm upgrade` re-render.

So `kubectl delete` cannot drive an Ironic state walk — nothing is being re-rendered.
Releasing a blade back to `available` is therefore a **spec field** (`spec.undeploy: true`),
which stays on the same upgrade-on-reconcile rail every other transition uses, rather than a
deletion side-effect that would need a Helm pre-delete hook. Always undeploy first, then
delete: deleting an `active` node leaves the k8s CR gone while Ironic keeps running the
blade (KOG's `DELETE /v1/nodes/{id}` 409s while active). See
[ORPHAN-RECOVERY.md](./ORPHAN-RECOVERY.md) for the desync recovery path.

## Multi-version compositions

`core-provider` keeps every prior chart version served on the generated CRD, so multiple
consumers can pin different chart versions of the same composition. The generated CRD marks
all of them `served: true` with a `vacuum` storage version
(`x-kubernetes-preserve-unknown-fields: true`) and a conversion webhook translating on read.
This is intentional. The consequence — the one rule that catches everyone — is that when you
`kubectl apply`, the `apiVersion` you target must be a version whose schema actually has the
fields you set, or the kube-apiserver schema-prunes the unknown ones silently at write time.
See [usage](./usage.md) and [api](./api.md).

## Free local test environment

Everything runs on a laptop for free — no hardware, PXE, VMs, or public cloud. An isolated
`kind` cluster runs Krateo and a standalone Ironic (the official openstack-helm image
`quay.io/airshipit/ironic` with the `fake-hardware` driver, SQLite, noauth). The standalone
Ironic pod includes an nginx sidecar that injects a default
`X-OpenStack-Ironic-API-Version` header, because Ironic rejects write requests without a
microversion (HTTP 406) and the RDC does not send one. See [usage](./usage.md).
