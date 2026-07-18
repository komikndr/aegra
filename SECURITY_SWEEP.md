# Security Sweep

Read-only security review of `/home/kxn/aegra` and `/home/kxn/vega_ai/executive-ai-canvas`.

No files, services, dependencies, or commits were changed during the audit itself. This document records only source-to-sink validated findings and separates exploitable defects from deployment hardening.

## Critical

### 1. Cross-Tenant Thread And Checkpoint Access Through Run Creation

References:

- `libs/aegra-api/src/aegra_api/services/run_preparation.py:101-110`
- `libs/aegra-api/src/aegra_api/services/run_preparation.py:173-227`
- `libs/aegra-api/src/aegra_api/services/run_preparation.py:271-278`
- `libs/aegra-api/src/aegra_api/services/langgraph_service.py:660-757`

Run endpoints accept a URL `thread_id` without first proving ownership. Thread metadata and resume validation query by thread ID without `user_id`. Client-controlled `config.configurable.thread_id`, `user_id`, and `langgraph_auth_user` survive because server values use `setdefault()`.

The shared LangGraph checkpointer is keyed by `thread_id`, allowing an authenticated attacker who knows another thread UUID to execute against that conversation state. Assistant lookup is also not owner-scoped.

Impact:

- Cross-user conversation disclosure
- Victim thread mutation
- Unauthorized assistant configuration use

Confidence: High

Remediation:

- Validate existing threads by `(thread_id, user.identity)` before every create, stream, wait, or resume operation.
- Scope metadata and resume queries by owner.
- Resolve assistants only when owned by the caller or explicitly system-owned.
- Reject client-supplied reserved configurable fields.
- Overwrite, rather than default, `thread_id`, `run_id`, `user_id`, and `langgraph_auth_user`.
- Add two-user IDOR tests across all run modes.

### 2. Pipeline Endpoint Permits Credential Exfiltration Through Attacker-Selected Model Endpoints

References:

- `custom_routes_example.py:1263-1310`
- `custom_routes_example.py:1358-1366`
- `custom_routes_example.py:1790-1949`
- `graphs/react_agent/utils.py:98-108`
- `graphs/react_agent/utils.py:195-233`

An authenticated workflow owner can configure an arbitrary model `baseUrl`. Aegra may forward `OPENAI_API_KEY` to that endpoint, including during `/models` discovery. Pipeline prompts and row data are also sent to the selected endpoint.

Impact:

- Server credential theft
- Pipeline data exfiltration

Confidence: High

Remediation:

- Never accept provider base URLs or credentials from workflow configuration.
- Map an allowlisted model identifier to immutable server-side provider settings.
- Ensure model discovery never forwards secrets to an untrusted host.

## High

### 3. Authenticated SSRF Through Pipeline Sink Nodes

References:

- `custom_routes_example.py:1081-1087`
- `custom_routes_example.py:1295-1310`
- `custom_routes_example.py:1358-1366`

Workflow owners control URL, method, and headers passed to `urllib.request.urlopen`. There is no scheme, hostname, resolved-IP, redirect, or private-network validation. Responses are fully buffered before truncation.

Impact:

- Access to localhost, internal services, RFC1918 networks, and cloud metadata
- Possible credential-bearing requests
- Memory exhaustion

Confidence: High

Remediation:

- Permit only `https`.
- Resolve and reject loopback, private, link-local, multicast, and reserved addresses for IPv4 and IPv6.
- Revalidate every redirect and protect against DNS rebinding.
- Restrict methods and headers.
- Stream responses with a strict byte cap.
- Enforce an outbound proxy or firewall.

### 4. Global Articles/OpenSearch Administration Is Available To Every Authenticated User

References:

- `custom_routes_example.py:2162-2247`
- `custom_routes_example.py:2314-2578`

Any authenticated user can overwrite global OpenSearch endpoint, credentials, TLS verification, and embedding-model configuration. Users can enumerate, create, ingest into, and query arbitrary indexes. The configurable endpoint also creates another authenticated SSRF path.

Impact:

- Global configuration takeover
- Cross-user search access
- Index corruption
- SSRF
- Potential credential disclosure

Confidence: High

Remediation:

