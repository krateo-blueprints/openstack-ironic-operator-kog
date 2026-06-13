{{- define "kubernetes-cluster.nodeApiVersion" -}}baremetal.ogen.krateo.io/v1alpha1{{- end -}}

{{/* Per-node CR kind/apiVersion. The chart renders BaremetalHost
     (unified composition) instead of BaremetalLifecycle so we can lean
     on the v0.3.4 widened `spec.undeploy` gate for drain & undeploy
     (Milestone 5b) and image swaps (Milestone 5a). Single CR per node
     throughout its lifecycle. */}}
{{- define "kubernetes-cluster.lifecycleApiVersion" -}}composition.krateo.io/v0-3-5{{- end -}}
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
      apt-get install -y kubelet={{ trimPrefix "v" .Values.k8sVersion }}-1.1 kubeadm={{ trimPrefix "v" .Values.k8sVersion }}-1.1 kubectl={{ trimPrefix "v" .Values.k8sVersion }}-1.1 containerd
      apt-mark hold kubelet kubeadm kubectl
      # Debian 13 trixie's containerd ships with bin_dir = "/usr/lib/cni" by
      # default. Flannel's install-cni-plugin initContainer (and kubeadm)
      # drop CNI binaries at /opt/cni/bin instead, so kubelet errors with
      # `failed to find plugin "flannel" in path [/usr/lib/cni]` and coredns
      # never gets a sandbox. Override before starting containerd.
      mkdir -p /etc/containerd
      containerd config default | sed 's|/usr/lib/cni|/opt/cni/bin|g' > /etc/containerd/config.toml
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
      ADVERTISE_IP=$(ip route get 8.8.8.8 2>/dev/null | awk '/src/ {for (i=1;i<=NF;i++) if ($i=="src") print $(i+1)}' | head -1)
      echo "ADVERTISE_IP=$ADVERTISE_IP"
      test -n "$ADVERTISE_IP"
      # Mint a fresh certificate-key locally. Stored at /etc/kubernetes/cert-key
      # so publish-join.sh picks it up and publishes alongside the join command.
      # Always done — even for single-CP — so promoting to HA later is a values
      # change, not an OS-level reconfiguration.
      openssl rand -hex 32 > /etc/kubernetes/cert-key
      chmod 0600 /etc/kubernetes/cert-key
      kubeadm init \
        --upload-certs \
        --certificate-key=$(cat /etc/kubernetes/cert-key) \
        --pod-network-cidr={{ .Values.network.podCIDR }} \
        --service-cidr={{ .Values.network.serviceCIDR }} \
        --kubernetes-version={{ .Values.k8sVersion }} \
        --apiserver-advertise-address=$ADVERTISE_IP \
        --apiserver-bind-port=6443{{ with include "kubernetes-cluster.controlPlaneEndpoint" . }} \
        --control-plane-endpoint={{ . }}{{ end }}
      mkdir -p /root/.kube
      cp /etc/kubernetes/admin.conf /root/.kube/config
      KUBECONFIG=/etc/kubernetes/admin.conf kubectl apply -f {{ .Values.cni.manifestUrl }}
runcmd:
  - /etc/kubernetes/install-k8s.sh
  - /etc/kubernetes/cp-init.sh
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
      apt-get install -y kubelet={{ trimPrefix "v" .Values.k8sVersion }}-1.1 kubeadm={{ trimPrefix "v" .Values.k8sVersion }}-1.1 containerd
      apt-mark hold kubelet kubeadm
      # Debian 13 trixie's containerd ships with bin_dir = "/usr/lib/cni" by
      # default. Flannel's install-cni-plugin initContainer (and kubeadm)
      # drop CNI binaries at /opt/cni/bin instead, so kubelet errors with
      # `failed to find plugin "flannel" in path [/usr/lib/cni]` and coredns
      # never gets a sandbox. Override before starting containerd.
      mkdir -p /etc/containerd
      containerd config default | sed 's|/usr/lib/cni|/opt/cni/bin|g' > /etc/containerd/config.toml
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
      # Retry kubeadm join. The bootstrap CP's apiserver may flap right
      # after kubeadm init while etcd / kube-proxy / cm stabilise; if the
      # join lands in one of those gaps it errors with
      #   `connect: connection refused` against <cp>:6443
      # and cloud-init's runcmd doesn't retry. 30 × 20s gives ~10min of
      # retry — long enough to catch a healthy window without burning a
      # whole BH-redeploy cycle.
      exec >> /var/log/join.log 2>&1
      set -x
      for i in $(seq 1 30); do
        echo "[$(date -Iseconds)] join attempt $i"
        if {{ $jc }}; then
          echo "[$(date -Iseconds)] join succeeded on attempt $i"
          exit 0
        fi
        echo "[$(date -Iseconds)] attempt $i failed; sleeping 20s"
        sleep 20
      done
      echo "[$(date -Iseconds)] all 30 attempts exhausted"
      exit 1
runcmd:
  - /etc/kubernetes/install-k8s.sh
  - /etc/kubernetes/join.sh
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
      apt-get install -y kubelet={{ trimPrefix "v" .Values.k8sVersion }}-1.1 kubeadm={{ trimPrefix "v" .Values.k8sVersion }}-1.1 kubectl={{ trimPrefix "v" .Values.k8sVersion }}-1.1 containerd
      apt-mark hold kubelet kubeadm kubectl
      # Debian 13 trixie's containerd ships with bin_dir = "/usr/lib/cni" by
      # default. Flannel's install-cni-plugin initContainer (and kubeadm)
      # drop CNI binaries at /opt/cni/bin instead, so kubelet errors with
      # `failed to find plugin "flannel" in path [/usr/lib/cni]` and coredns
      # never gets a sandbox. Override before starting containerd.
      mkdir -p /etc/containerd
      containerd config default | sed 's|/usr/lib/cni|/opt/cni/bin|g' > /etc/containerd/config.toml
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
      for i in $(seq 1 30); do
        echo "[$(date -Iseconds)] join attempt $i"
        if {{ $jc }} --control-plane --certificate-key {{ $ck }}; then
          echo "[$(date -Iseconds)] join succeeded on attempt $i"
          exit 0
        fi
        echo "[$(date -Iseconds)] attempt $i failed; sleeping 20s"
        sleep 20
      done
      echo "[$(date -Iseconds)] all 30 attempts exhausted"
      exit 1
runcmd:
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
