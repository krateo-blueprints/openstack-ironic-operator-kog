---
type: Architecture
title: This composition vs metal3 BareMetalHost
description: An evidence-backed comparison of the Krateo Ironic composition against metal3's baremetal-operator — lifecycle parity, operability trade-offs, and where each approach wins.
resource: oci://ghcr.io/krateo-blueprints/charts/baremetal-host
tags: [ironic, metal3, comparison, architecture, baremetal]
timestamp: 2026-08-11T00:00:00Z
---

# Comparative analysis: this composition vs metal3 BareMetalHost

Honest, evidence-backed comparison. We claim parity on the core lifecycle and
better operability in specific areas; metal3 wins in others. Choose accordingly.

## TL;DR

| Dimension | Krateo composition (this repo) | metal3 baremetal-operator |
|---|---|---|
| Core lifecycle parity | ✅ enroll → inspect → deploy → active → undeploy → available | ✅ same |
| Implementation | `ironic-operator-kog` (~250 lines of OAS + RestDefinitions, generates CRDs + controllers) + a Krateo blueprint (~600 lines of Helm templates) reconciled by core-provider | Go operator, ~50k LoC |
| Custom logic to extend | Edit YAML templates, bump chart version | Write Go, rebuild operator |
| Stuck-state recovery time | **45 s** (verified gap 4 v0.3.4) | minutes-to-hours (IPA timeout or operator) |
| Configdrive pipeline | ✅ validated end-to-end via SSH (gap 1+2) | ✅ |
| Concurrent fleet operations | ✅ no contention, ~17 min for 2 (gap 5) | ✅ |
| Typed BIOS/RAID/firmware specs | ❌ raw `cleanSteps` only | ✅ first-class `spec.firmware`, `spec.raid` |
| Rescue mode | ❌ (not exposed; Ironic supports it) | ✅ `spec.rescued`, rescue ramdisk |
| Secret refs for userData/networkData | ❌ inline only | ✅ `spec.userDataName` → Secret |
| Cluster-API integration | ❌ | ✅ via `spec.consumerRef` |
| Operational footprint | Krateo + Ironic | metal3 (+ IronicCore) |
| Track record | New (2026) | Mature (since 2018) |

## Architectural difference, named precisely

metal3's `baremetal-operator` is a Go controller (~50k LoC, `vendor/` tree included) that
encodes Ironic's state machine in Go and reconciles `BareMetalHost` CRs.

We don't have a Go controller. We have **two Krateo layers stacked**:

