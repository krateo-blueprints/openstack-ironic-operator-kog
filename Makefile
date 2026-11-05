# openstack-ironic-operator - Ironic + Krateo integration
.PHONY: help apply-oas apply-restdef deploy-chart package-chart template-chart deploy-ironic validate-chart \
        kind-up kind-down local-up local-down ironic-up ironic-image ironic-down ironic-forward smoke-test kubeconfig \
        ironic-backend-direct ironic-backend-local

HELM_NAMESPACE ?= default
IRONIC_NS ?= openstack

# --- Local test environment (isolated kind cluster; never touches ~/.kube/config) ---
KIND_CLUSTER ?= ironic-kog
KUBECONFIG_FILE ?= local/kubeconfig.$(KIND_CLUSTER)
KCTX := kind-$(KIND_CLUSTER)
KUBECTL := kubectl --kubeconfig $(KUBECONFIG_FILE) --context $(KCTX)
HELMK := helm --kubeconfig $(KUBECONFIG_FILE) --kube-context $(KCTX)

help:
	@echo "Targets:"
	@echo "  apply-oas      - Create ConfigMap with OAS in cluster"
	@echo "  apply-restdef  - Apply RestDefinition (requires apply-oas first)"
	@echo "  deploy-chart   - Helm install baremetal-lifecycle chart"
	@echo "  package-chart  - Package chart to .tgz for publishing"
	@echo "  template-chart - Dry-run helm template (requires nodeName in values)"
	@echo "  validate-chart - Verify Node CR and Job templates render"
	@echo "  deploy-ironic  - Helm install Ironic with overrides (req: openstack-helm repo, backends)"
	@echo "Local test env (isolated kind cluster, never touches ~/.kube/config):"
	@echo "  local-up       - Full local stack: kind + Ironic + Krateo + RestDefinition"
	@echo "  krateo-up      - Install Krateo KOG (oasgen) + composition (core) providers"
	@echo "  restdef-up     - Apply OAS ConfigMaps + RestDefinitions (Node + Port + NodeProvision) + singleton Node/PortConfiguration"
	@echo "  ironic-up      - (Re)deploy standalone Ironic into the kind cluster"
	@echo "  provision-demo - Provision a sample fake node -> active (simulated reconciles via helm)"
	@echo "  composition-up - Host the chart + install the CompositionDefinition (real cdc path)"
	@echo "  composition-demo - Create a BaremetalLifecycle instance; cdc walks it to active"
	@echo "  bifrost-up     - Point the operator at a remote standalone Ironic (BIFROST_URL=http://host:6385)"
	@echo "  ironic-backend-direct - Point Service/ironic at IRONIC_BACKEND_MANIFEST's endpoint"
	@echo "  ironic-backend-local  - Point Service/ironic at the in-cluster fake Ironic"
	@echo "Dedicated lab cluster (WireGuard tunnel runs in-cluster, host networking untouched):"
	@echo "  lab-up         - Spin up a separate kind cluster with WG + Keystone proxy + operator"
	@echo "  lab-down       - Delete the lab kind cluster"
	@echo "  ironic-forward - Port-forward Ironic API to localhost:6385"
	@echo "  smoke-test     - Drive a fake node enroll->active against local Ironic"
	@echo "  local-down     - Delete the local kind cluster"

apply-oas:
	KUBECTL="$(KUBECTL)" ./scripts/create-ironic-oas-configmap.sh $(IRONIC_NS)

apply-restdef: apply-oas
	# All four definitions that are actually installed. restdefinition-power was
	# missing here while being live in the cluster, so `apply-restdef` silently
	# left the NodePower definition at whatever an earlier apply had put there.
	# allocation / deploy-template / portgroup exist in manifests/ but are not
	# installed; add them here when they are.
	$(KUBECTL) apply -f manifests/restdefinition-node.yaml
	$(KUBECTL) apply -f manifests/restdefinition-port.yaml
	$(KUBECTL) apply -f manifests/restdefinition-provision.yaml
	$(KUBECTL) apply -f manifests/restdefinition-power.yaml

deploy-chart:
	$(HELMK) upgrade --install baremetal-lifecycle ./charts/baremetal-lifecycle \
		-n $(HELM_NAMESPACE) --create-namespace

package-chart:
	@mkdir -p dist
	helm package charts/baremetal-lifecycle -d dist/

