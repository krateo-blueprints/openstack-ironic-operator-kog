package oas2jsonschema

// LIVES HERE, RUNS THERE. Copy into
//   <oasgen>/go/oasgen-provider/internal/tools/oas2jsonschema/
// and `go test . -run TestIronicActionOnly -v`. See README.md.
//
// The regression arms oasgen asked this repo for, against oasgen-provider#625/#629.
//
// #625: a generation warning tells authors to make findby match on a definition where
// a never-matching findby is the OPERATING MECHANISM. Acting on it disabled every
// bare-metal provision transition here for 24 hours.
// #629: the inverse, and the worse half -- when such a definition's findby CAN match,
// its create is unreachable and nothing warns at all.
//
// TWO ARMS OVER ONE VARIABLE: the verb set.
//
//	ironic-node-provision   findby + create, no get, no update   -> MUST NOT be advised
//	ironic-node-power       findby + get + create + update       -> MUST still be advised
//
// The control is the point. "The warning is suppressed" and "the warning stopped
// working" are indistinguishable from the first arm alone, and a fix that silenced the
// diagnostic everywhere would pass it.
//
// ASSERTIONS ARE ON WHAT THE AUTHOR IS TOLD, not on internal codes -- oasgen's own
// request. A reworded message still carrying the wrong advice must fail these arms.
//
// THE DEFINITIONS ARE READ FROM THE REAL MANIFESTS, not transcribed. A sibling
// consumer's port of a different screen was defeated by exactly that: their fixture
// drifted from the shipped shape, so the arm passed while testing nothing.

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"gopkg.in/yaml.v3"
)

const ironicRepo = "/Users/diegobraga/openstack-ironic-operator"

type rdVerb struct {
	Action                 string `yaml:"action"`
	Method                 string `yaml:"method"`
	Path                   string `yaml:"path"`
	IdentifiersMatchPolicy string `yaml:"identifiersMatchPolicy"`
	RequestFieldMapping    []struct {
		InPath           string `yaml:"inPath"`
		InBody           string `yaml:"inBody"`
		InQuery          string `yaml:"inQuery"`
		InCustomResource string `yaml:"inCustomResource"`
	} `yaml:"requestFieldMapping"`
}

type rdDoc struct {
	Spec struct {
		Resource struct {
			Kind                   string   `yaml:"kind"`
			Identifiers            []string `yaml:"identifiers"`
			AdditionalStatusFields []string `yaml:"additionalStatusFields"`
			VerbsDescription       []rdVerb `yaml:"verbsDescription"`
		} `yaml:"resource"`
	} `yaml:"spec"`
}

// loadIronic builds a ResourceConfig from a shipped RestDefinition and returns it with
// its OAS bytes. slug is the manifest suffix: "provision" -> manifests/restdefinition-provision.yaml
// and oas/ironic-provision.yaml.
func loadIronic(t *testing.T, slug string) (*ResourceConfig, []byte) {
	t.Helper()
	rdPath := filepath.Join(ironicRepo, "manifests", "restdefinition-"+slug+".yaml")
	raw, err := os.ReadFile(rdPath)
	if err != nil {
		t.Fatalf("VOID: cannot read %s: %v", rdPath, err)
	}
	var d rdDoc
	if err := yaml.Unmarshal(raw, &d); err != nil {
		t.Fatalf("VOID: cannot parse %s: %v", rdPath, err)
	}
	res := d.Spec.Resource
	if len(res.VerbsDescription) == 0 {
		t.Fatalf("VOID: %s declares no verbs, so this arm would assert nothing", slug)
	}
	cfg := &ResourceConfig{
		Identifiers:            res.Identifiers,
		AdditionalStatusFields: res.AdditionalStatusFields,
	}
	for _, v := range res.VerbsDescription {
		verb := Verb{
			Action: v.Action, Method: v.Method, Path: v.Path,
			IdentifiersMatchPolicy: v.IdentifiersMatchPolicy,
		}
		for _, m := range v.RequestFieldMapping {
			verb.FieldMapping = append(verb.FieldMapping, FieldMappingEntry{
				InPath: m.InPath, InBody: m.InBody, InQuery: m.InQuery,
				InCustomResource: m.InCustomResource,
			})
		}
		cfg.Verbs = append(cfg.Verbs, verb)
	}
	oasPath := filepath.Join(ironicRepo, "oas", "ironic-"+slug+".yaml")
	spec, err := os.ReadFile(oasPath)
	if err != nil {
		t.Fatalf("VOID: cannot read %s: %v", oasPath, err)
	}
	return cfg, spec
}

