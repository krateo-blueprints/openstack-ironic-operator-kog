package oas2jsonschema

import "testing"

// The four openstack-ironic-operator RestDefinitions that set
// excludedSpecFields, with their REAL verb mappings as shipped.
//
// Contributed as the over-eagerness arm for oasgen #455 / PR #456. These
// definitions are NOT the motivating case: every one supplies the excluded
// required parameter through a request-direction mapping, so the PRE-EXISTING
// mappingSupplied exemption already covers them and a fresh 0.51.0 install
// generates them correctly -- measured on a live cluster (ports CRD uid
// 3fc83f7d, created after the 0.51.0 upgrade: `uuid` absent from properties
// AND required).
//
// That is exactly why they belong here. #456's only real risk is exempting too
// much, and these are four real documents whose behaviour must NOT change. An
// arm over a case that already looks correct is the one most likely to be
// skipped.
func ironicDefinitions() []struct {
	name     string
	resource *ResourceConfig
} {
	return []struct {
		name     string
		resource *ResourceConfig
	}{
		{"ports", &ResourceConfig{
			ExcludedSpecFields:     []string{"uuid"},
			AdditionalStatusFields: []string{"uuid", "node_uuid"},
			Identifiers:            []string{"address"},
			Verbs: []Verb{
				{Action: "findby", Method: "GET", Path: "/v1/ports"},
				{Action: "create", Method: "POST", Path: "/v1/ports"},
				{Action: "get", Method: "GET", Path: "/v1/ports/{uuid}"},
				{Action: "delete", Method: "DELETE", Path: "/v1/ports/{uuid}",
					FieldMapping: []FieldMapping{{InPath: "uuid", InCustomResource: "status.uuid"}}},
			}}},
		{"portgroups", &ResourceConfig{
			ExcludedSpecFields: []string{"uuid"},
			Identifiers:        []string{"name"},
			Verbs: []Verb{
				{Action: "get", Method: "GET", Path: "/v1/portgroups/{uuid}",
					FieldMapping: []FieldMapping{{InPath: "uuid", InCustomResource: "spec.name"}}},
				{Action: "delete", Method: "DELETE", Path: "/v1/portgroups/{uuid}",
					FieldMapping: []FieldMapping{{InPath: "uuid", InCustomResource: "spec.name"}}},
			}}},
		{"allocations", &ResourceConfig{
			ExcludedSpecFields: []string{"uuid"},
			Identifiers:        []string{"name"},
			Verbs: []Verb{
				{Action: "get", Method: "GET", Path: "/v1/allocations/{uuid}",
					FieldMapping: []FieldMapping{{InPath: "uuid", InCustomResource: "spec.name"}}},
				{Action: "delete", Method: "DELETE", Path: "/v1/allocations/{uuid}",
					FieldMapping: []FieldMapping{{InPath: "uuid", InCustomResource: "spec.name"}}},
			}}},
		{"deploytemplates", &ResourceConfig{
			ExcludedSpecFields: []string{"uuid"},
			Identifiers:        []string{"name"},
			Verbs: []Verb{
				{Action: "get", Method: "GET", Path: "/v1/deploy_templates/{uuid}",
					FieldMapping: []FieldMapping{{InPath: "uuid", InCustomResource: "spec.name"}}},
				{Action: "delete", Method: "DELETE", Path: "/v1/deploy_templates/{uuid}",
					FieldMapping: []FieldMapping{{InPath: "uuid", InCustomResource: "spec.name"}}},
			}}},
	}
}

// Each definition's excluded `uuid` must be recognised as mapping-supplied.
// That is what keeps it out of requiredParameters, which is what keeps the
// #410 guard from refusing the exclusion.
func TestIronicDefinitionsAreMappingSupplied(t *testing.T) {
	for _, d := range ironicDefinitions() {
		t.Run(d.name, func(t *testing.T) {
			supplied := mappingSuppliedParams(d.resource.Verbs)
			if _, ok := supplied["uuid"]; !ok {
				t.Fatalf("'uuid' is not recognised as mapping-supplied for %s. It would then enter "+
					"requiredParameters, the #410 guard would refuse the exclusion, and every CR of "+
					"this kind with an empty spec.uuid becomes invalid -- 12 of them are live.",
					d.name)
			}
		})
	}
}

// With `uuid` exempt, excluding it must be HONOURED and must remove only it.
func TestIronicDefinitionsExcludeUUIDCleanly(t *testing.T) {
	for _, d := range ironicDefinitions() {
		t.Run(d.name, func(t *testing.T) {
			g := &OASSchemaGenerator{
				generatorConfig: DefaultGeneratorConfig(),
				resourceConfig:  d.resource,
			}
			schema := specShape("uuid", "address")
			// uuid is mapping-supplied, so it never reaches requiredParameters.
			warnings := g.removeExcludedSpecFields(schema, map[string]bool{})
			if len(warnings) != 0 {
				t.Errorf("warnings = %v, want none", warnings)
			}
			if hasProp(schema, "uuid") {
				t.Error("'uuid' survived the exclusion -- the vendor assigns it and no author can " +
					"supply it, so the CR would be rejected for omitting a value it must not set")
			}
			if !hasProp(schema, "address") {
				t.Error("'address' was removed; only the excluded field should go")
			}
		})
	}
}

// THE CONTROL. #456 must not exempt so much that #410 re-opens: a required
// parameter that NOTHING supplies must still be refused, and must survive in
// the spec. Without this arm, "uuid disappears" above is satisfied by a fix
// that simply stopped guarding anything.
func TestARequiredParameterNothingSuppliesIsStillRefused(t *testing.T) {
	g := &OASSchemaGenerator{
		generatorConfig: DefaultGeneratorConfig(),
		resourceConfig: &ResourceConfig{
			ExcludedSpecFields: []string{"uuid"},
			Identifiers:        []string{"address"},
			// No mapping, no additionalStatusFields: nothing can supply it.
			Verbs: []Verb{{Action: "get", Method: "GET", Path: "/v1/ports/{uuid}"}},
		},
	}
	schema := specShape("uuid", "address")
	warnings := g.removeExcludedSpecFields(schema, map[string]bool{"uuid": true})
	if len(warnings) == 0 {
		t.Error("no warning: an unsatisfiable exclusion was accepted silently, so #410 has re-opened")
	}
	if !hasProp(schema, "uuid") {
		t.Error("'uuid' was removed although nothing supplies it -- the kind cannot build its own URL")
	}
}
