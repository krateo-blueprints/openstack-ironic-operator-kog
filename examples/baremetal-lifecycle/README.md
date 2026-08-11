---
type: Example
title: baremetal-lifecycle — enroll a node to active
description: A BaremetalLifecycle composition CR that drives an Ironic bare-metal node from enroll to active through the composition-dynamic-controller, showing node identity, driver_info, PXE ports, the deploy image and power control.
resource: oci://ghcr.io/krateo-blueprints/charts/baremetal-lifecycle
tags: [ironic, example, composition, baremetal-lifecycle]
timestamp: 2026-08-11T00:00:00Z
---

# baremetal-lifecycle example

[`composition.yaml`](./composition.yaml) is a `BaremetalLifecycle` composition instance. Once
the `baremetal-lifecycle` `CompositionDefinition` is registered (`make composition-up`),
`core-provider` has generated the CRD and its `composition-dynamic-controller` (cdc). Applying
this CR makes cdc helm-install the `charts/baremetal-lifecycle` chart and — re-evaluating the
chart's `lookup` of the Node CR's `status.provision_state` on every reconcile — walk the node
`enroll → manage → manageable → provide → available → deploy → active`.

## The CR

```yaml
apiVersion: composition.krateo.io/v0-3-1   # chart 0.3.1 -> v0-3-1
kind: BaremetalLifecycle                    # PascalCase of the CD name baremetal-lifecycle
metadata:
  name: baremetal-lifecycle
  namespace: krateo-system
spec:
  nodeName: server-01               # node identity
  driver: ipmi
  driver_info:                      # BMC creds passed straight to Ironic
    ipmi_address: 172.19.74.10
    ipmi_username: admin
    ipmi_password: changeme
  nodeUuid: 11111111-1111-1111-1111-111111111111   # required when ports are declared
  ports:
    - address: "00:60:2f:32:81:01"  # PXE NIC MAC
      pxe_enabled: true
  instance_info:                    # image to deploy (available -> active)
    image_source: "http://172.19.74.1:8089/debian-12-generic-amd64.qcow2"
    image_checksum: "http://172.19.74.1:8089/CHECKSUM"
  online: true                      # power on (omit to leave power unmanaged)
```

## Apply and watch

```bash
kubectl apply -f examples/baremetal-lifecycle/composition.yaml

# Watch the Ironic provision_state on the KOG-generated Node CR:
kubectl -n krateo-system get node.baremetal.ogen.krateo.io server-01 \
  -o jsonpath='{.status.provision_state}'
```

The per-resource CRs (`Node` / `Port` / `NodeProvision` / `NodePower`) are rendered by the
chart at install time — there is no separate `samples/` directory.

> **Apply at the right `apiVersion`.** `0.3.1` maps to `composition.krateo.io/v0-3-1`.
> Applying at an older served version silently schema-prunes any field that version's schema
> doesn't have. See [docs/api.md](../../docs/api.md).

A full enroll→active walk against the fake driver takes a few minutes — progression is gated
by KOG's Node-controller status resync (~tens of seconds per state).
