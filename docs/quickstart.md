---
type: Runbook
title: Quickstart — Ironic bare-metal operator
description: First-time quickstart — install the KOG provider, kubectl apply a Node, and verify enrollment with the openstack baremetal CLI before driving the full lifecycle via a composition.
resource: oci://ghcr.io/krateo-blueprints/charts/baremetal-host
tags: [ironic, quickstart, runbook, kog]
timestamp: 2026-08-11T00:00:00Z
---

# Quickstart — Ironic (bare metal) operator

Manage OpenStack **Ironic** bare-metal resources as Kubernetes CRs. End to end:
install the operator, `kubectl apply` a `Node`, and the operator enrolls it in Ironic.

> **Horizon note:** Ironic has no panel in a stock Horizon (it needs the separate
> `ironic-ui` dashboard plugin), and enrolling a node requires a running
> `ironic-conductor` on a provisioning network — i.e. **real bare-metal infra**.
> This quickstart therefore verifies with the `openstack baremetal` CLI. For the
> full lifecycle (enroll → active) driven by a Krateo Composition, see
> [`docs/E2E.md`](E2E.md) and [`docs/REAL-IRONIC.md`](REAL-IRONIC.md).

## 1. Prerequisites

Krateo's KOG provider in the cluster:

```bash
helm repo add krateo https://charts.krateo.io && helm repo update
helm upgrade --install oasgen-provider krateo/oasgen-provider -n krateo-system --create-namespace
```

Ironic reachable in-cluster (`ironic-api` on `:6385`). Because Ironic is
Keystone-protected and needs a microversion header, point the operator at an
auth proxy (see `docs/REAL-IRONIC.md`) at a **distinct** Service (the openstack-helm
chart already owns the `ironic` Service name).

## 2. Install the operator

```bash
kubectl create configmap ironic-node-oas -n openstack \
  --from-file=ironic_node.yaml=oas/ironic-node.yaml
kubectl apply -f manifests/restdefinition-node.yaml
kubectl wait restdefinition/ironic-node -n openstack --for=condition=Ready --timeout=300s
kubectl get crd | grep nodes.baremetal.ogen.krateo.io
```

## 3. Enroll a node

```bash
cat <<'EOF' | kubectl apply -f -
apiVersion: baremetal.ogen.krateo.io/v1alpha1
kind: NodeConfiguration
metadata:
  name: ironic-config
  namespace: openstack
spec:
  configuration:
    header:
      create:
        X-Ironic-Endpoint: "http://ironic-kog-proxy.openstack.svc.cluster.local:6385"
        X-OpenStack-Ironic-API-Version: "1.81"
      get:
        X-Ironic-Endpoint: "http://ironic-kog-proxy.openstack.svc.cluster.local:6385"
        X-OpenStack-Ironic-API-Version: "1.81"
      delete:
        X-Ironic-Endpoint: "http://ironic-kog-proxy.openstack.svc.cluster.local:6385"
        X-OpenStack-Ironic-API-Version: "1.81"
      findby:
        X-Ironic-Endpoint: "http://ironic-kog-proxy.openstack.svc.cluster.local:6385"
        X-OpenStack-Ironic-API-Version: "1.81"
---
apiVersion: baremetal.ogen.krateo.io/v1alpha1
kind: Node
metadata:
  name: metal-a
  namespace: openstack
spec:
  configurationRef:
    name: ironic-config
    namespace: openstack
  name: metal-a
  driver: ipmi
  driver_info:
    ipmi_address: "172.24.6.10"
    ipmi_username: admin
    ipmi_password: secret
  resource_class: baremetal
EOF
```

## 4. Verify

```bash
openstack baremetal node list
# +--------------------------------------+---------+---------------+-------------+--------------------+-------------+
# | UUID                                 | Name    | Instance UUID | Power State | Provisioning State | Maintenance |
# +--------------------------------------+---------+---------------+-------------+--------------------+-------------+
# | ...                                  | metal-a | None          | None        | enroll             | False       |
# +--------------------------------------+---------+---------------+-------------+--------------------+-------------+
```

Drive `enroll → manageable → available → active` with `NodeProvision` CRs (or a
`BaremetalLifecycle` Composition). `Port`, `PortGroup`, `Allocation` and
`DeployTemplate` are managed the same way.

## 5. (Optional) Install the kagent SME

The repo ships a [kagent](https://kagent.dev) `Agent` CRD —
`ironic-kog-expert` — that turns an LLM into a subject-matter expert on
this blueprint: the two-layer architecture, the FSM, the stuck-state
recovery levers, and the comparison vs `metal3-io/baremetal-operator`.

### Install kagent (v0.9.6, pulled from GHCR)

```bash
kubectl create ns kagent --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install kagent-crds \
  oci://ghcr.io/kagent-dev/kagent/helm/kagent-crds \
  --version 0.9.6 --namespace kagent --wait --timeout 5m

helm upgrade --install kagent \
  oci://ghcr.io/kagent-dev/kagent/helm/kagent \
  --version 0.9.6 --namespace kagent \
  --set providers.default=anthropic \
  --set providers.anthropic.model=claude-sonnet-4-6 \
  --timeout 10m
```

### Wire up a model provider

The chart auto-creates a `ModelConfig` named `default-model-config`
matching `providers.default` (`anthropic`, `openAI`, `gemini`,
`azureOpenAI`, or `ollama`). You then create the secret it expects —
e.g. for Anthropic:

```bash
kubectl -n kagent create secret generic kagent-anthropic \
  --from-literal=ANTHROPIC_API_KEY='sk-ant-...'
```

For **Gemini on Vertex AI** (what this repo ships in
[`kagent/modelconfig-vertex-gemini.yaml`](../kagent/modelconfig-vertex-gemini.yaml)),
mount a GCP service-account JSON instead:

```bash
kubectl -n kagent create secret generic kagent-vertex \
  --from-file=key.json=$HOME/Downloads/<your-sa-key>.json

$EDITOR kagent/modelconfig-vertex-gemini.yaml  # set projectID + location
kubectl apply -f kagent/modelconfig-vertex-gemini.yaml
```

See [`kagent/README.md`](../kagent/README.md) for the provider matrix
(Anthropic, OpenAI, Gemini, GeminiVertexAI, AnthropicVertexAI) and the
GCP service-account prep walkthrough.

### Apply the agent and talk to it

```bash
kubectl apply -f kagent/agent-ironic-expert.yaml
kubectl -n kagent get agent ironic-kog-expert    # wait for READY=True

kubectl -n kagent port-forward svc/kagent-ui 8080:8080
# → http://localhost:8080 → pick "ironic-kog-expert"
```

Example prompts the agent is wired to answer well:

- "How does the `BaremetalHost` composition drive Ironic state transitions?"
- "`blade07` has been in `wait call-back` for 20 minutes — what now?"
- "Why would I pick this over metal3?"
- "Walk me through the `spec.undeploy` widening introduced in v0.3.4."
