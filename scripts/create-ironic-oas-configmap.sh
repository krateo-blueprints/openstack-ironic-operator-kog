#!/bin/bash
# Create/refresh ConfigMaps holding the Ironic OpenAPI specs for oasgen-provider:
#   ironic-node-oas      <- oas/ironic-node.yaml
#   ironic-port-oas      <- oas/ironic-port.yaml
#   ironic-provision-oas <- oas/ironic-provision.yaml
#
# Run from repo root. Requires the target namespace to exist.
# Override KUBECTL to target a specific cluster, e.g.
#   KUBECTL="kubectl --kubeconfig local/kubeconfig.ironic-lab --context kind-ironic-lab"
# Configmap keys avoid '-' (KOG's oasPath regex disallows hyphens in the filename segment).
# pipefail is load-bearing here, not boilerplate. apply_oas_cm below pipes
# `create configmap --dry-run` into `apply -f -`. Under plain `set -e` the
# pipeline's status is the RIGHT side's, so a failing left side -- a missing or
# unreadable oas/ file, a bad --from-file key -- is masked: `apply -f -` reads
# empty stdin, succeeds, and the script prints "ConfigMap ... applied" having
# applied nothing. Measured: `set -e` alone continues past `false | cat` and
# exits 0; with pipefail it exits 1.
#
# That failure mode is the expensive one for this script specifically. These
# ConfigMaps are the OAS documents oasgen generates the CRDs from, so a silent
# no-op leaves the CRDs on their previous schema while the files on disk say
# otherwise -- which on 2026-11-04 is exactly the shape that hid a missing
# microversion for five days.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAMESPACE="${1:-openstack}"
KUBECTL="${KUBECTL:-kubectl}"

apply_oas_cm() {
  local cm_name="$1" key="$2" path="$3"
  # Assert the input exists rather than letting kubectl report it obliquely:
  # the whole point of this script is that the file on disk reaches the cluster.
  [ -r "$path" ] || { echo "FATAL: $path is missing or unreadable" >&2; exit 1; }
  $KUBECTL create configmap "$cm_name" \
    -n "$NAMESPACE" \
    --from-file="${key}=${path}" \
    --dry-run=client -o yaml | $KUBECTL apply -f -
  echo "ConfigMap $cm_name applied in namespace $NAMESPACE (from $path)"
}

apply_oas_cm ironic-node-oas      ironic_node.yaml "$REPO_ROOT/oas/ironic-node.yaml"
apply_oas_cm ironic-port-oas      ironic_port.yaml "$REPO_ROOT/oas/ironic-port.yaml"
apply_oas_cm ironic-provision-oas provision.yaml   "$REPO_ROOT/oas/ironic-provision.yaml"
apply_oas_cm ironic-power-oas     power.yaml       "$REPO_ROOT/oas/ironic-power.yaml"
