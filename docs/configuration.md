---
type: Configuration
title: openstack-ironic-operator-kog — configuration
description: The value surface of each of the two charts — baremetal-host, kubernetes-cluster — the keys that drive Ironic transitions, and the singleton NodeConfiguration every generated controller reads.
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

`baremetal-lifecycle` and `baremetal-discovery` were retired on 2026-11-25: both were
`deprecated: true`, both rendered a `kind: Node` that no RestDefinition declares (so their
output could never be applied), and neither carried the in-chart `compositiondefinition.yaml`
the Marketplace index gate requires. Use `baremetal-host` with `enableInspection: true`,
which does what the split did. Already-published versions stay served and cannot be
withdrawn — `baremetal-lifecycle` 0.3.1 is still in the `blueprints` index.

## kubernetes-cluster (`charts/kubernetes-cluster`)

Renders one `BaremetalHost` per member and gates the join sequence. Defaults here are
placeholders — a real cluster overrides them on the `KubernetesCluster` CR.

| key | default | effect |
|---|---|---|
| `clusterName` | `lab` | cluster name. |
| `k8sVersion` | `v1.31.4` | kubeadm/kubelet/kubectl package version via cloud-init. |
| `cni.install` / `.manifestUrl` | `flannel` / *(flannel release URL)* | CNI applied by the CP post-init. |
| `image.source` / `.checksum` | *(empty)* | cloud-init OS image deployed onto every blade (Debian/Ubuntu-shaped). |
| `lifecycleNamespace` | *(empty)* | where the rendered `BaremetalHost` CRs land. (The key keeps its name for compatibility; the CRs are `BaremetalHost`.) |
| `configurationRef` | `ironic-endpoint` / `openstack` | Ironic auth, same shape as `baremetal-host`. |
| `conformance.install` / `.version` / `.runOnBoot` / `.outputDir` | `false` / `v0.8.0` / `false` / `/var/lib/conformance` | CNCF conformance runner on the bootstrap CP. **Reachable from a Composition since 0.14.0** -- the keys were read by the templates but absent from `values.schema.json`, so the generated CRD had no such field. |
| `cni.kubeProxyReplacement` | `true` | Cilium replaces kube-proxy. **Reachable from a Composition since 0.14.0** -- `cni` declares properties, so it is a structural schema with no `x-kubernetes-preserve-unknown-fields`, and the API server silently PRUNED this key. |
| `ironicAuth.*` | `""` (userDomain `Default`) | Keystone creds baked into CP cloud-init so `publish-join.sh` can PATCH Ironic's `node.extra` from the blade. |
| `managementCluster.apiUrl` / `.caBundle` / `.serviceAccount*` | `""` | management API the CP cloud-init patches to publish the join token. `caBundle` is optional (the chart `lookup`s `kube-root-ca.crt`). |
| `network.podCIDR` / `.serviceCIDR` | `10.244.0.0/16` / `10.96.0.0/12` | cluster networking. |
| `network.managementApiReachability` | `external-lb` | **Documentation only — no template branches on it.** An ingress hint (kube-vip / MetalLB / external-lb / NodePort+DNS) describing how the management API is exposed to blades; the chart does not read it. See the [USER-GUIDE](./USER-GUIDE.md) option matrix for the network design it describes. |
| `controlPlane.endpoint` | `""` | stable apiserver address; **required** for HA (>1 node), points at a load-balanced VIP. |
| `controlPlane.nodes[]` | *(commented)* | preferred CP shape; 1/3/5 entries, index 0 is the bootstrap CP. `.oobIp` is **vestigial since v0.13.2 and read by nothing** — etcd's address is resolved at runtime from the bootstrap's `spec.extra.etcd_ip`. |
| `controlPlane.node` | *(empty)* | legacy single-CP shape, ignored when `nodes[]` is populated. |
| `controlPlane.etcd.*` (all four) | *(as shipped)* | **VESTIGIAL SINCE v0.12.0 AND READ BY NOTHING.** `aptPackage`, `snapshotImage`, `snapshotSchedule` and `snapshotRetentionDays` configured the external-etcd install and its snapshot CronJob. v0.12.0 reverted to kubeadm-stacked etcd and deleted the CronJob, so **there are no scheduled etcd backups** and setting these has no effect. Measured 2026-11-17: zero template interpolations of any of the four, against 8 for `allowPasswordAuth` as a control. Kept in the schema (and not required) so existing CRs that set them still validate — same treatment as `.oobIp`. See [RUNBOOK-ETCD-RESTORE.md](./RUNBOOK-ETCD-RESTORE.md). |

HA/etcd rationale and the full day-2 lever set (`upgrade.targetNode`,
`recovery.failedNodes[]`) are in
[KUBERNETES-CLUSTER-V0.11.0-DESIGN.md](./KUBERNETES-CLUSTER-V0.11.0-DESIGN.md) and
[KUBERNETES-CLUSTER.md](./KUBERNETES-CLUSTER.md).

## The singleton NodeConfiguration

Every generated controller reads a namespace-scoped singleton `NodeConfiguration`
(`manifests/nodeconfiguration-ironic.yaml`, named `ironic-endpoint`) holding the Ironic
endpoint, microversion header and auth. `configurationRef.name` on every chart points at it.
Override only when multiple Ironic endpoints coexist in one namespace.
