#!/usr/bin/env bash
# verify-publisher-scope.sh -- does the scoped `ironic-publisher` credential have
# exactly the reach the blade-side publishers need, and no more?
#
# This is the verification half of docs/IMPLEMENTATION.md §9.10 / §9.23. It is
# DELIBERATELY NOT the creation half: creating the project, user and role
# assignment is one interactive sequence that must not be re-run (it errors on an
# existing user), while this must be safe to re-run -- and it WILL be re-run,
# because openstack-as-a-service regenerates the Keystone admin password on every
# install, so the credential has to be recreated and re-verified after each one.
#
# WHAT IT WILL NOT DO, and why the runbook's own commands were not used verbatim.
# §9.23's pre-flight asks for `baremetal node set --driver fake-hardware blade01`
# and `baremetal node power off blade01` as probes, and asks for the same two as
# `admin` as the positive control. As the publisher those are expected to be
# refused and are therefore harmless; AS ADMIN THEY SUCCEED -- the control would
# change a live blade's driver and power a live blade off. So:
#
#   * every privileged write here is a NO-OP WRITE: the field is set to the value
#     it already holds. It traverses the whole policy-and-write path and changes
#     nothing. That keeps the positive control (an unscoped credential accepted by
#     the same path at the same moment) without touching a blade.
#   * power is NOT PROBED AT ALL. There is no no-op form of a power command, and
#     manual power operations on blades are out by standing instruction. The power
#     rule is read from the default policy in §9.10, which is a document reading
#     and not a measurement -- the summary says so rather than quietly counting it
#     as verified.
#   * `instance_info image_source` is NOT PROBED: a no-op set is possible but
#     image_source is the one field this repo does not touch by hand (it fakes
#     deploy drift).
#
# VERDICTS ARE VOID / PASS / FAIL, and the VOID gates run FIRST. A 403 from a
# credential proves scoping only if an unscoped credential is accepted right then.
# A 403 because the API is down, the node name is wrong, or the password is wrong
# is not a pass -- it is no reading at all.
#
# usage:
#   scripts/verify-publisher-scope.sh <node-name>     # admin env must already work
#   scripts/verify-publisher-scope.sh --dry-run <node-name>
#   scripts/verify-publisher-scope.sh --self-test
#
# The publisher password is read with `read -rs`: never echoed, never written to
# disk, never placed in argv (it is passed to the one command that needs it
# through its environment).

set -u
set -o pipefail

PUB_USER="${PUB_USER:-ironic-publisher}"
PUB_USER_DOMAIN="${PUB_USER_DOMAIN:-Default}"

# ---------------------------------------------------------------------------
# classify: turn an exit code + combined output into exactly one outcome.
#
# This is the part that decides whether a refusal is a reading, so it is the part
# with a self-test. The failure it exists to prevent: reading "command failed" as
# "permission denied", which turns a wrong node name or an unreachable Keystone
# into a scoping pass.
# ---------------------------------------------------------------------------
classify() {
  local code="$1" out="$2"
  if [ "$code" -eq 0 ]; then echo "OK"; return; fi
  case "$out" in
    *"Forbidden"*|*"403"*|*"not authorized"*|*"policy"*"disallow"*) echo "FORBIDDEN" ;;
    *"Unauthorized"*|*"401"*|*"Invalid user"*|*"authentication"*"fail"*) echo "AUTHFAIL" ;;
    *"could not be found"*|*"404"*|*"No Node found"*) echo "NOTFOUND" ;;
    *"Unable to establish"*|*"Connection refused"*|*"timed out"*|*"Name or service not known"*|*"EndpointNotFound"*|*"Could not find versioned identity endpoints"*) echo "UNREACHABLE" ;;
    *) echo "OTHER" ;;
  esac
}

# status_of: in a dry run nothing was executed, so there is no outcome to report.
# Printing classify()'s "OK" there would show a verdict for a command that never ran.
status_of() { if [ "$DRY" = 1 ]; then echo "[dry-run]"; else classify "$1" "$2"; fi; }

self_test() {
  local fails=0 got
  check() { # expected, code, text
    got="$(classify "$2" "$3")"
    if [ "$got" = "$1" ]; then printf '  %-12s <- %-58s ok\n' "$got" "$(printf '%.58s' "$3")"
    else printf '  %-12s <- %-58s FAIL (wanted %s)\n' "$got" "$(printf '%.58s' "$3")" "$1"; fails=$((fails+1)); fi
  }
  echo "classify() self-test -- a refusal must not be confused with a failure:"
  check OK          0 "ok"
  check FORBIDDEN   1 "Forbidden: baremetal:node:update is disallowed by policy"
  check FORBIDDEN   1 "HTTP 403"
  check AUTHFAIL    1 "The request you have made requires authentication. (HTTP 401)"
  check NOTFOUND    1 "No Node found for blade99 (HTTP 404)"
  check UNREACHABLE 1 "Unable to establish connection to https://keystone:5000"
  check UNREACHABLE 1 "Could not find versioned identity endpoints"
  check OTHER       1 "ValueError: something else entirely"
  # the two that matter most: these must NOT read as FORBIDDEN
  check AUTHFAIL    1 "401 Unauthorized"
  check NOTFOUND    1 "node blade01 could not be found"
  echo "self-test: 10 cases, $fails failure(s)"
  [ "$fails" -eq 0 ] || return 1
  echo "  -> a 401, a 404 and an unreachable endpoint are each distinguishable from a 403,"
  echo "     so a negative check cannot pass by failing for the wrong reason."
}

