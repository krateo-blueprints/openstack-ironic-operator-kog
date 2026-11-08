{{- define "kubernetes-cluster.nodeApiVersion" -}}baremetal.ogen.krateo.io/v1-0-0{{- end -}}

{{/* Per-node CR kind/apiVersion. The chart renders BaremetalHost
     (unified composition) instead of BaremetalLifecycle so we can lean
     on the v0.3.4 widened `spec.undeploy` gate for drain & undeploy
     (Milestone 5b) and image swaps (Milestone 5a). Single CR per node
     throughout its lifecycle. */}}
{{/* v0.13.11: the child BaremetalHost apiVersion. This WAS the literal
     `composition.krateo.io/v0-5-5`, which wedged the whole composition the
     moment baremetal-host was bumped to 0.5.6: core-provider stops serving
     the old CRD version, chart-inspector then fails every render with
     "no matches for kind BaremetalHost in version composition.krateo.io/v0-5-5",
     cdc reports `Cannot observe external resource` in a loop, and NOTHING
     reconciles -- the parent and child charts are in lockstep through a
     string neither declares. Overridable now, so the next bump is a value
     change rather than a chart release. */}}
{{/*
The BaremetalHost apiVersion this parent renders. `lifecycleApiVersion` is
OPTIONAL in the generated CRD and carries no schema default, so this fallback is
REACHABLE: any KubernetesCluster CR that omits the field renders with it.

It therefore has to name the child version this parent is released against, and
it was stale at v0-5-6 while the served child version was v0-6-2 -- so a cluster
CR created without the field would have rendered BaremetalHosts at an apiVersion
the apiserver no longer serves, and wedged with a confusing error. The existing
lab CR sets the field explicitly, which is the only reason this never fired.

`make check-examples` now asserts this string equals charts/baremetal-host's own
Chart.yaml version, so bumping the child without bumping this fails the build
instead of waiting for a fresh cluster CR to discover it.
*/}}
{{- define "kubernetes-cluster.lifecycleApiVersion" -}}
{{- default "composition.krateo.io/v0-6-3" .Values.lifecycleApiVersion -}}
{{- end -}}
{{- define "kubernetes-cluster.lifecycleKind" -}}BaremetalHost{{- end -}}

{{- define "kubernetes-cluster.lifecycleNamespace" -}}
{{- if .Values.lifecycleNamespace -}}{{- .Values.lifecycleNamespace -}}{{- else -}}{{- .Release.Namespace -}}{{- end -}}
{{- end -}}

{{/* Stable name for the rendered BaremetalLifecycle CR for a given node. */}}
{{- define "kubernetes-cluster.lifecycleName" -}}
{{- .nodeName -}}
{{- end -}}

{{/* Effective list of control-plane node specs. HA support: prefer
     spec.controlPlane.nodes[] when populated; fall back to the legacy
     single-node spec.controlPlane.node for back-compat with single-CP
     deployments. Index 0 is the BOOTSTRAP CP — the one that runs
     `kubeadm init --upload-certs` and publishes both kubeadm_join and
     cert_key on its own BaremetalNode CR. Replica CPs (index 1..N-1) join via
     `kubeadm join --control-plane --certificate-key`. */}}
{{- define "kubernetes-cluster.cpNodes" -}}
{{- $cp := default (dict) .Values.controlPlane -}}
{{- $nodes := default (list) $cp.nodes -}}
{{- if gt (len $nodes) 0 -}}
{{- toYaml $nodes -}}
{{- else if and $cp.node $cp.node.nodeName -}}
{{- toYaml (list $cp.node) -}}
{{- end -}}
{{- end -}}

{{/* CP node name shortcut. Returns the BOOTSTRAP CP's nodeName
     (.controlPlane.nodes[0].nodeName OR legacy .controlPlane.node.nodeName).
     Used by the rendezvous lookups (joinCommand, certKey, cpToken). */}}
{{- define "kubernetes-cluster.cpNodeName" -}}
{{- $cp := default (dict) .Values.controlPlane -}}
{{- $nodes := default (list) $cp.nodes -}}
{{- if gt (len $nodes) 0 -}}
{{- (index $nodes 0).nodeName -}}
{{- else if $cp.node -}}
{{- $cp.node.nodeName -}}
{{- end -}}
{{- end -}}

{{/* Bootstrap CP's nodeUuid — sibling to cpNodeName. Used by
     publish-join.sh to target the right Ironic node. */}}
{{- define "kubernetes-cluster.bootstrapNodeUuid" -}}
{{- $cp := default (dict) .Values.controlPlane -}}
{{- $nodes := default (list) $cp.nodes -}}
{{- if gt (len $nodes) 0 -}}
{{- (index $nodes 0).nodeUuid -}}
{{- else if $cp.node -}}
{{- $cp.node.nodeUuid -}}
{{- end -}}
{{- end -}}

{{/* The bootstrap CP's certificate-key, read live from the CP BaremetalNode CR's
     spec.extra.cert_key. Empty until the bootstrap CP's cloud-init has
     finished `kubeadm init --upload-certs --certificate-key=$KEY` AND
     patched its own BaremetalNode CR. This is the gate that controls whether
     replica CPs render — without the key they can't decrypt the
     uploaded kubeadm-certs Secret. The TTL is 2h (kubeadm hard-coded).

     https://kubernetes.io/docs/setup/production-environment/tools/kubeadm/high-availability/
     "Please note that the certificate-key gives access to cluster
      sensitive data, keep it secret! As a safeguard, uploaded-certs will
      be deleted in two hours; If necessary, you can use kubeadm init
      phase upload-certs to reload certs afterward." */}}
{{/* The drain-scoped kubeconfig the bootstrap CP published into Ironic's
     node.extra, mirrored onto the CR by the extra-bridge. Base64 as published;
     a Secret's `data` wants base64, so it is carried through unchanged rather
     than decoded and re-encoded.

     Empty until the CP has booted and the bridge has mirrored the key -- the
     same two-phase shape as cert_key and cp_endpoint. The Secret template
     renders nothing while it is empty, and drain-jobs.yaml already skips when
     the Secret is absent, so the gate is the existing one. */}}
{{- define "kubernetes-cluster.drainKubeconfig" -}}
{{- $ns  := include "kubernetes-cluster.lifecycleNamespace" . -}}
{{- $cp  := include "kubernetes-cluster.cpNodeName"          . -}}
{{- $node := lookup (include "kubernetes-cluster.nodeApiVersion" .) "BaremetalNode" $ns $cp -}}
{{- if $node -}}
{{- $extra := dig "spec" "extra" (dict) $node -}}
{{- $kc    := index $extra "drain_kubeconfig" -}}
{{- if $kc -}}{{- $kc -}}{{- end -}}
{{- end -}}
{{- end -}}

{{- define "kubernetes-cluster.certKey" -}}
{{- $ns  := include "kubernetes-cluster.lifecycleNamespace" . -}}
{{- $cp  := include "kubernetes-cluster.cpNodeName"          . -}}
{{- $node := lookup (include "kubernetes-cluster.nodeApiVersion" .) "BaremetalNode" $ns $cp -}}
{{- if $node -}}
{{- $extra := dig "spec" "extra" (dict) $node -}}
{{- $ck    := index $extra "cert_key" -}}
{{- if $ck -}}{{- $ck -}}{{- end -}}
{{- end -}}
{{- end -}}

{{/* The bootstrap CP's own etcd address, read live from its BaremetalNode CR's
     spec.extra.etcd_ip. Published by publish-join.sh from the address the node
     DERIVED for itself (see cp-init.sh ADVERTISE_IP).

     v0.13.2: this replaces .controlPlane.nodes[].oobIp. An IP typed into the CR
     is a fact about one lab frozen into desired state: it cannot be validated
     at render time, it goes stale when a lease moves, and the same number had
     to be kept consistent by hand with the advertise address the node derives
     anyway. The node is the only thing that reliably knows its own address, so
     it publishes it through the rendezvous that already carries kubeadm_join
     and cert_key.

     Empty until the bootstrap CP has run cp-init.sh and patched itself, which
     makes it an FSM gate for replica CPs exactly like cert_key. */}}
{{- define "kubernetes-cluster.bootstrapEtcdIp" -}}
{{- $ns  := include "kubernetes-cluster.lifecycleNamespace" . -}}
{{- $cp  := include "kubernetes-cluster.cpNodeName"          . -}}
{{- $node := lookup (include "kubernetes-cluster.nodeApiVersion" .) "BaremetalNode" $ns $cp -}}
{{- if $node -}}
{{- $extra := dig "spec" "extra" (dict) $node -}}
{{- $ip    := index $extra "etcd_ip" -}}
{{- if $ip -}}{{- $ip -}}{{- end -}}
{{- end -}}
{{- end -}}

{{/* The captured `kubeadm join` command, read live from the CP BaremetalNode CR's
     spec.extra.kubeadm_join. Empty until the CP cloud-init has finished
     `kubeadm init` AND patched its own BaremetalNode CR. This is the FSM gate
     that controls whether worker BaremetalLifecycles get rendered. */}}
{{- define "kubernetes-cluster.joinCommand" -}}
{{- $ns  := include "kubernetes-cluster.lifecycleNamespace" . -}}
{{- $cp  := include "kubernetes-cluster.cpNodeName"          . -}}
{{- $node := lookup (include "kubernetes-cluster.nodeApiVersion" .) "BaremetalNode" $ns $cp -}}
{{- if $node -}}
{{- $extra := dig "spec" "extra" (dict) $node -}}
{{- $jc    := index $extra "kubeadm_join" -}}
{{- if $jc -}}{{- $jc -}}{{- end -}}
{{- end -}}
{{- end -}}

{{/* Image dict in BaremetalHost shape (source + checksum). Differs from
     baremetal-lifecycle's flatter `instance_info.image_source` —
     baremetal-host nests under spec.image. */}}
{{- define "kubernetes-cluster.image" -}}
source:   {{ .Values.image.source   | quote }}
checksum: {{ .Values.image.checksum | quote }}
{{- end -}}

{{/* networkData generator. Without this, cloud-init only DHCPs the
     first detected interface — which on the Ettore lab is `enp1s0`
     bridged to oob0 (BMC network, no internet). The data NIC
     (`enp2s0`, lan0-bridged with default route to internet) stays
     DOWN and apt/kubeadm install fails.

     We render one link + DHCP network per port in ports[]. Naming
     follows the existing fixtures' enp1s0/enp2s0 convention (positional;
     cloud-init resolves by MAC).

     If the caller passed an explicit networkData, use it verbatim. */}}
{{- define "kubernetes-cluster.networkData" -}}
{{- $node := .node -}}
{{- if $node.networkData -}}
{{- toYaml $node.networkData -}}
{{- else if $node.ports -}}
links:
{{- range $i, $p := $node.ports }}
  - id: {{ printf "enp%ds0" (add $i 1) }}
    type: phy
    ethernet_mac_address: {{ $p.address | quote }}
{{- end }}
networks:
{{- range $i, $p := $node.ports }}
  - id: {{ printf "enp%ds0" (add $i 1) }}
    network_id: {{ printf "enp%ds0" (add $i 1) }}
    type: ipv4_dhcp
    link: {{ printf "enp%ds0" (add $i 1) }}
{{- end }}
services:
  - type: dns
    address: "8.8.8.8"
{{- end -}}
{{- end -}}

{{/* Workers explicitly marked for removal in spec.workers.removed[]. */}}
{{- define "kubernetes-cluster.removedWorkers" -}}
{{- $workers := default (dict) .Values.workers -}}
{{- toYaml (default (list) $workers.removed) -}}
{{- end -}}

{{/* Secret in the lifecycle namespace that carries the workload cluster's
     admin.conf — published by the CP cloud-init on first boot, used by
     the drain Jobs (templates/drain-jobs.yaml) to talk to the workload
     cluster's apiserver. */}}
{{- define "kubernetes-cluster.workloadKubeconfigSecretName" -}}
{{- printf "%s-workload-kubeconfig" .Values.clusterName -}}
{{- end -}}

{{/* Has the drain Job for a given worker completed successfully?
     Looked up live so the workers template can gate undeploy on it. */}}
{{- define "kubernetes-cluster.workerDrainComplete" -}}
{{- $ns   := include "kubernetes-cluster.lifecycleNamespace" . -}}
{{- $name := printf "drain-%s-%s" .Values.clusterName .workerName -}}
{{- $job  := lookup "batch/v1" "Job" $ns $name -}}
{{- if $job -}}
{{- $st := default (dict) $job.status -}}
{{- if eq (default 0 $st.succeeded | int) 1 -}}true{{- end -}}
{{- end -}}
{{- end -}}

{{/* Management-cluster CA bundle for the CP's `kubectl patch` against the
     management API. Tries the auto-projected `kube-root-ca.crt` ConfigMap
     in the SA namespace first (root-ca-cert-publisher controller, k8s 1.20+),
     then falls back to an explicit .Values.managementCluster.caBundle for
     hardened clusters where the auto-projection is disabled. Returns empty
     when nothing resolves — lifecycle-cp.yaml gates on this. */}}
{{- define "kubernetes-cluster.mgmtCaBundle" -}}
{{- $mc := default (dict) .Values.managementCluster -}}
{{- $ns := $mc.serviceAccountNamespace | default .Release.Namespace -}}
{{- $cm := lookup "v1" "ConfigMap" $ns "kube-root-ca.crt" -}}
{{- if $cm -}}
{{- $ca := index (default (dict) $cm.data) "ca.crt" -}}
{{- if $ca -}}{{- $ca -}}{{- end -}}
{{- else if $mc.caBundle -}}
{{- $mc.caBundle -}}
{{- end -}}
{{- end -}}

{{/* The `--control-plane-endpoint` advertised to kubeadm init. Empty for
     single-CP clusters (Ironic node's primary IP becomes the apiserver
     advertise address — kubeadm default). Non-empty for HA — must be a
     stable address (LB VIP / DNS) reachable from every member blade.
     See docs/USER-GUIDE.md "Cluster ingress" section for option matrix. */}}
{{- define "kubernetes-cluster.controlPlaneEndpoint" -}}
{{- $cp := default (dict) .Values.controlPlane -}}
{{- if $cp.endpoint -}}
{{- $cp.endpoint -}}
{{- else -}}
{{- /* v0.13.2: DERIVED. No endpoint declared, so use the one the bootstrap CP
       picked for itself and published to spec.extra.cp_endpoint. Empty on the
       first render — which is correct and is the same two-phase shape the etcd
       PKI and cert_key gates already rely on: the bootstrap boots with no
       endpoint (kubeadm then advertises its own primary IP, the single-CP
       path), derives a free VIP, claims it with kube-vip, publishes it, and
       the NEXT reconcile renders replicas and workers against it. */ -}}
{{- $ep := include "kubernetes-cluster.publishedCpEndpoint" . -}}
{{- if $ep -}}{{- $ep -}}{{- end -}}
{{- end -}}
{{- end -}}

{{/* The control-plane endpoint the bootstrap CP DERIVED and published to its
     own BaremetalNode CR's spec.extra.cp_endpoint.

     v0.13.2: this is why controlPlane.endpoint is no longer required. A VIP
     typed into the CR is an address the chart cannot validate: nothing checks
     it is inside the blades' subnet (the kube-vip interface lookup silently
     finds no NIC if it is not), and nothing checks it is unused. The bootstrap
     node can check both, because it is ON that subnet — so it probes and
     chooses, and the chart learns the answer. */}}
{{- define "kubernetes-cluster.publishedCpEndpoint" -}}
{{- $ns  := include "kubernetes-cluster.lifecycleNamespace" . -}}
{{- $cp  := include "kubernetes-cluster.cpNodeName"          . -}}
{{- $node := lookup (include "kubernetes-cluster.nodeApiVersion" .) "BaremetalNode" $ns $cp -}}
{{- if $node -}}
{{- $extra := dig "spec" "extra" (dict) $node -}}
{{- $ep    := index $extra "cp_endpoint" -}}
{{- if $ep -}}{{- $ep -}}{{- end -}}
{{- end -}}
{{- end -}}

{{/* Strip the optional ":port" off controlPlane.endpoint and return just
     the host (or IP). Used by the kube-vip static-pod manifest to know
     which address to claim. Empty when the user hasn't set endpoint. */}}
{{- define "kubernetes-cluster.controlPlaneVip" -}}
{{- $ep := include "kubernetes-cluster.controlPlaneEndpoint" . -}}
{{- if $ep -}}
{{- regexReplaceAll ":[0-9]+$" $ep "" -}}
{{- end -}}
{{- end -}}

{{/* === v0.11.0: External etcd PKI ===

     HA clusters (cpNodes count > 1) run a 3-member etcd cluster as a
     SYSTEMD UNIT on each CP — independent of kubeadm's stacked-etcd
     flow. This block manages the PKI:

       - etcd CA (cert + key, 10y validity)
       - apiserver-etcd-client cert (cert + key, 10y, signed by CA)
       - per-CP etcd server cert: NOT minted here. kubeadm mints it on the
         node from the chart-supplied CA, so its SANs come from the node's
         own addresses. This is why 0.13.2 could drop
         .controlPlane.nodes[].oobIp outright: the comment previously
         claimed the SAN needed a declared IP, but nothing in this chart
         ever generated that cert

     Stability: certs are generated ONCE on the first chart render via
     genCA / genSignedCert. Subsequent renders `lookup` the persisted
     Secret (`<release>-etcd-pki`) and reuse the existing material.
     Without lookup-and-reuse, every cdc reconcile (~60s) would rotate
     the CA and break the running cluster.

     Two-pass apply caveat: BaremetalHost templates gate on
     `etcdPkiReady` returning "yes". On the first chart render the
     Secret doesn't exist in the live cluster yet, so lookup returns
     nil, the per-CP server cert paths are empty, and `etcdPkiReady`
     returns empty → BHs don't render. Only the Secret manifest
     renders. cdc applies the Secret. ~60s later the next reconcile
     hits the Secret via lookup, BHs render with real certs, deploy
     proceeds. Documented in docs/KUBERNETES-CLUSTER-V0.11.0-DESIGN.md. */}}

