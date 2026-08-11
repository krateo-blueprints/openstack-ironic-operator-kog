---
type: Log
title: openstack-ironic-operator-kog — log
description: Curated chronological history of the OpenStack Ironic KOG blueprint — notable changes and decisions, not a generated changelog.
resource: oci://ghcr.io/krateo-blueprints/charts/baremetal-host
tags: [ironic, log, history]
timestamp: 2026-08-11T00:00:00Z
---

# Log

Curated history; release notes live in GitHub Releases.

## 2026-08-11 — Documentation Standard adoption

The repo adopts the Krateo Documentation Standard: the invariant docs bundle
(`index`/`overview`/`usage`/`configuration`/`api`/`examples`/`release`/`log` + `llms.txt`), a
runnable `examples/baremetal-lifecycle`, and the shared `lint-docs` check wired into a new
`lint.yaml`. The pre-existing design/runbook docs (KUBERNETES-CLUSTER, USER-GUIDE, VS-METAL3,
BIFROST, REAL-IRONIC, E2E, TEST-PLAN, ORPHAN-RECOVERY, RUNBOOK-ETCD-RESTORE, quickstart, the
two KUBERNETES-CLUSTER design notes) gain OKF frontmatter and remain the deep-dive references.

## kubernetes-cluster v0.11.0 — external etcd

HA clusters (cpNodes > 1) run etcd as a host **systemd unit** on each control plane,
bootstrapped from a static `initial-cluster=` list, so kubeadm sees etcd as *external* and
skips the join-time etcd-member-dance that broke v0.10.15 HA with an irrecoverable
ghost-member loop. Single-CP path unchanged (stacked etcd). New required HA value
`controlPlane.nodes[].oobIp` (etcd peer URLs are hard-pinned to the OOB NIC). Design:
[KUBERNETES-CLUSTER-V0.11.0-DESIGN.md](./KUBERNETES-CLUSTER-V0.11.0-DESIGN.md); restore
runbook: [RUNBOOK-ETCD-RESTORE.md](./RUNBOOK-ETCD-RESTORE.md).

## baremetal-host — the unified single-CRD composition

`baremetal-host` absorbs the `baremetal-discovery` + `baremetal-lifecycle` split into one
metal3-shaped `BaremetalHost` resource with phase gates
(`registering → (inspecting → ready)? → available → (provisioning → provisioned)`), adding
maintenance, detached, clean steps, config-drive and a spec-field `undeploy` (rather than a
deletion side-effect, because cdc's delete path runs `helm uninstall` with no re-render). The
split charts remain for callers wanting the narrower surface.

## The core design

Two decisions define the blueprint:

- **The composition is the orchestrator.** The Ironic provision-state machine is modeled as
  one custom resource per state, selected on every reconcile with Helm `lookup`; cdc's
  repeated reconciles walk the machine one transition at a time. No CLI, no middleware.
- **No hand-written operator.** KOG (`oasgen-provider` + `RestDefinition`s) generates the
  Ironic API clients (`Node`, `NodeProvision`, `Port`, `NodePower`, ...) from the OpenAPI
  specs in `oas/`; `core-provider` generates the composition controllers from the charts.
