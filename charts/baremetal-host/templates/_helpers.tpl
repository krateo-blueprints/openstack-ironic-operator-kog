{{- define "baremetal-host.nodeApiVersion" -}}baremetal.ogen.krateo.io/v1alpha1{{- end -}}
{{- define "baremetal-host.nodeNamespace" -}}
{{- if .Values.nodeNamespace -}}{{- .Values.nodeNamespace -}}{{- else -}}{{- .Release.Namespace -}}{{- end -}}
{{- end -}}
{{- define "baremetal-host.configName" -}}
{{- if and .Values.configurationRef .Values.configurationRef.name -}}{{- .Values.configurationRef.name -}}{{- else -}}ironic-endpoint{{- end -}}
{{- end -}}

{{/* True when the host is "detached" — all transition templates should short-circuit and
     render nothing. The Node + Port CRs stay (so hand-off works) but no NodeProvision /
     NodePower CRs are produced. Mirror of metal3's `detached` annotation. */}}
{{- define "baremetal-host.detached" -}}
{{- if .Values.detached -}}true{{- end -}}
{{- end -}}

{{/* Live provision_state of the Node via lookup. Empty when CR doesn't exist yet. */}}
{{- define "baremetal-host.provisionState" -}}
{{- $node := lookup (include "baremetal-host.nodeApiVersion" .) "Node" (include "baremetal-host.nodeNamespace" .) .Values.nodeName -}}
{{- if $node -}}{{- dig "status" "provision_state" "" $node -}}{{- end -}}
{{- end -}}

{{/* Live power_state of the Node via lookup. */}}
{{- define "baremetal-host.powerState" -}}
{{- $node := lookup (include "baremetal-host.nodeApiVersion" .) "Node" (include "baremetal-host.nodeNamespace" .) .Values.nodeName -}}
{{- if $node -}}{{- dig "status" "power_state" "" $node -}}{{- end -}}
{{- end -}}

{{/* inspection_finished_at via lookup — used to gate provide on completed inspect. */}}
{{- define "baremetal-host.inspectionFinishedAt" -}}
{{- $node := lookup (include "baremetal-host.nodeApiVersion" .) "Node" (include "baremetal-host.nodeNamespace" .) .Values.nodeName -}}
{{- if $node -}}{{- dig "status" "inspection_finished_at" "" $node -}}{{- end -}}
{{- end -}}

{{/* Translate spec.image into Ironic's instance_info shape. spec.image.root_device maps to
     instance_info.root_device, image.source -> image_source, etc. spec.networkData (the
     deployed OS's view of networking) flows to instance_info.network_data.
     configdrive is NOT here: it belongs on the provision-action body (NodeProvision.spec
     .configdrive, the canonical Ironic field per oas/ironic-provision.yaml), assembled by
     baremetal-host.configdrive. Setting instance_info.config_drive would be silently
     ignored — Ironic's deploy looks up node.instance_info.get('configdrive') (no underscore)
     but the recommended path is the provision body. */}}
{{- define "baremetal-host.instanceInfo" -}}
{{- $img := .Values.image | default dict -}}
{{- $ii := dict -}}
{{- with $img.source       }}{{ $_ := set $ii "image_source"   . }}{{ end -}}
{{- with $img.checksum     }}{{ $_ := set $ii "image_checksum" . }}{{ end -}}
{{- with $img.checksum_type}}{{ $_ := set $ii "image_checksum_algorithm" . }}{{ end -}}
{{- with $img.format       }}{{ $_ := set $ii "image_type" . }}{{ end -}}
{{- with $img.root_device  }}{{ $_ := set $ii "root_device" . }}{{ end -}}
{{- with .Values.networkData }}{{- if . }}{{ $_ := set $ii "network_data" . }}{{ end }}{{ end -}}
{{- toYaml $ii -}}
{{- end -}}