{{/* === v0.12.0: chart-managed kubeadm PKI ===

     One chart handles both single-CP and HA (1, 3, or 5 CPs). All
     PKI is pre-generated at chart render time and baked into every CP's
     cloud-init via write_files. No runtime rendezvous between bootstrap
     and replicas: replicas wait for the apiserver VIP to be healthy,
     then kubeadm-join with chart-baked token + chart-computable CA hash.

     PKI material per kubeadm's expectations:
       - cluster CA            → /etc/kubernetes/pki/ca.{crt,key}
       - front-proxy CA        → /etc/kubernetes/pki/front-proxy-ca.{crt,key}
       - etcd CA (stacked)     → /etc/kubernetes/pki/etcd/ca.{crt,key}
       - SA signing key + pub  → /etc/kubernetes/pki/sa.{key,pub}
       - bootstrap token       → baked into kubeadm-init.yaml + replica
                                 join.sh + worker join.sh

     2-pass apply: first chart render creates the kubeadm-pki Secret;
     `kubeadmPkiReady` returns empty until the Secret exists in the
     live cluster. BH templates gate on this so they don't render with
     empty PKI. ~60s later the next cdc reconcile sees the Secret and
     renders the BHs.

     Stacked etcd: kubeadm manages etcd cluster expansion natively on
     `kubeadm join --control-plane`. No external etcd, no install-etcd
     systemd unit. */}}

{{- define "kubernetes-cluster.kubeadmPkiSecretName" -}}
{{- printf "%s-kubeadm-pki" .Values.clusterName -}}
{{- end -}}

{{/* v0.12.3 (Task #91): the deployed-configdrive hash IS this chart's
     version. Embedded in cloud-init userData via /etc/kubernetes/
     .deployed-hash, published to Ironic.node.extra.deployed_configdrive_
     hash by publish-deployed-hash.sh in runcmd's last step, then
     mirrored to BaremetalNode CR.spec.extra by the bridge sidecar. The
     baremetal-host chart's shouldAutoRedeploy compares this value to
     the BH-baked configDriveHashInput (also = .Chart.Version when
     rendered) — match means the running OS is the chart's intended
     userData; mismatch (including empty after a teardown+redeploy)
     fires transition-undeploy.

     Stable across cdc reconciles because .Chart.Version doesn't
     change between renders of the same chart. Drift fires when the
     CHART itself bumps (operator-triggered redeploy). */}}
{{/* v0.13.26: a CONTENT hash, not the chart version.

     Takes (dict "ctx" . "builder" "<userData template name>") and returns
     sha256 of that builder's rendered userData, with this very value replaced
     by a sentinel during the hashing pass. That sentinel is what breaks the
     "hash includes hash" recursion which v0.4.2 had sidestepped by using
     .Chart.Version -- and using the chart version is why the bridge had to
     refuse to mirror the published hash at all, since every bump then looked
     like drift and would have reimaged every blade. See scripts/
     ironic-node-extra-bridge.py and docs/IMPLEMENTATION.md §9.8.

     Consequence worth stating plainly: the hash now tracks the configdrive,
     so a chart bump that changes no userData produces the SAME hash and
     fires nothing, while a genuine userData change fires a redeploy. That is
     the feature working as designed rather than shipped inert.

     The builder is passed explicitly rather than inferred because the three
     node classes render different userData and must hash their own. */}}
{{- define "kubernetes-cluster.deployedConfigDriveHash" -}}
{{- $ctx := .ctx -}}
{{- if $ctx.Values.__cdHashSentinel -}}
__CONFIGDRIVE_HASH_SENTINEL__
{{- else -}}
{{- $vals := merge (dict "__cdHashSentinel" true) (deepCopy $ctx.Values) -}}
{{- $probe := dict "Values" $vals "Chart" $ctx.Chart "Release" $ctx.Release "Capabilities" $ctx.Capabilities "Template" $ctx.Template "Files" $ctx.Files -}}
{{- include .builder $probe | sha256sum | trunc 16 -}}
{{- end -}}
{{- end -}}

{{/* Stable per-cluster bootstrap token. Format kubeadm requires:
     <id>.<secret>, [a-z0-9]{6}\.[a-z0-9]{16}. Derived from the cluster
     name via sha256 so it's deterministic per cluster (re-renders give
     the same token; replicas + workers always know it). */}}
{{- define "kubernetes-cluster.bootstrapToken" -}}
{{- $hash := sha256sum (printf "kubeadm-token-%s" .Values.clusterName) -}}
{{- printf "%s.%s" (substr 0 6 $hash) (substr 6 22 $hash) -}}
{{- end -}}

{{/* Stable certificate-key (32-byte hex) for kubeadm-certs Secret
     encryption. Same derivation pattern as bootstrapToken. */}}
{{- define "kubernetes-cluster.kubeadmCertKey" -}}
{{- sha256sum (printf "kubeadm-cert-key-%s" .Values.clusterName) | trunc 64 -}}
{{- end -}}

{{/* PKI bundle: returns YAML dict with all kubeadm CAs and SA keys.
     Reused via lookup-then-genCA: on first render, generates fresh
     PKI; on subsequent renders, reuses what's already in the Secret. */}}
{{- define "kubernetes-cluster.kubeadmPkiBundle" -}}
{{- $secretName := include "kubernetes-cluster.kubeadmPkiSecretName" . -}}
{{- $existing := lookup "v1" "Secret" .Release.Namespace $secretName -}}
{{- $result := dict -}}
{{- /* Reuse the full bundle from the live Secret only when ALL fields
       are present (including v0.12.6's etcd-client.crt). If a key is
       missing — e.g. upgrading from a pre-v0.12.6 Secret — fall through
       to the fresh-CA path. Sprig genSignedCert needs a sprig.certificate
       struct (not a dict), so we can't partially regenerate; one-shot
       rotation is acceptable because BH auto-redeploy converges via
       configDriveHash drift detection. */ -}}
{{- if and $existing (index (default (dict) $existing.data) "etcd-client.crt") -}}
  {{- $data := default (dict) $existing.data -}}
  {{- $_ := set $result "caCert"            (b64dec (index $data "ca.crt")) -}}
  {{- $_ := set $result "caKey"             (b64dec (index $data "ca.key")) -}}
  {{- $_ := set $result "frontProxyCaCert"  (b64dec (index $data "front-proxy-ca.crt")) -}}
  {{- $_ := set $result "frontProxyCaKey"   (b64dec (index $data "front-proxy-ca.key")) -}}
  {{- $_ := set $result "etcdCaCert"        (b64dec (index $data "etcd-ca.crt")) -}}
  {{- $_ := set $result "etcdCaKey"         (b64dec (index $data "etcd-ca.key")) -}}
  {{- $_ := set $result "saKey"             (b64dec (index $data "sa.key")) -}}
  {{- $_ := set $result "etcdClientCert"    (b64dec (index $data "etcd-client.crt")) -}}
  {{- $_ := set $result "etcdClientKey"     (b64dec (index $data "etcd-client.key")) -}}
{{- else -}}
  {{- $ca       := genCA (printf "kubernetes-%s" .Values.clusterName) 3650 -}}
  {{- $fpCa     := genCA (printf "front-proxy-%s" .Values.clusterName) 3650 -}}
  {{- $etcdCa   := genCA (printf "etcd-%s" .Values.clusterName) 3650 -}}
  {{- /* kubeadm expects an RSA service-account signing key. Generate a
         throwaway leaf cert just to extract the key+pub the same way the
         etcd-PKI flow did. The cert itself is discarded. */ -}}
  {{- $saTmpCa  := genCA (printf "sa-tmp-%s" .Values.clusterName) 30 -}}
  {{- $saLeaf   := genSignedCert (printf "sa-%s" .Values.clusterName) (list) (list "sa") 3650 $saTmpCa -}}
  {{- /* v0.12.6: etcd-client cert signed by etcd-ca. Lets replica join.sh
         authenticate to the bootstrap etcd before kubeadm has placed
         apiserver-etcd-client.crt on disk (which only happens late in
         kubeadm join). Used by wait_for_etcd_no_pending_learner and
         remove_ghost_etcd_members. */ -}}
  {{- $etcdClient := genSignedCert (printf "etcd-client-%s" .Values.clusterName) (list) (list "etcd-client") 3650 $etcdCa -}}
  {{- $_ := set $result "caCert"            $ca.Cert -}}
  {{- $_ := set $result "caKey"             $ca.Key -}}
  {{- $_ := set $result "frontProxyCaCert"  $fpCa.Cert -}}
  {{- $_ := set $result "frontProxyCaKey"   $fpCa.Key -}}
  {{- $_ := set $result "etcdCaCert"        $etcdCa.Cert -}}
  {{- $_ := set $result "etcdCaKey"         $etcdCa.Key -}}
  {{- $_ := set $result "saKey"             $saLeaf.Key -}}
  {{- $_ := set $result "etcdClientCert"    $etcdClient.Cert -}}
  {{- $_ := set $result "etcdClientKey"     $etcdClient.Key -}}
{{- end -}}
{{- $result | toYaml -}}
{{- end -}}

{{/* Returns "yes" when the kubeadm-pki Secret exists in the live
     cluster with CA + front-proxy + etcd-CA + SA key populated.
     2-pass-apply gate: first render creates the Secret, second
     reconcile (~60s) sees it and renders the BHs. */}}
{{- define "kubernetes-cluster.kubeadmPkiReady" -}}
{{- $secretName := include "kubernetes-cluster.kubeadmPkiSecretName" . -}}
{{- $existing := lookup "v1" "Secret" .Release.Namespace $secretName -}}
{{- if $existing -}}
{{- $data := default (dict) $existing.data -}}
{{- if and (index $data "ca.crt") (index $data "front-proxy-ca.crt") (index $data "etcd-ca.crt") (index $data "sa.key") -}}yes{{- end -}}
{{- end -}}
{{- end -}}

{{/* Apiserver endpoint that replicas + workers point their kubeadm
     join command at. For HA (cpNodes > 1): controlPlane.endpoint VIP.
     For single-CP: bootstrap CP's apiserver — but we don't know its IP
     at chart render time when it's DHCP-assigned. Solution: replicas
     and workers are gated to NOT render for single-CP (len cpNodes ==
     1 means there are no replicas), and single-CP workers wait for the
     bootstrap CP's IP via the same publish mechanism that drives
     workload-kubeconfig delivery. */}}
{{- define "kubernetes-cluster.kubeadmJoinEndpoint" -}}
{{- $cp := default (dict) .Values.controlPlane -}}
{{- if $cp.endpoint -}}{{- $cp.endpoint -}}{{- end -}}
{{- end -}}

{{/* v0.12.0: kubeadm PKI write_files. Renders the chart-managed CAs
     and SA key into /etc/kubernetes/pki/ on every CP. With these in
     place, `kubeadm init` skips its certs phase (would error if certs
     already exist), `kubeadm join --control-plane` doesn't need
     --certificate-key or --upload-certs (no kubeadm-certs Secret
     download), and all CPs share the same CA so the discovery-token-
     ca-cert-hash matches.

     Workers don't need PKI pre-placement — kubeadm join (without
     --control-plane) only needs the discovery token + CA hash, which
     it verifies against the apiserver's TLS cert at join time. */}}
{{- define "kubernetes-cluster.kubeadmPkiWriteFiles" -}}
{{- if not (include "kubernetes-cluster.kubeadmPkiReady" .) -}}{{- /* 2-pass gate */ -}}
{{- else -}}
{{- $bundle := (include "kubernetes-cluster.kubeadmPkiBundle" . | fromYaml) -}}
{{- /* v0.12.5: PKI persisted to /etc/kubernetes/pki-chart/ (backup
       location). cp-init.sh and replica join.sh copy from -chart/ to
       /etc/kubernetes/pki/ before kubeadm operations. `kubeadm reset`
       wipes pki/ but doesn't touch pki-chart/ — so retries after a
       failed join survive with the chart-baked CAs intact. */ -}}
- path: /etc/kubernetes/pki-chart/ca.crt
  permissions: "0644"
  content: |
{{ $bundle.caCert | indent 4 }}
- path: /etc/kubernetes/pki-chart/ca.key
  permissions: "0600"
  content: |
{{ $bundle.caKey | indent 4 }}
- path: /etc/kubernetes/pki-chart/front-proxy-ca.crt
  permissions: "0644"
  content: |
{{ $bundle.frontProxyCaCert | indent 4 }}
- path: /etc/kubernetes/pki-chart/front-proxy-ca.key
  permissions: "0600"
  content: |
{{ $bundle.frontProxyCaKey | indent 4 }}
- path: /etc/kubernetes/pki-chart/etcd-ca.crt
  permissions: "0644"
  content: |
{{ $bundle.etcdCaCert | indent 4 }}
- path: /etc/kubernetes/pki-chart/etcd-ca.key
  permissions: "0600"
  content: |
{{ $bundle.etcdCaKey | indent 4 }}
- path: /etc/kubernetes/pki-chart/sa.key
  permissions: "0600"
  content: |
{{ $bundle.saKey | indent 4 }}
{{- /* v0.12.6: etcd-client cert/key. Stays in pki-chart/ — used directly
       by replica join.sh's etcdctl calls so it doesn't have to wait for
       kubeadm to materialize apiserver-etcd-client.crt under pki/. */}}
- path: /etc/kubernetes/pki-chart/etcd-client.crt
  permissions: "0644"
  content: |
{{ $bundle.etcdClientCert | indent 4 }}
- path: /etc/kubernetes/pki-chart/etcd-client.key
  permissions: "0600"
  content: |
{{ $bundle.etcdClientKey | indent 4 }}
- path: /usr/local/bin/restore-chart-pki.sh
  permissions: "0755"
  content: |
    #!/bin/bash
    # v0.12.5: restore chart-baked PKI from the backup location to where
    # kubeadm expects it. Idempotent — run before every kubeadm op so
    # `kubeadm reset` -> retry cycles don't end up with empty pki/.
    set -euo pipefail
    mkdir -p /etc/kubernetes/pki/etcd
    cp /etc/kubernetes/pki-chart/ca.crt              /etc/kubernetes/pki/ca.crt
    cp /etc/kubernetes/pki-chart/ca.key              /etc/kubernetes/pki/ca.key
    cp /etc/kubernetes/pki-chart/front-proxy-ca.crt  /etc/kubernetes/pki/front-proxy-ca.crt
    cp /etc/kubernetes/pki-chart/front-proxy-ca.key  /etc/kubernetes/pki/front-proxy-ca.key
    cp /etc/kubernetes/pki-chart/etcd-ca.crt         /etc/kubernetes/pki/etcd/ca.crt
    cp /etc/kubernetes/pki-chart/etcd-ca.key         /etc/kubernetes/pki/etcd/ca.key
    cp /etc/kubernetes/pki-chart/sa.key              /etc/kubernetes/pki/sa.key
    openssl rsa -in /etc/kubernetes/pki/sa.key -pubout \
      > /etc/kubernetes/pki/sa.pub 2>/dev/null
    chmod 0644 /etc/kubernetes/pki/sa.pub
    chmod 0600 /etc/kubernetes/pki/ca.key /etc/kubernetes/pki/front-proxy-ca.key \
               /etc/kubernetes/pki/etcd/ca.key /etc/kubernetes/pki/sa.key
{{- end -}}
{{- end -}}

{{/* Just the cluster CA cert, for workers. Workers don't need the full
     kubeadm PKI but they DO need the CA to compute the discovery-token-
     ca-cert-hash at runtime (cleaner than --unsafe-skip-ca-verification). */}}
{{- define "kubernetes-cluster.kubeadmCaWriteFiles" -}}
{{- if not (include "kubernetes-cluster.kubeadmPkiReady" .) -}}{{- /* gate */ -}}
{{- else -}}
{{- $bundle := (include "kubernetes-cluster.kubeadmPkiBundle" . | fromYaml) -}}
- path: /etc/kubernetes/pki/ca.crt
  permissions: "0644"
  content: |
{{ $bundle.caCert | indent 4 }}
{{- end -}}
{{- end -}}

