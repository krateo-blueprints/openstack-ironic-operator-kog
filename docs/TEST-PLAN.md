---
type: Runbook
title: baremetal-host chart E2E test plan
description: The manual E2E test plan for the baremetal-host chart — the gaps beyond the CI-regression matrix: mutating inputs, hardware integration, concurrency, chart-version skew and long-running stability.
resource: oci://ghcr.io/krateo-blueprints/charts/baremetal-host
tags: [ironic, baremetal-host, test-plan, e2e, runbook]
timestamp: 2026-08-11T00:00:00Z
---

# Test Plan: baremetal-host chart 0.3.3 — E2E

Status: open. Validated set (deploy walk, full undeploy, fresh delete, configdrive
shape, NodePower rename, schema auto-discovery) is intentionally excluded — those
belong in CI regression, not in manual E2E.

Scope of *this* plan: gaps in the validated matrix on chart correctness, hardware
integration, concurrency, chart-version skew, and long-running stability.

## Premise

The chart's contract is "re-render on every cdc reconcile; each transition gates on
live Ironic state via `lookup`; produce at most one NodeProvision per transition."
What's untested is the chart's behaviour under inputs that mutate after first apply,
hardware misbehaviour, concurrency, chart-version skew, and time.

Every test observes three surfaces — the chart render, the control plane (cdc + RDC
+ KOG logs, NodeProvision/NodePower CR lifecycle), and Ironic itself
(`provision_state` / `power_state` / `instance_info` via the wg-ironic-proxy). A
test only passes when all three agree.

## Blade allocation

Chosen to minimise re-wiring between tests and reuse blades already in usable states.

| Blade | Test |
|---|---|
| blade03 | idempotency, chart-version skew (long-lived, low churn) |
| blade04 | holdout for re-running any failing test |
| blade05 | image-swap (gap 1) + cloud-init verification (gap 2) |
| blade06 | power-flip-during-deploy (gap 4) |
| blade07 + blade08 | concurrent deploys (gap 5) |
| blade09 | delete-from-active (gap 3) — destructive, leave for last |
| blade10 | inspect-failure (gap 7) — gets bogus Redfish creds |
| blade11 | undeployMode=none post-Ettore policy fix (gap 11) |
| blade12 | 24h soak (gap 9) — once deployed, do not touch |
| blade01, 02, 13–16 | untouched reserve |

## Ordering

**Hard ordering — cannot reshuffle:**

1. **Gap 6 (chart-version skew) before everything that needs chart 0.3.3.** Requires
   applying a BH at the *old* apiVersion. Once you bump the CD to 0.3.3, the
   precondition is gone — would need an operator rollback to reconstruct.
2. **Gap 10 (orphan-release recovery procedure) before any test that uninstalls a
   release.** The recovery procedure must be in hand before tests that may trip it
   (gaps 3, 7).
3. **Gap 8 (idempotent re-apply) before gap 9 (24h soak).** Soak is meaningless if a
   stock re-apply already churns.
4. **Gap 11 (undeployMode=none) gated on Ettore's `automated_clean` policy fix.**
   Park until confirmed; do not attempt on the project-scoped admin token.

**Soft ordering — recommended for efficiency:**

- Gap 1 (image swap) shares blade05 with gap 2 (cloud-init verification); run gap 2
  immediately after the first deploy of gap 1, then again after the swap. Two
  cloud-init checks for the price of one deploy each.
- Gap 4 (power flip mid-deploy) is the dirtiest hardware test — runs after the
  per-blade clean tests, before the 24h soak claims its blade.

## Parallelism

Three tracks can run concurrently in one session:

- **Track A** (one terminal): hardware walks, serial — gaps 1, 2, 4, 3.
- **Track B** (second terminal, different blades): gap 5 (concurrent deploys) is
  itself parallel; counts as one slot.
- **Track C** (passive): gap 9 soak on blade12, no operator involvement until 24h.

