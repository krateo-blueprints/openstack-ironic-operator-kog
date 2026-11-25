---
type: ExampleIndex
title: openstack-ironic-operator-kog — examples
description: Index of the runnable examples under examples/ — the BaremetalLifecycle composition that drives an Ironic node from enroll to active.
resource: oci://ghcr.io/krateo-blueprints/charts/baremetal-host
tags: [ironic, examples, composition]
timestamp: 2026-08-11T00:00:00Z
---

# Examples

- [`manifests/baremetalhost-fake-demo.yaml`](../manifests/baremetalhost-fake-demo.yaml) — a
  `BaremetalHost` composition CR that drives an Ironic bare-metal node from `enroll` to
  `active` through the `composition-dynamic-controller` generated from the
  `baremetal-host` `CompositionDefinition`. It shows the load-bearing spec fields (node
  identity, `driver`, PXE ports, `spec.image`, power) and the chart-version-encoded
  `apiVersion` (`composition.krateo.io/v0-7-0`). It runs against the local standalone
  Ironic with no hardware, and is what `make composition-demo` applies.

  This replaces `examples/baremetal-lifecycle`, retired on 2026-11-25 with its chart.

For more CR shapes, `manifests/` carries example instances of every kind — `BaremetalHost`
(`baremetalhost-blade*.yaml`, addressing real lab blades) and `KubernetesCluster`
(`kubernetescluster-*.yaml`).
