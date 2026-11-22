#!/bin/bash
# ARM 7 -- what does a live NodeProvision CR rest at?  (v2, after a VOID first run)
#
# THE FIRST RUN (2026-11-15, arm7-void-run.log) IS VOID. Four instrument defects,
# all mine, kept here because three of them produced plausible-looking output:
#
#   1. THE SETUP COULD NOT PRODUCE THE STATE. The node sits at provision_state
#      `enroll` and the arm asked for `target: active`. Ironic's machine is
#      enroll -manage-> manageable -provide-> available -active-> active, so
#      `active` from `enroll` is INVALID and every reconcile was a rejected
#      transition. The VOID gate accepted enroll as provisionable -- my error. So
#      the run measured a refusal, not a resting state, and the analysis would have
#      printed "THE EXEMPTION PREMISE HOLDS" from it.
#      v2 asks for `target: manage`, which IS valid from enroll and touches no
#      disk, and it requires the state to MOVE as a positive control.
#
#   2. THE DISPATCH COUNT WAS STRUCTURALLY ZERO. It grepped rdc's log for the PUT,
#      which rdc logs at Debug (restResources.go:1324, :445), and
#      REST_CONTROLLER_DEBUG defaults FALSE. So "PUTs=0" was the instrument, not the
#      world -- and the count is the output I had told oasgen was primary.
#
#      v2's FIRST FIX WAS ALSO WRONG, and failed in the safe direction: it tried to
#      turn debug on by setting the env on the rdc Deployment. That cannot work and
#      must not be done. The Deployment takes env via `envFrom` from an
#      oasgen-RENDERED ConfigMap (ironic-node-provision-controller-configmap),
#      oasgen reconciles both, and at 0.61.0 that map carries no
#      REST_CONTROLLER_DEBUG key at all -- measured on the live cluster, env was
#      empty after `kubectl set env` and a successful rollout. Editing either object
#      would be patching controller-managed state. The gate refused, correctly.
#
#      THE RIGHT INSTRUMENT NEEDS NO DEBUG AND IS EXACT. rdc maintains the number
#      itself: reportInferredAbsence calls inferredCounter.record and persists it on
#      the CR as `krateo.io/inferred-absence-repeats` (latched_absence.go:55),
#      logging it at INFO, with the semantics in its own event text -- "has had its
#      absence inferred ... %d times for the same spec generation: the create that
#      followed the previous inference did not make it findable". That is the
#      dispatch count, maintained by the thing being measured, one kubectl get away.
#
#   3. `grep -c` PRINTS 0 AND EXITS 1, so `|| echo 0` appended a SECOND line and
#      put a newline inside a CSV field. That misaligned every row: the analysis
#      read the gate column as a Ready reason ("distinct Ready reasons: yes") and
#      then crashed on a None. A crash beat a false verdict by luck, not design.
#      v2 captures counts as single values and the analysis REFUSES a CSV whose
#      rows do not all have the expected field count.
#
#   4. A leftover `put_logged` call after renaming the function to `put_count`
#      ("line 326: command not found"). Cosmetic, but it printed `PUT=` and read
#      like a measurement.
#
# WHAT THE VOID RUN STILL SHOWED, from its human progress lines rather than its
# CSV, and worth carrying because it is the first live NodeProvision CR this repo
# has ever had: across ~14 minutes in BOTH orderings the CR held
# Ready=Creating / Synced=ReconcileError, provision_state never left enroll, and
# the synthesised-absence count GREW (3->5 and 3->4). So Observe reported absent
# repeatedly and the runtime re-created repeatedly -- the re-dispatch direction --
# and the resting condition was `Creating`, which nobody predicted: not Pending,
# not Available. Under a transition that could never succeed, so not a result.
#
# WHY IT IS SAFE HERE. local/ironic.conf runs enabled_deploy_interfaces = fake and
# default_deploy_interface = fake, asserted before anything runs. `manage` does not
# touch a disk under any driver; the fake deploy interface means even `active`
# would not. The hazard that makes this kind terminal on real blades does not exist
# on a kind cluster.
#
# BOTH ORDERINGS, SEPARATED, because whether findby can match on the first
# reconcile is the whole question:
#   A  node exists FIRST, then the CR   <- what the charts produce
#   B  the CR exists first, pointing at a node that does not exist yet
#
# usage:  arm7.sh | arm7.sh --analyze FILE.csv | arm7.sh --self-test
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# PATHS ARE DERIVED, NOT HARDCODED. This arm lived in a session scratchpad whose
# absolute path was baked into two variables; committed as-is it would write to a
# directory belonging to a session that no longer exists. REPO comes from the
# script's own location so the file can be moved or checked out anywhere, and the
# output directory is overridable with ARM7_OUT for a run whose artefacts must
# land somewhere durable.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRATCH="${ARM7_OUT:-${TMPDIR:-/tmp}/arm7-$$}"
mkdir -p "$SCRATCH" || { echo "cannot create output dir $SCRATCH" >&2; exit 1; }
FIELDS=18

