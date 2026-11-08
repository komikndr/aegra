# Paired research comparison

`evaluations.swarm.comparison` compares short planned reviews with bounded strategic search.
It uses the configured swarm model and existing worker functions inside Aegra.
It creates no objectives, tasks, board entries, market requests, or portfolio transactions.

## Experimental contract

The version is `onyx-paired-research-v1`. Five synthetic scenarios cover conflicting policies, missing consumption, expired quotes, negative costs, and corrected inventory.
Both workflows receive the same snapshot hash for each scenario and repetition.
The default experiment repeats each scenario twice and reverses workflow order on the second repetition.

Both workflows share an eleven-call admission ceiling, 4096 output tokens per call, a 160 KB worker-context limit, and a 600-second trial deadline.
Failed model requests consume call admissions. Deterministic calculations and verification jobs do not.
Equal ceilings do not mean equal actual spending. A short review can finish without consuming unused capacity.

The short runner executes planned tasks sequentially. Strategic search executes independent branches concurrently, with at most two branches and two passes.
These isolated runners do not reproduce production queue timing or test PostgreSQL recovery.
They compare worker behavior under controlled evidence. Acquisition remains disabled.

The snapshot clock is fixed. Historical expiry checks use that clock, not the current date of a replay.
The runner records model identities, structured outputs, intermediate artifacts, provider token usage when available, errors, and elapsed time.
Missing usage remains unknown rather than becoming zero. Failed schema generations can lack a decoded output; error types remain available.

## Commands

Run commands from the Aegra repository. The output directory must already exist.

```bash
PYTHONPATH=.:graphs uv run --env-file .env python -m evaluations.swarm.comparison \
  --validate-only --output /tmp/opencode/onyx-paired-inputs.json

PYTHONPATH=.:graphs uv run --env-file .env python -m evaluations.swarm.comparison \
  --live --repetitions 2 --output /tmp/opencode/onyx-paired-research-v1.json

PYTHONPATH=.:graphs uv run python -m evaluations.swarm.comparison \
  --responses /tmp/opencode/onyx-paired-research-v1.json \
  --output /tmp/opencode/onyx-paired-research-v1-replay.json
```

Live mode incurs model usage. Replay mode makes no inference requests.
Use repeated `--case` arguments to select scenarios. The default includes all five.
Exit code 1 means that at least one automated check failed. Exit code 2 identifies an invalid invocation or capture.
Live mode saves a checkpoint after each trial. An incomplete checkpoint cannot receive a complete paired score.
Temporary report paths are not durable storage. Select a persistent output directory when retaining an experiment.

## Initial live result

On October 28, 2026, the configured `openai/gpt-5.4-mini` model completed twenty trials: five scenarios, two repetitions, and two workflows.

| Measurement | Short planned review | Strategic search |
| --- | ---: | ---: |
| Trials passing all automated checks | 8/10 | 5/10 |
| Actual model calls | 14 | 52 |
| Provider input plus output tokens | 28,806 | 82,905 |
| Sum of trial elapsed time | 34.07 seconds | 114.92 seconds |

Strategic search used approximately 2.88 times as many tokens in this run. It did not demonstrate an automated-score advantage.
The first capture used a 500-second inner deadline for the short runner beneath the shared 600-second outer deadline.
No trial approached either limit. Subsequent runs pass the shared deadline into both runners explicitly.

Observed failures were:

- Both short conflicting-policy trials failed during planner or specialist execution. One decoded generation failed schema validation; the other worker raised `ValueError`.
- Both strategic conflicting-policy trials omitted a required coverage figure, despite identifying the 400,000-unit stress shortfall. Numeric omission does not establish incorrect arithmetic.
- Both strategic missing-consumption trials included a controller action for nonexistent `branch_2`. Exact active-branch validation rejected those decisions.
- One strategic expired-quote trial produced `wait` while citing expired evidence. The guard requires `needs_information` for that evidence and rejected both branch attempts.

The saved failures remain part of the baseline. Repeated runs must not replace an unfavorable baseline without an explicit experiment version or explanation.

## Scoring and human review

Both workflows receive the same final-output checks: input identity, execution, resource admissions, assessment schema, scoped citations, availability, explicit expiry, and required numeric mentions.
Citation and uncertainty presence are mechanical checks. Numeric matching does not establish units, claim alignment, or absence of contradictions.
The report does not reward extra branches, longer reports, or model-written claims of successful verification.

Each trial reserves unscored human-review fields for claim support, numerical interpretation, alternative quality, correction quality, and unsupported execution claims.
For each field, record a judgment and the exact output passage and evidence or receipt that supports it.
Use “not applicable” when a simple missing-input case does not require competing strategies. Do not award invented alternatives additional credit.
Keep these judgments separate from automated pass counts. The initial report leaves them unset.

Two repetitions cannot establish statistical reliability or general research superiority.
The five scenarios emphasize evidence discipline and arithmetic. They do not establish performance on open-ended discovery, live strategy outcomes, or actual trading.

Next work is active-branch controller reliability, missing-input termination, supported multi-record calculations, and reviewed claim quality.
Any fixes require a new, labeled comparison while retaining this baseline.
