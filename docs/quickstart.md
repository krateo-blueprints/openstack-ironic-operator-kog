---
type: Runbook
title: Quickstart — Ironic bare-metal operator
description: First-time quickstart — install the KOG provider, kubectl apply a Node, and verify enrollment with the openstack baremetal CLI before driving the full lifecycle via a composition.
resource: oci://ghcr.io/krateo-blueprints/charts/baremetal-host
tags: [ironic, quickstart, runbook, kog]
timestamp: 2026-08-11T00:00:00Z
---

# Quickstart — Ironic (bare metal) operator

Manage OpenStack **Ironic** bare-metal resources as Kubernetes CRs. This walks a
node to `enroll` against a throwaway local Ironic, with no hardware and nothing
to pay for.

> **Rewritten 2026-11-04.** The previous version of §1–§4 could not work: it
> applied `kind: Node` and `kind: NodeConfiguration`, neither of which the
> current `RestDefinition`s generate, at `apiVersion: .../v1alpha1`, which the
> node CRD does not serve. It also set the Ironic microversion through
> `spec.configuration.header`, which RDC does not send, at `1.81`, which is
> below the `1.83` these definitions need. A reader was rejected on the first
> `kubectl apply`.
>
> It rotted because it hand-copied commands the `Makefile` already owns. This
> version calls the targets instead, so it cannot drift from them, and the YAML
> below is screened by `make check-examples` like every other manifest a reader
> is told to apply.

## 1. Bring up a local environment

No hardware, no OpenStack account, no credentials. A kind cluster and a
standalone Ironic with fake drivers:

```bash
make kind-up        # isolated kubeconfig at local/kubeconfig.ironic-kog
make ironic-up      # standalone Ironic, noauth, fake hardware
make krateo-up      # oasgen-provider (KOG) + core-provider (compositions)
make restdef-up     # OAS ConfigMaps + the four RestDefinitions + endpoint singletons
```

`make restdef-up` is the step that creates the generated CRDs. It also creates
the `BaremetalNodeConfiguration` the CRs below reference, so there is nothing to
hand-write.

For real, Keystone-protected Ironic instead, see
[`REAL-IRONIC.md`](REAL-IRONIC.md); for real PXE against Bifrost, see
[`BIFROST.md`](BIFROST.md).

## 2. Confirm the operator is up

```bash
export KUBECONFIG=local/kubeconfig.ironic-kog

kubectl get restdefinitions -n openstack
kubectl api-resources --api-group=baremetal.ogen.krateo.io
```

Four `RestDefinition`s should report `READY=True`, and the group should list
`baremetalnodes`, `ports`, `nodepowers` and `nodeprovisions` at
**`baremetal.ogen.krateo.io/v1-0-0`**, alongside their `*configurations` at
`v1alpha1`. The resources and their configurations are on **different API
versions**; that is expected, and copying one onto the other is the mistake the
old quickstart made.

## 3. Enroll a node

```bash
kubectl apply -f manifests/baremetalnode-quickstart.yaml
```

That manifest is the minimum a node needs, and is screened by
`make check-examples`:

- `kind: BaremetalNode` at `baremetal.ogen.krateo.io/v1-0-0` — there is no
  `Node` kind.
- `configurationRef` pointing at the `ironic-endpoint`
  `BaremetalNodeConfiguration` that `make restdef-up` created. It is
  **required**; a CR without it is rejected at admission.
- `X-OpenStack-Ironic-API-Version` is **not set by hand**. It is declared in the
  OAS with a default, and the API server stamps `1.109` onto every CR. An unset
  microversion makes Ironic negotiate its minimum (1.1), which silently breaks
  anything needing a modern field.

## 4. Verify

```bash
kubectl get baremetalnodes.v1-0-0.baremetal.ogen.krateo.io -n openstack \
  -o custom-columns='NAME:.metadata.name,STATE:.status.provision_state'
```

```
NAME              STATE
quickstart-node   enroll
```

Pin the full `resource.version.group` as above. A bare `kubectl get
baremetalnodes` negotiates a version for you, and when several are served that
is not always the one you wrote.

Then drive `enroll → manageable → available → active` with `NodeProvision` CRs,
or let a composition do it: `make composition-demo` creates a
`BaremetalLifecycle` and the composition-dynamic-controller walks the whole
state machine. `make smoke-test` does the same end to end and cleans up after
itself with `CLEANUP=1`.

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