template-chart:
	helm template baremetal-lifecycle ./charts/baremetal-lifecycle \
		-n $(HELM_NAMESPACE) \
		--set nodeName=test-node \
		--set driver_info.ipmi_address=172.19.74.202 \
		--set driver_info.ipmi_password=secret

validate-chart:
	@helm template baremetal-lifecycle ./charts/baremetal-lifecycle \
		-n $(HELM_NAMESPACE) \
		--set nodeName=test-node \
		--set driver_info.ipmi_address=172.19.74.202 \
		--set driver_info.ipmi_password=secret \
		--set ports[0].address=9c:b6:54:b2:b0:ca > /dev/null && echo "Chart templates render OK"
	@helm template baremetal-lifecycle ./charts/baremetal-lifecycle -n $(HELM_NAMESPACE) --set nodeName=test-node | grep -q "kind: Node" && echo "Node CR present: OK"

# Deploy Ironic via openstack-helm (requires: helm repo add openstack-helm, rabbitmq/mariadb/memcached/keystone/glance/neutron)
deploy-ironic:
	$(HELMK) upgrade --install ironic openstack-helm/ironic \
		--namespace=$(IRONIC_NS) \
		-f deploy/values/ironic-overrides.yaml

# --- Local test environment ----------------------------------------------------
# An isolated kind cluster + standalone Ironic (official openstack-helm image, fake
# drivers, noauth). All kubectl/helm calls use --kubeconfig $(KUBECONFIG_FILE) and
# --context $(KCTX), so the default ~/.kube/config is never touched.

kubeconfig:   # (re)write the isolated kubeconfig for the kind cluster
	kind export kubeconfig --name $(KIND_CLUSTER) --kubeconfig $(KUBECONFIG_FILE)

KIND_CONFIG   ?= local/kind-config.yaml
# kind's own readiness bound, separate from the kubeadm timeouts in KIND_CONFIG and not
# reachable from them. On an emulated amd64 node (KIND_PLATFORM, forced because the
# airshipit images publish no linux/arm64) the control plane needs well over the 60s
# default, and the failure reads as a cluster that would not come up rather than as a
# deadline that was too short.
KIND_WAIT     ?= 300s

kind-up:      # create the local kind cluster (idempotent; isolated kubeconfig)
	@kind get clusters 2>/dev/null | grep -qx $(KIND_CLUSTER) || \
		kind create cluster --name $(KIND_CLUSTER) --kubeconfig $(KUBECONFIG_FILE) \
			--config $(KIND_CONFIG) --wait $(KIND_WAIT)
	@kind export kubeconfig --name $(KIND_CLUSTER) --kubeconfig $(KUBECONFIG_FILE) >/dev/null
	@echo "kubeconfig=$(KUBECONFIG_FILE) context=$(KCTX)"

IRONIC_IMAGE ?= quay.io/airshipit/ironic:2026.2-ubuntu_resolute

ironic-image: # pull the Ironic image for the node's platform and side-load it
	# The airshipit Ironic images are published for linux/amd64 ONLY. On an
	# arm64 host, containerd inside the kind node refuses the pull with
	# "no match for platform in manifest: not found", so `ironic-up` could
	# never work on a fresh Apple Silicon cluster -- measured 2026-11-04.
	#
	# The long-lived lab cluster worked only because the image had been
	# side-loaded by hand at some point, and NOTHING in this repo recorded
	# that step. So the documented path was broken while a working cluster
	# sat beside it as evidence that it was fine.
	#
	# --platform is explicit rather than inherited: on an arm64 host the
	# daemon would otherwise pick the host platform and fail the same way.
	docker pull --platform linux/amd64 $(IRONIC_IMAGE)
	kind load docker-image $(IRONIC_IMAGE) --name $(KIND_CLUSTER)
	# Assert it actually landed: `kind load` exits 0 on some no-ops, and a
	# missing image here reappears later as an ImagePullBackOff that looks
	# like a registry problem.
	# Match repo AND tag. Grepping the repo alone would pass on any stale
	# ironic tag already in the node -- an assertion that is true for free,
	# which is the failure mode this assertion exists to prevent.
	@docker exec $(KIND_CLUSTER)-control-plane crictl images \
		| grep -q "$(word 1,$(subst :, ,$(IRONIC_IMAGE))).*$(word 2,$(subst :, ,$(IRONIC_IMAGE)))" \
		|| { echo "FATAL: $(IRONIC_IMAGE) not present in $(KIND_CLUSTER) after kind load" >&2; exit 1; }