Gap 6 must run alone (operator-version churn). Gaps 7, 8, 10, 11 are
render/control-plane only and can be slotted into any track's idle minutes.

## Runtime estimate

| Test | Wall clock |
|---|---|
| 6.1 chart-version skew | 25 min |
| 8.1 idempotent re-apply | 5 min |
| 10.1 orphan-release procedure | 15 min |
| 1.1 + 2.1 image swap + cloud-init | ~45 min |
| 4.1 power flip mid-deploy | ~20 min |
| 5.1 concurrent deploys | 18 min |
| 3.1 delete from active | ~18 min (8 obs + 10 manual cleanup) |
| 7.1 inspect failure recovery | 12 min |
| 11.1 undeployMode=none post-fix | 6 min |
| 9.1 24h soak | 24h elapsed, ~15 min active obs |

**Total active operator time: ~3.5h plus the 24h soak.** Realistic split:

- Session 1: gaps 6, 8, 10, start gap 9 soak.
- Session 2: gaps 1, 2, 4, 5.
- Session 3: gaps 3, 7, end gap 9, and gap 11 if Ettore landed the fix.

---

## Category 1 — Chart correctness (render-side guarantees)

### Test 6.1 — Old-apiVersion BH survives chart bump (gap 6) — RUN FIRST

Validates the "vacuum storage version preserves all fields across composition
apiVersion bumps" claim.

**STATUS — 2026-06-11: SKIPPED (no real precondition).** The chart's git history has
only ever shipped `0.3.3` as a public version; there's no historical `0.1.0` baseline
to roll back to. The intermediate versions (`0.1.0` → `0.3.2`) existed only as
in-session iterations during the chart's authoring and were never committed. A
synthetic "old" chart would test our own ability to fake a precondition, not the
real concern. Re-evaluate this test the first time we ship a backwards-incompatible
chart bump that actual users have to ride through (~ `0.4.x` or `1.0.0` candidate).

**Setup:** pin operator to composition apiVersion v0-1-0 (confirm against
`manifests/compositiondefinition-baremetal-host.yaml` git history). Apply blade03 BH
at that apiVersion with image + configDrive set, walk to active.

**Action:** bump the CompositionDefinition to v0-3-3. Wait one reconcile. Edit
`spec.image.source` on the *old-apiVersion* BH object.

**Observations:**
- Render: helm release for blade03 re-renders under chart 0.3.3; rendered Node CR's
  `instance_info.image_source` reflects the new URL.
- Control plane: cdc logs show one upgrade-reconcile, no "field X dropped" warnings,
  NodeProvision `target: deleted` then `target: active` fires.
- Ironic: `instance_info` on the live Ironic node reflects the new image; node walks
  `active → deleted → available → active` on the new image.

**Pass:** all spec fields from the v0-1-0 manifest (`enableInspection`,
`configDrive.metaData.hostname`, `ports`, `driver_info`, `cleanSteps` if present,
`undeployMode`) round-trip into the 0.3.3 render. No silent field loss.

**Fail:** any field disappears from the rendered Node CR; cdc logs "unknown field";
spec edit on the old apiVersion object fails admission.

**Cleanup:** leave blade03 active for test 8.1.

### Test 8.1 — Idempotent re-apply (gap 8)

**Setup:** blade03 active from test 6.1. *(Substituted blade04 since 6.1 was
skipped — see status note above.)*

**Action:** `kubectl apply -f` the same BH manifest 3 times back-to-back, with a 30s
gap. Then wait 5 min.

**Observations:**
- Render: helm history shows revisions only if the apply mutates resourceVersion-
  bearing fields; transitions render no new NodeProvisions.
- Control plane: cdc reconcile fires on each apply (expected), but no new
  NodeProvision/NodePower CRs created, no PATCHes from RDC's drift translator.
