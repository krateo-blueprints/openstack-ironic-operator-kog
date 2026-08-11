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

This installs Krateo (KOG `oasgen-provider` + composition `core-provider`) and applies both
RestDefinitions (`Node` CRUD + `NodeProvision` action). It also starts the local fake Ironic —
that's fine; the next step repoints the `ironic` Service away from it.

### 2. Point the operator at your real Ironic (Keystone auth proxy)

Put your file at `./clouds.yaml` (gitignored), then:

```bash
make keystone-up CLOUDS_FILE=clouds.yaml OS_CLOUD=<your-cloud-name>
```

This creates a Secret from `clouds.yaml`, deploys the proxy (`scripts/keystone-ironic-proxy.py`
in the openstack-client image, which uses `openstacksdk` to auto-refresh the Keystone token and
discover the `baremetal` endpoint), and repoints the `ironic` Service at it. The proxy listens on
the same port the operator already targets, so nothing else changes. `make keystone-down` switches
back to the local fake Ironic.

### 3. Verify connectivity through the proxy

```bash
KCTL="kubectl --kubeconfig local/kubeconfig.ironic-kog --context kind-ironic-kog"
$KCTL -n openstack port-forward svc/ironic 6385:6385 &
curl -s -H "X-OpenStack-Ironic-API-Version: 1.81" http://localhost:6385/v1/nodes | jq .
```

A `200` with your real nodes (or an empty list) confirms Keystone auth + Ironic reachability.

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

- The microversion header is mandatory for Ironic writes; the proxy injects `1.81` (override with
  `IRONIC_API_VERSION`).
- The `Node` RestDefinition has **no `update` verb** on purpose (Ironic PATCH is JSON-Patch-only
  and clears `instance_info` during cleaning, which would freeze the Node CR). Node spec is set at
  create; lifecycle is via `NodeProvision`.
- Progression is paced by KOG's Node-controller status resync (tens of seconds per state) plus the
  real deploy time (minutes).
- App credentials: if your cloud supports Keystone application credentials, a `clouds.yaml` with
  `auth_type: v3applicationcredential` works with the proxy and is safer than your password.
