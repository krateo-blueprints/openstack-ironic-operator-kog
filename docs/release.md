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
| `baremetal-host` | `0.4.5` |
| `baremetal-lifecycle` | `0.3.1` |
| `baremetal-discovery` | `0.1.1` |
| `kubernetes-cluster` | `0.12.11` |

## PR-time checks

`security.yml` runs the shared `krateo-platformops/.github` security workflow on every PR and
push to `main`. `lint.yaml` runs the shared docs-standard linter (`lint-docs`) on the docs
bundle. Consumers install compositions by explicit `spec.chart.version`; nothing tracks a
mutable tag.