- Restrict configuration and index administration to an explicit admin permission.
- Make provider configuration immutable at runtime.
- Namespace indexes per tenant and enforce ACLs.
- Validate endpoints against a server allowlist.
- Add ingestion count and aggregate-size limits.

### 5. Authentication Configuration Fails Open

Reference:

- `libs/aegra-api/src/aegra_api/core/auth_middleware.py:223-236`

If the configured auth instance cannot be loaded, the middleware deliberately creates an authenticated `anonymous` identity. All callers consequently share one authenticated tenant.

Impact:

- Authentication bypass after deployment or configuration error
- Cross-user resource collapse

Confidence: High

Remediation:

- Fail startup when configured authentication cannot load.
- Permit anonymous mode only through an explicit development-only setting.
- Add production startup tests for missing or invalid auth modules.

### 6. Graph Authorization Is Denylist-Like And Fails Open For Unknown Graph IDs

Reference:

- `libs/aegra-api/src/aegra_api/core/agent_access.py:49-71`

Only `kms`, `qa`, `analytic`, and `casual` prefixes have role mappings. Unknown registered graphs such as `agent`, `agent_hitl`, factory, and stress graphs are permitted.

Impact:

- Users may access graphs not represented in the prefix policy.

Confidence: High, assuming those graphs are intended to be restricted.

Remediation:

- Replace prefix inference with an explicit graph-to-role mapping.
- Deny unknown graph IDs by default.
- Validate all graphs in `aegra.json` at startup.

### 7. SQL Tools Rely On Bypassable Lexical Read-Only Checks

References:

- `graphs/dbchat_agent/tools.py`
- `graphs/analytic_agent/tools.py`

Model-provided SQL is passed to `connection.execute(text(query))`. Space-delimited keyword checks do not provide a reliable SQL security boundary.

Examples include data-changing CTE variants, SQLite writable PRAGMAs, PostgreSQL `SELECT INTO`, and side-effecting functions. Result-row limits do not prevent expensive or indefinitely running queries.

Impact depends on database credentials:

- Write access or data mutation with privileged credentials
- Query-based DoS and unintended reads with read-only credentials

Confidence: High for control weakness; deployment-dependent impact.

Remediation:

- Use a dedicated read-only database principal.
- Enforce read-only transactions at the database layer.
- Set statement and transaction timeouts.
- Parse and allowlist exactly one query statement using an AST parser.
- Explicitly reject dangerous PRAGMAs, CTE writes, `SELECT INTO`, and side-effecting functions.

### 8. Internet-Exposed Public Copilot Can Be Abused For Unauthenticated Model Consumption

Reference:

- `src/app/api/copilot/message/route.ts`

CORS is not authentication and does not stop non-browser callers. The endpoint has no request-size, message-length, rate, concurrency, visitor, or cost quota. It invokes Aegra through a shared service identity.

Impact:

- Model-cost abuse
- Service exhaustion

Confidence: High if publicly reachable.

Remediation:

- Add per-IP and per-visitor quotas, concurrency controls, message limits, and global budget protection.
- Require a tenant or site token if fully anonymous access is unnecessary.
- Bind visitors through a signed token or HttpOnly cookie rather than caller-controlled localStorage identifiers.

### 9. Frontend Dependency Set Contains Known Runtime Vulnerabilities

Read-only `npm audit` found 18 packages: 2 critical, 6 high, 9 moderate, 1 low.

Priority direct dependencies:

- `next@16.1.6`: upgrade to at least the offered patched `16.2.10`.
- `echarts@6.0.0`: upgrade to `>=6.1.0`.
- `@xmldom/xmldom@0.9.9`: upgrade to `>=0.9.10`.
- `xlsx@0.18.5`: vulnerable with no registry fix; replace or strongly isolate it.
- `jspdf@4.1.0`: upgrade beyond the affected range when a compatible version is available.

Avoid `npm audit fix --force`; its proposed NextAuth resolution is inappropriate.

Confidence: High for vulnerable versions; exploitability varies by used feature.

## Medium

### 10. Image Analysis Route Has No Count Or Aggregate Resource Limits

Reference:

- `custom_routes_example.py:1952-2012`

The route reads all images into memory, duplicates them as Base64, and submits them to an LLM. MIME validation checks only the declared `image/` prefix.

Impact:

- Authenticated memory, compute, and model-cost exhaustion

