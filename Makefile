# openstack-ironic-operator - Ironic + Krateo integration
.PHONY: help apply-oas apply-restdef deploy-chart package-chart template-chart deploy-ironic validate-chart \
        kind-up kind-down local-up local-down ironic-up ironic-down ironic-forward smoke-test kubeconfig

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
	@echo "  bifrost-down   - Repoint the ironic Service back at the local fake Ironic"
	@echo "  keystone-up    - Point the operator at a Keystone-protected Ironic (CLOUDS_FILE + OS_CLOUD)"
	@echo "  keystone-down  - Repoint the ironic Service back at the local fake Ironic"
	@echo "Dedicated lab cluster (WireGuard tunnel runs in-cluster, host networking untouched):"
	@echo "  lab-up         - Spin up a separate kind cluster with WG + Keystone proxy + operator"
	@echo "  lab-down       - Delete the lab kind cluster"
	@echo "  ironic-forward - Port-forward Ironic API to localhost:6385"
	@echo "  smoke-test     - Drive a fake node enroll->active against local Ironic"
	@echo "  local-down     - Delete the local kind cluster"

apply-oas:
	./scripts/create-ironic-oas-configmap.sh $(IRONIC_NS)

apply-restdef: apply-oas
	kubectl apply -f manifests/restdefinition-node.yaml
	kubectl apply -f manifests/restdefinition-port.yaml
	kubectl apply -f manifests/restdefinition-provision.yaml

deploy-chart:
	helm upgrade --install baremetal-lifecycle ./charts/baremetal-lifecycle \
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
	helm upgrade --install ironic openstack-helm/ironic \
		--namespace=$(IRONIC_NS) \
		-f deploy/values/ironic-overrides.yaml

# --- Local test environment ----------------------------------------------------
# An isolated kind cluster + standalone Ironic (official openstack-helm image, fake
# drivers, noauth). All kubectl/helm calls use --kubeconfig $(KUBECONFIG_FILE) and
# --context $(KCTX), so the default ~/.kube/config is never touched.

kubeconfig:   # (re)write the isolated kubeconfig for the kind cluster
	kind export kubeconfig --name $(KIND_CLUSTER) --kubeconfig $(KUBECONFIG_FILE)

kind-up:      # create the local kind cluster (idempotent; isolated kubeconfig)
	@kind get clusters 2>/dev/null | grep -qx $(KIND_CLUSTER) || \
		kind create cluster --name $(KIND_CLUSTER) --kubeconfig $(KUBECONFIG_FILE) --wait 60s
	@kind export kubeconfig --name $(KIND_CLUSTER) --kubeconfig $(KUBECONFIG_FILE) >/dev/null
	@echo "kubeconfig=$(KUBECONFIG_FILE) context=$(KCTX)"

ironic-up:    # deploy standalone Ironic (fake drivers, noauth) into the kind cluster
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
KRATEO_OASGEN_VERSION ?= 0.11.1

krateo-up:    # install Krateo KOG (oasgen-provider) + composition engine (core-provider)
	helm repo add krateo https://charts.krateo.io 2>/dev/null || true
	helm repo update krateo
	$(HELMK) upgrade --install krateo-core-provider krateo/core-provider \
		-n krateo-system --create-namespace --version $(KRATEO_CORE_VERSION) --wait --timeout 6m
	$(HELMK) upgrade --install krateo-oasgen-provider krateo/oasgen-provider \
		-n krateo-system --version $(KRATEO_OASGEN_VERSION) --wait --timeout 6m
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
# Default cloud entry name used by wg-ironic-proxy / keystone-ironic-proxy when
# authenticating to the lab Keystone. Must match an entry in clouds.yaml.
# `ironic-system` is the system-scoped admin entry used in the Ettore lab
# (see reference_ironic-system-scope-clouds memory: project-scoped admin
# returns 403 on baremetal:port:create at the deploy walk).
OS_CLOUD ?= ironic-system

