---
type: Runbook
title: Publishing to the Krateo Marketplace
description: How the two blueprints — kubernetes-cluster and baremetal-host — reach the Krateo Marketplace. The channel gate's three in-package requirements, the curated-index and live-index routes to a card, the one-time publish-token setup, why a chart version IS the served composition apiVersion, and why upgrading the lab stays a separate decision.
resource: oci://ghcr.io/krateo-blueprints/charts/kubernetes-cluster
tags: [marketplace, blueprints, krateo, charts, publishing, runbook]
timestamp: 2026-11-06T00:00:00Z
---

# Publishing to the Krateo Marketplace

This repo ships **two** Marketplace blueprints:

| Chart | Card | Category |
|---|---|---|
| `charts/kubernetes-cluster` | Kubernetes on Bare Metal | compute |
| `charts/baremetal-host` | Bare-Metal Host (OpenStack Ironic) | compute |

`baremetal-discovery` and `baremetal-lifecycle` are **not** blueprints and are
not published to the channel. They are the superseded discovery/lifecycle split
that `baremetal-host` absorbed into one resource. `hook-rbac-probe` and
`release-adopt-probe` are throwaway measurement rigs. All four are correctly
rejected by the channel gate, because none carries an in-chart
`compositiondefinition.yaml` — the gate is doing its job, not blocking us.

Run `make check-blueprints` before tagging. It reproduces the gate locally and
covers six further failures the gate passes and the Marketplace then shows.

---

## How a chart becomes a card

There are two routes, and only the second is self-service.

**1. The curated catalog.** The portal reads a `blueprints-catalog-index`
ConfigMap. That ConfigMap is built by `krateo-blueprints/marketplace`'s
`hack/build-index.py`, which fetches **this same live channel index** and removes
only 18 named platform charts (`portal`, `installer`, `krateo-frontend`, …).

So publishing to the channel is enough for this route too — there is no PR to
raise and no list to be added to. What it costs is time: the ConfigMap only
learns about a new chart when `krateo-marketplace-catalog` is released again, and
that release is not ours to cut.