ironic-up: ironic-image  # deploy standalone Ironic (fake drivers, noauth) into the kind cluster
	$(KUBECTL) apply -f local/ironic-standalone.yaml
	$(KUBECTL) -n $(IRONIC_NS) create configmap ironic-config \
		--from-file=ironic.conf=local/ironic.conf --dry-run=client -o yaml | $(KUBECTL) apply -f -
	$(KUBECTL) -n $(IRONIC_NS) rollout restart deploy/ironic
	$(KUBECTL) -n $(IRONIC_NS) rollout status deploy/ironic --timeout=240s

ironic-down:  # remove standalone Ironic from the kind cluster
	-$(KUBECTL) delete -f local/ironic-standalone.yaml

ironic-forward: # port-forward the Ironic API to localhost:6385 (run in a separate shell)
	$(KUBECTL) -n $(IRONIC_NS) port-forward svc/ironic 6385:6385

smoke-test:   # drive a fake node enroll->active against the local Ironic (CLEANUP=1 to delete after)
	@$(KUBECTL) -n $(IRONIC_NS) port-forward svc/ironic 6385:6385 >/tmp/ironic-pf.log 2>&1 & \
	pf=$$!; sleep 4; \
	IRONIC_API=http://localhost:6385 CLEANUP=$${CLEANUP:-1} ./scripts/smoke-test-ironic.sh; rc=$$?; \
	kill $$pf 2>/dev/null; exit $$rc

KRATEO_CORE_VERSION ?= 1.0.0
# The https://charts.krateo.io index stops at oasgen 0.11.1; the real releases
# live in the OCI registry, which is where the 0.47.0 install actually came
# from. Pinning the stale index version here meant `make krateo-up` would have
# silently reinstalled a years-old provider over a working one.
KRATEO_OASGEN_VERSION ?= 0.50.0
KRATEO_RDC_VERSION    ?= 0.50.0
KRATEO_OCI            ?= oci://ghcr.io/krateo-platformops/charts

krateo-up:    # install Krateo KOG (oasgen-provider) + composition engine (core-provider)
	helm repo add krateo https://charts.krateo.io 2>/dev/null || true
	helm repo update krateo
	$(HELMK) upgrade --install krateo-core-provider krateo/core-provider \
		-n krateo-system --create-namespace --version $(KRATEO_CORE_VERSION) --wait --timeout 6m
	$(HELMK) upgrade --install krateo-oasgen-provider-crds $(KRATEO_OCI)/oasgen-provider-crds \
		-n krateo-system --create-namespace --version $(KRATEO_OASGEN_VERSION) --wait --timeout 5m
	$(HELMK) upgrade --install krateo-oasgen-provider $(KRATEO_OCI)/oasgen-provider \
		-n krateo-system --version $(KRATEO_OASGEN_VERSION) \
		--set rdc.image.registry=ghcr.io/krateo-platformops \
		--set rdc.image.repository=rest-dynamic-controller \
		--set rdc.image.tag=$(KRATEO_RDC_VERSION) \
		--set rdc.image.pullPolicy=IfNotPresent \
		--wait --timeout 8m
	$(KUBECTL) -n krateo-system get pods

restdef-up:   # apply OAS ConfigMaps + RestDefinitions (Node + Port + NodeProvision + NodePower) + endpoint singletons
	KUBECTL="$(KUBECTL)" ./scripts/create-ironic-oas-configmap.sh $(IRONIC_NS)
	$(KUBECTL) apply -f manifests/restdefinition-node.yaml
	$(KUBECTL) apply -f manifests/restdefinition-port.yaml
	$(KUBECTL) apply -f manifests/restdefinition-provision.yaml
	$(KUBECTL) apply -f manifests/restdefinition-power.yaml
	$(KUBECTL) -n $(IRONIC_NS) wait --for=condition=Ready restdefinition/ironic-node --timeout=180s
	$(KUBECTL) -n $(IRONIC_NS) wait --for=condition=Ready restdefinition/ironic-port --timeout=180s
	$(KUBECTL) -n $(IRONIC_NS) wait --for=condition=Ready restdefinition/ironic-node-provision --timeout=180s
	$(KUBECTL) -n $(IRONIC_NS) wait --for=condition=Ready restdefinition/ironic-node-power --timeout=180s
	# Singleton NodeConfiguration + PortConfiguration. Applied AFTER the RestDefinitions are
	# Ready so the *Configuration CRDs exist. Kept OUT of charts/baremetal-lifecycle so RDC
	# can still resolve spec.configurationRef during the per-node delete drain (see
	# manifests/nodeconfiguration-ironic.yaml header for the orphan-on-uninstall race).
	$(KUBECTL) apply -f manifests/nodeconfiguration-ironic.yaml
	$(KUBECTL) apply -f manifests/portconfiguration-ironic.yaml

