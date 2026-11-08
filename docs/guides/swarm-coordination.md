# Durable ONYX evidence reviews

For remaining backend milestones, see the [swarm backend TODO](./swarm-backend-todo.md).

## Scope

This functionality is part of Aegra. It uses the existing PostgreSQL database, LangGraph checkpoints, executor, and Redis workers.
It adds no separate application or queue.
The optional D3 router does not gate objective reviews. An offline GPU does not prevent this workflow from using the main LLM.

The first approved workflow is `evidence_review_v1`:

1. Create a persistent objective with success criteria and supplied evidence.
2. Request a bounded review.
3. Run an evidence analyst and a skeptical reviewer in parallel.
4. Reconcile their findings with a coordinator model call.
5. Save an assessment version and finish the review.
6. Submit feedback to request a new assessment version.

The workflow uses the configured main model through `resolve_agent_model("swarm")`.
The existing agent-model configuration can select a different provider or model for `swarm`.
Each task permits one model call, at most 4096 output tokens, no SDK retries, and a 120-second deadline.
A review permits two specialist tasks and one coordinator task, with a ten-minute deadline.
A failed specialist produces a blocker. A successful specialist can still support a partial final assessment.
If both specialists fail, the review fails without a final assessment.

This workflow assesses supplied evidence. It has no live ESI collector, web search, broker, execution tools, or autonomous trading authority.
It does not claim that a five-percent daily return is feasible or achieved.
The [bounded supervisor](./swarm-supervisor.md) adds persistent schedules and evidence-triggered reviews around this fixed workflow.
The opt-in [strategic search](./swarm-strategy.md) adds independent alternatives, immutable branch versions, critique, and tested revision.
Model-selected task plans, duty-authority policies, A2A, and OpenSearch projections remain later work.
The existing control-plane Message Board page remains a placeholder; the durable board is available through the API.

## API

All routes currently require SuperAdmin access. Each objective belongs to its authenticated creator.
Another SuperAdmin cannot read that objective without a future explicit sharing policy.
Clients cannot select arbitrary graph IDs, credentials, agent identities, or remote endpoints.

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/swarm/v1/objectives` | Create an objective, idempotent by owner and request key |
| GET | `/swarm/v1/objectives` | List owned objectives with bounded offset pagination |
| GET | `/swarm/v1/objectives/{id}` | Read an owned objective |
| POST | `/swarm/v1/objectives/{id}/reviews` | Start or recover a request-keyed review |
| GET | `/swarm/v1/objectives/{id}/reviews` | List assessment runs with a revision cursor |
| GET | `/swarm/v1/objectives/{id}/reviews/{review_id}` | Read the exact input snapshot for an assessment |
| POST | `/swarm/v1/objectives/{id}/feedback` | Append feedback and start reassessment |
| POST | `/swarm/v1/objectives/{id}/status?status=paused` | Pause and cancel current reviews |
| POST | `/swarm/v1/objectives/{id}/status?status=active` | Resume; a new review requires a separate request |
| GET | `/swarm/v1/objectives/{id}/board?after=0&limit=50` | Read ordered journal entries |
| GET | `/swarm/v1/objectives/{id}/reviews/{review_id}/tasks` | Read task status, findings, and Aegra run references |

An objective request has this form:

```json
{
  "request_key": "eve-supply-objective-1",
  "title": "Synthetic EVE supply assessment",
  "goal": "Assess seven-day supply risk from the supplied observations.",
  "success_criteria": "Identify supported findings, uncertainty, and missing information. Do not place trades.",
  "evidence": [{
    "evidence_id": "stock-report",
    "source": "fixture:eve-stock",
    "observed_at": "2026-01-01T00:00:00Z",
    "available_at": "2026-01-01T00:00:01Z",
    "content": "Synthetic stock report: 200000 units. Consumption rate is unknown.",
    "simulation": true
  }]
}
```

Start a review with `{"request_key":"review-1"}`.
Submit feedback with `request_key`, `kind`, `content`, and optional `evidence`.
Permitted feedback kinds are `disagreement`, `additional_information`, and `outcome`.
An outcome report is evidence supplied by its author, not proof that an action occurred.

The same request key and input return the existing record. A changed input with that key returns `409`.
Only one review can run per objective. Feedback supersedes an active review and creates a fresh revision.
Later results from the superseded review cannot become the current assessment.
Pause also prevents late publication. It cannot reverse model usage that already occurred.

## Persistence and evidence

`swarm_objectives` stores the goal and a bounded projection of current evidence.
`swarm_reviews` stores immutable input snapshots, assessment revisions, deadlines, and lifecycle status.
`swarm_tasks` stores execution references, results, and retryable dispatch intent.
`swarm_entries` stores typed, append-only entries ordered within an objective.
The objective lock serializes lifecycle transitions and journal sequence allocation.
The board is not an unrestricted shared conversation or a private reasoning log.

Evidence records include an ID, source, observation time, availability time, content, and simulation flag.
Future-available evidence cannot enter a review snapshot.
A correction can replace an ID in the current projection. Prior review snapshots and journal entries retain the original observation.
Each assessment references its review snapshot, so its citations remain resolvable after corrections.
The runtime rejects citations outside that snapshot. Citation validity alone does not prove that a claim follows from its source.

The objective retains at most 40 current evidence records. Review snapshots must fit within 128 KB.
The model receives the latest assessment and a bounded recent board excerpt, not the complete journal.
The board API retains the complete ordered history with an `after` cursor.

## Recovery

Task, thread, Aegra run, and journal records commit before queue delivery.
A background reconciler retries pending dispatch with the same run ID after 30 seconds.
Redis workers use the existing lease and recovery mechanisms to fence duplicate deliveries.
Delivery is at least once. The system does not promise exactly-once model execution or billing.
Publishing a task result or assessment is idempotent under the objective lock.

The custom app lifespan starts the reconciler after Aegra initializes its database and executor.
Production uses the existing Redis worker configuration.
Local-executor recovery supports single-instance development only.
On restart, it resets unfinished workflow runs to pending and preserves their execution references.

## Verification and reference

PostgreSQL integration tests create disposable schemas. They do not use application records.
Set `SWARM_TEST_DATABASE_URL` to a test PostgreSQL connection URL before running:

```bash
uv run --package aegra-api pytest libs/aegra-api/tests/integration/test_swarm_coordination.py -q
```

Graph boundary tests require the graph modules on the Python path:

```bash
PYTHONPATH=graphs uv run --package aegra-api pytest libs/aegra-api/tests/unit/test_swarm_review.py -q
```

AI-Q is cloned at `~/nvidia-aiq` as reference source, initially at revision `951a1a1114e9818073ddbe2a5fdbfa824751f4d3`.
This implementation adds no AI-Q runtime dependency and copies no AI-Q source code.

### Live check on October 26, 2026

Authenticated Playwright requests exercised the real API, Redis workers, and configured main LLM with a labeled synthetic EVE objective.
The first coordinator assessment identified insufficient coverage from 200000 stock units and an estimated consumption of 70000 units per day.
An Aegra restart preserved the objective, its first assessment, and all ten initial journal entries.
Operator feedback corrected stock to 700000 units. The second assessment reported ten days of coverage and retained the earlier version.
A duplicate feedback request returned the same review ID. The journal contained one feedback entry and two assessment versions.
This check made no D3 request and resolved no external URLs.

Both initial specialist summaries contained inconsistent inventory statements. The coordinator reconciled the arithmetic in this case.
This result demonstrates the workflow, not reliable numerical judgment across scenarios.
Deterministic domain calculations and broader assessment-quality evaluations remain necessary for the market POC.