analyze() {
python3 -I - "$1" "$FIELDS" <<'PYEOF'
import sys, csv, collections
path, want = sys.argv[1], int(sys.argv[2])

raw = open(path).read().splitlines()
if len(raw) < 2:
    print("ANALYSIS VOID -- fewer than two lines in the CSV."); sys.exit(4)
hdr = raw[0].split(",")
bad = [(i + 2, len(l.split(","))) for i, l in enumerate(raw[1:]) if len(l.split(",")) != want]
if len(hdr) != want:
    print("ANALYSIS VOID -- header has %d fields, expected %d." % (len(hdr), want)); sys.exit(4)
if bad:
    print("ANALYSIS VOID -- %d of %d rows have the wrong field count (expected %d)."
          % (len(bad), len(raw) - 1, want))
    print("   first offenders (line, fields): %s" % bad[:5])
    print("   A misaligned CSV reads one column as another. The v1 run did exactly this")
    print("   and the analysis reported a gate value as a Ready reason before crashing.")
    sys.exit(4)

rows = list(csv.DictReader(raw))

# NAME THE REF, AND REFUSE A CSV THAT MIXES TWO. This arm exists to be compared
# against the 0.61.0 run, so the version is part of the reading, not part of the
# filename -- a filename does not survive concatenating the two runs into one
# table, which is exactly how the comparison gets made. More than one version in
# one file means two runs were merged, and every per-ordering count below would
# then be summed across versions while still looking like a single clean number.
vers = sorted({r.get("ver", "") for r in rows})
print("  measured at oasgen/rdc : %s" % ",".join(vers))
if len(vers) != 1:
    print("ANALYSIS VOID -- %d distinct versions in one CSV: %s" % (len(vers), vers))
    sys.exit(4)

by = collections.defaultdict(list)
for r in rows:
    by[r["ordering"]].append(r)

print("===== READINGS =====")
verdicts = {}
for ordering in sorted(by):
    rs = by[ordering]
    print()
    print("  ORDERING %s  (%d samples, %s..%ss)" % (ordering, len(rs), rs[0]["elapsed"], rs[-1]["elapsed"]))
    g = lambda k: [r[k] for r in rs]
    ints = lambda k: [int(r[k] or 0) for r in rs]
    inf, lat = ints("inferred_repeats"), ints("latched_repeats")
    # THE DISPATCH COUNT IS THE *INFERRED* COUNTER, NOT THE LATCHED ONE. v6 had
    # `puts = lat` and so called a climbing latch a re-dispatch loop -- backwards,
    # and it produced a confident false alarm on the first run whose window was
    # long enough to reach the latch threshold. rdc's own source settles which is
    # which:
    #   inferred-absence-repeats : "absence inferences on the MAIN path (the
    #                               else-if, WHERE A CREATE DOES FOLLOW)"
    #   latched-absence-repeats  : "consecutive observes that took the LATCH with a
    #                               synthesised not-found"
    # inferred counts DISPATCHES; latched counts SUPPRESSIONS. A flat inferred with
    # a climbing latched is the premise HOLDING -- the exact shape v6 inverted.
    puts, aes = inf, ints("absence_events")
    gens = sorted({r["generation"] for r in rs if r["generation"]})
    readable = [r["counter_readable"] for r in rs]
    states = g("provision_state")
    timeline = [p for i, p in enumerate(states) if i == 0 or p != states[i - 1]]
    print("    Ready reason/status seen : %s  /  %s" % ("|".join(sorted(set(g("ready")))),
                                                            "|".join(sorted(set(g("ready_status"))))))
    print("    Synced reasons seen      : %s" % "|".join(sorted(set(g("synced")))))
    print("    provision_state timeline : %s" % " -> ".join(timeline))
    print("    DISPATCHES (inferred)    : %d -> %d   %s" % (min(inf), max(inf),
          "RE-DISPATCHING" if max(inf) - min(inf) >= 2 else "one-shot"))
    print("    SUPPRESSED (latched)     : %d -> %d   %s" % (min(lat), max(lat),
          ("latch held; THRESHOLD REACHED (>=5), so rdc fires its stuck-latch Warning"
           if max(lat) >= 5 else ("latch held" if max(lat) else "latch never taken"))))
    print("    generation(s) seen       : %s" % (",".join(gens) or "none"))
    print("    gate basis               : %s" % rs[0]["gate_basis"])
    print("    synthesised-absence      : %d -> %d" % (min(aes), max(aes)))
    print("    last_error               : %s" % (sorted({r["last_error"] for r in rs
                                                         if r["last_error"] not in ("None", "", "null")})[:1] or "none"))

    # VOID GATES FIRST.
    if len(gens) > 1:
        verdicts[ordering] = ("VOID", "metadata.generation changed (%s) mid-window, so the absence "
                                      "counters reset and no count is comparable" % ",".join(gens)); continue
    if "yes" not in readable:
        verdicts[ordering] = ("VOID", "neither absence counter was ever readable, so a "
                                      "dispatch count of %d is an absence of instrument, not of "
                                      "dispatches" % max(puts)); continue
    if rs[0]["gate_target_valid"] != "yes":
        verdicts[ordering] = ("VOID", "the requested target was not valid from the node's state, so a "
                                      "refusal says nothing about resting behaviour"); continue
    if all(s in ("None", "", "null") for s in states):
        verdicts[ordering] = ("VOID", "provision_state never read -- the instrument"); continue
    if not any(r["ready"] not in ("", "ABSENT") for r in rs):
        verdicts[ordering] = ("VOID", "the CR never reported a Ready condition -- a missing "
                                      "Configuration or unserved CRD, not a resting state"); continue

    moved = len([s for s in set(states) if s not in ("None", "", "null")]) > 1
    final = next((r["ready"] for r in reversed(rs) if r["ready"] not in ("", "ABSENT")), "")
    redispatch = (max(puts) - min(puts)) >= 2

    # The POSITIVE CONTROL for the whole arm: the transition must actually happen.
    if not moved and max(puts) == 0:
        verdicts[ordering] = ("VOID", "the state never moved AND nothing was ever dispatched, so "
                                      "was exercised -- setup failure, not a finding"); continue

    if redispatch:
        verdicts[ordering] = ("RE-DISPATCH_LOOP",
            "%d -> %d dispatches across the window; state %s; CR ended Ready=%s"
            % (min(puts), max(puts), "moved: " + " -> ".join(timeline) if moved else "never moved", final))
    elif moved and final == "Available":
        verdicts[ordering] = ("CONVERGES", "the transition landed and the CR reports Available")
    elif moved and final != "Available":
        verdicts[ordering] = ("RESTS_NOT_READY",
            "the transition landed (%s) and the CR still rests at Ready=%s" % (" -> ".join(timeline), final))
    elif not moved and final == "Available":
        verdicts[ordering] = ("READY_WHILE_INERT",
            "Ready=Available with the state never moving and %d dispatch(es)" % max(puts))
    else:
        verdicts[ordering] = ("UNCLASSIFIED", "moved=%s final=%s puts=%d" % (moved, final, max(puts)))

print()
print("===== VERDICT =====")
for o in sorted(verdicts):
    v, why = verdicts[o]
    print("  ORDERING %s : %-18s %s" % (o, v, why))
vals = {v for v, _ in verdicts.values()}
print()
if "VOID" in vals or "UNCLASSIFIED" in vals:
    print("NOT A READING for at least one ordering. The orderings differ in whether findby can")
    print("match, which is the whole question, so a valid arm does not answer for the other.")
    sys.exit(4)
if "RE-DISPATCH_LOOP" in vals:
    print("THE KIND RE-ISSUES ITS TRANSITION. Counted from rdc's own persisted counter.")
    print("It does not rest inertly: Observe reports absent, the runtime creates, forever.")
    print("Exempting it as 'healthy, never stalled' would silence a repeating write.")
elif vals == {"RESTS_NOT_READY"}:
    print("THE EXEMPTION PREMISE HOLDS: the transition SUCCEEDED and the CR still rests at")
    print("not-ready. That is the state the exemption is about, now measured rather than argued.")
elif vals == {"CONVERGES"}:
    print("THE PREMISE IS WRONG: this kind converges, so it is no more terminal than")
    print("BaremetalNode and must NOT be exempted.")
elif "READY_WHILE_INERT" in vals:
    print("THE PREMISE IS WRONG THE OTHER WAY: healthy while inert. No exemption needed; the")
    print("exposure is a FALSE HEALTHY across the six renderable render sites.")
else:
    print("THE ORDERINGS DISAGREE: %s -- itself the finding." % sorted(vals))
print()
print("Fake deploy interface, kind cluster, target=manage. What transfers is the CONTROL-PLANE")
print("behaviour -- dispatched or not, resting or not -- never a duration.")
PYEOF
}

