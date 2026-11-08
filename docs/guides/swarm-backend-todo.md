# Onyx swarm backend TODO

Updated: 2026-10-28.

This checklist tracks backend work before further UI changes. Implementation stays in Aegra.
Unchecked items are planned, not implemented capabilities.

## Implemented foundation

- [x] Persist objectives, evidence, scoped activity, and assessment revisions in PostgreSQL.
- [x] Run a bounded analyst, skeptic, and synthesis workflow through the existing executor.
- [x] Recover committed dispatch intent and protect against duplicate result publication.
- [x] Support feedback, revised assessments, objective pause, and restart recovery.
- [x] Route natural head Coordinator chat to authorized objective sub-coordinators.
- [x] Persist sub-coordinator conversations and separate proposed actions from server receipts.
- [x] Provide bounded virtual-ISK paper cycles, deterministic accounting, and public EVE market observations.
- [x] Support optional native SystemOne routing without requiring D3 for Coordinator operation.
- [x] Supervise fixed evidence reviews with persisted schedules, evidence wakeups, and explicit budgets.
- [x] Separate objective lifecycle from execution status and expose supervisor Start, Wake, Stop, and remaining budgets in Control Plane.

Supervision supports fixed reviews, bounded model-led task plans, and bounded strategic search.
Model-selected action execution remains pending.
Paper execution has its own bounded schedule.

## 1. Persistent objective supervisor: initial implementation

- [x] Persist the next wakeup, its reason, waiting conditions, and stop conditions for each supervised objective.
- [x] Support scheduled reviews, new-evidence triggers, and explicit human requests.
- [x] Coalesce duplicate wakeups and fence concurrent supervision attempts.
- [x] Load the previous assessment, new evidence, scoped activity, and execution outcomes before each bounded run.
- [x] Set explicit limits for supervisor review count, logical model calls, specialist tasks, and elapsed time.
- [x] Persist fixed-workflow decisions to schedule, wait for information, or stop.
- [ ] Define an aggregate execution-cycle budget across model-selected action workflows.
- [x] Persist planned-review decisions to answer, assign tasks, request information, wait, or stop supervision.
- [ ] Add validated action selection through approved execution workflows.
- [x] Apply pause, cancellation, and deadline rules to supervised review publication.
- [x] Recover supervised reviews with the same execution IDs and without duplicate assessment publication.

Acceptance scenario: an EVE objective completes a review, records its next wakeup, survives restart, and reassesses changed evidence.
A waiting or stopped objective must not consume model calls until a permitted wakeup occurs.

See [bounded objective supervision](./swarm-supervisor.md) for API examples, budget scope, and limitations.
Action selection remains pending; the supervisor does not start paper trades.

## 2. Model-led planning and bounded task assignment

- [x] Define an initial approved read-only capability catalog with typed inputs, outputs, permissions, and budgets.
- [x] Let the coordinator choose tasks in the opt-in `planned_review_v1` workflow.
- [x] Persist task dependencies, assignments, partial results, and continuation decisions.
- [x] Bound delegation depth, per-review concurrency, model calls, and elapsed time.
- [x] Validate evidence scope and deterministic calculation receipts without treating model reports as verified facts.
- [ ] Evaluate semantic no-progress across review generations instead of relying only on finite budgets.
- [x] Persist bounded alternative branches with independent development, critique, version history, and execution references.
- [x] Run targeted stock and explicit-expiry verification with server-recomputed receipts.
- [x] Add a paired comparison with identical scenarios and model, shared resource ceilings, and repeated trials.
- [ ] Correct active-branch controller failures and rerun a labeled comparison without discarding the initial baseline.
- [ ] Add broader approved verification modules and environment receipts beyond stock arithmetic and explicit expiry.

See [bounded review planning](./swarm-planning.md) for the capability catalog and API examples.
See [bounded strategic search](./swarm-strategy.md) for alternative development, critique, revision, pruning, and limitations.
Planning selects work on supplied evidence or explicitly approved EVE acquisition. It does not select trading actions.

## 3. Evidence acquisition

- [ ] Add approved dataset, market, news, and log connectors as the first scenario requires them.
- [x] Add the initial opt-in public EVE market acquisition capability with server-approved scopes.
- [x] Record source identity, observation time, availability time, retrieval time, and query scope for EVE receipts.
- [x] Separate regional daily history from current location quotes and top-of-book liquidity.
- [ ] Define freshness limits, missing-source behavior, and historical replay rules.
- [x] Keep arbitrary endpoints and credentials outside model-controlled acquisition input.

Initial EVE freshness, cache deduplication, source failure, and publication expiry checks are implemented.
Historical replay rules and broader connectors remain pending. See [approved evidence acquisition](swarm-acquisition.md).

## 4. Deterministic assessment validation