Remediation:

- Enforce image count, per-file bytes, aggregate bytes, decoded dimensions, magic-byte validation, rate limits, and proxy body limits.

### 11. Compressed-Document Parsing Remains Susceptible To Resource Exhaustion

Frontend upload routes cap compressed file size but do not constrain decompressed size, entry count, parser runtime, or memory. `xlsx` and XML-processing dependencies also have relevant advisories.

Remediation:

- Parse in an isolated worker with time and memory limits.
- Enforce archive expansion controls.
- Replace or isolate vulnerable parsing dependencies.

### 12. Development Compose Configuration Is Unsafe Outside A Trusted Workstation

Aegra compose publishes PostgreSQL, unauthenticated Redis, Keycloak, and Aegra on all interfaces. Keycloak uses HTTP and development credentials; Aegra runs with `--reload`. Frontend local MinIO configuration also uses development defaults and host networking.

Impact:

- High if deployed on an untrusted LAN or internet host
- Otherwise development hardening

Remediation:

- Use private Docker networks.
- Do not publish internal services on host interfaces.
- Enable Redis authentication and TLS.
- Require generated secrets.
- Disable reload outside development.
- Terminate TLS at the edge.

## Low / Hardening

- NextAuth and Keycloak configuration should fail startup instead of using fallback issuer or client-secret values in `src/auth.ts`.
- Add CSP, `frame-ancestors`, HSTS at the TLS edge, Referrer-Policy, Permissions-Policy, and explicit browser protection headers.
- Add a timeout to the STT upstream request.
- Stop returning raw backend or provider exception messages to clients.
- Limit ECharts option depth, series count, and data count.
- Consider proxying or allowlisting remote Markdown images to prevent tracking.
- Restrict production CORS to known frontend origins.

## Explicit Non-Findings

- No cross-client SSE broadcast or SSE newline injection was established. Run ownership is checked before subscription, channels are run-specific, and payloads are JSON encoded.
- `Object.fromEntries` did not pollute `Object.prototype` in the tested usage; the earlier claim was incorrect.
- Markdown does not enable raw HTML, and no stored Markdown XSS was found.
- User-scoped MinIO object retrieval properly authenticates and verifies key ownership.
- Sensitive frontend API routes inspected enforce their own sessions even when not covered by `proxy.ts`.
- No actual tracked `.env` or `.aegra-articles.json` secret file was found.

## Remediation Order

1. Close the run/checkpointer IDOR and reserved-config override.
2. Remove workflow-controlled model endpoints and credential forwarding.
3. Constrain or disable arbitrary pipeline egress.
4. Lock Articles/OpenSearch administration and tenant access.
5. Make authentication and graph authorization deny-by-default.
6. Enforce database-level read-only access and query timeouts.
7. Upgrade vulnerable frontend dependencies and replace `xlsx`.
8. Add public Copilot abuse controls and upload/parser resource limits.
9. Harden compose networking and browser headers.

## Required Decisions

1. Should public Copilot remain completely anonymous, or can it require a site or tenant credential?
2. Which registered graphs should ordinary authenticated users access? A deny-by-default mapping needs an explicit policy.
3. Do pipeline workflows genuinely require arbitrary external webhooks and custom model endpoints? Custom model endpoints should be removed regardless; webhook egress can instead use an allowlist.
4. Are the current compose files strictly development-only? If not, deployment hardening becomes an immediate high-priority requirement.

## Plan to Fix

Approved policy decisions:

- Integrate official Aegra through staged security and reliability backports rather than a full rebase.
- Disable pipeline sink/egress nodes by default, including legacy persisted workflows.
- Restrict all Articles/OpenSearch routes to `SuperAdmin` initially and make provider configuration server-controlled and immutable.
- Replace graph-prefix inference with an explicit deny-by-default policy. Every graph in `aegra.json` must have a policy or startup must fail.

Implementation sequence:

