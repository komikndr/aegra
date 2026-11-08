# SystemOne routing interface

## Scope

SystemOne supplies routing decisions through the native TypeSafe-compatible API, separate from the main strategic LLM.
GLM 5.3 or another configured LLM provides strategic reasoning and coordination.
The supplied endpoint is `http://desktop-14p3oeb:8001/v1`, with model ID `d3-flash`.
Model discovery reports `owned_by: llamacpp`, GGUF Q8_0, and approximately 8.95 billion parameters.
This differs from the earlier D3 27B/vLLM description. The adapter does not assume a particular serving engine or parameter count.
Discovery succeeded from the host and Aegra container. A native decision request also succeeded through Playwright.

This slice implements a routing adapter and external-boundary fixture catalog.
It does not implement durable boards, strategic objectives, task dispatch, or a complete scenario runner.
The capability endpoint reports those features as unavailable.

## Configuration

The adapter reads only server environment variables:

```dotenv
SYSTEMONE_BASE_URL=http://desktop-14p3oeb:8001/v1
SYSTEMONE_MODEL=d3-flash
SYSTEMONE_API_KEY=
SYSTEMONE_TIMEOUT_SECONDS=30
```

The base URL must end in `/v1`. The model must match the served model ID.
The model family name does not establish that ID.
URL credentials, query parameters, and fragments are not accepted.
Requests cannot supply an endpoint, model, or credential.

The transport is `POST /v1/systemone` with `state` and a typed `questions.route` choice.
Its criteria define the three permitted destinations. The response supplies a choice, option probabilities, and confidence.
This decision model does not generate a chat answer or textual rationale. There is no chat-completion fallback.
The contract follows the [llama.cpp System One API](https://github.com/ggml-org/llama.cpp/tree/master/tools/server#post-v1systemone-typesafe-compatible-system-one-api).

## API

All routes currently require SuperAdmin. This is a development boundary, not the final operator authorization policy.

| Route | Result |
| --- | --- |
| `GET /swarm/v1/capabilities` | Configuration and implemented feature flags; no health probe |
| `POST /swarm/v1/routing/assess` | Validated routing proposal or explicit failure status |
| `GET /swarm/v1/simulations/routing` | Four labeled scenario input bundles; no execution |

The routing request has this shape:

```json
{
  "signal_id": "observation-001",
  "source": "approved-platform-source",
  "observed_at": "2026-01-01T00:00:00Z",
  "summary": "Readiness decreased across three observation periods.",
  "evidence_refs": ["dataset:readiness-history"]
}
```

The initial destinations are `local_assessment`, `strategic_review`, and `record_only`.
They are provisional logical destinations, not graph IDs or executable workflows.
The adapter rejects unknown destinations, mismatched models, malformed answers, and invalid probability distributions.
It treats signal text as untrusted input and validates the model output independently of prompt instructions.
The signal ID comes from the submitted signal. The result preserves native probabilities and confidence without inventing a model rationale.
These scores are not calibrated probabilities of operational outcomes. They do not authorize actions.

The result status is `routed`, `unconfigured`, `unavailable`, or `invalid_response`.
Expected availability failures use HTTP 200 with the explicit result status and no decision.
Authentication, authorization, request validation, and invalid server configuration use HTTP error responses.
Clients must inspect `status`; HTTP 200 alone does not indicate a routing decision.

No routing call dispatches work, writes a board entry, or issues an operator instruction.
Future intake must authenticate source identity, persist signals, deduplicate delivery, and resolve approved workflows before dispatch.
Signal IDs supplied here provide correlation, not authenticated source identity or a durable idempotency guarantee.

## Offline behavior and limits

### Integration observation

An authenticated Playwright check on October 25, 2026 submitted all four synthetic signals through Aegra to the real endpoint.
All four returned validated native choices. End-to-end times were approximately 0.15–0.55 seconds in this small sample.
Routine, readiness, and duty scenarios matched their scripted fixture destinations.
The EVE signal selected `local_assessment`, while its scripted fixture uses `strategic_review`.
The readiness choice was close: strategic review scored 0.440, while local assessment scored 0.394.
These observations establish interface compatibility, not routing accuracy or a performance benchmark.
Fixture destinations are scripted inputs, not approved evaluation labels. The model saw signal summaries and evidence references, not resolved datasets.

### Failure handling

Empty endpoint or model configuration returns `unconfigured` without a network request.
Connection failures, HTTP failures, and deadlines return `unavailable`.
Malformed or oversized output returns `invalid_response`.
No failure selects a default destination or substitutes a fixture.

The adapter bounds response bodies to 256 KiB before JSON parsing and refuses compressed responses.
The configurable whole-operation deadline defaults to 30 seconds, with an allowed range of 1 to 120 seconds.
Redirects are not followed. Upstream bodies and credentials do not appear in error results.

## Scenario inputs

The fixture catalog contains routine observation, readiness disagreement, duty-authority review, and EVE regional supply scenarios.
Each has a fixed synthetic signal and routing result. Some include operator or duty-authority responses.
Fetching the catalog does not call D3 or run a swarm.
The fixture routing result is scripted external input; it does not establish real D3 accuracy.

The later scenario runner will submit these inputs to real persistence, coordination, specialist execution, and human-response interfaces.
The main LLM's conclusions will not be scripted.
Unresolved client escalation policy remains explicit rather than encoded as an invented operational rule.

### EVE Online first domain

EVE provides a useful boundary for strategic assessment with market history, news, and human reports.
The initial fixture is `examples/swarm/eve-regional-supply.json`.
It contains seven synthetic daily price and volume observations, speculative news, a prior stock finding, and a later operator correction.
The intended objective is a seven-day regional stocking and logistics assessment, not automated game control.

Public ESI regional market history is a candidate source for daily observations.
Current orders are a separate snapshot with pagination and freshness requirements.
Regional historical averages are not current executable prices or warehouse inventory.
News needs a separately approved source; the fixture does not assume an ESI news feed.
Human stock reports remain separate from public market data.

The future runner will let market and news specialists contribute evidence, then request a revised assessment after the operator correction.
The fixture scripts external inputs, not the main LLM's assessment.
Live ESI collection, news collection, forecasting, board persistence, and scenario execution are not implemented by this fixture.

## Verification

Run the offline adapter and API tests:

```bash
uv run --package aegra-api pytest libs/aegra-api/tests/unit/test_systemone.py -q
```
