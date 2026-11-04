# Reproducing these conformance results

Kubernetes **v1.36.6**, installed onto six bare-metal nodes by the
`kubernetes-cluster` Krateo composition, which drives OpenStack Ironic through
Krateo's `rest-dynamic-controller`. CNI is **Cilium 1.20.4** with kube-proxy
replacement; the container runtime is **containerd 2.4.2**.

The product is an **installer** in CNCF's taxonomy: it downloads and installs
unmodified upstream Kubernetes from `pkgs.k8s.io`. No patches are applied to
the Kubernetes codebase.

## Topology under test

| Node | Role | Notes |
|---|---|---|
| blade01 | control-plane | bootstrap; runs `kubeadm init`, installs the CNI |
| blade02, blade03 | control-plane | `kubeadm join --control-plane`, stacked etcd |
| blade04–blade06 | worker | `kubeadm join` |

The control-plane endpoint is a VIP (`192.168.0.250:6443`) that the bootstrap
node **derives at runtime** by probing its own subnet with `arping -D` for a
free address, then claims with a `kube-vip` static pod. No IP addresses are
declared as inputs; the VIP is discovered, published to Ironic's `node.extra`,
and read back by the joining nodes.

## Prerequisites

- An OpenStack Ironic API reachable from the management cluster, with six
  enrolled nodes (`redfish` or `ipmi` driver) and a reachable BMC per node.
- A Kubernetes management cluster running Krateo:
  - `core-provider` (composition-dynamic-controller)
  - `oasgen-provider` + `rest-dynamic-controller` 0.47.0
- The four Ironic `RestDefinition`s from `manifests/` applied
  (`ironic-node`, `ironic-port`, `ironic-node-power`, `ironic-node-provision`).
- The `ironic-extra-bridge` Deployment from `manifests/ironic-extra-bridge.yaml`,
  which mirrors Ironic's `node.extra` onto the generated CRs.

## Steps

```bash
# 1. publish the composition charts and register them
make chart-host
kubectl apply -f manifests/compositiondefinition-baremetal-host.yaml
kubectl apply -f manifests/compositiondefinition-kubernetes-cluster.yaml

# 2. declare the cluster — ONE manifest, six nodes, no IPs
#    (see manifests/kubernetescluster-ettore-ha.example.yaml for the shape)
kubectl apply -f manifests/kubernetescluster-lab-ha.yaml

# 3. wait for all six Ironic nodes to reach `active`, then for the bootstrap
#    to publish its join data. Progress is observable without SSH:
kubectl -n openstack get baremetalnodes.v1-0-0.baremetal.ogen.krateo.io \
  -o custom-columns='NAME:.metadata.name,STATE:.status.provision_state'
kubectl -n openstack get baremetalnodes.v1-0-0.baremetal.ogen.krateo.io blade01 \
  -o jsonpath='{.spec.extra.cp_stage}{"\n"}'     # progress marker
```

The cluster is complete when `kubectl get nodes` on the bootstrap reports three
`control-plane` and three worker nodes `Ready`. Typical bring-up from
`available` to six `Ready` is about 25 minutes, most of it `apt` and image
pulls.

## Running the tests

`hydrophone` was run **on the bootstrap node**, so the cluster-admin kubeconfig
never leaves the machine that owns it:

```bash
# on blade01
curl -fsSL -o /tmp/h.tgz \
  https://github.com/kubernetes-sigs/hydrophone/releases/download/v0.8.0/hydrophone_Linux_x86_64.tar.gz
sudo tar -xzf /tmp/h.tgz -C /usr/local/bin hydrophone

sudo mkdir -p /var/lib/conformance
sudo env KUBECONFIG=/etc/kubernetes/admin.conf \
  hydrophone --conformance --output-dir /var/lib/conformance
```

`hydrophone` reported the API endpoint as the derived VIP
(`https://192.168.0.250:6443`) and used `registry.k8s.io/conformance:v1.36.6`.

## Result

```
Ran 443 of 7579 Specs in 7012.954 seconds
SUCCESS! -- 443 Passed | 0 Failed | 0 Pending | 7136 Skipped
Ginkgo ran 1 suite in 1h56m55.285064003s
```

`junit_01.xml` carries `tests="7586" disabled="7136" errors="0" failures="0"`
and `SuiteSucceeded = true`.

### Two settings this result depends on

Both were found by running the suite and are required for a clean pass:

- **`cni.kubeProxyReplacement: true`.** Without it nothing implements
  `hostPort`: Cilium provides it only under kube-proxy replacement, and it is
  not chained with the `portmap` CNI plugin. A *running* kube-proxy does not
  cover it, and `[sig-network] HostPort` fails.
- **containerd 2.x.** Debian trixie ships 1.7.24 and offers nothing newer, and
  `[sig-node] ImageVolume` needs runtime support that landed in the 2.x line.
  The chart installs `containerd.io` from `download.docker.com`.

## Verification of the artefacts in this directory

```
e2e.log       8937 bytes     sha256 ca26d971a37ee4ea...
junit_01.xml  2659622 bytes  sha256 90188a1d55e5c3f7...
```

Both were copied off the bootstrap node and their checksums compared against
the originals on disk there.