1. Preserve the current custom graph and security-report work, fetch the current official `aegra/aegra` main branch, and verify the target commits and release ancestry.
2. Port the upstream thread ownership fix (`e1b2042`), assistant ownership fix (`a5ae9f9`), and the non-cron thread/config/checkpoint protections from `2695989`.
3. Overwrite all server-owned run configuration fields, including `thread_id`, `run_id`, `user_id`, `user_display_name`, and `langgraph_auth_user`. Scope service-layer resume, metadata, and status operations by owner.
4. Port selected reliability improvements for SSE database-session release, keepalive handling, JSONB sanitation, worker recovery, and patched dependencies. Do not duplicate the Redis worker/broker, graph-factory, runtime-context, or graph-namespace features already present locally.
5. Make configured authentication fail startup instead of falling back to a shared anonymous tenant.
6. Define an explicit policy for every registered graph. Initial policy: authenticated access for QA and ordinary casual graphs; `Staff` for KMS graphs; `Executive` or `Senior Data Engineer` for analytic graphs; `SuperAdmin` or development-only access for factory, stress, subgraph, and legacy example graphs.
7. Replace workflow-controlled pipeline model/provider settings with opaque server-defined model IDs. Reject legacy `baseUrl`, provider credential, model, URL, method, and header fields during both persistence and execution.
8. Disable pipeline sink nodes at validation and execution time so legacy database rows cannot retain SSRF behavior.
9. Remove writable Articles/OpenSearch runtime configuration, load provider settings only from the server environment, enforce `SuperAdmin` on every Articles route, validate index names, and add ingestion/query limits.
10. Replace all three lexical SQL checks with a shared one-statement AST policy, server-owned database credentials, database-enforced read-only transactions, statement timeouts, and a dedicated read-only principal.
11. Add image count, per-file bytes, aggregate bytes, decoded pixel/dimension, actual MIME, prompt, concurrency, and rate limits before invoking the image model.
12. Stop returning raw provider, authentication, database, and internal exception details to clients.

Verification gates:

- Add two-user regression coverage for run create, stream, wait, resume, thread state/history, configurable-key overrides, and assistant ownership.
- Prove an attacker-selected pipeline endpoint receives no request, model data, or credential.
- Prove legacy pipeline workflows cannot enable custom model endpoints or sink nodes.
- Prove ordinary authenticated users receive `403` from all Articles/OpenSearch routes.
- Validate at startup that every configured graph has an explicit access policy and that unknown graphs are denied.
- Cover SQL write CTEs, stacked statements, `SELECT INTO`, writable PRAGMAs, locking clauses, unsupported dialects, rollback, and timeout behavior.
- Cover image count, byte, aggregate, format, dimension, pixel, and prompt limits and prove invalid requests never invoke the model.
- Run `make ci-check`, focused unit/integration tests, and `make e2e-both` so both local and Redis worker execution paths are exercised.

## Implementation Status (2026-07-18)

The historical findings above describe the code at audit time. The following backend remediations are now implemented in the current worktree.

### Fixed

- **Finding 1:** Run create, stream, wait, resume, metadata, and status paths enforce thread ownership. Assistant lookup is limited to caller-owned or system assistants. Server-owned identity, thread, run, and checkpoint configuration keys are stripped from client input and overwritten with authoritative values.
- **Finding 2:** Pipeline formatter nodes accept only opaque model IDs resolved from immutable server configuration. Workflow-controlled model names, provider URLs, and API keys are rejected during validation and execution, including for legacy persisted workflows.
- **Finding 3:** Pipeline sink nodes are rejected during both graph validation and execution. The arbitrary outbound HTTP implementation was removed, so legacy workflows cannot retain sink-based SSRF behavior.
- **Finding 4:** Articles/OpenSearch configuration is server-owned and immutable at runtime. All Articles routes require `SuperAdmin`; index names are prefix constrained; ingest and query payloads are bounded; provider errors are not returned verbatim.
- **Finding 5:** Configured authentication failures abort startup. Unknown auth types fail closed, production no-auth mode is rejected, unauthenticated identities are denied, Keycloak audience verification is required, and accepted roles are restricted to realm roles and the configured API client.
- **Finding 6:** Graph authorization uses an exact policy for every graph registered in `aegra.json`. Missing or extra policy entries fail startup, unknown non-empty graph IDs are denied, and role checks are explicit. Graphless empty threads remain permitted because they grant no graph execution.
- **Finding 7:** Analytic, DBChat graph, and custom DBChat SQL paths share a one-statement `sqlglot` policy, server-owned database credentials, read-only transaction controls, statement deadlines, rollback, and row limits. Unsupported dialects and mutation, locking, command, writable PRAGMA, and selected side-effect function forms fail closed.
- **Finding 10:** Image analysis enforces file-count, per-file, aggregate-byte, decoded-format, MIME, dimension, pixel, and prompt limits before model invocation, and closes uploads on every path.

