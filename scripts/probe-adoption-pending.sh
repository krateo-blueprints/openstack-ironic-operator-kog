#!/usr/bin/env bash
# core-provider #303/#322/#329 adoption probe, pending-install orphan variant.
#
# Reaches the orphan the way core-provider's own e2e does -- remove the
# finalizer, THEN delete the CR -- so the release survives and the two readings
# stay comparable. Modelling a controller absent at delete time.
#
# The `pending-install` status is CONSTRUCTED on the surviving release secret,
# and this script says so in its own output. In the wild that status comes from
# an interrupted install; what is under test is cdc's handling of the state, not
# how the state arose. Constructing it is the only way to get there without
# racing cdc's own install.
#
# Expected path, per core-provider's reading of the 2.13.65 code (DERIVED by
# them, measured here):
#   grace = 5m default (COMPOSITION_CONTROLLER_PENDING_GRACE), measured from the
#   release's Info.LastDeployed, NOT from when cdc first saw it.
#   During grace: Ready=False reason=PendingHelmOperation, message carries
#   `pendingFor`. That means WAITING, not wedged.
#   After grace: rollback, then the next reconcile proceeds.
#   Ceiling: ~5m grace + 60s resync + the operations themselves ~= 7m.
#   At 10m still PendingHelmOperation with pendingFor past grace => WEDGED.
set -uo pipefail
# RUN THIS WITH BASH, NOT ZSH. The $K/$H command variables below rely on word
# splitting, which bash does and zsh does NOT by default -- under zsh the whole
# string is one word and `$K get ...` fails with "command not found", or worse,
# silently returns nothing inside a $(...) that the caller then reads as "no
# results". Verified 2026-11-05: the same one-liner finds 21 helm secrets under
# bash and 0 under zsh. This bit twice in one session, once as a false
# "PROBE FAILED" from a monitor and once as an empty JSON parse.
K="kubectl --kubeconfig local/kubeconfig.ironic-kog --context kind-ironic-kog"
H="helm --kubeconfig local/kubeconfig.ironic-kog --kube-context kind-ironic-kog"
NS=openstack
CRD=releaseadoptprobes
GRP=composition.krateo.io
CEILING_S=${CEILING_S:-660}   # 11 minutes: core-provider's 10m call + a margin

say() { printf '%s %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }

say "== core-provider version under test =="
$K -n krateo-system get deploy krateo-core-provider \
  -o jsonpath='   {.spec.template.spec.containers[0].image}{"\n"}'

VER=$($K get crd ${CRD}.${GRP} -o jsonpath='{range .spec.versions[?(@.served==true)]}{.name}{end}' 2>/dev/null)
[ -z "$VER" ] && { say "FATAL: no served version for ${CRD}.${GRP} -- is the probe CompositionDefinition installed?"; exit 9; }
say "served version: $VER"

CR="$CRD.$VER.$GRP"
REL=$($K -n $NS get "$CR" adopt-probe -o jsonpath='{.metadata.labels.krateo\.io/release-name}' 2>/dev/null)
[ -z "$REL" ] && { say "FATAL: no release-name label on adopt-probe -- baseline not established"; exit 9; }
say "baseline release: $REL"

say "== (a) BEFORE =="
$H list -A --all 2>/dev/null | awk 'NR==1||/adopt/'

say "== making the orphan: finalizer off, then delete =="
$K -n $NS patch "$CR" adopt-probe --type=merge -p '{"metadata":{"finalizers":null}}' >/dev/null 2>&1 \
  || { say "FATAL: could not clear the finalizer"; exit 9; }
$K -n $NS delete "$CR" adopt-probe --wait=true --timeout=60s >/dev/null 2>&1
sleep 5
SURV=$($H list -A --all 2>/dev/null | grep -c "$REL")
say "release survived the delete: $SURV (1 = orphan created, 0 = it was uninstalled anyway)"
[ "$SURV" = "0" ] && { say "ABORT: no orphan -- the finalizer removal did not prevent the uninstall"; exit 1; }

say "== CONSTRUCTING pending-install on the surviving release (stated, not natural) =="
SEC=$($K -n $NS get secret -l "name=$REL,owner=helm" -o jsonpath='{.items[-1].metadata.name}' 2>/dev/null)
[ -z "$SEC" ] && { say "FATAL: helm release secret for $REL not found"; exit 9; }
say "release secret: $SEC"
$K -n $NS label secret "$SEC" status=pending-install --overwrite >/dev/null 2>&1
$H list -A --all 2>/dev/null | awk 'NR==1||/adopt/'

say "== recreating the CR (fresh, so NO release-name label) =="
$K apply -f "$1" >/dev/null 2>&1 || { say "FATAL: could not apply $1"; exit 9; }

say "== observing: expect adopt -> helm lock -> ~5m grace -> rollback -> upgrade =="
T0=$(date +%s)
PREV=""
while :; do
  EL=$(( $(date +%s) - T0 ))
  [ "$EL" -gt "$CEILING_S" ] && { say "CEILING $CEILING_S s reached"; break; }
  LINE=$($K -n $NS get "$CR" adopt-probe -o json 2>/dev/null | python3 -I -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: print('read-failed'); raise SystemExit
l=(d['metadata'].get('labels') or {}).get('krateo.io/release-name')
c={x['type']:(x.get('reason'),(x.get('message') or '')[:90]) for x in (d.get('status',{}).get('conditions') or [])}
r=c.get('Ready',('?',''));s=c.get('Synced',('?',''))
print(f\"label={l} Ready={r[0]} Synced={s[0]} | {r[1]}\")")
  RELS=$($H list -A --all 2>/dev/null | grep adopt | awk '{printf "%s(rev%s,%s) ", $1,$3,$8}')
  CUR="$LINE | $RELS"
  if [ "$CUR" != "$PREV" ]; then
    say "t+${EL}s  $LINE"
    say "         releases: $RELS"
    PREV="$CUR"
  fi
  case "$LINE" in *"Ready=Available"*) say "RECONCILE COMPLETED at t+${EL}s"; break;; esac
  sleep 15
done

say "== (a) AFTER =="
$H list -A --all 2>/dev/null | awk 'NR==1||/adopt/'
say "== (b) adopted release history =="
$H history "$REL" -n $NS 2>/dev/null | tail -5
say "== (c) final conditions =="
$K -n $NS get "$CR" adopt-probe -o json 2>/dev/null | python3 -I -c "
import json,sys
d=json.load(sys.stdin)
for c in (d.get('status',{}).get('conditions') or []):
    print(f\"   {c['type']}={c['status']} reason={c.get('reason')}\")
    if c.get('message'): print(f\"      {c['message'][:200]}\")"
