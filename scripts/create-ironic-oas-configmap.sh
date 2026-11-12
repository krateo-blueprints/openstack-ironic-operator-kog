#!/bin/bash
# Create/refresh ConfigMaps holding the Ironic OpenAPI specs for oasgen-provider:
#   ironic-node-oas            <- oas/ironic-node.yaml
#   ironic-port-oas            <- oas/ironic-port.yaml
#   ironic-provision-oas       <- oas/ironic-provision.yaml
#   ironic-power-oas           <- oas/ironic-power.yaml
#   ironic-allocation-oas      <- oas/ironic-allocation.yaml
#   ironic-deploy-template-oas <- oas/ironic-deploy-template.yaml
#   ironic-portgroup-oas       <- oas/ironic-portgroup.yaml
#
# This list was wrong until 2026-11-12: it named three while the script created
# four, with ironic-power-oas undocumented. The same class of defect as the
# `apply-restdef` target that omitted restdefinition-power while NodePower was
# live in the cluster -- a comment that denies what the code does, in the one
# file whose whole purpose is that the files on disk reach the cluster.
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

# The three kinds that shipped for months with nothing installing or exercising
# them. Their RestDefinition headers instructed the reader to build these
# ConfigMaps by hand, and nothing in the repo did it -- so the documented path
# required a step the tooling did not provide.
apply_oas_cm ironic-allocation-oas      ironic_allocation.yaml      "$REPO_ROOT/oas/ironic-allocation.yaml"
apply_oas_cm ironic-deploy-template-oas ironic_deploy_template.yaml "$REPO_ROOT/oas/ironic-deploy-template.yaml"
apply_oas_cm ironic-portgroup-oas       ironic_portgroup.yaml       "$REPO_ROOT/oas/ironic-portgroup.yaml"
