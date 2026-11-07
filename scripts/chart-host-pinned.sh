#!/usr/bin/env bash
# Restore the chart versions the live CompositionDefinitions PIN, not the ones
# currently in Chart.yaml.
#
# Why this exists. `make chart-host` packages whatever version each chart has on
# disk right now. A CompositionDefinition pinning an OLDER version then cannot
# fetch its chart, chart-inspector returns 500, and cdc reports
#
# The premise this script was written on is GONE as of 2026-11-07: it said "that
# pod has NO VOLUMES, so everything it serves lives in its container filesystem
# and is lost the moment the pod is recreated -- which happens on every cluster
# restart", and that was true and was the bug. local/chartrepo.yaml now mounts a
# ConfigMap, whose content lives in etcd and survives pod restarts, node
# restarts and `docker stop`. So this is no longer a restore-on-every-resume
# chore; it is for adding a pinned version that is no longer in Chart.yaml.
#
#   observe failed: generating RBAC using chart-inspector: ... status code: 500
#
# which reads as a chart-inspector fault rather than a missing file. Measured
# 2026-11-04: after a restart, all four CDs pinned versions the chartrepo no
# longer had, and every BaremetalHost went Synced=False.
#
# This script reads the pinned version from each CompositionDefinition, finds the
# commit where that chart last carried it, packages it from git, and uploads it.
# Charts served from OCI rather than the in-cluster chartrepo are skipped — they
# are unaffected by the pod being recreated.
set -euo pipefail

KUBECONFIG_FILE="${KUBECONFIG_FILE:-local/kubeconfig.ironic-kog}"
KCTX="${KCTX:-kind-ironic-kog}"
IRONIC_NS="${IRONIC_NS:-openstack}"
K="kubectl --kubeconfig ${KUBECONFIG_FILE} --context ${KCTX}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

POD="$($K -n "$IRONIC_NS" get pod -l app=chartrepo -o jsonpath='{.items[0].metadata.name}')"
[ -n "$POD" ] || { echo "FATAL: no chartrepo pod" >&2; exit 1; }
echo "chartrepo pod: $POD"

# name<TAB>version<TAB>url, one CompositionDefinition per line
$K -n krateo-system get compositiondefinitions -o json \
  | python3 -c '
import json,sys
for i in json.load(sys.stdin)["items"]:
    c = i["spec"]["chart"]
    print("%s\t%s\t%s" % (i["metadata"]["name"], c.get("version",""), c.get("url","")))
' > "$WORK/cds"

rc=0
PENDING=()   # declared before the loop: ${#PENDING[@]} under `set -u` is fatal if the loop body never runs
while IFS=$'\t' read -r name version url; do
  case "$url" in
    *chartrepo*) ;;
    *) echo "  SKIP  $name $version — not served from chartrepo ($url)"; continue ;;
  esac

  tgz="${name}-${version}.tgz"
  if $K -n "$IRONIC_NS" exec "$POD" -- test -f "/usr/share/nginx/html/$tgz" 2>/dev/null; then
    echo "  OK    $tgz already served"
    continue
  fi

  # The commit where this chart last carried the pinned version. Reading
  # Chart.yaml at each commit rather than `git log -S`, because -S matches the
  # commit that CHANGED the string — which is as likely to be the bump away
  # from it as the bump to it.
  sha="$(python3 - "$name" "$version" <<'PY'
import subprocess, sys
chart, want = sys.argv[1], sys.argv[2]
log = subprocess.run(["git","log","--format=%H","--",f"charts/{chart}/Chart.yaml"],
                     capture_output=True, text=True).stdout.split()
for c in log:
    y = subprocess.run(["git","show",f"{c}:charts/{chart}/Chart.yaml"],
                       capture_output=True, text=True).stdout
    for line in y.splitlines():
        if line.startswith("version:"):
            if line.split()[1] == want:
                print(c)
                sys.exit(0)
            break
PY
)"
  if [ -z "$sha" ]; then
    echo "  FAIL  $tgz — no commit found where charts/$name is $version" >&2
    rc=1
    continue
  fi

  git archive "$sha" "charts/$name" | tar -x -C "$WORK"
  built="$(awk '/^version:/{print $2; exit}' "$WORK/charts/$name/Chart.yaml")"
  # Assert rather than assume: a wrong checkout would otherwise upload a chart
  # under a filename that does not match its contents, which is worse than the
  # missing file it replaces.
  [ "$built" = "$version" ] || { echo "  FAIL  $name: git gave $built, wanted $version" >&2; rc=1; continue; }

  helm package "$WORK/charts/$name" -d "$WORK" >/dev/null
  # Collected, not copied. `kubectl cp` into /usr/share/nginx/html stopped
  # working when that path became a read-only ConfigMap mount -- it fails with
  # "Read-only file system" and, under `set -euo pipefail`, aborted this script.
  # The packages are merged into the ConfigMap in one call after the loop.
  PENDING+=("$WORK/$tgz")
  echo "  BUILT $tgz  (from ${sha:0:12})"
done < "$WORK/cds"

if [ "${#PENDING[@]}" -gt 0 ]; then
  python3 hack/chartrepo-sync.py \
    --namespace "$IRONIC_NS" --kubeconfig "$KUBECONFIG_FILE" --context "$KCTX" \
    "${PENDING[@]}" | $K apply --server-side --force-conflicts -f -
else
  echo "  nothing to add: every pinned version is already in Chart.yaml"
fi

echo "chartrepo now serving:"
$K -n "$IRONIC_NS" exec "$POD" -- ls -1 /usr/share/nginx/html/ | grep '\.tgz$' | sed 's/^/    /'
exit $rc
