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

This installs Krateo (KOG `oasgen-provider` + composition `core-provider`) and applies both
RestDefinitions (`BaremetalNode` CRUD + `NodeProvision` action). It also starts the local fake Ironic —
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
$KCTL -n openstack get baremetalnode.baremetal.ogen.krateo.io server01 -o jsonpath='{.status.provision_state}'
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
- The `BaremetalNode` RestDefinition has **no `update` verb** on purpose (Ironic PATCH is JSON-Patch-only
  and clears `instance_info` during cleaning, which would freeze the BaremetalNode CR). Node spec is set at
  create; lifecycle is via `NodeProvision`.
- Progression is paced by KOG's Node-controller status resync (tens of seconds per state) plus the
  real deploy time (minutes).
- App credentials: if your cloud supports Keystone application credentials, a `clouds.yaml` with
  `auth_type: v3applicationcredential` works with the proxy and is safer than your password.

## Running WITHOUT the proxy (measured 2026-10-29, against the real lab)

Everything the keystone proxy used to do has a home in plain oasgen-provider except one thing,
and that one is a property of the Ironic you are talking to rather than of this repo.

| proxy job | replacement | status |
|---|---|---|
| Keystone auth | `apiKey` security scheme in the OAS + `spec.authentication.apiKey.tokenRef` on each `<Kind>Configuration` | works |
| microversion header | **static per-verb `verbsDescription[].headers`** | works |
| PATCH -> RFC-6902 | `requestTransform` (gojq) | blocked, oasgen #308 |
| de-chunking request bodies | nothing | blocked, see below |

### The microversion does NOT go in the Configuration

