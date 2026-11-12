---
title: "Design note — requesting capacity with Ironic Allocation"
status: proposal
scope: workers
date: 2026-11-12
---

# Design note — requesting capacity with Ironic Allocation

**Status: a proposal for `workers` only.** The control-plane path is left as stated
open questions, because the evidence does not yet settle it and designing past the
evidence is how this repo has generated its retractions.

## The change in one line

Today `kubernetes-cluster` names hardware. With Allocation it would **request
capacity**:

```yaml
# today
workers:
  nodes:
    - nodeName: blade07
      nodeUuid: 2f05176e-…
      driver: redfish
      driver_info: {redfish_address: …, redfish_username: …, redfish_password: …}
      ports: [{address: "00:60:2f:…"}]

# with Allocation
workers:
  request:
    count: 3
    resourceClass: baremetal-worker
    traits: [CUSTOM_NVME]
```

This is how Nova consumes Ironic, and it is a materially better blueprint UX: a user
asks for three workers of a class instead of transcribing three Redfish addresses,
three sets of BMC credentials and three MAC addresses.

## What makes it feasible here, which the documentation denied

**This chart already resolves node identity at runtime.** Since v0.13.2 the bootstrap
control plane derives its own etcd address and publishes it to its own
`BaremetalNode` CR's `spec.extra.etcd_ip`, and the replicas read it from there. The
chart's own docs claimed the opposite — that `controlPlane.nodes[].oobIp` was
**required** and baked into the etcd PKI cert SAN at template time — in six places,
corrected in PR #54 after measuring **zero** template interpolations of `.oobIp`
against 15 of `.nodeUuid` as a control.

That matters because Allocation is inherently a runtime resolution: you ask, Ironic
picks, and the chosen node appears in `status.node_uuid`. A chart that resolved
identity only at render time could not consume it. **This one already crossed that
bridge for a different reason.**

## The real division of responsibility this forces

A worker entry today carries `nodeName`, `nodeUuid`, `driver`, `driver_info`,
`ports`, `parentNode` and `undeployMode` — **a hardware description**, because the
chart *enrols* the blade with Ironic.

**An allocated node is already enrolled.** Ironic cannot allocate a node it does not
know about, and `driver_info` and `ports` are properties that node already has. So
Allocation does not simplify the existing values — it **splits them in two**:

| | who does it | what it needs |
|---|---|---|
| **enrolment** | a separate, prior step — an operator, or a `baremetal-discovery` release | driver, driver_info, ports, resource_class |
| **consumption** | `kubernetes-cluster` | a resource class, a count, optional traits |

That split is the actual proposal. It is not a refactor of the worker list; it is a
statement that **registering hardware and claiming hardware are different jobs**, and
today one chart does both. Worth deciding on its own merits before any code.

One thing it *improves*: ports must exist before `target=active` or the blade
PXE-loops on "PXE boot failed" (see the per-MAC PXE timing note). An allocated node
already has its ports, so that ordering hazard largely disappears for workers.

## Shape of the Allocation itself

```
POST /v1/allocations   required: resource_class
                       optional: name, candidate_nodes, traits, extra
status                 uuid, state, node_uuid
state                  allocating -> active (node_uuid set) | error
```

So the flow is: render one `Allocation` CR per requested worker, wait for
`status.node_uuid`, then render the `BaremetalHost`/lifecycle CRs against the
resolved uuid. That is the same lookup-driven reconcile the provisioning state
machine already uses — each reconcile renders for the current state — so it needs no
new mechanism, only a new input.

## What is NOT settled, and why I am not proposing it

1. **Control planes.** `controlPlane.nodes[]` is an **ordered** list: index 0
   bootstraps. The upgrade flow targets one entry at a time
   (`controlPlane.upgrade.targetNode`), and recovery moves an entry into
   `controlPlane.recovery.failedNodes` and back "at any index but 0". Every one of
   those addresses a *specific* entry. An Allocation is a claim that must stay bound
   to the same node across reconciles, and I have not established what happens to
   index 0's identity if its Allocation is ever recreated. **Needs its own argument.**

2. **Allocation lifetime versus CR lifetime.** `name` is the identifier, so a
   re-created Allocation CR with the same name should re-find the same allocation —
   but if the allocation itself was deleted, Ironic would pick again, and the chart
   would silently be pointing at different hardware. The failure mode is a cluster
   that reprovisions onto a node nobody chose. Not measured.

3. **No `available` node exists here, so the success path is UNEXERCISED.** All eight
   lab blades are `active`, and Ironic allocates only from `available`. Driving an
   Allocation CR here produced `state=allocating` and would settle in `error` — which
   exercised create, get and delete, and says nothing about a successful claim. **Any
   confidence in this design is confidence in a path no measurement here has run.**
   Closing that needs either a ninth blade taken to `available`, or a clean cluster.

## Recommendation

Decide the **enrolment/consumption split** first, because it is a product question
and the rest follows from it. If the answer is that `kubernetes-cluster` should keep
enrolling its own hardware, Allocation buys little and should not be built. If
enrolment belongs elsewhere, the workers path above is small and the control-plane
questions become worth answering.
