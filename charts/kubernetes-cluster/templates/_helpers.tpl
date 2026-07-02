{{- define "kubernetes-cluster.nodeApiVersion" -}}baremetal.ogen.krateo.io/v1alpha1{{- end -}}

{{/* Per-node CR kind/apiVersion. The chart renders BaremetalHost
     (unified composition) instead of BaremetalLifecycle so we can lean
     on the v0.3.4 widened `spec.undeploy` gate for drain & undeploy
     (Milestone 5b) and image swaps (Milestone 5a). Single CR per node
     throughout its lifecycle. */}}
{{- define "kubernetes-cluster.lifecycleApiVersion" -}}composition.krateo.io/v0-4-5{{- end -}}
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
     cert_key on its own Node CR. Replica CPs (index 1..N-1) join via
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

{{/* The bootstrap CP's certificate-key, read live from the CP Node CR's
     spec.extra.cert_key. Empty until the bootstrap CP's cloud-init has
     finished `kubeadm init --upload-certs --certificate-key=$KEY` AND
     patched its own Node CR. This is the gate that controls whether
     replica CPs render — without the key they can't decrypt the
     uploaded kubeadm-certs Secret. The TTL is 2h (kubeadm hard-coded).

     https://kubernetes.io/docs/setup/production-environment/tools/kubeadm/high-availability/
     "Please note that the certificate-key gives access to cluster
      sensitive data, keep it secret! As a safeguard, uploaded-certs will
      be deleted in two hours; If necessary, you can use kubeadm init
      phase upload-certs to reload certs afterward." */}}
{{- define "kubernetes-cluster.certKey" -}}
{{- $ns  := include "kubernetes-cluster.lifecycleNamespace" . -}}
{{- $cp  := include "kubernetes-cluster.cpNodeName"          . -}}
{{- $node := lookup (include "kubernetes-cluster.nodeApiVersion" .) "Node" $ns $cp -}}
{{- if $node -}}
{{- $extra := dig "spec" "extra" (dict) $node -}}
{{- $ck    := index $extra "cert_key" -}}
{{- if $ck -}}{{- $ck -}}{{- end -}}
{{- end -}}
{{- end -}}

{{/* The captured `kubeadm join` command, read live from the CP Node CR's
     spec.extra.kubeadm_join. Empty until the CP cloud-init has finished
     `kubeadm init` AND patched its own Node CR. This is the FSM gate
     that controls whether worker BaremetalLifecycles get rendered. */}}
{{- define "kubernetes-cluster.joinCommand" -}}
{{- $ns  := include "kubernetes-cluster.lifecycleNamespace" . -}}
{{- $cp  := include "kubernetes-cluster.cpNodeName"          . -}}
{{- $node := lookup (include "kubernetes-cluster.nodeApiVersion" .) "Node" $ns $cp -}}
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
{{- if $cp.endpoint -}}{{- $cp.endpoint -}}{{- end -}}
{{- end -}}

{{/* Strip the optional ":port" off controlPlane.endpoint and return just
     the host (or IP). Used by the kube-vip static-pod manifest to know
     which address to claim. Empty when the user hasn't set endpoint. */}}
{{- define "kubernetes-cluster.controlPlaneVip" -}}
{{- $cp := default (dict) .Values.controlPlane -}}
{{- if $cp.endpoint -}}
{{- regexReplaceAll ":[0-9]+$" $cp.endpoint "" -}}
{{- end -}}
{{- end -}}

