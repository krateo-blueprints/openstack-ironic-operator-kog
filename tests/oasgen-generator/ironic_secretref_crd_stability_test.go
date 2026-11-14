package oas2jsonschema

// LIVES HERE, RUNS THERE. Copy into
//   <oasgen>/go/oasgen-provider/internal/tools/oas2jsonschema/
// and `go test . -run TestIronicSecretRef -v`. See README.md.
//
// ONE QUESTION: does adding the `secretRef` resolver to the shipped node definition
// change what oasgen GENERATES?
//
// Why it matters more than it sounds. Regenerating the node CRD unserves its previous
// version, and that wedges every kubernetes-cluster render here until the parent chart's
// pin moves -- measured twice in this repo (see the parent/child lockstep notes). So
// "does this regenerate" decides whether a security improvement is a one-line manifest
// change or a four-chart coordinated release.
//
// THE CLAIM BEING TESTED: it does not, because the resolver needed no OAS change.
// `NodeCreate.required` is `[driver]` only, so dropResolverSuppliedRequired has nothing
// to drop, and both `driver_info` and `extra` are `additionalProperties: true`, so the
// pointer carrier `spec.extra.bmc_secret` is expressible without declaring a property.
//
// TWO ARMS OVER ONE VARIABLE: the presence of the resolver entries. Everything else --
// the document, the verb set, the other mappings -- is the real shipped shape, read from
// the manifest rather than transcribed.
//
// AND A CONTROL, WHICH IS THE POINT. "The schemas are identical" and "the comparison
// cannot see a difference" are indistinguishable from the two arms alone, and byte
// equality is exactly the kind of result a collapsed instrument produces (two renders
// agreeing to the byte is a tell, not a finding). The control excludes `extra` from the
// spec and demands the bytes MOVE. It also tests this file's premise: if excluding
// `extra` changes nothing, then `extra` is not in the generated spec and the carrier
// path cannot work at all.

import (
	"bytes"
	"os"
	"path/filepath"
	"testing"

	"gopkg.in/yaml.v3"
)

const srIronicRepo = "/Users/diegobraga/openstack-ironic-operator"

type srMapping struct {
	InPath           string `yaml:"inPath"`
	InQuery          string `yaml:"inQuery"`
	InBody           string `yaml:"inBody"`
	InResponse       string `yaml:"inResponse"`
	InCustomResource string `yaml:"inCustomResource"`
	Resolver         *struct {
		Type string `yaml:"type"`
	} `yaml:"resolver"`
}

type srVerb struct {
	Action                 string      `yaml:"action"`
	Method                 string      `yaml:"method"`
	Path                   string      `yaml:"path"`
	IdentifiersMatchPolicy string      `yaml:"identifiersMatchPolicy"`
	FieldMapping           []srMapping `yaml:"fieldMapping"`
	RequestFieldMapping    []srMapping `yaml:"requestFieldMapping"`
}

type srDoc struct {
	Spec struct {
		Resource struct {
			Kind                   string   `yaml:"kind"`
			Identifiers            []string `yaml:"identifiers"`
			AdditionalStatusFields []string `yaml:"additionalStatusFields"`
			VerbsDescription       []srVerb `yaml:"verbsDescription"`
		} `yaml:"resource"`
	} `yaml:"spec"`
}

