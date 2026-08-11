---
type: Configuration
title: openstack-ironic-operator-kog — configuration
description: The value surface of each of the four charts — baremetal-host, baremetal-lifecycle, baremetal-discovery, kubernetes-cluster — the keys that drive Ironic transitions, and the singleton NodeConfiguration every generated controller reads.
resource: oci://ghcr.io/krateo-blueprints/charts/baremetal-host
tags: [ironic, configuration, values, chart]
timestamp: 2026-08-11T00:00:00Z
---

# Configuration

Each chart's values are typed by its `values.schema.json`; `core-provider` derives the
generated composition CRD's schema from that file, so a value that exists in the schema is a
field you can set on the CR. All defaults are pure block YAML (no flow-style braces).

## baremetal-host (`charts/baremetal-host`)

The unified single-CRD host lifecycle. Set only what you need — omitting `image` stops the
node at `available`; setting it walks to `active`.

| key | default | effect |
|---|---|---|
| `nodeName` | `""` | node identity (required at apply time). |
| `nodeUuid` | `""` | optional pinned Ironic UUID (stable across re-enroll). |
| `parentNode` | `""` | optional parent Node (e.g. blade → enclosure). |
| `driver` | `ipmi` | Ironic driver. `driver_info` carries the `ipmi_*` / `redfish_*` keys it needs. |
| `driver_info` | *(empty)* | BMC connection details passed straight to Ironic. |
| `nodeNamespace` | `""` | Node CR namespace; defaults to the release namespace. |
| `ports` | *(empty)* | list of `{address, pxe_enabled}` NICs. Ironic needs ≥1 port for inspect + deploy validation. |
| `enableInspection` | `false` | run `target: inspect` after manageable; results land in the Node CR status. |
| `image` | *(empty)* | `{source, checksum, checksum_type, format, root_device}`. Set `image.source` to deploy. |
| `configDrive` | *(empty)* | `{metaData, userData, networkData}` for cloud-init, assembled into `instance_info.config_drive`. |
| `maintenance` | `false` | Ironic maintenance mode (suspends power/agent monitoring during servicing). |
| `detached` | `false` | skip **all** transitions (Node/Port CRs stay; Ironic state not driven). metal3's `detached`. |
| `online` | *(unset)* | `true`=power on, `false`=power off, unset=don't manage. Each flip renames the NodePower CR so KOG fires the PUT once. |
| `cleanSteps` | *(empty)* | RAID/BIOS/firmware clean steps; runs `target: clean` from manageable. Each edit rehashes into a new NodeProvision name. |
| `networkData` | *(empty)* | merged into `instance_info.network_data` (the deployed OS's static view). |
| `undeploy` | `false` | `true` drives `active → available` via the normal reconcile loop (`transition-undeploy.yaml`). Not triggered by deleting the CR. |
| `undeployMode` | `full` | `full`=`automated_clean=true` (IPA disk-erase; production); `none`=skip cleaning (seconds, keeps tenant data; labs only). |
| `configurationRef.name` | `ironic-endpoint` | the singleton `NodeConfiguration` the Node CR points at. |

## baremetal-lifecycle (`charts/baremetal-lifecycle`)

Provisioning-only walk `enroll → active`. Inspection is out of scope — use
`baremetal-discovery` first for inventory.

| key | default | effect |
|---|---|---|
| `nodeName` / `nodeUuid` / `parentNode` | `""` | identity, as above. |
| `driver` / `driver_info` | `ipmi` / *(empty)* | driver + BMC keys (ipmi or redfish). |
| `ports` | *(empty)* | KOG `Port` CR per entry; `node_uuid` filled from `nodeUuid` (which must be set when ports are declared). |
| `instance_info` | *(empty)* | `{image_source, image_checksum}`; required before deploy (`available → active`). |
| `configDrive` | *(empty)* | cloud-init meta/user/network data merged into `instance_info` at deploy. |
| `ironicApiUrl` | `http://ironic.openstack.svc.cluster.local:6385` | endpoint the provisioner Job hits. |
| `ironicAuthType` | `none` | standalone/noauth by default. |
| `ironicApiVersion` | `1.99` | microversion sent by the provisioner Job. |
| `waitTimeout` | `300` | per-transition `--wait` seconds for the `openstack baremetal` CLI. |
| `openstackClientImage` | `quay.io/airshipit/openstack-client:2026.1-ubuntu_noble` | client image for the provisioner Job. |
| `online` | *(unset)* | power control; each flip renders a freshly-named NodePower CR. |
| `nodeNamespace` | `""` | Node CR namespace (where the RDC watches). |
| `configurationRef.name` | `ironic-endpoint` | singleton `NodeConfiguration`. |

## baremetal-discovery (`charts/baremetal-discovery`)

Discovery-only walk `enroll → manage → inspect → manageable`; inventory is read from the
Node CR's status. Output feeds a lifecycle spec.

| key | default | effect |
|---|---|---|
| `nodeName` / `nodeUuid` / `parentNode` | `""` | identity. Preserve `nodeUuid` if you later re-enroll the same node under lifecycle. |
| `driver` / `driver_info` | `ipmi` / *(empty)* | driver + BMC keys. |
| `ports` | *(empty)* | required for `inspect_interface: agent` — Ironic refuses `PUT target=inspect` with no port. |
| `nodeNamespace` | `""` | Node CR namespace (defaults to release namespace). |
| `configurationRef.name` | `ironic-endpoint` | the same singleton both compositions share. |

## kubernetes-cluster (`charts/kubernetes-cluster`)

Renders one `BaremetalLifecycle` per member and gates the join sequence. Defaults here are
placeholders — a real cluster overrides them on the `KubernetesCluster` CR.

| key | default | effect |
|---|---|---|
| `clusterName` | `lab` | cluster name. |
| `k8sVersion` | `v1.31.4` | kubeadm/kubelet/kubectl package version via cloud-init. |
| `cni.install` / `.manifestUrl` | `flannel` / *(flannel release URL)* | CNI applied by the CP post-init. |
| `image.source` / `.checksum` | *(empty)* | cloud-init OS image deployed onto every blade (Debian/Ubuntu-shaped). |
| `lifecycleNamespace` | *(empty)* | where the rendered `BaremetalLifecycle` CRs land. |
| `configurationRef` | `ironic-endpoint` / `openstack` | Ironic auth, same shape as lifecycle. |
| `ironicApiUrl` / `ironicApiVersion` | `http://ironic.openstack.svc.cluster.local:6385` / `1.81` | Ironic endpoint. |
| `ironicAuth.*` | `""` (userDomain `Default`) | Keystone creds baked into CP cloud-init so `publish-join.sh` can PATCH Ironic's `node.extra` from the blade. |
| `managementCluster.apiUrl` / `.caBundle` / `.serviceAccount*` | `""` | management API the CP cloud-init patches to publish the join token. `caBundle` is optional (the chart `lookup`s `kube-root-ca.crt`). |
| `network.podCIDR` / `.serviceCIDR` | `10.244.0.0/16` / `10.96.0.0/12` | cluster networking. |
| `network.managementApiReachability` | `external-lb` | ingress hint (kube-vip / MetalLB / external-lb / NodePort+DNS). See the [USER-GUIDE](./USER-GUIDE.md) option matrix. |
| `controlPlane.endpoint` | `""` | stable apiserver address; **required** for HA (>1 node), points at a load-balanced VIP. |
| `controlPlane.nodes[]` | *(commented)* | preferred CP shape; 1/3/5 entries, index 0 is the bootstrap CP. Each HA CP must set `.oobIp`. |
| `controlPlane.node` | *(empty)* | legacy single-CP shape, ignored when `nodes[]` is populated. |
| `controlPlane.etcd.aptPackage` | `etcd-server=3.5.16-4` | external-etcd apt package for HA CPs (v0.11.0+). |
| `controlPlane.etcd.snapshotImage` | `gcr.io/etcd-development/etcd:v3.5.16` | etcdctl image for the snapshot CronJob. |
| `controlPlane.etcd.snapshotSchedule` | `0 */6 * * *` | snapshot cron (default 6h). |
| `controlPlane.etcd.snapshotRetentionDays` | `7` | days of on-host snapshots kept. |

HA/etcd rationale and the full day-2 lever set (`upgrade.targetNode`,
`recovery.failedNodes[]`) are in
[KUBERNETES-CLUSTER-V0.11.0-DESIGN.md](./KUBERNETES-CLUSTER-V0.11.0-DESIGN.md) and
[KUBERNETES-CLUSTER.md](./KUBERNETES-CLUSTER.md).

## The singleton NodeConfiguration

Every generated controller reads a namespace-scoped singleton `NodeConfiguration`
(`manifests/nodeconfiguration-ironic.yaml`, named `ironic-endpoint`) holding the Ironic
endpoint, microversion header and auth. `configurationRef.name` on every chart points at it.
Override only when multiple Ironic endpoints coexist in one namespace.
