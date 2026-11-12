package builder

// LIVES HERE, RUNS THERE. Copy into
//   <oasgen>/go/rest-dynamic-controller/internal/tools/client/builder/
// and `go test . -run TestIronicProvisionGetVerbRouting -v`. It needs that package
// because it exercises unexported BuildCallConfig/APICallBuilder and reuses
// mockUnstructuredClient from builder_test.go. See README.md.

// Would adding a `get` verb to ironic-node-provision make Observe report the resource
// EXISTS on the very first reconcile -- and so make the create (the provision PUT)
// unreachable, which is the defect krateo-blueprints/openstack-ironic-operator-kog#65
// just removed by another route?
//
// The question is entirely about WHERE the get verb's path parameter is resolved from.
// One variable: the requestFieldMapping's source.
//
// WHY THIS DOES NOT USE mockUnstructuredClient's ValidateRequest. That mock returns
// m.validateError unconditionally and never looks at `params`, so IsResourceKnown
// through it is a constant function of validateError. The first version of this test
// did exactly that and reported BOTH arms AND the control as known=true -- it
// discriminated nothing, and only the control said so. So:
//   - the ground truth is asserted on BuildCallConfig's Parameters map, which is where
//     the mapping actually resolves;
//   - IsResourceKnown is then exercised through a mock that really checks the params,
//     so its verdict means something.

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

// paramCheckingClient validates the way the real client does for this question: every
// path parameter the operation declares must be present AND non-empty.
type paramCheckingClient struct {
	mockUnstructuredClient
	required []string
}

func (c *paramCheckingClient) ValidateRequest(method, path string, params, query, headers, cookies map[string]string) error {
	for _, r := range c.required {
		if v, ok := params[r]; !ok || v == "" {
			return fmt.Errorf("path parameter %q is missing or empty", r)
		}
	}
	return nil
}

func (c *paramCheckingClient) Validate(req *http.Request) (bool, []error) { return true, nil }

func provisionInfo(src string) *getter.Info {
	return &getter.Info{
		Resource: getter.Resource{
			Identifiers: []string{"node_id"},
			VerbsDescription: []getter.VerbsDescription{
				{
					Action: "get", Method: "GET",
					Path: "/v1/nodes/{node_id}/states",
					RequestFieldMapping: []getter.RequestFieldMappingItem{
						{InPath: "node_id", InCustomResource: src},
					},
				},
			},
		},
	}
}

func TestIronicProvisionGetVerbRouting(t *testing.T) {
	// A NodeProvision CR exactly as charts/baremetal-host/templates/transition-manage.yaml
	// writes it: spec.node_id set, spec.target set, and NO status at all -- the state on
	// the first reconcile, before anything has been observed.
	fresh := &unstructured.Unstructured{Object: map[string]interface{}{
		"spec": map[string]interface{}{
			"node_id": "probe01",
			"target":  "manage",
		},
	}}
	withStatus := fresh.DeepCopy()
	if err := unstructured.SetNestedField(withStatus.Object, "abc-123", "status", "uuid"); err != nil {
		t.Fatalf("building the populated-status control: %v", err)
	}

	cli := &paramCheckingClient{
		mockUnstructuredClient: mockUnstructuredClient{
			requestedParams: map[string]text.StringSet{
				"/v1/nodes/{node_id}/states": text.NewStringSet("node_id"),
			},
		},
		required: []string{"node_id"},
	}

	// resolve returns the path parameter BuildCallConfig produced, which is the fact
	// everything else follows from.
	resolve := func(src string, mg *unstructured.Unstructured) (string, bool) {
		info := provisionInfo(src)
		apiCall, callInfo, err := APICallBuilder(cli, info, apiaction.Get)
		if apiCall == nil || err != nil {
			t.Fatalf("APICallBuilder refused the get verb (src=%s): %v", src, err)
		}
		cfg := BuildCallConfig(context.Background(), callInfo, mg, info.ConfigurationSpec, nil)
		if cfg == nil {
			t.Fatalf("BuildCallConfig returned nil (src=%s)", src)
		}
		if cfg.BuildErr != nil {
			return "", false
		}
		v, ok := cfg.Parameters["node_id"]
		return v, ok && v != ""
	}

	for _, tc := range []struct {
		name        string
		src         string
		mg          *unstructured.Unstructured
		wantParam   string
		wantKnown   bool
		consequence string
	}{
		{
			name: "mapped from spec.node_id, fresh CR", src: "spec.node_id", mg: fresh,
			wantParam: "probe01", wantKnown: true,
			consequence: "KNOWN on the first reconcile. Observe routes to `get`, " +
				"GET /v1/nodes/{id}/states answers 200 because the NODE exists, so " +
				"ResourceExists=true and the create -- the provision PUT -- is NEVER " +
				"dispatched. The #65 defect, made structural instead of transform-borne.",
		},
		{
			name: "mapped from status.uuid, fresh CR (status empty)", src: "status.uuid", mg: fresh,
			wantParam: "probe01", wantKnown: true,
			consequence: "THE FINDING, and it defeats every mapping-based workaround. The " +
				"param is NOT empty -- it holds spec.node_id. BuildCallConfig step 3 " +
				"auto-populates path params from the spec BY NAME before step 5 applies " +
				"the mapping, so {node_id} is filled from spec.node_id whatever the " +
				"mapping says; the mapping only overrides when its own source resolves. " +
				"So a get verb on this path is ALWAYS buildable for a NodeProvision, " +
				"spec.node_id being required. There is no mapping source that keeps it " +
				"inert until status populates.",
		},
		{
			name: "CONTROL: mapped from status.uuid, status POPULATED", src: "status.uuid", mg: withStatus,
			wantParam: "abc-123", wantKnown: true,
			consequence: "The discriminator. Without this arm, `false` above could mean " +
				"the mapping never resolves from status at all -- a different claim -- " +
				"rather than that it resolves and finds the field empty.",
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			gotParam, filled := resolve(tc.src, tc.mg)
			assert.Equal(t, tc.wantParam, gotParam, "the resolved path parameter is the ground truth")
			assert.Equal(t, tc.wantKnown, filled, "param populated?")

			known, err := IsResourceKnown(context.Background(), cli, provisionInfo(tc.src), tc.mg)
			assert.NoError(t, err)
			assert.Equal(t, tc.wantKnown, known, tc.consequence)
			t.Logf("src=%-14s param=%-11q known=%-5v  %s", tc.src, gotParam, known, tc.consequence)
		})
	}
}

