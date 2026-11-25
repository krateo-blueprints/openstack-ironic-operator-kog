# OpenStack-Helm Ironic Deployment

This directory contains values overrides and scripts for deploying Ironic on Kubernetes via openstack-helm, as specified in the plan (Phase 1).

## Prerequisites

- Kubernetes cluster (1.30+)
- Helm 3+
- [openstack-helm plugin](https://docs.openstack.org/openstack-helm/latest/install/before_starting.html) and repositories
- `openstack` namespace created
- **Provisioning network**: Ironic conductor must run on a node with an interface on the PXE/provisioning network (e.g. `172.19.74.0/24` for OOB/IB). Bare metal targets must be able to reach this network for PXE boot.

## Deployment Sequence

Deploy in this order:

1. **Backend services**: rabbitmq, mariadb, memcached
2. **OpenStack core**: keystone, glance, neutron (minimal)
3. **Object store**: Ceph or enable `object_store` for Glance (required for IPA images)
4. **Ironic**: with conductor on host network

## Environment Variables

```bash
export OPENSTACK_RELEASE=2025.1
export FEATURES="${OPENSTACK_RELEASE} ubuntu_noble"
export OVERRIDES_DIR=$(pwd)/overrides
```

**Note**: The Ironic conductor uses `network.pxe.device` as `PROVISIONER_INTERFACE`. Edit `deploy/values/ironic-overrides.yaml` and set `network.pxe.device` to your actual provisioning NIC (e.g. `eth1`, `ens192`).

## Deploy Script

For a one-shot deployment (requires openstack-helm plugin and repo):

```bash
./deploy/deploy-ironic-stack.sh
```

Environment variables: `OPENSTACK_RELEASE`, `FEATURES`, `OVERRIDES_DIR`, `OVERRIDES_URL`. Use `IRONIC_ONLY=true` to skip backends if already deployed.

## Step-by-Step

### 1. Backend Services

```bash
helm upgrade --install rabbitmq openstack-helm/rabbitmq \
  --namespace=openstack \
  --set pod.replicas.server=1 \
  --timeout=600s

helm upgrade --install mariadb openstack-helm/mariadb \
  --namespace=openstack \
  --set pod.replicas.server=1

helm upgrade --install memcached openstack-helm/memcached \
  --namespace=openstack
```

### 2. Keystone, Glance, Neutron

Follow [OpenStack-Helm Install](https://docs.openstack.org/openstack-helm/latest/install/openstack.html) for keystone, glance, and neutron. Glance requires Ceph or object storage for image storage.

### 3. Ironic

```bash
helm upgrade --install ironic openstack-helm/ironic \
  --namespace=openstack \
  -f deploy/values/ironic-overrides.yaml
```

**Important**: The conductor pod uses `hostNetwork: true` and must be scheduled on a node that has the provisioning interface. Set `labels.conductor.node_selector_key` and `labels.conductor.node_selector_value` to target that node, or use a DaemonSet-style placement.

## Key Ironic Overrides

| Setting | Purpose |
|---------|---------|
| `pod.useHostNetwork.conductor: true` | Required for PXE/TFTP to bind to provisioning interface |
| `conductor.http.enabled: true` | HTTP server for IPA ramdisk |
| `conductor.pxe.enabled: true` | TFTP for PXE boot |
| `PROVISIONER_INTERFACE` | Env var: interface name for PXE (e.g. eth1) |
| `network.pxe.neutron_subnet_cidr` | Provisioning subnet (e.g. 172.24.6.0/24) |

## Verifying

After deployment, enrol a test node manually:

```bash
openstack baremetal node create \
  --driver ipmi \
  --driver-info ipmi_address=172.19.74.202 \
  --driver-info ipmi_username=admin \
  --driver-info ipmi_password=password \
  --driver-info ipmi_port=623
```

Then run state transitions: `manage`, `inspect`, `provide`, `deploy`.

## Inspect → Provide: no longer a two-phase flow

This section used to describe a **two-phase deployment**: install the
`baremetal-lifecycle` chart with `runInspect: true`, wait for the `ironic-inspect-*`
Job, then `helm upgrade` with `runInspect: false` to emit `provide`. Without the second
phase the chart re-emitted `inspect` on every reconcile and looped.

That chart was retired on 2026-11-25 and the workaround went with it. Set
`enableInspection: true` on `baremetal-host` instead: inspection and provisioning are
one convergent walk there, so there are no phases to sequence and nothing to upgrade
between them. Walking transitions by hand with repeated `helm upgrade` is the YAML
choreography this repo rules out -- the controller advances the state machine.
