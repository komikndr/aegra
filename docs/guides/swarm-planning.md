# Bounded model-led review plans

`planned_review_v1` adds model-selected tasks to existing Aegra objective reviews.
The fixed `evidence_review_v1` workflow remains the default. Existing clients keep their current behavior.
For alternative development and bounded revision, use [strategic search](./swarm-strategy.md) instead of the short plan.
Both workflows require SuperAdmin and objective ownership.

## Execution contract

The planner receives an immutable evidence snapshot, the previous assessment, bounded activity, and the approved capability catalog.
It returns one of five decisions:

- `answer`: Answer from supplied evidence without specialist tasks.
- `tasks`: Request one or two approved specialist tasks.
- `needs_information`: Record missing inputs and wait without timer-based model calls.
- `wait`: Record an assessment and wait for the next permitted review.
- `stop`: Finish this review and stop its supervisor, if present.

Every non-task decision requires an assessment with scoped citations. Stop does not delete or pause the objective.
It does not stop an independently configured paper account.

Server code validates task IDs, capabilities, evidence references, task count, and dependency order before dispatch.
Tasks can depend only on earlier tasks in the same plan. Each capability can occur once per review.
The plan cannot add tasks after validation, delegate recursively, supply code, choose arbitrary endpoints, or grant permissions.
Invalid plans fail the review without a fallback plan.

PostgreSQL stores the validated plan, task assignments, worker IDs, findings, blockers, and final assessment.
Existing workers execute the committed dispatch intents. Recovery retains the same run IDs.
Objective locks and terminal review states prevent duplicate assessment publication.
At-least-once delivery can still repeat inference; exactly-once model billing is not guaranteed.

Independent tasks can run concurrently, with at most two specialist tasks per review.
A dependent task waits for its predecessor to finish and receives its success or failure receipt.
Read-only dependent analysis can explain a failed predecessor; it must not assume that predecessor succeeded.
If all specialists fail, the review fails without a synthesis call.
Otherwise synthesis receives successful results and explicit failures, and publication records partial coverage.

## Approved capabilities

| Capability | Input | Output | Execution |
| --- | --- | --- | --- |
| `evidence_analysis` | Selected supplied evidence, bounded question, dependency receipts | Cited assessment | One model call |
| `market_analysis` | Selected supplied EVE market/news records | Cited assessment with freshness and uncertainty | One model call |
| `skeptical_review` | Selected evidence and optional predecessor findings | Cited counterassessment | One model call |
| `stock_calculation` | One structured stock record | Versioned Decimal calculation receipt | Deterministic code |

Specialists receive only their selected current evidence records. Historical context remains objective-scoped and untrusted.
These capabilities do not fetch sources, place orders, start paper loops, or execute shell commands.
Market analysis describes supplied data or explicitly acquired approved evidence. Historical daily averages cannot establish an executable trading opportunity.
An optional [approved acquisition step](swarm-acquisition.md) can collect a configured public EVE scope before specialists run.
D3 is not necessary for this workflow.

## Structured stock inputs

Set an evidence record's `content` to a JSON string with exactly these fields:

```json
{
  "inventory_units": "700000",
  "daily_consumption_units": "70000",
  "horizon_days": "7"
}
```

Supply this record when creating an objective or submitting structured feedback.
The existing evidence correction form can also accept JSON text as its content.
The calculator does not extract numbers from prose. Consumption must be positive, and all quantities have explicit bounds.
Its receipt contains inputs, units, evidence IDs, calculation version, coverage, required stock, remaining stock, and shortfall.
Reconciliation recomputes the receipt before accepting worker output.

For these inputs, coverage is 10 days, seven-day demand is 490000 units, and remaining stock is 210000 units.
The receipt proves arithmetic for supplied inputs, not the accuracy of the underlying observation.
Synthesis receives the receipt, but general numerical and semantic validation of its prose remains future work.

## API usage

Start one planned review:

```http
POST /swarm/v1/objectives/{id}/reviews
Content-Type: application/json

{"request_key":"plan-1","workflow":"planned_review_v1"}
```

The same request key and input return the same review. Changed input with the same key returns `409`.
The review-detail endpoint returns `workflow`, `plan`, and `input_snapshot`.
The existing tasks and board endpoints expose task results and plan entries.
Feedback can use `workflow: "planned_review_v1"` to supersede the current review with a fresh planned assessment.
Existing feedback without that field still uses the fixed workflow.

Start bounded planned supervision:

```http
POST /swarm/v1/objectives/{id}/supervisor/start
Content-Type: application/json

{
  "request_key": "planned-supervisor-1",
  "workflow": "planned_review_v1",
  "max_reviews": 2,
  "interval_seconds": 300,
  "max_elapsed_seconds": 3600,
  "max_model_calls": 8,
  "max_specialist_tasks": 4
}
```

The supervisor reserves four logical model calls and two specialist tasks before each planned review.
This covers the planner, two model specialists, and synthesis. Deterministic calculations use no model call.
Unused reservations remain charged to keep admission conservative. A direct answer therefore still reserves the complete worst-case budget.
The existing per-start ceiling remains 72 model calls; this can stop supervision before its review-count ceiling.

Each model call has a 4096-output-token limit, no SDK retries, and a 120-second deadline.
The complete review has the existing ten-minute deadline, capped by the supervisor's elapsed deadline.
Pause, superseding feedback, cancellation, and deadline checks occur before reconciliation can publish an assessment.

## Current boundary

The capability catalog and plan structure are server-controlled. Models select approved read-only work, not new executable capabilities.
Additional connectors, claim-support verification, semantic no-progress evaluation, and model-selected paper execution remain later milestones.
No new UI controls are necessary to use these API endpoints.

## Verification

On 2026-10-27, an authenticated Playwright API check ran this workflow with the configured `openai/gpt-5.4-mini` model.
The planner selected only `stock_calculation` for the synthetic stock question.
The calculator returned 10 days of coverage, 490000 required units, and 210000 remaining units.
Synthesis preserved these figures and the simulation label.
Repeating the original request returned the same completed review with one plan entry and one assessment entry.

Isolated PostgreSQL tests cover concurrent reconciliation, recovery, invalid plans, dependency failures, superseding feedback, and supervisor admission budgets.