# Provision a sample fake node. The state machine is lookup-driven: each reconcile renders the
# NodeProvision CR for the node's current state. composition-dynamic-controller does this on its
# reconcile loop; here we simulate reconciles with repeated `helm upgrade` until the node is active.
PROVISION_NODE ?= server01
provision-demo:
	@for i in $$(seq 1 40); do \
	  $(HELMK) upgrade --install baremetal-lifecycle ./charts/baremetal-lifecycle -n $(IRONIC_NS) \
	    --set nodeName=$(PROVISION_NODE) --set driver=fake-hardware \
	    --set instance_info.image_source=http://example.invalid/image.qcow2 \
	    --set instance_info.image_checksum=0 >/dev/null; \
	  st=$$($(KUBECTL) -n $(IRONIC_NS) get node.baremetal.ogen.krateo.io $(PROVISION_NODE) -o jsonpath='{.status.provision_state}' 2>/dev/null); \
	  echo "reconcile $$i: $(PROVISION_NODE) provision_state=$$st"; \
	  [ "$$st" = "active" ] && { echo "server provisioned (active)"; break; }; \
	  sleep 12; \
	done

LIFECYCLE_CHART_VERSION ?= $(shell awk '/^version:/ {print $$2; exit}' charts/baremetal-lifecycle/Chart.yaml)
DISCOVERY_CHART_VERSION ?= $(shell awk '/^version:/ {print $$2; exit}' charts/baremetal-discovery/Chart.yaml)
HOST_CHART_VERSION      ?= $(shell awk '/^version:/ {print $$2; exit}' charts/baremetal-host/Chart.yaml)
CLUSTER_CHART_VERSION   ?= $(shell awk '/^version:/ {print $$2; exit}' charts/kubernetes-cluster/Chart.yaml)
chart-host:   # package all four charts (baremetal-lifecycle + baremetal-discovery + baremetal-host + kubernetes-cluster) and serve them
	helm package charts/baremetal-lifecycle -d dist/
	helm package charts/baremetal-discovery -d dist/
	helm package charts/baremetal-host      -d dist/
	helm package charts/kubernetes-cluster  -d dist/
	$(KUBECTL) -n $(IRONIC_NS) create deployment chartrepo --image=nginx:1.27-alpine --dry-run=client -o yaml | $(KUBECTL) apply -f -
	$(KUBECTL) -n $(IRONIC_NS) expose deployment chartrepo --port=80 --dry-run=client -o yaml | $(KUBECTL) apply -f - 2>/dev/null || true
	$(KUBECTL) -n $(IRONIC_NS) rollout status deploy/chartrepo --timeout=120s
	$(KUBECTL) -n $(IRONIC_NS) cp dist/baremetal-lifecycle-$(LIFECYCLE_CHART_VERSION).tgz \
		$$($(KUBECTL) -n $(IRONIC_NS) get pod -l app=chartrepo -o jsonpath='{.items[0].metadata.name}'):/usr/share/nginx/html/baremetal-lifecycle-$(LIFECYCLE_CHART_VERSION).tgz
	$(KUBECTL) -n $(IRONIC_NS) cp dist/baremetal-discovery-$(DISCOVERY_CHART_VERSION).tgz \
		$$($(KUBECTL) -n $(IRONIC_NS) get pod -l app=chartrepo -o jsonpath='{.items[0].metadata.name}'):/usr/share/nginx/html/baremetal-discovery-$(DISCOVERY_CHART_VERSION).tgz
	$(KUBECTL) -n $(IRONIC_NS) cp dist/baremetal-host-$(HOST_CHART_VERSION).tgz \
		$$($(KUBECTL) -n $(IRONIC_NS) get pod -l app=chartrepo -o jsonpath='{.items[0].metadata.name}'):/usr/share/nginx/html/baremetal-host-$(HOST_CHART_VERSION).tgz
	$(KUBECTL) -n $(IRONIC_NS) cp dist/kubernetes-cluster-$(CLUSTER_CHART_VERSION).tgz \
		$$($(KUBECTL) -n $(IRONIC_NS) get pod -l app=chartrepo -o jsonpath='{.items[0].metadata.name}'):/usr/share/nginx/html/kubernetes-cluster-$(CLUSTER_CHART_VERSION).tgz