// THE DISCRIMINATOR. Every arm above answers known=true, and a test whose every
// case agrees is the shape that already fooled this file once: the first version used
// mockUnstructuredClient, whose ValidateRequest ignores params, and reported true for
// all three including the control. So one case MUST come back false, and it must do so
// for the stated reason -- the path parameter has no same-named spec field to be
// auto-populated from, and the mapping's source does not resolve either.
func TestIronicProvisionGetVerbRoutingDiscriminator(t *testing.T) {
	fresh := &unstructured.Unstructured{Object: map[string]interface{}{
		"spec": map[string]interface{}{"node_id": "probe01", "target": "manage"},
	}}

	info := &getter.Info{Resource: getter.Resource{
		Identifiers: []string{"node_id"},
		VerbsDescription: []getter.VerbsDescription{{
			Action: "get", Method: "GET",
			// {allocation_uuid} is in NO spec field and NO status field.
			Path: "/v1/nodes/{allocation_uuid}/states",
			RequestFieldMapping: []getter.RequestFieldMappingItem{
				{InPath: "allocation_uuid", InCustomResource: "status.nonesuch"},
			},
		}},
	}}

	cli := &paramCheckingClient{
		mockUnstructuredClient: mockUnstructuredClient{
			requestedParams: map[string]text.StringSet{
				"/v1/nodes/{allocation_uuid}/states": text.NewStringSet("allocation_uuid"),
			},
		},
		required: []string{"allocation_uuid"},
	}

	apiCall, callInfo, err := APICallBuilder(cli, info, apiaction.Get)
	if apiCall == nil || err != nil {
		t.Fatalf("APICallBuilder refused the get verb: %v", err)
	}
	cfg := BuildCallConfig(context.Background(), callInfo, fresh, info.ConfigurationSpec, nil)
	assert.NotNil(t, cfg)
	assert.Empty(t, cfg.Parameters["allocation_uuid"],
		"nothing can supply this path param: no same-named spec field for step 3 to "+
			"auto-populate from, and the mapping reads status.nonesuch")

	known, kerr := IsResourceKnown(context.Background(), cli, info, fresh)
	assert.NoError(t, kerr)
	assert.False(t, known,
		"DISCRIMINATOR: this must be false. If it is true, ValidateRequest is not "+
			"actually checking the params and every other arm in this file is vacuous.")
	t.Logf("discriminator: param=%q known=%v (both must be empty/false)",
		cfg.Parameters["allocation_uuid"], known)
}

var _ restclient.UnstructuredClientInterface = (*paramCheckingClient)(nil)