{{/* kubeadm-init.yaml template for the bootstrap CP. Stacked etcd
     (kubeadm-managed). __ADVERTISE_IP__ + __CERT_KEY__ are sed-
     substituted at runtime in cp-init.sh from the chart-derived
     bootstrap token / cert-key (both stable per cluster — see
     kubeadm.bootstrapToken and kubeadm.kubeadmCertKey helpers).

     bootstrapTokens has the chart-known token pre-baked so replicas
     and workers can use it for discovery without runtime publish. */}}
{{- define "kubernetes-cluster.kubeadmInitWriteFiles" -}}
{{- if not (include "kubernetes-cluster.kubeadmPkiReady" .) -}}{{- /* gate */ -}}
{{- else -}}
{{- $cpe := include "kubernetes-cluster.controlPlaneEndpoint" . -}}
{{- $tok := include "kubernetes-cluster.bootstrapToken" . -}}
- path: /etc/kubernetes/kubeadm-init.yaml.template
  permissions: "0644"
  content: |
    apiVersion: kubeadm.k8s.io/v1beta4
    kind: InitConfiguration
    bootstrapTokens:
      - token: {{ $tok | quote }}
        ttl: 24h
        usages: [signing, authentication]
        groups: [system:bootstrappers:kubeadm:default-node-token]
    localAPIEndpoint:
      advertiseAddress: __ADVERTISE_IP__
      bindPort: 6443
    certificateKey: __CERT_KEY__
    {{/* v0.13.20: pin the kubelet's node-ip.
         Without this the init config has NO nodeRegistration at all, so
         kubelet auto-detects its node IP and on a dual-NIC blade picks the
         FIRST interface — the OOB network — while everything else the chart
         configures sits on the data network. Measured on blade01:

           advertiseAddress:  192.168.0.201   (data)
           etcd peer URL:     192.168.0.201   (data)
           derived VIP:       192.168.0.250   (data, enp2s0)
           node InternalIP:   192.0.2.123   (OOB)  <- the odd one out
           KUBELET_KUBEADM_ARGS=""                   <- no --node-ip anywhere

         The CP REPLICAS were already correct because their JoinConfiguration
         carries nodeRegistration.kubeletExtraArgs; only the bootstrap and the
         workers lacked it, which is why blade02/03 reported data-network
         addresses and blade01 and the workers did not. __ADVERTISE_IP__ is
         already substituted by the same sed that fills advertiseAddress. */}}
    nodeRegistration:
      kubeletExtraArgs:
        - name: node-ip
          value: __ADVERTISE_IP__
    ---
    apiVersion: kubeadm.k8s.io/v1beta4
    kind: ClusterConfiguration
    kubernetesVersion: {{ .Values.k8sVersion }}
{{- if $cpe }}
    controlPlaneEndpoint: {{ $cpe | quote }}
{{- else if gt (len (fromYamlArray (include "kubernetes-cluster.cpNodes" .))) 1 }}
    {{/* v0.13.10: HA with no declared endpoint. $cpe is EMPTY at render time
         by construction -- the VIP is derived by arping after this node boots
         -- so `if $cpe` omitted this line entirely and kubeadm init ran with
         no controlPlaneEndpoint. kubeadm only accepts `--control-plane` joins
         when the endpoint was set AT INIT TIME, so the cluster was permanently
         single-CP: controlPlaneEndpoint empty in kubeadm-config, and the VIP
         absent from the apiserver cert SANs (measured: SANs held only
         10.96.0.1 and the node's own 192.168.0.201). Replicas then failed for
         a second, louder reason and this one stayed hidden underneath.
         Substituted by cp-init.sh from the derived VIP, exactly as
         __ADVERTISE_IP__ and __CERT_KEY__ already are. */}}
    controlPlaneEndpoint: __CONTROL_PLANE_ENDPOINT__
{{- end }}
    networking:
      podSubnet: {{ .Values.network.podCIDR | quote }}
      serviceSubnet: {{ .Values.network.serviceCIDR | quote }}
{{- end -}}
{{- end -}}

{{/* v0.12.8: kubeadm-join.yaml.template for replica CPs (HA only).
     Without a JoinConfiguration file, `kubeadm join --control-plane`
     uses the node's default-route IP for apiserver advertise AND etcd
     peer URL. On dual-NIC blades where the default route is via the
     management network (192.168.0.x) but the bootstrap CP advertised
     etcd on the data network (192.0.2.x via VIP_PREFIX), peers can
     never reach each other → etcd member never started → kubeadm join
     fails. Replica join.sh sed-substitutes ADVERTISE_IP / CA_HASH /
     CERT_KEY at runtime, computed via the same VIP_PREFIX trick the
     bootstrap CP uses. */}}
{{- define "kubernetes-cluster.kubeadmJoinWriteFiles" -}}
{{- if not (include "kubernetes-cluster.kubeadmPkiReady" .) -}}{{- /* gate */ -}}
{{- else -}}
{{- $cpe := include "kubernetes-cluster.controlPlaneEndpoint" . -}}
{{- $tok := include "kubernetes-cluster.bootstrapToken" . -}}
- path: /etc/kubernetes/kubeadm-join.yaml.template
  permissions: "0644"
  content: |
    apiVersion: kubeadm.k8s.io/v1beta4
    kind: JoinConfiguration
    discovery:
      bootstrapToken:
        token: {{ $tok | quote }}
        {{/* v0.13.19: a PLACEHOLDER, substituted by join.sh from the
             runtime-resolved endpoint. It was `$cpe` -- the render-time
             controlPlaneEndpoint helper -- which is empty for every HA render
             because the VIP is derived by arping after the bootstrap boots. So
             this rendered `apiServerEndpoint: ""` and kubeadm refused the
             config outright:
               discovery.bootstrapToken: Required value: APIServerEndpoint is not set
               apiServerEndpoint: Invalid value: "": missing port in address
             join.sh had already resolved the VIP correctly for its healthz
             poll; the value simply never reached this file. Third surface of
             the same render-time/runtime split, after join.sh's own VIP and
             kubeadm-init.yaml's controlPlaneEndpoint. */}}
        apiServerEndpoint: __CP_ENDPOINT__
        caCertHashes:
          - "sha256:__CA_HASH__"
    controlPlane:
      localAPIEndpoint:
        advertiseAddress: __ADVERTISE_IP__
        bindPort: 6443
      certificateKey: __CERT_KEY__
    nodeRegistration:
      kubeletExtraArgs:
        - name: node-ip
          value: __ADVERTISE_IP__
{{- end -}}
{{- end -}}

{{/* v0.13.9: resolve the control-plane endpoint at RUNTIME from Ironic,
     the store the bootstrap CP itself publishes to.

     WHY THIS EXISTS. The VIP cannot be known at render time. The bootstrap
     derives it by arping for a free address AFTER it boots, so every replica
     and worker renders CONCURRENTLY with a bootstrap that has not run yet.
     The etcd_ip poll fifteen hundred lines down already says this in full --
     "a render-time lookup would therefore bake an empty endpoint" -- and the
     VIP was the one value still baked. It baked empty, and three replicas ran
     `curl -k https:///healthz` 120 times against a URL with no host, logged
     "apiserver never became reachable", and exited 1 while Ironic reported
     `active` for all six blades.

     WHY IRONIC AND NOT THE CR. The sibling poll reads the management cluster's
     copy of this value, which arrives there only if ironic-extra-bridge happens
     to mirror the key. It did not: its EXTRA_KEYS allowlist predates HA, and
     rdc then replays CR.spec.extra onto Ironic as the whole `extra` object, so
     any key the bridge omits is DELETED from Ironic about ninety seconds after
     the blade publishes it. The VIP's measured lifetime was 82 seconds. The
     allowlist is fixed, but reading Ironic directly takes that entire lossy hop
     off the join's critical path: this reads what the publisher wrote, from the
     store it wrote it to, over the OOB network the blade already uses for
     exactly this purpose.

     Credentials and reachability are the same .Values.ironicAuth that
     publish-deployed-hash.sh uses, and that helper is already universal across
     cpUserData, cpReplicaUserData and workerUserData -- so nothing new is
     granted to the blade here.

     Prints the endpoint on stdout and nothing else, or exits non-zero. Callers
     fall back to the rendered value, which is correct and non-empty for a
     single-CP cluster, where no VIP is derived and none is published. */}}
{{- define "kubernetes-cluster.resolveNodeExtraWriteFiles" -}}
- path: /etc/kubernetes/resolve-node-extra.sh
  permissions: "0755"
  content: |
    #!/bin/bash
    # Poll Ironic for a key of the bootstrap CP's published node.extra.
    #   $1 = key name (default cp_endpoint)
    # stdout is the endpoint ONLY -- all logging goes to the log file, so
    # `VIP=$(bash resolve-node-extra.sh cp_endpoint)` can never capture chatter.
    set -uo pipefail
    exec 2>> /var/log/resolve-node-extra.log
    KEY="${1:-cp_endpoint}"
    BOOTSTRAP={{ include "kubernetes-cluster.cpNodeName" . | quote }}
    AUTH_URL={{ .Values.ironicAuth.authUrl | quote }}
    IRONIC_URL={{ .Values.ironicAuth.ironicUrl | quote }}
    USERNAME={{ .Values.ironicAuth.username | quote }}
    PASSWORD={{ .Values.ironicAuth.password | quote }}
    USER_DOMAIN={{ .Values.ironicAuth.userDomain | quote }}
    BUDGET={{ (.Values.join).cpEndpointPolls | default 120 }}
    log() { echo "[$(date -Iseconds)] $*" >&2; }
    log "resolving $KEY for bootstrap=$BOOTSTRAP budget=${BUDGET}x5s"
    for i in $(seq 1 "$BUDGET"); do
      TOKEN=$(curl -sS -D - -o /dev/null --max-time 15 \
        -H "Content-Type: application/json" \
        -d "{\"auth\":{\"identity\":{\"methods\":[\"password\"],\"password\":{\"user\":{\"name\":\"$USERNAME\",\"password\":\"$PASSWORD\",\"domain\":{\"name\":\"$USER_DOMAIN\"}}}},\"scope\":{\"system\":{\"all\":true}}}}" \
        "$AUTH_URL/v3/auth/tokens" 2>/dev/null \
        | grep -i 'x-subject-token' | awk '{print $2}' | tr -d '\r')
      if [ -n "$TOKEN" ]; then
        EP=$(curl -sS --max-time 15 \
          -H "X-Auth-Token: $TOKEN" \
          -H "X-OpenStack-Ironic-API-Version: 1.81" \
          "$IRONIC_URL/v1/nodes/$BOOTSTRAP" 2>/dev/null \
          | python3 -c "import sys,json; print((json.load(sys.stdin).get('extra') or {}).get(sys.argv[1]) or '')" "$KEY" 2>/dev/null)
        if [ -n "$EP" ]; then
          log "resolved $KEY=$EP on poll $i"
          printf '%s' "$EP"
          exit 0
        fi
        # Say so when the call SUCCEEDED but the key was not there. Without
        # this the loop is indistinguishable from "not published yet", which
        # is how a KeyError in this very pipeline burned a whole deploy cycle
        # looking like a patient wait.
        if [ -z "$EP" ] && { [ "$i" = "1" ] || [ $((i % 20)) = "0" ]; }; then
          log "poll $i: token ok, but extra.$KEY not present in the response"
        fi
      else
        log "poll $i: no keystone token"
      fi
      sleep 5
    done
    # Name what was being waited for. A gate that times out silently is
    # how the previous failure stayed invisible for half an hour.
    log "TIMEOUT: bootstrap $BOOTSTRAP never published extra.$KEY"
    exit 1
{{- end -}}

{{/* v0.12.3 (Task #91): publish-deployed-hash.sh + /etc/kubernetes/
     .deployed-hash file. Universal across cpUserData, cpReplicaUserData,
     workerUserData. */}}
{{/* CNCF conformance runner, baked into the bootstrap control plane.

     Why this is in the chart at all: the 443/443 v1.36.6 result in
     conformance/v1.36/ was produced by hydrophone installed BY HAND on blade01,
     and a re-image silently takes it away. The runner being absent is not
     discovered until someone wants to re-certify, which is exactly when they
     least want to rediscover the install steps. So the chart installs it.

     The SUITE is not run here by default. It takes about two hours and pulls a
     large image, so `runOnBoot` is off and an operator starts it deliberately:

       sudo env KUBECONFIG=/etc/kubernetes/admin.conf \
         hydrophone --conformance --output-dir /var/lib/conformance

     Two deliberate choices in the script below:
       * `curl --fail`. A plain `curl -sS` exits 0 on an HTTP 404 and would
         hand tar an HTML error page. This repo has already shipped one bug of
         exactly that shape (the teardown hook's POST, removed in
         baremetal-host 0.6.2), so every download here fails loudly.
       * the release checksums file is fetched and verified. An unverified
         tarball extracted into /usr/local/bin is a straight code-execution
         path, and `sha256sum -c` costs one line. */}}
{{- define "kubernetes-cluster.conformanceWriteFiles" -}}
{{- $c := default (dict) .Values.conformance -}}
{{- $ver := $c.version | default "v0.8.0" -}}
{{- $out := $c.outputDir | default "/var/lib/conformance" -}}
- path: /etc/kubernetes/install-conformance.sh
  permissions: "0755"
  content: |
    #!/bin/bash
    # Installs the CNCF conformance runner. Runs LAST in runcmd and never
    # gates cluster bring-up: if this fails the cluster is still correct,
    # only un-certifiable until someone looks at the log.
    set -euo pipefail
    exec >> /var/log/install-conformance.log 2>&1
    set -x
    date
    VER={{ $ver | quote }}
    OUT={{ $out | quote }}
    BASE="https://github.com/kubernetes-sigs/hydrophone/releases/download/${VER}"
    TAR="hydrophone_Linux_x86_64.tar.gz"
    # The checksum asset is NOT "checksums.txt" -- it is name_version_checksums.txt
    # with the leading "v" stripped from the tag. Verified against all nine
    # hydrophone releases (v0.1.0..v0.8.0), so this is the project's pattern and
    # not a quirk of one tag. Held in a variable precisely so the URL and the
    # error message below cannot drift apart again: the old message named
    # "checksums.txt" and was perfectly accurate about a file that never existed.
    SUMS="hydrophone_${VER#v}_checksums.txt"

    # One EXIT trap, set once, doing both jobs. Bash keeps a SINGLE EXIT trap,
    # so the `trap 'rm -rf "$WORK"' EXIT` that used to live inside the else
    # branch would silently REPLACE any trap set out here -- which is how a
    # failure marker added later would quietly never be written.
    WORK=""
    # Distinguishes the two ways this script can fail. With runOnBoot the suite
    # itself runs here, so "nonzero exit" alone cannot tell "no binary" from
    # "binary fine, suite failed" -- and a marker claiming hydrophone is absent
    # when it is sitting on the node sends the reader to the wrong problem.
    INSTALLED=0
    mkdir -p "$OUT"
    cleanup() {
      rc=$?
      if [ -n "$WORK" ]; then rm -rf "$WORK"; fi
      if [ "$rc" -ne 0 ]; then
        # Without this, a failed install leaves NOTHING in $OUT -- measured
        # 2026-11-04: the directory was absent entirely, so the one place
        # anyone looks was indistinguishable from "the runner was never
        # enabled". Bring-up stays ungated; the failure stops being silent.
        {
          if [ "$INSTALLED" -eq 1 ]; then
            echo "conformance SUITE failed (exit $rc) at $(date -Is)"
            echo "hydrophone IS installed on this node; the run did not complete."
          else
            echo "conformance runner INSTALL failed (exit $rc) at $(date -Is)"
            echo "Nothing was installed. hydrophone is NOT on this node."
          fi
          echo "Full trace: /var/log/install-conformance.log"
        } > "$OUT/INSTALL-FAILED.txt"
      else
        # Clear a stale marker from an earlier failed boot.
        rm -f "$OUT/INSTALL-FAILED.txt"
      fi
    }
    trap cleanup EXIT

    # `hydrophone --version` prints a MULTI-LINE build block whose first line is
    # just the program name, so the old `| head -1` recorded "hydrophone" and no
    # version at all -- a log line whose entire purpose is saying what got
    # installed, saying nothing. The version is on the `version:` row.
    hydro_ver() { hydrophone --version 2>&1 | awk '$1=="version:"{print $2; exit}'; }

    if command -v hydrophone >/dev/null 2>&1; then
      echo "hydrophone already present: $(hydro_ver) (chart requested $VER)"
    else
      WORK=$(mktemp -d)
      # --fail so an HTTP error is an error, not an HTML file fed to tar.
      curl --fail -sSL -o "$WORK/$TAR"        "$BASE/$TAR"
      curl --fail -sSL -o "$WORK/sums"        "$BASE/$SUMS" || {
        echo "FATAL: cannot fetch $SUMS for $VER; refusing to install unverified binary" >&2
        exit 1
      }
      ( cd "$WORK" && grep " ${TAR}\$" sums > sums.one && sha256sum -c sums.one )
      tar -xzf "$WORK/$TAR" -C /usr/local/bin hydrophone
      chmod 0755 /usr/local/bin/hydrophone
      # Assert rather than assume: a tarball whose layout changed would
      # otherwise leave nothing on PATH and report success.
      command -v hydrophone >/dev/null || { echo "FATAL: hydrophone not on PATH after extract" >&2; exit 1; }
      echo "installed: $(hydro_ver) (chart requested $VER)"
    fi
    INSTALLED=1

    {{- if $c.runOnBoot }}
    # runOnBoot is ON. ~2h. Results land in $OUT.
    env KUBECONFIG=/etc/kubernetes/admin.conf \
      hydrophone --conformance --output-dir "$OUT"
    {{- else }}
    cat > "$OUT/HOW-TO-RUN.txt" <<'EOT'
    Run the CNCF conformance suite from this node (the kubeconfig never
    leaves the machine that owns it):

      sudo env KUBECONFIG=/etc/kubernetes/admin.conf \
        hydrophone --conformance --output-dir {{ $out }}

    Takes roughly two hours. Produces e2e.log and junit_01.xml here.
    Both are what CNCF wants attached to a submission.
    EOT
    # Append the RESOLVED version. The heredoc above is quoted on purpose, so
    # this goes in afterwards: whoever opens this file to re-certify needs to
    # know which runner is on the node, not which one the chart asked for.
    echo "Runner on this node: hydrophone $(hydro_ver) (chart requested $VER)" >> "$OUT/HOW-TO-RUN.txt"
    echo "runner installed; suite NOT run (conformance.runOnBoot is false)"
    {{- end }}
{{- end -}}

{{- define "kubernetes-cluster.publishDeployedHashWriteFiles" -}}
{{- $ctx := .ctx -}}
- path: /etc/kubernetes/.deployed-hash
  permissions: "0644"
  content: |
    {{ include "kubernetes-cluster.deployedConfigDriveHash" (dict "ctx" $ctx "builder" .builder) }}
