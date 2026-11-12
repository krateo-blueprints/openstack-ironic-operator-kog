package oas2jsonschema

// Measures ONE thing, on MY document, at whatever version this worktree is:
// does the generated spec carry `node_id`, the path parameter that
// restdefinition-power.yaml maps SELF-REFERENTIALLY from spec.node_id?
//
// This is the question PR #49 in openstack-ironic-operator-kog is held on. It is a
// claim about the GENERATOR, so the generator is the right instrument -- the live
// CRD answers a different question (what is STORED, which may be stale).

import (
	"encoding/json"
	"os"
	"testing"
)

func TestIronicPowerNodeIDSpecField(t *testing.T) {
	spec, err := os.ReadFile("/Users/diegobraga/openstack-ironic-operator/oas/ironic-power.yaml")
	if err != nil {
		t.Fatalf("VOID: cannot read the OAS: %v", err)
	}
	doc, err := NewLibOASParser().Parse(spec)
	if err != nil {
		t.Fatalf("VOID: parse failed: %v", err)
	}

	// restdefinition-power.yaml, verbatim: node_id <- spec.node_id on get/create/update.
	selfref := []FieldMappingEntry{{InPath: "node_id", InCustomResource: "spec.node_id"}}
	gen := NewOASSchemaGenerator(doc, DefaultGeneratorConfig(), &ResourceConfig{
		Verbs: []Verb{
			{Action: "findby", Method: "GET", Path: "/v1/nodes"},
			{Action: "get", Method: "GET", Path: "/v1/nodes/{node_id}", FieldMapping: selfref},
			{Action: "create", Method: "PUT", Path: "/v1/nodes/{node_id}/states/power", FieldMapping: selfref},
			{Action: "update", Method: "PUT", Path: "/v1/nodes/{node_id}/states/power", FieldMapping: selfref},
		},
		Identifiers:            []string{"node_id"},
		AdditionalStatusFields: []string{"power_state", "target_power_state", "uuid"},
	})

	raw, warnings, err := gen.BuildSpecSchema()
	if err != nil {
		t.Fatalf("VOID: BuildSpecSchema failed: %v", err)
	}
	var out map[string]any
	if err := json.Unmarshal(raw, &out); err != nil {
		t.Fatalf("VOID: unmarshal failed: %v", err)
	}
	props, _ := out["properties"].(map[string]any)

	keys := make([]string, 0, len(props))
	for k := range props {
		keys = append(keys, k)
	}
	_, hasNodeID := props["node_id"]
	_, hasTarget := props["target"] // CONTROL: declared in the PowerTarget body, must be present

	t.Logf("RESULT  spec.node_id = %v", map[bool]string{true: "PRESENT", false: "ABSENT"}[hasNodeID])
	t.Logf("CONTROL spec.target  = %v  (body-declared; ABSENT here means the harness is broken)",
		map[bool]string{true: "PRESENT", false: "ABSENT"}[hasTarget])
	t.Logf("all spec properties: %v", keys)
	t.Logf("warnings: %d %v", len(warnings), warnings)

	if !hasTarget {
		t.Fatalf("CONTROL FAILED: spec.target absent, so this measurement is VOID not a result")
	}
}