{{/* === v0.11.0: External etcd PKI ===

     HA clusters (cpNodes count > 1) run a 3-member etcd cluster as a
     SYSTEMD UNIT on each CP — independent of kubeadm's stacked-etcd
     flow. This block manages the PKI:

       - etcd CA (cert + key, 10y validity)
       - apiserver-etcd-client cert (cert + key, 10y, signed by CA)
       - per-CP etcd server cert (cert + key, 10y, IP+DNS SANs, signed
         by CA) — requires user to supply .controlPlane.nodes[].oobIp
         so the SAN matches what apiserver/peers dial

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
     mirrored to Node CR.spec.extra by the bridge sidecar. The
     baremetal-host chart's shouldAutoRedeploy compares this value to
     the BH-baked configDriveHashInput (also = .Chart.Version when
     rendered) — match means the running OS is the chart's intended
     userData; mismatch (including empty after a teardown+redeploy)
     fires transition-undeploy.

     Stable across cdc reconciles because .Chart.Version doesn't
     change between renders of the same chart. Drift fires when the
     CHART itself bumps (operator-triggered redeploy). */}}
{{- define "kubernetes-cluster.deployedConfigDriveHash" -}}
{{- .Chart.Version -}}
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
    ---
    apiVersion: kubeadm.k8s.io/v1beta4
    kind: ClusterConfiguration
    kubernetesVersion: {{ .Values.k8sVersion }}
{{- if $cpe }}
    controlPlaneEndpoint: {{ $cpe | quote }}
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
     etcd on the data network (172.19.74.x via VIP_PREFIX), peers can
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
        apiServerEndpoint: {{ $cpe | quote }}
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

{{/* v0.12.3 (Task #91): publish-deployed-hash.sh + /etc/kubernetes/
     .deployed-hash file. Universal across cpUserData, cpReplicaUserData,
     workerUserData. */}}
{{- define "kubernetes-cluster.publishDeployedHashWriteFiles" -}}
- path: /etc/kubernetes/.deployed-hash
  permissions: "0644"
  content: |
    {{ include "kubernetes-cluster.deployedConfigDriveHash" . }}
