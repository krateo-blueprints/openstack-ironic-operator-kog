---
type: Runbook
title: Run against real standalone Ironic via Bifrost
description: Quickstart for driving the KOG operator against a real standalone Ironic deployed with Bifrost — real PXE deploys to libvirt VMs as virtual bare metal, no Keystone/Glance/Nova.
resource: oci://ghcr.io/krateo-blueprints/charts/baremetal-host
tags: [ironic, bifrost, runbook, pxe, standalone]
timestamp: 2026-08-11T00:00:00Z
---

# Quickstart: real standalone Ironic via Bifrost

The realistic *real-Ironic* path that stays 100% standalone — no Keystone, Glance, Nova,
Neutron, RabbitMQ. **Bifrost** is the OpenStack project that bundles standalone Ironic with
its own PXE/TFTP/HTTP, IPA ramdisk, and (via `--testenv`) sushy-tools + libvirt VMs that act as
virtual bare-metal targets so you can do *real* PXE deploys without buying hardware.

Architecture:

```
   your Mac                                 remote Linux host (KVM)
 ┌────────────┐                            ┌──────────────────────────────┐
 │ kind       │                            │ Bifrost                      │
 │  └─ Krateo │  HTTP :6385 (+microv hdr)  │   ironic  api+conductor      │
 │     +OAS───┼───── tunnel/tailscale ────▶│   dnsmasq PXE/TFTP/HTTP      │
 │     bifrost│                            │   IPA ramdisk + deploy img   │
 │     proxy  │                            │ sushy-tools (Redfish BMCs)   │
 └────────────┘                            │ libvirt VMs (bm1, bm2…)      │
                                           └──────────────────────────────┘
```

The operator and all its KOG / composition wiring **do not change**. Only what sits behind the
in-cluster `ironic` Service changes — `make bifrost-up` swaps the local fake for a small nginx
proxy that injects the microversion and forwards to your remote Bifrost.

## Prerequisites

- A **Linux host with KVM/libvirt** (Ubuntu 22.04 / 24.04 or Rocky 9, ≥ 4 vCPU / 8 GB / 40 GB).
  *Apple Silicon Macs cannot host this; use a Linux box you own or a small KVM VPS.*
- Working kind cluster + operator: `make local-up`.
- A way for the kind cluster to reach the remote's `:6385` (see *Connectivity* below).

## 1. Install Bifrost on the remote host

SSH in, then:

```bash
sudo apt update && sudo apt install -y git python3-pip
git clone https://opendev.org/openstack/bifrost
cd bifrost
./bifrost-cli install \
  --network-interface=$(ip -o -4 route show to default | awk '{print $5}') \
  --testenv \
  --enable-tls=false
```

`--testenv` provisions **sushy-tools + 2 libvirt VMs** (`bm1`, `bm2`) wired up as virtual Redfish
BMCs, downloads the IPA ramdisk, and stages a default Ubuntu deploy image. Ironic listens on
**`:6385`** (noauth), sushy-tools on **`:8000`**. Installs take 15–30 minutes; mostly downloads.

Verify locally on the host:

```bash
source /opt/stack/bifrost/bin/activate || source ~/openrc bifrost
openstack baremetal driver list             # redfish/ipmi
openstack baremetal node list               # bm1, bm2 (created by --testenv)
virsh list --all                            # the underlying libvirt VMs
curl -s http://localhost:8000/redfish/v1/Systems | jq .  # sushy-tools
```

Note each test node's UUID and PXE NIC MAC — you'll need them for the operator.

## 2. Connectivity from kind → `:6385`

Pick one. The kind proxy points at whichever URL you set as `BIFROST_URL`.

| Option | How | `BIFROST_URL` example |
|---|---|---|
| **Tailscale** *(recommended)* | `tailscale up` on the remote (and your Mac); `tailscale serve --tcp 6385 localhost:6385` if you want it on a shared address, otherwise just use the magicDNS name | `http://<remote-tailnet>:6385` |
| **SSH local-forward** | Locally: `ssh -fN -L 6385:127.0.0.1:6385 user@remote`. Then expose host port to kind via `host.docker.internal` | `http://host.docker.internal:6385` |
| **Public IP + firewall** | `ufw allow from <your-ip> to any port 6385` on the remote | `http://<remote-public-ip>:6385` |

