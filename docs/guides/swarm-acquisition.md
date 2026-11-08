# Approved evidence acquisition

Planned reviews can collect approved EVE market evidence before specialist analysis.
This capability belongs to the existing Aegra backend. It does not create another application or submit orders.

## Server configuration

Set `SWARM_EVE_MARKETS` to a JSON object of named scopes:

```dotenv
SWARM_EVE_MARKETS={"jita-tritanium":{"region_id":10000002,"location_id":60003760,"type_id":34,"fee_bps":100,"slippage_bps":10}}
```

Each scope contains one region, one location, and one item type. The catalog permits ten scopes.
The default empty catalog disables acquisition. Scope IDs contain lowercase letters, digits, underscores, or hyphens, with a 64-character limit.
Models cannot supply endpoints, credentials, regions, locations, or item IDs outside this catalog.
All requests use the fixed public ESI host. No EVE account credentials are necessary.

The fee and slippage values are explicit calculation assumptions. They are not observed exchange fees or an account's actual tax policy.

## Start a review

Send this request as the owning SuperAdmin:

```http
POST /swarm/v1/objectives/{id}/reviews
Content-Type: application/json

{
  "request_key": "current-market-1",
  "workflow": "planned_review_v1",
  "allow_market_acquisition": true
}
```

Existing requests keep their previous behavior. Acquisition requires this explicit opt-in and at least one configured scope.
An objective can start this review without manually supplied evidence.
For scheduled reviews, include the same flag and workflow in the supervisor start request.

The objective sub-coordinator can also propose this workflow after an explicit request to research current EVE market evidence.
It receives the approved catalog and returns a separate server action receipt. Ordinary questions do not start collection.

## Planning and execution

The planner selects `acquire_market` from the offered scopes or returns null.
It can assign `market_analysis` and `skeptical_review` using these prospective evidence IDs:

```text
eve:<scope_id>:quote
eve:<scope_id>:history
```

The server validates the complete plan before dispatch. Supplied evidence cannot shadow these acquired records.
One non-model acquisition task runs before the selected specialists. It reuses the existing committed RunJob, worker, and recovery lifecycle.
The plan still permits two specialists and at most four logical model calls, including planning and synthesis.
Supervision reserves the existing four-model-call and two-specialist budget; the separate acquisition step makes no model call.

Collection reads up to five order-book pages and the latest twenty regional daily history records.
It rejects oversized books rather than treating an incomplete book as complete evidence.
The complete collection has a 35-second deadline. Existing streaming limits bound each order page to 2 MB and history to 500 KB.
Redirects and compressed responses are rejected. There is no automatic fallback source.

## Evidence and provenance

PostgreSQL retains a receipt with the selected scope, content version, retrieval time, expiry, source identity, and normalized evidence.
The original review input snapshot remains unchanged. The review detail endpoint exposes the separate `acquisition` receipt.
Specialist inputs contain only their selected evidence records. Synthesis can cite the acquired records within that review.

Daily history describes regional aggregates. It does not establish executable location prices.
The quote describes the selected location's best bid, ask, and available top-of-book volume.
It does not prove that a future order will fill.

The upstream event time is unknown. Quote `observed_at` records collection time, not the age of the underlying orders.
Evidence explicitly labels this distinction because ESI can return cached data.
History `observed_at` identifies its latest daily record. Availability time conservatively records retrieval, not assumed historical availability.

The content version deduplicates repeated cached results within the objective.
A repeated version reuses the first receipt's timestamps instead of making old data appear fresh.
Quote validity ends at the earlier of upstream expiry and the collector's five-minute limit.
Expired or invalid receipts block specialist dispatch or final publication. A late model answer cannot override this check.
Acquisition failures produce a blocker and failed review. The supervisor waits for an allowed trigger instead of repeatedly calling models.

Acquired records remain review-scoped; they do not silently replace current evidence or trigger another review through an evidence fingerprint change.
OpenSearch indexing and additional dataset, news, and log connectors remain separate work.

## Cost receipt and limitations

The quote includes a deterministic `immediate_round_trip_v1` calculation.
It applies configured adverse slippage and fees to both a purchase at ask and an immediate sale at bid.
Reconciliation recomputes the calculation before accepting the receipt.
The calculation precedes quantity rounding and excludes future price changes. It is neither a forecast nor a broker execution result.

The workflow can conclude that no opportunity is demonstrated. It cannot promise daily returns or place real or paper orders.
General claim-support checks and validation of every numerical statement in model prose remain later milestones.

## Verification

Unit tests cover approved scope selection, endpoint rejection, cached content identity, provenance integrity, expiry, and the non-model worker path.
Isolated PostgreSQL tests cover committed dispatch recovery, receipt reuse, scoped specialist inputs, source failures, and expiry before publication.

An authenticated Playwright check on October 27, 2026 completed a real public-ESI review in approximately 31 seconds.
The model selected acquisition, market analysis, and a dependent skeptical review before synthesis. All five tasks succeeded.
The collected bid was 3.77 ISK and ask was 3.94 ISK. The configured calculation returned approximately -6.40% for an immediate round trip.
The final assessment retained the cache and execution limitations and concluded that no immediate opportunity was demonstrated.
Repeated submission with the same request key returned the same completed review, with one acquisition receipt and one assessment.
This check used real market evidence and model calls. It submitted no orders and changed no portfolio.
