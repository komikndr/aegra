# Coordinator and the Objectives workspace

Coordinator has two scopes: natural head chat and an objective-specific sub-coordinator.
Both use the main LLM through the existing model configuration.
The `coordinator` entry in the agent configuration can select a different model. SystemOne is not required.

General Assistant chat keeps its existing Casual, QA, KMS, and Analytics agents.
SuperAdmin users can select **Coordinator** in the standard Chat agent selector and speak naturally.
No objective selection is necessary. The head Coordinator finds authorized objectives internally.
It asks for clarification when a request matches multiple objectives.
Each objective keeps its own sub-coordinator and collapsible conversation panel.
Head chat has normal chat history. A delegated message also appears in the target objective's conversation and activity history.

## Head chat and routing

Ask a general question, request an objective status summary, or ask for help with a named objective.
The `coordinator_chat` graph provides discovery, bounded context, delegation, and saved-reply tools.
Every tool checks server-pinned identity, SuperAdmin access, and objective ownership.
This initial policy does not grant cross-owner access or implement shared-objective roles.

The head forwards the exact current user message. It cannot add model-authored instructions to that message.
The handoff commits a durable sub-coordinator turn before delivery. Retrying the same tool call reuses that turn.
The head waits up to 20 seconds for its reply. A pending receipt remains pending, with a link to the objective workspace.
The head can retrieve the saved reply on a later request.
Each head model call has a 120-second deadline. Each chat turn permits at most eight sequential tool calls.
The head does not submit orders or bypass the sub-coordinator's action checks.

## Use the workspace

1. Sign in with a SuperAdmin account.
2. Open **Objectives** in the Control Plane navigation.
3. Create an objective with a goal and measurable success criteria.
4. Add initial evidence if available.
5. Select **Request review** to start the evidence-review workflow.
6. Read the latest assessment, its evidence references, and its uncertainty.
7. Ask Coordinator about the objective or request a supported workflow action.

The conversation, activity, evidence, and paper accounts belong to the selected objective.
Objective management lives at `/control-plane/objectives`. Previous `/dashboard/objectives` links redirect there.
The head Coordinator remains in Assistant Chat and links to the Control Plane workspace.
The list shows the latest assessment and review status. Status requests do not invoke an LLM.
The workspace refreshes status every five seconds after the previous request completes.

## Conversation and action receipts

An objective sub-coordinator turn makes one bounded model call. It has no external retrieval or execution tools.
The model returns an answer and at most one proposed workflow action.
Trusted reconciliation applies a valid action and saves its receipt in the same transaction as the completed turn.

Supported actions are:

- Request a review
- Record additional information, disagreement, or an outcome report
- Pause or resume the objective
- Start a bounded paper loop for a linked account
- Stop a paper loop for a linked account.

Coordinator cannot create an account, change capital or risk limits, or submit real orders.
A paper action must reference an account owned by the user and linked to the current objective.
A paused objective cannot start research or paper execution. The conversation remains available for questions and resume requests.

The answer is model-generated text. An **applied** receipt identifies an accepted command, not a completed trade or research result.
A **refused** receipt identifies a server check that prevented the command.
Research outcomes appear in assessments. Paper outcomes appear in the portfolio and activity history.

## Correct evidence

Conversation feedback records the exact user message. It does not replace an evidence record.
To correct a record, use **Feedback and outcomes**:

1. Enter the report and select its feedback type.
2. Select the evidence ID to replace, or select a new record.
3. Enter the replacement content.
4. Select **Submit feedback and reassess**.

The server keeps previous review snapshots. The new review uses the corrected current evidence.
The form records the submission time as the observation and availability time.
Use the API when source-specific timestamps are necessary.

## Paper portfolio

The **Paper portfolio** section uses the existing virtual-ISK broker.
The user supplies the starting capital and EVE item IDs. The form shows the fee, slippage, exposure, and loss policy.
The default location is Jita 4-4 in The Forge. The policy is fixed when the account is created.

**Start paper loop** requests one to 24 cycles with a bounded interval and public ESI refresh.
**Stop paper loop** prevents pending proposals from filling when the broker reconciles them.
The workspace shows cash, positions, fresh quotes, marked return, and execution outcomes.
Missing fresh position quotes produce an unavailable valuation, not a fabricated value.

See [Paper trading](./paper-trading.md) for accounting assumptions and broker limits.

## API and durability

The new API routes are:

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/swarm/v1/objectives/{id}/conversation` | Submit `{request_key, message}` |
| GET | `/swarm/v1/objectives/{id}/conversation` | Read the latest 50 turns; maximum 100 |
| GET | `/swarm/v1/objectives/{id}/evidence` | Read current evidence |

Objective list and detail responses also contain the latest assessment and review status.
The board supports `latest=true` for the latest bounded page. The original ascending cursor API remains available.
Paper account listing supports an owner-checked `objective_id` filter.

All routes require SuperAdmin access and enforce objective ownership on the server.
The frontend uses `/api/swarm/...`, with the same role check and a fixed path allowlist.
Browser requests cannot select another upstream host or forward arbitrary credentials.

`coordinator_turns` stores the request, existing Aegra run ID, deadline, delivery timestamp, result, and action receipt.
One turn can be pending per objective. Duplicate request keys return the original turn; changed messages with the same key return `409`.
The dispatch intent commits before queue delivery. Delivery retries reuse the same run ID.
Existing worker leases and local recovery apply. An expired turn cannot apply a late model action.

The graph has a 120-second model deadline and a bounded context. The durable turn has a four-minute deadline.
Context contains bounded excerpts of current evidence, recent board entries, prior conversation, the latest assessment, and linked paper accounts.
It does not read another objective, private model reasoning, or arbitrary URLs.

The objective workspace shows the latest 100 activity entries and 50 conversation turns.
**Control Plane → Execution inspector** shows review tasks and exact review input snapshots.
The API remains available for older journal pages and execution records.
