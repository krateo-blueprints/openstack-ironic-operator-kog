package builder

// LIVES HERE, RUNS THERE. Copy into
//   <oasgen>/go/rest-dynamic-controller/internal/tools/client/builder/
// and `go test . -run TestIronicPowerGetVerbRouting -v`. It needs that package because
// it exercises unexported BuildCallConfig/APICallBuilder/IsResourceKnown and reuses
// mockUnstructuredClient from builder_test.go. See README.md.
//
// NOTE ON THE VERSION COORDINATE: `rest-dynamic-controller` lives INSIDE the
// oasgen-provider tree at go/rest-dynamic-controller/, and the numbers this repo pins
// (KRATEO_RDC_VERSION, KRATEO_OASGEN_VERSION, both 0.61.0) are OASGEN tags. The
// standalone krateo-platformops/rest-dynamic-controller repo stops at 0.20.0 and has no
// 0.61.0 or 0.80.0 tag at all -- so "RDC 0.61.0" means the nested copy at oasgen 0.61.0.
// A run that cloned the standalone repo looking for those tags would find nothing and
// that absence would say nothing about this code.
//
// ------------------------------------------------------------------------------------
// THE QUESTION: is NodePower's `findby` verb ever consulted?
// ------------------------------------------------------------------------------------
//
// manifests/restdefinition-power.yaml carries a `findby` with a responseTransform, an
// `include_children=true` query added after measuring 3 nodes vs 17 on the collection,
// and `identifiersMatchPolicy: OR`. It also carries a comment asserting that when the
// findby does NOT match, "ResourceExists=false and `create` drives the state, which is
// the only outcome that actually powers the node."
//
// That comment is testable, and this file tests it. Observe consults `findby` only when
// IsResourceKnown is false. IsResourceKnown builds the GET and asks the client whether
// the request validates -- so the whole question is whether {node_id} can be filled on
// a CR that has never been observed.
//
// It can, by a mechanism that is NOT the mapping. BuildCallConfig's writer order
// (builder.go, numbered in its own comments):
//
//	3. processFields(specFields)    <- auto-populates path params from spec BY NAME
//	4. processFields(statusFields)
//	5. applyRequestFieldMapping     <- wins only where its own source resolves
//
// Step 3 fills {node_id} from spec.node_id before any mapping runs. This is the same
// mechanism ironic_provision_get_routing_test.go measured; that file asked whether
// ADDING a get to provision would break it. This file asks what the get NodePower
// ALREADY HAS (added 2026-11-08) does to its findby.
//
// WHY NOT mockUnstructuredClient's own ValidateRequest: it returns m.validateError
// unconditionally and never looks at `params`, so IsResourceKnown through it is a
// constant. The provision file was fooled by exactly that and reported every arm AND
// its control as true. So the ground truth here is BuildCallConfig's Parameters map,
// and IsResourceKnown is exercised through a client that really checks the params.
//
// THE ARMS MUST NOT ALL AGREE. Arm 1 is true; arms 2, 3 and the discriminator are
// false, each for a different stated reason. A run where they agree is vacuous.

import (
	"context"
	"fmt"
	"net/http"
	"testing"

	"github.com/krateo-platformops/rest-dynamic-controller/internal/text"
	restclient "github.com/krateo-platformops/rest-dynamic-controller/internal/tools/client"
	"github.com/krateo-platformops/rest-dynamic-controller/internal/tools/client/apiaction"
	getter "github.com/krateo-platformops/rest-dynamic-controller/internal/tools/definitiongetter"
	"github.com/stretchr/testify/assert"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
)

// powerParamCheckingClient validates the way the real client does for this question:
// every path parameter the operation declares must be present AND non-empty.
//
// Named distinctly from the provision file's paramCheckingClient on purpose: both files
// get copied into the same package, and a shared helper would make either one
// un-runnable alone.
type powerParamCheckingClient struct {
	mockUnstructuredClient
	required []string
}

func (c *powerParamCheckingClient) ValidateRequest(method, path string, params, query, headers, cookies map[string]string) error {
	for _, r := range c.required {
		if v, ok := params[r]; !ok || v == "" {
			return fmt.Errorf("path parameter %q is missing or empty", r)
		}
	}
	return nil
}

func (c *powerParamCheckingClient) Validate(req *http.Request) (bool, []error) { return true, nil }