self_test() {
  # EVERY CASE ASSERTS. This printed "exit 4 (want 4)" and never compared the two,
  # so it exited 0 with the v6 latched/inferred defect reintroduced -- a screen
  # that cannot fail, which is worth less than no screen because it reports
  # success. Proven by mutation before this rewrite: reverting `puts = inf` to
  # `puts = lat` left the suite green.
  local f="$SCRATCH/arm7-selftest.csv" fails=0 n=0
  H="elapsed,ordering,ready,ready_status,synced,observed,provision_state,target_provision_state,provision_updated_at,last_error,inferred_repeats,latched_repeats,generation,absence_events,gate_target_valid,gate_basis,counter_readable,ver"
  mk() { # 1 name 2 ready 3 states 4 latchmode 5 gate_target 6 counter_readable 7 "grow"=grow INFERRED
    { echo "$H"; local i=0; for st in $3; do
        local pc=0; [ "$4" = 1 ] && pc=1; [ "$4" = 2 ] && pc=$((1+i))
        local ic=1; [ "${7:-}" = grow ] && ic=$((1+i))
        echo "$((i*60)),A,$2,False,ReconcileSuccess,ResponseObtained,$st,None,t,None,$ic,$pc,1,$pc,$5,measured,$6,0.0.0-selftest"
        i=$((i+1)); done; } > "$f.$1"; }

  # want_exit CASE FILE WANTED-RC LABEL
  want_exit() { n=$((n+1)); local got; analyze "$2" >/dev/null 2>&1; got=$?
    if [ "$got" = "$3" ]; then printf '  ok   %d: %s (exit %s)\n' "$1" "$4" "$got"
    else printf '  FAIL %d: %s -- exit %s, want %s\n' "$1" "$4" "$got" "$3"; fails=$((fails+1)); fi; }
  # CAPTURE, THEN MATCH -- never `analyze | grep -q`. This script runs under
  # `set -o pipefail`, and grep -q closes the pipe on its first match, so analyze
  # dies of SIGPIPE and the PIPELINE reports failure even though the text matched.
  # That produced two FAILs whose tempting "fix" was to change the analyzer's
  # output, which was correct all along.
  out_of() { analyze "$1" 2>&1 || true; }
  # want_text CASE FILE REGEX LABEL
  want_text() { n=$((n+1)); local o; o=$(out_of "$2")
    if printf '%s' "$o" | grep -qE "$3"; then printf '  ok   %d: %s\n' "$1" "$4"
    else printf '  FAIL %d: %s -- output lacks /%s/\n' "$1" "$4" "$3"; fails=$((fails+1)); fi; }
  # deny_text CASE FILE REGEX LABEL
  deny_text() { n=$((n+1)); local o; o=$(out_of "$2")
    if printf '%s' "$o" | grep -qE "$3"; then printf '  FAIL %d: %s -- output CONTAINS /%s/\n' "$1" "$4" "$3"; fails=$((fails+1))
    else printf '  ok   %d: %s\n' "$1" "$4"; fi; }

  mk holds     Pending   "enroll manageable manageable" 1 yes yes
  want_text 1 "$f.holds" "PREMISE HOLDS" "landed, rests not-ready, one dispatch -> premise HOLDS"
  mk loop      Creating  "enroll manageable manageable" 1 yes yes grow
  want_text 2 "$f.loop" "RE-ISSUES" "DISPATCH count growing -> RE-DISPATCH"
  mk conv      Available "enroll manageable manageable" 1 yes yes
  want_text 3 "$f.conv" "PREMISE IS WRONG:" "landed and Available -> CONVERGES"
  mk nodbg     Pending   "enroll manageable" 0 yes no
  want_exit 4 "$f.nodbg" 4 "counter never readable -> VOID"
  mk badtgt    Creating  "enroll enroll" 0 no yes
  want_exit 5 "$f.badtgt" 4 "target invalid from the state -> VOID (the v1 defect)"
  mk nothing   Creating  "enroll enroll" 0 yes yes
  want_exit 6 "$f.nothing" 4 "nothing moved, nothing dispatched -> VOID (setup failure)"
  mk holds2    Pending   "enroll manageable" 1 yes yes; echo "0,A,extra,field,here" >> "$f.holds2"
  want_exit 7 "$f.holds2" 4 "misaligned row -> VOID (the v1 CSV defect)"
  want_text 7 "$f.holds2" "wrong field count" "misaligned row names the reason"
  mk holds3    Pending   "enroll manageable manageable" 1 yes yes
  sed '$s/0\.0\.0-selftest/0.73.0/' "$f.holds3" > "$f.mixed"
  want_exit 8 "$f.mixed" 4 "two versions in ONE CSV -> VOID"
  want_text 8 "$f.mixed" "distinct versions" "mixed CSV names the reason"
  want_exit 8 "$f.holds3" 0 "NEGATIVE CONTROL: the same CSV unmixed is NOT VOID"
  # CASE 9 IS THE REAL 0.73.0 ORDERING-A SHAPE, and the one that produced a
  # confident false alarm: v6 read the latch as the dispatch count and called it
  # RE-DISPATCH_LOOP. Both halves are asserted -- it must read premise-holds AND
  # must not print RE-DISPATCH -- because only the pair pins the MAPPING.
  mk latchclimb Pending  "enroll manageable manageable" 2 yes yes
  want_text 9 "$f.latchclimb" "PREMISE HOLDS" "latch CLIMBS, dispatches flat -> premise HOLDS"
  deny_text 9 "$f.latchclimb" "RE-DISPATCH" "latch climb is NOT reported as re-dispatch"

  # CASE 10 EXISTS BECAUSE A MUTATION TEST FOUND IT MISSING. Deleting the HEADER
  # width gate (`if len(hdr) != want`) left the suite green: case 7 misaligns a
  # ROW, which is a different gate. The header gate is what catches a stale
  # analyzer against a new CSV shape -- exactly the situation when `ver` was added
  # as an 18th column -- so a suite that cannot see it removed is not screening it.
  mk hdr Pending "enroll manageable manageable" 1 yes yes
  sed '1s/,ver$//' "$f.hdr" > "$f.badhdr"
  want_exit 10 "$f.badhdr" 4 "HEADER of the wrong width -> VOID"
  want_text 10 "$f.badhdr" "header has [0-9]+ fields" "bad header names the reason"

  echo
  if [ "$fails" = 0 ]; then echo "SELF-TEST: $n/$n assertions passed"; return 0
  else echo "SELF-TEST: $fails of $n assertions FAILED"; return 1; fi
}

