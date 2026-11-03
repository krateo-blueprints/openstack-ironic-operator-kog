{{- define "baremetal-host.nodeApiVersion" -}}baremetal.ogen.krateo.io/v1-0-0{{- end -}}
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
{{- $node := lookup (include "baremetal-host.nodeApiVersion" .) "BaremetalNode" (include "baremetal-host.nodeNamespace" .) .Values.nodeName -}}
{{- if $node -}}{{- dig "status" "provision_state" "" $node -}}{{- end -}}
{{- end -}}

{{/* inspection_finished_at via lookup — used to gate provide on completed inspect. */}}
{{- define "baremetal-host.inspectionFinishedAt" -}}
{{- $node := lookup (include "baremetal-host.nodeApiVersion" .) "BaremetalNode" (include "baremetal-host.nodeNamespace" .) .Values.nodeName -}}
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
     hash as an annotation on the BaremetalNode CR. On the next reconcile, the
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
{{/* v0.4.2 (Task #91): observed hash sourced from BaremetalNode CR spec.extra.
     deployed_configdrive_hash — set by the bridge sidecar mirroring
     Ironic.node.extra (populated by cloud-init publish-deployed-hash.sh
     at end of runcmd). Empty if blade hasn't successfully completed
     cloud-init + publish yet. */}}
{{- $node := lookup (include "baremetal-host.nodeApiVersion" .) "BaremetalNode" (include "baremetal-host.nodeNamespace" .) .Values.nodeName -}}
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
{{/* v0.6.0: THE desired provision target for this node — one value, computed
     from desired state and observed state, driving ONE NodeProvision CR.

     WHY THIS REPLACES SIX TEMPLATES. Each transition used to render its own CR
     (`-deploy` target=active, `-undeploy` target=deleted, plus manage/inspect/
     provide/clean), gated on the state it was trying to leave. cdc PATCHES what
     the chart renders and NEVER deletes what disappears from it, so a CR
     outlived the condition that produced it and then kept driving its old
     target. Measured on blade05: `-deploy` (target=active) and `-undeploy`
     (target=deleted) existed SIMULTANEOUSLY, rdc drove the node both ways, and
     it oscillated available -> deploying -> wait call-back -> available
     indefinitely. The `-undeploy` CR was absent from its release's rendered
     manifest across three successful helm upgrades and still present in the
     cluster.

     So the fix is not a better gate, it is removing the ability to hold two
     contradictory intents at once. One CR, one name, one target, always
     rendered — cdc patches it in place and there is never a stale sibling.

     ALWAYS RENDERED is load-bearing. A conditionally-rendered CR would be left
     behind holding its last target the moment the condition closed, which is
     precisely the bug. So every state maps to some valid target; where no
     transition is currently applicable the terminal intent is used and Ironic
     rejects it with a 409, which is noise rather than harm (22 such 409s
     measured tonight, all absorbed).

     stuckCallback is DELIBERATELY NOT CONSULTED. It judged "stuck" from the
     BaremetalNode CR's creationTimestamp — the age of the record, not of the
     attempt — so it was permanently true and tore down every deploy that
     reached `wait call-back`. 0.5.7 bounded it; it has no place in a computed
     target until it can be anchored on Ironic's `provision_updated_at`, which
     the CRD's status does not carry. Recovering a genuinely hung deploy is
     worth having and is NOT worth a predicate that fires on every healthy one. */}}
{{- define "baremetal-host.transitionTarget" -}}
{{- $state := include "baremetal-host.provisionState" . -}}
{{- $teardown := or .Values.undeploy (include "baremetal-host.shouldAutoRedeploy" .) -}}
{{- if $teardown -}}
deleted
{{- else if eq $state "enroll" -}}
manage
{{- else if eq $state "manageable" -}}
{{- if and .Values.enableInspection (not (include "baremetal-host.inspectionFinishedAt" .)) -}}
inspect
{{- else if .Values.cleanSteps -}}
clean
{{- else -}}
provide
{{- end -}}
{{- else -}}
{{- /* available -> active; and for transient or settled states (deploying,
       wait call-back, active, cleaning) `active` is the terminal intent and a
       no-op once reached. */ -}}
active
{{- end -}}
{{- end -}}

{{- define "baremetal-host.shouldRecoverStuckCallback" -}}
{{- $state := include "baremetal-host.provisionState" . -}}
{{- if eq $state "wait call-back" -}}
  {{/* v0.4.4: anchor age on the BaremetalNode CR's metadata.creationTimestamp.
       v0.4.3 mistakenly looked up sh.helm.release.v1.<release>.v1 — the
       FIRST helm revision Secret — but Helm prunes old revisions, so
       after ~10 cdc reconciles that Secret is gone and the helper
       silently never fires. The BaremetalNode CR's creationTimestamp is stable
       across BH-chart helm revisions for the same node and resets
       cleanly on K8sCluster delete → reapply. */ -}}
  {{- $node := lookup (include "baremetal-host.nodeApiVersion" .) "BaremetalNode" (include "baremetal-host.nodeNamespace" .) .Values.nodeName -}}
  {{- if $node -}}
    {{- $created := dig "metadata" "creationTimestamp" "" $node -}}
    {{- if $created -}}
      {{- $ageSec := sub (now | unixEpoch) (toDate "2006-01-02T15:04:05Z" $created | unixEpoch) -}}
      {{/* v0.5.7: suppress while a recent undeploy exists, exactly as
           shouldAutoRedeploy does, and for a sharper reason.

           THE ANCHOR BELOW IS WRONG and this guard is what makes it survivable.
           `$created` is the BaremetalNode CR's creationTimestamp -- the age of
           the RECORD, not of the current deploy attempt. v0.4.4 chose it
           because it is "stable across BH-chart helm revisions", which is true
           and is precisely the defect: once the CR is older than 900s, and it
           always is within fifteen minutes of first enrolment, this condition
           is PERMANENTLY TRUE. Every deploy transits `wait call-back`. So every
           deploy was being judged stuck the instant it started and torn back
           down:

             available -> deploy -> wait call-back -> undeploy -> available -> ...

           A node escaped only by racing `wait call-back` -> `active` between
           two 60s cdc reconciles. Measured on a six-node rebuild: five won the
           race, blade05 lost it nineteen times, and because load widens the
           window the whole thing reads as resource starvation. It is not. It is
           deterministic for any node whose record is a quarter of an hour old.

           The real fix is to anchor on how long the node has been IN this
           state -- Ironic's `provision_updated_at` -- which is not in the
           BaremetalNode CRD's status today and so needs a RestDefinition
           change. Until then this suppression bounds the damage to one
           undeploy per ten minutes instead of one per reconcile, which is
           enough to let a deploy finish. */}}
      {{- $undeployName := printf "%s-undeploy" .Release.Name | trunc 63 | trimSuffix "-" -}}
      {{- $recent := lookup (include "baremetal-host.nodeApiVersion" .) "NodeProvision" .Release.Namespace $undeployName -}}
      {{- $suppressed := false -}}
      {{- if $recent -}}
        {{- $uc := dig "metadata" "creationTimestamp" "" $recent -}}
        {{- if $uc -}}
          {{- $uAge := sub (now | unixEpoch) (toDate "2006-01-02T15:04:05Z" $uc | unixEpoch) -}}
          {{- if lt (int $uAge) 600 -}}{{- $suppressed = true -}}{{- end -}}
        {{- end -}}
      {{- end -}}
      {{- if and (gt $ageSec 900) (not $suppressed) -}}yes{{- end -}}
    {{- end -}}
  {{- end -}}
{{- end -}}
{{- end -}}
