# Kubernetes on Bare Metal

A Krateo blueprint that turns a set of physical servers into a working
Kubernetes cluster. Name the control-plane and worker blades and the OS image;
the composition provisions each machine through OpenStack Ironic, bootstraps the
cluster with `kubeadm`, joins the rest, and installs Cilium.

The Kubernetes it installs is **unmodified upstream** from `pkgs.k8s.io`. In
CNCF's terms this is an *installer*, not a *distribution* — nothing is patched.
The v1.36.6 cluster it builds has been through the CNCF conformance suite:
**443 of 443 specs passed, 0 failed** (`conformance/v1.36/`, measured on chart
0.13.22).

## How the join works

The thing worth understanding before you use it. A joining node needs the
`kubeadm join` command, which only exists once the first control plane has run
`kubeadm init` — and that happens on a blade, minutes after the composition was
rendered.

So the blueprint does not try to pass the token through the chart. The
bootstrapping control plane **publishes its own join command into its own host
resource**, and every other node is gated on that value appearing. The
rendezvous runs through a resource that already exists, which is why this needs
no new CRDs and no new controller.

```
control plane 0:  provision → kubeadm init → publish join command
other nodes:      wait for it → provision → kubeadm join
```

## What it does not do

- **It does not install Ironic**, and it does not install Krateo. It needs both.
- **It does not manage the machines itself.** It renders one `BaremetalHost` per
  cluster member, so the **`baremetal-host` blueprint must be registered too** —
  and registered *first*, or every render fails against an apiVersion nothing
  serves yet.
- **It does not provide a load balancer for the API server.** A multi-node
  control plane needs `controlPlane.endpoint` to be a virtual address that is
  already load-balanced — kube-vip, MetalLB, or something upstream of the
  cluster. Which one is a network decision, not a chart input.

## Requirements

| | Version | How this was established |
|---|---|---|
| Krateo control plane | `>= 2.5.1` | the `krateoSupportedVersion` the sibling blueprints of this product declare |
| core-provider | `2.13.65` | the version this blueprint runs under today -- core-provider and chart-inspector are 2.13.65 (upgraded 2026-11-05, §9.20); the five generated cdc controllers still run `composition-dynamic-controller:2.13.62` until the next `make krateo-up` converges them |
| oasgen-provider | `0.50.0` | provides the Ironic KOG resources underneath |
| `baremetal-host` blueprint | `0.6.3` | the version this chart renders by default (`lifecycleApiVersion`) |
| Ironic API microversion | `>= 1.83` | below it the node look-up rejects `include_children` with a `406` |

## Install

Register `baremetal-host` **first**, then this one:

```sh
helm pull oci://ghcr.io/krateo-blueprints/charts/baremetal-host --version 0.6.3
kubectl apply -f compositiondefinition.yaml     # from inside that chart

helm pull oci://ghcr.io/krateo-blueprints/charts/kubernetes-cluster --version 0.15.3
kubectl apply -f compositiondefinition.yaml     # from inside this chart
```

The chart version *is* the served apiVersion: `0.15.3` is served as
`composition.krateo.io/v0-15-3`, and registering a version stops the previous one
being served. `lifecycleApiVersion` must name the registered `baremetal-host`
version; left empty it uses the one this chart was released against
(`v0-6-3`).

### Migrating a live cluster to a new chart version, measured

> The worked example below is the **0.14.0 → 0.15.0** migration as measured on
> 2026-11-08, and its version numbers are deliberately left alone: it is a dated
> record, not a template. The current version is above.

Investigated 2026-11-08 before touching anything, because `lab-ha` is a live
3CP/3worker cluster. The risk is **not** where I assumed it was.

**What is NOT a problem.** The CRD's storage version is `vacuum`, not the chart
version, and `spec.conversion.strategy` is `None`. So the stored object is
version-neutral: serving it under a new version name needs no data conversion,
and the stored spec of a v0-14-0 CR contains nothing that v0-15-0 would reject
(`nodeAccess` is new and optional, and is simply absent).