- path: /etc/kubernetes/publish-deployed-hash.sh
  permissions: "0755"
  content: |
    #!/bin/bash
    # Publish the chart-baked deploy hash to Ironic.node.extra.
    # deployed_configdrive_hash. Bridge sidecar mirrors to BaremetalNode CR
    # spec.extra. baremetal-host chart's shouldAutoRedeploy compares
    # current chart-rendered hash vs spec.extra value. Match = OS is
    # in chart's intended state. Drift = teardown OR chart bump → fire
    # transition-undeploy.
    set -euo pipefail
    exec >> /var/log/publish-deployed-hash.log 2>&1
    set -x
    date
    HASH=$(cat /etc/kubernetes/.deployed-hash)
    test -n "$HASH"
    HN="$(hostname)"
    AUTH_URL={{ $ctx.Values.ironicAuth.authUrl | quote }}
    IRONIC_URL={{ $ctx.Values.ironicAuth.ironicUrl | quote }}
    USERNAME={{ $ctx.Values.ironicAuth.username | quote }}
    PASSWORD={{ $ctx.Values.ironicAuth.password | quote }}
    USER_DOMAIN={{ $ctx.Values.ironicAuth.userDomain | quote }}
    TOKEN=$(curl -sS -D - -o /dev/null \
      -H "Content-Type: application/json" \
      -d "{\"auth\":{\"identity\":{\"methods\":[\"password\"],\"password\":{\"user\":{\"name\":\"$USERNAME\",\"password\":\"$PASSWORD\",\"domain\":{\"name\":\"$USER_DOMAIN\"}}}},\"scope\":{\"system\":{\"all\":true}}}}" \
      "$AUTH_URL/v3/auth/tokens" \
      | grep -i 'x-subject-token' | awk '{print $2}' | tr -d '\r')
    test -n "$TOKEN"
    UUID=$(curl -sS -H "X-Auth-Token: $TOKEN" \
      -H "X-OpenStack-Ironic-API-Version: 1.81" \
      "$IRONIC_URL/v1/nodes/$HN" \
      | python3 -c "import sys,json; print(json.load(sys.stdin).get('uuid',''))")
    test -n "$UUID"
    curl -sS -X PATCH \
      -H "X-Auth-Token: $TOKEN" \
      -H "X-OpenStack-Ironic-API-Version: 1.81" \
      -H "Content-Type: application/json-patch+json" \
      -d "[{\"op\":\"add\",\"path\":\"/extra/deployed_configdrive_hash\",\"value\":\"$HASH\"}]" \
      "$IRONIC_URL/v1/nodes/$UUID"
    echo "published deployed_configdrive_hash=$HASH for $HN ($UUID)"
{{- end -}}

{{/* Cloud-init userData for the CONTROL PLANE node. Installs kubeadm,
     runs `kubeadm init` (with --control-plane-endpoint when set),
     applies the CNI, then mints a join token and publishes it onto the
     CP's own BaremetalNode CR via `kubectl patch` against the management cluster.
     The patch path is spec.extra.kubeadm_join — chosen because spec.extra
     is a writable, KOG-reconciled field in the BaremetalNode CRD (Ironic round-
     trip is handled by the rest-dynamic-controller; we don't fight it).

     Token rotation: kubeadm-token-refresh.timer fires every 12h and re-
     runs publish-join.sh, so the published kubeadm_join stays valid past
     the 24h kubeadm-default TTL. Workers added beyond the first day pick
     up a fresh token on the cdc reconcile that follows the timer fire.

     The management-cluster API URL, CA bundle, and SA token are baked
     into the userData at render time via .Values.managementCluster.
     CA bundle is sourced via kubernetes-cluster.mgmtCaBundle (looks up
     kube-root-ca.crt ConfigMap first; falls back to caBundle value).
     The chart's rbac.yaml template creates a scoped SA (`patch nodes`
     on this one node) and a long-lived token Secret, then the helper
     `kubernetes-cluster.cpToken` pulls the token via lookup. */}}
{{- define "kubernetes-cluster.cpUserData" -}}
#cloud-config
{{- $na := default (dict) .Values.nodeAccess }}
{{- $keys := default (list) $na.sshAuthorizedKeys }}
{{- $pw := default "" $na.password }}
{{- $pwAuth := and (default false $na.allowPasswordAuth) (ne $pw "") }}
{{- /* DEFAULT-SAFE. This block used to hardcode `ssh_pwauth: true`, an unlocked
       `ironic` user with `plain_text_passwd: baremetal`, no expiry and sudo --
       in all three user-data paths, with no value or schema field to override
       it. charts/kubernetes-cluster is a PUBLISHED Marketplace blueprint
       (0.14.0 reached the channel on 2026-11-07), so every installer provisioned
       blades with a known password and SSH password auth on.
       Password login is now opt-in: it needs BOTH nodeAccess.allowPasswordAuth
       and a non-empty nodeAccess.password. With neither set the account is
       locked and ssh_pwauth is false. */}}
ssh_pwauth: {{ $pwAuth }}
chpasswd:
  expire: false
users:
  - name: ironic
{{- if $pwAuth }}
    plain_text_passwd: {{ $pw | quote }}
    lock-passwd: false
{{- else }}
    lock-passwd: true
{{- end }}
{{- if $keys }}
    ssh_authorized_keys:
{{- range $keys }}
      - {{ . | quote }}
{{- end }}
{{- end }}
    shell: /bin/bash
    sudo:
      - "ALL=(ALL) NOPASSWD:ALL"