check-examples:   # every manifest a reader is told to apply would actually apply
	# Catches the two defect classes that `helm lint` and kubeconform cannot see
	# and only the API server rejects: a kind no RestDefinition makes real, and a
	# composition apiVersion from a pruned chart version. Both found in this repo
	# on 2026-11-04. Historical test fixtures are deliberately out of scope.
	#
	# The self-test runs FIRST and the && is load-bearing: it proves the check can
	# still fail, against real files built to be broken, so a pass below means
	# something. Without it a screen that silently stopped detecting would report
	# OK forever -- which is the failure mode the check itself exists to catch,
	# one level up.
	python3 scripts/check-example-manifests.py --self-test && \
		python3 scripts/check-example-manifests.py

check-mapping-sources:   # every request-direction fieldMapping reads from a field that EXISTS
	# A mapping declares `inPath: X <- inCustomResource: spec.Y`, and declaring it
	# has a side effect: oasgen removes X's spec property. So when Y == X the
	# mapping's own declaration deletes the field it reads from, and the parameter
	# resolves to nothing on the wire -- oasgen-provider #419, which cost five days
	# here because the 406 it produced went unread.
	#
	# This reads the DOCUMENTS, so it answers before a single API call, and it is
	# calibrated the hard way: it must find the two known-broken mappings and will
	# exit NON-ZERO if it finds zero. The chain is merged -> tagged -> published ->
	# deployed -> REGENERATED, and only the last rung decides what these documents
	# render to. #421 is merged (8461460), in no released tag, the generator here
	# is 0.50.0 -- and even once it ships, oasgen does not regenerate an existing
	# CRD on upgrade (#453), so a deployed fix alone changes nothing. Verified on
	# the stored side: node_id is ABSENT from both served CRD schemas. A clean run
	# means the screen broke, not that the repo healed.
	python3 -I scripts/check-mapping-sources.py

regenerate-crds:   # force oasgen to re-read the OAS and rewrite the generated CRDs
	# oasgen does NOT regenerate an existing CRD when the provider is upgraded --
	# Observe compares four hashes and none covers the generator version (#453).
	# So a version that fixes a schema defect leaves every already-generated CRD
	# exactly as the broken version wrote it. Upgrading is not the procedure;
	# upgrading and then regenerating is, and `oasHash` is the hash that moves.
	#
	# The script VERIFIES the nudge took rather than reporting success because the
	# commands ran: if oasHash does not move, it says the procedure failed and
	# refuses to let you read the stored side as a verdict on the generator.
	#
	# SCOPE: oasgen is implementing #453, which makes an upgrade regenerate by
	# itself. From whatever release carries that, this stops being the mechanism
	# and becomes the pre-#453 procedure, the opt-out path, and the recovery path
	# for anyone already sitting on a stale schema. Check whether your build
	# regenerates on its own first.
	python3 -I scripts/regenerate-restdefinition-crds.py --kubectl "$(KUBECTL)"

regenerate-crds-check:   # read the stored side only; change nothing
	python3 -I scripts/regenerate-restdefinition-crds.py --kubectl "$(KUBECTL)" --check-only

check-oas-defaults:   # no OAS body schema may acquire the v1.26.x defaulting shape
	# An OPTIONAL object property with a reachable defaulted descendant and no
	# required properties of its own gains `default: {}` under plumbing v1.26.x.
	# The apiserver materialises defaults on READ, and rdc sends top-level spec
	# values whole, so a re-render would put a stanza on the wire that nobody
	# authored -- arriving at the next re-render rather than at the upgrade.
	#
	# This is a STANDING check, not a one-off reading, because the exposure
	# arrives with a NEW OAS document and because it is NOT REVERSIBLE: once a
	# default is materialised, one unrelated write persists it to etcd, pinning
	# the CRD back does not remove it, and the field is unowned in managedFields
	# so `kubectl apply` will not prune it. Measured by the aruba fleet, which
	# found the only field instances anyone has (2 of 35).
	python3 -I scripts/check-defaulted-optional-objects.py