case "${1:-}" in
  --self-test|--selftest) self_test; exit $? ;;   # NOT `exit 0`: that discarded every failure
  --analyze)   [ -n "${2:-}" ] || { echo "usage: $0 --analyze FILE.csv" >&2; exit 64; }; analyze "$2"; exit $? ;;
  --*)
    # A MISTYPED MODE FLAG USED TO CREATE A KIND CLUSTER. There was no `*)` arm, so
    # anything other than the two above fell straight through to the real measurement
    # -- which prechecks the toolchain, sets CLUSTER=arm7-provision and calls
    # `make local-up`. So `--selftest` (the spelling SEVEN other screens in this repo
    # use) did not run the wrong mode, it provisioned a cluster.
    #
    # Only `--`-prefixed arguments are refused: a bare invocation with no arguments IS
    # the real arm and must keep working. `--selftest` is now an accepted alias, for
    # the same reason the python screens take both spellings.
    echo "usage: $0 [--self-test|--selftest] [--analyze FILE.csv]" >&2
    echo "       (no arguments runs the real arm, which CREATES a kind cluster)" >&2
    echo "refusing unrecognised option: $1" >&2
    exit 64 ;;
esac

for t in kind kubectl helm docker make python3; do
  command -v "$t" >/dev/null || { echo "PRECHECK FAILED: $t missing"; exit 1; }
