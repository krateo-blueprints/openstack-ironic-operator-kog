#!/usr/bin/env bash
# The ~1500 lines of Go that NOTHING in this repo compiles.
#
# WHY THIS EXISTS. tests/oasgen-generator/ and tests/upstream-arms/ hold Go files that
# nothing here builds, vets or formats. That is not an oversight: they are
# `package builder` / `package oas2jsonschema` and import another module's INTERNAL
# packages, so they are unbuildable in this repo BY CONSTRUCTION and run only when
# copied into the oasgen tree. There is no go.mod here either, which is also why
# `security / go` and `security / codeql` correctly SKIP on every PR -- measured on
# #125, #126 and #127, all three skipping the same three checks.
#
# THE CONSEQUENCE, measured 2026-11-21: a syntax error or formatting drift in any of
# them lands on main with all 12 checks green, and surfaces only when someone next
# copies the file upstream, possibly months later. `gofmt` needs no imports and no
# module, so it is the one piece of the Go toolchain that CAN run here.
#
# PROVEN, NOT ASSUMED. A deliberately broken file exits non-zero and names the line
# ("expected ')', found '{'"); a valid file exits 0 silently. And the gap was not
# hypothetical: tests/upstream-arms/ironic_real_definitions_test.go was ALREADY
# flagged on main when this screen was written -- formatting only, landed 2026-11-05.
#
# WHAT IT DOES NOT CATCH, said plainly so a pass is not over-read: gofmt sees syntax
# and layout, never types, imports or semantics. The stale claim found in that same
# file -- a comment that contradicted the manifests for nine days -- is invisible to
# it. That class needs a fixture-vs-manifest comparison, which this is not.
#
# CALIBRATION: `--self-test` builds its own bad input in a temp directory and asserts
# this screen FAILS on it, then asserts it PASSES on a good one. That is why the root
# is an argument rather than hardcoded -- a screen that cannot be pointed at a known
# bad tree cannot be shown to fire, and this repo has already shipped two screens that
# quietly narrowed their own scope and kept passing.
set -euo pipefail
cd "$(dirname "$0")/.."

# both spellings: a typo used to fall through and become ROOT, so the screen looked
# for Go files under a directory named "--selftest", found none, and VOIDed -- which
# fails, but names the wrong cause.
if [ "${1:-}" = "--self-test" ] || [ "${1:-}" = "--selftest" ]; then
  tmp=$(mktemp -d)
  mkdir -p "$tmp/good" "$tmp/bad"
  printf 'package builder\n\nfunc fine() {}\n'   > "$tmp/good/ok_test.go"
  printf 'package builder\n\nfunc broken( {\n'   > "$tmp/bad/broken_test.go"
  mkdir -p "$tmp/empty"
  fails=0
  check() {  # name expected-exit root
    set +e; out=$(bash "$0" "$3" 2>&1); rc=$?; set -e
    if [ "$rc" = "$2" ]; then echo "  ok    $1 (exit $rc)"
    else echo "  FAIL  $1: expected exit $2, got $rc"; echo "$out" | sed 's/^/        /'; fails=1; fi
  }
  echo "SELF-TEST (fixtures under $tmp)"
  check "POSITIVE CONTROL: a file that does not parse -> FAIL"   1 "$tmp/bad"
  check "NEGATIVE CONTROL: a clean file -> PASS"                 0 "$tmp/good"
  check "VOID CONTROL: a tree with no Go files is NOT a pass"    2 "$tmp/empty"
  check "the real tree -> PASS"                                  0 "tests"
  [ "$fails" = 0 ] && echo "SELF-TEST: 4/4 assertions passed" || { echo "SELF-TEST FAILED"; exit 2; }
  exit 0
fi

case "${1:-}" in
  --*) echo "usage: $0 [--self-test|--selftest] [<root>]" >&2
       echo "refusing unrecognised option: $1" >&2
       exit 64 ;;
esac
ROOT="${1:-tests}"

command -v gofmt >/dev/null || {
  echo "VOID: gofmt is not on PATH, so this screen measured NOTHING."
  echo "VOID: a pass here would be indistinguishable from an absent toolchain."
  exit 2
}

mapfile -t files < <(find "$ROOT" -name '*.go' -type f | sort)
if [ "${#files[@]}" -eq 0 ]; then
  echo "VOID: no Go files found under ${ROOT}/. Either they moved or the search is wrong;"
  echo "VOID: a clean run over zero files is not a pass. Refusing to report one."
  exit 2
fi
echo "  $(printf '%s\n' "${files[@]}" | wc -l | tr -d ' ') Go file(s) that nothing in this repo compiles:"

bad=$(gofmt -l "${files[@]}" || true)
if [ -n "$bad" ]; then
  echo "FAIL -- gofmt objects to:"
  printf '%s\n' "$bad" | sed 's/^/    /'
  echo "  fix with: gofmt -w <file>"
  exit 1
fi

for f in "${files[@]}"; do
  if ! gofmt -e "$f" >/dev/null; then
    echo "FAIL -- $f does not parse (see the error above)"
    exit 1
  fi
done

echo "PASS -- all parse and are gofmt-clean."