check: check-examples check-mapping-sources check-oas-defaults   # every standing screen
	@echo "all standing screens passed"

chart-host-pinned:   # restore the chart versions the LIVE CompositionDefinitions pin
	# `chart-host` publishes whatever is in Chart.yaml today. The chartrepo pod has
	# no volumes, so a cluster restart wipes everything it serves and leaves the
	# pinned older versions missing -- which surfaces as a chart-inspector 500 and
	# every composition going Synced=False. This rebuilds the pinned versions from
	# git and uploads them. Run it after `chart-host` on every resume.
	./scripts/chart-host-pinned.sh

composition-up: chart-host   # install all four CompositionDefinitions
	$(KUBECTL) apply -f manifests/compositiondefinition-baremetal-lifecycle.yaml
	$(KUBECTL) apply -f manifests/compositiondefinition-baremetal-discovery.yaml
	$(KUBECTL) apply -f manifests/compositiondefinition-baremetal-host.yaml
	$(KUBECTL) apply -f manifests/compositiondefinition-kubernetes-cluster.yaml
	$(KUBECTL) -n krateo-system wait --for=condition=Ready compositiondefinition/baremetal-lifecycle --timeout=180s
	$(KUBECTL) -n krateo-system wait --for=condition=Ready compositiondefinition/baremetal-discovery --timeout=180s
	$(KUBECTL) -n krateo-system wait --for=condition=Ready compositiondefinition/baremetal-host --timeout=180s
	$(KUBECTL) -n krateo-system wait --for=condition=Ready compositiondefinition/kubernetes-cluster --timeout=180s

composition-demo: # create a BaremetalLifecycle instance; composition-dynamic-controller walks it enroll -> active
	$(KUBECTL) apply -f manifests/baremetallifecycle-example.yaml
	@echo "watch: $(KUBECTL) -n $(IRONIC_NS) get node.baremetal.ogen.krateo.io metal-a -o jsonpath='{.status.provision_state}'"

# --- Real Ironic via a Keystone-auth proxy --------------------------------------
# Point the operator at any Keystone-protected Ironic (on-prem or hosted) without changing the
# operator: a proxy authenticates with your clouds.yaml and the `ironic` Service is repointed
# at it. The OAS server URL never changes.
CLOUDS_FILE ?= clouds.yaml
# Default cloud entry name used by the openstack client in lab tooling when
# authenticating to the lab Keystone. Must match an entry in clouds.yaml.
# `ironic-system` is the system-scoped admin entry used in the Ettore lab
# (see reference_ironic-system-scope-clouds memory: project-scoped admin
# returns 403 on baremetal:port:create at the deploy walk).
OS_CLOUD ?= ironic-system

# --- What backs Service/ironic ---------------------------------------------------
#
# AGNOSTIC BY CONSTRUCTION, and verified rather than asserted on 2026-11-04:
# every OAS document addresses `ironic.openstack.svc.cluster.local:6385`, and a
# server-side render of kubernetes-cluster contains ZERO occurrences of
# `wireguard`, `wg0`, `bifrost` or `wg-lab-route`. The operator and the charts
# cannot tell whether Ironic is in-cluster, on another host, or across a VPN.
# The only addresses in the render come from values (environment input) or are
# derived at runtime -- the VIP the bootstrap node picks with `arping -D`.
#
# So "which Ironic" is a property of THIS layer only, and switching it is
# ALWAYS EXPLICIT. There is deliberately no `*-down` target any more: the two
# that existed patched `selector: {app: ironic}`, which selects the in-cluster
# FAKE Ironic holding zero nodes. They read as "undo" and silently repointed
# Krateo at an empty Ironic -- every node would appear to vanish, with nothing
# erroring. Measured 2026-11-04.
#
# Point this at your own environment's manifest; nothing below assumes a VPN.
IRONIC_BACKEND_MANIFEST ?= manifests/wg-lab-route.yaml