### Reliability Backports

- SSE setup releases database sessions before returning long-lived responses.
- SSE uses `EventSourceResponse` heartbeats and transport-level disconnect cancellation without treating ordinary generator cancellation as an execution failure.
- Streamed internal failures return generic client messages while detailed errors remain server-side.
- Redis queue socket timeouts are treated as normal idle behavior; actual Redis failures still trigger fallback handling.
- All JSONB columns recursively strip PostgreSQL-invalid NULL bytes at bind time with depth and key-collision safeguards.
- Dataclass graph-factory contexts ignore undeclared server context fields while retaining authoritative context for mapping and Pydantic contexts.

### Remaining Controls

- SQL safety still requires deployment of a dedicated database principal with database-level read-only grants. The application policy is defense in depth, not a substitute for least privilege.
- Image-analysis distributed rate limits, concurrency quotas, and reverse-proxy body limits remain deployment controls.
- Articles access is intentionally `SuperAdmin`-only. A tenant index ownership/grant model is still required before ordinary-user access can be enabled safely.
- **Finding 8** (public Copilot abuse), **Finding 9** (frontend dependency advisories), and **Finding 11** (compressed-document parser isolation) are frontend work and remain open.
- **Finding 12** (development compose exposure) remains open. Current compose files must be limited to trusted development hosts until networking, credentials, TLS, Redis authentication, and reload settings are hardened.
- Browser security headers, broad rate limiting, and remaining low-severity error-hardening opportunities remain follow-up work.
- Python advisory scanning against `uv.lock` remains pending because no advisory scanner was installed during this work.

### Verification Evidence

- API unit suite: 1,147 passed.
- API integration suite: 282 passed, 1 skipped.
- CLI suite: 181 passed.
- Focused authentication, custom-route, and SQL security suite: 70 passed.
- Focused graph model/config/parallel-tool suite: 15 passed.
- LocalExecutor E2E: 88 passed, 2 skipped, 6 production-only tests deselected.
- Redis WorkerExecutor E2E: 94 passed, 2 skipped, including all Redis worker tests.
- Focused factory E2E: 6 passed in each execution mode.
- Ruff checks passed for all changed and new Python files, and the format check reported 118 files already formatted. Targeted `ty` checks for the security, Agent Protocol v2, model, observability, and executor changes passed; a full-repository `ty` clean result is not claimed because of existing baseline diagnostics.
- `uv lock --check`, deterministic OpenAPI `--check`, modified-JSON parsing, and `git diff --check` passed.
- The rebuilt database reached Alembic head `20260718090000`; PostgreSQL has the assistant metadata GIN index and fixed-width unique config-hash index. The restored authenticated stack reported healthy database, checkpointer, store, and analytic database connections.

### Upstream Alignment Completed

The staged alignment with official Aegra v0.9.24-era behavior now also includes:

- Checkpoint-only and fork/resume requests preserve `input=null` instead of restarting at `__start__`; null state updates with checkpoint or node targets remain valid.
- Redis event writes are serialized and independently retried for replay-cache and publish operations. Local and Redis brokers provide fan-out semantics and suppress adjacent duplicate event IDs caused by at-least-once delivery.
- The custom Alembic chain remains single-headed and adds fixed-width assistant-config uniqueness plus assistant metadata GIN indexing without importing a conflicting upstream migration branch.
- API and CLI packages identify as `0.9.24+onyx.1`; patched dependency floors and the lockfile were reconciled while retaining SQLGlot, Pillow, OpenSearch, and other custom runtime dependencies.
- Assistant authorization is enforced inside the service layer. Auth-handler metadata filters, exact graph policy, escaped search wildcards, stable sorting/pagination, filtered counts, and safe class serialization are covered by regression tests.
- Agent Protocol v2 event streaming is available behind `FF_V2_EVENT_STREAMING`, with authenticated thread-scoped command and event endpoints, native LangGraph v3 channels, replay cursors, HITL handling, and matching local/Redis fan-out behavior.
- The standard Agent Protocol OpenAPI schema is generated deterministically and checked in CI. Custom DBChat, Pipeline, Articles, Artifacts, and Media routes use separate `Onyx` tags and do not leak into the standard exported schema.
- Run metadata is bounded at request validation, persisted for workers, merged with authoritative runtime IDs, and emitted through vendor-neutral OTEL `aegra.*` and `metadata.*` attributes. This supports MLflow through generic OTLP without adding a direct MLflow or Langfuse integration.

