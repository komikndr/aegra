# Bounded objective supervision

The supervisor runs inside Aegra. Its default workflow is `evidence_review_v1`.
PostgreSQL stores its policy, next wakeup, waiting state, review reference, and budget reservations.
The existing executor runs the analyst, skeptic, and synthesis tasks. No additional queue or service is necessary.
D3 is optional and does not gate this loop.

## Scope and states

An explicit start schedules the first review immediately. A completed assessment with disposition `wait` schedules the next review after the configured interval.
Disposition `needs_information` suspends timer-based reviews. A failed or refused review also waits for input instead of retrying indefinitely.
An objective without evidence waits without model calls.

A changed current-evidence projection or an explicit wakeup can resume a waiting supervisor.
This includes evidence from a linked paper account when that account writes an execution outcome.
The supervisor does not fetch new sources by itself. Repeated reviews can use the same evidence unless a connector or human changes it.
Each review still receives the previous assessment, bounded recent activity, and the current evidence snapshot.

Concurrent wakeups coalesce under the objective lock. The supervisor waits for an existing human-requested review instead of starting a duplicate.
Human feedback can supersede an active review through the existing feedback API.
The supervisor then follows the replacement review.

Pause stops supervision in the same transaction. Resume does not restart it; a new explicit start is necessary.
Stop cancels the supervisor's current review, but does not cancel an unrelated human-requested review.
Late results cannot publish an assessment for a cancelled or expired review.
Cancellation cannot reverse model usage that already occurred.

The default workflow has a fixed review plan. The opt-in [planned review](./swarm-planning.md) selects approved read-only specialist tasks.
The opt-in [strategic search](./swarm-strategy.md) develops independent alternatives with critique, verification, and bounded revision.
These workflows do not place orders or start paper cycles. Approved action selection remains a separate backend milestone.

## API

All endpoints require SuperAdmin and objective ownership.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/swarm/v1/objectives/{id}/supervisor` | Read policy, state, wakeup, deadline, and counters |
| POST | `/swarm/v1/objectives/{id}/supervisor/start` | Start an explicit bounded policy |
| POST | `/swarm/v1/objectives/{id}/supervisor/wake` | Persist an immediate wakeup |
| POST | `/swarm/v1/objectives/{id}/supervisor/stop` | Stop the supervisor and cancel its current review |

Example start body:

```json
{
  "request_key": "supervision-1",
  "max_reviews": 2,
  "interval_seconds": 300,
  "max_elapsed_seconds": 3600,
  "max_model_calls": 6,
  "max_specialist_tasks": 4
}
```

Example wake or stop body:

```json
{
  "request_key": "stock-report-event-123",
  "reason": "An updated stock report is available."
}
```

The request key identifies a command within the objective. The same key and body return current state without applying the command again.
A changed command with the same key returns `409`. Retrying an old start does not restart a stopped generation.
Wake reasons describe the trigger; they do not replace evidence or authorize another workflow.
Several events during one active review produce at most one pending follow-up wakeup.

An active policy cannot change in place. Stop it before starting a new generation with a new request key.

## Budgets and recovery

The maximum policy permits 24 reviews, 72 logical model calls, and 48 specialist tasks per start.
Intervals range from 60 seconds to one day. Elapsed-time budgets range from five minutes to seven days.
The elapsed deadline applies even while the supervisor waits for information.

Before a review starts, one transaction reserves its full worst-case logical budget:

| Workflow | Model calls | Specialist tasks |
| --- | --- | --- |
| Fixed evidence review | 3 | 2 |
| Short planned review | 4 | 2 |
| Strategic search | 11 | 8 |

These reservations remain charged after failures or supersession. The supervisor never creates a partially funded review.
Each call uses the existing 4096-output-token limit, zero SDK retries, and 120-second deadline.
The review deadline cannot exceed the supervisor deadline.

These limits cover supervisor-created reviews, not independent human requests, Coordinator conversations, or paper cycles.
Reservations count logical work, not exact provider billing. A crash can repeat a model call under at-least-once delivery.
The system does not promise exactly-once inference.

Review creation, budget reservation, and journal entries commit atomically before worker delivery.
Restart retains the same review and run IDs. The existing dispatch reconciler recovers committed but undelivered work.
Periodic checks use persisted oldest-check ordering so the first hundred waiting objectives cannot monopolize every tick.
The `supervisor_state` journal records scheduling, waiting, and stop reasons.

## Verification

The PostgreSQL tests use disposable schemas and synthetic worker results.
They cover concurrent ticks, duplicate events, changed command input, budget exhaustion, waiting conditions, evidence changes, pause/resume, stop, deadlines, and local recovery.
They also cover interaction with manually requested reviews and owner-scoped API access.

A live synthetic arithmetic check completed two scheduled reviews after one start request.
The real model workflow used the existing analyst, skeptic, and synthesis roles.
The supervisor then stopped at two reviews, six reserved model calls, and four reserved specialist tasks.
Container replacement preserved both review IDs, both assessment entries, and the stopped state.
This check demonstrates scheduling and persistence, not decision accuracy or trading performance.

The Coordinator's objective context includes supervision state and counters for read-only inspection.
Control Plane objective workspaces expose Start, Wake, and Stop controls, next wakeup, stop reason, and remaining reservations.
The objective's active/paused status is separate from whether a review is running, scheduled, waiting, or stopped.
An active objective with stopped supervision does not automatically consume model calls.

An authenticated live check on October 28, 2026 started two planned public-ESI market reviews with a sixty-second interval.
The backend restarted after the first review completed and before its saved next wakeup.
The second review started automatically after restart. Both reviews completed, with one assessment per revision and no blockers.
The same supervisor generation stopped at two reviews, eight reserved model calls, and four reserved specialist tasks.
The first supplied quote produced approximately -3.18% for an immediate round trip under the configured costs.
Both assessments allowed a no-opportunity conclusion. The check submitted no orders and changed no portfolio.

See the [backend checklist](./swarm-backend-todo.md) for the remaining autonomous-planning work.
