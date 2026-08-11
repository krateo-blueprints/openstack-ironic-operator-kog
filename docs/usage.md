---
type: Usage
title: openstack-ironic-operator-kog — usage
description: How to run the blueprint — the free local kind + standalone Ironic env, installing Krateo KOG and the compositions, driving a BaremetalLifecycle to active with composition-dynamic-controller, and pointing at a real Ironic API (Bifrost or Keystone-protected).
resource: oci://ghcr.io/krateo-blueprints/charts/baremetal-host
tags: [ironic, usage, kind, composition, kog]
timestamp: 2026-08-11T00:00:00Z
---

# Usage

## Free local environment

Everything runs on a laptop for free — no hardware, PXE, VMs, or public cloud. An isolated
`kind` cluster runs Krateo and a standalone Ironic (official openstack-helm image,
`fake-hardware` driver, SQLite, noauth). Full detail in [`local/README.md`](../local/README.md).

```bash
make local-up        # kind + standalone Ironic + Krateo (KOG + core) + RestDefinitions
make provision-demo  # composition provisions a sample fake node -> active
make local-down      # tear down
```

`local-up` chains `kind-up → ironic-up → krateo-up → restdef-up`. Every `kubectl`/`helm`
call uses the isolated kubeconfig (`local/kubeconfig.ironic-kog`) and explicit
`--context kind-ironic-kog`, so your default `~/.kube/config` is never touched.

> The standalone Ironic pod includes an nginx sidecar that injects a default
> `X-OpenStack-Ironic-API-Version` header — Ironic rejects write requests without a
> microversion (HTTP 406) and the rest-dynamic-controller does not send one. If a `Node` CR
> reports `create failed: 406`, check the sidecar is up (`kubectl -n openstack get pod
> -l app=ironic` shows 2/2).

## Driving a composition with composition-dynamic-controller

This is the real flow. The state machine is `lookup`-driven, so it needs cdc's repeated
reconciles — a single `helm install` advances only one step.

```bash
make composition-up    # package + host all four charts, apply the CompositionDefinitions
make composition-demo  # create a BaremetalLifecycle instance; cdc walks it enroll -> active
# watch:
kubectl -n openstack get node.baremetal.ogen.krateo.io metal-a \
  -o jsonpath='{.status.provision_state}'
```

`composition-up` serves the charts from an in-cluster nginx (`make chart-host`) and points
each `CompositionDefinition` at it. `core-provider` then generates the composition CRD +
controller. For a real cluster, publish the chart (`make package-chart`, or the GHCR OCI
artifact the release workflow pushes) and set `spec.chart.url` in the matching
`manifests/compositiondefinition-*.yaml`.

`make provision-demo` is a lighter alternative that simulates reconciles with repeated
`helm upgrade` (no `CompositionDefinition` needed).

> Pacing: progression is gated by KOG's Node-controller status resync (~tens of seconds per
> state), so a full enroll→active walk takes a few minutes against the fake driver.

## Apply at the right `apiVersion`

`core-provider` keeps every prior chart version served on the generated CRD (`served: true`,
`vacuum` storage), so multiple consumers can pin different chart versions. **The one rule
that catches everyone**: on `kubectl apply`, set the `apiVersion` to a version whose schema
actually has the fields you use. Apply at an older `apiVersion` and the kube-apiserver
schema-prunes the unknown fields silently at write time — the apply succeeds, but the stored
spec is missing them and the composition renders without them.

```yaml
# Applying a BaremetalHost using spec.online (v0-1-0) and spec.maintenance (v0-1-2):
# apply at v0-1-2 or later, the version that has BOTH fields.
apiVersion: composition.krateo.io/v0-1-2
kind: BaremetalHost
metadata:
  name: blade03
spec:
  nodeName: blade03
  online: true
  maintenance: false
```

Reading defaults to the **oldest** served version (a cosmetic `kubectl` nuisance). To see
the latest schema's view, hit the precise endpoint:

```bash
kubectl get --raw \
  /apis/composition.krateo.io/v0-1-2/namespaces/openstack/baremetalhosts/blade03 | jq .
```

Symptoms of a wrong-version apply, and their fixes, are in [api](./api.md) and the README
troubleshooting section.

## Release and re-deploy a blade

Releasing a blade back to `available` is a **spec field**, not a deletion — see
[overview](./overview.md) for why. `kubectl delete` runs `helm uninstall` with no re-render,
so it cannot drive an Ironic walk.

```bash
# 1. release the blade back to available; the composition drives the state walk.
kubectl patch baremetalhost blade05 --type=merge -p='{"spec":{"undeploy":true}}'

# 2. (optional) wait for available.
kubectl get baremetalhost blade05 -w   # provision_state ticks deleted -> cleaning -> available

# 3. (optional) now safe to remove from k8s — Ironic is at available, DELETE round-trips.
kubectl delete baremetalhost blade05

# Re-deploy instead: clear undeploy and (optionally) change the image.
kubectl patch baremetalhost blade05 --type=merge \
  -p='{"spec":{"undeploy":false,"image":{"source":"http://.../new.qcow2","checksum":"..."}}}'
```

`spec.undeployMode` controls Ironic's cleanup pass between `deleted` and `available`:
`full` (default, `automated_clean=true`, IPA disk-erase — production) or `none`
(`automated_clean=false`, seconds, keeps tenant data — private labs only). See
[configuration](./configuration.md).

## Against a real Ironic API

Neither path needs operator changes — only the endpoint behind the in-cluster `ironic`
Service changes.

- **Standalone Ironic on a Linux host (Bifrost)** — real PXE deploys to libvirt VMs as
  virtual bare metal, no Keystone/Glance/Nova. `make bifrost-up BIFROST_URL=http://<host>:6385`.
  Full walkthrough: [BIFROST.md](./BIFROST.md).
- **Keystone-protected Ironic** — an auth proxy authenticates with your `clouds.yaml`,
  injects a fresh `X-Auth-Token` + microversion, and forwards.
  `make keystone-up CLOUDS_FILE=clouds.yaml OS_CLOUD=<name>`. Full walkthrough:
  [REAL-IRONIC.md](./REAL-IRONIC.md).

## Kubernetes cluster on bare metal

The `kubernetes-cluster` composition turns Ironic-provisioned blades into a
kubeadm-bootstrapped cluster from a single `KubernetesCluster` CR. Architecture, HA/etcd
design and the ingress option matrix are in [KUBERNETES-CLUSTER.md](./KUBERNETES-CLUSTER.md)
and [USER-GUIDE.md](./USER-GUIDE.md).

## Related runbooks

- First-time smoke test with the `openstack baremetal` CLI: [quickstart.md](./quickstart.md).
- End-to-end validation on the fake driver: [E2E.md](./E2E.md).
- Recovering a desynced release: [ORPHAN-RECOVERY.md](./ORPHAN-RECOVERY.md).
- Restoring HA etcd from a snapshot: [RUNBOOK-ETCD-RESTORE.md](./RUNBOOK-ETCD-RESTORE.md).
