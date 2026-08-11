---
type: Runbook
title: End-to-end validation on the fake driver
description: Step-by-step end-to-end validation of the blueprint on a local kind cluster with standalone Ironic (fake-hardware), from local-up through a full enroll-to-active provision walk.
resource: oci://ghcr.io/krateo-blueprints/charts/baremetal-host
tags: [ironic, e2e, validation, runbook, kind]
timestamp: 2026-08-11T00:00:00Z
---

# End-to-End Validation

Validated locally with `kind` + standalone Ironic (fake-hardware) — no real hardware.

## 1. Bring up the stack

```bash
make local-up
```

This creates the kind cluster `ironic-kog`, deploys standalone Ironic (official
openstack-helm image, fake drivers, noauth, + version-injecting nginx sidecar), installs
Krateo (`oasgen-provider` + `core-provider`), and applies the RestDefinition.

Check:

```bash
KCTL="kubectl --kubeconfig local/kubeconfig.ironic-kog --context kind-ironic-kog"
$KCTL -n krateo-system get pods                      # providers Running
$KCTL -n openstack get restdefinition ironic-node    # READY=True
$KCTL get crd | grep baremetal                       # nodes + nodeconfigurations CRDs
$KCTL -n openstack get deploy ironic-node-controller # rest-dynamic-controller Running
```

## 2. Provision a server through composition-dynamic-controller

```bash
make composition-up    # package + host the chart, apply the CompositionDefinition
make composition-demo  # create a BaremetalLifecycle instance (node metal-a)
```

`core-provider` generates a `BaremetalLifecycle` CRD; composition-dynamic-controller reconciles
the instance, helm-rendering the chart each reconcile. The chart renders, by `lookup` of the
Node's current `provision_state`, exactly one `NodeProvision` CR per state:
`enroll → manage`, `manageable → provide`, `available → active`. Each `NodeProvision` fires
`PUT /v1/nodes/{node}/states/provision` once (via the ironic-node-provision RestDefinition).

## 3. Observe

```bash
KCTL="kubectl --kubeconfig local/kubeconfig.ironic-kog --context kind-ironic-kog"
$KCTL -n openstack get baremetallifecycle metal-a                      # composition instance
$KCTL -n openstack get nodeprovision                                   # current transition CR
$KCTL -n openstack get node.baremetal.ogen.krateo.io metal-a -o jsonpath='{.status.provision_state}'

# node state in Ironic (walks enroll -> manageable -> available -> active):
$KCTL -n openstack exec deploy/ironic -c ironic -- \
  curl -s -H "X-OpenStack-Ironic-API-Version: 1.81" http://127.0.0.1:6385/v1/nodes/metal-a \
  | jq -r .provision_state
# -> active
```

Progression is paced by KOG's Node-controller status resync (a few minutes total against the
fake driver). Each NodeProvision fires its PUT exactly once (202 -> Pending guard).

## Lighter alternatives (no CompositionDefinition)

```bash
make provision-demo   # simulate reconciles with repeated `helm upgrade` until active
make smoke-test       # drive a fake node enroll->active directly via the Ironic API (no Krateo)
```

## Troubleshooting

See the Troubleshooting section in the top-level `README.md`.