done

CLUSTER=arm7-provision
NS=openstack
VER=${VER:-0.61.0}
TARGET=manage
K="kubectl --kubeconfig $REPO/local/kubeconfig.$CLUSTER --context kind-$CLUSTER"
NODEGVR=baremetalnodes.v1-0-0.baremetal.ogen.krateo.io
PRVGVR=nodeprovisions.v1-0-0.baremetal.ogen.krateo.io
CSV="$SCRATCH/arm7-samples-$VER.csv"

say() { echo; echo "===== $* ====="; }
fail() { echo; echo "FAIL: $*"; exit 1; }
[ "$CLUSTER" = "ironic-kog" ] && fail "refusing to run against the parked lab"

say "GUARD: target=$CLUSTER (never ironic-kog)"
docker ps -a --format '{{.Names}} {{.State}}' | grep ironic-kog | sed 's/^/  lab before: /'

say "SAFETY PREMISE, asserted not assumed"
grep -q '^enabled_deploy_interfaces = fake$' "$REPO/local/ironic.conf" || fail "enabled_deploy_interfaces is not exactly 'fake'"
grep -q '^default_deploy_interface = fake$' "$REPO/local/ironic.conf" || fail "default_deploy_interface is not fake"
echo "  fake deploy interface confirmed; and target=$TARGET touches no disk under any driver"