DRY=0
case "${1:-}" in
  --self-test) self_test; exit $? ;;
  --dry-run)   DRY=1; shift ;;
esac

NODE="${1:-}"
if [ -z "$NODE" ]; then
  echo "usage: $0 [--dry-run] <node-name>   |   $0 --self-test" >&2
  exit 64
fi

# Prechecks apply to a real run only. --dry-run exists to review the exact commands
# BEFORE having a cloud to point at, so requiring the CLI and an auth URL to review
# them defeats the purpose -- which is how this was first written.
if [ "$DRY" = 0 ]; then
  command -v openstack >/dev/null || { echo "PRECHECK FAILED: openstack CLI not on PATH" >&2; exit 1; }
  : "${OS_AUTH_URL:?PRECHECK FAILED: OS_AUTH_URL must be set (the admin env must already work)}"
fi

# as_admin: the caller's existing admin environment, untouched.
as_admin() { if [ "$DRY" = 1 ]; then echo "    [dry-run] openstack $*" >&2; return 0; fi; openstack "$@" 2>&1; }

# as_publisher: explicit flags, and OS_CLOUD plus every project-scope variable
# cleared. A clouds.yaml entry combined with --os-system-scope is the exact
# collision that produced a spurious 403 on port:create once before
# (reference: system_scope:all clouds.yaml duplicate-entry workaround) -- so this
# builds the auth from scratch rather than inheriting half of it.
as_publisher() {
  if [ "$DRY" = 1 ]; then echo "    [dry-run] openstack --os-system-scope all --os-username $PUB_USER $*" >&2; return 0; fi
  env -u OS_CLOUD -u OS_PROJECT_NAME -u OS_PROJECT_ID -u OS_PROJECT_DOMAIN_NAME \
      -u OS_PROJECT_DOMAIN_ID -u OS_TOKEN -u OS_USERNAME -u OS_USER_ID \
      -u OS_USER_DOMAIN_ID -u OS_TENANT_NAME -u OS_TENANT_ID \
      OS_PASSWORD="$PUB_PASS" \
      openstack --os-auth-type password \
                --os-auth-url "$OS_AUTH_URL" \
                --os-identity-api-version 3 \
                --os-system-scope all \
                --os-user-domain-name "$PUB_USER_DOMAIN" \
                --os-username "$PUB_USER" \
                "$@" 2>&1
}

PUB_PASS=""
if [ "$DRY" = 0 ]; then
  printf 'password for %s (not echoed, not stored): ' "$PUB_USER" >&2
  read -rs PUB_PASS; echo >&2
  [ -n "$PUB_PASS" ] || { echo "PRECHECK FAILED: empty password" >&2; exit 1; }
fi

echo
echo "===== verify-publisher-scope: node=$NODE user=$PUB_USER $([ "$DRY" = 1 ] && echo '(DRY RUN)') ====="

# --- read the node's CURRENT driver and one extra key, so every write below can
# --- be a no-op. If we cannot read these as admin, there is nothing to do.
CUR_DRIVER=""; EXTRA_K=""; EXTRA_V=""; EXTRA_COUNT="unknown"
if [ "$DRY" = 0 ]; then
  CUR_DRIVER="$(openstack baremetal node show "$NODE" -f value -c driver 2>&1)" || CUR_DRIVER=""
  extra_json="$(openstack baremetal node show "$NODE" -f json 2>/dev/null)" || extra_json=""
  if [ -n "$extra_json" ]; then
    read -r EXTRA_COUNT EXTRA_K EXTRA_V <<<"$(printf '%s' "$extra_json" | python3 -I -c '
import sys, json
d = json.load(sys.stdin) or {}
e = d.get("extra") or {}
items = [(k, v) for k, v in e.items() if isinstance(v, str) and v and " " not in v and "=" not in v]
if items:
    k, v = items[0]
    print(len(e), k, v)
else:
    print(len(e), "", "")
' 2>/dev/null)" || true
  fi
fi

echo
echo "----- VOID GATES (these run FIRST; if either fails nothing below is a reading) -----"

g0_out="$(as_admin baremetal node show "$NODE" -f value -c uuid)"; g0="$(status_of $? "$g0_out")"
echo "  gate 0  admin can READ the node .......................... $g0"