{{/* Assemble the Ironic configdrive dict from spec.configDrive. Empty (returns "") when none
     of metaData/userData/networkData is set, so callers can `if` on the result. Emits the
     canonical Ironic shape with snake_case keys: {meta_data, user_data, network_data} —
     this is the dict form documented for the provision PUT body (microversion >= 1.60).
     Used by transition-deploy.yaml. */}}
{{- define "baremetal-host.configdrive" -}}
{{- $cd := .Values.configDrive | default dict -}}
{{- if or $cd.metaData $cd.userData $cd.networkData -}}
{{- $out := dict "meta_data" ($cd.metaData | default dict) "user_data" ($cd.userData | default "") "network_data" ($cd.networkData | default dict) -}}
{{- toYaml $out -}}
{{- end -}}
{{- end -}}

{{/* v0.4.0: configdrive-drift detection enables single-apply redeploys.
     Hash the effective configdrive at chart-template time, stamp the
     hash as an annotation on the Node CR. On the next reconcile, the
     chart compares the freshly-rendered hash against the live Node
     annotation (via lookup); if they differ on an `active` node, the
     chart auto-fires transition-undeploy (= NodeProvision target=
     deleted). After Ironic walks the node back to `available`, the
     re-render proceeds normally through transition-deploy with the new
     configdrive. Single apply, chart drives the full redeploy cycle.
     Kills the "move entry between pots in the parent CR to choreograph
     a redeploy" anti-pattern. See feedback_no-yaml-choreography. */}}

{{- define "baremetal-host.configDriveHash" -}}
{{/* v0.4.2 (Task #91): hash is now passed by the parent chart (e.g.,
     kubernetes-cluster passes its .Chart.Version as
     `deployedConfigDriveHash` in BH spec). This breaks the recursive
     "hash includes hash" problem and lets the chart-rendered value
     match what cloud-init publishes to Ironic.extra. */}}
{{- .Values.deployedConfigDriveHash | default "" -}}
{{- end -}}

{{- define "baremetal-host.observedConfigDriveHash" -}}
{{/* v0.4.2 (Task #91): observed hash sourced from Node CR spec.extra.
     deployed_configdrive_hash — set by the bridge sidecar mirroring
     Ironic.node.extra (populated by cloud-init publish-deployed-hash.sh
     at end of runcmd). Empty if blade hasn't successfully completed
     cloud-init + publish yet. */}}
{{- $node := lookup (include "baremetal-host.nodeApiVersion" .) "Node" (include "baremetal-host.nodeNamespace" .) .Values.nodeName -}}
{{- if $node -}}{{- dig "spec" "extra" "deployed_configdrive_hash" "" $node -}}{{- end -}}
{{- end -}}

