# Arms contributed to upstream repositories

Test arms written against **this fleet's real configurations** for defects in
Krateo components. They live here because the configurations are ours; they are
meant to be **copied into the upstream repo**, where that project's CI breaks if
someone changes the behaviour. A cross-repo reference would rot — nothing stops
us restructuring `manifests/` for reasons unrelated to their test suite.

## `ironic_real_definitions_test.go` → oasgen-provider, PR #456 (issue #455)

Drop into `go/oasgen-provider/internal/tools/oas2jsonschema/`. It uses that
package's own helpers (`specShape`, `hasProp`) and compiles against the
`fix/455-guard-exempts-status-satisfiable` branch at `b9c54a6e`.

**It is an over-eagerness arm, not a fix arm.** All four of this repo's
RestDefinitions that set `excludedSpecFields: [uuid]` supply that `uuid`
through a request-direction mapping — `uuid <- status.uuid` for ports,
`uuid <- spec.name` for portgroup/allocation/deploy-template — so the
**pre-existing** `mappingSupplied` exemption already covers them. Measured on a
live cluster: the `ports` CRD generated *by* oasgen 0.51.0 (uid `3fc83f7d`,
created after the upgrade) has `uuid` absent from `properties` **and**
`required`.

So these four must behave **identically** before and after #456. That is
precisely why they are worth asserting: the only real risk in #456 is exempting
too much, and an arm over a case that already looks correct is the one most
likely to be skipped.

Three sub-arms:

| arm | asserts |
|---|---|
| `TestIronicDefinitionsAreMappingSupplied` | `uuid` is recognised as mapping-supplied for all four, so it never enters `requiredParameters` |
| `TestIronicDefinitionsExcludeUUIDCleanly` | the exclusion is honoured and removes **only** `uuid` |
| `TestARequiredParameterNothingSuppliesIsStillRefused` | **the control** — a required parameter nothing supplies is still refused and still survives, so #410 has not re-opened |

Without the third, "`uuid` disappears" in the second is satisfied by a fix that
simply stopped guarding anything.

**Not run here.** Executing code cloned from an external repository is outside
this session's permissions, and contributing the arm is the better outcome
regardless: upstream CI runs it on every change, a local run would be a one-off.