`spec.configuration.header` can never deliver it: oasgen generates the property as `header`
(singular, from the OAS `in` value) and rest-dynamic-controller reads `headers` (plural), so the
lookup misses silently (oasgen #304). `path` and `query` work; `header` and `cookie` cannot.

The failure is invisible and expensive. With no microversion, Ironic negotiates its MINIMUM (1.1)
and serves the 2014 API rather than erroring: node lookup by name 404s (names need >= 1.5) and any
body using a modern field is rejected (`parent_node` needs >= 1.83). Both look like bugs in this
chart. Pin the version with static per-verb headers instead -- see `manifests/restdefinition-node.yaml`.

`verbsDescription` is MUTABLE, so this migration can be done in place. `configurationFields` is
immutable: changing it means deleting and recreating the RestDefinition, which cascades the
generated CRD and fires a real `DELETE /v1/nodes/{uuid}` at every live CR. On a real lab that
de-enrols physical blades. If you must recreate, point the data path at a disposable Ironic first.

### Adding a security scheme makes `configurationRef` required

Once an OAS declares a security scheme, oasgen marks `spec.configurationRef` required on every
generated kind. The chart's transition CRs then fail with `spec.configurationRef: Required value`,
which surfaces as a Helm render error on the composition and points nowhere near the cause. All
transition templates set it, and `manifests/transitionconfiguration-ironic.yaml` supplies the
`NodePower` / `NodeProvision` singletons.

### Your Ironic must accept chunked request bodies

rest-dynamic-controller builds `http.Request` as a struct literal, so `ContentLength` is 0 with a
non-nil body and Go falls back to `Transfer-Encoding: chunked` (oasgen #307, PR #310).

Most Ironic deployments read that fine. An older one fronted by a bare eventlet WSGI does not --
it hands the application an empty stream, and Ironic then reports a required field as missing even
though it is on the wire:

    PUT /v1/nodes/<name>/states/power
    Transfer-Encoding: chunked
    {"target":"power on"}
    -> 400  "Missing mandatory parameter: target"

Measured both ways on the same RDC, same relay, differing only in the server: a newer Ironic
parsed the body (it rejected `parent_node` by value, which requires having read it), the lab's
older one did not. Fingerprint: a healthy one answers `Server: ironic_api`; the affected one sends
no `Server` header and `Connection: close`, and caps out at a lower microversion (1.111 vs 1.115).

If writes fail this way, fix the Ironic front end -- the chunked framing is legal HTTP and any
client that omits Content-Length will hit it. Do NOT reintroduce a de-chunking proxy.

### Masked secrets: drop them from the observed body

Ironic masks `driver_info.redfish_password` as `******` on read, so the CR value and the observed
value can never converge and RDC re-PATCHes forever (oasgen #309). Affects any API that masks a
write-only field on read.

Fixed here with a `responseTransform` on BOTH observe verbs of
`manifests/restdefinition-node.yaml`:

    responseTransform:
      inline: 'if (.driver_info | type) == "object" then .driver_info |= del(.redfish_password) else . end'

DELETING the key is what works, and it is worth understanding why nothing softer does. The drift
comparison walks the SPEC's keys and looks each one up in the response; a key present with a
different value is a difference, a key ABSENT from the response is skipped entirely. A
responseTransform cannot substitute the real value instead -- it sees only the response, never the
spec -- so removal is the only reshaping available, and it is also the honest one: drift on a field
the API refuses to disclose is not detectable by any client, so there was never a signal to lose.
Changing the password still works; it ships on the next update like any other `driver_info` field,
it just cannot be confirmed by reading it back.

Both verbs need it because exactly one of them produces the observed body -- `get` once the
resource is known, `findby` before that -- and each normalizes with only its own transforms.
`findby` applies it per list item before matching, so the shape is the same in both cases.

Symptom before the fix, and the thing to look for: `Ready=False`, reason `DriftNotConverging`,
"the update has been issued 783 times without resolving it". 0.41.0 gives up rather than looping
forever and its message names this remedy. Measured here: applying it flipped `Ready` to True on
the next reconcile, 14 seconds later.

### An EMPTY object in the response is not an absent one

RDC's drift comparison walks the CR's keys and looks each one up in the response, skipping any
that are absent -- correct, and what the masked-password fix above relies on. But when the
response holds the PARENT as an empty object, the recursion looks up every child in `{}`, finds
none, skips them all, and returns equal.

Ironic answers `"instance_info": {}` for a node enrolled out of band. The CR carried
`instance_info.image_source` and `image_checksum`; no drift was detected, no PATCH was issued, and
the node reported `Synced=True` having never received the desired state. It surfaced two layers
away, as a deploy rejected for `Missing are: ['instance_info.image_source']` -- which reads like a
chart bug and says nothing about comparison.

This is the map-shaped twin of a slice defect RDC already fixed (#76): a CR key holding an explicit
`[]` is the user declaring the list empty, and treating present-and-empty as "no opinion" removes
the only way to say that. `compareSlices` compares lengths for exactly that reason; the map branch
never got the same treatment. Filed as oasgen #322, patched locally at the ENTRY to
`CompareExisting` rather than per call site -- there are three, and a per-site guard reproduces the
very asymmetry that caused the bug.

The escape hatch still composes: a `responseTransform` that DELETES a key makes it absent, not
empty, so a deliberately unobservable field stays silent while an observably-empty one now speaks.

### Ironic HIDES CHILD NODES from GET /v1/nodes

Microversion 1.83 introduced parent/child nodes, and the node list omits children by default --
they are treated as nested resources rather than machines in their own right. blade01 sets
`parent_node: enclosure01`, so every `findby` response contained exactly one node, `enclosure01`.

No identifier scheme can survive that: the resource is not in the list to be matched. Add
`include_children=true` as a static per-verb query (see `manifests/restdefinition-power.yaml`).

Declare it in the RestDefinition's `queries`, NOT in the OAS. An OAS parameter can materialise as a
generated spec field, and the CRD surface is the expensive thing to get wrong; `ValidateRequest`
only enforces that REQUIRED parameters are present, so an undeclared query is injected without
complaint.

Diagnostic note: RDC logs `"External resource is up to date"` on BOTH the success path and the
Pending early-return, so it cannot distinguish "compared and found no difference" from "did not
compare" (oasgen #323). The signal that does distinguish them is whether `additionalStatusFields`
got populated -- if `findby` had matched, they would have been.

### An action CR must be declarative, or it can never report Ready

The fire-once pattern (one CR per transition, PUT fires once) works by writing `findby` so it NEVER
matches: every new CR is "not found", so it creates, and the 202 sets `Pending` which makes later
Observes report exists. The catch is that nothing ever clears `Pending` -- the guard returns
exists+up-to-date WITHOUT touching the condition.

That is harmless for a CR the chart removes once its transition completes. It is fatal for one that
lives as long as the host: composition-dynamic-controller aggregates child readiness, so a single
permanently-Pending child holds the whole composition at `Ready=False` even with the host fully
provisioned. Measured: 51 reconciles, zero transitions.

`NodePower` is therefore DECLARATIVE (see `manifests/restdefinition-power.yaml`): `findby` lifts the
node's observed power state onto `target`, the same key the spec uses, so the drift comparison
answers the question the resource actually asks -- "is the node in the target state?" -- and an
`update` verb re-issues the PUT when it is not. Continuous enforcement, self-healing after
out-of-band power loss, and the CR reaches `Ready=True`.

`(.target_power_state // .power_state)` is load-bearing: while Ironic is mid-action
`target_power_state` already holds the requested value, so the comparison sees convergence and does
not issue a second PUT. `tolerateCodes: [409]` covers the benign race where a power action is
already pending.

`NodeProvision` stays fire-once ON PURPOSE -- see the header comment in
`manifests/restdefinition-provision.yaml` for why converting it is not the same easy win: `target`
there is a VERB, and the verb->state mapping is not injective (`provide` and `deleted` both settle
on `available`; `manage`, `clean` and `inspect` all settle on `manageable`), so a state->verb
transform cannot tell "clean this manageable node" from "nothing to do".

### Renaming a chart-managed child can freeze the composition's Ready

Not an Ironic issue, but it will be met by anyone changing these charts. cdc records the child
inventory in `status.managed` only on its Create and Update phases; Observe recomputes `Ready` FROM
that list and never refreshes it. If the Update that renames a child is interrupted -- here, a
CompositionDefinition version migration killed the old controller mid-upgrade -- `managed` keeps the
old name, readiness is computed against a `NotFound`, and `digest == previousDigest` means no
further Update is ever dispatched. Permanent by construction.

Symptom: `Ready=False`, "N of M managed children are not ready: <a resource that does not exist>",
while `Synced` keeps refreshing and every child is actually `Ready=True`. Reported to core-provider.
