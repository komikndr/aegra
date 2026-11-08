"""Controlled external-boundary fixtures, not a fake swarm execution runtime."""

from datetime import UTC, datetime

from aegra_api.models.swarm import OperatorReply, RoutingDecision, RoutingDestination, RoutingScenario, Signal


def routing_scenarios() -> tuple[RoutingScenario, ...]:
    observed_at = datetime(2026, 1, 1, tzinfo=UTC)
    return (
        RoutingScenario(
            scenario_id="routine-observation",
            description="Synthetic routine observation; record it without asserting an operational action.",
            signal=Signal(
                signal_id="sim-routine-001",
                source="fixture:systemone",
                observed_at=observed_at,
                summary="No material change in the fixture observation.",
            ),
            routing=RoutingDecision(
                signal_id="sim-routine-001",
                destination=RoutingDestination.RECORD_ONLY,
                reason="Synthetic record-only routing decision.",
            ),
        ),
        RoutingScenario(
            scenario_id="readiness-disagreement",
            description="Synthetic persistent readiness decline followed by an operator correction.",
            signal=Signal(
                signal_id="sim-readiness-001",
                source="fixture:systemone",
                observed_at=observed_at,
                summary="Readiness decreased across three fixture periods.",
                evidence_refs=("fixture:readiness-history-v1",),
            ),
            routing=RoutingDecision(
                signal_id="sim-readiness-001",
                destination=RoutingDestination.STRATEGIC_REVIEW,
                reason="Synthetic multi-period pattern for strategic assessment.",
            ),
            operator_reply=OperatorReply(
                kind="disagreement",
                text="The equipment status is outdated. A newer observation reports restored availability.",
            ),
        ),
        RoutingScenario(
            scenario_id="duty-authority-review",
            description="Synthetic urgent observation. Human-review policy is unresolved; the fixture supplies a duty-authority response only.",
            signal=Signal(
                signal_id="sim-duty-001",
                source="fixture:systemone",
                observed_at=observed_at,
                summary="Conflicting fixture observations require further assessment before action.",
            ),
            routing=RoutingDecision(
                signal_id="sim-duty-001",
                destination=RoutingDestination.LOCAL_ASSESSMENT,
                reason="Synthetic routing to local assessment; this does not authorize action.",
            ),
            operator_reply=OperatorReply(
                kind="additional_information", text="The observations conflict with the current local situation."
            ),
            duty_authority_reply="Request additional evidence before issuing a revised instruction.",
        ),
        RoutingScenario(
            scenario_id="eve-regional-supply",
            description="Synthetic EVE Online regional supply assessment. Dataset: examples/swarm/eve-regional-supply.json. No live market or news collection has occurred.",
            signal=Signal(
                signal_id="sim-eve-supply-001",
                source="fixture:eve-market",
                observed_at=observed_at,
                summary="Synthetic Tritanium prices increased over seven daily observations in The Forge. A synthetic news report suggests more manufacturing activity. Assess the next seven days of stocking and logistics using the data and existing findings.",
                evidence_refs=("fixture:eve-market-history-v1", "fixture:eve-news-v1"),
            ),
            routing=RoutingDecision(
                signal_id="sim-eve-supply-001",
                destination=RoutingDestination.STRATEGIC_REVIEW,
                reason="Synthetic multi-day market and news evidence requests strategic assessment, not automatic execution.",
            ),
            operator_reply=OperatorReply(
                kind="additional_information",
                text="Synthetic operator report: warehouse stock is newer than the prior snapshot. Reassess after considering the stock correction in the fixture dataset.",
            ),
        ),
    )