// powerInfo is manifests/restdefinition-power.yaml's verb set, verbatim in the parts
// that matter. withGet=false removes ONLY the get verb, which is the shape this kind
// had before 2026-11-08.
func powerInfo(withGet bool) *getter.Info {
	selfref := []getter.RequestFieldMappingItem{
		{InPath: "node_id", InCustomResource: "spec.node_id"},
	}
	verbs := []getter.VerbsDescription{
		{Action: "findby", Method: "GET", Path: "/v1/nodes"},
	}
	if withGet {
		verbs = append(verbs, getter.VerbsDescription{
			Action: "get", Method: "GET",
			Path:                "/v1/nodes/{node_id}",
			RequestFieldMapping: selfref,
		})
	}
	verbs = append(verbs,
		getter.VerbsDescription{
			Action: "create", Method: "PUT",
			Path:                "/v1/nodes/{node_id}/states/power",
			RequestFieldMapping: selfref,
		},
		getter.VerbsDescription{
			Action: "update", Method: "PUT",
			Path:                "/v1/nodes/{node_id}/states/power",
			RequestFieldMapping: selfref,
		},
	)
	return &getter.Info{Resource: getter.Resource{
		Identifiers:      []string{"node_id"},
		VerbsDescription: verbs,
	}}
}

func powerClient() *powerParamCheckingClient {
	return &powerParamCheckingClient{
		mockUnstructuredClient: mockUnstructuredClient{
			requestedParams: map[string]text.StringSet{
				"/v1/nodes/{node_id}": text.NewStringSet("node_id"),
			},
		},
		required: []string{"node_id"},
	}
}

func TestIronicPowerGetVerbRouting(t *testing.T) {
	// A NodePower CR as charts/baremetal-host/templates/transition-power.yaml writes it:
	// spec.node_id and spec.target set, NO status -- the first reconcile.
	fresh := &unstructured.Unstructured{Object: map[string]interface{}{
		"spec": map[string]interface{}{
			"node_id": "blade01",
			"target":  "power on",
		},
	}}

	// The oasgen 0.50.0 state: spec.node_id SUPPRESSED by the self-referencing mapping
	// (#421, fixed in 0.52.0). ironic_power_spec_test.go measured this directly --
	// PRESENT at 0.59.0, ABSENT at 0.50.0, with spec.target present at both as the
	// control. This arm reproduces the ABSENT side.
	suppressed := &unstructured.Unstructured{Object: map[string]interface{}{
		"spec": map[string]interface{}{
			"target": "power on",
		},
	}}

	cli := powerClient()

	// resolve returns the path parameter BuildCallConfig produced, which is the fact
	// everything else follows from. Only meaningful when a get verb exists.
	resolve := func(mg *unstructured.Unstructured) (string, bool) {
		info := powerInfo(true)
		apiCall, callInfo, err := APICallBuilder(cli, info, apiaction.Get)
		if apiCall == nil || err != nil {
			t.Fatalf("APICallBuilder refused the get verb: %v", err)
		}
		cfg := BuildCallConfig(context.Background(), callInfo, mg, info.ConfigurationSpec, nil)
		if cfg == nil {
			t.Fatalf("BuildCallConfig returned nil")
		}
		if cfg.BuildErr != nil {
			return "", false
		}
		v, ok := cfg.Parameters["node_id"]
		return v, ok && v != ""
	}

	t.Run("ARM 1: the real shape, fresh CR -- get is buildable, findby is BYPASSED", func(t *testing.T) {
		gotParam, filled := resolve(fresh)
		assert.Equal(t, "blade01", gotParam,
			"step 3 auto-populates {node_id} from spec.node_id BY NAME, before the mapping runs")
		assert.True(t, filled)

		known, err := IsResourceKnown(context.Background(), cli, powerInfo(true), fresh)
		assert.NoError(t, err)
		assert.True(t, known,
			"THE FINDING, and it is exactly this much: KNOWN on the FIRST reconcile, so "+
				"Observe routes to `get` and NEVER REACHES findby. That is what this arm "+
				"measures. The findby's responseTransform, its include_children query and "+
				"its identifiersMatchPolicy are therefore on a path that cannot execute. "+
				"\n\nWHAT FOLLOWS BUT IS NOT MEASURED HERE: GET /v1/nodes/{node_id} answers "+
				"200 for any node that exists, so ResourceExists=true and `create` -- PUT "+
				".../states/power -- is also never dispatched, leaving get+update to drive "+
				"the kind. That step is an inference from Ironic's endpoint behaviour, NOT "+
				"a reading of this harness, which never performs the GET. Do not cite this "+
				"arm as evidence for the create half.")
		t.Logf("ARM 1  get=present  spec.node_id=%q  param=%q  known=%v", "blade01", gotParam, known)
	})

	t.Run("ARM 2: findby only, the pre-2026-11-08 shape -- findby IS the path", func(t *testing.T) {
		// No get verb at all: APICallBuilder(Get) returns nil and IsResourceKnown
		// short-circuits to false (builder.go, the apiCall==nil branch). This arm is
		// what makes ARM 1 attributable to the GET VERB rather than to anything else
		// about this kind.
		info := powerInfo(false)
		apiCall, _, _ := APICallBuilder(cli, info, apiaction.Get)
		assert.Nil(t, apiCall, "there is no get verb to build")

		known, err := IsResourceKnown(context.Background(), cli, info, fresh)
		assert.NoError(t, err)
		assert.False(t, known,
			"DISCRIMINATES ARM 1. Remove only the get verb and the same CR is NOT known, "+
				"so Observe falls to findby. The get verb added on 2026-11-08 is what "+
				"retired the findby path -- the manifest comment describing findby's "+
				"non-match as the thing that drives `create` was written for THIS shape.")
		t.Logf("ARM 2  get=ABSENT   spec.node_id=%q  known=%v", "blade01", known)
	})

	t.Run("ARM 3: get present but spec.node_id suppressed (oasgen 0.50.0) -- not known", func(t *testing.T) {
		gotParam, filled := resolve(suppressed)
		assert.Empty(t, gotParam,
			"no same-named spec field for step 3, and the mapping's own source "+
				"spec.node_id does not resolve either")
		assert.False(t, filled)

		known, err := IsResourceKnown(context.Background(), cli, powerInfo(true), suppressed)
		assert.NoError(t, err)
		assert.False(t, known,
			"THE VERSION BOUNDARY, and it explains the archived evidence. At oasgen "+
				"0.50.0 spec.node_id did not exist, so the get was unbuildable, Observe "+
				"used findby, and `create` WAS dispatched -- which is exactly what "+
				"evidence/blade06-rdc-log-2026-11-05.jsonl records (op=Create, refusing "+
				"the unresolved {node_id}). The finding in ARM 1 is a property of "+
				">=0.52.0, not of this manifest for all time.")
		t.Logf("ARM 3  get=present  spec.node_id=ABSENT  param=%q  known=%v", gotParam, known)
	})
}

