# Bare-Metal Host (OpenStack Ironic)

A Krateo blueprint that manages one physical server through [OpenStack
Ironic](https://docs.openstack.org/ironic/latest/) as a single Kubernetes
resource. Declare the host's BMC, its NICs and the OS image you want on it; the
composition drives Ironic's state machine from enrollment to a running machine,
holds it there, and takes it back when you ask.

The surface is deliberately close to metal3's `BareMetalHost`, so the shape is
familiar — but the engine underneath is Ironic's own API, reached through Krateo's
KOG, with no metal3 operator in the cluster.

```
registering → (inspecting → ready)? → available → (provisioning → provisioned)
```

## What it does not do

- **It does not install Ironic.** It needs a reachable Ironic API — standalone
  (Bifrost-style) or part of a full OpenStack. For Ironic itself, see the
  `ironic` blueprint in `krateo-blueprints/krateo-openstack-blueprint`.
- **It does not build a cluster.** For that, the `kubernetes-cluster` blueprint
  renders one of these per machine.
- **Deleting the host does not release the hardware.** That is deliberate, and
  the next section says why.

## Requirements

| | Version | How this was established |
|---|---|---|
| Krateo control plane | `>= 2.5.1` | the `krateoSupportedVersion` the sibling blueprints of this product declare |
| core-provider | `2.13.62` | the version this blueprint is running under today; the composition-adoption behaviour it depends on was probed on 2.13.62 and 2.13.65 |
| oasgen-provider | `0.50.0` | provides the Ironic KOG resources (`BaremetalNode`, `Port`, `NodeProvision`, `NodePower`) this chart renders |
| Ironic API microversion | `>= 1.83` | below it the node look-up rejects `include_children` with a `406` |

Those middle two are the versions in use, not a floor anyone has tested
downwards from. Older ones may work; nobody has measured it.

## Install

Register the blueprint, then create hosts.

```sh
helm pull oci://ghcr.io/krateo-blueprints/charts/baremetal-host --version 0.6.3
kubectl apply -f compositiondefinition.yaml     # ships inside the chart
```

`core-provider` reads the chart's `values.schema.json`, generates the
`BaremetalHost` CRD from it, and starts a controller **for this chart version**.
The chart version *is* the served apiVersion: `0.6.3` is served as
`composition.krateo.io/v0-6-3`. Registering a new version stops the previous one
being served, so anything that renders `BaremetalHost` resources at a pinned
apiVersion — the `kubernetes-cluster` blueprint does — has to move in the same
step.

Then one host:

```yaml
apiVersion: composition.krateo.io/v0-6-3
kind: BaremetalHost
metadata:
  name: blade03
  namespace: openstack
spec:
  nodeName: blade03
  driver: redfish
  driver_info:
    redfish_address: https://172.19.74.103
    redfish_username: admin
    redfish_password: <…>
    redfish_system_id: /redfish/v1/Systems/1
  ports:
    - address: "00:60:2f:33:81:01"
      pxe_enabled: true
  enableInspection: true
  image:
    source: http://172.19.74.1:8089/debian-13-genericcloud-amd64.qcow2
    checksum: http://172.19.74.1:8089/CHECKSUM
    checksum_type: sha256
  configDrive:
    metaData:
      hostname: blade03
    userData: |
      #cloud-config
      users:
        - name: ironic
          sudo: ['ALL=(ALL) NOPASSWD:ALL']
          ssh_authorized_keys:
            - ssh-ed25519 AAAA… you@example
  configurationRef:
    name: ironic-endpoint
```

Leave `image` out entirely and the host registers and stops at `available`,
without anything being written to its disks.

## Releasing the hardware

**Deleting the `BaremetalHost` removes it from Kubernetes and leaves Ironic
untouched.** To release the machine, set `undeploy: true` and wait for Ironic to
report it `available`; only then delete the resource.

This is not an oversight. The delete path calls `helm uninstall` without
re-rendering the chart, so it cannot walk a state machine at all — a resource
deleted while `active` would leave Ironic holding a provisioned node with nothing
in Kubernetes describing it. Driving the release through the normal reconcile
loop is the only way it can be made to converge.

`undeployMode` chooses what Ironic does on the way back. `full` runs the
standard clean steps including the disk erase. `none` skips cleaning: seconds
instead of hours, with the previous tenant's data still on the disks. Private
labs only.

## Notes on the inputs

- **`ports` must be complete before the first deploy.** Ironic writes the
  per-MAC PXE configuration only for ports that exist when the deploy is
  prepared, so a NIC declared late leaves the blade looping on "PXE boot
  failed".
- **`driver_info` is passed to Ironic verbatim** and is not validated here.
  Ironic rejects an unusable set at registration; set `debug.connectorVerbose`
  to see what it actually said, because the error otherwise arrives as a bare
  status code with Ironic's own explanation discarded.
- **`detached: true` stops every transition** while keeping the host
  registered — the hand-off switch, equivalent to metal3's `detached`
  annotation.
- **`deployedConfigDriveHash` is managed for you.** It is in the schema because
  the composition writes it; setting it by hand is not useful.

## A default that is not in this schema, on purpose

`debug.connectorVerbose` has no `default` in `values.schema.json`. Its default
lives in `values.yaml`, where Helm applies it at render time and nothing is
persisted.

This matters more than it looks. When a *nested* property under an optional
object carries a schema default, plumbing's `crdgen` gives the parent
`default: {}`, the API server materialises the whole stanza on read, and the
first unrelated write persists it — after which pinning the CRD back does not
remove it and `kubectl apply` will not prune it, because the field is unowned in
`managedFields`. It was measured on this fleet on 2026-11-05: with that default
present, all six live hosts carried a `spec.debug: {connectorVerbose: false}`
that no manifest had ever written. `make check-blueprints` now fails if such a
default is added again.

The hosts provisioned before 0.6.3 keep the stanza. It is harmless — the value
it asserts is the one the chart would have used anyway — but it is not
removable, and that is the point of the check.