ironic-backend-direct: # Service/ironic -> the endpoint IRONIC_BACKEND_MANIFEST declares (no proxy)
	@test -f "$(IRONIC_BACKEND_MANIFEST)" || { echo "ERROR: IRONIC_BACKEND_MANIFEST not found: '$(IRONIC_BACKEND_MANIFEST)'"; exit 1; }
	# selector:null FIRST. A Service with any selector makes Kubernetes manage
	# its EndpointSlices, which silently overrides the hand-written one -- so
	# applying the manifest while a selector is still set does nothing useful.
	-$(KUBECTL) -n $(IRONIC_NS) patch service ironic --type=merge -p '{"spec":{"selector":null}}'
	$(KUBECTL) apply -f $(IRONIC_BACKEND_MANIFEST)
	@echo "ironic Service -> endpoint from $(IRONIC_BACKEND_MANIFEST). Operator endpoint (OAS) unchanged."
	$(KUBECTL) -n $(IRONIC_NS) get endpointslice -l kubernetes.io/service-name=ironic \
		-o custom-columns=NAME:.metadata.name,ENDPOINTS:.endpoints[*].addresses

ironic-backend-local: # Service/ironic -> the in-cluster fake Ironic (local testing ONLY)
	@echo "NOTE: this selects the in-cluster fake Ironic. It holds no real nodes."
	$(KUBECTL) -n $(IRONIC_NS) patch service ironic -p '{"spec":{"selector":{"app":"ironic"}}}'

# --- Remote standalone Ironic (Bifrost) -----------------------------------------
# Point the operator at a real Bifrost on a remote Linux host. See docs/BIFROST.md.
bifrost-up:   # deploy the in-cluster proxy and point `ironic` Service at a remote Bifrost (set BIFROST_URL=http://host:6385)
	@test -n "$(BIFROST_URL)" || { echo "ERROR: set BIFROST_URL=http://<bifrost-host>:6385"; exit 1; }
	@sed 's|__UPSTREAM__|$(BIFROST_URL)|' manifests/bifrost-nginx.conf.tpl > /tmp/bifrost-nginx.conf
	$(KUBECTL) -n $(IRONIC_NS) create configmap bifrost-proxy-nginx \
		--from-file=nginx.conf=/tmp/bifrost-nginx.conf --dry-run=client -o yaml | $(KUBECTL) apply -f -
	$(KUBECTL) apply -f manifests/bifrost-proxy.yaml
	$(KUBECTL) -n $(IRONIC_NS) rollout restart deploy/bifrost-proxy
	$(KUBECTL) -n $(IRONIC_NS) rollout status deploy/bifrost-proxy --timeout=120s
	# Setting ANY selector makes Kubernetes manage this Service's EndpointSlices,
	# which overrides the hand-written one. That is intended here and it is also
	# destructive: it replaces whatever backend was in place.
	@echo "WARNING: this replaces the current Service/ironic backend. Restore afterwards with"
	@echo "         make ironic-backend-direct   (or ironic-backend-local for the fake)"
	$(KUBECTL) -n $(IRONIC_NS) patch service ironic -p '{"spec":{"selector":{"app":"bifrost-proxy"}}}'
	@echo "ironic Service -> Bifrost at $(BIFROST_URL). Operator endpoint (OAS) unchanged."

# bifrost-down REMOVED 2026-11-04: it patched `selector: {app: ironic}`, i.e. the
# in-cluster fake Ironic. To leave bifrost mode, say which backend you want:
#   make ironic-backend-direct   (the endpoint IRONIC_BACKEND_MANIFEST declares)
#   make ironic-backend-local    (the in-cluster fake, for local testing)

