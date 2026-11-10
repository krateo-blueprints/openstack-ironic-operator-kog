---
type: Runbook
title: Live demo — the commands, with their real output
description: A read-only walkthrough of a running lab for presenting the blueprint live — one export for kubeconfig isolation, the generated API, what a chart renders for one blade right now, and the three transition lifetimes. Every command verified against the lab.
resource: oci://ghcr.io/krateo-blueprints/charts/baremetal-host
tags: [ironic, demo, runbook, presentation, baremetal-host]
timestamp: 2026-11-10T00:00:00Z
---

# Live demo

Every command below was run against the `ironic-kog` lab on 2026-11-10 and the output
shown is what it printed. Nothing here is reconstructed from memory.

**Everything is read-only.** No command changes the cluster, so nothing can go wrong on
stage. The cost is that the audience never sees a transition *happen* — see
[What this demo cannot show](#what-this-demo-cannot-show).

## Before you start

Read these four. Three of them are ways to be embarrassed in front of an audience.

1. **Never `-o yaml` a BaremetalHost.** `spec.driver_info.redfish_password` is **inline in
   the CR**. The chart's `configurationRef` points at the Ironic endpoint config, not at
   the BMC credential, so there is no by-reference path today. Every command here is
   field-selected for that reason.
2. **Do not shorten with a shell variable.** `kubectl $FLAGS get …` **silently breaks in
   zsh**, which does not word-split unquoted parameter expansions — the whole string is
   passed as one argument and the command fails, or worse, returns an empty result that
   reads as a fact about the cluster. The `export` in step 0 is the safe way to shorten.
3. **Step 1 shows a red row.** `migprobe` reads `False False` — an unrelated failure, a
   chart tarball missing from the chart-repo ConfigMap. Name it in one sentence rather
   than scrolling past it.
4. **Pin `RESOURCE.VERSION.GROUP` on Krateo CRs.** Two versions of the composition CRD
   stay served and an unpinned read can be answered by the older schema, silently
   dropping fields. That is why the commands below are written the long way.

## 0 — isolation

```bash
cd ~/openstack-ironic-operator
export KUBECONFIG=$PWD/local/kubeconfig.ironic-kog
```

One line. The context is already current inside that file, so no `use-context` is needed,
and `~/.kube/config` is never read or written. Worth saying out loud: every call in the
demo goes to an isolated kubeconfig, so nothing can touch a real cluster by accident.

## 1 — the one input

```bash
kubectl get compositiondefinitions.v1alpha1.core.krateo.io -n krateo-system
```

```
NAME                  READY   SYNCED   AGE
baremetal-discovery   True    True     11d
baremetal-host        True    True     11d
baremetal-lifecycle   True    True     11d
kubernetes-cluster    True    True     11d
migprobe              False   False    3d22h
```

Nine lines of YAML each. That is the entire input to everything that follows.

## 2 — what core-provider made of it

```bash
kubectl get crd baremetalhosts.composition.krateo.io \
  -o jsonpath='{range .spec.versions[*]}{.name} served={.served} storage={.storage}{"\n"}{end}'
```

```
vacuum  served=false storage=true
v0-6-2  served=true  storage=false
v0-6-3  served=true  storage=false
```

The storage version is a placeholder and **two versions stay served** — which is the
reason for the pinning rule above, and also the reason the live cluster keeps working
after a chart bump.

```bash
kubectl get deploy -n krateo-system | grep -- -controller
```

```
baremetaldiscoveries-v0-1-1-controller   1/1     1            1           5d15h
baremetalhosts-v0-6-3-controller         1/1     1            1           2d23h
baremetallifecycles-v0-3-1-controller    1/1     1            1           5d15h
kubernetesclusters-v0-14-0-controller    1/1     1            1           2d23h
migprobes-v0-2-0-controller              1/1     1            1           3d22h
```

One controller per CompositionDefinition, named `<plural>-<version>-controller`. None of
it was written by hand.

## 3 — the compositions

```bash
kubectl get baremetalhosts.v0-6-3.composition.krateo.io -n openstack
kubectl get kubernetesclusters.v0-14-0.composition.krateo.io -n openstack
```

```
NAME                    AGE     VERSION
lab-ha-cp-blade01       9d      v0-6-3
lab-ha-cp-blade02       7d11h   v0-6-3
lab-ha-cp-blade03       7d11h   v0-6-3
lab-ha-worker-blade04   7d10h   v0-6-3
lab-ha-worker-blade05   7d10h   v0-6-3
lab-ha-worker-blade06   5d23h   v0-6-3

NAME     AGE   VERSION
lab-ha   10d   v0-14-0
```

Six bare-metal hosts and the cluster blueprint that renders them. The parent names the
child's apiVersion explicitly:

```bash
kubectl get kubernetesclusters.v0-14-0.composition.krateo.io lab-ha -n openstack \
  -o jsonpath='{.spec.lifecycleApiVersion}{"\n"}'
```

```
composition.krateo.io/v0-6-2
```

Note this pin is `v0-6-2` while the child chart in the repo is now `0.6.3`. Nothing is
broken — both versions are served, which is exactly why it keeps reconciling. Unserve the
old version and every render wedges, which is the parent/child lockstep hazard.

## 4 — the state machine, live

The command to spend time on.

```bash
REL=$(helm list -n openstack -q -f lab-ha-cp-blade01)
helm get manifest "$REL" -n openstack | grep '^kind:' | sort | uniq -c
```

```
   1 kind: BaremetalNode
   1 kind: NodePower
   2 kind: Port
```

**And no transition at all.** The blade is `active`, so all seven transition guards
evaluate false and the chart renders the node, its power and its ports — nothing else.
The state machine is not a script that ran; it is a render that depends on observed state.

## 5 — three kinds, three lifetimes

```bash
kubectl get nodeprovisions.v1-0-0.baremetal.ogen.krateo.io -n openstack
kubectl get nodepowers.v1-0-0.baremetal.ogen.krateo.io -n openstack --no-headers | wc -l
kubectl get ports.v1-0-0.baremetal.ogen.krateo.io -n openstack --no-headers | wc -l
```

```
No resources found in openstack namespace.
       6
      12
```

| Kind | Count | Lifetime |
|---|---|---|
| `NodeProvision` | 0 | rendered only while a transition is needed |
| `NodePower` | 6 | one per blade, **never withdrawn** |
| `Port` | 12 | two per blade, outliving both |

**Zero NodeProvisions is the demo.** Nothing cleaned them up and nothing recorded that the
work finished — the chart simply stops rendering a transition once its guard closes. The
six NodePowers are the opposite: never withdrawn, so a blade power-cycled by hand is
corrected on the next pass.

## 6 — the generated provider's schema

```bash
kubectl get crd nodeprovisions.baremetal.ogen.krateo.io \
  -o jsonpath='{.spec.versions[0].schema.openAPIV3Schema.properties.spec.properties.X-OpenStack-Ironic-API-Version.default}{"\n"}'
```

```
1.109
```

That default came from one `required: true` plus `default` in `oas/ironic-provision.yaml`.
It is also irreversible: the API server materialises a CRD default onto existing CRs, so
all six live `NodePower` CRs carry it. See the note in `oas/ironic-port.yaml` for why that
parameter deliberately does *not* have one.

## What this demo cannot show

A transition appearing and disappearing. Doing that live means enrolling and wiping a
blade, and blade01–06 are the running `lab-ha` cluster. It needs a spare blade and an
explicit go-ahead for a destructive cycle.

Say so rather than improvising. "Here is the state at rest, and here is the one command
whose output would change" is a better answer than a demo that half-works.
