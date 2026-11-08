# Virtual ISK paper trading

This API runs bounded paper-trading cycles inside Aegra.
The configured swarm LLM proposes one buy, sell, or hold per cycle.
PostgreSQL transactions apply deterministic checks and record each outcome.
SystemOne is optional and does not participate in this workflow.

No EVE credentials, wallet access, or order-submission transport exist here.
All accounts, fills, and returns are simulations, including accounts with public ESI observations.
A target such as 5% daily return is an objective to evaluate, not a promised result.

## Account and execution policy

The caller supplies virtual starting capital and one to three EVE type IDs.
The default market is The Forge, location 60003760, the Jita 4-4 station.
Other region and location IDs require explicit account settings.
Public collection uses fixed ESI URLs; callers cannot supply hosts or credentials.

Default paper assumptions are:

- A 1% fee on each buy and each sell
- A 0.1% adverse price adjustment on each fill
- A 25% maximum position share of post-fill marked equity
- A 10% loss threshold relative to starting capital.

These are configurable simulation assumptions, not verified character-specific EVE fees.
Prices, balances, fees, and average acquisition costs use decimal arithmetic.
Buys consume virtual cash including fees. Sells require existing holdings.
Short selling, borrowing, partial fills, and resting orders are unavailable.

The broker rejects expired quotes, insufficient cash or holdings, excessive size, and violated limit prices.
Buys must also satisfy exposure and loss limits. Risk-reducing sells remain possible after the loss threshold.
The loss threshold blocks new risk. It cannot prevent market gaps or guarantee a maximum portfolio loss.

Fills consume remaining best-price volume within an account's snapshot.
Re-fetching the same cached ESI page cannot replenish that snapshot's consumed volume.
New snapshots can contain new liquidity. Accounts are independent experiments, not one shared simulated exchange.
There is no queue-position model or claim that the corresponding real order would fill.

Equity uses fresh best bids for position marks plus cash.
The mark excludes future liquidation fees and depth beyond the best bid.
It is not a guaranteed liquidation value. Missing fresh marks produce a null equity and return.
Realized P&L includes acquisition costs, sale proceeds, and both fees.

## Data and context

The ESI collector reads orders at the account's single location.
It excludes buy orders with minimum volume greater than one.
It rejects books larger than five pages instead of using an incomplete best price.
Daily history contains the latest twenty regional observations, not executable station prices.
The collector buffers bounded responses and has a 35-second account refresh deadline.
Order snapshots expire within five minutes and respect earlier ESI expiry headers.

Manual quote submissions are always labeled fixture inputs.
The model receives policy, cash, positions, quotes, daily history, and five recent cycle outcomes.
An optional owned objective adds its goal, success criteria, and the latest eight current evidence records.
That evidence subset and total count are explicit in the cycle snapshot.
News collection and unrestricted model-led research are not part of this paper workflow.

## Durable cycles

Each start request has an idempotency key and a budget of one to twenty-four cycles.
The minimum interval is sixty seconds. Scheduling is best effort, not a market-latency guarantee.
Each cycle commits a RunJob before dispatch through the existing Aegra executor.
The same run ID is retried after a delivery gap.
Local restart recovery and production worker leases reuse the existing execution architecture.

One model call has a 120-second timeout and a 4,096-token output ceiling.
A cycle expires after four minutes. There are no model retries or forced trade fallbacks.
An account row lock serializes fill validation, balances, positions, quote usage, and the cycle's unique ledger record.
Duplicate completion cannot apply a second fill.
Stop prevents uncommitted fills even if a model later completes.
Cancellation delivery is best effort; late output has no trading authority.

Linked objectives receive typed `paper_execution` board entries in the same transaction.
Their latest paper outcome becomes a labeled simulation evidence record when the forty-record context has space.
Earlier board entries and review snapshots remain intact.
Pausing the linked objective stops the paper loop before execution.

## API

All routes require SuperAdmin access and enforce account ownership.
The prefix is `/swarm/v1/paper`.

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/accounts` | Create an idempotent virtual account |
| GET | `/accounts` | List owned accounts |
| GET | `/accounts/{id}` | Read policy, balances, marks, quotes, and loop state |
| POST | `/accounts/{id}/quotes` | Add an explicitly synthetic fixture quote |
| POST | `/accounts/{id}/quotes/refresh` | Collect public ESI orders and history |
| POST | `/accounts/{id}/start` | Start bounded autonomous cycles |
| POST | `/accounts/{id}/stop` | Stop further execution |
| GET | `/accounts/{id}/cycles` | Inspect execution status and run IDs |
| GET | `/accounts/{id}/cycles/{cycle_id}` | Inspect the exact model input and outcome |
| GET | `/accounts/{id}/ledger` | Read ordered outcomes, fills, and marks |

Create an account with this JSON:

```json
{
  "request_key": "eve-paper-account-1",
  "name": "EVE paper experiment",
  "starting_isk": "1000000",
  "type_ids": [34]
}
```

To link a mission, include its owned `objective_id` in that request.
Start two cycles with this JSON:

```json
{
  "request_key": "eve-paper-start-1",
  "cycles": 2,
  "interval_seconds": 300,
  "refresh_esi": true
}
```

The loop collects observations, proposes an action, validates it, records the outcome, and repeats within its budget.
A hold or rejection is a valid recorded outcome. It does not prove a successful trading strategy.
The control-plane trading UI remains planned; these endpoints are usable through authenticated API requests.

## Initial verification

The October 26, 2026 integration check read public Tritanium orders at Jita and twenty regional history records.
The configured `openai/gpt-5.4-mini` model completed two scheduled cycles without a second manual start.
Both decisions were holds. The virtual balance remained 10,000 ISK.
The account and its ledger survived an Aegra container replacement.
Earlier failed model calls remain visible as failed outcomes with no balance changes.

Separate PostgreSQL tests apply buy and sell proposals, validate their accounting, and reject duplicate fills and late output after stop.
These checks establish execution behavior. They do not establish profitable strategy performance.