| Layer | What it is | What it does | Lines of code we wrote |
|---|---|---|---|
| 1 — `ironic-operator-kog` (this repo's "primitives" side) | OAS specs → KOG `RestDefinition`s → auto-generated CRDs + `rest-dynamic-controller`s | Each CR (Node, Port, NodeProvision, NodePower, etc.) maps to ONE Ironic REST call. No state machine here, just a 1:1 declarative wrapper. | ~250 lines (OAS + RestDefinitions); the controllers + CRDs are generated |
| 2 — Krateo blueprint (the FSM driver) | A `baremetal-host` Helm chart referenced from a `CompositionDefinition`, continuously reconciled by `core-provider` + per-version `composition-dynamic-controller` (cdc) | cdc renders the chart on every reconcile. Templates use Helm `lookup` to read live Ironic state via Layer 1's Node CR, then render the appropriate Layer-1 primitive CRs for the current transition. Re-rendering is what makes it a *machine*, not a static template. | ~600 lines (chart templates + values + schema) |

So the state machine is **declared in 7 Helm template files**
(`charts/baremetal-host/templates/transition-*.yaml`), each one an edge of the FSM gated on
a Helm `lookup` of the current Ironic state. When the gate matches, that template renders
a `NodeProvision` (or `NodePower`) primitive; KOG's `rest-dynamic-controller` fires it
once as a single PUT against Ironic. State changes; next cdc reconcile reads it via
`lookup`; renders the next transition. No Go code orchestrates this — core-provider does
its standard "render + diff + apply" job, the chart templates *are* the FSM.

Effect, in operational terms: changing how a transition fires (e.g., gap 4's widened
`transition-undeploy` gate that now recovers from `wait call-back`) is a 2-line YAML edit
+ a chart-version bump + `helm package` + `kubectl apply -f CompositionDefinition`. The
equivalent change in metal3 means a Go PR, bake new images, rolling upgrades.

## Where we're at parity (no compromise)

These are validated by the tests in `docs/TEST-PLAN.md`.

| What | metal3 | Ours | Evidence |
|---|---|---|---|
| `spec.online` continuous power enforcement | yes | yes | Gap 4 — chart's NodePower rename pattern survives BMC flaps |
| `spec.image` (source, checksum, checksumType, format) | yes | yes (+ `root_device` passthrough) | Gap 1+2 |
| Configdrive (userData / networkData / metaData) | inline + secret refs | inline | Gap 1+2 — full SSH verification on the deployed OS |
| Inspection toggle | `spec.preInspectionUserData` (newer) | `spec.enableInspection` | Test plan baseline |
| Maintenance flag | yes (annotation + spec) | `spec.maintenance` | Render-validated |
| External-provisioned / paused | `spec.externallyProvisioned` + annotation | `spec.detached` | Render-validated |
| Multi-blade concurrent provisioning | yes | yes — no contention | Gap 5 — both blades active simultaneously |

## Where we are operationally better, with evidence

### 1. Sub-minute undeploy when cleanup is skippable

| | metal3 | Ours |
|---|---|---|
| Mode | `automatedCleaningMode: disabled` | `undeployMode: none` |
| Mechanism | sets `node.automated_clean=false`, undeploy skips IPA boot | same effect via `spec.automated_clean: false` on the Node CR |
| Validated time | not measured in their tests | **6 seconds** (gap 11) vs 4m11s with full cleaning |

### 2. Stuck-state recovery without operator intervention

Gap 4 demonstrated this empirically. A power flip mid-deploy in metal3 typically leaves
the host in `provisioning` with `Ironic state: wait call-back` until the IPA agent
timeout (30-60 min on default Ironic config). Operator intervention required.

v0.3.4 of this chart treats `spec.undeploy: true` as a state-agnostic release signal —
the widened transition-undeploy gate fires from `active`, `deploy failed`, `clean failed`,
`wait call-back`, *and* `deploying`. **End-to-end recovery clocked at 45 seconds**
(force-off mid-deploy → patch spec → Ironic at available). See test 4.1 status block.

### 3. Image swap = orchestrated undeploy + deploy, no rebuild surface

metal3 supports `target: rebuild` for hot image swap. We dropped it (v0.3.3 cleanup, see
`docs/TEST-PLAN.md` gap 1+2 status). Reason: rebuild bypasses the cleaning pass, which is
what most production environments actually want (tenant data must not leak between
images). Image swap in our model:

```bash
kubectl patch bh blade05 --type=merge -p='{"spec":{"undeploy":true}}'         # wait for available
kubectl patch bh blade05 --type=merge -p='{"spec":{"image":{"source":"new.qcow2"},"undeploy":false}}'
```

Two steps, but unambiguous and the cleaning pass enforces tenant isolation. metal3's
rebuild is faster but more dangerous in shared-tenant deployments.

### 4. Cross-version CRD pattern with vacuum storage

The composition-pattern's per-version CRD with `vacuum` storage and conversion webhook
lets you bump chart versions without breaking existing CRs. metal3's BMH CRD is the
output of code generation — a major version bump means rolling the operator. Test plan
gap 6 documents the vacuum-storage pattern; it's not faked.

### 5. Composition-driven changes ship in minutes, not days

You can verify this on the git log: the v0.3.4 fix from gap 4 (widen the undeploy gate)
is a single-file edit, packaged + uploaded in seconds, redeployed via
`kubectl apply -f compositiondefinition`. Same change to metal3: PR, review, release
cut, image push, rolling upgrade.

## Where metal3 is better

Honesty matters here.

### 1. Typed BIOS / RAID / firmware specs

metal3:
```yaml
spec:
  firmware:
    simultaneousMultithreadingEnabled: true
    sriovEnabled: true
  raid:
    hardwareRAIDVolumes:
      - sizeGibibytes: 1024
        level: "1"
        physicalDisks:
          - disk0
          - disk1
```

Ours: you have to write the raw Ironic clean_steps in `spec.cleanSteps`. Less ergonomic
for operators not fluent in Ironic's clean-step schema.

### 2. Rescue mode

metal3 exposes `spec.rescued` + a rescue ramdisk URL. Ironic supports this (`target: rescue`)
and the chart could add `transition-rescue.yaml` (~80 lines), but it's not done. Use case:
ssh into a stuck blade's rescue env to debug.

### 3. Secret references for sensitive fields

metal3 reads `userData`, `networkData`, and `metaData` from `Secret` CRs via name
references (`spec.userDataName`). Rotating the secret rotates the cloud-init payload
without re-applying the BMH. Ours is inline only; rotating means editing the BH manifest.

### 4. Cluster API integration

metal3 plugs into Cluster API via `spec.consumerRef` — Machine objects own BMHs, Cluster
API drives the bare-metal lifecycle. Ours has nothing in this space; you'd build it on
top.

### 5. HardwareData CR with rich inventory

metal3 publishes a separate `HardwareData` CR per BMH with fully structured inventory
(CPUs, NICs with MAC + speed, disks with WWN + sizes, etc.). We surface
`status.properties` (cpu_arch + a few capabilities) and `status.inspection_finished_at`
on the Node CR. The Ironic inventory IS available via the `properties` field; we just
don't flatten it into a dedicated CR.

### 6. Pre-provisioning network data

metal3 has `spec.preprovisioningNetworkDataName` — a separate config drive applied to
the IPA agent ramdisk *before* the deployed OS. Useful for blades whose deploy network
needs a specific config (no DHCP on the wire, static IPs, etc.). We don't expose this;
the lab uses DHCP-on-the-wire so we never needed it.

### 7. Reboot + re-inspection annotations

metal3 supports `reboot.metal3.io/<UID>` and `inspect.metal3.io` annotations for
imperative one-shot ops. Useful when the declarative model can't model the request
cleanly (debugging a flaky host, getting fresh hardware data after a HW swap).

### 8. Mature ecosystem

metal3 has been in development since ~2018, ships in OpenShift assisted-installer, has a
contributor community, has Helm/operator hub presence, has logs of production incidents
and fixes. We're net-new (2026), tested against one lab.

## When to choose which

**Choose this composition when:**
- You're already invested in Krateo / GitOps / Helm-as-control-plane.
- You want to extend or modify the lifecycle without writing Go.
- You need sub-minute undeploy or fast recovery from stuck deploys.
- Your BIOS/RAID/firmware config is static and clean_steps is acceptable.
- You're integrating into a custom workflow, not Cluster API.
- You like that the entire state machine is in 7 readable YAML files instead of a Go
  codebase.

**Choose metal3 when:**
- You're using Cluster API and need `spec.consumerRef`.
- You need rescue mode out of the box.
- Typed BIOS/RAID/firmware abstractions matter operationally.
- You need secret refs for credential rotation without manifest edits.
- You want a mature, battle-tested codebase with a deep contributor base.
- You're shipping into OpenShift / OKD.

## Closing note

If you'd told us six months ago we'd be at functional parity for the core lifecycle —
seven Helm files vs a Go operator — we'd have raised an eyebrow. The composition pattern
turned out to be the right shape for Ironic specifically because Ironic *is* a state
machine with a small API surface and well-defined transitions. metal3's Go controller is
elegant, but most of its complexity is from features we either don't need (Cluster API
integration) or absorbed differently (cleanSteps instead of typed firmware specs).

This isn't an "either/or" closing. The two solutions cover the same ground from different
ends. If your team's culture is "all declarative, all GitOps, modify via PRs to YAML,"
this one fits. If your team's culture is "Go operators, custom controllers, OpenShift,"
stick with metal3.

## Validation references

All "verified" claims in this document are backed by:
- `docs/TEST-PLAN.md` (10 of 11 gaps PASS, the 11th is the 24h soak still running)
- `docs/ORPHAN-RECOVERY.md` (real-world recovery procedure, validated twice)
- Commits between `a425e8b` and `41d4ff9` on the repo