**2. The live index** (portal PR #292, merged 2026-10-05). The portal also reads
`https://krateo-blueprints.github.io/charts/blueprints/index.yaml` and adds a
card for every chart the ConfigMap does not already carry **whose newest version
carries the `krateo.io/source-repo` annotation**. Publishing to the channel is
then enough; nobody else has to act.

**2 is what the annotation buys: immediacy, not existence.** Without it the
blueprint still reaches the Marketplace, on route 1, whenever the catalog is next
released. With it the card appears as soon as the publish lands.

```yaml
annotations:
  krateo.io/source-repo: "krateo-blueprints/openstack-ironic-operator-kog"
```

The portal's selection predicate is literally
`(.value[0].annotations["krateo.io/source-repo"] // "") != ""`. The filter exists
because the live index also carries 18 platform charts the curation removed on
purpose (`portal`, `installer`, `krateo-frontend`, …), none of which is
annotated.

Checked against the real index on 2026-11-05: **no entry carries that
annotation yet** — not `baremetal-lifecycle` 0.3.1 (this product's own, published
before the current publisher existed), and not the `aws-*-stack` blueprints
either. portal#292's own validation table says the same thing in its third row:
*"real live index (no source-repo entries yet)"*, with the output identical to
`main`'s.

Two things follow, and both are worth saying plainly:

- Every card in the Marketplace today came through route 1. Route 2 is merely
  newer, not better-travelled, and these two blueprints would be the first
  entries to use it. If a card does not appear, that is the thing to suspect
  first, and route 1 is the fallback that needs no annotation at all.
- Nothing warns you either way. The gate does not check the annotation, the
  index does not require it, and the portal simply does not select the entry.

Note the `[0]`: the predicate reads the entry's **newest** version. An older
annotated version does not keep the card alive once a newer un-annotated one
publishes. `make check-blueprints` fails if the annotation goes missing from
either chart.

---

## The channel gate, exactly

`krateo-blueprints/charts/.github/workflows/publish-chart.yaml` admits a chart
to the `blueprints` channel only if the **packaged `.tgz`** contains:

1. `values.schema.json`
2. `compositiondefinition.yaml` (or `.yml`)

…and `Chart.yaml`'s `icon` begins with `https://`.

Three things about that are worth knowing before you trust it:

- **It reads the archive, not the source tree.** A `compositiondefinition.yaml`
  sitting *beside* the chart does not count. Ours is at
  `charts/<name>/compositiondefinition.yaml`, and `make check-blueprints` packages
  the chart and reads the archive for the same reason — `.helmignore` is
  invisible to a source-tree check.
- **The icon test is a scheme test only.** A
  `https://github.com/owner/repo/blob/main/logo.svg` URL passes it and then
  renders a blank card, because that URL serves an HTML page rather than an
  image. Our screen additionally rejects `/blob/` URLs and, with
  `BLUEPRINT_ONLINE=1`, fetches the URL and requires an `image/*` content type.
- **Names are unique per channel.** Re-measured 2026-11-07: `kubernetes-cluster`
  and `baremetal-host` are free; `baremetal-lifecycle` and `ironic` are taken.
  Who holds them is not what this section said before, and both corrections
  change what you would do about it:

  - `baremetal-lifecycle` 0.3.1 is **this repository's own chart** —
    `maintainers: openstack-ironic-operator`, with a description identical to
    `charts/baremetal-lifecycle/Chart.yaml` (now 0.3.2). The name is not held by
    a stranger.

    It does **not** follow that this repo's publish path has ever worked, and an
    earlier draft said it did ("we published it, on or before 2026-08-19, so
    this repo *has* reached the channel successfully at least once"). That date
    is the 221-millisecond import of 311 charts in the table below — somebody's
    bulk backfill, which our chart was swept into. The openstack blueprint
    reports the same of its own 0.2.0 entries (their #184: a one-off backfill,
    not that repo's pipeline). So there is **no evidence any release of ours has
    reached this channel**, which makes the 401 below the likely original and
    only state of the publish path rather than a regression.
  - `ironic` 0.2.0 is the **upstream OpenStack-Helm chart** — `maintainers:
    OpenStack-Helm Authors`, `home: docs.openstack.org/ironic/latest/`,
    `sources: opendev.org/openstack/{ironic,openstack-helm}`. This section
    previously attributed it to `krateo-blueprints/krateo-openstack-blueprint`.
    The index does not support that — the entry carries no
    `krateo.io/source-repo` annotation — and that repo reports it has never
    published to this channel at all: its cross-org feed step is skipped because
    `feed_repo` is empty, and GHCR is its only channel.

    Refined 2026-11-07, because "upstream's chart" names the metadata and not
    the publisher. This channel holds **28 charts whose `maintainers` is
    `OpenStack-Helm Authors`** — aodh through watcher — and **every one is at
    exactly 0.2.0**. Upstream does not version 28 service charts in lockstep, so
    a uniform 0.2.0 is one pipeline packaging a vendored subchart set at its
    umbrella's version. The umbrella is here too: `openstack` 0.2.0, whose
    `sources` name `github.com/braghettos/krateo-openstack-blueprint` — the
    personal-namespace predecessor of the blueprint, which is exactly why
    `krateo-blueprints/krateo-openstack-blueprint` can truthfully report it has
    never published here.

    So the name `ironic` is held by the openstack-blueprint lineage, not by
    upstream. Practically that is better than a stranger holding it: it is one
    team's to move or rename, and a publish from the current repo extends that
    chart rather than colliding with someone unreachable. It also means the
    "stale at 0.2.0" that repo sees is not its pipeline publishing an old
    version — its pipeline has published nothing, and 0.2.0 is its
    predecessor's.

  The complementarity still holds, just not via the repo named before: the
  channel's `ironic` deploys the service, `baremetal-host` manages a machine
  through it, `kubernetes-cluster` builds a cluster out of those machines.

- **Do not reason about name ownership from `krateo.io/source-repo`.** 1551 of
  the 1558 versions in the blueprints index carry no such annotation; the only
  ones that do are `meetup-site` (4 versions), `acme-feedback` and
  `event-feedback`. An earlier draft of this section asserted the publisher
  "refuses to overwrite a name owned by a different `krateo.io/source-repo`".
  Whatever it does on a collision, the index gives no evidence it keys on that
  annotation. Treat a taken name as taken and pick another.

- **A stale channel index is the normal state.** Measured 2026-11-07:
  byte-identical at 1,618,360 bytes, 810 charts, 1558 versions, and no `created`
  later than 2026-10-09 — **nothing has entered this channel in 29 days**. A
  chart missing from the index still does not by itself prove the publish
  failed, so pair it with the job conclusion.

- **`created` DOES date a write, and an earlier draft of this bullet denied it.**
  That draft read: `generated` equals the newest `created`, "so the whole file is
  one regeneration and no `created` in it dates a publish". The first half is
  true and the inference does not follow — `created` is preserved across
  regenerations. The openstack blueprint session caught it; measuring the whole
  distribution shows five write events, not one:

  | `created` date | versions | span | reading |
  |---|---|---|---|
  | 2026-08-19 | 311 | 221 **ms** | bulk import |
  | 2026-10-06 | 7 | 17 hours | ordinary individual publishes |
  | 2026-10-07 | 206 | 29 s | batch |
  | 2026-10-08 | 828 | 53 min | batch |
  | 2026-10-09 | 206 | 73 s | batch, ending 0.9 ms *after* `generated` |

  So `generated` is stamped at the start of a regeneration run and that run's own
  `created` values straddle it — which is the whole of the coincidence that the
  draft generalised to 1558 entries. Use a `created` as the date that version
  entered the channel; use its **span** to tell a publish from an import.

Our icon is the Krateo Akmon mark, pinned to a commit:

```
https://raw.githubusercontent.com/krateo-blueprints/openstack-ironic-operator-kog/<sha>/branding/krateo-akmon-mark.svg
```

Commit-pinned rather than `main`-pinned so the card cannot change under us.
Verified 2026-11-05: HTTP 200, `content-type: image/svg+xml`.

---

## The publish path

```
git tag 1.2.3 && git push --tags
      │
      ├─ release-chart.yaml / publish           → oci://ghcr.io/krateo-blueprints/charts/<name>
      │                                           (all four charts; source of truth)
      │                                           uploads the TWO blueprints as `charts-dist-blueprints`
      │
      ├─ blueprints-token-preflight             → fails early and legibly if the token is unset
      │
      └─ publish-blueprints-channel             → krateo-blueprints/charts, `blueprints` channel
                                                  (republishes the same bytes)
```

The artifact deliberately holds only the two blueprints: the gate loops over
**every** `.tgz` it is given, so including the superseded charts would fail the
whole publish.

### The publish token — present is not the same as valid

The reusable publisher pushes into `krateo-blueprints/charts`, so it needs a
token with write access there. **As of 2026-11-07 this repository does need
setup for that: the org secret no longer authenticates** (the measurement is
below). The history is recorded because both of the obvious conclusions have
been wrong once each — first that a PAT was required, then that none was.

`krateo-blueprints` is an **Organisation**, and it carries an organisation
secret `RELEASE_FEED_TOKEN` at `visibility=all` — so it is already reachable
from here. The three sibling blueprint repos resolve the token exactly this way:

```yaml
# krateo-aws-blueprint, krateo-azure-blueprint, krateo-gcp-blueprint
# .github/workflows/release-tag.yaml
PUBLISH_TOKEN: ${{ secrets.CHARTS_PUBLISH_TOKEN || secrets.RELEASE_FEED_TOKEN }}
```

This workflow now resolves it identically. The evidence, measured rather than
inferred:

| Check | Result |
|---|---|
| Org secrets reachable from this repo | `CLAUDE_CODE_OAUTH_TOKEN`, `RELEASE_FEED_TOKEN` |
| Same set reachable from `krateo-aws-blueprint` | identical |
| Repo-level secret on any of the three siblings | **none** — all three listings empty |
| `krateo-aws-blueprint` run `37742845675` | `publish-blueprints / publish` → **success** |

That last row is the one that matters, and it is deliberately the job's
conclusion rather than the run's. A skipped job also leaves a green run, so
"the release passed" would not have shown that the channel publish actually
happened.

A repository secret `CHARTS_PUBLISH_TOKEN` takes precedence if one is set — a
CLASSIC PAT with the `repo` scope (classic tokens have no granular
`contents:write`).

**The two tokens are not interchangeable in general — only here.** In this repo
the channel publish resolves `CHARTS_PUBLISH_TOKEN || RELEASE_FEED_TOKEN`
(`release-chart.yaml:138,183`), so either one unblocks it and the 401 below is a
real verdict on the org token. `krateo-blueprints/krateo-openstack-blueprint`
has no such fallback: its release pushes only to GHCR and never attempts an
index publish, its `RELEASE_FEED_TOKEN` is used solely by the release-notes
cross-org feed step (skipped there, `feed_repo` empty), and the publisher its
PR #186 introduces reads `CHARTS_PUBLISH_TOKEN` alone. Reported by that session
2026-11-07. So for that repo rotating the org token would not unblock the index
and creating the repo secret would not fix release-notes, while for this one the
fallback makes either sufficient for the channel. Do not carry a conclusion
about one repo's token across to the other.

**And as of 2026-11-07 one is needed, because `RELEASE_FEED_TOKEN` no longer
authenticates.** Tag `0.3.0` ran and split exactly along that line:

| job | result |
|---|---|
| `publish` (OCI → GHCR) | **success** — both charts live at 0.14.0 / 0.6.3 |
| `blueprints-token-preflight` | success — and this was the defect |
| `publish-blueprints-channel` | **failure** — `HTTP 401: Bad credentials` |

The evidence that said the token was fine was: the org secret exists at
`visibility=all`, and `krateo-aws-blueprint` run `37742845675` published with it
successfully. That run was **2026-10-08**. "Exists, and worked a month ago" is
not "authenticates now", and nothing in the earlier reasoning tested the
difference.

The preflight is what should have caught it and could not: it tested `-n` on the
secret. A check that cannot distinguish a live credential from an expired one is
not a preflight, it is a reassurance. It now calls `gh api repos/<repo>` against
both this repository and `krateo-blueprints/charts`, which fails on a dead
credential. It still does **not** prove write scope — deliberately, because a
preflight should not make writes — so a token that authenticates but lacks
`repo` can still fail inside the publisher.

**This likely affects every sibling blueprint repo**, since they all resolve the
same org secret with the same `||` fallback.

**An earlier version of this document said the opposite** — that a PAT had to be
created here because "this is a user account, so there are no org-level
secrets." `krateo-blueprints` is an Organisation; the claim was simply false,
and it would have cost someone a pointless credential.

---

### The token is not the last blocker: this repo is PRIVATE and the index URL is not

Found 2026-11-07, before the token was replaced, so it has never been exercised.
A publish from here will go **green and produce an uninstallable card.**

The reusable publisher builds each index entry's download URL from the caller:

```yaml
# krateo-blueprints/charts/.github/workflows/publish-chart.yaml:44
RELEASES_BASE: https://github.com/${{ inputs.source_repo }}/releases/download
```

and the index bears that out — measured over all 1558 versions:

| `urls:` host | versions | fetchable anonymously |
|---|---|---|
| `github.com/<source_repo>/releases/download/…` | 1247 | only if that repo is public |
| `krateo-blueprints.github.io/charts/blueprints/…` | 311 | yes — the 2026-08-19 import |

And the repos:

| repo | visibility |
|---|---|
| `krateo-blueprints/openstack-ironic-operator` | **PRIVATE** |
| `krateo-aws-blueprint`, `-azure-`, `-gcp-`, `krateo-openstack-blueprint` | PUBLIC |

An anonymous `curl` of a published sibling asset returns **200**; a private
repo's release asset cannot return one. So once a valid `CHARTS_PUBLISH_TOKEN`
exists, the publish job succeeds, the card appears, and `helm` cannot fetch the
chart. The job's own conclusion will not show it — the same defect shape as the
preflight that tested presence instead of validity, one layer further out.

Two ways out, and the second is the clearer one:

1. **Make this repository public**, matching all four siblings. One setting, no
   code, and it is the user's call — the repo carries lab topology and host
   details that have never been reviewed for publication.
2. **Give the publisher a caller-independent asset host.** Its own header
   comment already describes that design — "uploads the .tgz to *this repo's*
   Releases (shared blob store)" — while the code uploads to the caller's
   release instead, so comment and code disagree. The store exists and works:
   `krateo-blueprints/charts` is PUBLIC and its `gh-pages` branch already serves
   **316 `.tgz` files** from `blueprints/`, which is exactly what the 311
   Pages-hosted entries resolve against. Uploading there and setting the entry
   URL to the Pages host makes the channel independent of caller visibility for
   everyone, not just for us. That is an upstream change to
   `krateo-blueprints/charts`, not a change here.

   **The patch is written and kept here:
   [`docs/proposals/publish-chart-asset-host.patch`](proposals/publish-chart-asset-host.patch)**
   — 34 lines added, 7 removed, against that repo's
   `.github/workflows/publish-chart.yaml`. It replaces `RELEASES_BASE` with
   `PAGES_BASE: https://krateo-blueprints.github.io/charts`, adds one step that
   copies `dist/*.tgz` into the already-cloned `gh-pages/${CHANNEL}/`, points
   `helm repo index --url` at the Pages host, and adds the tarballs to the
   existing commit. The generated URL is byte-identical in shape to the 311
   entries already serving 200 from there. `--merge` leaves every existing entry
   untouched, so it is **forward-only**: the 1247 `releases/download` entries
   keep working. The caller-release upload is kept as provenance and marked as
   resolving nothing.

   **It has not been run.** It parses as YAML and the URL shape is verified
   against the live index; nothing beyond that is tested, and it cannot be
   tested from here — a reusable workflow only runs when a caller tags a
   release.

Do not "solve" this by publishing only `baremetal-lifecycle`-style charts, by
hand-editing an index entry's `urls:`, or by making the release assets public
some other way. The asset host is the publisher's decision and belongs there.

**Related hazard, not ours but worth knowing:** those 1247 URLs embed the
*per-chart* release tag — e.g.
`…/krateo-aws-blueprint/releases/download/aws-wafv2-webacl-0.3.3/aws-wafv2-webacl-0.3.3.tgz`.
`krateo-blueprints/charts#5` (merged 2026-11-07T14:28Z) stops creating a release
per chart, which is right, but **deleting the existing per-chart releases as
cleanup would 404 all 1247 entries.** Resetting "Latest" is safe; deleting is
not.

---

## The chart version is the API version

`0.14.0` is served as `composition.krateo.io/v0-14-0`. Three consequences:

1. **Two files hold the number.** `Chart.yaml`'s `version` and the in-chart
   `compositiondefinition.yaml`'s `spec.chart.version`. `make check-blueprints`
   fails if they drift.
2. **Registering a version unserves the previous one.** Anything that renders at
   a pinned apiVersion breaks at that moment.
3. **Which makes the two blueprints a lockstep pair.** `kubernetes-cluster`
   renders `BaremetalHost` resources at its `lifecycleApiVersion`, whose
   reachable default names the `baremetal-host` version it was released against.
   Bumping `baremetal-host` without moving that default leaves every cluster
   render failing against an apiVersion nothing serves — so `make check-examples`
   asserts the two agree, and the build fails rather than a fresh cluster
   discovering it.

**Register `baremetal-host` first**, then `kubernetes-cluster`.

---

## After publishing, verify

**Check the right population.** Three different index documents all answer
HTTP 200, with different contents, and a fourth thing decides whether a card
renders:

| Document | Size | Charts | What it is |
|---|---|---|---|
| `krateo-blueprints.github.io/charts/blueprints/index.yaml` | 1,618,360 B | 810 charts / 1558 versions | the **publish** index — what the channel gate writes |
| `charts.krateo.io/index.yaml` | 687,583 B | — | a third document, not the one above |
| `marketplace.krateo.io/index.yaml` | 213,553 B | 219 entries | another, smaller still |
| `blueprints-catalog-index` ConfigMap, key `blueprints-index.json` | 284,927 B | **293** charts, one version each | the **display** population — what the portal actually reads |

Measured 2026-11-06. The curation from 810 to 293 is `build-index.py` minus
`PLATFORM_LEAVES`, and it is much heavier than "a handful": **517 charts in the
publish index are not in what the portal reads.**

So the two questions need two checks, and conflating them is how a publish gets
declared a success that shows nothing.

**Did the publish land?** Read the publish index — this is the population the
channel gate writes and therefore the one that can fail your release:

```sh
curl -sL https://krateo-blueprints.github.io/charts/blueprints/index.yaml \
  | python3 -c '
import sys, yaml
idx = yaml.safe_load(sys.stdin)["entries"]
for n in ("kubernetes-cluster", "baremetal-host"):
    e = idx.get(n)
    if not e:
        print(n, "-> ABSENT: the publish did not land")
        continue
    newest = e[0]
    src = (newest.get("annotations") or {}).get("krateo.io/source-repo", "")
    print(n, newest["version"], "source-repo=" + repr(src),
          "-> published" + ("" if src else "; NO source-repo, so no live-index immediacy"))
'
```

**Will a card appear?** That is a question about the display population, and the
publish index cannot answer it. Read the ConfigMap the portal resolves:

```sh
kubectl --context <ctx> -n krateo-system get configmap blueprints-catalog-index \
  -o jsonpath='{.data.blueprints-index\.json}' \
  | python3 -c 'import sys,json; e=json.load(sys.stdin)["entries"]; print(sorted(e)[:0] or len(e), "charts"); [print(n, "present" if n in e else "ABSENT") for n in ("kubernetes-cluster","baremetal-host")]'
```

The resolver reads it as
`filter: '{catalog: (.catalog.data["blueprints-index.json"] | fromjson)} | <postFilter>'`
— one version per chart.

**Three distinct failures hide here**, which is why there are two checks and why
neither reads only the name:

1. the chart is absent from the publish index — the publish failed;
2. it is present but carries no `krateo.io/source-repo` — it still lands, it just
   waits for the curated rebuild instead of appearing immediately;
3. it is in the publish index and **not** in the curated ConfigMap — published,
   annotated, and still showing nothing.

The third is the quiet one, and an earlier version of this document could not
see it: it checked the publish index and printed "a card appears" on the
strength of the annotation alone. Presence plus annotation is not a card.

---

## Wrong card metadata does NOT need a version bump

The section above says the chart version *is* the served composition
apiVersion, and the natural inference from that is that everything about a
published chart is frozen at its version. **Annotations are not.**

Measured 2026-11-06 with the publisher's own command, `helm repo index --merge`:

1. index a chart carrying `maturity: beta` and the old display name — the index
   entry shows `beta`;
2. republish the **same version** with corrected annotations and merge — the
   index entry becomes `alpha` with the new display name, and there is still
   exactly **one** entry for that version.

So if a card ships with wrong copy, the fix is a republish of the same version,
not a bump. The release workflow carries `workflow_dispatch: {}`, so that
republish needs no new tag — the tag pattern would refuse a second
`0.14.0` anyway.

What this does **not** cover: the CRD version, which really is fixed by the
chart version, and anything a consumer has already pinned. It is specifically
`Chart.yaml` metadata — category, display name, maturity, icon, source-repo —
that stays correctable.

Worth stating because the conservative reading costs a version. An earlier
commit message in this branch asserted that shipping `alpha` "costs a chart
bump later" to raise; that was an assumption, and measuring it showed it is
false.

---

## Upgrading the lab is a separate decision

Publishing a chart version touches no cluster. The live `CompositionDefinition`
resources keep pointing at whatever they already point at, and the fleet keeps
running it.

Moving the lab to these versions means registering both new
`CompositionDefinition`s, in order, which unserves `v0-13-26` and `v0-6-2` and
re-renders six live hosts. Three things follow from `0.14.0`'s contract change
and should be done in the same step:

- Manifests that set `ironicApiUrl` or `ironicApiVersion` must drop them — they
  are now rejected with `unknown field`, not ignored. Done in this repo's
  `manifests/`.
- Any `KubernetesCluster` setting `lifecycleApiVersion` explicitly must move to
  `composition.krateo.io/v0-6-3`.
- The six hosts provisioned before `baremetal-host` 0.6.3 keep their
  materialised `spec.debug: {connectorVerbose: false}`. Removing the schema
  default stops it being added to *new* hosts; it does not remove it from
  existing ones, and nothing can — see `make check-blueprints` for the mechanism.

### What the migration does to the two removed fields — measured

`0.14.0` drops `ironicApiUrl` and `ironicApiVersion`. **The migration deletes
them from storage.** It is not a read-time effect and the values do not survive
in etcd.

Measured by the core-provider session on **2.13.71**, GKE, k8s 1.36.4, three
instances, with the witness that makes the result trustworthy: it waited until
`krateo.io/composition-version` had moved to the new version on all three
(24s after the bump) and only then read the after-state.

The read that settles it is through the **old** endpoint, which still declares
the field — so absence there cannot be explained by pruning on the way out:

| | `spec` read through `v0-1-0` |
|---|---|
| before the bump | `{keptField, opt, removedField: "..."}` |
| after migration | `{keptField, opt}` |

**It is not silent.** Two detectors fire, and both are worth watching for during
convergence:

- `krateo.io/pruned-fields` on each object, carrying the lost subtree —
  `{"spec":{"removedField":"SHOULD-IT-SURVIVE-MIGRATION"}}`
- a `CompositionFieldsPruned` **Warning** event per object

**On this fleet that is ONE annotation and ONE warning, not seven.** An earlier
version of this section predicted seven — one per composition — which was wrong,
and the convergence on 2026-11-07 measured it:

| object | label moved | pruned |
|---|---|---|
| `lab-ha` (KubernetesCluster) | `v0-13-26` → `v0-14-0` | `ironicApiUrl`, `ironicApiVersion` |
| six `lab-ha-*` BaremetalHosts | `v0-6-2` → `v0-6-3` | **nothing** |

Only `kubernetes-cluster` 0.14.0 removes fields. `baremetal-host` 0.6.3 removed a
schema **default**, not a field, so the six hosts migrate with nothing to prune.
Predicting seven would have had someone hunting six warnings that were never due.

Mechanism, which rules out the obvious wrong explanation: the storage version
`vacuum` is `{type: object, x-kubernetes-preserve-unknown-fields: true}` with
conversion `None`, so **storage cannot prune**. The loss is the migration's own
write through the narrower endpoint. `uid` is stable and `generation` moves
1 → 2, so it is a spec write, not a recreate.

Also measured, and it corrects "unserves `v0-13-26`" above: the old version
stays **served** while any helm release still names it, and was pruned 20s after
that last reference went. `prunableServedVersions` needs *no reference*, not
merely zero instances — all three instances had moved 40s earlier and the
version stayed.

**2.13.59 now agrees, measured on this lab.** The above was measured on 2.13.71;
an earlier probe on 2.13.59 produced no valid result, because the new controller
was unschedulable and the migration never ran — so the field appeared to survive
when nothing had happened. The real convergence on 2026-11-07 settled it, with
the same witness:

    label                 v0-13-26 -> v0-14-0        (the migration genuinely ran)
    krateo.io/pruned-fields  {"spec":{"ironicApiUrl":"http://ironic.openstack
                              .svc.cluster.local:6385","ironicApiVersion":"1.81"}}
    pruned-fields-truncated  absent (under the 16 KiB cap)
    spec.ironicApiUrl        gone
    CompositionFieldsPruned  1 Warning, on lab-ha

So the two versions behave identically on this path, and the values were
recoverable from the annotation. **Always read the label before the after-state**
— it is the only thing that distinguishes "the migration ran" from "something
else wrote the object".

### The recovery annotation has a cap, and it is not where you would guess

`krateo.io/pruned-fields` holds at most **16 KiB** (`maxPrunedAnnotationBytes`),
and the limit applies to **the JSON of the removed subtree — not to the size of
the composition.** So `lab-ha` being the largest composition here is irrelevant:
`ironicApiUrl` and `ironicApiVersion` are short strings, far under the cap, and
`0.14.0` is unaffected. The cap would only bite on a future version that removed
a large object or array.

Above the cap, recovery degrades rather than failing loudly in the ordinary
sense: the annotation holds **paths only**, plus
`krateo.io/pruned-fields-truncated: "true"`, and nothing at all if even the paths
do not fit. A Warn log and a Warning event both fire, so it is not silent.

**And the event is not a backup copy.** `CompositionFieldsPruned` carries only
paths, in every case. There is a known defect here, reported by the
core-provider session: above the cap the event directs the operator to the
controller log, and a code comment and that project's own
`docs/internals/behavior.md` both state the log "always carries the full
subtree" — it does not. The Warn log emits paths and a byte count; the payload
reaches no log at all. So above 16 KiB the values are genuinely gone, while the
tooling claims otherwise.

*Provenance:* the cap, the degradation behaviour and the defect are **read from
code at 2.13.71**, not measured. The migration behaviour in the table above
**was** measured. The distinction is kept because the two carry different
weight.