func ironicWarningsFor(t *testing.T, slug string) []string {
	t.Helper()
	cfg, spec := loadIronic(t, slug)
	doc, err := NewLibOASParser().Parse(spec)
	if err != nil {
		t.Fatalf("VOID: parsing the %s OAS failed: %v", slug, err)
	}
	gen := NewOASSchemaGenerator(doc, DefaultGeneratorConfig(), cfg)

	// Generate(), NOT BuildSpecSchema(). The first version of this file called the
	// latter and both arms reported ZERO warnings -- the identifier-matchability
	// warning is appended by Generate (generator.go:103), one stage further out, so
	// the arm I cared about was passing because nothing had been measured. The
	// CONTROL is what said so: it demands the warning be PRESENT on the non-action-
	// only kind, and it failed.
	res, err := gen.Generate()
	if err != nil {
		t.Fatalf("VOID: generation failed for %s: %v", slug, err)
	}
	all := append(append([]error{}, res.GenerationWarnings...), res.ValidationWarnings...)
	out := make([]string, 0, len(all))
	for _, w := range all {
		if w != nil {
			out = append(out, w.Error())
		}
	}
	return out
}

// advisesAgainstNotFoundCreate reports whether any warning tells the author to make
// findby match, or frames "the reconciler creates on not-found" as a fault.
//
// Matched on ADVICE, deliberately. The exact sentence has already been reworded once
// upstream, and a check keyed to a code or to one phrasing would report success against
// a message that still gives the harmful instruction.
func advisesAgainstNotFoundCreate(warnings []string) (bool, string) {
	for _, w := range warnings {
		l := strings.ToLower(strings.Join(strings.Fields(w), " "))
		switch {
		case strings.Contains(l, "acts on not-found by creating"),
			strings.Contains(l, "acts on not-found by create"),
			strings.Contains(l, "reconciler acts on by creating"),
			strings.Contains(l, "remove ") && strings.Contains(l, "from identifiers"),
			strings.Contains(l, "findby can never match") && strings.Contains(l, "creat"):
			return true, w
		}
	}
	return false, ""
}

func TestIronicActionOnlyIsNotAdvisedToBreakItself(t *testing.T) {
	// ARM 1 -- the action-only kind. create is its ONLY effect and a never-matching
	// findby is what dispatches it, so advice to make findby match is advice to
	// disable the kind.
	provision := ironicWarningsFor(t, "provision")
	if bad, w := advisesAgainstNotFoundCreate(provision); bad {
		t.Errorf("ironic-node-provision (findby + create, no get, no update) is STILL told "+
			"to make its findby match. Acting on exactly this advice disabled every "+
			"provision transition in openstack-ironic-operator-kog for 24 hours.\n"+
			"  warning: %s\n"+
			"  If oasgen #633 is not in this worktree, that is the expected state and this "+
			"arm is tracking it rather than reporting a new defect.", w)
	}

	// ARM 2 -- THE CONTROL, and the reason arm 1 means anything. This kind has a get
	// AND an update, so for it "findby reports not-found and the reconciler creates"
	// really is a defect and the author SHOULD be told.
	power := ironicWarningsFor(t, "power")
	if ok, _ := advisesAgainstNotFoundCreate(power); !ok {
		t.Errorf("ironic-node-power (findby + get + create + update) is NO LONGER told "+
			"anything about an unmatched findby. Arm 1 passing is then meaningless: a fix "+
			"that silenced the diagnostic everywhere would satisfy it.\n"+
			"  warnings seen (%d): %s", len(power), strings.Join(power, " | "))
	}

	t.Logf("provision warnings: %d, power warnings: %d", len(provision), len(power))
}

// TestIronicCorpusGenerates is the smoke arm oasgen asked for: every shipped
// RestDefinition must generate against its own document. It asserts nothing about
// WARNINGS -- several are expected and documented in this repo's own screen -- only
// that generation itself does not break for a real seven-kind corpus.
func TestIronicCorpusGenerates(t *testing.T) {
	slugs := []string{"node", "port", "power", "provision", "allocation",
		"deploy-template", "portgroup"}
	for _, s := range slugs {
		s := s
		t.Run(s, func(t *testing.T) {
			w := ironicWarningsFor(t, s) // t.Fatalf inside on any failure, incl. VOID cases
			t.Logf("%-16s generated, %d warning(s)", s, len(w))
		})
	}
	// A corpus that silently shrank would make this arm weaker every time a kind was
	// renamed, and nothing would say so.
	if len(slugs) != 7 {
		t.Fatalf("this arm claims to cover the whole corpus but lists %d kinds", len(slugs))
	}
}