if [ "$DRY" = 0 ] && [ -z "$CUR_DRIVER" ]; then
  echo "  gate 1  admin NO-OP privileged write .................... SKIPPED (driver unreadable)"
  g1="OTHER"
else
  g1_out="$(as_admin baremetal node set --driver "${CUR_DRIVER:-<current>}" "$NODE")"; g1="$(status_of $? "$g1_out")"
  echo "  gate 1  admin NO-OP privileged write (--driver=current) . $g1"
fi
echo "          (this is §9.23's positive control, made non-destructive: the driver is"
echo "           set to the value it already holds, so the policy and write path is"
echo "           exercised and the blade is unchanged.)"

echo
echo "----- WHAT THE PUBLISHER MUST BE ABLE TO DO -----"
p1_out="$(as_publisher baremetal node show "$NODE" -f value -c extra)"; p1="$(status_of $? "$p1_out")"
echo "  can READ extra (baremetal:node:get) .................... $p1   [want OK]"

if [ "$DRY" = 1 ]; then
  echo "  can WRITE extra (baremetal:node:update_extra) ......... [dry-run]  [want OK]"
  as_publisher baremetal node set --extra "<existing-key>=<existing-value>" "$NODE" >/dev/null
  p2="DRY"
elif [ -n "$EXTRA_K" ]; then
  p2_out="$(as_publisher baremetal node set --extra "${EXTRA_K}=${EXTRA_V}" "$NODE")"; p2="$(classify $? "$p2_out")"
  echo "  can WRITE extra (no-op: ${EXTRA_K} to its own value) ... $p2   [want OK]"
else
  p2="NOT PROBED"
  echo "  can WRITE extra (baremetal:node:update_extra) ......... NOT PROBED"
  echo "          (extra holds $EXTRA_COUNT usable key(s), so there is no value to re-set"
  echo "           idempotently. Inventing a key would write to the field the publishers"
  echo "           themselves own. Re-run against a node that already publishes.)"
fi

echo
echo "----- WHAT IT MUST **NOT** BE ABLE TO DO -----"
if [ "$DRY" = 1 ] || [ -n "$CUR_DRIVER" ]; then
  n1_out="$(as_publisher baremetal node set --driver "${CUR_DRIVER:-<current>}" "$NODE")"; n1="$(status_of $? "$n1_out")"
  echo "  cannot change driver (baremetal:node:update) .......... $n1   [want FORBIDDEN]"
  echo "          (same no-op write as gate 1, so a refusal here is policy and not breakage)"
else
  n1="OTHER"; echo "  cannot change driver .................................. SKIPPED"
fi
echo "  cannot power the node (set_power_state) ............... NOT PROBED"
echo "          (no no-op form exists, and manual power operations on blades are out by"
echo "           standing instruction. §9.10 reads this rule from the default policy --"
echo "           a document reading, not a measurement.)"
echo "  cannot set instance_info image_source ................. NOT PROBED"
echo "          (a no-op set is possible, but image_source is the one field this repo"
echo "           never touches by hand.)"

echo
echo "===== VERDICT ====="
if [ "$DRY" = 1 ]; then
  echo "DRY RUN -- no command was executed. Review the lines above, then re-run without --dry-run."
  exit 0
fi
if [ "$g0" != "OK" ] || [ "$g1" != "OK" ]; then
  echo "VOID -- the unscoped control did not succeed (gate 0=$g0, gate 1=$g1)."
  echo "  Every refusal below it is therefore indistinguishable from the API refusing"
  echo "  everyone. Fix the admin path first; nothing here says anything about scoping yet."
  exit 4
fi
if [ "$p1" = "AUTHFAIL" ]; then
  echo "VOID -- the publisher credential did not authenticate (401), so no probe below it ran"
  echo "  as that user. Check the password, the user domain, and that the role assignment is"
  echo "  'openstack role add --user $PUB_USER --system all member'."
  exit 4
fi
ok=1
[ "$p1" = "OK" ] || { echo "FAIL -- the publisher cannot read extra ($p1); the publishers would 403 on every node."; ok=0; }
case "$p2" in OK|"NOT PROBED") ;; *) echo "FAIL -- the publisher cannot write extra ($p2); this is the one thing it exists to do."; ok=0 ;; esac
[ "$n1" = "FORBIDDEN" ] || { echo "FAIL -- the publisher CAN change a node's driver ($n1). It is not scoped; do not ship it."; ok=0; }
if [ "$ok" = 1 ]; then
  echo "PASS -- reads extra, writes extra, refused on driver, with the unscoped control"
  echo "  accepted at the same moment. Power and image_source remain NOT PROBED above."
  if [ "$p2" = "NOT PROBED" ]; then
    echo "  NOTE: the write probe did not run. The credential is proven readable and"
    echo "  correctly refused, but NOT yet proven able to write extra -- re-run against a"
    echo "  node that already has an extra key before setting ironicAuth on the CR."
  fi
  exit 0
fi
exit 1