# --- Lab cluster: dedicated kind cluster with in-cluster WireGuard + Keystone proxy ----
# A separate kind cluster (default name: ironic-lab) where the WireGuard tunnel runs inside the
# proxy pod, so the host's networking stays untouched. The operator pods talk to
# `ironic.openstack.svc.cluster.local:6385` and traffic exits via WG to the real Ironic.
LAB_CLUSTER ?= ironic-lab
WG_CONF     ?= local/wireguard/ironic-lab.conf
# NOTE: `managementCluster.apiUrl` (http://198.51.100.5:8443) has no listener.
#
# Measured 2026-11-04, and the ADDRESS is fine -- the LISTENER is gone:
#   * the live wg-lab-route pod still holds 198.51.100.5/32 on wg0, and the
#     lab routes 198.51.100.0/28 through the tunnel, so the address resolves
#     and routes. (198.51.100.0/24 is RFC 5737 TEST-NET-2, chosen deliberately
#     as an overlay range precisely because it is not routed on the internet.)
#   * but :8443 was served by a `mgmt-api-proxy` kubectl-proxy SIDECAR inside
#     the wg-ironic-proxy pod. wg-lab-route has ONE container and no such
#     sidecar, and nothing listens on 8443 on that address.
#
# So the CP cloud-init's publish-workload-kubeconfig.sh POSTs into a refused
# connection, retries 5 times, logs to the blade and exits 1 without gating
# bring-up -- which is why the <cluster>-workload-kubeconfig Secret has never
# existed. Tracked separately; do NOT "fix" it by reinstating the proxy, which
# would overwrite Service/ironic and break the route to Ironic.
# Project-scoped admin works at microversion 1.109+ for both node:create and port:create
# (Ironic's RBAC matches the caller's project against node.owner). At 1.99 it returned 403
# and needed a system_scope:all workaround; that's no longer the default.
# OS_CLOUD default is set above (currently `ironic-system`); lab tooling only.

# v2026-11-04: applies wg-lab-route, NOT the removed wg-ironic-proxy.
#
# The proxy was superseded by the "zero proxies" work: wg-lab-route is a
# DaemonSet that terminates the tunnel and publishes `ironic` and `keystone`
# Services backed by EndpointSlices pointing straight at the lab gateway. Both
# manifests declared `Service/ironic`, so this target used to OVERWRITE the
# working Service with a selector aimed at proxy pods that no longer exist --
# i.e. running it would have broken Krateo's only route to Ironic. Measured
# 2026-11-04; the proxy manifest is deleted.
lab-tunnel-up: # apply the WG route DaemonSet + the extra-bridge + their Secrets/ConfigMaps
	@test -f "$(WG_CONF)" || { echo "ERROR: set WG_CONF (got '$(WG_CONF)')"; exit 1; }
	@test -f "$(CLOUDS_FILE)" || { echo "ERROR: set CLOUDS_FILE (got '$(CLOUDS_FILE)')"; exit 1; }
	$(KUBECTL) apply -f manifests/wg-lab-route.yaml
	$(KUBECTL) -n $(IRONIC_NS) create secret generic ironic-wg-config \
		--from-file=wg0.conf="$(WG_CONF)" --dry-run=client -o yaml | $(KUBECTL) apply -f -
	$(KUBECTL) -n $(IRONIC_NS) create secret generic ironic-clouds \
		--from-file=clouds.yaml="$(CLOUDS_FILE)" --dry-run=client -o yaml | $(KUBECTL) apply -f -
	$(KUBECTL) -n $(IRONIC_NS) create configmap ironic-extra-bridge-script \
		--from-file=ironic-node-extra-bridge.py=scripts/ironic-node-extra-bridge.py --dry-run=client -o yaml | $(KUBECTL) apply -f -
	$(KUBECTL) apply -f manifests/ironic-extra-bridge.yaml
	$(KUBECTL) -n $(IRONIC_NS) rollout status ds/wg-lab-route --timeout=300s
	$(KUBECTL) -n $(IRONIC_NS) rollout status deploy/ironic-extra-bridge --timeout=300s

lab-up: # bring up the lab cluster end-to-end (kind + Krateo + WG/proxy + RestDefinitions)
	$(MAKE) KIND_CLUSTER=$(LAB_CLUSTER) KUBECONFIG_FILE=local/kubeconfig.$(LAB_CLUSTER) \
		kind-up krateo-up lab-tunnel-up restdef-up

lab-down: # delete the lab kind cluster (does NOT touch your default kubeconfig)
	-kind delete cluster --name $(LAB_CLUSTER) --kubeconfig local/kubeconfig.$(LAB_CLUSTER)

local-up: kind-up ironic-up krateo-up restdef-up   # full local stack: kind + Ironic + Krateo + RestDefinitions

local-down:   # delete the whole local kind cluster
	-kind delete cluster --name $(KIND_CLUSTER) --kubeconfig $(KUBECONFIG_FILE)
