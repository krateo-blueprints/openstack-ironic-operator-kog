---
type: ExampleIndex
title: openstack-ironic-operator-kog — examples
description: Index of the runnable examples under examples/ — the BaremetalLifecycle composition that drives an Ironic node from enroll to active.
resource: oci://ghcr.io/krateo-blueprints/charts/baremetal-host
tags: [ironic, examples, composition]
timestamp: 2026-08-11T00:00:00Z
---

# Examples

- [examples/baremetal-lifecycle](../examples/baremetal-lifecycle/README.md) — a
  `BaremetalLifecycle` composition CR that drives an Ironic bare-metal node from `enroll` to
  `active` through the `composition-dynamic-controller` generated from the
  `baremetal-lifecycle` `CompositionDefinition`. It shows the load-bearing spec fields
  (node identity, `driver_info`, PXE ports, `instance_info` image, power) and the
  chart-version-encoded `apiVersion` (`composition.krateo.io/v0-3-1`).

For more CR shapes, `manifests/` carries example instances of every kind — `BaremetalHost`
(`baremetalhost-blade*.yaml`), `BaremetalDiscovery` (`baremetaldiscovery-blade03.yaml`) and
`KubernetesCluster` (`kubernetescluster-*.yaml`).
