{{- define "kubernetes-cluster.nodeApiVersion" -}}baremetal.ogen.krateo.io/v1alpha1{{- end -}}
{{- define "kubernetes-cluster.lifecycleApiVersion" -}}composition.krateo.io/v0-3-1{{- end -}}
{{- define "kubernetes-cluster.lifecycleKind" -}}BaremetalLifecycle{{- end -}}
{{- define "kubernetes-cluster.lifecycleNamespace" -}}
{{- if .Values.lifecycleNamespace -}}{{- .Values.lifecycleNamespace -}}{{- else -}}{{- .Release.Namespace -}}{{- end -}}
{{- end -}}

{{/* Stable name for the rendered BaremetalLifecycle CR for a given node. */}}
{{- define "kubernetes-cluster.lifecycleName" -}}
{{- .nodeName -}}
{{- end -}}

{{/* CP node name shortcut. */}}
{{- define "kubernetes-cluster.cpNodeName" -}}
{{- .Values.controlPlane.node.nodeName -}}
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

{{/* Common image dict that each rendered BaremetalLifecycle CR carries.
     instance_info shape is what Ironic expects (image_source / image_checksum). */}}
{{- define "kubernetes-cluster.instanceInfo" -}}
image_source:   {{ .Values.image.source   | quote }}
image_checksum: {{ .Values.image.checksum | quote }}
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
     runs `kubeadm init`, applies the CNI, then mints a join token and
     publishes it onto the CP's own Node CR via `kubectl patch` against
     the management cluster. The patch path is spec.extra.kubeadm_join
     — chosen because spec.extra is a writable, KOG-reconciled field in
     the Node CRD (Ironic round-trip is handled by the rest-dynamic
     -controller; we don't fight it).

     The management-cluster API URL, CA bundle, and SA token are baked
     into the userData at render time via .Values.managementCluster.
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
      curl -fsSL https://pkgs.k8s.io/core:/stable:/{{ regexReplaceAll "^(v[0-9]+\\.[0-9]+).*" .Values.k8sVersion "${1}" }}/deb/Release.key | gpg --dearmor -o /usr/share/keyrings/kubernetes-archive-keyring.gpg
      echo "deb [signed-by=/usr/share/keyrings/kubernetes-archive-keyring.gpg] https://pkgs.k8s.io/core:/stable:/{{ regexReplaceAll "^(v[0-9]+\\.[0-9]+).*" .Values.k8sVersion "${1}" }}/deb/ /" > /etc/apt/sources.list.d/kubernetes.list
      apt-get update
      apt-get install -y kubelet={{ trimPrefix "v" .Values.k8sVersion }}-1.1 kubeadm={{ trimPrefix "v" .Values.k8sVersion }}-1.1 kubectl={{ trimPrefix "v" .Values.k8sVersion }}-1.1 containerd
      apt-mark hold kubelet kubeadm kubectl
      systemctl enable --now containerd
  - path: /etc/kubernetes/mgmt-ca.crt
    permissions: "0644"
    content: |
      {{- include "kubernetes-cluster.mgmtCaBundle" . | nindent 6 }}
  - path: /etc/kubernetes/publish-join.sh
    permissions: "0755"
    content: |
      #!/bin/bash
      set -euo pipefail
      JOIN_CMD=$(KUBECONFIG=/etc/kubernetes/admin.conf kubeadm token create --print-join-command --ttl 24h)
      MGMT_API="{{ .Values.managementCluster.apiUrl }}"
      MGMT_TOKEN="{{ include "kubernetes-cluster.cpToken" . }}"
      curl -fsSL \
        -H "Authorization: Bearer ${MGMT_TOKEN}" \
        -H "Content-Type: application/merge-patch+json" \
        --cacert /etc/kubernetes/mgmt-ca.crt \
        --request PATCH \
        --data "{\"spec\":{\"extra\":{\"kubeadm_join\":\"${JOIN_CMD//\"/\\\"}\"}}}" \
        "${MGMT_API}/apis/baremetal.ogen.krateo.io/v1alpha1/namespaces/{{ include "kubernetes-cluster.lifecycleNamespace" . }}/nodes/{{ include "kubernetes-cluster.cpNodeName" . }}"
runcmd:
  - /etc/kubernetes/install-k8s.sh
  - kubeadm init --pod-network-cidr={{ .Values.network.podCIDR }} --service-cidr={{ .Values.network.serviceCIDR }} --kubernetes-version={{ .Values.k8sVersion }}{{ with include "kubernetes-cluster.controlPlaneEndpoint" . }} --control-plane-endpoint={{ . }}{{ end }}
  - mkdir -p /root/.kube && cp /etc/kubernetes/admin.conf /root/.kube/config
  - KUBECONFIG=/etc/kubernetes/admin.conf kubectl apply -f {{ .Values.cni.manifestUrl }}
  - /etc/kubernetes/publish-join.sh
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