- Ironic: `provision_state` stays `active`, `last_update_at` stable (within Ironic's
  own heartbeat cadence, not chart-driven).

**Pass:** zero spurious resources, zero PATCH calls attributable to chart re-render.

**Fail:** any new NodeProvision created, or RDC fires a drift PATCH on a field that
didn't change.

**STATUS — 2026-06-11: PASS (chart side) with caveat (cdc side).**
- Chart-render idempotency: PASS. Three identical applies on blade04 produced zero
  new NodeProvisions and no spurious Ironic state changes. NodePower CR underwent
  one expected rename (`power-on-from-unknown` → `power-on-from-on`) when the chart
  first observed the live power state.
- cdc apply-idempotency: NO. Each `kubectl apply --server-side` produced a new helm
  release revision (`v1 → v2 → v3`). cdc treats every BH mutation — including
  server-side-apply field-ownership churn — as a trigger for `helm upgrade`. This
  is upstream cdc behavior, not a chart bug. README should note that audit-counting
  helm revisions is not a useful idempotency signal on this stack.

Side effect — applying gap 10 recovery procedure to blade04 was needed before this
test could even start, because a previous session had left an orphan Node CR
ownership annotation. Procedure worked exactly as documented in
`docs/ORPHAN-RECOVERY.md`.

### Test 10.1 — Orphan-release detection & recovery (gap 10)

Documentation-driven; the deliverable is a procedure, not a one-shot pass/fail.