This is a logical security and protocol alignment, not a history-level merge of every upstream feature. Cron scheduling and low-priority store namespace normalization remain intentionally excluded.

## Dependency Audit Notes

Python advisory scanning was not completed because neither `pip-audit` nor `osv-scanner` was installed during the audit and installing tools was out of scope at the time. Add a CI scan against `uv.lock` using `pip-audit`, OSV, or equivalent.

## Upstream Aegra Main Comparison (v0.9.24)

This section maps each finding to the state of the official upstream `aegra/aegra` main branch (around v0.9.24) and notes what has been resolved, remains, or is fork-specific.

| # | Finding | Upstream Status | Notes |
|---|---------|----------------|-------|
| 1 | Cross-tenant thread/checkpoint access through run creation | **Fixed upstream** | Advisory CVE-2026-44504 / GHSA-m98r-6667-4wq7. Patched from v0.9.7 onward. Current main checks thread ownership for `create`/`stream`/`wait`, pins server `thread_id`/`run_id`, strips checkpoint/config overrides, and scopes assistants to caller or system. Equivalent fixes are now ported to this custom fork. |
| 2 | Pipeline model credential exfiltration | **Fork-only** | Not present in upstream; out-of-tree custom route. |
| 3 | Pipeline SSRF | **Fork-only** | Not present in upstream; out-of-tree custom route. |
| 4 | Global Articles/OpenSearch admin | **Fork-only** | Not present in upstream; out-of-tree custom route. |
| 5 | Authentication fails open | **Partially improved upstream** | Upstream added stronger warnings, but a configured auth load failure can still fall back to anonymous. Recommendation: fail closed when auth is configured. |
| 6 | Graph authorization prefix fail-open | **Fork-only** | Custom fork policy; not present in upstream. |
| 7 | SQL lexical checks | **Fork-only** | Applies to custom fork graph agents only. |
| 8 | Public Copilot abuse | **Frontend-only** | Not an upstream backend concern. |
| 9 | Frontend dependencies | **Frontend-only** | Not an upstream Python concern. |
| 10 | Image analysis limits | **Fork-only** | Custom fork route. |
| 11 | Compressed doc parser limits | **Frontend-only** | Not an upstream backend concern. |
| 12 | Dev compose unsafe defaults | **Still an issue upstream** | Deployment-hardening gap persists in both upstream and local compose files. |

### Broader upstream improvements (v0.9.24+)

- `SECURITY.md` and published security advisory added.
- CodeQL and private-key detection enabled in CI; Bandit and `ty` are non-blocking.
- Dependency upgrades applied across the stack.
- Dynamic graph namespace sanitization and per-request graph factories with auth context.
- Cron job improvements: ownership checks, quotas, and atomic claiming.
- Streaming DB sessions released before long SSE/wait responses.
- Redis worker/broker architecture adopted.
- Assistant `LIKE` wildcard escaping in search queries.
- Configurable CORS (previously broad/wildcard).

### Residual upstream hardening gaps

- `inject_user_context` still uses `setdefault` for `user_id`/`user_display_name` — caller can override.
- Service-layer metadata, resume, and status helpers still rely on caller prechecks rather than enforcing ownership.
- Raw exception strings can still be returned to clients in some error paths.

### Recommended integration order for custom fork

1. Port official run/thread/config/assistant/cron security fixes from upstream.
2. Update backend dependencies to the latest patched versions.
3. Selectively merge runtime, worker, and session improvements.
4. Independently remediate fork-only findings: pipeline credential/SSRf, Articles/OpenSearch admin, SQL lexical checks, graph RBAC, image upload limits.
5. Address frontend-only findings: vulnerable dependencies, Copilot abuse controls, compressed doc parsing limits.
6. Add custom regression tests covering each fork-specific finding.
