{{/* DO NOT "FIX" spec.node_id IN THE transition-*.yaml TEMPLATES. Measured 2026-11-04.

     All seven transition templates set `spec.node_id`, and the generated
     NodeProvision / NodePower CRDs HAVE NO SUCH PROPERTY. Their served spec is
     `{clean_steps, configdrive, configurationRef, target}` and
     `{configurationRef, target, timeout}` respectively. A CR that sets it is rejected
     under fieldValidation=Strict ("strict decoding error: unknown field
     spec.node_id") and silently PRUNED otherwise — which is the path helm takes, so
     nothing has ever reported it.

     The cause is upstream and confirmed by the oasgen-provider maintainers: the
     `requestFieldMapping` that redirects the path parameter (`node_id <- spec.node_id`)
     also SUPPRESSES the spec property, because the suppression is meant to stop an
     author being asked for a field the controller does not read from them — and it
     deletes the property the mapping reads. BaremetalNode escapes it only because its
     mapping is `uuid <- spec.name`, where the two names differ, so both survive.

     AND YET the state machine works: six blades walked available -> active through
     these exact kinds. So the node identity is arriving from somewhere other than
     `spec.node_id`, and nobody yet knows where — oasgen is tracing it. Until that lands,
     these templates stay as written. Removing `node_id` would be removing a field that
     is already being dropped, which changes nothing; "correcting" it to some other
     source would be guessing at a mechanism we have not identified. Leave it.

     I also briefly reported a `requestFieldMapping` / `altFieldMapping` rename hazard
     here. There is none — `altFieldMapping` does not exist; it was a grep artifact of
     mine. The manifests in manifests/ are safe to apply.

     UPDATE 2026-11-04, and this is the operational half: THESE TRANSITIONS ARE
     LIKELY BROKEN AS OF THE rdc 0.47.0 -> 0.50.0 UPGRADE, LATENTLY.

     oasgen-provider traced the parameter path at HEAD. There are exactly four
     writers of a path parameter, in precedence order: fieldMapping >
     requestFieldMapping > Configuration CR > spec > status. Ours is a
     `requestFieldMapping` reading `spec.node_id`, which is pruned on write, so it
     finds nothing and SILENTLY SKIPS (builder.go:215-217, `if err != nil || !found
     { continue }` — no parameter, no error, no log line). An unresolved path
     parameter is then REFUSED by a guard added in efbf36b (#363, 2026-11-01), first
     released in 0.48.0.

     Why the cluster nonetheless exists: it was provisioned on 2026-11-03 under rdc
     0.47.0, which predates that guard by two releases. KRATEO_RDC_VERSION moved to
     0.50.0 on 2026-11-04 (commit b331b80) and the provision controller has
     reconciled nothing since. So the six successful transitions and the code that
     says they should fail are three releases apart.

     WHAT THIS MEANS: the next time any node needs to deploy, undeploy, power-cycle
     or clean, expect the request to be refused with `unresolved path parameter(s)
     [node_id]` in the rest-dynamic-controller log. If you are reading this because
     a transition stopped working, that is why — it is not your CR and not Ironic.

     DO NOT "fix" it by guessing a different source. `status.node_id` is a dead end
     by design: populateStatusFields reads only a response body, our findby items
     carry no node_id, and the PUT returns 202 with no body — the generator already
     warns `status field 'node_id' not found in response`. The open question, with
     oasgen, is what resolved {node_id} under 0.47.0 and what removed it; the answer
     decides the fix. */}}
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
{{/* v0.6.2: see the comment in configdrive. Called from node.yaml too, because
     node.yaml renders unconditionally while configdrive is only reached through
     the state-gated deploy template -- so a guard living only there cannot be
     exercised by `helm template`, which is exactly how the broken user-data
     shipped in the first place. */}}
{{- define "baremetal-host.validateConfigDrive" -}}
{{- $cd := .Values.configDrive | default dict -}}
{{- if $cd.userData -}}
{{- $parsed := fromYaml $cd.userData -}}
{{- if hasKey $parsed "Error" -}}
{{- fail (printf "configDrive.userData for node %s is not valid YAML, so cloud-init would silently discard ALL of it and boot an inert machine that still reports `active`. Parser said: %s" (.Values.nodeName | default "<unnamed>") (get $parsed "Error")) -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "baremetal-host.configdrive" -}}
{{- $cd := .Values.configDrive | default dict -}}
{{/* v0.6.2: FAIL THE RENDER if userData is not parseable YAML.

     cloud-init silently discards user-data it cannot parse. The network stage
     still runs (network_data is a separate document), so the machine boots,
     gets its NICs, answers SSH, and Ironic reports `active` -- while no
     ssh_pwauth, no write_files and no runcmd were ever applied. Every layer
     reports success and the machine does nothing.

     Measured: kubernetes-cluster 0.13.9 added a multi-line `python3 -c`
     snippet to resolve-cp-endpoint.sh whose continuation lines sat at column 3
     and so dedented out of the enclosing `content: |` block scalar. That
     invalidated the WHOLE cloud-config:

       ParserError ... expected <block end>, but found '?'
       in "<unicode string>", line 903, column 3:   try:

     49,704 bytes of user-data, attached to the node, entirely inert. It cost
     two full six-node rebuilds to find, because nothing anywhere reports it.

     A render that fails is recoverable in seconds. A deploy that boots an
     inert machine is not distinguishable from success at any layer. */}}
{{- include "baremetal-host.validateConfigDrive" . -}}
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
