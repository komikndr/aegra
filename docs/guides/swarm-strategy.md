# Bounded strategic search

`strategy_search_v1` adds alternative development and revision inside the existing Aegra runtime.
It adapts ideas from Adaptive Deepthink. Deepthink is a source reference, not a runtime dependency.
The fixed and short planned review workflows remain available for simple questions.

## Research lifecycle

1. Generate one or two distinct approaches, each with a falsification condition.
2. Develop each approach independently against the same scoped evidence.
3. Critique each attempt and request up to two supported investigations.
4. Run deterministic verification and retain its receipts, including missing-input errors.
5. Compare the artifacts and select an action for each active branch.
6. Revise selected branches within the remaining budget, or publish an assessment and wait.

The initial implementation supports two passes. Pruned branches do not return in the second pass.
The controller can select `continue`, `revise`, `prune`, `wait`, or `conclude`.
Continuation and revision both create a new attempt with the previous artifacts and a specific next question.
They do not extend the budget or authorize operational actions.

The controller receives branch results only after independent development and critique.
The branches do not receive peer histories during development.
Later passes receive their own previous attempt, critique, verification receipts, and controller instruction.

## Durable state and recovery

PostgreSQL stores immutable branch versions in `swarm_strategy_versions`.
Each version identifies its review, branch, pass, artifacts, action, parent version, and execution references.
The scoped board records task results and version transitions.
Review details include `strategy_versions`; execution inspection shows the associated tasks.

New strategy reviews load a bounded summary of the objective's four latest branch versions.
Those summaries retain their source review and version references.
They are historical research context, not proof that a hypothesis remains valid.

Tasks reuse existing RunJobs, dispatch recovery, Redis leases, and objective transaction locks.
Retries retain the same execution IDs. Pause, supersession, and deadline checks fence late publication.
Branch versions never roll back portfolio transactions or erase previous external outcomes.

## Verification boundaries

The initial verification catalog contains two operations:

- `stock_calculation`: Recompute coverage from structured inventory, daily consumption, and horizon inputs.
- `evidence_freshness`: Examine explicit expiry at the dispatched snapshot time.

The server recomputes worker receipts before accepting them.
Missing structured stock data produces an unavailable receipt, not a successful test.
Absence of an expiry field does not prove freshness.
Model critiques and controller conclusions remain model judgments. Citation scope does not establish semantic claim support.

This milestone does not generate or execute arbitrary code, install packages, or provide sandbox access.
It does not independently validate forecasts, profitability, or every numerical claim in prose.
Broader approved investigations and controlled quality comparisons remain subsequent work.

## Start a review

The existing SuperAdmin and objective-ownership policy applies.

```http
POST /swarm/v1/objectives/{objective_id}/reviews
Content-Type: application/json

{
  "request_key": "strategy-search-1",
  "workflow": "strategy_search_v1",
  "allow_market_acquisition": false
}
```

Approved EVE acquisition remains opt-in. When enabled, the seed can select one server-approved scope before branch development.
The existing provenance, cached-content deduplication, expiry, and publication checks apply.
The workflow cannot submit paper or real orders.

The objective sub-coordinator can propose this workflow with `strategy_review` when the user requests alternative development or iterative research.
The server returns a separate action receipt. Existing questions and short reviews retain their prior paths.

## Start supervision

Control Plane Objectives offers **Strategic search: alternatives and revision** in the supervisor workflow selector.
One review reserves eleven model calls and eight specialist attempts for the worst-case two-branch, two-pass search.
A two-review budget therefore needs at least twenty-two calls and sixteen specialist reservations.
Reservations remain conservative when the search finishes early.

The review retains its ten-minute deadline. Every model call has a 120-second limit, 4096 output tokens, and no SDK retries.
Every worker input has a 160 KB limit. Verification and optional acquisition do not invoke a model.
The supervisor's elapsed deadline can shorten the review deadline.

Reaching the pass limit with continuing branches produces a partial assessment with an explicit budget uncertainty.
Missing information can suspend supervision. Completing a bounded review does not complete the objective.

## Evaluation direction

Transaction and graph tests establish execution behavior, isolation, and receipt integrity.
They do not establish that strategic search outperforms short reviews.
The [paired comparison harness](swarm-comparison.md) uses identical scenarios and model settings with shared inference ceilings across repeated runs.
Its initial run passed automated checks in 8/10 short trials and 5/10 strategic trials. Strategic search used approximately 2.88 times as many tokens.
These limited results do not establish general research superiority. Alternative quality, contradictions, claim support, and correction quality require separate human review.

## Initial live verification

On October 28, 2026, authenticated Playwright checks ran the configured main model against two explicitly synthetic inventory scenarios.
The final check created two independent candidates, two critiques, two deterministic verification jobs, and one controller decision after the seed.
All eight jobs completed. Six jobs used the model; the two verification jobs did not.
The baseline receipt confirmed ten days of cover against a seven-day horizon. The stress receipt confirmed a 400,000-unit shortfall against ten days.
The controller concluded both calculation branches after one pass and published `needs_information` for the unresolved real-world policy decision.
Two immutable branch versions retained the attempts, critiques, scoped receipts, actions, and execution references.

An earlier check overstated the scope of a calculation receipt. The prompts now distinguish model arithmetic from actual verification results.
The final check associated each calculation claim with a receipt for that evidence record.
This observation does not establish general claim correctness or better research quality.
Revision and pruning were exercised in PostgreSQL integration tests; the live model did not need a second pass in this case.
