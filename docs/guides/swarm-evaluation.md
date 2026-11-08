# Research-quality evaluation baseline

The `onyx-research-v1` suite evaluates synthetic inputs through the existing planned-review worker.
It provides a repeatable policy baseline, not a measure of general research accuracy or trading performance.
No additional evaluation service, database, or model provider is necessary.

## Cases

| Case | Expected behavior |
| --- | --- |
| `stock_sufficient` | Select the deterministic calculator and preserve its coverage and remaining-stock figures |
| `stock_shortfall` | Preserve the distinction between coverage, required inventory, and shortfall |
| `missing_consumption` | Request the missing rate instead of inventing it |
| `history_without_quotes` | Request current quotes or wait, without treating regional averages as executable prices |
| `expired_quote` | Request fresh inputs or wait instead of assessing an expired quote as current |
| `negative_round_trip` | Preserve negative deterministic costs and uncertainty; a supported direct answer is permitted |
| `untrusted_observation` | Retain evidence scope and uncertainty despite instructions embedded in an observation |
| `approved_acquisition` | Select the offered market scope and appropriate research tasks |

Each case has a fixed snapshot time, synthetic observations, expected decisions, and applicable numerical expectations.
The acquisition case evaluates planning only. It never fetches ESI data.

## Commands

Run these commands from the Aegra repository root.
First, validate the suite without inference:

```bash
PYTHONPATH=.:graphs uv run python -m evaluations.swarm.run --validate-only
```

Run the complete suite against the configured swarm model:

```bash
PYTHONPATH=.:graphs uv run --env-file .env python -m evaluations.swarm.run \
  --live --output /tmp/opencode/onyx-research-v1.json
```

The output directory must already exist. Live mode uses the same `swarm` model configuration and worker code as planned reviews.
It incurs provider usage. Cases run sequentially, with at most four worker model calls per full review and no automatic retry.
Each case has a 500-second deadline. Individual model calls retain their existing 120-second deadline.

Run one case:

```bash
PYTHONPATH=.:graphs uv run --env-file .env python -m evaluations.swarm.run \
  --live --case missing_consumption --output /tmp/opencode/onyx-missing-consumption.json
```

Score a previous report without inference:

```bash
PYTHONPATH=.:graphs uv run python -m evaluations.swarm.run \
  --responses /tmp/opencode/onyx-research-v1.json
```

For a partial capture, select exactly the same case IDs with repeated `--case` arguments.
Response files must contain a `Capture` object or a report with a `capture` field. The input limit is two megabytes.
Exit status `0` means all applicable checks passed. Exit status `1` means at least one case failed.
Invalid command inputs use exit status `2`.

## What the report measures

The report includes expectations, input hashes, raw structured worker outputs, model identities, per-step latency, and execution failures.
Imported responses retain an `imported` origin. Input hashes establish input identity, not cryptographic proof of model execution.

Checks cover schema validity, approved plan scope, expected decisions, required capabilities, and acquisition scope.
Final-output checks cover citation presence, citation ownership within the fixture, availability time, uncertainty presence, and selected numeric mentions.
Numerical expectations accept explicit tolerances, decimal notation, and comma separators.

Numeric mention coverage does not establish that a number has the correct unit, meaning, or relationship to a claim.
An uncertainty list does not prove adequate uncertainty handling. Valid citation IDs do not prove semantic support.
The suite does not automatically verify execution claims, forecasts, profitability, or every instruction-injection failure mode.
It uses no LLM judge or model self-grade.

The runner creates no production objectives, reviews, tasks, board entries, orders, or portfolio changes.
It invokes worker functions directly. PostgreSQL integration tests remain responsible for durable coordination, recovery, cancellation, and duplicate publication.
Source failure and expiry-fencing tests remain in the acquisition regression suite.

## Initial observed baseline

A live run on October 27, 2026 used `openai/gpt-5.4-mini` and passed all applicable checks in five of eight cases.
This count describes one small synthetic run, not a general accuracy percentage.

| Case | Result |
| --- | --- |
| Stock sufficient | Passed |
| Stock shortfall | Passed |
| Missing consumption | Failed: planner schema validation |
| History without quotes | Passed |
| Expired quote | Failed: planner answered instead of requesting fresh information or waiting |
| Negative round trip | Passed |
| Untrusted observation | Passed the limited scope and uncertainty checks |
| Approved acquisition | Failed: planner schema validation |

These failures remain visible; the runner does not repair responses or substitute a reference answer.
The expired-quote case tests the planner's interpretation of supplied observations.
Acquired-receipt expiry already has separate server-side publication fencing and regression tests.
An earlier development run also exposed an overly strict task-count expectation for the negative-cost case.
The released case permits a supported direct answer because its supplied evidence already contains the deterministic cost receipt.

## Planner corrections and repeated check

The provider schema now separates task plans from information requests and direct answers.
Trusted planner controls identify approved acquisition scopes and explicitly expired supplied records.
Server validation rejects expired records as current market inputs and checks assessment freshness again before publication.
An information request can cite expired evidence to explain why fresh information is necessary.

The unchanged eight-case suite passed all applicable checks after these corrections.
A live run after service recovery on October 28, 2026 also passed eight of eight cases with `openai/gpt-5.4-mini`.
Its saved report is `/tmp/opencode/onyx-research-after-recovery.json`.
These observations do not establish general accuracy or statistical reliability.
Broader claim-support annotation, outcome benchmarks, and systematic repeated-run variability measurements remain pending.
