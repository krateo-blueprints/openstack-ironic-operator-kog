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

// srSchemas returns the generated SPEC and STATUS schemas.
//
// IT RETURNED ONLY THE SPEC UNTIL 2026-11-14, WHICH WAS THE DEFECT THIS FILE WARNED ABOUT
// IN ITS OWN README. oasgen#646 declares `status.resolvedResolversDigest` gated on the
// update verb carrying a secretRef -- a STATUS-side change. A spec-only comparison is blind
// to it, so this suite would have gone on reporting "no regeneration" at a version where
// the node CRD does in fact regenerate. The note predicting that was written before the
// tag existed; this is the fix, applied before the prediction could come true in use.
func srSchemas(t *testing.T, cfg *ResourceConfig, spec []byte) (specSchema, statusSchema []byte) {
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
	return res.SpecSchema, res.StatusSchema
}

const resolverDigestField = "resolvedResolversDigest"

func TestIronicSecretRefDoesNotRegenerateTheNodeCRD(t *testing.T) {
	withCfg, spec := loadNodeForSecretRef(t, true)
	withoutCfg, _ := loadNodeForSecretRef(t, false)

	specA, statusA := srSchemas(t, withCfg, spec)
	specB, statusB := srSchemas(t, withoutCfg, spec)

	// THE SPEC SIDE. Unchanged at every version so far, and for a reason that does not
	// depend on the version: NodeCreate.required is [driver], so dropResolverSuppliedRequired
	// has nothing to drop, and the carrier rides in a free-form `extra`.
	if !bytes.Equal(specA, specB) {
		t.Errorf("the resolver CHANGED the generated SPEC schema (%d vs %d bytes), so adding "+
			"it regenerates the node CRD and can no longer ship as a manifest-only edit.\n"+
			"with:    %s\nwithout: %s", len(specA), len(specB), string(specA), string(specB))
	} else {
		t.Logf("SPEC schema identical with and without the resolver (%d bytes)", len(specA))
	}

	// THE STATUS SIDE, which is where #646 lands and which this test used not to read.
	//
	// The assertion is on the MECHANISM rather than on a version, so it holds either side
	// of #646: the digest field is gated on the update verb carrying a secretRef, so it must
	// NEVER appear in the without-resolver arm. If it appears in the with-resolver arm, the
	// status schemas must differ -- and that difference IS the CRD regeneration this repo
	// pays for adopting a resolver.
	hasA := bytes.Contains(statusA, []byte(resolverDigestField))
	hasB := bytes.Contains(statusB, []byte(resolverDigestField))

	if hasB {
		t.Errorf("UNGATED: %q appears in the status schema of the arm with NO resolver. "+
			"#646 declares it gated on the update verb carrying a secretRef, so this means "+
			"the gate is not holding and every consumer gets the field.\nwithout: %s",
			resolverDigestField, string(statusB))
	}

	switch {
	case hasA && bytes.Equal(statusA, statusB):
		t.Errorf("INCOHERENT: %q is present with the resolver yet the status schemas are "+
			"byte-identical (%d bytes). One of the two reads is wrong.",
			resolverDigestField, len(statusA))
	case hasA:
		t.Logf("STATUS schema MOVED: %q present with the resolver, absent without "+
			"(%d vs %d bytes). This oasgen build contains #646, so adopting the resolver "+
			"REGENERATES the node CRD -- the unserved-version wedge is owed here, and is "+
			"deferred only by choosing when to turn regeneration back on. Expected, not a "+
			"regression.", resolverDigestField, len(statusA), len(statusB))
	case bytes.Equal(statusA, statusB):
		t.Logf("STATUS schema identical (%d bytes) and %q absent from both -- this oasgen "+
			"build predates #646, so the change is still manifest-only here.",
			len(statusA), resolverDigestField)
	default:
		t.Errorf("the status schemas differ (%d vs %d bytes) but %q is in NEITHER, so "+
			"something other than #646 moved and this test cannot say what.\n"+
			"with:    %s\nwithout: %s", len(statusA), len(statusB), resolverDigestField,
			string(statusA), string(statusB))
	}
}

// THE CONTROL. Without this, the test above passes against any comparison that cannot
// see a difference -- and it also pins the premise that makes the carrier work at all.
func TestIronicSecretRefCRDStabilityControl(t *testing.T) {
	cfg, spec := loadNodeForSecretRef(t, true)
	base, _ := srSchemas(t, cfg, spec)

	excluded, _ := loadNodeForSecretRef(t, true)
	excluded.ExcludedSpecFields = []string{"extra"}
	moved, _ := srSchemas(t, excluded, spec)

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