say "LOAD GATE (sustained <8, two readings 60s apart)"
ok=0
for a in $(seq 1 60); do
  x=$(uptime | sed -E 's/.*load averages?: *([0-9]+[.,][0-9]+).*/\1/' | tr ',' '.')
  if awk "BEGIN{exit !($x < 8)}"; then
    sleep 60
    y=$(uptime | sed -E 's/.*load averages?: *([0-9]+[.,][0-9]+).*/\1/' | tr ',' '.')
    awk "BEGIN{exit !($y < 8)}" && { echo "  window: $x then $y"; ok=1; break; }
    echo "  attempt $a: $x then $y -- waiting"
  else echo "  attempt $a: $x -- waiting"; fi
  sleep 60
done
[ "$ok" = 1 ] || fail "no sustained window under load 8"

say "STEP 1: the stack at $VER"
cd "$REPO" || fail "cd"
make local-up KIND_CLUSTER="$CLUSTER" KRATEO_OASGEN_VERSION="$VER" KRATEO_RDC_VERSION="$VER" \
  >"$SCRATCH/arm7-localup.log" 2>&1 || fail "local-up failed, see $SCRATCH/arm7-localup.log"

# NO DEBUG STEP, deliberately -- see defect 2 in the header. Debug cannot be enabled
# from outside without patching controller-managed state, and it is not needed: the
# dispatch count is persisted on the CR by rdc itself.

# PROVENANCE: WHICH DIGEST DID THE TAG ACTUALLY RESOLVE TO. The 0.73.0 run proved
# the arm64 binary RAN -- pods started, controllers reconciled, the transition
# landed -- but could not say which manifest it was pulled from. oasgen drew the
# distinction: that is a PROVENANCE gap, not the runtime gap of their 0.41.0 base
# -image swap, where no pod had ever started at all. Captured by default now,
# because the one failure it catches is the one nothing else can: a tag that
# resolved to an image nobody expected. Recorded, never asserted -- a digest that
# cannot be read must not fail an arm that is measuring something else.
say "PROVENANCE: resolved imageIDs at $VER"
{
  echo "# arm7 provenance, VER=$VER, $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "# host arch: $(uname -m)"
  $K -n krateo-system get pods \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{range .status.containerStatuses[*]}{.image}{" "}{.imageID}{"\n"}{end}{end}' 2>/dev/null \
    | grep -E 'oasgen|rest-dynamic|rdc' || echo "# NO MATCHING PODS -- provenance not captured"
} > "$SCRATCH/arm7-provenance-$VER.txt" 2>&1 || true
sed -n '2,6p' "$SCRATCH/arm7-provenance-$VER.txt" | sed 's/^/  /'
echo "  (full: $SCRATCH/arm7-provenance-$VER.txt)"

say "PROBE + instrument control"
$K -n $NS delete pod probe --ignore-not-found --timeout=60s >/dev/null 2>&1
$K -n $NS run probe --image=curlimages/curl:8.11.1 --restart=Never --command -- sleep 7200 >/dev/null 2>&1
for i in $(seq 1 30); do
  [ "$($K -n $NS get pod probe -o jsonpath='{.status.phase}' 2>/dev/null)" = "Running" ] && break; sleep 5
done
[ "$($K -n $NS get pod probe -o jsonpath='{.status.phase}' 2>/dev/null)" = "Running" ] || fail "probe not Running"
c=$($K -n $NS exec probe -- curl -sS -o /dev/null -w '%{http_code}' "http://ironic:6385/v1/nodes/nope-xyz" 2>/dev/null)
[ "$c" = "404" ] || fail "instrument control failed (HTTP $c)"
echo "  ironic control: 404 on a nonexistent node"

nodefield() { $K -n $NS exec probe -- curl -sS -H "X-OpenStack-Ironic-API-Version: 1.109" \
  "http://ironic:6385/v1/nodes/$1" 2>/dev/null | \
  python3 -I -c "import sys,json;d=json.load(sys.stdin);print(d.get('$2'))" 2>/dev/null; }

