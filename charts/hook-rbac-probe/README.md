# hook-rbac-probe — a measurement, not a product

Throwaway chart whose only job is its **object shape**: four objects from one
template, exactly one carrying `helm.sh/hook`. It exists to measure
core-provider's generated RBAC for helm lifecycle hooks (#311 / #316) and is
not part of the blueprint bundle. Do not register it as a
`CompositionDefinition` in a real environment.

## Why the shape matters

Before #316, core-provider derived a composition's permissions from a helm
dry-run **install**, and helm keeps hooks in `release.Hooks` rather than in
`release.Manifest` — so hook objects got no grant. The uninstall then failed
permanently, because `before-hook-creation` must **delete** the previous hook
object and the generated ServiceAccount could not.

## Result, core-provider 2.13.58, measured 2026-11-04

All four objects get a grant, where three did before:

```
""                         serviceaccounts   ['*']    plain
rbac.authorization.k8s.io  roles             ['*']    plain
rbac.authorization.k8s.io  rolebindings      ['*']    plain
batch                      jobs              ['*']    HOOK   <- was absent
```

The verbs are `*` on **all four**, not the enumerated
`get,list,watch,create,delete` the fix was described with. So the hook's row
is no wider than the plain rows; a chart with hooks gains a resource, not
broader verbs.

**And the delete path works**, which is the half a generator test cannot
reach. Two full create/delete cycles with `hook-delete-policy:
before-hook-creation` only — deliberately *without* `hook-succeeded`, or the
Job is removed on success and the DELETE verb is never exercised:

```
cycle 1   uninstall completed ~20s   Job uid 9242bbf7-...
cycle 2   uninstall completed ~20s   Job uid 60929888-...
```

Different uids on the same-named object: cycle 2 deleted cycle 1's Job and
created a new one. Events confirm the hook actually ran rather than being
skipped — `SuccessfulCreate` → `Created pod` → `Job completed`. Absence of the
Job alone would have proved nothing.

## Note for anyone adding a chart here

`values.schema.json` must **not** set `additionalProperties: false`. Helm
injects a `global` key and chart-inspector performs a real dry-run install, so
a strict schema fails generation with an opaque 500 rather than a validation
message. Every other chart in this repo leaves it unset; this one learned why.
