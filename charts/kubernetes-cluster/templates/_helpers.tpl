{{- define "kubernetes-cluster.nodeApiVersion" -}}baremetal.ogen.krateo.io/v1alpha1{{- end -}}

{{/* Per-node CR kind/apiVersion. The chart renders BaremetalHost
     (unified composition) instead of BaremetalLifecycle so we can lean
     on the v0.3.4 widened `spec.undeploy` gate for drain & undeploy
     (Milestone 5b) and image swaps (Milestone 5a). Single CR per node
     throughout its lifecycle. */}}
{{- define "kubernetes-cluster.lifecycleApiVersion" -}}composition.krateo.io/v0-3-4{{- end -}}
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
write_files:
  - path: /etc/kubernetes/install-k8s.sh
    permissions: "0755"
    content: |
      #!/bin/bash
      set -euo pipefail
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
      apt-get install -y kubelet={{ trimPrefix "v" .Values.k8sVersion }}-1.1 kubeadm={{ trimPrefix "v" .Values.k8sVersion }}-1.1 kubectl={{ trimPrefix "v" .Values.k8sVersion }}-1.1 containerd
      apt-mark hold kubelet kubeadm kubectl
      systemctl enable --now containerd
  - path: /etc/kubernetes/mgmt-ca.crt
    permissions: "0644"
    content: |
      {{- include "kubernetes-cluster.mgmtCaBundle" . | nindent 6 }}
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
      # Mint a fresh kubeadm join token (24h TTL) and publish it onto our
      # Node CR's spec.extra.kubeadm_join. Called once from cloud-init at
      # boot, then every 12h by kubeadm-token-refresh.timer. The 12h
      # cadence is a 50% margin against the 24h TTL —
      # https://kubernetes.io/docs/reference/setup-tools/kubeadm/kubeadm-token/
      # — so a refresher miss still leaves a valid window before workers
      # see InvalidToken.
      #
      # When CERT_KEY file exists (HA bootstrap CP), also publishes the
      # cert_key so replica CPs can join with --certificate-key. The
      # cert_key has a 2h TTL upstream of kubeadm; renew via
      # `kubeadm init phase upload-certs --upload-certs --certificate-key=<key>`.
      set -uo pipefail
      log() { logger -t kubeadm-token-refresh "$*"; }
      JOIN_CMD=$(KUBECONFIG=/etc/kubernetes/admin.conf kubeadm token create --print-join-command --ttl 24h 2>/dev/null) || {
        log "kubeadm token create failed; will retry on next timer fire"
        exit 1
      }
      MGMT_API="{{ .Values.managementCluster.apiUrl }}"
      MGMT_TOKEN="{{ include "kubernetes-cluster.cpToken" . }}"
      # When apiUrl is http://, the request goes via the kubectl-proxy
      # sidecar in wg-ironic-proxy (lab path). The sidecar re-auths with
      # its own SA token; the Authorization header below is harmless.
      # --cacert is also harmless on http (curl ignores it).
      # Build the patch body. Include cert_key only if /etc/kubernetes/cert-key
      # exists (HA bootstrap CP wrote it before kubeadm init).
      ESCAPED_JOIN=${JOIN_CMD//\"/\\\"}
      if [ -f /etc/kubernetes/cert-key ]; then
        CERT_KEY=$(cat /etc/kubernetes/cert-key)
        PATCH_BODY="{\"spec\":{\"extra\":{\"kubeadm_join\":\"${ESCAPED_JOIN}\",\"cert_key\":\"${CERT_KEY}\"}}}"
      else
        PATCH_BODY="{\"spec\":{\"extra\":{\"kubeadm_join\":\"${ESCAPED_JOIN}\"}}}"
      fi
      for attempt in 1 2 3 4 5; do
        if curl -fsSL \
          -H "Authorization: Bearer ${MGMT_TOKEN}" \
          -H "Content-Type: application/merge-patch+json" \
          --cacert /etc/kubernetes/mgmt-ca.crt \
          --request PATCH \
          --data "${PATCH_BODY}" \
          "${MGMT_API}/apis/baremetal.ogen.krateo.io/v1alpha1/namespaces/{{ include "kubernetes-cluster.lifecycleNamespace" . }}/nodes/{{ include "kubernetes-cluster.cpNodeName" . }}" > /tmp/publish-join.out 2>&1; then
          log "join command published (attempt ${attempt})"
          exit 0
        fi
        log "publish attempt ${attempt} failed; sleeping $((attempt * 10))s before retry"
        sleep $((attempt * 10))
      done
      log "publish failed after 5 attempts — see /tmp/publish-join.out"
      exit 1
  - path: /etc/kubernetes/publish-workload-kubeconfig.sh
    permissions: "0755"
    content: |
      #!/bin/bash
      # One-shot: stash the workload cluster's admin.conf into a Secret on
      # the management cluster so drain Jobs (Milestone 5b) can talk to
      # the workload apiserver. Idempotent — POST returns 409 if the
      # Secret already exists, which we treat as success.
      set -uo pipefail
      log() { logger -t kubeadm-workload-kubeconfig "$*"; }
      MGMT_API="{{ .Values.managementCluster.apiUrl }}"
      MGMT_TOKEN="{{ include "kubernetes-cluster.cpToken" . }}"
      # When apiUrl is http://, the request goes via the kubectl-proxy
      # sidecar in wg-ironic-proxy (lab path). The sidecar re-auths with
      # its own SA token; the Authorization header below is harmless.
      # --cacert is also harmless on http (curl ignores it).
      SECRET_NS="{{ include "kubernetes-cluster.lifecycleNamespace" . }}"
      SECRET_NAME="{{ include "kubernetes-cluster.workloadKubeconfigSecretName" . }}"
      KCFG_B64=$(base64 -w0 /etc/kubernetes/admin.conf)
      RC=$(curl -sS -o /tmp/wkc.out -w "%{http_code}" \
        -H "Authorization: Bearer ${MGMT_TOKEN}" \
        -H "Content-Type: application/json" \
        --cacert /etc/kubernetes/mgmt-ca.crt \
        --request POST \
        --data "{\"apiVersion\":\"v1\",\"kind\":\"Secret\",\"metadata\":{\"name\":\"${SECRET_NAME}\",\"namespace\":\"${SECRET_NS}\"},\"data\":{\"kubeconfig\":\"${KCFG_B64}\"}}" \
        "${MGMT_API}/api/v1/namespaces/${SECRET_NS}/secrets") || RC=000
      case "$RC" in
        201) log "workload kubeconfig Secret created" ;;
        409) log "workload kubeconfig Secret already exists — idempotent skip" ;;
        *)   log "publish failed (HTTP $RC) — see /tmp/wkc.out"; exit 1 ;;
      esac
