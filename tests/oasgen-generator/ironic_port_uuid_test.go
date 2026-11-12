package oas2jsonschema

// Is spec.uuid on Port rescued by its UNMAPPED get verb, even though its delete
// maps uuid <- status.uuid? The live CRD says PRESENT and required at 0.59.0, which
// contradicts my screen's KNOWN entry. Measure the generator rather than infer.
import (
	"encoding/json"
	"os"
	"testing"
)

func portGen(t *testing.T, verbs []Verb) map[string]any {
	t.Helper()
	spec, err := os.ReadFile("/Users/diegobraga/openstack-ironic-operator/oas/ironic-port.yaml")
	if err != nil {
		t.Fatalf("VOID: %v", err)
	}
	doc, err := NewLibOASParser().Parse(spec)
	if err != nil {
		t.Fatalf("VOID: %v", err)
	}
	raw, _, err := NewOASSchemaGenerator(doc, DefaultGeneratorConfig(), &ResourceConfig{
		Verbs: verbs, Identifiers: []string{"address"},
		AdditionalStatusFields: []string{"node_uuid", "uuid"},
	}).BuildSpecSchema()
	if err != nil {
		t.Fatalf("VOID: %v", err)
	}
	var out map[string]any
	if err := json.Unmarshal(raw, &out); err != nil {
		t.Fatalf("VOID: %v", err)
	}
	return out
}

func report(t *testing.T, label string, out map[string]any) {
	props, _ := out["properties"].(map[string]any)
	req, _ := out["required"].([]any)
	isReq := false
	for _, r := range req {
		if r == "uuid" {
			isReq = true
		}
	}
	_, has := props["uuid"]
	_, ctrl := props["address"]
	t.Logf("%-36s spec.uuid=%-7v required=%-5v  CONTROL spec.address=%v",
		label, map[bool]string{true: "PRESENT", false: "ABSENT"}[has], isReq,
		map[bool]string{true: "PRESENT", false: "ABSENT -- BROKEN"}[ctrl])
	if !ctrl {
		t.Fatalf("CONTROL FAILED for %s", label)
	}
}

func TestIronicPortUUIDSurvival(t *testing.T) {
	del := []FieldMappingEntry{{InPath: "uuid", InCustomResource: "status.uuid"}}
	report(t, "A real shipped shape (get UNMAPPED)", portGen(t, []Verb{
		{Action: "findby", Method: "GET", Path: "/v1/ports"},
		{Action: "get", Method: "GET", Path: "/v1/ports/{uuid}"},
		{Action: "create", Method: "POST", Path: "/v1/ports"},
		{Action: "delete", Method: "DELETE", Path: "/v1/ports/{uuid}", FieldMapping: del},
	}))
	report(t, "B DISCRIMINATOR: get ALSO mapped", portGen(t, []Verb{
		{Action: "findby", Method: "GET", Path: "/v1/ports"},
		{Action: "get", Method: "GET", Path: "/v1/ports/{uuid}", FieldMapping: del},
		{Action: "create", Method: "POST", Path: "/v1/ports"},
		{Action: "delete", Method: "DELETE", Path: "/v1/ports/{uuid}", FieldMapping: del},
	}))
}
