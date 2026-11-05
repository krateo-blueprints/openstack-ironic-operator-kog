---
type: Runbook
title: Run the operator against a real Ironic API
description: Quickstart for driving the operator against a real, Keystone-protected Ironic — an auth proxy authenticates with your clouds.yaml, injects a fresh token plus microversion, and forwards to your Ironic.
resource: oci://ghcr.io/krateo-blueprints/charts/baremetal-host
tags: [ironic, keystone, runbook, real-ironic, proxy]
timestamp: 2026-08-11T00:00:00Z
---

# Quickstart: run the operator against a real Ironic API

The operator is the same as in the local (fake) env — only the endpoint behind the in-cluster
`ironic` Service changes. For any real, Keystone-protected Ironic (on-prem, hosted-private, your
own openstack-helm/devstack deployment, etc.) a small **auth proxy** authenticates with your
`clouds.yaml`, injects a fresh `X-Auth-Token` + microversion, and forwards to your Ironic. The
operator's config (OAS endpoint, RestDefinitions, chart) is unchanged.

## Prerequisites

- A real OpenStack with the **`baremetal` (Ironic) service in the Keystone catalog** and
  admin/operator access (your own cluster, a lab, an on-prem deployment, a hosted-private cloud).
- A `clouds.yaml` for it (Horizon → API Access, the provider portal, or your installer's output).
  Note the cloud name (top-level key under `clouds:`).
- Docker + kind + helm + kubectl (the operator runs in a local kind cluster).

## Steps

### 1. Bring up the operator (kind + Krateo + RestDefinitions)

```bash
make local-up
```

This installs Krateo (KOG `oasgen-provider` + composition `core-provider`) and applies **four**
RestDefinitions, generating `BaremetalNode`, `Port`, `NodeProvision` and `NodePower`. It also
starts the local fake Ironic — that's fine; the next step repoints the `ironic` Service away
from it.

(Corrected 2026-11-05: this said "both RestDefinitions (`Node` CRUD + `NodeProvision`)". There
are four, and the node kind is `BaremetalNode` — a reader following the old text would apply a
`kind: Node` that nothing serves, which is the same defect the quickstart had.)

### 2. Point the operator at your real Ironic

> **Changed 2026-11-04: there is no auth proxy any more.** `make keystone-up`,
> `make keystone-down` and `manifests/keystone-ironic-proxy.yaml` were deleted.
> Authentication is now declared on the **Configuration CR** —
> `spec.authentication.apiKey.tokenRef` pointing at a Secret — and the
> `keystone-token-refresher` CronJob keeps that token current. Nothing sits in
> the request path.

Two independent things, and keeping them separate is the point:

**Auth** is a Configuration CR plus a token Secret. See
`manifests/portconfiguration-ironic.yaml` for the shape; every Configuration
kind needs its own `authentication` block.

**Reachability** is whatever backs the in-cluster `ironic` Service. The
operator always addresses `ironic.openstack.svc.cluster.local:6385` and cannot
tell the difference between an in-cluster Ironic, a remote one across a VPN, or
a proxy — verified by rendering the charts, which contain no mention of any
transport. Switch it explicitly:

```bash
make ironic-backend-direct   # the endpoint IRONIC_BACKEND_MANIFEST declares
make ironic-backend-local    # the in-cluster fake Ironic, for local testing
```

### 3. Verify reachability — and auth separately, because they fail differently

```bash
KCTL="kubectl --kubeconfig local/kubeconfig.ironic-kog --context kind-ironic-kog"
$KCTL -n openstack port-forward svc/ironic 6385:6385 &

# Reachability only. No token, so a Keystone-protected Ironic answers 401 --
# which is a PASS for this step: it proves the request arrived.
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "X-OpenStack-Ironic-API-Version: 1.109" http://localhost:6385/v1/nodes

# Auth. The token is the one the keystone-token-refresher CronJob maintains.
TOKEN=$($KCTL -n openstack get secret ironic-keystone-token -o jsonpath='{.data.token}' | base64 -d)
curl -s -H "X-Auth-Token: $TOKEN" \
  -H "X-OpenStack-Ironic-API-Version: 1.109" http://localhost:6385/v1/nodes | jq '.nodes | length'
```

**`200` on the first call without a token means your Ironic is noauth**, not that auth works.
`401` means reachable and protected — then the second call must return a node count.

Two corrections here, 2026-11-05, both of which made this step unable to verify what it claimed:

- the title said "through the proxy"; **there is no proxy** (see §2);
- it sent **no token** while claiming a `200` "confirms Keystone auth". Against a
  Keystone-protected Ironic that request 401s, so the documented pass condition could only be
  met by an Ironic that was not checking credentials at all;
- the microversion was `1.81`, below the **1.83** floor this system needs. `parent_node` and
  `include_children` require ≥ 1.83, and an unset or too-low microversion is what produced
  `406 Request not acceptable` on every Observe for five days. Use `1.109`.

### 4. Provision (real hardware values)

The fake driver from the local env won't drive real hardware. Edit
`manifests/baremetallifecycle-example.yaml` (or your composition instance) for your node:

```yaml
spec:
  nodeName: server01
  driver: redfish                 # or ipmi  (must be enabled on the cloud)
  driver_info:                    # real BMC details
    redfish_address: https://<bmc-ip>
    redfish_username: <user>
    redfish_password: <pass>
    redfish_verify_ca: false
  ports:                          # PXE NIC MAC(s)
    - address: "aa:bb:cc:dd:ee:ff"
  instance_info:                  # OS image to deploy
    image_source: http://<http-or-glance>/image.qcow2
    image_checksum: <sha256-or-url>
```

Then drive it through composition-dynamic-controller:

```bash
make composition-up      # host the chart + install the CompositionDefinition
make composition-demo    # create the BaremetalLifecycle instance
# watch it walk enroll -> manage -> manageable -> provide -> available -> deploy -> active:
$KCTL -n openstack get node.baremetal.ogen.krateo.io server01 -o jsonpath='{.status.provision_state}'
```

The composition renders one `NodeProvision` CR per state (selected by `lookup` of the node's
current `provision_state`); cdc re-evaluates each reconcile until the node is `active`.

## Against a Krateo-blueprint Ironic (in-cluster Keystone) — recipe

Validated against an Ironic deployed in-cluster from the
[Krateo OpenStack blueprint](https://github.com/krateo-blueprints/krateo-openstack-blueprint) `ironic`
component (Keystone-protected, `ironic-api` Service on `:6385`). The same proxy is used, pointed at
the in-cluster Keystone. Three blueprint-specific points:

1. **clouds.yaml for the in-cluster Keystone** (internal interface, admin), as a Secret the proxy
   mounts. Set `OS_INTERFACE=internal` and override discovery with
   `IRONIC_ENDPOINT=http://ironic-api.openstack.svc.cluster.local:6385`:
   ```yaml
   clouds:
     osh:
       auth:
         auth_url: http://keystone-api.openstack.svc.cluster.local:5000/v3
         username: admin
         password: password
         project_name: admin
         user_domain_name: Default
         project_domain_name: Default
       region_name: RegionOne
       identity_api_version: 3
       interface: internal
   ```

2. **Use a distinct proxy Service name — do NOT reuse `ironic`.** The openstack-helm ironic chart
   already owns a Service named `ironic` (its ingress, selector `app: ingress-api`). The OAS server
   URL defaults to `http://ironic.openstack.svc.cluster.local:6385`, so the operator would hit that
   dead service — and a Krateo Composition re-applies (reverts) any repoint of it every reconcile.
   Expose the proxy as e.g. `ironic-kog-proxy` and set `servers[0].url` in both OAS files to it,
   then **regenerate** the RestDefinitions (the server URL is baked at generation, not read live):
   `kubectl delete -f manifests/restdefinition-*.yaml`, delete the `nodes`/`nodeconfigurations`/
   `nodeprovisions` CRDs, restart `oasgen-provider`, re-apply.

3. **`enroll` (and beyond) needs a running conductor.** Ironic refuses `POST /v1/nodes` with `503`
   ("Resource temporarily unavailable") when no conductor is registered (`GET /v1/drivers` empty).
   On a cloud-only cluster the conductor cannot start (it hard-fails at pxe-init without a
   provisioning NIC), so node creation needs real provisioning infra — the operator → proxy → Ironic
   path itself (routing, auth, microversion, body) is fully exercised up to that point.

## Self-hosted noauth Ironic (Bifrost) — variant

If your Ironic is standalone/noauth (e.g. Bifrost), you don't need the proxy. Instead route the
`ironic` Service at the external endpoint (a headless Service + manual EndpointSlice, or an
`ExternalName` Service) and ensure a microversion header is supplied (the local env's nginx
sidecar does this; reuse that pattern pointing at the external host). The operator stays unchanged.

## Notes / gotchas

- The microversion header is mandatory for Ironic writes. **There is no proxy injecting it**
  (that was the deleted `keystone-ironic-proxy`): each OAS document declares
  `X-OpenStack-Ironic-API-Version` as a *required* parameter with a `1.109` default, so the
  apiserver stamps it onto every generated CR and RDC puts it on the wire. Anything below
  **1.83** breaks `parent_node` and `include_children` with a `406`.
- The `Node` RestDefinition has **no `update` verb** on purpose (Ironic PATCH is JSON-Patch-only
  and clears `instance_info` during cleaning, which would freeze the Node CR). Node spec is set at
  create; lifecycle is via `NodeProvision`.
- Progression is paced by KOG's Node-controller status resync (tens of seconds per state) plus the
  real deploy time (minutes).
- App credentials: if your cloud supports Keystone application credentials, a `clouds.yaml` with
  `auth_type: v3applicationcredential` works with the proxy and is safer than your password.