runcmd:
  - /etc/kubernetes/install-k8s.sh
  # Mint a fresh certificate-key locally. Stored at /etc/kubernetes/cert-key
  # so publish-join.sh picks it up and publishes alongside the join command.
  # Always done — even for single-CP — so promoting to HA later is a values
  # change, not an OS-level reconfiguration.
  - openssl rand -hex 32 > /etc/kubernetes/cert-key
  - chmod 0600 /etc/kubernetes/cert-key
  - kubeadm init --upload-certs --certificate-key=$(cat /etc/kubernetes/cert-key) --pod-network-cidr={{ .Values.network.podCIDR }} --service-cidr={{ .Values.network.serviceCIDR }} --kubernetes-version={{ .Values.k8sVersion }}{{ with include "kubernetes-cluster.controlPlaneEndpoint" . }} --control-plane-endpoint={{ . }}{{ end }}
  - mkdir -p /root/.kube && cp /etc/kubernetes/admin.conf /root/.kube/config
  - KUBECONFIG=/etc/kubernetes/admin.conf kubectl apply -f {{ .Values.cni.manifestUrl }}
  - /etc/kubernetes/publish-join.sh
  - /etc/kubernetes/publish-workload-kubeconfig.sh
  - systemctl daemon-reload
  - systemctl enable --now kubeadm-token-refresh.timer
{{- end -}}

{{/* Cloud-init userData for a WORKER node. Installs kubeadm and runs the
     join command that was captured live from the CP Node CR. */}}
{{- define "kubernetes-cluster.workerUserData" -}}
{{- $jc := include "kubernetes-cluster.joinCommand" . -}}
#cloud-config
write_files:
  - path: /etc/kubernetes/install-k8s.sh
    permissions: "0755"
    content: |
      #!/bin/bash
      set -euo pipefail
      curl -fsSL https://pkgs.k8s.io/core:/stable:/{{ regexReplaceAll "^(v[0-9]+\\.[0-9]+).*" .Values.k8sVersion "${1}" }}/deb/Release.key | gpg --dearmor -o /usr/share/keyrings/kubernetes-archive-keyring.gpg
      echo "deb [signed-by=/usr/share/keyrings/kubernetes-archive-keyring.gpg] https://pkgs.k8s.io/core:/stable:/{{ regexReplaceAll "^(v[0-9]+\\.[0-9]+).*" .Values.k8sVersion "${1}" }}/deb/ /" > /etc/apt/sources.list.d/kubernetes.list
      apt-get update
      apt-get install -y kubelet={{ trimPrefix "v" .Values.k8sVersion }}-1.1 kubeadm={{ trimPrefix "v" .Values.k8sVersion }}-1.1 containerd
      apt-mark hold kubelet kubeadm
      systemctl enable --now containerd
runcmd:
  - /etc/kubernetes/install-k8s.sh
  - {{ $jc | quote }}
{{- end -}}

{{/* Cloud-init userData for a REPLICA CP node (HA). Installs kubeadm
     and runs `kubeadm join ... --control-plane --certificate-key`. The
     join command and cert_key were both published by the bootstrap CP
     onto its own Node CR's spec.extra. Replica CPs render only when
     both lookups resolve (see lifecycle-cp-replicas.yaml gate). */}}
{{- define "kubernetes-cluster.cpReplicaUserData" -}}
{{- $jc := include "kubernetes-cluster.joinCommand" . -}}
{{- $ck := include "kubernetes-cluster.certKey"     . -}}
#cloud-config
write_files:
  - path: /etc/kubernetes/install-k8s.sh
    permissions: "0755"
    content: |
      #!/bin/bash
      set -euo pipefail
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
      apt-get install -y kubelet={{ trimPrefix "v" .Values.k8sVersion }}-1.1 kubeadm={{ trimPrefix "v" .Values.k8sVersion }}-1.1 kubectl={{ trimPrefix "v" .Values.k8sVersion }}-1.1 containerd
      apt-mark hold kubelet kubeadm kubectl
      systemctl enable --now containerd
runcmd:
  - /etc/kubernetes/install-k8s.sh
  # Replica CP join: --control-plane + --certificate-key decrypts the
  # kubeadm-certs Secret uploaded by the bootstrap CP's
  # `kubeadm init --upload-certs --certificate-key=...`. Cert-key has a
  # 2h TTL upstream; if join fails with `unable to fetch the certs`,
  # re-upload from any healthy CP via:
  #   kubeadm init phase upload-certs --upload-certs \
  #     --certificate-key=<key-from-Node.spec.extra.cert_key>
  - {{ $jc }} --control-plane --certificate-key {{ $ck }}
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