- [x] Store initial stock-calculator results with inputs, units, calculation version, and evidence references.
- [ ] Validate numerical conclusions before assessment publication.
- [ ] Check whether cited records support each claim; a valid citation ID alone is insufficient.
- [ ] Preserve uncertain or conflicting findings without converting them into verified facts.

## 5. Outcome measurement and improvement

- [x] Add a versioned synthetic research-quality suite with optional live inference and replayable captured-output scoring.
- [x] Record per-case checks, model identities, worker latency, and failures without an LLM self-grade.
- [x] Record the initial twenty-trial comparison: short reviews passed 8/10 checks; strategic search passed 5/10 with higher usage.
- [x] Correct planner schema failures and expired-quote handling exposed by the initial evaluation baseline.
- [ ] Add semantic claim-support annotations and repeated-run variability measurements.
- [x] Persist immutable branch attempts, critiques, verification results, and continuation decisions.
- [ ] Version evaluation criteria and validate proposed strategy improvements before activation.
- [ ] Compare paper outcomes with explicit benchmarks after fees and execution assumptions.
- [ ] Score forecasts against later observations with defined time windows.
- [ ] Evaluate evidence support, operator corrections, specialist usefulness, and resource use.
- [ ] Separate proposed improvements from evaluated and activated changes.

The suggested five-percent daily return remains an aspirational target, not a demonstrated result or guaranteed outcome.
See [research-quality evaluations](swarm-evaluation.md) for the initial five-of-eight baseline and subsequent eight-of-eight checks.
Passing these limited checks does not prove semantic claim support or general research accuracy.
See [paired research comparison](swarm-comparison.md) for equal-ceiling controls, actual usage, failures, and pending human review.

## 6. Operator and duty-authority workflows

- [ ] Persist assignments, acknowledgements, completion reports, disagreements, and requests for information.
- [ ] Link feedback to the instruction and assessment version that produced it.
- [ ] Configure exception thresholds and no-response behavior from an explicit client policy.
- [ ] Keep decision, instruction, execution, and observed outcome as distinct records.

Routine decisions do not acquire a blanket approval requirement.
Admin and Head can initiate conversations; they are not a default escalation chain.

## 7. OpenSearch projections and observation streams

- [ ] Add a transactional outbox for searchable projections of PostgreSQL coordination records.
- [ ] Make indexing retryable and idempotent, with visible projection freshness.
- [ ] Define timestamped observation streams, retention, and correction rules.
- [ ] Prevent historical replay from sending new operational notifications.

PostgreSQL remains authoritative for objectives, tasks, scoped activity, and the paper ledger.
OpenSearch provides search, reporting, time-series observations, and anomaly analysis. No additional time-series database is required for this milestone.

## 8. Shared-objective authorization

- [ ] Define explicit shared-objective permissions for Operator, Head, and designated duty authority.
- [ ] Enforce those permissions in APIs, discovery tools, delegation, and evidence access.
- [ ] Test cross-owner isolation, role revocation, and scoped sharing.

Current access remains SuperAdmin plus objective ownership until a broader policy is implemented.

## Later: external agents

- [ ] Add A2A only when an independent external agent and its contract are available.
- [ ] Map external task identity, cancellation, errors, evidence, and uncertain outcomes into local records.

A2A is not a prerequisite for the internal objective loop.
Kafka, Flink, and Hudi remain external Big Data Platform responsibilities.

## Architecture references: Deepthink

Adaptive Deepthink is the reference for the strategic reasoning direction, rather than an optional report-writing feature.
Persistent objective supervision remains the mission lifecycle; bounded strategy search develops and tests approaches within that lifecycle.
The initial `strategy_search_v1` implementation uses one or two alternatives and at most two passes through development, critique, verification, and comparison.
Simple questions retain short workflows. Shared task persistence and execution remain in Aegra.

The local `/home/kxn/Deepthink` source provides a reference for parallel strategies, critique cycles, targeted modules, and branch history compression.
Its deterministic branch formulas consume model-produced progress reports. A deterministic choice does not independently validate those reports.
The inspected module pipeline derives its verdict from model-written classification text, even when sandbox experiments are available.
ONYX must retain test receipts, evidence provenance, explicit budgets, and durable recovery when adapting these patterns.
This review adds no Deepthink dependency or automatic modification of ONYX's validation rules.

## Verification across milestones

- [ ] Add regression coverage with each implementation stage, not only at the end.
- [ ] Exercise duplicate delivery, concurrent wakeups, restart recovery, cancellation races, and late results.
- [ ] Exercise source outages and D3 unavailability without fabricated observations or decisions.
- [ ] Record model usage, task latency, retries, queue delay, and workflow outcomes.
- [ ] Run the first complete scenario with labeled evidence and virtual funds before claiming autonomous strategy performance.
