---
type: Runbook
title: Orphan-release detection & recovery
description: How to detect and recover a desynced helm release — when cdc's install/upgrade is interrupted and the BaremetalHost CR, the Node/Port/NodePower CRs and the helm release secrets fall out of sync.
resource: oci://ghcr.io/krateo-blueprints/charts/baremetal-host
tags: [ironic, recovery, runbook, helm, cdc]
timestamp: 2026-08-11T00:00:00Z
---

# Orphan-release detection & recovery

When cdc's helm-install/upgrade is interrupted in a way that desyncs the BH CR, the
Node/Port/NodePower CRs, and the helm release secrets, the chart cannot reconcile
forward. New `kubectl apply baremetalhost` re-issues a fresh release name (derived
from the BH UID), but Helm refuses to import resources whose ownership metadata
points at the old release. The visible symptom from cdc logs:

```
install failed: Unable to continue with install: Node "blade05" in namespace
"openstack" exists and cannot be imported into the current release: invalid
ownership metadata; annotation validation error: key "meta.helm.sh/release-name"
must equal "blade05-<new-suffix>": current value is "blade05-<old-suffix>"
```

Until cleaned, every cdc reconcile and chart-inspector call for that BH returns 500.

## Triggers we hit this session

1. **Force-removing the `composition.krateo.io/finalizer` on a BH CR** while cdc was
   mid-handler-Delete. The helm release was uninstalled but the rendered children
   (Node, Ports) survived because the finalizer-clear short-circuited cdc's
   teardown.
2. **Deleting + re-creating the CompositionDefinition** while a BH still existed.
   When the CD came back, cdc generated a new release name; old children still had
   the previous release annotations.
3. **kube-apiserver CRD finalizer GC** (the original bug we chased — see
   `memory/reference_krateo-cd-stuck-crd-finalizer.md`). When the CRD enters
   Terminating and the apiserver deletes every BH instance every ~60s, the helm
   release sticks around but the BH is gone, while cdc keeps trying to reconcile.

## Detection

One liner that returns non-empty when at least one orphan secret exists:

```bash
KUBECONFIG=… kubectl get secret -n openstack -o json |
  jq -r '.items[] | select(.metadata.name | startswith("sh.helm.release.v1.")) | .metadata.name' |
  while read s; do
    rel=$(echo "$s" | sed -E 's/sh\.helm\.release\.v1\.([^.]+)\..*/\1/')
    bh_name=$(echo "$rel" | sed -E 's/-[a-z0-9]{8}$//')
    if ! kubectl get -n openstack baremetalhost "$bh_name" >/dev/null 2>&1; then
      echo "ORPHAN: secret=$s (no BH/$bh_name)"
    fi
  done
```

(The chart's release name is `<bh-name>-<8-char-hash>`; stripping the hash gives the
BH name to check.)

You can also spot stuck cdc reconciles by `kubectl events` for
`CannotCreateExternalResource` warnings with the "invalid ownership metadata" text.

## Cleanup procedure

Do these in order. Each step is idempotent.

**Step 1 — list orphan releases for the affected BH:**

```bash
BH=blade03   # name of the BH that the orphan is for
KUBECONFIG=… kubectl -n openstack get secret -o name |
  grep "sh.helm.release.v1.${BH}-"
```

If you see secrets here AND `kubectl get bh "$BH"` returns NotFound, those are
orphans.

**Step 2 — strip helm ownership annotations from any surviving rendered children.**

These are the per-BH CRs created by the chart (Node, Port, NodeProvision, NodePower).
They carry `meta.helm.sh/release-*` annotations and `app.kubernetes.io/managed-by:
Helm` labels. We don't want to delete them (the Ironic-side blade is fine and
adopting it back is faster than re-enrolling), so just disown them from the dead
release:

```bash
for r in node.baremetal.ogen.krateo.io/${BH} \
         port.baremetal.ogen.krateo.io/${BH}-00-60-2f-XX-XX-XX \  # MACs per blade
         port.baremetal.ogen.krateo.io/${BH}-00-60-2f-XX-XX-XX; do
  kubectl -n openstack annotate "$r" \
    meta.helm.sh/release-name- meta.helm.sh/release-namespace- \
    --overwrite 2>/dev/null || true
  kubectl -n openstack label "$r" \
    app.kubernetes.io/managed-by- \
    --overwrite 2>/dev/null || true
done
```

If a Node CR has `metadata.deletionTimestamp` set (KOG-RDC keeps 409'ing trying to
delete an active Ironic node), force-clear its finalizers too — but ONLY after you
confirm via the Ironic API that the blade is in a state you want to keep:

```bash
kubectl -n openstack patch node.baremetal.ogen.krateo.io/${BH} \
  --type=json -p='[{"op":"replace","path":"/metadata/finalizers","value":[]}]'
```

**Step 3 — delete the orphan helm release secrets:**

```bash
kubectl -n openstack get secret -o name |
  grep "sh.helm.release.v1.${BH}-" |
  xargs -I {} kubectl -n openstack delete {}
```

**Step 4 — re-apply the BH manifest at the current `apiVersion`:**

```bash
kubectl apply --server-side --force-conflicts \
  -f manifests/baremetalhost-${BH}-*.yaml
```

cdc's next reconcile will helm-install fresh. Children get adopted (the unowned
Node + Ports are now reachable to the new release). Walk continues from wherever
Ironic actually is.

## Preventing orphans

- **Don't `kubectl delete crd baremetalhosts.composition.krateo.io`.** That's the
  root cause of the stuck-finalizer cascade. If the CRD enters Terminating, fix
  *that* first per `memory/reference_krateo-cd-stuck-crd-finalizer.md`.
- **Don't `kubectl patch baremetalhost ... -p='[{"op":"replace","path":"/metadata/finalizers","value":[]}]'`
  while cdc is mid-reconcile.** If you need to force-remove the cdc finalizer (the
  blade is genuinely gone, you don't care about Ironic-side cleanup), confirm cdc
  isn't actively processing a delete *first* — check its logs for `event: delete`
  on that name.
- **Don't delete + re-create the CompositionDefinition while BHs exist.** Bump the
  chart version through it instead. core-provider handles that path safely; full
  CD deletion is the dangerous one.

## Limits of this procedure

- It only handles the "helm release stranded, children survive" case. If the Ironic
  node itself is in a state that doesn't match Helm's view (e.g., Ironic is
  `wait call-back` but the chart expected `available`), this procedure clears the
  k8s side but doesn't reconcile Ironic — you still need to drive the state
  machine via the chart's normal `spec.undeploy` / `spec.image` fields.
- If the orphan is from a cdc bug that re-creates the same release name (we never
  hit this; included for completeness), this loop won't terminate; you'll need to
  pause cdc (`scale deploy/baremetalhosts-vX-Y-Z-controller --replicas=0`), clean
  up, then scale back.