keystone-up: # deploy the Keystone-auth proxy and point the `ironic` Service at your real Ironic
	@test -f "$(CLOUDS_FILE)" || { echo "ERROR: set CLOUDS_FILE=<path to clouds.yaml> (got '$(CLOUDS_FILE)')"; exit 1; }
	$(KUBECTL) -n $(IRONIC_NS) create secret generic ironic-clouds \
		--from-file=clouds.yaml="$(CLOUDS_FILE)" --dry-run=client -o yaml | $(KUBECTL) apply -f -
	$(KUBECTL) -n $(IRONIC_NS) create configmap keystone-ironic-proxy-script \
		--from-file=keystone-ironic-proxy.py=scripts/keystone-ironic-proxy.py --dry-run=client -o yaml | $(KUBECTL) apply -f -
	$(KUBECTL) apply -f manifests/keystone-ironic-proxy.yaml
	$(KUBECTL) -n $(IRONIC_NS) set env deploy/keystone-ironic-proxy OS_CLOUD=$(OS_CLOUD)
	$(KUBECTL) -n $(IRONIC_NS) rollout status deploy/keystone-ironic-proxy --timeout=150s
	$(KUBECTL) -n $(IRONIC_NS) patch service ironic -p '{"spec":{"selector":{"app":"keystone-ironic-proxy"}}}'
	@echo "ironic Service -> Keystone-auth proxy. Operator endpoint (OAS) unchanged."

keystone-down: # repoint the `ironic` Service back at the local fake Ironic
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
	$(KUBECTL) -n $(IRONIC_NS) patch service ironic -p '{"spec":{"selector":{"app":"bifrost-proxy"}}}'
	@echo "ironic Service -> Bifrost at $(BIFROST_URL). Operator endpoint (OAS) unchanged."

bifrost-down: # repoint the `ironic` Service back at the local fake Ironic
	$(KUBECTL) -n $(IRONIC_NS) patch service ironic -p '{"spec":{"selector":{"app":"ironic"}}}'

# --- Lab cluster: dedicated kind cluster with in-cluster WireGuard + Keystone proxy ----
# A separate kind cluster (default name: ironic-lab) where the WireGuard tunnel runs inside the
# proxy pod, so the host's networking stays untouched. The operator pods talk to
# `ironic.openstack.svc.cluster.local:6385` and traffic exits via WG to the real Ironic.
LAB_CLUSTER ?= ironic-lab
WG_CONF     ?= local/wireguard/ironic-lab.conf
# Project-scoped admin works at microversion 1.109+ for both node:create and port:create
# (Ironic's RBAC matches the caller's project against node.owner). At 1.99 it returned 403
# and needed a system_scope:all workaround; that's no longer the default.
# OS_CLOUD default lives near the keystone-up target (currently `ironic-system`).

lab-tunnel-up: # apply the wg+proxy Deployment+Service + Secrets/ConfigMap (kubeconfig from caller)
	@test -f "$(WG_CONF)" || { echo "ERROR: set WG_CONF (got '$(WG_CONF)')"; exit 1; }
	@test -f "$(CLOUDS_FILE)" || { echo "ERROR: set CLOUDS_FILE (got '$(CLOUDS_FILE)')"; exit 1; }
	$(KUBECTL) apply -f manifests/wg-ironic-proxy.yaml
	$(KUBECTL) -n $(IRONIC_NS) create secret generic ironic-wg-config \
		--from-file=wg0.conf="$(WG_CONF)" --dry-run=client -o yaml | $(KUBECTL) apply -f -
	$(KUBECTL) -n $(IRONIC_NS) create secret generic ironic-clouds \
		--from-file=clouds.yaml="$(CLOUDS_FILE)" --dry-run=client -o yaml | $(KUBECTL) apply -f -
	$(KUBECTL) -n $(IRONIC_NS) create configmap keystone-ironic-proxy-script \
		--from-file=keystone-ironic-proxy.py=scripts/keystone-ironic-proxy.py --dry-run=client -o yaml | $(KUBECTL) apply -f -
	$(KUBECTL) -n $(IRONIC_NS) set env deploy/wg-ironic-proxy OS_CLOUD=$(OS_CLOUD)
	$(KUBECTL) -n $(IRONIC_NS) rollout restart deploy/wg-ironic-proxy
	$(KUBECTL) -n $(IRONIC_NS) rollout status deploy/wg-ironic-proxy --timeout=300s

lab-up: # bring up the lab cluster end-to-end (kind + Krateo + WG/proxy + RestDefinitions)
	$(MAKE) KIND_CLUSTER=$(LAB_CLUSTER) KUBECONFIG_FILE=local/kubeconfig.$(LAB_CLUSTER) \
		kind-up krateo-up lab-tunnel-up restdef-up

lab-down: # delete the lab kind cluster (does NOT touch your default kubeconfig)
	-kind delete cluster --name $(LAB_CLUSTER) --kubeconfig local/kubeconfig.$(LAB_CLUSTER)

local-up: kind-up ironic-up krateo-up restdef-up   # full local stack: kind + Ironic + Krateo + RestDefinitions

local-down:   # delete the whole local kind cluster
	-kind delete cluster --name $(KIND_CLUSTER) --kubeconfig $(KUBECONFIG_FILE)