> noauth Ironic on the open internet is unsafe. The first two keep it private; the third
> restricts by source IP and is only for short tests.

## 3. Switch the kind cluster to point at Bifrost

```bash
make bifrost-up BIFROST_URL=http://<remote-or-tunnel>:6385
```

This deploys an nginx proxy in the `openstack` namespace, generates its config with your
`BIFROST_URL`, and patches the `ironic` Service selector at it. The operator's OAS endpoint
(`http://ironic.openstack.svc.cluster.local:6385`) is unchanged. `make bifrost-down` reverts to
the local fake Ironic.

Sanity-check from kind:

```bash
make ironic-forward &        # background port-forward to localhost:6385
curl -s -H "X-OpenStack-Ironic-API-Version: 1.81" http://localhost:6385/v1/nodes \
  | jq '.nodes[] | {name, uuid, provision_state}'
# expect bm1/bm2 from your Bifrost testenv
```

## 4. Provision a real VM via the operator

Edit `manifests/baremetallifecycle-example.yaml` with the test VM's actual values (UUID and MAC
from step 1, image URL served by Bifrost's HTTP):

```yaml
spec:
  nodeName: bm1
  nodeNamespace: openstack
  driver: redfish
  driver_info:
    redfish_address: http://<bifrost-host>:8000
    redfish_system_id: "/redfish/v1/Systems/<libvirt-vm-uuid>"
    redfish_verify_ca: false
  ports:
    - address: "<vm-mac>"          # e.g. 52:54:00:aa:bb:cc
  instance_info:
    image_source: http://<bifrost-host>:8080/deploy/ubuntu-22.04.qcow2
    image_checksum: <sha256>
```

Then drive it through composition-dynamic-controller:

```bash
make composition-up
make composition-demo

# Watch the *real* state machine: each step really happens on the VM.
kubectl --kubeconfig local/kubeconfig.ironic-kog --context kind-ironic-kog \
  -n openstack get node.baremetal.ogen.krateo.io bm1 \
  -o jsonpath='{.status.provision_state}'
```

Real flow (minutes per step, not seconds):

```
enroll
  → manage      (Redfish: power on, BMC sanity)
  → manageable
  → inspect     (PXE boots IPA ramdisk; reports hardware to Ironic)
  → manageable
  → provide     (Ironic cleans the VM disk)
  → available
  → deploy      (writes the OS image; cloud-init runs)
  → active
```

## What's different vs the local fake env

- The driver actually contacts sushy-tools / IPA and writes real bits.
- `manage`/`inspect`/`provide`/`deploy` each take real time (minutes); the operator's pacing
  (lookup + Node status resync) doesn't change but the wall-clock does.
- Errors surface in `Node.status.conditions` / Bifrost logs (`journalctl -u ironic-api -u
  ironic-conductor`, sushy-tools `journalctl -u sushy-tools`).

## Tear down

```bash
make bifrost-down              # repoint ironic Service back at the local fake
# On the remote, to wipe Bifrost completely:
cd bifrost && ./bifrost-cli uninstall && sudo virsh destroy bm1; sudo virsh undefine bm1; ...
```

## Caveats

- This recipe is wired and the proxy is unit-tested, but has not yet been run end-to-end against
  a live Bifrost. Expect minor environment-specific iteration (NIC names, image URL paths, MAC
  vs UUID mapping).
- Bifrost downloads ~GB of artifacts on first install; ensure the host has the disk and bandwidth.
- For more than 2 virtual nodes, edit `bifrost/playbooks/inventory/test_vm.yml` *before* running
  `--testenv` (or define them yourself with `virt-install` + sushy-tools).
- If `redfish_system_id` is wrong, `manage` fails fast with a 404 in the Node CR conditions.