# THE DISPATCH COUNTER, read from the annotation rdc maintains itself. ABSENT is
# distinct from 0: absent means rdc has recorded no inference for this CR, while 0
# would be a recorded zero. The CSV carries the raw value and the analysis treats a
# never-readable counter as VOID rather than as a count of zero.
# TWO COUNTERS, and reading only the first is how v3 reported a flat 1 that meant
# nothing (oasgen corrected it before merge):
#   inferred-absence-repeats  reportInferredAbsence, the ELSE-IF branch -- notfound
#                             with NO Pending. This kind writes it ONCE, on the very
#                             first observe, before the create exists.
#   latched-absence-repeats   reportLatchedInferredAbsence, called INSIDE the Pending
#                             guard -- the one that increments on every latch after.
# The latched one is the dispatch-SUPPRESSION counter. Watch it to see the latch
# HOLDING -- never as a dispatch count. This comment was already correct in v6 while
# the code three functions below did `puts = lat` and called a climbing latch a
# re-dispatch loop. The doc was right and the implementation contradicted it, which
# is the opposite of a stale comment and reads as MORE trustworthy, because the
# prose agrees with the source. Trust neither: assert the mapping in a self-test
# (case 9) where a flat inferred with a climbing latched MUST read premise-holds.
annot() {
  local n; n=$($K -n $NS get "$PRVGVR" "$2" \
    -o jsonpath="{.metadata.annotations.krateo\.io/$1}" 2>/dev/null || true)
  printf '%s' "${n:-}"
}
inferred_repeats() { annot inferred-absence-repeats "$1"; }
latched_repeats()  { annot latched-absence-repeats "$1"; }
generation() { local g; g=$($K -n $NS get "$PRVGVR" "$1" -o jsonpath='{.metadata.generation}' 2>/dev/null || true); printf '%s' "${g:-}"; }
absence_events() {
  local n; n=$($K -n $NS get events --field-selector "involvedObject.name=$1" -o json 2>/dev/null \
    | python3 -I -c "
import sys, json
try: d = json.load(sys.stdin)
except Exception: print(0); raise SystemExit
n = 0
for e in d.get('items', []):
    blob = (e.get('reason','') + ' ' + e.get('message','')).lower()
    if 'absen' in blob or 'not found' in blob or 'creating' in blob: n += e.get('count', 1) or 1
print(n)" 2>/dev/null || true); printf '%s' "${n:-0}"
}

echo "elapsed,ordering,ready,ready_status,synced,observed,provision_state,target_provision_state,provision_updated_at,last_error,inferred_repeats,latched_repeats,generation,absence_events,gate_target_valid,gate_basis,counter_readable,ver" > "$CSV"

run_ordering() {
  local ORD="$1" NODE="$2" CRNAME="$3" CR_FIRST="$4"
  say "ORDERING $ORD -- $( [ "$CR_FIRST" = yes ] && echo 'CR FIRST, node after' || echo 'node FIRST, CR after' )"
  mkcr() { cat <<EOF | $K apply -f - >/dev/null || fail "CR apply failed"
apiVersion: baremetal.ogen.krateo.io/v1-0-0
kind: NodeProvision
metadata: {name: $CRNAME, namespace: openstack}
spec:
  configurationRef: {name: ironic-endpoint, namespace: openstack}
  node_id: $NODE
  target: $TARGET
EOF
  }
  if [ "$CR_FIRST" = yes ]; then
    mkcr; echo "  CR created while node '$NODE' does not exist (findby cannot match)"
    sleep 60
    echo "  after 60s with no node: Ready=$($K -n $NS get "$PRVGVR" $CRNAME -o jsonpath='{.status.conditions[?(@.type=="Ready")].reason}' 2>/dev/null)  inferred=$(inferred_repeats "$CRNAME")"
  fi
  cat <<EOF | $K apply -f - >/dev/null || fail "node apply failed"
apiVersion: baremetal.ogen.krateo.io/v1-0-0
kind: BaremetalNode
metadata: {name: $NODE, namespace: openstack}
spec:
  configurationRef: {name: ironic-endpoint, namespace: openstack}
  name: $NODE
  driver: fake-hardware
  resource_class: baremetal
EOF
  for i in $(seq 1 30); do
    [ "$($K -n $NS get "$NODEGVR" $NODE -o jsonpath='{.status.conditions[?(@.type=="Ready")].reason}' 2>/dev/null)" = "Available" ] \
      && { echo "  node Available after ~$((i*20))s"; break; }
    sleep 20
  done

  local ps; ps=$(nodefield "$NODE" provision_state)
  echo "  node provision_state observed: $ps"
  # THE GATE, evaluated against the state the target must be valid FROM, with its
  # BASIS recorded. v3 voided ordering B by checking the OBSERVED state after the CR
  # had already driven the transition -- `manage` is not valid from `manageable`, so
  # a successful dispatch read as an invalid setup.
  #
  # In the CR-first ordering the CR can act the moment the node appears, so the
  # observed state is not the one the target was requested from. A BaremetalNode is
  # created at `enroll` by construction, which is the correct basis there -- recorded
  # as `creation-state`, not `measured`, because it is a premise about how nodes are
  # created rather than a reading taken at the time.
  local GT=no GB=measured BASE="$ps"
  if [ "$CR_FIRST" = yes ]; then GB=creation-state; BASE=enroll; fi
  case "$TARGET:$BASE" in
    manage:enroll|provide:manageable|active:available) GT=yes ;;
    *) GT=no ;;
  esac
  echo "  gate: target=$TARGET valid from '$BASE' -> $GT  (basis: $GB)"
  [ "$GT" = yes ] || echo "  WARNING: this ordering will read VOID"

  [ "$CR_FIRST" != yes ] && { mkcr; echo "  CR created with the node present (findby CAN match)"; }

  local T0; T0=$(date +%s)
  for i in $(seq 1 130); do
    local now el; now=$(date +%s); el=$((now - T0))
    # PAST FIVE LATCHES, not four resyncs. maxLatchedAbsenceRepeats = 5
    # (latched_absence.go:49), resync 180s, so the Warning cannot fire before ~900s
    # of Pending. v3 sampled 831s and read the detector's silence as possibly a
    # defect; it was a window short by ONE latch.
    [ "$el" -gt 1150 ] && break
    local r rst sy ob pst tgt pua le ir lr gn ae ck
    r=$($K -n $NS get "$PRVGVR" $CRNAME -o jsonpath='{.status.conditions[?(@.type=="Ready")].reason}' 2>/dev/null)
    # THE STATUS AS WELL AS THE REASON. oasgen's point: if the TerminalByDesign
    # trigger is ever wrong in the false-healthy direction, the symptom is
    # Ready=False/TerminalByDesign on a kind whose create never landed, and a run
    # recording only "not ready" cannot tell that from the correct resting state.
    # The reason alone was already sampled; the pair is what makes it verifiable.
    rst=$($K -n $NS get "$PRVGVR" $CRNAME -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)
    sy=$($K -n $NS get "$PRVGVR" $CRNAME -o jsonpath='{.status.conditions[?(@.type=="Synced")].reason}' 2>/dev/null)
    ob=$($K -n $NS get "$PRVGVR" $CRNAME -o jsonpath='{.status.conditions[?(@.type=="Observed")].reason}' 2>/dev/null)
    pst=$(nodefield "$NODE" provision_state); tgt=$(nodefield "$NODE" target_provision_state)
    pua=$(nodefield "$NODE" provision_updated_at); le=$(nodefield "$NODE" last_error)
    ir=$(inferred_repeats "$CRNAME"); lr=$(latched_repeats "$CRNAME")
    gn=$(generation "$CRNAME"); ae=$(absence_events "$CRNAME")
    ck=no; { [ -n "$ir" ] || [ -n "$lr" ]; } && ck=yes
    # one line, no embedded newlines: tr guards against any counter regressing
    printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n' \
      "$el" "$ORD" "${r:-ABSENT}" "${rst:-ABSENT}" "${sy:-ABSENT}" "${ob:-ABSENT}" "$pst" "$tgt" "$pua" \
      "$(printf '%s' "${le:-None}" | tr -d ',\n')" "$(printf '%s' "$ir" | tr -cd '0-9')" \
      "$(printf '%s' "$lr" | tr -cd '0-9')" "$(printf '%s' "$gn" | tr -cd '0-9')" \
      "$(printf '%s' "$ae" | tr -cd '0-9')" "$GT" "$GB" "$ck" "$VER" >> "$CSV"
    [ $((i % 6)) -eq 0 ] && echo "  t+${el}s Ready=${r:-ABSENT} Synced=${sy:-ABSENT} state=$pst inferred=${ir:-ABSENT} latched=${lr:-ABSENT} gen=${gn:-?} absence=$ae"
    sleep 10
  done
  echo "  ordering $ORD done -- state $(nodefield "$NODE" provision_state), inferred=$(inferred_repeats "$CRNAME") latched=$(latched_repeats "$CRNAME")"
  $K -n $NS delete "$PRVGVR" $CRNAME --ignore-not-found >/dev/null 2>&1
}

run_ordering A arm7-node-a arm7-prv-a no
run_ordering B arm7-node-b arm7-prv-b yes

say "CSV INTEGRITY (before any verdict)"
echo "  rows: $(( $(wc -l < "$CSV") - 1 ))  fields expected: $FIELDS"
awk -F, -v n="$FIELDS" 'NR>1 && NF!=n {bad++} END {printf "  misaligned rows: %d\n", bad+0}' "$CSV"

say "ANALYSIS"
analyze "$CSV"; rc=$?

say "TEARDOWN"
kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 && echo "  deleted $CLUSTER"
docker ps -a --format '{{.Names}} {{.State}}' | grep ironic-kog | sed 's/^/  lab after: /'
exit $rc
