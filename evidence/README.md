# evidence/

Durable copies of cluster state whose only native channel expires.

## schemawarnings-2026-11-05T0608Z.json

oasgen emits its CRD generation and validation warnings as a Kubernetes
**Warning Event** (`reason=SchemaWarnings`, `restdefinition.go:1031`) and
**nowhere else** — no condition on the RestDefinition carries them. The
apiserver's default `--event-ttl` is one hour, and the emitting code path runs
only on create or on an OAS/spec change, so for a long-lived definition the
warnings fire once at install and are garbage-collected an hour later.

The facts they report are permanent. The channel that reports them is not.

These three events were emitted at 2026-11-05T06:08:21Z by the regeneration in
§9.12, captured here before they expired at ~07:08. `ironic-node` has none,
consistent with it being the one definition that declares no `excludedSpecFields`.

Captured because a one-hour record of a permanent condition is not a record.