// THE DISCRIMINATOR. If this ever comes back true, ValidateRequest is not actually
// checking the params and every arm above is vacuous. It is the same guard the
// provision file needed after its first version reported true for all three arms
// through a mock that could not disagree.
func TestIronicPowerGetVerbRoutingDiscriminator(t *testing.T) {
	fresh := &unstructured.Unstructured{Object: map[string]interface{}{
		"spec": map[string]interface{}{"node_id": "blade01", "target": "power on"},
	}}

	// {chassis_uuid} is in NO spec field and NO status field, and the mapping reads a
	// path that does not exist.
	info := &getter.Info{Resource: getter.Resource{
		Identifiers: []string{"node_id"},
		VerbsDescription: []getter.VerbsDescription{{
			Action: "get", Method: "GET",
			Path: "/v1/nodes/{chassis_uuid}",
			RequestFieldMapping: []getter.RequestFieldMappingItem{
				{InPath: "chassis_uuid", InCustomResource: "status.nonesuch"},
			},
		}},
	}}

	cli := &powerParamCheckingClient{
		mockUnstructuredClient: mockUnstructuredClient{
			requestedParams: map[string]text.StringSet{
				"/v1/nodes/{chassis_uuid}": text.NewStringSet("chassis_uuid"),
			},
		},
		required: []string{"chassis_uuid"},
	}

	apiCall, callInfo, err := APICallBuilder(cli, info, apiaction.Get)
	if apiCall == nil || err != nil {
		t.Fatalf("APICallBuilder refused the get verb: %v", err)
	}
	cfg := BuildCallConfig(context.Background(), callInfo, fresh, info.ConfigurationSpec, nil)
	assert.NotNil(t, cfg)
	assert.Empty(t, cfg.Parameters["chassis_uuid"],
		"nothing can supply this path param: spec.node_id is present but differently "+
			"named, so step 3 cannot auto-populate it")

	known, kerr := IsResourceKnown(context.Background(), cli, info, fresh)
	assert.NoError(t, kerr)
	assert.False(t, known,
		"DISCRIMINATOR: must be false. If true, ValidateRequest ignores params and every "+
			"other arm in this file means nothing.")
	t.Logf("DISCRIMINATOR  param=%q  known=%v  (both must be empty/false)",
		cfg.Parameters["chassis_uuid"], known)
}

var _ restclient.UnstructuredClientInterface = (*powerParamCheckingClient)(nil)