**What IS the coupling: the label.** A live composition carries
`krateo.io/composition-version: v0-14-0`, and the per-version controller selects
on it. Register v0-15-0 and a `kubernetesclusters-v0-15-0-controller` appears
selecting `v0-15-0` — which matches nothing until the existing CR's label moves.
That, not data migration, is what leaves a cluster unreconciled.

**And the ordering trap, which bites first.** The chartrepo serves what
`make chart-host` last packaged. After bumping the chart in git, the repo's
CompositionDefinition asks for `kubernetes-cluster-0.15.0.tgz` while the pod
still serves only `0.14.0` — so applying the CD first gives chart-inspector a
404 and the composition reports `reason=Unavailable` wrapping
`500 ... failed to fetch ... 404`. Measured in exactly that state on
2026-11-08: CD manifest at 0.15.0, chartrepo holding 0.14.0 only. This is the
`rendered vs stored` trap: the repo declared a version the cluster did not
serve.

So the sequence is:

1. `make chart-host` — packages 0.15.0 and MERGES it in, so 0.14.0 stays served
   and a rollback has something to roll back to.
2. Confirm the pod serves both (the target's `hack/chartrepo-wait.py` step does
   this, and a re-run's `rollout status` does not).
3. Apply the CompositionDefinition. v0-15-0 becomes served, v0-14-0 stops.
4. Move the existing CR's `krateo.io/composition-version` label, or recreate the
   CR. Until then the new controller selects nothing.

**Rollback:** revert the CompositionDefinition to 0.14.0. Because the chartrepo
merges rather than replaces, `kubernetes-cluster-0.14.0.tgz` is still being
served, so this is clean — which is the second reason step 1 comes first.

A three-node control plane with three workers:

```yaml
apiVersion: composition.krateo.io/v0-15-3
kind: KubernetesCluster
metadata:
  name: lab-ha
  namespace: openstack
spec:
  clusterName: lab-ha
  k8sVersion: v1.36.6
  image:
    source: http://192.0.2.1:8089/debian-13-genericcloud-amd64.qcow2
    checksum: http://192.0.2.1:8089/CHECKSUM
  configurationRef:
    name: ironic-endpoint
    namespace: openstack
  network:
    podCIDR: 10.244.0.0/16
    serviceCIDR: 10.96.0.0/12
  controlPlane:
    endpoint: 192.0.2.200          # a load-balanced VIP; required for HA
    nodes:
      - nodeName: blade01            # index 0 bootstraps the cluster
        oobIp: 192.0.2.101         # vestigial since v0.13.2; read by nothing
        driver: redfish
        driver_info:
          redfish_address: https://192.0.2.101
          redfish_username: admin
          redfish_password: replace-me
          redfish_system_id: /redfish/v1/Systems/1
        ports:
          - address: "00:00:5e:00:53:01"
            pxe_enabled: true
      - nodeName: blade02
        oobIp: 192.0.2.102
        driver: redfish
        driver_info:
          redfish_address: https://192.0.2.102
          redfish_username: admin
          redfish_password: replace-me
          redfish_system_id: /redfish/v1/Systems/1
        ports:
          - address: "00:00:5e:00:53:02"
            pxe_enabled: true
      - nodeName: blade03
        oobIp: 192.0.2.103
        driver: redfish
        driver_info:
          redfish_address: https://192.0.2.103
          redfish_username: admin
          redfish_password: replace-me
          redfish_system_id: /redfish/v1/Systems/1
        ports:
          - address: "00:00:5e:00:53:03"
            pxe_enabled: true
  workers:
    nodes:
      - nodeName: blade04
        driver: redfish
        driver_info:
          redfish_address: https://192.0.2.104
          redfish_username: admin
          redfish_password: replace-me
          redfish_system_id: /redfish/v1/Systems/1
        ports:
          - address: "00:00:5e:00:53:04"
            pxe_enabled: true
  ironicAuth:
    authUrl: http://192.0.2.1:5000
    ironicUrl: http://192.0.2.1:6385
    username: ironic-publisher
    password: replace-me
    userDomain: Default
```

Use one, three or five control-plane nodes. An even number cannot form an etcd
quorum.

## Day two

Every operation is an edit to the same resource — there is no second tool and no
sequence of manifests to apply in order.

| To do this | Change this |
|---|---|
| Add a worker | append to `workers.nodes` |
| Drain and remove a worker | move its entry to `workers.removed` — pods are evicted, then the blade is released; delete the entry once Ironic reports it `available` |
| Remove an unreachable worker | move its entry to `workers.failedNodes` (no drain) |
| Upgrade Kubernetes | raise `k8sVersion`, then set `controlPlane.upgrade.targetNode` to one node at a time; it is reimaged onto the new version |
| Recover a failed control plane | move its entry to `controlPlane.recovery.failedNodes`; once available, move it back into `nodes` at any index but 0 |
| Back up etcd | `controlPlane.etcd.snapshotSchedule` (default: every 6h, 7 days retained) |

A multi-node control plane runs etcd as a service on each node rather than
kubeadm's stacked etcd. Each member's address is resolved **at runtime** — the
bootstrap CP derives its own and publishes it to its `BaremetalNode` CR's
`spec.extra.etcd_ip`, and the replicas read it from there.

`oobIp` is **vestigial since v0.13.2 and read by nothing.** This section used to
say it was required because "etcd binds its peer and client URLs to that address
and it is written into the etcd server certificate". That described v0.11.0.
Hard-pinning etcd to a hand-typed address is precisely what v0.13.2 removed:
replicas render *concurrently* with the bootstrap so etcd can reach quorum, so at
render time the bootstrap has not booted and the value would be empty — baking
`https://:2379` into a member-add, which looks like an etcd fault and is not.

## Cilium, and why kube-proxy is replaced

`cni.install: cilium` is the default and the only path with Helm support;
anything else applies `cni.manifestUrl` with `kubectl`, which is how you bring
your own. Flannel will work and will also leave you unable to express which pods
may not talk to which, since it implements no NetworkPolicy.

`cni.kubeProxyReplacement` defaults to on, and turning it off costs more than it
looks:

- **Conformance fails.** Nothing implements `hostPort` otherwise — Cilium does
  so only under kube-proxy replacement, and it is not chained with the `portmap`
  plugin either. Measured against a cluster with kube-proxy 6/6 Ready: a running
  kube-proxy does *not* cover it.
- **`Service type=LoadBalancer` is unavailable**: Cilium's LB-IPAM and its L2
  announcements both require it.

With it on, the kube-proxy addon is skipped at `kubeadm init` — never installed,
rather than installed and deleted.

## Conformance

`conformance.install: true` puts the `hydrophone` binary on the bootstrapping
control plane, with a note next to the output directory on how to run it. It
**does not run the suite**: that takes around two hours and pulls a large image,
so starting it stays a deliberate act. Run it on the node, so the cluster-admin
kubeconfig never leaves the machine that owns it.

Both this and `cni.kubeProxyReplacement` became reachable from a Composition in
0.14.0. The templates had read them all along, but neither was in
`values.schema.json` — so the generated CRD had no such field, and `conformance`
was settable only by a direct `helm install`, while `cni.kubeProxyReplacement`
was *silently pruned* by the API server, because `cni` declares properties and is
therefore a structural schema with no `x-kubernetes-preserve-unknown-fields`.

## Removed in 0.14.0

`ironicApiUrl` and `ironicApiVersion` are **gone from the input contract** —
schema, `values.yaml` and manifests. They were read by no template in either
chart; the endpoint actually used is `ironicAuth.ironicUrl`, and the Ironic
microversion comes from the OAS documents.

`ironicApiVersion` is the one that had to go rather than merely being left
alone. It reads exactly like the knob that sets the microversion, it was pinned
to `1.81` — below the `1.83` floor the node look-up needs — and it was inert, so
anyone "fixing" the apparent disconnect by wiring it through would have
reintroduced a `406` on every transition. A Marketplace form would have shown it
as a labelled, editable field that does nothing.

A `KubernetesCluster` that still sets either is now **rejected** by the API
server with `unknown field`, rather than accepted and ignored. Remove them from
your manifests when you move to `v0-14-0`.
