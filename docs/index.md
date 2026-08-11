---
type: ChartRepo
title: openstack-ironic-operator-kog — index
description: The map of the OpenStack Ironic KOG blueprint bundle — four Krateo compositions (baremetal-host, baremetal-lifecycle, baremetal-discovery, kubernetes-cluster) that drive Ironic's bare-metal lifecycle through the Ironic REST API with no hand-written operator.
resource: oci://ghcr.io/krateo-blueprints/charts/baremetal-host
tags: [ironic, bare-metal, openstack, krateo, kog, composition]
timestamp: 2026-08-11T00:00:00Z
---

# openstack-ironic-operator-kog

This repository provisions bare-metal servers with **OpenStack Ironic** using Krateo's
dynamic controllers instead of a hand-written Go operator. Two halves make it work:

- **KOG (Krateo Operator Generator)** — `oasgen-provider` consumes the OpenAPI specs in
  `oas/` and the `RestDefinition`s in `manifests/`, generating CRDs (`Node`,
  `NodeProvision`, `Port`, `PortGroup`, `Allocation`, `DeployTemplate`) and a
  `rest-dynamic-controller` per definition that talks straight to the Ironic REST API.
- **Compositions** — `core-provider` consumes the `CompositionDefinition`s in `manifests/`
  and, for each, generates a CRD plus a `composition-dynamic-controller` (cdc) that
  helm-installs one of the four charts under `charts/` per instance. Each chart models
  Ironic's provision-state machine as *one custom resource per state*, selected on every
  reconcile with the Helm `lookup` function — so the composition itself is the orchestrator
  (no CLI, no middleware service).

The bundle ships **four charts**, versioned independently:

| chart | Kind | what it does |
|---|---|---|
| `charts/baremetal-host` | `BaremetalHost` | unified single-CRD host lifecycle (metal3-equivalent surface) |
| `charts/baremetal-lifecycle` | `BaremetalLifecycle` | provisioning-only walk `enroll → active` |
| `charts/baremetal-discovery` | `BaremetalDiscovery` | discovery-only walk `enroll → manage → inspect`, surfaces inventory |
| `charts/kubernetes-cluster` | `KubernetesCluster` | kubeadm cluster over Ironic-provisioned blades |

## The bundle (start here)

- [overview](./overview.md) — the KOG + composition architecture, the lookup-driven state
  machine, and how the four charts relate.
- [usage](./usage.md) — the free local env, driving a composition with cdc, running against
  a real Ironic API.
- [configuration](./configuration.md) — the value surface of each chart.
- [api](./api.md) — the `CompositionDefinition` CRD and the four generated composition CRDs.
- [examples](./examples.md) — the runnable example under `examples/`.
- [release](./release.md) — how the four charts are published to GHCR.
- [log](./log.md) — curated history.
- [llms.txt](./llms.txt) — the file index of this bundle.

## Deep-dive references

The design and runbook docs alongside the core bundle:

- [KUBERNETES-CLUSTER.md](./KUBERNETES-CLUSTER.md) — the layered kubernetes-cluster composition.
- [USER-GUIDE.md](./USER-GUIDE.md) — the BaremetalHost composition user guide.
- [VS-METAL3.md](./VS-METAL3.md) — comparison against metal3's baremetal-operator.
- [BIFROST.md](./BIFROST.md) / [REAL-IRONIC.md](./REAL-IRONIC.md) — running against a real Ironic.
- [E2E.md](./E2E.md) / [TEST-PLAN.md](./TEST-PLAN.md) — validation.
- [ORPHAN-RECOVERY.md](./ORPHAN-RECOVERY.md) / [RUNBOOK-ETCD-RESTORE.md](./RUNBOOK-ETCD-RESTORE.md) — recovery runbooks.

## Layout

- `oas/` — the OpenAPI specs KOG consumes (Node, Provision, Port, Portgroup, Allocation, Deploy Template).
- `manifests/` — `RestDefinition`s, `CompositionDefinition`s, and example CRs.
- `charts/` — the four composition Helm charts.
- `local/` — the free local env (kind config, standalone Ironic, kubeconfig isolation).
- `deploy/` — openstack-helm Ironic deployment for real clusters.
- `kagent/` — the Ironic-KOG expert agent (kagent).
- `scripts/` — OAS ConfigMap creation, proxies, smoke tests.