write_files:
  - path: /etc/kubernetes/install-k8s.sh
    permissions: "0755"
    content: |
      #!/bin/bash
      set -euo pipefail
      exec >> /var/log/install-k8s.log 2>&1
      set -x
      date
      # Debian generic cloud doesn't ship gnupg / apt-transport-https.
      apt-get update
      apt-get install -y --no-install-recommends ca-certificates curl apt-transport-https
      # `trusted=yes` skips OpenPGP signature verification on this repo.
      # Required because Debian 13 trixie's sqv rejects the k8s repo's
      # current Release.key as "Signature Packet v3 not considered secure
      # since 2026-02-01". The packages themselves still come from
      # pkgs.k8s.io over HTTPS; this only disables the signature check.
      echo "deb [trusted=yes] https://pkgs.k8s.io/core:/stable:/{{ regexReplaceAll "^(v[0-9]+\\.[0-9]+).*" .Values.k8sVersion "${1}" }}/deb/ /" > /etc/apt/sources.list.d/kubernetes.list
      # v0.13.17: containerd 2.x, from Docker's repo, for CONFORMANCE.
      #
      # Debian trixie offers containerd 1.7.24 and NOTHING ELSE
      # (apt-cache policy: Candidate: 1.7.24~ds1-6+deb13u1). The CNCF suite's
      #   [sig-node] ImageVolume ... (common/node/image_volume.go:119)
      # mounts an OCI image as a volume, which requires runtime support that
      # landed in the containerd 2.x line. On 1.7.24 the test pod never reaches
      # Running and the spec times out after five minutes. It is not a
      # configuration problem and no feature gate fixes it: the capability is
      # absent from the runtime, so this is a property of the BASE IMAGE rather
      # than of the chart's logic.
      #
      # download.docker.com is what the Kubernetes documentation prescribes and
      # what certified installers use, and `containerd.io` tracks the 2.x line
      # while staying apt-managed. Same `trusted=yes` rationale as the k8s repo
      # above: trixie's sqv rejects v3 signature packets, the transport is still
      # HTTPS, and this only skips the signature check.
      echo "deb [trusted=yes] https://download.docker.com/linux/debian trixie stable" > /etc/apt/sources.list.d/docker.list
      apt-get update
      # Resolve the full Debian package version including the upstream
      # revision suffix (older minors ship -1.1, v1.36+ ships -2.1, etc.)
      # by querying apt-cache rather than hard-coding `-1.1`.
      K8S_PKG_VER=$(apt-cache madison kubelet | awk -v v="{{ trimPrefix "v" .Values.k8sVersion }}" '$3 ~ "^"v"-" {print $3; exit}')
      test -n "$K8S_PKG_VER"
      apt-get install -y kubelet="$K8S_PKG_VER" kubeadm="$K8S_PKG_VER" kubectl="$K8S_PKG_VER" containerd.io etcd-client jq
      apt-mark hold kubelet kubeadm kubectl
      bash /etc/kubernetes/publish-stage.sh k8s-packages-installed
      # Debian 13 trixie's containerd ships with bin_dir = "/usr/lib/cni" by
      # default. Flannel's install-cni-plugin initContainer (and kubeadm)
      # drop CNI binaries at /opt/cni/bin instead, so kubelet errors with
      # `failed to find plugin "flannel" in path [/usr/lib/cni]` and coredns
      # never gets a sandbox. Override before starting containerd.
      mkdir -p /etc/containerd
      # Two patches to the default config:
      # 1. CNI bin_dir: Debian 13's containerd defaults to /usr/lib/cni;
      #    flannel + kubeadm install at /opt/cni/bin.
      # 2. SystemdCgroup: Debian 13's containerd defaults to false (i.e.
      #    cgroupfs), but kubelet defaults to cgroupDriver: systemd since
      #    k8s 1.22. The mismatch causes kubelet to see containers in
      #    unexpected cgroup paths and terminate them roughly every 11s
      #    after start — observed empirically as etcd /apiserver flapping
      #    14-28 times in a fresh cluster bringup. Aligning to systemd on
      #    both sides eliminates the flap.
      containerd config default \
        | sed -e 's|/usr/lib/cni|/opt/cni/bin|g' \
              -e 's|SystemdCgroup = false|SystemdCgroup = true|g' \
        > /etc/containerd/config.toml
      # v0.13.17: ASSERT the substitution landed, do not assume it.
      #
      # Both seds are string matches against whatever `containerd config
      # default` emits, and that output changed shape in containerd 2.x:
      # config `version = 3` with renamed plugin paths. If the SystemdCgroup
      # match silently misses, containerd runs cgroupfs while kubelet defaults
      # to systemd, and the mismatch kills containers roughly every 11 seconds
      # — observed as etcd and apiserver flapping 14-28 times in a fresh
      # bringup. That failure is expensive to diagnose and trivial to detect
      # here, so detect it here.
      grep -q 'SystemdCgroup = true' /etc/containerd/config.toml || {
        echo "containerd config: SystemdCgroup=true NOT set after patching." >&2
        echo "  `containerd config default` output shape probably changed" >&2
        echo "  (containerd 2.x uses config version 3). kubelet defaults to" >&2
        echo "  cgroupDriver: systemd, so proceeding would flap every ~11s." >&2
        echo "  containerd version: $(containerd --version 2>&1)" >&2
        grep -n 'SystemdCgroup' /etc/containerd/config.toml >&2 || echo "  no SystemdCgroup key at all" >&2
        exit 1
      }
      echo "containerd config: SystemdCgroup=true confirmed ($(containerd --version 2>&1 | awk '{print $3}'))"
      # systemctl enable --now doesn't restart a unit that's already
      # running; Debian's containerd apt-package enables+starts on
      # install, so without an explicit restart the new config.toml is
      # ignored and kubelet errors with `failed to find plugin "flannel"
      # in path [/usr/lib/cni]`.
      systemctl enable containerd
      systemctl restart containerd
      # Debian 13 trixie's default iptables alternative is iptables-nft. With
      # the bundled kube-proxy (v1.31, iptables mode), the proxy exits with
      # code 2 right after caches sync — no error logged, no kernel panic.
      # Pinning iptables-legacy avoids the silent crash. Track upstream
      # adoption of `--proxy-mode=nftables` for a longer-term path.
      update-alternatives --set iptables /usr/sbin/iptables-legacy
      update-alternatives --set ip6tables /usr/sbin/ip6tables-legacy
      modprobe overlay
      modprobe br_netfilter
      printf 'overlay\nbr_netfilter\n' > /etc/modules-load.d/k8s.conf
      printf 'net.bridge.bridge-nf-call-iptables = 1\nnet.bridge.bridge-nf-call-ip6tables = 1\nnet.ipv4.ip_forward = 1\n' > /etc/sysctl.d/99-kubernetes.conf
      sysctl --system
  - path: /etc/systemd/system/kubeadm-token-refresh.service
    permissions: "0644"
    content: |
      [Unit]
      Description=Mint a fresh kubeadm join token and publish it onto our BaremetalNode CR
      Wants=network-online.target
      After=network-online.target

      [Service]
      Type=oneshot
      ExecStart=/etc/kubernetes/publish-join.sh
  - path: /etc/systemd/system/kubeadm-token-refresh.timer
    permissions: "0644"
    content: |
      [Unit]
      Description=Refresh kubeadm join token every 12h (50%% margin over 24h TTL)
      Documentation=https://kubernetes.io/docs/reference/setup-tools/kubeadm/kubeadm-token/

      [Timer]
      # First fire 12h after init; thereafter every 12h. Persistent so
      # missed runs (blade reboot / clock skew) are caught on next boot.
      OnBootSec=12h
      OnUnitActiveSec=12h
      Persistent=true
      RandomizedDelaySec=10m

      [Install]
      WantedBy=timers.target
  - path: /etc/kubernetes/node-uuid
    permissions: "0644"
    content: |
      {{ include "kubernetes-cluster.bootstrapNodeUuid" . }}
  - path: /etc/kubernetes/publish-stage.sh
    permissions: "0755"
    content: |
      #!/bin/bash
      # Publish a single progress marker to Ironic node.extra.cp_stage.
      #
      # WHY THIS EXISTS. cp-init.sh is a long bash script that runs once, on
      # first boot, on a machine with no inbound access. Every `set -e` abort in
      # it is invisible from the cluster: Ironic still reports the node `active`
      # (it wrote the image successfully — the image is fine, the script failed),
      # spec.extra stays empty, and there is no way to distinguish "still
      # installing packages" from "died 40 lines in" without logging into the
      # blade. That cost hours here, and the only diagnostics available were
      # port probes, which cannot tell an uninstalled kubelet from an installed
      # one awaiting kubeadm (neither listens).
      #
      # Best effort by design: never let reporting failure break the thing being
      # reported on. Exits 0 always.
      #
      # v0.13.23: "best effort" is NOT "silent", and it used to be. Every exit
      # below was a bare `exit 0` with no output, and the PATCH sent its own
      # errors to /dev/null. So a reporter failure looked EXACTLY like the
      # thing it reports on failing -- an empty cp_stage -- and
      # docs/KUBERNETES-CLUSTER.md's troubleshooting row for "publishes
      # NOTHING, no cp_stage" names a different cause entirely (a NIC missing
      # from the node's ports). A healthy blade whose reporter had lost its
      # Keystone credential would have sent an operator hunting a network
      # fault that was not there.
      #
      # It now logs every failure path to /var/log/publish-stage.log and still
      # exits 0. `tail /var/log/publish-stage.log` on the blade distinguishes
      # "the reporter broke" from "cp-init died", which is the whole question.
      set +e
      LOG=/var/log/publish-stage.log
      log() { echo "[$(date -Iseconds)] $*" >> "$LOG" 2>/dev/null; }
      STAGE="${1:-unknown}"
      KEYSTONE_URL="{{ .Values.ironicAuth.authUrl }}"
      IRONIC_URL="{{ .Values.ironicAuth.ironicUrl }}"
      NODE_UUID="$(cat /etc/kubernetes/node-uuid 2>/dev/null)"
      if [ -z "$NODE_UUID" ]; then
        log "stage=$STAGE NOT PUBLISHED: /etc/kubernetes/node-uuid is absent or empty"
        exit 0
      fi
      AUTH_BODY='{"auth":{"identity":{"methods":["password"],"password":{"user":{"name":"{{ .Values.ironicAuth.username }}","password":"{{ .Values.ironicAuth.password }}","domain":{"name":"{{ .Values.ironicAuth.userDomain }}"}}}},"scope":{"system":{"all":true}}}}'
      OUT=$(curl -sS -i --max-time 10 -H "Content-Type: application/json" -d "$AUTH_BODY" "${KEYSTONE_URL}/v3/auth/tokens" 2>/dev/null)
      TOKEN=$(echo "$OUT" | grep -i '^X-Subject-Token:' | awk '{print $2}' | tr -d '\r')
      if [ -z "$TOKEN" ]; then
        # Name the status line rather than just "no token": a 401 is a wrong
        # credential, a timeout is an unreachable Keystone, and the operator
        # fix differs.
        log "stage=$STAGE NOT PUBLISHED: no Keystone token from $KEYSTONE_URL -- $(echo "$OUT" | head -1 | tr -d '\r')"
        exit 0
      fi
      ERR=$(curl -fsSL --max-time 10 \
        -H "X-Auth-Token: ${TOKEN}" \
        -H "X-OpenStack-Ironic-API-Version: 1.109" \
        -H "Content-Type: application/json-patch+json" \
        -X PATCH "${IRONIC_URL}/v1/nodes/${NODE_UUID}" \
        -d "[{\"op\":\"add\",\"path\":\"/extra/cp_stage\",\"value\":\"${STAGE}\"}]" 2>&1 >/dev/null)
      if [ $? -ne 0 ]; then
        log "stage=$STAGE NOT PUBLISHED: PATCH to $IRONIC_URL failed -- ${ERR:-no output}"
      else
        log "stage=$STAGE published"
      fi
      exit 0
  - path: /etc/kubernetes/publish-join.sh
    permissions: "0755"
    content: |
      #!/bin/bash
      # Mint a fresh kubeadm join token (24h TTL) and PATCH it into the
      # Ironic node's `extra.kubeadm_join`. The bare-metal blade can
      # reach the lab Ironic API over its OOB network, but CANNOT reach
      # the management cluster's API (wg tunnel routes one direction
      # only on this lab). The ironic-extra-bridge sidecar in
      # wg-ironic-proxy propagates extra back into the k8s BaremetalNode CR's
      # spec.extra, where the chart's lookup picks it up.
      #
      # Called once from cloud-init at boot, then every 12h by
      # kubeadm-token-refresh.timer (50%% margin over the 24h kubeadm
      # token TTL). Also publishes /etc/kubernetes/cert-key into
      # extra.cert_key when present (HA bootstrap CP).
      set -uo pipefail
      # Diagnostic trail: when the script fails, we need to know which step.
      # /var/log/publish-join.log is on disk; we virt-cat it from outside
      # the running VM. set -x dumps every command + expansion.
      exec >> /var/log/publish-join.log 2>&1
      set -x
      date
      log() { logger -t kubeadm-token-refresh "$*"; echo "[$(date -Iseconds)] $*"; }

      # Capture kubeadm-token-create stderr (was being swallowed with 2>/dev/null).
      log "running kubeadm token create"
      if ! JOIN_CMD=$(KUBECONFIG=/etc/kubernetes/admin.conf kubeadm token create --print-join-command --ttl 24h 2>/tmp/kubeadm-token.err); then
        log "kubeadm token create FAILED (rc=$?); err:"
        cat /tmp/kubeadm-token.err
        log "check /etc/kubernetes/admin.conf exists + kubeadm init succeeded"
        ls -la /etc/kubernetes/ 2>&1 || true
        exit 1
      fi
      log "kubeadm token create OK: ${JOIN_CMD:0:80}..."

      # Escape join command and (optional) cert-key into JSON-Patch ops.
      ESCAPED_JOIN=${JOIN_CMD//\"/\\\"}
      PATCH='[{"op":"add","path":"/extra/kubeadm_join","value":"'$ESCAPED_JOIN'"}'
      if [ -f /etc/kubernetes/cert-key ]; then
        CERT_KEY=$(cat /etc/kubernetes/cert-key)
        PATCH+=',{"op":"add","path":"/extra/cert_key","value":"'$CERT_KEY'"}'
      fi
      # v0.13.2: publish THIS node's own etcd address, DERIVED here rather
      # than declared in values.
      #
      # Replica CPs need the bootstrap's etcd client URL to add themselves as
      # members. That used to come from controlPlane.nodes[0].oobIp — an IP
      # typed into the CR by hand, which is a fact about one lab hard-coded
      # into the chart, wrong the moment a lease moves and impossible to
      # validate at render time. The node knows its own address; nobody else
      # reliably does.
      #
      # /etc/kubernetes/etcd-ip is written by cp-init.sh, which already
      # derives ADVERTISE_IP for exactly this reason. Reusing that value
      # keeps the etcd peer address and the apiserver advertise address
      # consistent by construction instead of by the operator typing the
      # same number into two places.
      if [ -f /etc/kubernetes/etcd-ip ]; then
        ETCD_IP=$(cat /etc/kubernetes/etcd-ip)
        PATCH+=',{"op":"add","path":"/extra/etcd_ip","value":"'$ETCD_IP'"}'
      fi
      # v0.13.2: publish the DERIVED control-plane endpoint, so the chart's
      # next render gives replicas and workers a VIP nobody had to type in.
      # Written by cp-init.sh only when the cluster is HA and no endpoint was
      # declared; absent otherwise, which leaves any declared value untouched.
      if [ -f /etc/kubernetes/cp-endpoint ]; then
        CP_ENDPOINT=$(cat /etc/kubernetes/cp-endpoint)
        PATCH+=',{"op":"add","path":"/extra/cp_endpoint","value":"'$CP_ENDPOINT'"}'
      fi
      PATCH+=']'

      KEYSTONE_URL="{{ .Values.ironicAuth.authUrl }}"
      IRONIC_URL="{{ .Values.ironicAuth.ironicUrl }}"
      NODE_UUID="{{ include "kubernetes-cluster.bootstrapNodeUuid" . }}"
      AUTH_BODY='{"auth":{"identity":{"methods":["password"],"password":{"user":{"name":"{{ .Values.ironicAuth.username }}","password":"{{ .Values.ironicAuth.password }}","domain":{"name":"{{ .Values.ironicAuth.userDomain }}"}}}},"scope":{"system":{"all":true}}}}'
      log "config: keystone=$KEYSTONE_URL ironic=$IRONIC_URL node=$NODE_UUID"

      for attempt in 1 2 3 4 5; do
        log "attempt $attempt: requesting Keystone token"
        KEYSTONE_OUT=$(curl -sS -i --max-time 15 -H "Content-Type: application/json" -d "$AUTH_BODY" "${KEYSTONE_URL}/v3/auth/tokens" 2>&1)
        echo "--- keystone response (attempt $attempt) ---"
        echo "$KEYSTONE_OUT" | head -30
        TOKEN=$(echo "$KEYSTONE_OUT" | grep -i '^X-Subject-Token:' | awk '{print $2}' | tr -d '\r')
        if [ -z "$TOKEN" ]; then
          log "keystone auth failed (attempt ${attempt}); retrying in $((attempt * 10))s"
          sleep $((attempt * 10))
          continue
        fi
        log "got token (len=${#TOKEN})"
        log "attempt $attempt: PATCH Ironic /v1/nodes/${NODE_UUID}/extra"
        if curl -fsSL --max-time 15 \
          -H "X-Auth-Token: ${TOKEN}" \
          -H "X-OpenStack-Ironic-API-Version: 1.109" \
          -H "Content-Type: application/json-patch+json" \
          --request PATCH \
          --data "$PATCH" \
          "${IRONIC_URL}/v1/nodes/${NODE_UUID}" > /tmp/publish-join.out 2>&1; then
          log "join command published to Ironic (attempt ${attempt})"
          exit 0
        fi
        log "publish attempt ${attempt} failed; sleeping $((attempt * 10))s"
        sleep $((attempt * 10))
      done
      log "publish failed after 5 attempts — see /tmp/publish-join.out"
      exit 1
  - path: /etc/kubernetes/write-kube-vip-manifest.sh
    permissions: "0755"
    content: |
      #!/bin/bash
      # Write the kube-vip static-pod manifest into /etc/kubernetes/manifests/.
      # Args:
      #   $1 = VIP (the control-plane endpoint IP)
      #   $2 = interface name kube-vip should bind the VIP on
      # Leader election runs against the apiserver itself — admin.conf is
      # mounted from the host (kubeadm produces it as part of init/join).
      # Until admin.conf exists, kubelet retries the pod; once it does,
      # kube-vip claims the VIP on the leader CP via ARP.
      set -euo pipefail
      VIP="${1:?missing VIP}"
      IFACE="${2:?missing interface}"
      mkdir -p /etc/kubernetes/manifests
      cat > /etc/kubernetes/manifests/kube-vip.yaml <<EOF
      apiVersion: v1
      kind: Pod
      metadata:
        name: kube-vip
        namespace: kube-system
      spec:
        hostNetwork: true
        hostAliases:
          - hostnames:
              - kubernetes
            ip: 127.0.0.1
        priorityClassName: system-node-critical
        containers:
          - name: kube-vip
            image: ghcr.io/kube-vip/kube-vip:v0.8.7
            imagePullPolicy: IfNotPresent
            args: ["manager"]
            securityContext:
              capabilities:
                add: ["NET_ADMIN", "NET_RAW"]
            env:
              - { name: vip_arp,              value: "true" }
              - { name: port,                 value: "6443" }
              - { name: vip_interface,        value: "$IFACE" }
              - { name: vip_cidr,             value: "32" }
              - { name: cp_enable,            value: "true" }
              - { name: cp_namespace,         value: "kube-system" }
              - { name: svc_enable,           value: "false" }
              - { name: vip_leaderelection,   value: "true" }
              - { name: vip_leaseduration,    value: "15" }
              - { name: vip_renewdeadline,    value: "10" }
              - { name: vip_retryperiod,      value: "2" }
              - { name: address,              value: "$VIP" }
            volumeMounts:
              - { name: kubeconfig, mountPath: /etc/kubernetes/admin.conf }
        volumes:
          - name: kubeconfig
            hostPath:
              path: /etc/kubernetes/admin.conf
              type: FileOrCreate
      EOF
      echo "wrote /etc/kubernetes/manifests/kube-vip.yaml VIP=$VIP IFACE=$IFACE"
  - path: /etc/kubernetes/cp-init.sh
    permissions: "0755"
    content: |
      #!/bin/bash
      # Bootstrap the control plane in ONE script with a single diagnostic
      # log, instead of scattering openssl/kubeadm/kubectl across runcmd
      # entries where any silent failure leaves us guessing at which
      # phase died.
      set -euo pipefail
      exec >> /var/log/cp-init.log 2>&1
      set -x
      date
      # Pin --apiserver-advertise-address to the default-route source IP
      # so kube-apiserver doesn't autodetect at startup. With dual-NIC
      # blades the autodetection occasionally races the route table and
      # binds to the wrong interface, which then has etcd advertising on
      # one IP and apiserver on another. publish-join.sh embeds the SAME
      # IP in the join command (kubeadm read it back from apiserver's
      # config), so workers and CP agree on the endpoint.
{{- $vip := include "kubernetes-cluster.controlPlaneVip" . }}
{{- if $vip }}
      # HA path: derive ADVERTISE_IP from the VIP's /24 instead of the
      # default-route source IP, because the VIP lives on the OOB network
      # while the default route is via the data network. We need the
      # apiserver bind AND the kube-vip claim to land on the SAME
      # interface — otherwise a worker connecting to VIP:6443 hits a CP
      # whose apiserver isn't listening on that NIC.
      VIP={{ $vip | quote }}
      VIP_PREFIX=$(echo "$VIP" | awk -F. '{print $1"."$2"."$3"."}')
      ADVERTISE_IP=$(ip -o -4 addr show \
        | awk -v p="$VIP_PREFIX" '$4 ~ "^"p {sub("/.*","",$4); print $4; exit}')
      VIP_IFACE=$(ip -o -4 addr show \
        | awk -v p="$VIP_PREFIX" '$4 ~ "^"p {print $2; exit}')
      echo "HA: VIP=$VIP ADVERTISE_IP=$ADVERTISE_IP VIP_IFACE=$VIP_IFACE"
      test -n "$ADVERTISE_IP"
      test -n "$VIP_IFACE"
      mkdir -p /etc/kubernetes/manifests
      /etc/kubernetes/write-kube-vip-manifest.sh "$VIP" "$VIP_IFACE"
{{- else }}
      ADVERTISE_IP=$(ip route get 8.8.8.8 2>/dev/null \
        | awk '/src/ {for (i=1;i<=NF;i++) if ($i=="src") print $(i+1)}' \
        | head -1)
      echo "single-CP: ADVERTISE_IP=$ADVERTISE_IP"
      test -n "$ADVERTISE_IP"
{{- end }}
      # Mint a fresh certificate-key locally. Stored at /etc/kubernetes/cert-key
      # so publish-join.sh picks it up and publishes alongside the join command.
      # Always done — even for single-CP — so promoting to HA later is a values
      # change, not an OS-level reconfiguration.
      # v0.12.2: cert-key MUST match what replicas use to decrypt the
      # kubeadm-certs Secret. Use the chart-deterministic value, NOT a
      # random one — otherwise replicas fail with "cipher: message
      # authentication failed" when downloading uploaded-certs.
      echo "{{ include "kubernetes-cluster.kubeadmCertKey" . }}" > /etc/kubernetes/cert-key
      chmod 0600 /etc/kubernetes/cert-key
      # v0.13.2: persist the DERIVED advertise address so publish-join.sh can
      # hand it to replica CPs as the bootstrap etcd endpoint. See the
      # etcd_ip block there for why this is derived and not an input.
      echo "$ADVERTISE_IP" > /etc/kubernetes/etcd-ip
      chmod 0644 /etc/kubernetes/etcd-ip
{{- if and (gt (len (fromYamlArray (include "kubernetes-cluster.cpNodes" .))) 1) (not (include "kubernetes-cluster.controlPlaneEndpoint" .)) }}
      # v0.13.2: DERIVE the control-plane VIP. HA cluster, no endpoint declared.
      #
      # Picked here rather than in values because this node can verify what the
      # chart cannot: that the address is inside the blades' own subnet (the
      # kube-vip interface lookup matches on /24, and silently finds no NIC for
      # an address outside it) and that nothing currently answers for it.
      #
      # Scans DOWN from the top of the subnet: DHCP pools conventionally
      # allocate upward from the bottom, so the top is the least contended end.
      # `arping -D` is duplicate-address-detection — the same check a DHCP
      # client does before accepting a lease.
      #
      # NOTE this is first-free-at-probe-time, not a reservation. If this
      # subnet has no DHCP-excluded range, a later lease could in principle
      # collide; kube-vip re-announces continuously so the VIP would win ARP,
      # but the honest fix is an excluded range, declared as a RANGE rather
      # than as one hand-picked address.
      apt-get install -y iputils-arping >/dev/null 2>&1 || true
      VIP_SUBNET=$(echo "$ADVERTISE_IP" | awk -F. '{print $1"."$2"."$3}')
      VIP_IFACE_DERIVE=$(ip -o -4 addr show \
        | awk -v p="${VIP_SUBNET}." '$4 ~ "^"p {print $2; exit}')
      DERIVED_VIP=""
      for last in $(seq 250 -1 200); do
        cand="${VIP_SUBNET}.${last}"
        [ "$cand" = "$ADVERTISE_IP" ] && continue
        if arping -D -q -c 2 -w 3 -I "$VIP_IFACE_DERIVE" "$cand" >/dev/null 2>&1; then
          DERIVED_VIP="$cand"
          echo "derived control-plane VIP $DERIVED_VIP on $VIP_IFACE_DERIVE (arping DAD found it free)"
          break
        fi
      done
      test -n "$DERIVED_VIP" \
        || { echo "no free address in ${VIP_SUBNET}.200-250 — abort rather than guess"; exit 1; }
      echo "$DERIVED_VIP:6443" > /etc/kubernetes/cp-endpoint
      bash /etc/kubernetes/publish-stage.sh vip-derived-$DERIVED_VIP
      chmod 0644 /etc/kubernetes/cp-endpoint
      /etc/kubernetes/write-kube-vip-manifest.sh "$DERIVED_VIP" "$VIP_IFACE_DERIVE"
{{- end }}
      # v0.12.5: restore PKI from /etc/kubernetes/pki-chart/ to where
      # kubeadm expects it. Survives kubeadm reset because pki-chart/
      # is untouched by reset.
      bash /usr/local/bin/restore-chart-pki.sh
      # v0.13.10: the control-plane endpoint is known only now, after the
      # arping derivation above wrote /etc/kubernetes/cp-endpoint. Without
      # this substitution kubeadm init runs with no controlPlaneEndpoint and
      # the cluster can never accept a --control-plane join.
      CP_ENDPOINT_SUB=""
      if [ -s /etc/kubernetes/cp-endpoint ]; then
        CP_ENDPOINT_SUB=$(cat /etc/kubernetes/cp-endpoint)
      fi
      sed -e "s|__ADVERTISE_IP__|$ADVERTISE_IP|g" \
          -e "s|__CERT_KEY__|$(cat /etc/kubernetes/cert-key)|g" \
          -e "s|__CONTROL_PLANE_ENDPOINT__|${CP_ENDPOINT_SUB}|g" \
        /etc/kubernetes/kubeadm-init.yaml.template \
        > /etc/kubernetes/kubeadm-init.yaml
      # Fail loudly rather than init a cluster that can never become HA: an
      # unsubstituted placeholder, or an endpoint line left empty, both mean
      # the derivation did not produce a VIP.
      if grep -q "__CONTROL_PLANE_ENDPOINT__" /etc/kubernetes/kubeadm-init.yaml \
         || grep -qE "^\s*controlPlaneEndpoint:\s*(\"\")?\s*$" /etc/kubernetes/kubeadm-init.yaml; then
        echo "controlPlaneEndpoint unresolved: HA cluster requires a VIP at init time, derivation produced none — abort"
        exit 1
      fi
      grep -E "controlPlaneEndpoint|advertiseAddress" /etc/kubernetes/kubeadm-init.yaml
      chmod 0600 /etc/kubernetes/kubeadm-init.yaml
      # Skip cert generation phases — chart-baked PKI is already on disk.
      # Kubeadm will detect existing CAs and only generate leaf certs.
      bash /etc/kubernetes/publish-stage.sh kubeadm-init-start
      kubeadm init --upload-certs \
        {{- if (.Values.cni).kubeProxyReplacement }}
        {{- /* Never install kube-proxy rather than install-then-delete:
               Cilium replaces it, and a kube-proxy that briefly owns the
               iptables rules then disappears leaves stale chains behind. */}}
        --skip-phases=addon/kube-proxy \
        {{- end }}
        --config /etc/kubernetes/kubeadm-init.yaml
      bash /etc/kubernetes/publish-stage.sh kubeadm-init-ok
      mkdir -p /root/.kube
      cp /etc/kubernetes/admin.conf /root/.kube/config
      # CNI install. Two paths, selected by .Values.cni.install.
      #
      # v0.12.4: retry either way — the apiserver may take a few seconds to
      # be ready right after `kubeadm init` completes. Without retry, a
      # single attempt at the wrong moment leaves the cluster CNI-less,
      # and all nodes (CP + workers) stay NotReady forever.
      export KUBECONFIG=/etc/kubernetes/admin.conf
      {{- if eq (.Values.cni.install | default "flannel") "cilium" }}
      # CILIUM — installed with HELM, not a manifest, because that is
      # Cilium's canonical install path. The single-file quick-install.yaml
      # is deprecated upstream and hard-codes choices we must vary (most
      # importantly ipam.mode), so applying it would mean shipping a CNI
      # configured for someone else's cluster.
      #
      # Debian trixie has no helm package, so fetch a pinned release
      # tarball. Pinned rather than `get-helm-3` because that script
      # installs whatever is newest at blade-boot time, which makes the
      # cluster a function of the day it was built.
      HELM_VER={{ .Values.cni.helmVersion | default "v3.16.4" | quote }}
      curl -fsSL "https://get.helm.sh/helm-${HELM_VER}-linux-amd64.tar.gz" -o /tmp/helm.tgz
      tar -xzf /tmp/helm.tgz -C /tmp
      install -m 0755 /tmp/linux-amd64/helm /usr/local/bin/helm
      helm repo add cilium https://helm.cilium.io/
      helm repo update
      for i in $(seq 1 12); do
        # ipam.mode=kubernetes makes Cilium allocate from the podCIDR
        # kubeadm already assigned to each Node, instead of its own
        # cluster-pool default (10.0.0.0/8). Without it the CNI and the
        # cluster disagree about pod addressing and routing silently
        # breaks across nodes.
        #
        # v0.13.18: k8sServiceHost/Port are resolved at RUNTIME.
        #
        # They used to come from the controlPlaneVip helper, which is EMPTY on
        # every HA render for the same reason the VIP bake was: the address is
        # derived by arping after this node boots, so the chart cannot know it.
        # The flags were therefore omitted entirely, and that was survivable
        # only while kube-proxy existed to implement the in-cluster `kubernetes`
        # Service.
        #
        # With kubeProxyReplacement=true and the kube-proxy addon skipped,
        # NOTHING implements that Service until Cilium itself is up — so
        # cilium-agent cannot reach the apiserver, its init container
        # crash-loops, and `helm --wait` sits for its full ten minutes before
        # failing. Measured: `Init:CrashLoopBackOff`, 7 restarts, two helm
        # attempts burning "context deadline exceeded" back to back.
        #
        # /etc/kubernetes/cp-endpoint was written by the arping derivation
        # above and holds the VIP; ADVERTISE_IP is this node's own address and
        # is the single-CP fallback. Both are known by this point.
        CILIUM_K8S_HOST=""; CILIUM_K8S_PORT="6443"
        if [ -s /etc/kubernetes/cp-endpoint ]; then
          CILIUM_K8S_HOST=$(cut -d: -f1 < /etc/kubernetes/cp-endpoint)
          CILIUM_K8S_PORT=$(cut -d: -f2 < /etc/kubernetes/cp-endpoint)
        fi
        [ -n "$CILIUM_K8S_HOST" ] || CILIUM_K8S_HOST="$ADVERTISE_IP"
        {{- if (.Values.cni).kubeProxyReplacement }}
        # Fail loudly rather than spend two hours in `helm --wait`: without an
        # apiserver address Cilium cannot start, and with kube-proxy skipped
        # there is no fallback path for it to find one.
        [ -n "$CILIUM_K8S_HOST" ] || {
          echo "kubeProxyReplacement=true but no apiserver address for cilium-agent:" >&2
          echo "  /etc/kubernetes/cp-endpoint: $(cat /etc/kubernetes/cp-endpoint 2>/dev/null || echo '<absent>')" >&2
          echo "  ADVERTISE_IP: ${ADVERTISE_IP:-<empty>}" >&2
          echo "  kube-proxy is skipped, so nothing implements the kubernetes Service — abort" >&2
          exit 1
        }
        {{- end }}
        echo "cilium: k8sServiceHost=$CILIUM_K8S_HOST k8sServicePort=$CILIUM_K8S_PORT"
        if helm upgrade --install cilium cilium/cilium \
             --version {{ .Values.cni.version | default "1.20.4" }} \
             --namespace kube-system \
             --set ipam.mode=kubernetes \
             {{- if (.Values.cni).kubeProxyReplacement }}
             --set kubeProxyReplacement=true \
             {{- end }}
             --set k8sServiceHost="$CILIUM_K8S_HOST" \
             --set k8sServicePort="$CILIUM_K8S_PORT" \
             --wait --timeout 10m; then
          echo "CNI (cilium) installed on attempt $i"
          bash /etc/kubernetes/publish-stage.sh cilium-installed
          break
        fi
        echo "CNI install attempt $i failed; sleeping 10s"
        sleep 10
      done
      {{- else }}
      for i in $(seq 1 12); do
        if kubectl apply -f {{ .Values.cni.manifestUrl }}; then
          echo "CNI applied on attempt $i"
          break
        fi
        echo "CNI apply attempt $i failed; sleeping 10s"
        sleep 10
      done
      {{- end }}
  - path: /etc/kubernetes/publish-drain-kubeconfig.sh
    permissions: "0755"
    content: |
      #!/bin/bash
      # Publish a DRAIN-SCOPED kubeconfig for this workload cluster into
      # Ironic's node.extra, so the chart's drain Jobs can reach the workload
      # apiserver without anyone SSH-copying admin.conf off a blade.
      #
      # v0.13.25. REPLACES publish-workload-kubeconfig.sh, which POSTed
      # /etc/kubernetes/admin.conf to the management apiserver at
      # managementCluster.apiUrl. Two things were wrong with that:
      #
      #  1. THE CHANNEL WAS DEAD. apiUrl was http://198.51.100.5:8443, served
      #     by a kubectl-proxy sidecar inside the wg-ironic-proxy pod. That pod
      #     was deleted for "zero proxies" and nothing replaced the sidecar.
      #     The ADDRESS still routes -- the tunnel pod holds 198.51.100.5/32
      #     and the lab routes 198.51.100.0/28 -- but nothing listens on 8443.
      #     So this script POSTed into a refused connection, retried 5 times,
      #     logged, and exited 1 without gating bring-up. The Secret it was
      #     supposed to create has NEVER existed. Measured 2026-11-04.
      #
      #     Every other publisher here goes through Ironic -- kubeadm_join,
      #     cert_key, etcd_ip, cp_endpoint, the deployed hash. "Ironic is the
      #     only rendezvous both sides can see" is the design, and this script
      #     was the single place that broke it. It is also the only one that
      #     never worked.
      #
      #  2. IT PUBLISHED admin.conf -- cluster-admin, no expiry. node.extra
      #     already carries credentials (cert_key, a join token) but both are
      #     bounded: 2h and 24h. A never-expiring cluster-admin credential in a
      #     field readable by any Ironic reader is a different proposition.
      #
      # So: a ServiceAccount scoped to exactly what `kubectl drain` needs.
      # NOT a short-lived token -- a drain is triggered by moving a worker into
      # spec.workers.removed, which may be days after bring-up, so a 1h token
      # would be expired precisely when it is needed. Long-lived but scoped.
      set -uo pipefail
      exec >> /var/log/publish-drain-kubeconfig.log 2>&1
      set -x
      date
      log() { echo "[$(date -Iseconds)] $*"; }

      export KUBECONFIG=/etc/kubernetes/admin.conf
      SA_NS=kube-system
      SA_NAME={{ printf "%s-drainer" .Values.clusterName | quote }}

      # Exactly the verbs `kubectl drain` issues: cordon is a node patch,
      # eviction is a create on the pods/eviction subresource, and it lists
      # pods to decide what to evict. Nothing else. In particular NO secrets,
      # no configmaps, no exec.
      log "creating the drain-scoped SA, ClusterRole and binding"
      kubectl apply -f - <<EOT
      apiVersion: v1
      kind: ServiceAccount
      metadata:
        name: ${SA_NAME}
        namespace: ${SA_NS}
      ---
      apiVersion: rbac.authorization.k8s.io/v1
      kind: ClusterRole
      metadata:
        name: ${SA_NAME}
      rules:
        - apiGroups: [""]
          resources: ["nodes"]
          verbs: ["get", "list", "patch"]
        - apiGroups: [""]
          resources: ["pods"]
          verbs: ["get", "list"]
        - apiGroups: [""]
          resources: ["pods/eviction"]
          verbs: ["create"]
      ---
      apiVersion: rbac.authorization.k8s.io/v1
      kind: ClusterRoleBinding
      metadata:
        name: ${SA_NAME}
      roleRef:
        apiGroup: rbac.authorization.k8s.io
        kind: ClusterRole
        name: ${SA_NAME}
      subjects:
        - kind: ServiceAccount
          name: ${SA_NAME}
          namespace: ${SA_NS}
      ---
      # k8s 1.24+ does not auto-create SA token Secrets; a manually declared
      # one of this type still gets populated and does not expire. Same pattern
      # the chart used for the cp-publisher SA until v0.13.25 removed it.
      apiVersion: v1
      kind: Secret
      type: kubernetes.io/service-account-token
      metadata:
        name: ${SA_NAME}-token
        namespace: ${SA_NS}
        annotations:
          kubernetes.io/service-account.name: ${SA_NAME}
      EOT

      # The token is populated asynchronously by the token controller.
      TOKEN=""
      for i in $(seq 1 30); do
        TOKEN=$(kubectl -n "$SA_NS" get secret "${SA_NAME}-token" -o jsonpath='{.data.token}' 2>/dev/null | base64 -d 2>/dev/null || true)
        [ -n "$TOKEN" ] && break
        sleep 2
      done
      if [ -z "$TOKEN" ]; then
        log "FATAL: the SA token Secret never populated after 60s"
        exit 1
      fi
      CA=$(kubectl -n "$SA_NS" get secret "${SA_NAME}-token" -o jsonpath='{.data.ca\.crt}')
      log "drain SA token minted (len=${#TOKEN})"

      # Server address: prefer the derived VIP this node published, so the
      # kubeconfig survives the bootstrap node being replaced. Falls back to
      # this node's own advertise address for single-CP clusters.
      if [ -f /etc/kubernetes/cp-endpoint ]; then
        SERVER="https://$(cat /etc/kubernetes/cp-endpoint)"
      elif [ -f /etc/kubernetes/etcd-ip ]; then
        SERVER="https://$(cat /etc/kubernetes/etcd-ip):6443"
      else
        log "FATAL: neither cp-endpoint nor etcd-ip written; cannot address the apiserver"
        exit 1
      fi
      log "kubeconfig server=$SERVER"

      KCFG=$(cat <<EOT
      apiVersion: v1
      kind: Config
      clusters:
        - name: workload
          cluster:
            server: ${SERVER}
            certificate-authority-data: ${CA}
      users:
        - name: drainer
          user:
            token: ${TOKEN}
      contexts:
        - name: drainer@workload
          context:
            cluster: workload
            user: drainer
      current-context: drainer@workload
      EOT
      )
      # base64 contains no JSON metacharacters, so interpolating it into the
      # patch below is safe without escaping.
      KCFG_B64=$(printf '%s' "$KCFG" | base64 -w0)
      log "kubeconfig built (b64 len=${#KCFG_B64})"

      PATCH='[{"op":"add","path":"/extra/drain_kubeconfig","value":"'"$KCFG_B64"'"}]'

      KEYSTONE_URL="{{ .Values.ironicAuth.authUrl }}"
      IRONIC_URL="{{ .Values.ironicAuth.ironicUrl }}"
      NODE_UUID="{{ include "kubernetes-cluster.bootstrapNodeUuid" . }}"
      AUTH_BODY='{"auth":{"identity":{"methods":["password"],"password":{"user":{"name":"{{ .Values.ironicAuth.username }}","password":"{{ .Values.ironicAuth.password }}","domain":{"name":"{{ .Values.ironicAuth.userDomain }}"}}}},"scope":{"system":{"all":true}}}}'

      for attempt in 1 2 3 4 5; do
        TOKEN_K=$(curl -sS -i --max-time 15 -H "Content-Type: application/json" -d "$AUTH_BODY" "${KEYSTONE_URL}/v3/auth/tokens" 2>/dev/null \
          | grep -i '^X-Subject-Token:' | awk '{print $2}' | tr -d '\r')
        if [ -z "$TOKEN_K" ]; then
          log "keystone auth failed (attempt ${attempt}); retrying in $((attempt * 10))s"
          sleep $((attempt * 10)); continue
        fi
        if curl -fsSL --max-time 20 \
          -H "X-Auth-Token: ${TOKEN_K}" \
          -H "X-OpenStack-Ironic-API-Version: 1.109" \
          -H "Content-Type: application/json-patch+json" \
          --request PATCH --data "$PATCH" \
          "${IRONIC_URL}/v1/nodes/${NODE_UUID}" > /tmp/pdk.out 2>&1; then
          log "drain kubeconfig published to Ironic node.extra.drain_kubeconfig"
          exit 0
        fi
        log "publish attempt ${attempt} failed; sleeping $((attempt * 10))s"
        sleep $((attempt * 10))
      done
      log "publish-drain-kubeconfig failed after 5 attempts -- see /tmp/pdk.out"
      exit 1
{{- include "kubernetes-cluster.kubeadmPkiWriteFiles" . | nindent 2 }}
{{- include "kubernetes-cluster.kubeadmInitWriteFiles" . | nindent 2 }}
{{- include "kubernetes-cluster.publishDeployedHashWriteFiles" (dict "ctx" . "builder" "kubernetes-cluster.cpUserData") | nindent 2 }}
{{- include "kubernetes-cluster.resolveNodeExtraWriteFiles" . | nindent 2 }}
{{- if (default (dict) .Values.conformance).install }}
{{- include "kubernetes-cluster.conformanceWriteFiles" . | nindent 2 }}
{{- end }}
runcmd:
  # v0.12.7: publish FIRST so the bridge-mirrored observed hash matches
  # current within ~15s of boot — well before bh-chart's 60s reconcile
  # could race-fire auto-redeploy on a stale-leftover observed value.
  - bash /etc/kubernetes/publish-deployed-hash.sh
  - /etc/kubernetes/install-k8s.sh
  - /etc/kubernetes/cp-init.sh
  - /etc/kubernetes/publish-drain-kubeconfig.sh
  - /etc/kubernetes/publish-join.sh
  - systemctl daemon-reload
  - systemctl enable --now kubeadm-token-refresh.timer
{{- if (default (dict) .Values.conformance).install }}
  # LAST on purpose. The conformance runner is a convenience for re-certifying;
  # a cluster that came up correctly must not be reported as failed because a
  # GitHub release download did not work.
  - /etc/kubernetes/install-conformance.sh
{{- end }}
{{- end -}}

{{/* Cloud-init userData for a WORKER node. Installs kubeadm and runs the
     join command that was captured live from the CP BaremetalNode CR. */}}
{{- define "kubernetes-cluster.workerUserData" -}}
{{- $jc := include "kubernetes-cluster.joinCommand" . -}}
#cloud-config
{{- $na := default (dict) .Values.nodeAccess }}
{{- $keys := default (list) $na.sshAuthorizedKeys }}
{{- $pw := default "" $na.password }}
{{- $pwAuth := and (default false $na.allowPasswordAuth) (ne $pw "") }}
{{- /* DEFAULT-SAFE. This block used to hardcode `ssh_pwauth: true`, an unlocked
       `ironic` user with `plain_text_passwd: baremetal`, no expiry and sudo --
       in all three user-data paths, with no value or schema field to override
       it. charts/kubernetes-cluster is a PUBLISHED Marketplace blueprint
       (0.14.0 reached the channel on 2026-11-07), so every installer provisioned
       blades with a known password and SSH password auth on.
       Password login is now opt-in: it needs BOTH nodeAccess.allowPasswordAuth
       and a non-empty nodeAccess.password. With neither set the account is
       locked and ssh_pwauth is false. */}}
ssh_pwauth: {{ $pwAuth }}
chpasswd:
  expire: false
users:
  - name: ironic
{{- if $pwAuth }}
    plain_text_passwd: {{ $pw | quote }}
    lock-passwd: false
{{- else }}
    lock-passwd: true
{{- end }}
{{- if $keys }}
    ssh_authorized_keys:
{{- range $keys }}
      - {{ . | quote }}
{{- end }}
{{- end }}
    shell: /bin/bash
    sudo:
      - "ALL=(ALL) NOPASSWD:ALL"
write_files:
  - path: /etc/kubernetes/install-k8s.sh
    permissions: "0755"
    content: |
      #!/bin/bash
      set -euo pipefail
      exec >> /var/log/install-k8s.log 2>&1
      set -x
      date
      apt-get update
      apt-get install -y --no-install-recommends ca-certificates curl apt-transport-https
      # Same `trusted=yes` rationale as the CP install-k8s.sh: Debian 13 trixie's
      # sqv rejects the k8s repo Release.key as Signature Packet v3 not considered
      # secure since 2026-02-01. Packages still come from pkgs.k8s.io over HTTPS.
      echo "deb [trusted=yes] https://pkgs.k8s.io/core:/stable:/{{ regexReplaceAll "^(v[0-9]+\\.[0-9]+).*" .Values.k8sVersion "${1}" }}/deb/ /" > /etc/apt/sources.list.d/kubernetes.list
      # v0.13.17: containerd 2.x, from Docker's repo, for CONFORMANCE.
      #
      # Debian trixie offers containerd 1.7.24 and NOTHING ELSE
      # (apt-cache policy: Candidate: 1.7.24~ds1-6+deb13u1). The CNCF suite's
      #   [sig-node] ImageVolume ... (common/node/image_volume.go:119)
      # mounts an OCI image as a volume, which requires runtime support that
      # landed in the containerd 2.x line. On 1.7.24 the test pod never reaches
      # Running and the spec times out after five minutes. It is not a
      # configuration problem and no feature gate fixes it: the capability is
      # absent from the runtime, so this is a property of the BASE IMAGE rather
      # than of the chart's logic.
      #
      # download.docker.com is what the Kubernetes documentation prescribes and
      # what certified installers use, and `containerd.io` tracks the 2.x line
      # while staying apt-managed. Same `trusted=yes` rationale as the k8s repo
      # above: trixie's sqv rejects v3 signature packets, the transport is still
      # HTTPS, and this only skips the signature check.
      echo "deb [trusted=yes] https://download.docker.com/linux/debian trixie stable" > /etc/apt/sources.list.d/docker.list
      apt-get update
      # Resolve the full Debian package version including upstream revision
      # suffix (older minors -1.1, v1.36+ -2.1, etc.) via apt-cache instead
      # of hard-coding `-1.1`.
      K8S_PKG_VER=$(apt-cache madison kubelet | awk -v v="{{ trimPrefix "v" .Values.k8sVersion }}" '$3 ~ "^"v"-" {print $3; exit}')
      test -n "$K8S_PKG_VER"
      apt-get install -y kubelet="$K8S_PKG_VER" kubeadm="$K8S_PKG_VER" containerd.io jq
      apt-mark hold kubelet kubeadm
      # Debian 13 trixie's containerd ships with bin_dir = "/usr/lib/cni" by
      # default. Flannel's install-cni-plugin initContainer (and kubeadm)
      # drop CNI binaries at /opt/cni/bin instead, so kubelet errors with
      # `failed to find plugin "flannel" in path [/usr/lib/cni]` and coredns
      # never gets a sandbox. Override before starting containerd.
      mkdir -p /etc/containerd
      # Two patches to the default config:
      # 1. CNI bin_dir: Debian 13's containerd defaults to /usr/lib/cni;
      #    flannel + kubeadm install at /opt/cni/bin.
      # 2. SystemdCgroup: Debian 13's containerd defaults to false (i.e.
      #    cgroupfs), but kubelet defaults to cgroupDriver: systemd since
      #    k8s 1.22. The mismatch causes kubelet to see containers in
      #    unexpected cgroup paths and terminate them roughly every 11s
      #    after start — observed empirically as etcd /apiserver flapping
      #    14-28 times in a fresh cluster bringup. Aligning to systemd on
      #    both sides eliminates the flap.
      containerd config default \
        | sed -e 's|/usr/lib/cni|/opt/cni/bin|g' \
              -e 's|SystemdCgroup = false|SystemdCgroup = true|g' \
        > /etc/containerd/config.toml
      # v0.13.17: ASSERT the substitution landed, do not assume it.
      #
      # Both seds are string matches against whatever `containerd config
      # default` emits, and that output changed shape in containerd 2.x:
      # config `version = 3` with renamed plugin paths. If the SystemdCgroup
      # match silently misses, containerd runs cgroupfs while kubelet defaults
      # to systemd, and the mismatch kills containers roughly every 11 seconds
      # — observed as etcd and apiserver flapping 14-28 times in a fresh
      # bringup. That failure is expensive to diagnose and trivial to detect
      # here, so detect it here.
      grep -q 'SystemdCgroup = true' /etc/containerd/config.toml || {
        echo "containerd config: SystemdCgroup=true NOT set after patching." >&2
        echo "  `containerd config default` output shape probably changed" >&2
        echo "  (containerd 2.x uses config version 3). kubelet defaults to" >&2
        echo "  cgroupDriver: systemd, so proceeding would flap every ~11s." >&2
        echo "  containerd version: $(containerd --version 2>&1)" >&2
        grep -n 'SystemdCgroup' /etc/containerd/config.toml >&2 || echo "  no SystemdCgroup key at all" >&2
        exit 1
      }
      echo "containerd config: SystemdCgroup=true confirmed ($(containerd --version 2>&1 | awk '{print $3}'))"
      # systemctl enable --now doesn't restart a unit that's already
      # running; Debian's containerd apt-package enables+starts on
      # install, so without an explicit restart the new config.toml is
      # ignored and kubelet errors with `failed to find plugin "flannel"
      # in path [/usr/lib/cni]`.
      systemctl enable containerd
      systemctl restart containerd
      # Debian 13 trixie's default iptables alternative is iptables-nft. With
      # the bundled kube-proxy (v1.31, iptables mode), the proxy exits with
      # code 2 right after caches sync — no error logged, no kernel panic.
      # Pinning iptables-legacy avoids the silent crash. Track upstream
      # adoption of `--proxy-mode=nftables` for a longer-term path.
      update-alternatives --set iptables /usr/sbin/iptables-legacy
      update-alternatives --set ip6tables /usr/sbin/ip6tables-legacy
      modprobe overlay
      modprobe br_netfilter
      printf 'overlay\nbr_netfilter\n' > /etc/modules-load.d/k8s.conf
      printf 'net.bridge.bridge-nf-call-iptables = 1\nnet.bridge.bridge-nf-call-ip6tables = 1\nnet.ipv4.ip_forward = 1\n' > /etc/sysctl.d/99-kubernetes.conf
      sysctl --system
  - path: /etc/kubernetes/join.sh
    permissions: "0755"
    content: |
      #!/bin/bash
      # v0.12.0: chart-baked kubeadm join. Token + CA hash are derived
      # from the chart-managed cluster CA on disk (placed by the chart
      # if this is a worker bootstrapping into an HA cluster, or fetched
      # via discovery for workers without local CA — kubeadm validates
      # the discovery token against the apiserver's TLS cert hash).
      exec >> /var/log/join.log 2>&1
      set -x
      # v0.13.9: resolve the endpoint at RUNTIME from Ironic. The rendered
      # value is kept ONLY as the single-CP fallback, where no VIP is derived
      # and none is ever published. For HA it is empty at render time by
      # construction -- replicas render concurrently with a bootstrap that has
      # not booted -- and baking it is what produced `https:///healthz`.
      VIP_RENDERED={{ include "kubernetes-cluster.controlPlaneEndpoint" . | quote }}
      VIP=$(bash /etc/kubernetes/resolve-node-extra.sh cp_endpoint) || VIP=""
      if [ -z "$VIP" ]; then
        VIP="$VIP_RENDERED"
        echo "[$(date -Iseconds)] Ironic published no extra.cp_endpoint; falling back to rendered endpoint '"'"'$VIP'"'"'"
      fi
      # Name what is missing. A gate that aborts without saying what it waited
      # for is how the previous failure stayed invisible for thirty minutes.
      test -n "$VIP" || { echo "no control-plane endpoint: the bootstrap published no extra.cp_endpoint to Ironic AND the chart rendered none - cannot join"; exit 1; }
      TOKEN={{ include "kubernetes-cluster.bootstrapToken" . | quote }}
      # Wait for apiserver via VIP, ~10 min budget.
      for i in $(seq 1 120); do
        if curl -k -s --max-time 3 "https://$VIP/healthz" >/dev/null 2>&1; then
          echo "[$(date -Iseconds)] apiserver $VIP healthy on poll $i"
          break
        fi
        sleep 5
      done
      curl -k -s --max-time 3 "https://$VIP/healthz" >/dev/null 2>&1 \
        || { echo "apiserver never became reachable — abort"; exit 1; }
      # v0.12.2: workers MUST NOT have /etc/kubernetes/pki/ca.crt pre-
      # placed (kubeadm join's preflight rejects "file already exists").
      # Use --discovery-token-unsafe-skip-ca-verification — the bootstrap
      # token's signature validates cluster identity, so MITM on the
      # CA cert delivery is detected at token verification time.
      # v0.13.20: pin the worker's node-ip too. `kubeadm join` has no
      # --node-ip flag in its CLI form, so it goes to the kubelet via
      # /etc/default/kubelet, which kubeadm's systemd drop-in sources. Derived
      # from the VIP's /24 so workers report the same network as the control
      # plane; without it kubelet picks the first NIC (the OOB network) and the
      # cluster reports addresses on two different networks.
      NODE_IP_PREFIX=$(echo "$VIP" | awk -F: '{print $1}' | awk -F. '{print $1"."$2"."$3"."}')
      NODE_IP=$(ip -o -4 addr show \
        | awk -v p="$NODE_IP_PREFIX" '$4 ~ "^"p {sub("/.*","",$4); print $4; exit}')
      if [ -n "$NODE_IP" ]; then
        echo "KUBELET_EXTRA_ARGS=--node-ip=$NODE_IP" > /etc/default/kubelet
        echo "worker node-ip pinned to $NODE_IP (from VIP prefix $NODE_IP_PREFIX)"
      else
        # Not fatal: the join still works, the node just reports whichever
        # address kubelet picks. Say so rather than failing a working join.
        echo "WARNING: no address in $NODE_IP_PREFIX0/24; leaving node-ip to kubelet autodetect" >&2
      fi
      kubeadm join "$VIP" \
        --token "$TOKEN" \
        --discovery-token-unsafe-skip-ca-verification
{{- include "kubernetes-cluster.publishDeployedHashWriteFiles" (dict "ctx" . "builder" "kubernetes-cluster.workerUserData") | nindent 2 }}
{{- include "kubernetes-cluster.resolveNodeExtraWriteFiles" . | nindent 2 }}
runcmd:
  # v0.12.7: publish FIRST (see bootstrap CP rationale).
  - bash /etc/kubernetes/publish-deployed-hash.sh
  - /etc/kubernetes/install-k8s.sh
  - /etc/kubernetes/join.sh
{{- end -}}

{{/* Cloud-init userData for a REPLICA CP node (HA). Installs kubeadm
     and runs `kubeadm join ... --control-plane --certificate-key`. The
     join command and cert_key were both published by the bootstrap CP
     onto its own BaremetalNode CR's spec.extra. Replica CPs render only when
     both lookups resolve (see lifecycle-cp-replicas.yaml gate). */}}
{{- define "kubernetes-cluster.cpReplicaUserData" -}}
{{- /* v0.11.4: $jc + $ck are no longer baked at template time —
       join.sh polls the bootstrap CP's Node.spec.extra at runtime. */ -}}
#cloud-config
{{- $na := default (dict) .Values.nodeAccess }}
{{- $keys := default (list) $na.sshAuthorizedKeys }}
{{- $pw := default "" $na.password }}
{{- $pwAuth := and (default false $na.allowPasswordAuth) (ne $pw "") }}
{{- /* DEFAULT-SAFE. This block used to hardcode `ssh_pwauth: true`, an unlocked
       `ironic` user with `plain_text_passwd: baremetal`, no expiry and sudo --
       in all three user-data paths, with no value or schema field to override
       it. charts/kubernetes-cluster is a PUBLISHED Marketplace blueprint
       (0.14.0 reached the channel on 2026-11-07), so every installer provisioned
       blades with a known password and SSH password auth on.
       Password login is now opt-in: it needs BOTH nodeAccess.allowPasswordAuth
       and a non-empty nodeAccess.password. With neither set the account is
       locked and ssh_pwauth is false. */}}
ssh_pwauth: {{ $pwAuth }}
chpasswd:
  expire: false
users:
  - name: ironic
{{- if $pwAuth }}
    plain_text_passwd: {{ $pw | quote }}
    lock-passwd: false
{{- else }}
    lock-passwd: true
{{- end }}
{{- if $keys }}
    ssh_authorized_keys:
{{- range $keys }}
      - {{ . | quote }}
{{- end }}
{{- end }}
    shell: /bin/bash
    sudo:
      - "ALL=(ALL) NOPASSWD:ALL"
write_files:
  - path: /etc/kubernetes/install-k8s.sh
    permissions: "0755"
    content: |
      #!/bin/bash
      set -euo pipefail
      exec >> /var/log/install-k8s.log 2>&1
      set -x
      date
      # Debian generic cloud doesn't ship gnupg / apt-transport-https.
      apt-get update
      apt-get install -y --no-install-recommends ca-certificates curl apt-transport-https
      # `trusted=yes` skips OpenPGP signature verification on this repo.
      # Required because Debian 13 trixie's sqv rejects the k8s repo's
      # current Release.key as "Signature Packet v3 not considered secure
      # since 2026-02-01". The packages themselves still come from
      # pkgs.k8s.io over HTTPS; this only disables the signature check.
      echo "deb [trusted=yes] https://pkgs.k8s.io/core:/stable:/{{ regexReplaceAll "^(v[0-9]+\\.[0-9]+).*" .Values.k8sVersion "${1}" }}/deb/ /" > /etc/apt/sources.list.d/kubernetes.list
      # v0.13.17: containerd 2.x, from Docker's repo, for CONFORMANCE.
      #
      # Debian trixie offers containerd 1.7.24 and NOTHING ELSE
      # (apt-cache policy: Candidate: 1.7.24~ds1-6+deb13u1). The CNCF suite's
      #   [sig-node] ImageVolume ... (common/node/image_volume.go:119)
      # mounts an OCI image as a volume, which requires runtime support that
      # landed in the containerd 2.x line. On 1.7.24 the test pod never reaches
      # Running and the spec times out after five minutes. It is not a
      # configuration problem and no feature gate fixes it: the capability is
      # absent from the runtime, so this is a property of the BASE IMAGE rather
      # than of the chart's logic.
      #
      # download.docker.com is what the Kubernetes documentation prescribes and
      # what certified installers use, and `containerd.io` tracks the 2.x line
      # while staying apt-managed. Same `trusted=yes` rationale as the k8s repo
      # above: trixie's sqv rejects v3 signature packets, the transport is still
      # HTTPS, and this only skips the signature check.
      echo "deb [trusted=yes] https://download.docker.com/linux/debian trixie stable" > /etc/apt/sources.list.d/docker.list
      apt-get update
      # Resolve the full Debian package version including the upstream
      # revision suffix (older minors ship -1.1, v1.36+ ships -2.1, etc.)
      # by querying apt-cache rather than hard-coding `-1.1`.
      K8S_PKG_VER=$(apt-cache madison kubelet | awk -v v="{{ trimPrefix "v" .Values.k8sVersion }}" '$3 ~ "^"v"-" {print $3; exit}')
      test -n "$K8S_PKG_VER"
      apt-get install -y kubelet="$K8S_PKG_VER" kubeadm="$K8S_PKG_VER" kubectl="$K8S_PKG_VER" containerd.io etcd-client jq
      apt-mark hold kubelet kubeadm kubectl
      # Debian 13 trixie's containerd ships with bin_dir = "/usr/lib/cni" by
      # default. Flannel's install-cni-plugin initContainer (and kubeadm)
      # drop CNI binaries at /opt/cni/bin instead, so kubelet errors with
      # `failed to find plugin "flannel" in path [/usr/lib/cni]` and coredns
      # never gets a sandbox. Override before starting containerd.
      mkdir -p /etc/containerd
      # Two patches to the default config:
      # 1. CNI bin_dir: Debian 13's containerd defaults to /usr/lib/cni;
      #    flannel + kubeadm install at /opt/cni/bin.
      # 2. SystemdCgroup: Debian 13's containerd defaults to false (i.e.
      #    cgroupfs), but kubelet defaults to cgroupDriver: systemd since
      #    k8s 1.22. The mismatch causes kubelet to see containers in
      #    unexpected cgroup paths and terminate them roughly every 11s
      #    after start — observed empirically as etcd /apiserver flapping
      #    14-28 times in a fresh cluster bringup. Aligning to systemd on
      #    both sides eliminates the flap.
      containerd config default \
        | sed -e 's|/usr/lib/cni|/opt/cni/bin|g' \
              -e 's|SystemdCgroup = false|SystemdCgroup = true|g' \
        > /etc/containerd/config.toml
      # v0.13.17: ASSERT the substitution landed, do not assume it.
      #
      # Both seds are string matches against whatever `containerd config
      # default` emits, and that output changed shape in containerd 2.x:
      # config `version = 3` with renamed plugin paths. If the SystemdCgroup
      # match silently misses, containerd runs cgroupfs while kubelet defaults
      # to systemd, and the mismatch kills containers roughly every 11 seconds
      # — observed as etcd and apiserver flapping 14-28 times in a fresh
      # bringup. That failure is expensive to diagnose and trivial to detect
      # here, so detect it here.
      grep -q 'SystemdCgroup = true' /etc/containerd/config.toml || {
        echo "containerd config: SystemdCgroup=true NOT set after patching." >&2
        echo "  `containerd config default` output shape probably changed" >&2
        echo "  (containerd 2.x uses config version 3). kubelet defaults to" >&2
        echo "  cgroupDriver: systemd, so proceeding would flap every ~11s." >&2
        echo "  containerd version: $(containerd --version 2>&1)" >&2
        grep -n 'SystemdCgroup' /etc/containerd/config.toml >&2 || echo "  no SystemdCgroup key at all" >&2
        exit 1
      }
      echo "containerd config: SystemdCgroup=true confirmed ($(containerd --version 2>&1 | awk '{print $3}'))"
      # systemctl enable --now doesn't restart a unit that's already
      # running; Debian's containerd apt-package enables+starts on
      # install, so without an explicit restart the new config.toml is
      # ignored and kubelet errors with `failed to find plugin "flannel"
      # in path [/usr/lib/cni]`.
      systemctl enable containerd
      systemctl restart containerd
      # Debian 13 trixie's default iptables alternative is iptables-nft. With
      # the bundled kube-proxy (v1.31, iptables mode), the proxy exits with
      # code 2 right after caches sync — no error logged, no kernel panic.
      # Pinning iptables-legacy avoids the silent crash. Track upstream
      # adoption of `--proxy-mode=nftables` for a longer-term path.
      update-alternatives --set iptables /usr/sbin/iptables-legacy
      update-alternatives --set ip6tables /usr/sbin/ip6tables-legacy
      modprobe overlay
      modprobe br_netfilter
      printf 'overlay\nbr_netfilter\n' > /etc/modules-load.d/k8s.conf
      printf 'net.bridge.bridge-nf-call-iptables = 1\nnet.bridge.bridge-nf-call-ip6tables = 1\nnet.ipv4.ip_forward = 1\n' > /etc/sysctl.d/99-kubernetes.conf
      sysctl --system
  - path: /etc/kubernetes/write-kube-vip-manifest.sh
    permissions: "0755"
    content: |
      #!/bin/bash
      # Same writer as the bootstrap CP — see cpUserData. The replica
      # claims VIP ownership via leader election if/when the bootstrap CP
      # dies; until then it serves the apiserver locally and forwards
      # writes to the leader's apiserver.
      set -euo pipefail
      VIP="${1:?missing VIP}"
      IFACE="${2:?missing interface}"
      mkdir -p /etc/kubernetes/manifests
      cat > /etc/kubernetes/manifests/kube-vip.yaml <<EOF
      apiVersion: v1
      kind: Pod
      metadata:
        name: kube-vip
        namespace: kube-system
      spec:
        hostNetwork: true
        hostAliases:
          - hostnames:
              - kubernetes
            ip: 127.0.0.1
        priorityClassName: system-node-critical
        containers:
          - name: kube-vip
            image: ghcr.io/kube-vip/kube-vip:v0.8.7
            imagePullPolicy: IfNotPresent
            args: ["manager"]
            securityContext:
              capabilities:
                add: ["NET_ADMIN", "NET_RAW"]
            env:
              - { name: vip_arp,              value: "true" }
              - { name: port,                 value: "6443" }
              - { name: vip_interface,        value: "$IFACE" }
              - { name: vip_cidr,             value: "32" }
              - { name: cp_enable,            value: "true" }
              - { name: cp_namespace,         value: "kube-system" }
              - { name: svc_enable,           value: "false" }
              - { name: vip_leaderelection,   value: "true" }
              - { name: vip_leaseduration,    value: "15" }
              - { name: vip_renewdeadline,    value: "10" }
              - { name: vip_retryperiod,      value: "2" }
              - { name: address,              value: "$VIP" }
            volumeMounts:
              - { name: kubeconfig, mountPath: /etc/kubernetes/admin.conf }
        volumes:
          - name: kubeconfig
            hostPath:
              path: /etc/kubernetes/admin.conf
              type: FileOrCreate
      EOF
      echo "wrote /etc/kubernetes/manifests/kube-vip.yaml VIP=$VIP IFACE=$IFACE"
  - path: /etc/kubernetes/join.sh
    permissions: "0755"
    content: |
      #!/bin/bash
      # Replica CP join with retry. --control-plane + --certificate-key
      # decrypts the kubeadm-certs Secret uploaded by the bootstrap CP's
      # `kubeadm init --upload-certs --certificate-key=...`. The bootstrap
      # CP's apiserver may flap right after kubeadm init while etcd /
      # kube-proxy / cm stabilise; without retry, a join into a downtime
      # window leaves cloud-final failed and no auto-recovery. 30 × 20s.
      # Cert-key has a 2h TTL upstream; if every attempt fails with
      # `unable to fetch the certs`, re-upload from any healthy CP via:
      #   kubeadm init phase upload-certs --upload-certs \
      #     --certificate-key=<key-from-Node.spec.extra.cert_key>
      exec >> /var/log/join.log 2>&1
      set -x
{{- with include "kubernetes-cluster.controlPlaneVip" . }}
      # HA path: write the kube-vip static-pod manifest BEFORE kubeadm
      # join. kubelet starts kube-vip during the join; the bootstrap CP
      # already owns the VIP via ARP, so this replica's kube-vip stays
      # in backup mode until the bootstrap CP dies, then takes over.
      # The VIP_IFACE must be the OOB-network interface (same /24 as
      # the VIP), matching cp-init.sh's derivation on the bootstrap CP.
      VIP={{ . | quote }}
      VIP_PREFIX=$(echo "$VIP" | awk -F. '{print $1"."$2"."$3"."}')
      VIP_IFACE=$(ip -o -4 addr show \
        | awk -v p="$VIP_PREFIX" '$4 ~ "^"p {print $2; exit}')
      test -n "$VIP_IFACE"
      /etc/kubernetes/write-kube-vip-manifest.sh "$VIP" "$VIP_IFACE"
{{- end }}
      # v0.11.4: poll bootstrap CP's Node.spec.extra at runtime for the
      # kubeadm_join command + cert_key. Replicas are rendered
      # CONCURRENTLY with the bootstrap (no chart-template gate) so etcd
      # can form quorum; bootstrap publishes kubeadm_join only AFTER
      # kubeadm init succeeds. This script bridges the gap.
      #
      # v0.12.0: chart-baked values for kubeadm join. Token + cert-key
      # are deterministic per cluster (sha256 of clusterName), so all
      # CPs and workers know them at chart render time. The CA cert
      # hash is computed at runtime from the chart-distributed
      # /etc/kubernetes/pki/ca.crt (same CA on every CP via the
      # kubeadmPkiWriteFiles helper).
      # v0.13.9: resolve the endpoint at RUNTIME from Ironic. The rendered
      # value is kept ONLY as the single-CP fallback, where no VIP is derived
      # and none is ever published. For HA it is empty at render time by
      # construction -- replicas render concurrently with a bootstrap that has
      # not booted -- and baking it is what produced `https:///healthz`.
      VIP_RENDERED={{ include "kubernetes-cluster.controlPlaneEndpoint" . | quote }}
      VIP=$(bash /etc/kubernetes/resolve-node-extra.sh cp_endpoint) || VIP=""
      if [ -z "$VIP" ]; then
        VIP="$VIP_RENDERED"
        echo "[$(date -Iseconds)] Ironic published no extra.cp_endpoint; falling back to rendered endpoint '"'"'$VIP'"'"'"
      fi
      # Name what is missing. A gate that aborts without saying what it waited
      # for is how the previous failure stayed invisible for thirty minutes.
      test -n "$VIP" || { echo "no control-plane endpoint: the bootstrap published no extra.cp_endpoint to Ironic AND the chart rendered none - cannot join"; exit 1; }
      TOKEN={{ include "kubernetes-cluster.bootstrapToken" . | quote }}
      CERT_KEY={{ include "kubernetes-cluster.kubeadmCertKey" . | quote }}
      # v0.12.8: derive ADVERTISE_IP from the VIP's /24 — same trick the
      # bootstrap CP uses in cp-init.sh. Default-route IP on dual-NIC
      # blades lives on the mgmt network (192.168.0.x), but the bootstrap
      # advertised etcd on the data network (192.0.2.x). Matching here
      # is what lets the etcd learner peer with the leader.
      VIP_PREFIX_FOR_ADV=$(echo "$VIP" | awk -F: '{print $1}' | awk -F. '{print $1"."$2"."$3"."}')
      ADVERTISE_IP=$(ip -o -4 addr show \
        | awk -v p="$VIP_PREFIX_FOR_ADV" '$4 ~ "^"p {sub("/.*","",$4); print $4; exit}')
      echo "[$(date -Iseconds)] derived ADVERTISE_IP=$ADVERTISE_IP from VIP=$VIP"
      test -n "$ADVERTISE_IP"
      # Wait for the bootstrap CP's apiserver to be reachable via the
      # VIP. ~10 min budget covers bootstrap install-k8s + kubeadm init.
      for i in $(seq 1 120); do
        if curl -k -s --max-time 3 "https://$VIP/healthz" >/dev/null 2>&1; then
          echo "[$(date -Iseconds)] apiserver $VIP healthy on poll $i"
          break
        fi
        sleep 5
      done
      curl -k -s --max-time 3 "https://$VIP/healthz" >/dev/null 2>&1 \
        || { echo "apiserver never became reachable — abort"; exit 1; }
      # Compute the CA cert hash kubeadm expects: sha256 of the DER-
      # encoded SubjectPublicKeyInfo of the CA cert. Identical across
      # all CPs because the CA is chart-managed.
      # v0.12.5: restore chart PKI before computing CA_HASH. Subsequent
      # retries after kubeadm reset would otherwise see an empty pki/
      # and compute sha256 of /dev/null (= e3b0c44...), which fails CA
      # pinning at join time.
      bash /usr/local/bin/restore-chart-pki.sh
      CA_HASH=$(openssl x509 -pubkey -in /etc/kubernetes/pki/ca.crt -noout \
        | openssl rsa -pubin -outform der 2>/dev/null \
        | sha256sum | awk '{print $1}')
      test -n "$CA_HASH"
      # v0.12.4: stacked-etcd ghost-member recovery loop. kubeadm join
      # --control-plane announces a new etcd member to the existing
      # cluster BEFORE starting local etcd. If local etcd fails to come
      # up in time, the announced member sits "unstarted" in the cluster
      # forever and every retry collides ("member already exists").
      # v0.12.6: ghost cleanup + wait now use the chart-baked etcd-client
      # cert/key from /etc/kubernetes/pki-chart/ — present from cloud-init
      # so etcdctl works on the FIRST attempt (previously used apiserver-
      # etcd-client.crt, which kubeadm only writes mid-join; the first
      # attempt's silent etcdctl failures left ghosts uncleanable).
      # v0.13.2: resolve the bootstrap CP's etcd endpoint AT RUNTIME, from the
      # address the bootstrap derived for itself and published to its own
      # BaremetalNode CR's spec.extra.etcd_ip.
      #
      # This replaced .controlPlane.nodes[0].oobIp, a hand-typed IP. It must be
      # a runtime read and NOT a chart `lookup`: replica BHs render
      # CONCURRENTLY with the bootstrap (v0.11.4, so etcd can reach quorum), so
      # at render time the bootstrap has not booted and spec.extra is empty. A
      # render-time lookup would therefore bake an empty endpoint and the
      # member-add would fail with a URL of "https://:2379" — which looks like
      # an etcd problem and is not.
      #
      # Same credentials publish-join.sh uses for the reverse direction, and
      # the same management API the VIP health-check path already depends on.
      # v0.13.14: read etcd_ip from IRONIC, not from the management cluster.
      #
      # This polled $MGMT_API (http://198.51.100.5:8443) with a bearer token and
      # jq. It returned empty 120 times and aborted with "bootstrap never
      # published spec.extra.etcd_ip" -- while the value was sitting in both
      # Ironic AND the CR the whole time (etcd_ip=192.168.0.201). The channel
      # was dead, not the data: that address and port belonged to the wg proxy
      # pod removed for "zero proxies", and it is plain HTTP against an
      # apiserver port besides.
      #
      # The blade publishes to Ironic over its own OOB network and never needs
      # a management-API path for the join -- ironic-extra-bridge's own header
      # comment says exactly this. cp_endpoint already resolves that way and
      # worked on the first try ("resolved cp_endpoint=192.168.0.250:6443 on
      # poll 53"); etcd_ip was simply left on the old channel. One source of
      # truth now, the one the publisher writes to.
      ETCD_IP=$(bash /etc/kubernetes/resolve-node-extra.sh etcd_ip) || ETCD_IP=""
      test -n "$ETCD_IP" \
        || { echo "bootstrap never published extra.etcd_ip to Ironic — abort"; exit 1; }
      BOOTSTRAP_ETCD="https://${ETCD_IP}:2379"
      echo "[$(date -Iseconds)] bootstrap etcd resolved to $BOOTSTRAP_ETCD"
      ETCDCTL_AUTH="--cacert=/etc/kubernetes/pki-chart/etcd-ca.crt \
        --cert=/etc/kubernetes/pki-chart/etcd-client.crt \
        --key=/etc/kubernetes/pki-chart/etcd-client.key"
      # Local IPs of this host — used to scope ghost removal so we only
      # clean OUR OWN ghost (peerURL containing one of our IPs), never
      # someone else's currently-joining learner.
      LOCAL_IPS=$(ip -o -4 addr show 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | grep -v '^127\.' | sort -u)
      remove_ghost_etcd_members() {
        # Match unstarted (name=="") OR learner members whose peerURL
        # points at one of our local IPs. Keep going on etcdctl failure.
        local raw
        raw=$(ETCDCTL_API=3 etcdctl --endpoints="$BOOTSTRAP_ETCD" $ETCDCTL_AUTH \
          member list -w json 2>&1) || { echo "etcdctl member list failed: $raw"; return 0; }
        for ip in $LOCAL_IPS; do
          local ids
          ids=$(echo "$raw" | jq -r --arg ip "$ip" '.members[] | select((.name == "" or .isLearner == true) and any(.peerURLs[]; contains($ip))) | .ID' 2>/dev/null || true)
          for gid in $ids; do
            local hex=$(printf "%x" "$gid")
            echo "removing OUR ghost etcd member id=$hex (decimal $gid) peer=$ip"
            ETCDCTL_API=3 etcdctl --endpoints="$BOOTSTRAP_ETCD" $ETCDCTL_AUTH \
              member remove "$hex" || true
          done
        done
      }
      # v0.12.5: wait for etcd cluster to have NO pending learner (etcd
      # 3.6 rejects MemberAdd when an unpromoted learner exists). With
      # 2+ replicas booting concurrently, the second sees "too many
      # learner members" until the first is promoted (~30-60s after
      # local etcd starts). Polling here serializes joins via etcd state.
      # v0.12.6: distinguish etcdctl auth/connect failure (transient) from
      # actual pending count — empty result no longer counts as "0".
      wait_for_etcd_no_pending_learner() {
        for i in $(seq 1 40); do
          local raw count
          raw=$(ETCDCTL_API=3 etcdctl --endpoints="$BOOTSTRAP_ETCD" $ETCDCTL_AUTH \
            member list -w json 2>&1) || { echo "etcdctl unreachable (poll $i): $raw"; sleep 15; continue; }
          count=$(echo "$raw" | jq -r '[.members[] | select(.isLearner == true or .name == "")] | length' 2>/dev/null || echo "")
          if [ "$count" = "0" ]; then
            echo "etcd ready for member-add on poll $i"
            return 0
          fi
          if [ -z "$count" ]; then
            echo "jq parse failed (poll $i); raw=$raw"
          else
            echo "etcd has $count pending learner(s) on poll $i; waiting"
          fi
          sleep 15
        done
      }
      # v0.12.6: pre-pull kubeadm images BEFORE the retry loop. The
      # original attempt-1 failure was "etcd member is not started" —
      # kubeadm writes the etcd static-pod manifest, kubelet starts a
      # container, but image pull from k8s.gcr.io eats into kubeadm's
      # bounded wait window and the etcd-health check times out. Cache
      # the images once so subsequent kubelet pulls hit local store.
      echo "[$(date -Iseconds)] pre-pulling kubeadm images"
      kubeadm config images pull -v=0 2>&1 | tail -20 || true
      JOIN_OK=
      for attempt in 1 2 3 4 5; do
        echo "[$(date -Iseconds)] kubeadm join attempt $attempt"
        # Each retry: restore PKI (kubeadm reset wipes pki/), clean any
        # ghost left by a previous attempt, wait for etcd to be quiet.
        bash /usr/local/bin/restore-chart-pki.sh
        remove_ghost_etcd_members
        wait_for_etcd_no_pending_learner
        # v0.12.8: render JoinConfiguration with ADVERTISE_IP / CA_HASH /
        # CERT_KEY so kubeadm uses the VIP-prefix-derived IP (192.0.2.x)
        # for both apiserver advertise AND etcd peer URL — matches the
        # bootstrap's etcd advertise so peers can actually reach each other.
        sed -e "s|__ADVERTISE_IP__|$ADVERTISE_IP|g" \
            -e "s|__CA_HASH__|$CA_HASH|g" \
            -e "s|__CERT_KEY__|$CERT_KEY|g" \
            -e "s|__CP_ENDPOINT__|$VIP|g" \
          /etc/kubernetes/kubeadm-join.yaml.template \
          > /etc/kubernetes/kubeadm-join.yaml
        chmod 0600 /etc/kubernetes/kubeadm-join.yaml
        # Refuse before burning five kubeadm attempts on a config that cannot
        # work. kubeadm's own message is clear, but it arrives once per attempt
        # and says nothing about WHERE the endpoint was supposed to come from.
        if grep -q "__CP_ENDPOINT__" /etc/kubernetes/kubeadm-join.yaml \
           || grep -qE "^\s*apiServerEndpoint:\s*(\"\")?\s*$" /etc/kubernetes/kubeadm-join.yaml; then
          echo "[$(date -Iseconds)] apiServerEndpoint unresolved in kubeadm-join.yaml" >&2
          echo "  VIP as resolved by join.sh: '${VIP:-<empty>}'" >&2
          echo "  resolver log: $(tail -1 /var/log/resolve-node-extra.log 2>/dev/null)" >&2
          exit 1
        fi
        grep -E "apiServerEndpoint|advertiseAddress" /etc/kubernetes/kubeadm-join.yaml
        if kubeadm join --config /etc/kubernetes/kubeadm-join.yaml; then
          JOIN_OK=yes
          break
        fi
        echo "[$(date -Iseconds)] attempt $attempt failed; cleaning ghosts + resetting"
        remove_ghost_etcd_members
        kubeadm reset --force --cleanup-tmp-dir 2>&1 | tail -10 || true
        sleep 20
      done
      [ -n "$JOIN_OK" ] || { echo "all attempts failed"; exit 1; }
{{- include "kubernetes-cluster.kubeadmPkiWriteFiles" . | nindent 2 }}
{{- include "kubernetes-cluster.kubeadmJoinWriteFiles" . | nindent 2 }}
{{- include "kubernetes-cluster.publishDeployedHashWriteFiles" (dict "ctx" . "builder" "kubernetes-cluster.cpReplicaUserData") | nindent 2 }}
{{- include "kubernetes-cluster.resolveNodeExtraWriteFiles" . | nindent 2 }}
runcmd:
  # v0.12.7: publish FIRST (see bootstrap CP rationale).
  - bash /etc/kubernetes/publish-deployed-hash.sh
  - /etc/kubernetes/install-k8s.sh
  - /etc/kubernetes/join.sh
{{- end -}}

{{/* kubernetes-cluster.cpToken REMOVED in v0.13.25.

     It pulled the cp-publisher ServiceAccount's token so the CP cloud-init
     could authenticate to the MANAGEMENT apiserver. Its only consumer was
     publish-workload-kubeconfig.sh, which is gone -- the drain credential now
     travels through Ironic like every other published value, so no blade
     needs a management-cluster credential at all.

     templates/rbac.yaml went with it: the SA, its Role, its RoleBinding and
     its non-expiring token Secret existed solely to serve that one script.
     Verified before deleting -- the rendered userData contains zero
     occurrences of BEARER, "Authorization: Bearer" and
     "serviceaccount/token", and the drain Jobs authenticate with the mounted
     kubeconfig rather than an SA. Leaving them would have been standing
     privilege that this change itself made dead. */}}