{{/* Returns "yes" when the Node is at provision_state=active AND the
     chart wants to deploy a configdrive (current hash non-empty) AND
     the observed annotation is empty OR differs. Triggers transition-
     undeploy to auto-fire without requiring spec.undeploy=true on the
     BH CR.

     The empty-observed branch is intentional: it covers the case where
     a node is at active with stale userData but never had the
     configdrive-hash annotation stamped (e.g. upgraded from v0.3.x, or
     the previous render was a recovery flow that emitted an empty
     configdrive). Without firing for empty-observed, those nodes would
     stay at active with stale userData forever, requiring operator
     choreography to redeploy.

     Safety: if the chart caller doesn't set spec.image.source, the
     `configdrive` helper still produces a body (just userData/network/
     metadata under spec.configDrive). So this gate only short-circuits
     when configDrive itself is empty (recovery-style chart instance) —
     in that case current is "" and the AND fails. */}}
{{- define "baremetal-host.shouldAutoRedeploy" -}}
{{- $state := include "baremetal-host.provisionState" . -}}
{{- $current := include "baremetal-host.configDriveHash" . -}}
{{- $observed := include "baremetal-host.observedConfigDriveHash" . -}}
{{/* v0.4.3 (Task #91): proper desired-state reconciliation, race-safe.
     Fire ONLY when observed is non-empty AND mismatches current. Empty
     observed means "cloud-init is still running, publish step at end
     of runcmd hasn't fired yet" — DON'T fire (would cycle the blade
     forever, since state=active triggers immediately after Ironic
     deploy but runcmd needs ~10min more to complete and publish).
     Tradeoff: a stale-active blade running pre-v0.12.3 userData (no
     publish script baked in) will never auto-redeploy. Operator
     bootstraps with explicit spec.undeploy=true once. After that,
     normal drift detection takes over.
     v0.4.5 (Task #100): also suppress while a recent undeploy
     NodeProvision exists — covers the boot-publish race where the
     blade just walked through undeploy → clean → deploy → active but
     publish-first hasn't replaced the stale observed yet. Without
     this, multiple blades cycling simultaneously can loop once each
     when the bh-chart's 60s reconcile catches them between Ironic-
     active and publish-completion. 10min suppression covers the
     normal deploy+clean cycle but lets genuine post-stable drift
     still fire. */}}
{{- $undeployName := printf "%s-undeploy" .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- $undeploy := lookup (include "baremetal-host.nodeApiVersion" .) "NodeProvision" .Release.Namespace $undeployName -}}
{{- $suppress := dict "yes" false -}}
{{- if $undeploy -}}
  {{- $created := dig "metadata" "creationTimestamp" "" $undeploy -}}
  {{- if $created -}}
    {{- $ageSec := sub (now | unixEpoch) (toDate "2006-01-02T15:04:05Z" $created | unixEpoch) -}}
    {{- if lt (int $ageSec) 600 -}}{{- $_ := set $suppress "yes" true -}}{{- end -}}
  {{- end -}}
{{- end -}}
{{- if and (eq $state "active") $current $observed (ne $current $observed) (not $suppress.yes) -}}yes{{- end -}}
{{- end -}}

{{/* v0.4.3 (Task #98): recover blades stuck in `wait call-back` for too
     long. Ironic powers the blade on, asks it to PXE-boot the IPA
     ramdisk, and waits for IPA to call back. If the chassis manager
     lies about PowerState, or the PXE NIC can't reach Ironic's TFTP,
     or the IPA boots but can't route back — Ironic sits in
     wait-callback indefinitely with last_error=null (no fault to
     report). Empirically: PUT target=deleted is accepted from
     wait-callback (per the v0.3.4 transition-undeploy widened gate)
     and walks the node through deleting → (cleaning|skip) → available.
     Then the chart's normal transition-deploy fires a fresh attempt.

     Threshold: 15 min. Covers a slow IPA download / DHCP retry on
     real PXE without false-firing on a normal first boot. After
     recovery + redeploy cycle (~10 min Ironic cleaning), if state is
     wait-callback AGAIN within the 15 min window of the BH CR's
     creation, this will NOT fire again until the BH ages past
     threshold (Helm uses BH.metadata.creationTimestamp which is
     stable across renders). Worst case: stuck hardware cycles every
     ~25-30 min; operator can mark spec.detached=true to suppress. */}}
{{- define "baremetal-host.shouldRecoverStuckCallback" -}}
{{- $state := include "baremetal-host.provisionState" . -}}
{{- if eq $state "wait call-back" -}}
  {{/* v0.4.4: anchor age on the Node CR's metadata.creationTimestamp.
       v0.4.3 mistakenly looked up sh.helm.release.v1.<release>.v1 — the
       FIRST helm revision Secret — but Helm prunes old revisions, so
       after ~10 cdc reconciles that Secret is gone and the helper
       silently never fires. The Node CR's creationTimestamp is stable
       across BH-chart helm revisions for the same node and resets
       cleanly on K8sCluster delete → reapply. */ -}}
  {{- $node := lookup (include "baremetal-host.nodeApiVersion" .) "Node" (include "baremetal-host.nodeNamespace" .) .Values.nodeName -}}
  {{- if $node -}}
    {{- $created := dig "metadata" "creationTimestamp" "" $node -}}
    {{- if $created -}}
      {{- $ageSec := sub (now | unixEpoch) (toDate "2006-01-02T15:04:05Z" $created | unixEpoch) -}}
      {{- if gt $ageSec 900 -}}yes{{- end -}}
    {{- end -}}
  {{- end -}}
{{- end -}}
{{- end -}}
