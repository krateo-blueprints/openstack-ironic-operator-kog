---
type: Example
title: baremetal-host — enroll a node through inspection to active
description: A BaremetalHost composition CR that drives an Ironic bare-metal node from enroll through inspection to active through the composition-dynamic-controller, showing node identity, driver_info, a BMC password from a Secret, PXE ports, the deploy image and convergent power.
resource: oci://ghcr.io/krateo-blueprints/charts/baremetal-host
tags: [ironic, example, composition, baremetal-host]
timestamp: 2026-11-25T00:00:00Z
---

# baremetal-host example

[`composition.yaml`](./composition.yaml) is a `BaremetalHost` composition instance. Once the
`baremetal-host` `CompositionDefinition` is registered (`make composition-up`), `core-provider`
has generated the CRD and its `composition-dynamic-controller` (cdc). Applying this CR makes cdc
helm-install the `charts/baremetal-host` chart and — re-evaluating the chart's `lookup` of the
`BaremetalNode` CR's `status.provision_state` on every reconcile — walk the node
`enroll → manage → manageable → inspect → provide → available → deploy → active`.

This replaces `examples/baremetal-lifecycle`, retired on 2026-11-25 with its chart. That chart
and `baremetal-discovery` were the superseded split `baremetal-host` absorbed: both were
`deprecated: true` and both rendered a `kind: Node` that no RestDefinition in this repo
declares, so their output could never be applied at all.

## The CR

```yaml
apiVersion: composition.krateo.io/v0-7-0   # chart 0.7.0 -> v0-7-0
kind: BaremetalHost                         # PascalCase of the CD name baremetal-host
metadata:
  name: baremetal-host
  namespace: krateo-system
spec:
  nodeName: server-01               # node identity; with `driver`, the only required fields
  nodeNamespace: openstack          # where the per-resource CRs land
  driver: redfish
  driver_info:                      # BMC details passed straight to Ironic
    redfish_address: https://172.19.74.11
    redfish_username: ironic
    redfish_system_id: /redfish/v1/Systems/server-01
    redfish_verify_ca: false
  bmcSecret:                        # the password, from a Secret (see below)
    name: server-01-bmc
  nodeUuid: 11111111-1111-1111-1111-111111111111
  ports:                            # PXE NIC(s); nodeUuid required when ports are declared
    - address: "00:60:2f:32:81:01"
      pxe_enabled: true
  enableInspection: true            # inspection and provisioning are one convergent walk
  image:                            # deploy target; omit to stop at `available`
    source: "http://172.19.74.1:8089/debian-13-genericcloud-amd64.qcow2"
    checksum: "http://172.19.74.1:8089/CHECKSUM"
  online: true                      # convergent power, re-asserted every reconcile
```

## The BMC password never goes in the CR

`driver_info` deliberately carries **no** `redfish_password`. Create the Secret in the CR's own
namespace and name it in `bmcSecret`:

```bash
kubectl create secret generic server-01-bmc -n openstack \
  --from-literal=redfish_password="$REDFISH_PASSWORD"
```

`manifests/restdefinition-node.yaml`'s `secretRef` resolver reads it and fills
`driver_info.redfish_password` in the request body to Ironic, so the password never reaches
`values.yaml`, the rendered CR, or etcd outside the Secret. Setting both `bmcSecret.name` and
`driver_info.redfish_password` **fails the render on purpose**: the resolver would overwrite the
plaintext, leaving it in the CR doing nothing while looking load-bearing.

## Running it without hardware

This example addresses a real BMC over Redfish. For the free local walk — `kind` plus a
standalone Ironic on the `fake-hardware` driver, no hardware, no BMC, no PXE — use
[`manifests/baremetalhost-fake-demo.yaml`](../../manifests/baremetalhost-fake-demo.yaml), which
is what `make composition-demo` applies. See [`local/README.md`](../../local/README.md).
