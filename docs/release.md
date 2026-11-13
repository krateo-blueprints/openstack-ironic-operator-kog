---
type: Runbook
title: openstack-ironic-operator-kog — release
description: How the four charts are published — one SemVer tag runs release-chart.yaml, which lints, packages and pushes all four charts to GHCR as OCI artifacts at each chart's own Chart.yaml version.
resource: oci://ghcr.io/krateo-blueprints/charts/baremetal-host
tags: [ironic, release, oci, ghcr]
timestamp: 2026-08-11T00:00:00Z
---

# Release

The four charts version **independently** — each carries its own `version` in its
`Chart.yaml` — but they are published together by one workflow on any SemVer tag.

## What a tag ships

`.github/workflows/release-chart.yaml` runs on any plain-SemVer tag (`[0-9]+.[0-9]+.[0-9]+`,
**no** `v` prefix) or manual dispatch:

1. `helm lint` each of `charts/baremetal-discovery`, `charts/baremetal-host`,
   `charts/baremetal-lifecycle`, `charts/kubernetes-cluster`.
2. `helm package` each into `dist/`.
3. `helm registry login ghcr.io` with `GITHUB_TOKEN`.
4. `helm push` each `.tgz` to `oci://ghcr.io/<owner>/charts` (owner lowercased from
   `GITHUB_REPOSITORY_OWNER` — `GITHUB_TOKEN` can only write its own namespace, so a
   hardcoded org would 403 if the repo moved), with retry on GHCR flakiness.

There is **no single tag-version guard**: the tag triggers the run, but each chart is pushed
at whatever version its own `Chart.yaml` declares. Bumping one chart and tagging still
re-publishes all four (at their current versions) — harmless, since a push of an unchanged
version is a no-op.

## Steps

```console
$ git tag X.Y.Z && git push origin X.Y.Z
```

Then verify the artifacts exist at the versions the `Chart.yaml`s declare, e.g.:

```console
$ helm show chart oci://ghcr.io/krateo-blueprints/charts/baremetal-host --version 0.4.5 | head -3
$ helm show chart oci://ghcr.io/krateo-blueprints/charts/baremetal-lifecycle --version 0.3.1 | head -3
```

After a chart is published, bump the matching `manifests/compositiondefinition-*.yaml`
`spec.chart.version` (and `url` for released charts) to the version that now exists.

## Current published versions

| chart | version |
|---|---|
| `baremetal-host` | `0.6.5` |
| `baremetal-lifecycle` | `0.3.4` |
| `baremetal-discovery` | `0.1.4` |
| `kubernetes-cluster` | `0.15.5` |

> **`kubernetes-cluster` 0.15.2 is published and must not be used.** Its bundled
> `compositiondefinition.yaml` pins `version: "0.15.1"` while the chart is `0.15.2`, so a
> consumer installing from it gets a CompositionDefinition that installs the previous chart.
> The `publish-blueprints-channel` gate caught it — *"kubernetes-cluster 0.15.2: its
> CompositionDefinition installs version '0.15.1'"* — but **after** `helm push` had already
> succeeded in the same workflow, so the artifact exists. It is left immutable rather than
> overwritten, for the reason this page gives below: a re-push changes bytes that consumers
> already hold. `0.15.3` is the corrected release.
>
> The local gate `make check-blueprints` catches this before a tag, and it was not run.
> `release-chart.yaml` now runs the whole `make check` suite as its first step, before
> `helm package` and `helm push`, so the gate can no longer run too late to matter.

## PR-time checks

`security.yml` runs the shared `krateo-platformops/.github` security workflow on every PR and
push to `main`. `lint.yaml` runs the shared docs-standard linter (`lint-docs`) on the docs
bundle. Consumers install compositions by explicit `spec.chart.version`; nothing tracks a
mutable tag.