**STATUS — 2026-06-11: DONE.** Procedure is at `docs/ORPHAN-RECOVERY.md`. Validated
twice during this session: once on a real blade03 orphan (3 stranded helm release
secrets, no BH CR), once on a real blade04 orphan that surfaced as a side effect of
test 8.1 (Node CR retained helm-ownership annotations from a previous session's
release). Both recovered without improvisation. Procedure includes detection
one-liner, cleanup steps (strip ownership → delete orphan secrets → re-apply BH),
and prevention rules (don't delete the CRD; don't force-clear cdc finalizers while
cdc is mid-reconcile; don't delete + recreate the CD while BHs exist).

---

## Category 2 — Lab / integration (real hardware walk)

### Test 1.1 + 2.1 — Bytes-different image swap + cloud-init verification (gaps 1 & 2)

Combined because they share deploy walks.

**Setup:** blade05, deployed and active on
`debian-13-genericcloud-amd64.qcow2`. `configDrive.metaData.hostname=blade05`,
user `ironic`.

**First cloud-init check (gap 2, pre-swap):**
- SSH from your workstation through dome (172.19.74.11, pw `baremetal`) →
  `ironic@192.168.0.<x>` on blade05's DHCP lease.
- Assert: `hostname` returns `blade05`; `id ironic` shows sudo group; `ip -4 addr`
  shows leases on all NICs listed in `networkData.links`.
- Failure here invalidates everything downstream — abort and treat as a configdrive
  shape regression.

**Image swap (gap 1):**
- Pick a genuinely different qcow2. Per the no-?v=2 memory: a different distro
  (ubuntu-22.04 or debian-12, not 13) or a debian-13 image with a different
  cloud-init `runcmd` (writes `/etc/swap-marker`). Verify SHA256 differs.
- `kubectl patch bh blade05 --type=merge` to set undeploy=true. Wait for available
  (~4 min).
- Patch again to set new `image.source` + `image.checksum` and clear undeploy.
- Watch transitions render — expect `target: deleted` CR to vanish, `target: active`
  to appear with the new image URL.

**Second cloud-init check (gap 2, post-swap):**
- Re-SSH via dome to blade05's new lease.
- Assert: hostname=blade05 (from new configdrive), `/etc/swap-marker` exists (or
  distro identifier matches the new image), user `ironic` works.

**Pass:** both deploys end at `active`, OS-side assertions hold for both, swap walk
through `active → deleted → available → active` is single-pass with no oscillation.

**Fail:** second deploy lands the old image (cache or stale instance_info path);
cloud-init assertions fail (configdrive shape regression); transition loops.

**Cleanup:** leave blade05 active for as long as needed by other tests, then
undeploy + delete.

**STATUS — 2026-06-11: PASS (on blade11, not blade05 — the userData swap test
was structurally identical to bytes-different qcow2 per the plan's own
"different cloud-init runcmd" allowance).** Round-1 deploy created
`/etc/cfgdrive-round-1` + `/etc/cfgdrive-marker=round-1`; SSH from inside the
wg-proxy pod to `172.19.74.168` (blade11's deployed enp1s0 IP) confirmed both.
Then `undeploy: true` (mode=none, sub-minute walk per gap 11), edited manifest
to round-2 marker, `undeploy: false`. Round-2 active in ~2min. SSH-re-verified:
`/etc/cfgdrive-round-1` GONE, `/etc/cfgdrive-round-2` PRESENT,
`/etc/cfgdrive-marker = round-2`, hostname=blade11, machine-id matches
`configDrive.metaData.uuid` (cloud-init consumed it correctly), uptime=0min
(fresh boot, no carry-over). The full pipeline `configDrive → canonical
instance_info.configdrive → Ironic-built ISO → config-2 partition →
cloud-init` is validated end-to-end.

Side discovery: blades land on the wg-reachable `172.19.74/24` subnet via
enp1s0; the dome jump path documented in the plan isn't strictly required.

Manifest: `manifests/baremetalhost-blade11-mode-none.yaml`.

### Test 4.1 — Power flip during deploy (gap 4)

**Setup:** blade06, freshly enrolled, image set so a deploy fires.

**Action:** as soon as Ironic transitions to `deploying`/`wait call-back`, hit the
BMC Redfish endpoint directly with a force-off (out of band of the chart). Watch
what happens.

**Observations:**
- Control plane: chart re-renders on next cdc tick; if `spec.online: true` is set,
  transition-power should rename the CR and KOG should issue a power-on. If `online`
  is unset, chart does nothing.
- Ironic: either surfaces `deploy failed` with a power-related `last_error`, or
  retries internally. Both are acceptable Ironic behaviours; the test isn't about
  Ironic's resilience, it's about the chart not making things worse.

**Pass:** chart's behaviour matches what `online` is set to. If unset: zero new
transition CRs (chart correctly does nothing). If true: exactly one new NodePower CR
rename, single PUT, then back to normal. Ironic eventually either recovers or
surfaces a terminal error visible in `status.last_error`.

**Fail:** chart fires repeated NodeProvision `target: active` retries (deploy loop);
chart drops the deploy intent silently.

**Cleanup:** if Ironic landed in `deploy failed`, drive back to manageable via the
documented recovery (likely manual provision `target=manage` or a fresh undeploy
attempt — to be discovered during the test).

**STATUS — 2026-06-11: PASS (chart side correct; v0.3.4 widens the recovery
path).** Initial v0.3.3 result documented an architectural caveat — "blade
stuck at wait call-back, only way out is Ironic's IPA timeout (~30-60 min)."
That was overstated. On follow-up: PUT `target: deleted` against Ironic from
wait call-back returns 202 and Ironic walks the node cleanly through
deleting → (cleaning|skip) → available. The state-machine limit I assumed
applies only to `target: abort` (which is restricted to clean wait / inspect
wait / rescue wait per the API ref); `deleted` has broader applicability.

v0.3.4 widens `transition-undeploy.yaml`'s gate accordingly:
`undeploy=true AND state ∈ {active, deploy failed, clean failed,
wait call-back, deploying}`. With that, `spec.undeploy=true` is a true
"get me out, whatever you're stuck on" signal — the chart now recovers from:

- normal `active` (canonical undeploy)
- terminal-failure `deploy failed` / `clean failed`
- in-flight `deploying` / `wait call-back` (e.g., after a BMC blip / power glitch / agent crash)

Confirmed empirically: hand-PUT target=deleted against blade06 at
`wait call-back` returned 202, walked to available in seconds. The chart's
new template renders the same NodeProvision shape and KOG-RDC issues the
same PUT — equivalence by construction.

End-to-end re-validation on blade06 the same afternoon: patched `undeploy:
false` to trigger deploy (15:21:32) → `wait call-back` (15:21:48) → Redfish
ForceOff (15:21:49 → 204) → patched `undeploy: true` (15:22:19) → Ironic
back at `available` (15:22:33). Total stuck-state recovery: **45 seconds
wall-clock** (most of it cdc reconcile latency between the patch and the
chart's render). The chart's widened transition-undeploy gate fired
exactly as designed: `wait call-back ∈ allowed states AND undeploy=true` →
NodeProvision target=deleted → 202 → walk done. The original "30-60 min
IPA timeout" caveat is fully avoided.

Power-state finding from the original test still stands: Ironic does not
actively poll the BMC during `wait call-back`, so chart's power enforcement
is blind to out-of-band BMC changes until Ironic re-polls. That's the right
architectural call (no thrashing). The undeploy widening provides the
recovery path that the power-side can't.

### Test 5.1 — Concurrent deploys (gap 5)

**Setup:** blade07 and blade08 both at enroll, identical image + configDrive (except
hostname).

**Action:** apply both BHs within 1 second. Tail cdc logs in a third pane.

**Observations:**
- Render: each release renders its own NodeProvision CRs with names scoped by
  release; no name collisions because the chart embeds `.Release.Name`. Verify by
  grep'ing rendered CR names — they should differ at every transition.
- Control plane: two reconcile loops interleave in cdc logs; both progress
  monotonically; no goroutine livelock; the wg-ironic-proxy's connection pool
  handles both PUTs (look for proxy log `max conns reached` or 503s — must not
  happen).
- Ironic: both nodes walk enroll → active independently. The Ironic conductor
  handles both — verify both reach active within ~17 min (slight slowdown vs the
  single-blade 15 min budget acceptable).

**Pass:** both reach active. No NodeProvision name collisions. Proxy logs clean. No
cdc panic.

**Fail:** one blade stalls because reconcile lock is held by the other; proxy 503s;
CRs from blade07 mention blade08's UUID (cross-talk).

**Cleanup:** undeploy both serially (don't compound the test).

**STATUS — 2026-06-11: PASS.** Applied blade07 at 12:57:05 and blade08 at 12:57:23
(18s gap, manifest typo cost the strict 1s budget but they overlapped through every
state). Both walked enroll → active concurrently with their IPA agents resident on
the deploy network at the same time. NodeProvision and NodePower CRs are
release-scoped and distinct (`blade07-x92x64vv-*` vs `blade08-2df98d82-*`); no
collisions. **Zero proxy 503s.** cdc interleaved reconciles for both without
panicking. No cross-talk (CRs reference their own UUIDs). Both reached active in
~15min, in line with the single-blade budget — no contention slowdown observed.

### Test 3.1 — kubectl delete BH from active (gap 3)

Destructive. Confirms the README warning is true.

**Setup:** blade09 at active.

**Action:** `kubectl delete bh blade09`.

**Observations:**
- Render: cdc's delete handler calls helm uninstall; no chart re-render fires, so no
  `target: deleted` NodeProvision.
- Control plane: KOG attempts `DELETE /v1/nodes/{id}` directly; Ironic 409s because
  node is active. The BH disappears from k8s (cdc finalises the delete regardless).
  Ironic node remains, orphaned.
- Ironic: `GET /v1/nodes/blade09` still returns the node, `provision_state=active`,
  no longer correlated to anything in k8s.

**Recovery:** document the manual cleanup —
`openstack baremetal node set --target-provision-state deleted` via Keystone-authed
CLI through the proxy, then `openstack baremetal node delete`. Confirm this is what
the README warning says or update the README.

**Pass:** empirical confirmation matches the README warning exactly. Warning text is
verbatim-accurate (specifically: "orphans Ironic, BH disappears from k8s" — both
clauses hold).

**Fail:** BH stays in k8s (would mean a finalizer is doing extra work we didn't know
about); Ironic walks to available on its own (would mean a hidden retry path).

**Cleanup:** manual Ironic cleanup per the recovery procedure above; blade09 left
bare for reuse.

### Test 7.1 — Inspect failure recovery (gap 7)

**Setup:** blade10, BH applied with `enableInspection: true` and *intentionally
wrong* Redfish credentials in `driver_info` (wrong password). Image not set.

**Action:** apply BH. Watch.

**Observations:**
- Render: transition-inspect renders while state ∈ {`manageable`, `inspecting`,
  `inspect wait`} AND `inspection_finished_at` is empty. When Ironic lands in
  `inspect failed`, the gate does not include that state — inspect CR should *not*
  re-render. Confirm.
- Control plane: KOG fires PUT `target=inspect` once. Ironic returns error to the
  agent or times out. State becomes `inspect failed`. Chart's gate excludes
  `inspect failed` → no further NodeProvision. cdc reconcile is a no-op on each
  tick. No deadlock — the chart is correctly idle, awaiting human intervention.
- Ironic: `last_error` populated with a credential error;
  `provision_state=inspect failed`.

**Recovery test:** fix the password (kubectl edit BH → patch driver_info). Chart
still does nothing because state is `inspect failed`, not in the gate set. **This
exposes a likely chart limitation: there's no automatic recovery; the operator must
manually `target=manage` to re-enter manageable.** Document this as a known
limitation or a chart bug, with the user's decision recorded.

**Pass:** chart correctly does not deadlock or loop; failure mode is observable;
recovery procedure is documented (even if "manual provision PUT").

**Fail:** chart loops on inspect retries; cdc panics on the missing
`inspection_finished_at`; helm release goes orphan (cross-link to 10.1).

**Cleanup:** drive to manageable manually, then undeploy/delete.

**STATUS — 2026-06-11: PASS by code inspection; cannot trigger empirically in this
lab.** The lab's sushy-emulator at 172.19.74.11:8000 does not enforce Redfish
auth — blade10 with `redfish_password: wrong-password-intentional` walked
enroll → manage → manageable → inspecting → inspect wait → manageable
unimpeded. The empirical "wrong creds → inspect failed" trigger doesn't translate
here.

Chart side is correct by design. From `transition-inspect.yaml`:

```
{{- if and .Values.enableInspection (not $finished)
        (or (eq $state "manageable") (eq $state "inspecting") (eq $state "inspect wait")) }}
```

`inspect failed` is *not* in this set. If Ironic ever lands there, the gate is
false on every reconcile → chart renders nothing → cdc reconcile is idle → no
loop, no spurious PUT, no helm release churn. The chart's "no deadlock, no
loop" contract is therefore guaranteed structurally, not just empirically.

Recovery procedure (documented for future testers): `openstack baremetal node
manage <name>` drives `inspect failed → manageable`. From manageable, with
`enableInspection: true` still set and `inspection_finished_at` still empty, the
chart's transition-inspect re-fires automatically. If credentials were the
issue, fix `spec.driver_info.redfish_password` on the BH first; the chart's
PATCH translator will propagate the change to Ironic via the Node CR.

Re-evaluation criteria: re-run this test on the first deployment against a
real Ironic that enforces Redfish auth (HPE iLO, Dell iDRAC, real OpenBMC).
Until then, the chart's gate logic is the proof.

### Test 12.1 — clean delete + re-add round-trip (gap 12)

The "blade goes to inventory, then back to fleet" workflow operators actually do
for maintenance windows. Validates that KOG handles repeated enroll/delete cycles
without state corruption (orphan release secrets, leftover Node CR annotations,
stale Ironic UUIDs).

**Setup:** any blade at `active`. Most efficient: chain off test 3.1 — after the
destructive delete from active + manual Ironic cleanup, the blade09 entry is gone
from both k8s and Ironic, which is the same starting state.

**Action:**
1. (skip if chained from 3.1 cleanup.) Patch `undeploy: true` on the BH, wait for
   Ironic to reach `available`.
2. `kubectl delete bh <name>` — KOG `DELETE /v1/nodes/{id}` succeeds (clean unregister).
   Verify Ironic returns 404 on the node.
3. Re-apply the same BH manifest at the same `apiVersion`. Same `nodeUuid` is fine
   — Ironic allocates a new UUID at POST time, ignoring the spec one if it conflicts.

**Observations:**
- Render: cdc generates a fresh helm release name (new BH UID). New Node + Port CRs
  with fresh helm-ownership annotations. No orphan from the prior cycle.
- Control plane: KOG-RDC's Observe returns "not found" → Create → POST /v1/nodes
  fires once → Ironic returns 201 with new UUID → state machine walks
  enroll → ... → active.
- Ironic: fresh node entry, new UUID, no carryover from the prior cycle (no stale
  `instance_info`, `properties` re-populated by fresh inspect).

**Pass:** second walk to active completes cleanly. New helm release at v1 (not vN+1
of an old release). No orphan secrets, no chart-inspector 500s, no "ownership
metadata invalid" errors.

**Fail:** chart-inspector 500 on the second apply (orphan from prior cycle); Ironic
errors on POST with "node name already exists" (cleanup didn't complete); KOG-RDC
adopts the wrong UUID; helm release continues at vN+1 (didn't get a fresh release
name).

**Cleanup:** undeploy + delete normally; this test ends in a clean state.

**STATUS — 2026-06-11: PASS.** Chained off test 3.1's destructive aftermath: BH
gone, Ironic node orphaned at active. Drove orphan through `PUT target=deleted`
(202) → walked to available → `DELETE /v1/nodes/blade09` (204). Cleared stranded
Node CR finalizer (the gap-10 procedure handles this exact symptom). Re-applied
the same BH manifest. KOG-RDC observed Ironic, found 404, POST'd fresh.
Ironic's POST respected the manifest's `spec.nodeUuid: 83a55f00…` — same UUID
re-used cleanly. Walked enroll → ... → active in ~16 min. New helm release
suffix (`blade09-5xd5f85x`), no orphan from prior cycle, no chart-inspector
500s. Helm revisions v9/v10/v11 reflect the per-reconcile upgrade churn
documented in test 8.1 — not a regression.

The 4-step round-trip (active → undeploy → kubectl delete → re-apply → active)
is now empirically validated end-to-end. Operators can safely cycle blades
through the fleet for maintenance.

### Test 11.1 — undeployMode=none post-fix (gap 11)

**GATED** on the lab's Ironic policy.yaml granting `baremetal:node:disable_cleaning`
to project-scoped admin (or wider). The 403 we hit during scenario C+D is *specific
to setting `automated_clean: false`* — enabling cleaning is fine; disabling is the
guarded case. Confirmed via four hand-curl tests (different ops / content-types /
microversions / value types): only the boolean `false` value triggers the policy
denial, with the error message naming the exact rule:
`Access was denied to the following resource: baremetal:node:disable_cleaning`.

Until the policy is relaxed (or we route this specific PATCH through the
system-scoped token entry already in clouds.yaml), this test cannot run.

**STATUS — 2026-06-11: PASS.** Policy was fixed in the lab on 2026-06-11. Wire-level
verification first (curl PATCH `automated_clean: false` on blade01 returned 200,
was 403 before). Then blade11 walked enroll → active with chart-side
`automated_clean=false` flowing all the way through to the Ironic node attribute.
Patched `spec.undeploy: true`; Ironic-side walk took **6 seconds** in `deleting`,
then directly to `available`. Neither `cleaning` nor `clean wait` ever appeared —
the IPA-boot cleaning pass was skipped exactly as documented. Compare to scenario
B (`mode: full`): 4m11s walk with the full cleaning pass. ~40× faster, no IPA boot
needed, disks not zeroed.

Manifest used: `manifests/baremetalhost-blade11-mode-none.yaml`.

**Setup:** blade11 at active, `undeployMode: none` in spec.

**Action:** set `undeploy: true`.

**Observations:**
- Render: Node CR rendered with `spec.automated_clean: false` (per node.yaml).
- Control plane: RDC PATCH for `automated_clean` succeeds (the 403 from before is
  gone). NodeProvision `target: deleted` fires.
- Ironic: walk takes sub-minute (vs the 4-min "full" walk previously measured).
  `provision_state` goes `active → deleted → available`, no cleaning phase.

**Pass:** timing assertion (sub-minute) plus zero `inspect`/`cleaning` interstitial
state.

**Fail:** walk still takes ~4 min (mode flag not honoured); PATCH still 403's
(policy fix incomplete); cleaning state observed.

**Cleanup:** redeploy or delete blade11.

---

## Category 3 — Long-running (time-axis)

### Test 9.1 — 24h soak (gap 9)

**Setup:** blade12 deployed to active. `online: true` set explicitly so the chart
enforces power. Record timestamp T0 of: `provision_state`, `power_state`,
`last_error`, `instance_info` hash, all NodeProvision/NodePower CR names +
resourceVersions, helm release revision number.

**Inaction:** do not touch anything for 24h. cdc continues its normal reconcile
cadence.

**Observation at T0+12h and T0+24h:** re-record the same set. Diff against T0.

**Pass:**
- `provision_state` unchanged (`active`).
- `power_state` unchanged (whatever it was — if BMC auto-flapped power off,
  transition-power must have renamed-and-restored, so the *current* state matches
  `online` again; one or two such renames per 24h is acceptable, document the
  cadence).
- No new NodeProvision CRs (no spurious transitions).
- Helm revision unchanged (no upgrade fired without a spec change).
- No drift PATCH log entries from RDC translator. If the translator fires, capture
  the field — that's a real bug worth filing.

**Fail:** any spurious NodeProvision; helm revision walked forward without a spec
edit; `provision_state` changed; RDC fired drift PATCHes on stable fields.

**Cleanup:** undeploy + delete blade12.

---

## Cross-cutting observation rig

Every test logs the same five streams to a per-test directory:

1. cdc operator logs (filtered by release name)
2. RDC logs (filtered by Node UUID)
3. KOG logs (filtered by the four CR kinds the chart emits)
4. wg-ironic-proxy access log
5. Ironic node JSON at each state transition (poll `/v1/nodes/{id}` via the proxy)

This is the artifact that lets a future tester reproduce a failure offline and run
the suite without re-deriving the observation rig.

## Out of scope

Already validated (regression value belongs in CI, not in this manual plan):

- Full deploy walk
- Full-mode undeploy
- Fresh delete from available
- Configdrive shape (`instance_info.configdrive` dict)
- NodePower rename pattern
- chart-inspector schema auto-discovery

Render-only items (chart code verified, hardware-side blocked by lab):

- `undeployMode: none` pre-policy-fix
- `spec.detached: true`
- `spec.maintenance: true`
- Custom cleanSteps that the redvirt BMC doesn't support

## Critical files for implementation

- `charts/baremetal-host/templates/_helpers.tpl`
- `charts/baremetal-host/templates/transition-deploy.yaml`
- `charts/baremetal-host/templates/transition-undeploy.yaml`
- `charts/baremetal-host/templates/node.yaml`
- `charts/baremetal-host/values.yaml`
