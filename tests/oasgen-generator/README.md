# Generator-level measurement: no cluster required

`ironic_power_spec_test.go` answers one question about **this repo's own document**:

> does the generated spec carry `node_id`, the path parameter that
> `manifests/restdefinition-power.yaml` maps **self-referentially** from `spec.node_id`?

## Why this exists

`scripts/check-restdefinition-shape.py` encodes a version-dependent rule:

```
spec.<param> survives  IFF  the create body declares <param> at top level
                       OR   the mapping is SELF-REFERENCING AND oasgen >= 0.52.0   (#421)
```

The second clause came from the `openstack-nova-operator-kog` session, measured on
**one document on one vendor**. Adopting it here on that basis would have been the
dangerous direction: if it did not transfer, the screen would report a live defect as
resolved. So PR #49 was held until this was measured on an Ironic document.

## Why the GENERATOR and not the cluster

The screen's claim is about what oasgen **generates** at a given version. The live
CRD answers a different question — what the apiserver has **stored**, which can be
stale if the fleet has not regenerated since an upgrade. Fixing what a system renders
says nothing about what it already stored, and vice versa. Both are real questions;
this harness is the right instrument for the screen's one, and it needs no cluster,
no load budget and no credentials.

## Result, 2026-11-12

```
oasgen 0.59.0   spec.node_id = PRESENT    CONTROL spec.target = PRESENT
oasgen 0.50.0   spec.node_id = ABSENT     CONTROL spec.target = PRESENT
```

`spec.target` is declared in the `PowerTarget` request body, so it must be present at
**both** versions. It is — which is what makes the 0.50.0 `ABSENT` a measurement
rather than a broken build. Without that control the two runs would be
indistinguishable from the harness failing to compile against the older API.

So the rule transfers to Ironic's documents, #421's behaviour is not narrower than
its description, and the earlier `ABSENT` finding here was correct **for 0.50.0**.

## Reproducing it

The package is `internal/`, so the test has to run *inside* oasgen's own tree, the
way their own tests do. Worktrees keep each version isolated:

```sh
git clone https://github.com/krateo-platformops/oasgen-provider.git /tmp/ogp
cd /tmp/ogp && git fetch --tags
for v in 0.59.0 0.50.0; do
  git worktree add --detach /tmp/ogp-$v "$v"
  cp tests/oasgen-generator/ironic_power_spec_test.go \
     /tmp/ogp-$v/go/oasgen-provider/internal/tools/oas2jsonschema/
  (cd /tmp/ogp-$v/go/oasgen-provider && \
     go test ./internal/tools/oas2jsonschema/ -run TestIronicPowerNodeIDSpecField -v)
done
```

Note the **hyphen** in `krateo-platformops` and the `go/<module>/` prefix: the
unhyphenated org still resolves and serves a stale snapshot, and the repo became a
monorepo with no root `go.mod`.

The test reads the OAS by absolute path, so adjust it if this repo lives elsewhere.
It is not wired into `make check` — it needs a clone of another repository at two
tags, which is not a standing gate's business.