// loadNodeForSecretRef reads the REAL shipped node definition. withResolver=false drops
// every resolver-bearing entry, which is the only variable between the two arms.
func loadNodeForSecretRef(t *testing.T, withResolver bool) (*ResourceConfig, []byte) {
	t.Helper()
	rdPath := filepath.Join(srIronicRepo, "manifests", "restdefinition-node.yaml")
	raw, err := os.ReadFile(rdPath)
	if err != nil {
		t.Fatalf("VOID: cannot read %s: %v", rdPath, err)
	}
	var d srDoc
	if err := yaml.Unmarshal(raw, &d); err != nil {
		t.Fatalf("VOID: cannot parse %s: %v", rdPath, err)
	}
	res := d.Spec.Resource
	if len(res.VerbsDescription) == 0 {
		t.Fatalf("VOID: the node definition declares no verbs")
	}

	cfg := &ResourceConfig{
		Identifiers:            res.Identifiers,
		AdditionalStatusFields: res.AdditionalStatusFields,
	}
	resolverEntries := 0
	for _, v := range res.VerbsDescription {
		verb := Verb{
			Action: v.Action, Method: v.Method, Path: v.Path,
			IdentifiersMatchPolicy: v.IdentifiersMatchPolicy,
		}
		// Both lists feed the same generator type: oasgen treats each
		// requestFieldMapping entry "as an equivalent request-direction FieldMappingItem
		// at load time" (types.go:591-593). Reading only one would silently drop the
		// mappings the other verbs here still use.
		for _, m := range append(append([]srMapping{}, v.FieldMapping...), v.RequestFieldMapping...) {
			rt := ""
			if m.Resolver != nil {
				rt = m.Resolver.Type
			}
			if rt != "" {
				resolverEntries++
				if !withResolver {
					continue
				}
			}
			verb.FieldMapping = append(verb.FieldMapping, FieldMappingEntry{
				InPath: m.InPath, InQuery: m.InQuery, InBody: m.InBody,
				InResponse: m.InResponse, InCustomResource: m.InCustomResource,
				ResolverType: rt,
			})
		}
		cfg.Verbs = append(cfg.Verbs, verb)
	}

	// THE ARMS MUST ACTUALLY DIFFER. If the manifest carries no resolver, both arms are
	// the same input and byte equality below would be a tautology -- the shape that let a
	// sibling consumer's screen pass while testing nothing.
	if resolverEntries == 0 {
		t.Fatalf("VOID: the shipped node definition declares NO resolver entries, so the " +
			"two arms are identical inputs and this test would assert nothing")
	}

	oasPath := filepath.Join(srIronicRepo, "oas", "ironic-node.yaml")
	spec, err := os.ReadFile(oasPath)
	if err != nil {
		t.Fatalf("VOID: cannot read %s: %v", oasPath, err)
	}
	return cfg, spec
}

func srSpecSchema(t *testing.T, cfg *ResourceConfig, spec []byte) []byte {
	t.Helper()
	doc, err := NewLibOASParser().Parse(spec)
	if err != nil {
		t.Fatalf("VOID: parsing the node OAS failed: %v", err)
	}
	res, err := NewOASSchemaGenerator(doc, DefaultGeneratorConfig(), cfg).Generate()
	if err != nil {
		t.Fatalf("VOID: generation failed: %v", err)
	}
	if len(res.SpecSchema) == 0 {
		t.Fatalf("VOID: the generated spec schema is EMPTY, so comparing it proves nothing")
	}
	return res.SpecSchema
}

func TestIronicSecretRefDoesNotRegenerateTheNodeCRD(t *testing.T) {
	withCfg, spec := loadNodeForSecretRef(t, true)
	withoutCfg, _ := loadNodeForSecretRef(t, false)

	a := srSpecSchema(t, withCfg, spec)
	b := srSpecSchema(t, withoutCfg, spec)

	if !bytes.Equal(a, b) {
		t.Errorf("the secretRef resolver CHANGED the generated spec schema (%d vs %d bytes).\n"+
			"That means adding it regenerates the node CRD, which unserves the previous\n"+
			"version and wedges every kubernetes-cluster render until the parent pin moves.\n"+
			"This change can no longer ship as a manifest-only edit.\nwith:    %s\nwithout: %s",
			len(a), len(b), string(a), string(b))
		return
	}
	t.Logf("spec schema IDENTICAL with and without the resolver (%d bytes) -- "+
		"no CRD regeneration, so no chart lockstep", len(a))
}

// THE CONTROL. Without this, the test above passes against any comparison that cannot
// see a difference -- and it also pins the premise that makes the carrier work at all.
func TestIronicSecretRefCRDStabilityControl(t *testing.T) {
	cfg, spec := loadNodeForSecretRef(t, true)
	base := srSpecSchema(t, cfg, spec)

	excluded, _ := loadNodeForSecretRef(t, true)
	excluded.ExcludedSpecFields = []string{"extra"}
	moved := srSpecSchema(t, excluded, spec)

	if bytes.Equal(base, moved) {
		t.Fatalf("CONTROL FAILED: excluding `extra` from the spec did not change the generated\n"+
			"schema (%d bytes both). Two things are now unproven: that this comparison can\n"+
			"detect ANY change, and that `extra` is in the generated spec at all -- if it is\n"+
			"not, the carrier path spec.extra.bmc_secret cannot resolve and the resolver is\n"+
			"dead on arrival.", len(base))
	}
	t.Logf("CONTROL ok: excluding `extra` moved the schema (%d -> %d bytes), so the comparison "+
		"can see a difference AND `extra` really is a generated spec property", len(base), len(moved))
}