- path: /etc/kubernetes/publish-deployed-hash.sh
  permissions: "0755"
  content: |
    #!/bin/bash
    # Publish the chart-baked deploy hash to Ironic.node.extra.
    # deployed_configdrive_hash. Bridge sidecar mirrors to Node CR
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
    AUTH_URL={{ .Values.ironicAuth.authUrl | quote }}
    IRONIC_URL={{ .Values.ironicAuth.ironicUrl | quote }}
    USERNAME={{ .Values.ironicAuth.username | quote }}
    PASSWORD={{ .Values.ironicAuth.password | quote }}
    USER_DOMAIN={{ .Values.ironicAuth.userDomain | quote }}
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
     CP's own Node CR via `kubectl patch` against the management cluster.
     The patch path is spec.extra.kubeadm_join — chosen because spec.extra
     is a writable, KOG-reconciled field in the Node CRD (Ironic round-
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
ssh_pwauth: true
chpasswd:
  expire: false
users:
  - name: ironic
    plain_text_passwd: baremetal
    lock-passwd: false
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
      apt-get update
      # Resolve the full Debian package version including the upstream
      # revision suffix (older minors ship -1.1, v1.36+ ships -2.1, etc.)
      # by querying apt-cache rather than hard-coding `-1.1`.
      K8S_PKG_VER=$(apt-cache madison kubelet | awk -v v="{{ trimPrefix "v" .Values.k8sVersion }}" '$3 ~ "^"v"-" {print $3; exit}')
      test -n "$K8S_PKG_VER"
      apt-get install -y kubelet="$K8S_PKG_VER" kubeadm="$K8S_PKG_VER" kubectl="$K8S_PKG_VER" containerd etcd-client jq
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
      Description=Mint a fresh kubeadm join token and publish it onto our Node CR
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
  - path: /etc/kubernetes/publish-join.sh
    permissions: "0755"
    content: |
      #!/bin/bash
      # Mint a fresh kubeadm join token (24h TTL) and PATCH it into the
      # Ironic node's `extra.kubeadm_join`. The bare-metal blade can
      # reach the lab Ironic API over its OOB network, but CANNOT reach
      # the management cluster's API (wg tunnel routes one direction
      # only on this lab). The ironic-extra-bridge sidecar in
      # wg-ironic-proxy propagates extra back into the k8s Node CR's
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
      # v0.12.5: restore PKI from /etc/kubernetes/pki-chart/ to where
      # kubeadm expects it. Survives kubeadm reset because pki-chart/
      # is untouched by reset.
      bash /usr/local/bin/restore-chart-pki.sh
      sed -e "s|__ADVERTISE_IP__|$ADVERTISE_IP|g" \
          -e "s|__CERT_KEY__|$(cat /etc/kubernetes/cert-key)|g" \
        /etc/kubernetes/kubeadm-init.yaml.template \
        > /etc/kubernetes/kubeadm-init.yaml
      chmod 0600 /etc/kubernetes/kubeadm-init.yaml
      # Skip cert generation phases — chart-baked PKI is already on disk.
      # Kubeadm will detect existing CAs and only generate leaf certs.
      kubeadm init --upload-certs \
        --config /etc/kubernetes/kubeadm-init.yaml
      mkdir -p /root/.kube
      cp /etc/kubernetes/admin.conf /root/.kube/config
      # v0.12.4: retry CNI apply — apiserver may take a few seconds to
      # be ready right after `kubeadm init` completes. Without retry, a
      # single apply at the wrong moment leaves the cluster CNI-less,
      # and all nodes (CP + workers) stay NotReady forever.
      for i in $(seq 1 12); do
        if KUBECONFIG=/etc/kubernetes/admin.conf kubectl apply -f {{ .Values.cni.manifestUrl }}; then
          echo "CNI applied on attempt $i"
          break
        fi
        echo "CNI apply attempt $i failed; sleeping 10s"
        sleep 10
      done
  - path: /etc/kubernetes/publish-workload-kubeconfig.sh
    permissions: "0755"
    content: |
      #!/bin/bash
      # Push the freshly-minted /etc/kubernetes/admin.conf as a Secret in
      # the management cluster's openstack namespace, so external users
      # (the chart's drain Jobs, an operator like Ettore, …) can kubectl
      # into the new workload cluster without SSH-scping the file off
      # blade06.
      #
      # Authenticates as the chart's `<clusterName>-cp-publisher` SA — the
      # same SA used by publish-join.sh for the Node CR patch. Its Role
      # grants `secrets: create, get` in {{ default .Release.Namespace .Values.managementCluster.serviceAccountNamespace }}
      # (see templates/rbac.yaml).
      set -uo pipefail
      exec >> /var/log/publish-workload-kubeconfig.log 2>&1
      set -x
      date
      log() { echo "[$(date -Iseconds)] $*"; }

      MGMT_API="{{ .Values.managementCluster.apiUrl }}"
      MGMT_NS="{{ default .Release.Namespace .Values.managementCluster.serviceAccountNamespace }}"
      SECRET_NAME="{{ include "kubernetes-cluster.workloadKubeconfigSecretName" . }}"
      BEARER="{{ include "kubernetes-cluster.cpToken" . }}"

      if [ -z "$BEARER" ]; then
        log "no SA token rendered — chart's rbac.yaml has not produced the secret yet"
        exit 1
      fi

      KCFG_B64=$(base64 -w0 < /etc/kubernetes/admin.conf)
      PAYLOAD=$(cat <<EOF
      {"apiVersion":"v1","kind":"Secret","metadata":{"name":"${SECRET_NAME}","namespace":"${MGMT_NS}","labels":{"kubernetescluster.ogen.krateo.io/cluster":"{{ .Values.clusterName }}"}},"type":"Opaque","data":{"kubeconfig":"${KCFG_B64}"}}
      EOF
      )
      # merge-patch body for the update path (no resourceVersion required).
      PATCH_PAYLOAD="{\"data\":{\"kubeconfig\":\"${KCFG_B64}\"}}"

      for attempt in 1 2 3 4 5; do
        log "attempt $attempt: POST ${MGMT_API}/api/v1/namespaces/${MGMT_NS}/secrets"
        HTTP=$(curl -sS -o /tmp/pwk.out -w "%{http_code}" --max-time 15 \
          -H "Authorization: Bearer $BEARER" \
          -H "Content-Type: application/json" \
          --data "$PAYLOAD" \
          "${MGMT_API}/api/v1/namespaces/${MGMT_NS}/secrets")
        log "POST returned HTTP=$HTTP"
        if [ "$HTTP" = "201" ]; then
          log "workload-kubeconfig Secret created"
          exit 0
        fi
        if [ "$HTTP" = "409" ]; then
          log "Secret already exists; PATCH to update"
          HTTP=$(curl -sS -o /tmp/pwk.out -w "%{http_code}" --max-time 15 \
            -X PATCH \
            -H "Authorization: Bearer $BEARER" \
            -H "Content-Type: application/merge-patch+json" \
            --data "$PATCH_PAYLOAD" \
            "${MGMT_API}/api/v1/namespaces/${MGMT_NS}/secrets/${SECRET_NAME}")
          log "PATCH returned HTTP=$HTTP"
          if [ "$HTTP" = "200" ]; then
            log "workload-kubeconfig Secret updated"
            exit 0
          fi
        fi
        cat /tmp/pwk.out 2>/dev/null | head -3
        log "attempt $attempt failed; sleeping $((attempt * 10))s"
        sleep $((attempt * 10))
      done
      log "publish-workload-kubeconfig failed after 5 attempts"
      exit 1
{{- include "kubernetes-cluster.kubeadmPkiWriteFiles" . | nindent 2 }}
{{- include "kubernetes-cluster.kubeadmInitWriteFiles" . | nindent 2 }}
{{- include "kubernetes-cluster.publishDeployedHashWriteFiles" . | nindent 2 }}
runcmd:
  # v0.12.7: publish FIRST so the bridge-mirrored observed hash matches
  # current within ~15s of boot — well before bh-chart's 60s reconcile
  # could race-fire auto-redeploy on a stale-leftover observed value.
  - bash /etc/kubernetes/publish-deployed-hash.sh
  - /etc/kubernetes/install-k8s.sh
  - /etc/kubernetes/cp-init.sh
  - /etc/kubernetes/publish-workload-kubeconfig.sh
  - /etc/kubernetes/publish-join.sh
  - systemctl daemon-reload
  - systemctl enable --now kubeadm-token-refresh.timer
{{- end -}}

{{/* Cloud-init userData for a WORKER node. Installs kubeadm and runs the
     join command that was captured live from the CP Node CR. */}}
{{- define "kubernetes-cluster.workerUserData" -}}
{{- $jc := include "kubernetes-cluster.joinCommand" . -}}
#cloud-config
ssh_pwauth: true
chpasswd:
  expire: false
users:
  - name: ironic
    plain_text_passwd: baremetal
    lock-passwd: false
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
      apt-get update
      # Resolve the full Debian package version including upstream revision
      # suffix (older minors -1.1, v1.36+ -2.1, etc.) via apt-cache instead
      # of hard-coding `-1.1`.
      K8S_PKG_VER=$(apt-cache madison kubelet | awk -v v="{{ trimPrefix "v" .Values.k8sVersion }}" '$3 ~ "^"v"-" {print $3; exit}')
      test -n "$K8S_PKG_VER"
      apt-get install -y kubelet="$K8S_PKG_VER" kubeadm="$K8S_PKG_VER" containerd jq
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
      VIP={{ include "kubernetes-cluster.controlPlaneEndpoint" . | quote }}
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
      kubeadm join "$VIP" \
        --token "$TOKEN" \
        --discovery-token-unsafe-skip-ca-verification
{{- include "kubernetes-cluster.publishDeployedHashWriteFiles" . | nindent 2 }}
runcmd:
  # v0.12.7: publish FIRST (see bootstrap CP rationale).
  - bash /etc/kubernetes/publish-deployed-hash.sh
  - /etc/kubernetes/install-k8s.sh
  - /etc/kubernetes/join.sh
{{- end -}}

{{/* Cloud-init userData for a REPLICA CP node (HA). Installs kubeadm
     and runs `kubeadm join ... --control-plane --certificate-key`. The
     join command and cert_key were both published by the bootstrap CP
     onto its own Node CR's spec.extra. Replica CPs render only when
     both lookups resolve (see lifecycle-cp-replicas.yaml gate). */}}
{{- define "kubernetes-cluster.cpReplicaUserData" -}}
{{- /* v0.11.4: $jc + $ck are no longer baked at template time —
       join.sh polls the bootstrap CP's Node.spec.extra at runtime. */ -}}
#cloud-config
ssh_pwauth: true
chpasswd:
  expire: false
users:
  - name: ironic
    plain_text_passwd: baremetal
    lock-passwd: false
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
      apt-get update
      # Resolve the full Debian package version including the upstream
      # revision suffix (older minors ship -1.1, v1.36+ ships -2.1, etc.)
      # by querying apt-cache rather than hard-coding `-1.1`.
      K8S_PKG_VER=$(apt-cache madison kubelet | awk -v v="{{ trimPrefix "v" .Values.k8sVersion }}" '$3 ~ "^"v"-" {print $3; exit}')
      test -n "$K8S_PKG_VER"
      apt-get install -y kubelet="$K8S_PKG_VER" kubeadm="$K8S_PKG_VER" kubectl="$K8S_PKG_VER" containerd etcd-client jq
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
      VIP={{ include "kubernetes-cluster.controlPlaneEndpoint" . | quote }}
      TOKEN={{ include "kubernetes-cluster.bootstrapToken" . | quote }}
      CERT_KEY={{ include "kubernetes-cluster.kubeadmCertKey" . | quote }}
      # v0.12.8: derive ADVERTISE_IP from the VIP's /24 — same trick the
      # bootstrap CP uses in cp-init.sh. Default-route IP on dual-NIC
      # blades lives on the mgmt network (192.168.0.x), but the bootstrap
      # advertised etcd on the data network (172.19.74.x). Matching here
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
      BOOTSTRAP_ETCD={{ printf "https://%s:2379" (index ((include "kubernetes-cluster.cpNodes" . | fromYamlArray)) 0).oobIp | quote }}
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
        # CERT_KEY so kubeadm uses the VIP-prefix-derived IP (172.19.74.x)
        # for both apiserver advertise AND etcd peer URL — matches the
        # bootstrap's etcd advertise so peers can actually reach each other.
        sed -e "s|__ADVERTISE_IP__|$ADVERTISE_IP|g" \
            -e "s|__CA_HASH__|$CA_HASH|g" \
            -e "s|__CERT_KEY__|$CERT_KEY|g" \
          /etc/kubernetes/kubeadm-join.yaml.template \
          > /etc/kubernetes/kubeadm-join.yaml
        chmod 0600 /etc/kubernetes/kubeadm-join.yaml
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
{{- include "kubernetes-cluster.publishDeployedHashWriteFiles" . | nindent 2 }}
runcmd:
  # v0.12.7: publish FIRST (see bootstrap CP rationale).
  - bash /etc/kubernetes/publish-deployed-hash.sh
  - /etc/kubernetes/install-k8s.sh
  - /etc/kubernetes/join.sh
{{- end -}}

{{/* The Bearer token used by the CP to PATCH its own Node CR. Read live
     via lookup from the SA-bound Secret that templates/rbac.yaml
     created. Empty on the very first reconcile (Secret doesn't exist
     yet) — the chart's cp lifecycle template guards against rendering
     a userData with an empty token. */}}
{{- define "kubernetes-cluster.cpToken" -}}
{{- $sa  := .Values.managementCluster.serviceAccountName -}}
{{- $ns  := .Values.managementCluster.serviceAccountNamespace | default .Release.Namespace -}}
{{- if $sa -}}
{{- $secretName := printf "%s-token" $sa -}}
{{- $sec := lookup "v1" "Secret" $ns $secretName -}}
{{- if $sec -}}
{{- $t := index $sec.data "token" -}}
{{- if $t -}}{{- $t | b64dec -}}{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
