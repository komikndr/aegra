"""Real PostgreSQL transaction tests in disposable, isolated schemas.

Set SWARM_TEST_DATABASE_URL to a test PostgreSQL URL. No application rows are used.
"""

import asyncio
import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aegra_api.api.swarm import router
from aegra_api.core.auth_deps import require_auth
from aegra_api.core.coordinator_orm import CoordinatorTurn
from aegra_api.core.orm import Assistant, Base, Run, Thread, get_session
from aegra_api.core.paper_orm import PaperAccount, PaperCommand, PaperCycle, PaperLedger
from aegra_api.core.swarm_orm import SwarmEntry, SwarmObjective, SwarmReview, SwarmStrategyVersion, SwarmTask
from aegra_api.core.swarm_supervisor_orm import SwarmSupervisor, SwarmSupervisorCommand
from aegra_api.models.auth import User
from aegra_api.models.coordinator import CoordinatorMessage
from aegra_api.models.paper_trading import PaperQuoteInput
from aegra_api.models.swarm_coordination import Feedback, ObjectiveCreate, ReviewRequest
from aegra_api.models.swarm_supervisor import SupervisorCommand, SupervisorStart
from aegra_api.services import coordinator, objective_router, swarm_acquisition, swarm_dispatch, swarm_supervisor
from aegra_api.services.swarm_capabilities import stock_calculation
from aegra_api.services.swarm_coordination import (
    advance_review,
    create_objective,
    owned_objective,
    set_objective_status,
    start_review,
)
from aegra_api.services.swarm_strategy import verify_investigations

pytestmark = pytest.mark.asyncio
ALICE = User(identity="alice", permissions=["SuperAdmin"])
BOB = User(identity="bob", permissions=["SuperAdmin"])


async def setup_strategy(maker: async_sessionmaker[AsyncSession]) -> tuple[str, str]:
    data = objective_input().model_dump()
    data["evidence"][0]["content"] = '{"inventory_units":"700000","daily_consumption_units":"70000","horizon_days":"7"}'
    async with maker() as session, session.begin():
        objective = await create_objective(session, ALICE, ObjectiveCreate.model_validate(data))
        review = await start_review(session, objective, ALICE, ReviewRequest(request_key="strategy", workflow="strategy_search_v1"))
        return objective.objective_id, review.review_id


async def test_strategy_versions_revision_pruning_recovery_and_idempotent_publication(maker: async_sessionmaker[AsyncSession]) -> None:
    oid, rid = await setup_strategy(maker)
    seed = {"candidates": [{"branch_id": bid, "approach": approach, "falsification": "Measured stock violates assumptions"}
                           for bid, approach in [("branch_1", "Conserve inventory"), ("branch_2", "Seek alternate supply")]], "acquire_market": None}
    await asyncio.gather(*(planned_result(maker, oid, rid, "strategy_seed", {"seed": seed}) for _ in range(3)))
    async with maker() as session:
        developments = list((await session.scalars(select(SwarmTask).where(SwarmTask.role.like("%_develop")))).all())
        assert len(developments) == 2
        run_ids = [task.run_id for task in developments]
        for task in developments:
            run = await session.get(Run, task.run_id)
            assert run is not None and "reports" not in run.input
    await swarm_dispatch.recover_local_runs()
    async with maker() as session:
        assert list((await session.scalars(select(SwarmTask.run_id).where(SwarmTask.role.like("%_develop")))).all()) == run_ids
    critique = {"diagnosis": "Check the stock arithmetic before judging either strategy.", "investigations": [
        {"kind": "stock_calculation", "evidence_id": "stock", "question": "Does stock cover the horizon?"}]}
    for bid in ["branch_1", "branch_2"]:
        await planned_result(maker, oid, rid, f"p1_{bid}_develop", {"assessment": assessment()})
    for bid in ["branch_1", "branch_2"]:
        await planned_result(maker, oid, rid, f"p1_{bid}_critique", {"critique": critique})
    async with maker() as session:
        task = await session.scalar(select(SwarmTask).where(SwarmTask.review_id == rid, SwarmTask.role == "p1_branch_1_verify"))
        assert task is not None
        run = await session.get(Run, task.run_id)
        assert run is not None
        receipts = {"receipts": verify_investigations(critique, run.input["snapshot"])}
    for bid in ["branch_1", "branch_2"]:
        await planned_result(maker, oid, rid, f"p1_{bid}_verify", {"verification": receipts})
    decision = {"actions": [{"branch_id": "branch_1", "action": "revise", "reason": "Use calculated coverage", "next_question": "Account for variable consumption"},
                            {"branch_id": "branch_2", "action": "prune", "reason": "No evidence of supply deficit", "next_question": ""}], "assessment": assessment()}
    await planned_result(maker, oid, rid, "p1_decide", {"decision": decision})
    async with maker() as session:
        assert await session.scalar(select(func.count()).select_from(SwarmStrategyVersion)) == 2
        assert await session.scalar(select(func.count()).select_from(SwarmTask).where(SwarmTask.role == "p2_branch_2_develop")) == 0
        task = await session.scalar(select(SwarmTask).where(SwarmTask.role == "p2_branch_1_develop"))
        assert task is not None
        run = await session.get(Run, task.run_id)
        assert run is not None and run.input["previous"]["action"]["next_question"] == "Account for variable consumption"
    await planned_result(maker, oid, rid, "p2_branch_1_develop", {"assessment": assessment()})
    await planned_result(maker, oid, rid, "p2_branch_1_critique", {"critique": critique})
    await planned_result(maker, oid, rid, "p2_branch_1_verify", {"verification": receipts})
    final = {"actions": [{"branch_id": "branch_1", "action": "wait", "reason": "Need measured consumption", "next_question": ""}], "assessment": assessment()}
    await planned_result(maker, oid, rid, "p2_decide", {"decision": final})
    await planned_result(maker, oid, rid, "p2_decide", {"decision": final})
    async with maker() as session:
        assert await session.scalar(select(func.count()).select_from(SwarmStrategyVersion)) == 3
        entries = list((await session.scalars(select(SwarmEntry).where(SwarmEntry.kind == "assessment"))).all())
        assert len(entries) == 1 and entries[0].content["workflow"] == "strategy_search_v1"
        first = await session.get(SwarmStrategyVersion, (rid, "branch_1", 1))
        assert first is not None and first.content["action"]["action"] == "revise"


@pytest.mark.parametrize("failure", ["foreign", "forged_receipt", "pause"])
async def test_strategy_invalid_results_and_pause_cannot_publish(maker: async_sessionmaker[AsyncSession], failure: str) -> None:
    oid, rid = await setup_strategy(maker)
    await planned_result(maker, oid, rid, "strategy_seed", {"seed": {"candidates": [{"branch_id": "branch_1", "approach": "Conserve", "falsification": "Consumption rises"}], "acquire_market": None}})
    result = assessment()
    if failure == "foreign":
        result["findings"][0]["evidence_ids"] = ["foreign"]
    if failure == "pause":
        async with maker() as session, session.begin():
            objective = await owned_objective(session, oid, ALICE, lock=True)
            await set_objective_status(session, objective, "paused", ALICE)
    await planned_result(maker, oid, rid, "p1_branch_1_develop", {"assessment": result})
    if failure == "forged_receipt":
        critique = {"diagnosis": "Check", "investigations": []}
        await planned_result(maker, oid, rid, "p1_branch_1_critique", {"critique": critique})
        await planned_result(maker, oid, rid, "p1_branch_1_verify", {"verification": {"receipts": [{"status": "VALIDATED"}]}})
    async with maker() as session:
        review = await session.get(SwarmReview, rid)
        assert review is not None and review.status in {"failed", "cancelled"}
        assert await session.scalar(select(func.count()).select_from(SwarmEntry).where(SwarmEntry.kind == "assessment")) == 0


def objective_input(key: str = "objective-1") -> ObjectiveCreate:
    return ObjectiveCreate(
        request_key=key, title="Synthetic EVE supply", goal="Assess seven-day supply risk.",
        success_criteria="Evidence-supported assessment; no trading or return guarantee.",
        evidence=({"evidence_id": "stock", "source": "fixture:stock", "observed_at": "2026-01-01T00:00:00Z",
                   "available_at": "2026-01-01T00:00:01Z", "content": "Stock: 200000 units.", "simulation": True},),
    )


def assessment() -> dict[str, Any]:
    return {"summary": "Supply is uncertain.", "findings": [{"text": "Reported stock is limited.", "evidence_ids": ["stock"]}],
            "uncertainties": ["Consumption rate is unknown."], "recommendation": "Request consumption data.",
            "disposition": "needs_information"}


def planned_input() -> dict[str, Any]:
    return {"decision": "tasks", "rationale": "Calculate supplied inputs.", "assessment": None, "acquire_market": None,
            "tasks": [{"task_id": "task_1", "capability": "stock_calculation", "question": "Check coverage.",
                       "evidence_ids": ["stock"], "depends_on": []}]}


async def setup_planned(maker: async_sessionmaker[AsyncSession]) -> tuple[str, str]:
    data = objective_input().model_dump()
    data["evidence"][0]["content"] = '{"inventory_units":"700000","daily_consumption_units":"70000","horizon_days":"7"}'
    async with maker() as session, session.begin():
        objective = await create_objective(session, ALICE, ObjectiveCreate.model_validate(data))
        review = await start_review(session, objective, ALICE, ReviewRequest(request_key="planned", workflow="planned_review_v1"))
        return objective.objective_id, review.review_id


async def planned_result(
    maker: async_sessionmaker[AsyncSession], oid: str, rid: str, role: str, output: dict[str, Any], *, failed: bool = False,
) -> None:
    async with maker() as session, session.begin():
        objective = await owned_objective(session, oid, ALICE, lock=True)
        task = await session.scalar(select(SwarmTask).where(SwarmTask.review_id == rid, SwarmTask.role == role))
        assert task is not None
        run = await session.get(Run, task.run_id)
        assert run is not None
        run.status, run.output = ("error" if failed else "success"), output
        review = await session.get(SwarmReview, rid)
        assert review is not None
        await advance_review(session, objective, review)


async def test_planned_calculation_recovery_and_publication(maker: async_sessionmaker[AsyncSession]) -> None:
    oid, rid = await setup_planned(maker)
    await asyncio.gather(*(planned_result(maker, oid, rid, "planner", {"plan": planned_input(), "model": "fixture-planner"})
                           for _ in range(5)))
    async with maker() as session:
        review = await session.get(SwarmReview, rid)
        assert review is not None and review.plan == planned_input()
        receipt = stock_calculation(review.snapshot["evidence"][0])
        ids = list((await session.scalars(select(SwarmTask.run_id).where(SwarmTask.review_id == rid))).all())
    await swarm_dispatch.recover_local_runs()
    async with maker() as session:
        assert list((await session.scalars(select(SwarmTask.run_id).where(SwarmTask.review_id == rid))).all()) == ids
    await planned_result(maker, oid, rid, "task_1", {"calculation": receipt})
    result = {**assessment(), "summary": "10 days coverage, 210000 units remain after seven days.", "disposition": "wait"}
    await planned_result(maker, oid, rid, "coordinator", {"assessment": result})
    # Repeated reconciliation after delivery cannot publish the assessment twice.
    await planned_result(maker, oid, rid, "coordinator", {"assessment": result})
    async with maker() as session:
        entries = (await session.scalars(select(SwarmEntry).where(SwarmEntry.kind == "assessment"))).all()
        assert len(entries) == 1 and entries[0].content["assessment"] == result
        assert not entries[0].content["partial"]
        assert await session.scalar(select(func.count()).select_from(SwarmTask)) == 3


@pytest.mark.parametrize("invalid", ["foreign", "unknown", "cycle", "forged_calculation"])
async def test_planned_invalid_output_fails_closed(maker: async_sessionmaker[AsyncSession], invalid: str) -> None:
    oid, rid = await setup_planned(maker)
    plan = planned_input()
    if invalid == "foreign":
        plan["tasks"][0]["evidence_ids"] = ["foreign"]
    elif invalid == "unknown":
        plan["tasks"][0]["capability"] = "send_order"
    elif invalid == "cycle":
        plan["tasks"][0]["depends_on"] = ["task_1"]
    await planned_result(maker, oid, rid, "planner", {"plan": plan})
    if invalid == "forged_calculation":
        await planned_result(maker, oid, rid, "task_1", {"calculation": {"coverage_days": "999999"}})
    async with maker() as session:
        review = await session.get(SwarmReview, rid)
        assert review is not None and review.status == "failed"
        assert await session.scalar(select(func.count()).select_from(SwarmEntry).where(SwarmEntry.kind == "assessment")) == 0


async def test_planned_dependencies_retain_partial_failures(maker: async_sessionmaker[AsyncSession]) -> None:
    oid, rid = await setup_planned(maker)
    plan = planned_input()
    plan["tasks"][0]["capability"] = "evidence_analysis"
    plan["tasks"].append({"task_id": "task_2", "capability": "skeptical_review", "question": "Challenge the first finding.",
                          "evidence_ids": ["stock"], "depends_on": ["task_1"]})
    await planned_result(maker, oid, rid, "planner", {"plan": plan})
    async with maker() as session:
        assert await session.scalar(select(func.count()).select_from(SwarmTask)) == 2
    await planned_result(maker, oid, rid, "task_1", {}, failed=True)
    async with maker() as session:
        task = await session.scalar(select(SwarmTask).where(SwarmTask.role == "task_2"))
        assert task is not None
        run = await session.get(Run, task.run_id)
        assert run is not None and run.input["reports"][0]["status"] == "error"
    await planned_result(maker, oid, rid, "task_2", {"assessment": assessment()})
    await planned_result(maker, oid, rid, "coordinator", {"assessment": assessment()})
    async with maker() as session:
        entry = await session.scalar(select(SwarmEntry).where(SwarmEntry.kind == "assessment"))
        assert entry is not None and entry.content["partial"] is True


@pytest.mark.parametrize("decision", ["needs_information", "stop"])
async def test_planned_supervisor_terminal_decisions(maker: async_sessionmaker[AsyncSession], decision: str) -> None:
    oid = await setup_supervisor(maker, workflow="planned_review_v1", max_model_calls=8)
    await swarm_supervisor.supervisor_tick()
    async with maker() as session:
        row = await session.get(SwarmSupervisor, oid)
        assert row is not None and row.model_calls_reserved == 4 and row.review_id is not None
        rid = row.review_id
    await planned_result(maker, oid, rid, "planner", {"plan": {
        "decision": decision, "rationale": "Bounded decision.", "tasks": [],
        "assessment": {**assessment(), "disposition": "wait" if decision == "stop" else "needs_information"}}})
    await swarm_supervisor.supervisor_tick()
    await swarm_supervisor.supervisor_tick()
    async with maker() as session:
        row = await session.get(SwarmSupervisor, oid)
        assert row is not None and row.reviews_started == 1 and row.next_wakeup is None
        assert row.state == ("stopped" if decision == "stop" else "waiting_for_input")
        assert await session.scalar(select(func.count()).select_from(SwarmTask)) == 1


async def test_planned_budget_is_reserved_before_any_work(maker: async_sessionmaker[AsyncSession]) -> None:
    oid = await setup_supervisor(maker, workflow="planned_review_v1", max_model_calls=3)
    await swarm_supervisor.supervisor_tick()
    async with maker() as session:
        row = await session.get(SwarmSupervisor, oid)
        assert row is not None and not row.enabled and row.model_calls_reserved == 0
        assert await session.scalar(select(func.count()).select_from(SwarmReview)) == 0


async def test_planned_feedback_fences_late_plan(maker: async_sessionmaker[AsyncSession]) -> None:
    oid, rid = await setup_planned(maker)
    async with maker() as session, session.begin():
        objective = await owned_objective(session, oid, ALICE, lock=True)
        record = {**objective.current_evidence[0], "content": '{"inventory_units":"980000","daily_consumption_units":"70000","horizon_days":"7"}'}
        newer = await start_review(session, objective, ALICE, Feedback(
            request_key="corrected-plan", workflow="planned_review_v1", kind="additional_information",
            content="Corrected stock.", evidence=(record,)))
        newer_id = newer.review_id
    await planned_result(maker, oid, rid, "planner", {"plan": planned_input()})
    async with maker() as session:
        old = await session.get(SwarmReview, rid)
        new = await session.get(SwarmReview, newer_id)
        assert old is not None and old.status == "superseded" and old.plan is None
        assert new is not None and stock_calculation(new.snapshot["evidence"][0])["coverage_days"] == "14"
        assert await session.scalar(select(func.count()).select_from(SwarmTask).where(SwarmTask.review_id == rid)) == 1
        assert await session.scalar(select(func.count()).select_from(SwarmEntry).where(SwarmEntry.kind == "assessment")) == 0


async def setup_market_review(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    scope = {"region_id": 10000002, "location_id": 60003760, "type_id": 34, "fee_bps": 100, "slippage_bps": 10}
    monkeypatch.setenv("SWARM_EVE_MARKETS", json.dumps({"jita": scope}))
    quote = PaperQuoteInput(type_id=34, bid="3.8", ask="3.93", bid_quantity=10000, ask_quantity=10000, observed_at=datetime.now(UTC))
    monkeypatch.setattr(swarm_acquisition, "fetch_eve_quote", AsyncMock(return_value=(quote, "version-1", datetime.now(UTC) + timedelta(minutes=4))))
    monkeypatch.setattr(swarm_acquisition, "fetch_eve_history", AsyncMock(return_value=[
        {"date": "2026-01-01", "average": 4, "highest": 5, "lowest": 3, "volume": 100000, "order_count": 100}]))
    receipt = await swarm_acquisition.acquire_market("jita", scope)
    async with maker() as session, session.begin():
        data = objective_input().model_dump()
        data["evidence"] = []
        objective = await create_objective(session, ALICE, ObjectiveCreate.model_validate(data))
        review = await start_review(session, objective, ALICE, ReviewRequest(
            request_key="market-1", workflow="planned_review_v1", allow_market_acquisition=True))
        return objective.objective_id, review.review_id, receipt, scope


def acquisition_plan() -> dict[str, Any]:
    refs = ["eve:jita:quote", "eve:jita:history"]
    return {"decision": "tasks", "rationale": "Acquire approved evidence, then assess it.", "acquire_market": "jita", "tasks": [
        {"task_id": "task_1", "capability": "market_analysis", "question": "Assess spread, fees, liquidity and freshness.", "evidence_ids": refs, "depends_on": []},
        {"task_id": "task_2", "capability": "skeptical_review", "question": "Challenge the opportunity.", "evidence_ids": refs, "depends_on": []}]}


async def test_acquisition_dispatch_scope_receipts_and_cache_reuse(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch,
) -> None:
    oid, rid, receipt, scope = await setup_market_review(maker, monkeypatch)
    await planned_result(maker, oid, rid, "planner", {"plan": acquisition_plan()})
    async with maker() as session:
        assert await session.scalar(select(func.count()).select_from(SwarmTask)) == 2
        acquisition = await session.scalar(select(SwarmTask).where(SwarmTask.role == "market_acquisition"))
        assert acquisition is not None
        original_run_id = acquisition.run_id
    await swarm_dispatch.recover_local_runs()
    async with maker() as session:
        assert await session.scalar(select(SwarmTask.run_id).where(SwarmTask.role == "market_acquisition")) == original_run_id
    await planned_result(maker, oid, rid, "market_acquisition", {"acquisition": receipt})
    async with maker() as session:
        review = await session.get(SwarmReview, rid)
        assert review is not None and review.snapshot["evidence"] == [] and review.acquisition == receipt
        task = await session.scalar(select(SwarmTask).where(SwarmTask.role == "task_1"))
        assert task is not None
        run = await session.get(Run, task.run_id)
        assert run is not None and {e["evidence_id"] for e in run.input["snapshot"]["evidence"]} == {"eve:jita:quote", "eve:jita:history"}
    answer = {**assessment(), "findings": [{"text": "The supplied bid is below the ask before costs.", "evidence_ids": ["eve:jita:quote"]}]}
    await planned_result(maker, oid, rid, "task_1", {"assessment": answer})
    await planned_result(maker, oid, rid, "task_2", {"assessment": answer})
    await planned_result(maker, oid, rid, "coordinator", {"assessment": answer})
    async with maker() as session, session.begin():
        objective = await owned_objective(session, oid, ALICE, lock=True)
        second = await start_review(session, objective, ALICE, ReviewRequest(request_key="market-2", workflow="planned_review_v1", allow_market_acquisition=True))
        second_id = second.review_id
    await planned_result(maker, oid, second_id, "planner", {"plan": acquisition_plan()})
    cached = await swarm_acquisition.acquire_market("jita", scope)
    assert cached["retrieved_at"] != receipt["retrieved_at"] and cached["receipt_id"] == receipt["receipt_id"]
    await planned_result(maker, oid, second_id, "market_acquisition", {"acquisition": cached})
    async with maker() as session:
        second = await session.get(SwarmReview, second_id)
        assert second is not None and second.acquisition == receipt
        assert await session.scalar(select(func.count()).select_from(SwarmEntry).where(SwarmEntry.kind == "evidence_acquired")) == 1


@pytest.mark.parametrize("failure", ["source", "stale", "tampered"])
async def test_acquisition_failure_blocks_specialists(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    oid, rid, receipt, _ = await setup_market_review(maker, monkeypatch)
    await planned_result(maker, oid, rid, "planner", {"plan": acquisition_plan()})
    if failure == "stale":
        receipt["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    if failure == "tampered":
        receipt["scope"]["type_id"] = 99
    await planned_result(maker, oid, rid, "market_acquisition", {"acquisition": receipt}, failed=failure == "source")
    async with maker() as session:
        review = await session.get(SwarmReview, rid)
        assert review is not None and review.status == "failed" and review.acquisition is None
        assert await session.scalar(select(func.count()).select_from(SwarmTask)) == 2
        assert await session.scalar(select(func.count()).select_from(SwarmEntry).where(SwarmEntry.kind == "assessment")) == 0


async def test_acquisition_expiry_fences_late_synthesis(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch,
) -> None:
    oid, rid, receipt, _ = await setup_market_review(maker, monkeypatch)
    await planned_result(maker, oid, rid, "planner", {"plan": acquisition_plan()})
    await planned_result(maker, oid, rid, "market_acquisition", {"acquisition": receipt})
    answer = {**assessment(), "findings": [{"text": "Spread requires a cost check.", "evidence_ids": ["eve:jita:quote"]}]}
    await planned_result(maker, oid, rid, "task_1", {"assessment": answer})
    await planned_result(maker, oid, rid, "task_2", {"assessment": answer})
    async with maker() as session, session.begin():
        review = await session.get(SwarmReview, rid)
        assert review is not None and review.acquisition is not None
        review.acquisition = {**review.acquisition, "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()}
    await planned_result(maker, oid, rid, "coordinator", {"assessment": answer})
    async with maker() as session:
        review = await session.get(SwarmReview, rid)
        assert review is not None and review.status == "failed"
        assert await session.scalar(select(func.count()).select_from(SwarmEntry).where(SwarmEntry.kind == "assessment")) == 0


async def test_sub_coordinator_requests_opt_in_acquisition_review(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch,
) -> None:
    oid, initial_id, _, _ = await setup_market_review(maker, monkeypatch)
    async with maker() as session, session.begin():
        objective = await owned_objective(session, oid, ALICE, lock=True)
        initial = await session.get(SwarmReview, initial_id)
        assert initial is not None
        initial.status = "failed"
        turn = await coordinator.create_turn(session, objective, ALICE, CoordinatorMessage(
            request_key="research-market", message="Research the current approved EVE market. Do not place orders."))
        run = await session.get(Run, turn.run_id)
        assert run is not None
        assert "jita" in run.input["snapshot"]["approved_market_scopes"]
        run.status, run.output = "success", {"answer": {"text": "I propose a current-market review.", "action": "review",
            "feedback_kind": "additional_information", "account_id": "", "cycles": 1, "interval_seconds": 300,
            "allow_market_acquisition": True}}
        turn_id = turn.turn_id
    await coordinator.tick_turn(turn_id)
    async with maker() as session:
        turn = await session.get(CoordinatorTurn, turn_id)
        assert turn is not None and turn.result["receipt"]["status"] == "applied", turn.result if turn else None
        review = await session.get(SwarmReview, turn.result["receipt"]["review_id"])
        assert review is not None and review.request["allow_market_acquisition"] is True
        assert review.request["workflow"] == "planned_review_v1" and "jita" in review.snapshot["acquisition_scopes"]


@pytest_asyncio.fixture
async def maker(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    url = os.environ.get("SWARM_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set SWARM_TEST_DATABASE_URL for isolated PostgreSQL coordination tests.")
    schema = "swarm_test_" + uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        url, connect_args={"server_settings": {"search_path": schema + ",public"}},
        execution_options={"schema_translate_map": {None: schema}},
    )
    tables = [Assistant.__table__, Thread.__table__, Run.__table__, PaperAccount.__table__, PaperCycle.__table__, SwarmObjective.__table__,
              SwarmReview.__table__, SwarmTask.__table__, SwarmEntry.__table__, SwarmStrategyVersion.__table__, CoordinatorTurn.__table__, PaperCommand.__table__, PaperLedger.__table__,
              SwarmSupervisor.__table__, SwarmSupervisorCommand.__table__]
    try:
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(swarm_dispatch, "_get_session_maker", lambda: factory)
        monkeypatch.setattr(coordinator, "_get_session_maker", lambda: factory)
        monkeypatch.setattr(objective_router, "_get_session_maker", lambda: factory)
        monkeypatch.setattr(swarm_supervisor, "_get_session_maker", lambda: factory)
        yield factory
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


async def setup_review(maker: async_sessionmaker[AsyncSession]) -> tuple[str, str]:
    async with maker() as session, session.begin():
        objective = await create_objective(session, ALICE, objective_input())
        review = await start_review(session, objective, ALICE, ReviewRequest(request_key="review-1"))
        return objective.objective_id, review.review_id


async def setup_supervisor(maker: async_sessionmaker[AsyncSession], **policy: Any) -> str:
    async with maker() as session, session.begin():
        objective = await create_objective(session, ALICE, objective_input())
        await swarm_supervisor.configure_supervisor(session, objective, SupervisorStart(request_key="start", **policy))
        return objective.objective_id


async def complete_supervisor_review(maker: async_sessionmaker[AsyncSession], objective_id: str, disposition: str = "wait") -> None:
    async with maker() as session, session.begin():
        objective = await owned_objective(session, objective_id, ALICE, lock=True)
        row = await session.get(SwarmSupervisor, objective_id)
        assert row is not None and row.review_id is not None
        review = await session.get(SwarmReview, row.review_id)
        assert review is not None
        for _ in range(2):
            tasks = (await session.scalars(select(SwarmTask).where(SwarmTask.review_id == review.review_id))).all()
            for task in tasks:
                run = await session.get(Run, task.run_id)
                assert run is not None
                result = assessment()
                result["disposition"] = disposition
                run.status, run.output = "success", {"assessment": result, "model": "fixture-model"}
            await advance_review(session, objective, review)
        assert review.status == "completed"


async def test_supervisor_concurrent_ticks_restart_and_budget(maker: async_sessionmaker[AsyncSession]) -> None:
    objective_id = await setup_supervisor(maker, max_reviews=2, interval_seconds=60)
    await asyncio.gather(*(swarm_supervisor.supervisor_tick() for _ in range(5)))
    async with maker() as session:
        row = await session.get(SwarmSupervisor, objective_id)
        assert row is not None and row.reviews_started == 1 and row.model_calls_reserved == 3
        objective = await owned_objective(session, objective_id, ALICE)
        snapshot = await coordinator.conversation_snapshot(session, objective)
        assert snapshot["supervisor"]["reviews_started"] == 1
        assert snapshot["supervisor"]["enabled"] is True
        json.dumps(snapshot)  # Persisted Coordinator context must remain JSON serializable.
        first = row.review_id
        assert await session.scalar(select(func.count()).select_from(SwarmTask)) == 2
    await swarm_dispatch.recover_local_runs()
    await swarm_supervisor.supervisor_tick()
    async with maker() as session:
        row = await session.get(SwarmSupervisor, objective_id)
        assert row is not None and row.review_id == first and row.reviews_started == 1
    await complete_supervisor_review(maker, objective_id)
    await swarm_supervisor.supervisor_tick()
    async with maker() as session, session.begin():
        row = await session.get(SwarmSupervisor, objective_id)
        assert row is not None and row.state == "scheduled" and row.next_wakeup is not None
        row.next_wakeup = datetime.now(UTC) - timedelta(seconds=1)
    await swarm_supervisor.supervisor_tick()
    await complete_supervisor_review(maker, objective_id)
    await swarm_supervisor.supervisor_tick()
    async with maker() as session:
        row = await session.get(SwarmSupervisor, objective_id)
        assert row is not None and not row.enabled and row.reviews_started == 2
        assert row.model_calls_reserved == 6 and row.specialist_tasks_reserved == 4
        assert await session.scalar(select(func.count()).select_from(SwarmReview)) == 2


@pytest.mark.parametrize("calls,tasks,starts", [(10, 8, False), (11, 7, False), (11, 8, True)])
async def test_strategy_supervisor_reserves_entire_search_before_dispatch(
    maker: async_sessionmaker[AsyncSession], calls: int, tasks: int, starts: bool,
) -> None:
    oid = await setup_supervisor(maker, workflow="strategy_search_v1", max_model_calls=calls, max_specialist_tasks=tasks)
    await asyncio.gather(*(swarm_supervisor.supervisor_tick() for _ in range(3)))
    async with maker() as session:
        row = await session.get(SwarmSupervisor, oid)
        assert row is not None and row.reviews_started == int(starts)
        assert row.model_calls_reserved == (11 if starts else 0)
        assert row.specialist_tasks_reserved == (8 if starts else 0)
        assert await session.scalar(select(func.count()).select_from(SwarmTask)) == int(starts)


async def test_supervisor_waits_for_information_coalesces_events_and_validates_keys(maker: async_sessionmaker[AsyncSession]) -> None:
    objective_id = await setup_supervisor(maker)
    await swarm_supervisor.supervisor_tick()
    await complete_supervisor_review(maker, objective_id, "needs_information")
    await swarm_supervisor.supervisor_tick()
    await swarm_supervisor.supervisor_tick()
    async with maker() as session, session.begin():
        row = await session.get(SwarmSupervisor, objective_id)
        assert row is not None and row.state == "waiting_for_input" and row.next_wakeup is None and row.reviews_started == 1
        objective = await owned_objective(session, objective_id, ALICE, lock=True)
        for key in ("event-1", "event-1", "event-2"):
            await swarm_supervisor.command_supervisor(session, objective, SupervisorCommand(request_key=key), stop=False)
    await asyncio.gather(swarm_supervisor.supervisor_tick(), swarm_supervisor.supervisor_tick())
    async with maker() as session, session.begin():
        row = await session.get(SwarmSupervisor, objective_id)
        assert row is not None and row.reviews_started == 2
        objective = await owned_objective(session, objective_id, ALICE, lock=True)
        with pytest.raises(HTTPException) as error:
            await swarm_supervisor.command_supervisor(session, objective, SupervisorCommand(request_key="event-1", reason="Changed input"), stop=False)
        assert error.value.status_code == 409


@pytest.mark.parametrize("limit", [{"max_model_calls": 3}, {"max_specialist_tasks": 2}])
async def test_supervisor_reserves_whole_workflow_or_stops(maker: async_sessionmaker[AsyncSession], limit: dict[str, int]) -> None:
    objective_id = await setup_supervisor(maker, max_reviews=3, **limit)
    await swarm_supervisor.supervisor_tick()
    await complete_supervisor_review(maker, objective_id)
    await swarm_supervisor.supervisor_tick()
    async with maker() as session, session.begin():
        row = await session.get(SwarmSupervisor, objective_id)
        assert row is not None
        row.next_wakeup = datetime.now(UTC) - timedelta(seconds=1)
    await swarm_supervisor.supervisor_tick()
    async with maker() as session:
        row = await session.get(SwarmSupervisor, objective_id)
        assert row is not None and not row.enabled and row.reviews_started == 1


@pytest.mark.parametrize("reason", ["pause", "stop", "deadline"])
async def test_supervisor_stop_fences_late_success(maker: async_sessionmaker[AsyncSession], reason: str) -> None:
    objective_id = await setup_supervisor(maker)
    await swarm_supervisor.supervisor_tick()
    async with maker() as session, session.begin():
        objective = await owned_objective(session, objective_id, ALICE, lock=True)
        row = await session.get(SwarmSupervisor, objective_id)
        assert row is not None
        if reason == "pause":
            await set_objective_status(session, objective, "paused", ALICE)
            await set_objective_status(session, objective, "active", ALICE)
        elif reason == "stop":
            await swarm_supervisor.command_supervisor(session, objective, SupervisorCommand(request_key="stop"), stop=True)
        else:
            row.deadline = datetime.now(UTC) - timedelta(seconds=1)
        for run in (await session.scalars(select(Run))).all():
            run.status, run.output = "success", {"assessment": assessment()}
    await swarm_dispatch.coordination_tick()
    async with maker() as session:
        row = await session.get(SwarmSupervisor, objective_id)
        assert row is not None and not row.enabled and row.state == "stopped"
        assert await session.scalar(select(func.count()).select_from(SwarmEntry).where(SwarmEntry.kind == "assessment")) == 0


async def test_supervisor_changed_evidence_wakes_but_manual_review_is_not_duplicated(maker: async_sessionmaker[AsyncSession]) -> None:
    objective_id = await setup_supervisor(maker, max_reviews=3, max_model_calls=9, max_specialist_tasks=6)
    await swarm_supervisor.supervisor_tick()
    await complete_supervisor_review(maker, objective_id, "needs_information")
    await swarm_supervisor.supervisor_tick()
    async with maker() as session, session.begin():
        objective = await owned_objective(session, objective_id, ALICE, lock=True)
        objective.current_evidence = [{**objective.current_evidence[0], "content": "Corrected stock: 700000."}]
    await swarm_supervisor.supervisor_tick()
    async with maker() as session, session.begin():
        objective = await owned_objective(session, objective_id, ALICE, lock=True)
        external = await start_review(session, objective, ALICE, Feedback(request_key="human-feedback", kind="additional_information", content="New information."))
        external_id = external.review_id
    await swarm_supervisor.supervisor_tick()
    async with maker() as session:
        row = await session.get(SwarmSupervisor, objective_id)
        assert row is not None and row.review_id == external_id and not row.owns_review and row.reviews_started == 2
        assert await session.scalar(select(func.count()).select_from(SwarmReview)) == 3


async def test_supervisor_empty_input_and_owner_scoped_api(maker: async_sessionmaker[AsyncSession]) -> None:
    app = FastAPI()
    app.include_router(router)
    user = ALICE

    async def auth() -> User:
        return user

    async def database() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[require_auth] = auth
    app.dependency_overrides[get_session] = database
    async with maker() as session, session.begin():
        objective = await create_objective(session, ALICE, objective_input().model_copy(update={"evidence": ()}))
        objective_id = objective.objective_id
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        path = f"/swarm/v1/objectives/{objective_id}/supervisor"
        assert (await client.get(path)).json()["state"] == "unconfigured"
        body = {"request_key": "start"}
        assert (await client.post(path + "/start", json=body)).status_code == 200
        await swarm_supervisor.supervisor_tick()
        state = (await client.get(path)).json()
        assert state["state"] == "waiting_for_input" and state["model_calls_reserved"] == 0
        assert (await client.post(path + "/start", json=body)).json()["generation"] == state["generation"]
        assert (await client.post(path + "/start", json={**body, "max_reviews": 4})).status_code == 409
        user = BOB
        assert (await client.get(path)).status_code == 404
        assert (await client.post(path + "/stop", json={"request_key": "stop"})).status_code == 404
        user = User(identity="alice", permissions=["Executive"])
        assert (await client.get(path)).status_code == 403


async def test_head_router_uses_owner_policy_and_idempotent_subcoordinator_handoff(maker: async_sessionmaker[AsyncSession]) -> None:
    async with maker() as session, session.begin():
        own = await create_objective(session, ALICE, objective_input())
        other = await create_objective(session, BOB, objective_input())
        own_id, other_id = own.objective_id, other.objective_id
    visible = await objective_router.discover_objectives(ALICE, "Synthetic")
    assert [row["objective_id"] for row in visible["objectives"]] == [own_id]
    assert (await objective_router.discover_objectives(ALICE, "%"))["objectives"] == []
    for identifier in [other_id, "unknown"]:
        with pytest.raises(HTTPException) as denied:
            await objective_router.inspect_objective(ALICE, identifier)
        assert denied.value.status_code == 404
    with pytest.raises(HTTPException) as denied:
        await objective_router.discover_objectives(User(identity="alice", permissions=["Executive"]))
    assert denied.value.status_code == 403
    first = await objective_router.delegate_message(ALICE, own_id, "Explain the supply risk.", "call-1")
    retry = await objective_router.delegate_message(ALICE, own_id, "Explain the supply risk.", "call-1")
    assert first["turn_id"] == retry["turn_id"]
    reply = await objective_router.inspect_reply(ALICE, own_id, first["turn_id"])
    assert reply["status"] == "pending"
    assert reply["message"] == "Explain the supply risk."
    with pytest.raises(HTTPException) as denied:
        await objective_router.inspect_reply(BOB, other_id, first["turn_id"])
    assert denied.value.status_code == 404
    async with maker() as session:
        assert await session.scalar(select(func.count()).select_from(CoordinatorTurn)) == 1


async def finish_specialists(maker: async_sessionmaker[AsyncSession], review_id: str, *, fail_one: bool = False) -> None:
    async with maker() as session, session.begin():
        tasks = (await session.scalars(select(SwarmTask).where(SwarmTask.review_id == review_id))).all()
        for index, task in enumerate(tasks):
            run = await session.get(Run, task.run_id)
            assert run is not None
            run.status = "error" if fail_one and index == 0 else "success"
            run.output = {"assessment": assessment()}


async def test_duplicate_creation_is_atomic_and_conflicting_input_is_rejected(maker: async_sessionmaker[AsyncSession]) -> None:
    async def create() -> str:
        async with maker() as session, session.begin():
            return (await create_objective(session, ALICE, objective_input())).objective_id

    ids = await asyncio.gather(create(), create(), create())
    assert len(set(ids)) == 1
    async with maker() as session:
        assert await session.scalar(select(func.count()).select_from(SwarmEntry)) == 1
        with pytest.raises(HTTPException, match="different objective"):
            await create_objective(session, ALICE, objective_input().model_copy(update={"goal": "Different goal"}))


async def test_review_commits_two_jobs_and_recovers_dispatch_gap(maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch) -> None:
    _, review_id = await setup_review(maker)
    submit = AsyncMock(side_effect=OSError("queue offline"))
    monkeypatch.setattr(swarm_dispatch.executor, "submit", submit)
    async with maker() as session:
        tasks = (await session.scalars(select(SwarmTask).where(SwarmTask.review_id == review_id))).all()
        assert len(tasks) == 2
        task_id = tasks[0].task_id
        original_run = tasks[0].run_id
    with pytest.raises(OSError, match="queue offline"):
        await swarm_dispatch.dispatch_task(task_id)
    async with maker() as session, session.begin():
        task = await session.get(SwarmTask, task_id)
        assert task is not None
        task.last_submitted_at = datetime.now(UTC) - timedelta(seconds=31)
    submit.side_effect = None
    await swarm_dispatch.dispatch_task(task_id)
    assert submit.await_count == 2
    assert submit.await_args.args[0].identity.run_id == original_run


async def test_partial_specialist_failure_produces_versioned_assessment_once(maker: async_sessionmaker[AsyncSession]) -> None:
    objective_id, review_id = await setup_review(maker)
    await finish_specialists(maker, review_id, fail_one=True)
    async with maker() as session, session.begin():
        objective = await owned_objective(session, objective_id, ALICE, lock=True)
        review = await session.get(SwarmReview, review_id)
        assert review is not None
        await advance_review(session, objective, review)
    async with maker() as session, session.begin():
        synthesis = await session.scalar(select(SwarmTask).where(SwarmTask.role == "coordinator"))
        assert synthesis is not None
        run = await session.get(Run, synthesis.run_id)
        assert run is not None
        run.status, run.output = "success", {"assessment": assessment()}
    for _ in range(2):
        async with maker() as session, session.begin():
            objective = await owned_objective(session, objective_id, ALICE, lock=True)
            review = await session.get(SwarmReview, review_id)
            assert review is not None
            await advance_review(session, objective, review)
    async with maker() as session:
        entries = (await session.scalars(select(SwarmEntry).where(SwarmEntry.kind == "assessment"))).all()
        assert len(entries) == 1
        assert entries[0].content["partial"] is True
        assert entries[0].content["revision"] == 1


async def test_feedback_supersedes_old_work_and_deduplicates_reassessment(maker: async_sessionmaker[AsyncSession]) -> None:
    objective_id, old_review_id = await setup_review(maker)
    feedback = Feedback(request_key="correction", kind="disagreement", content="Stock report was stale.", evidence=(
        {**objective_input().evidence[0].model_dump(), "content": "Corrected stock: 700000 units."},
    ))
    async with maker() as session, session.begin():
        objective = await owned_objective(session, objective_id, ALICE, lock=True)
        fresh = await start_review(session, objective, ALICE, feedback)
        fresh_id = fresh.review_id
        again = await start_review(session, objective, ALICE, feedback)
        assert again.review_id == fresh_id
    await finish_specialists(maker, old_review_id)
    async with maker() as session, session.begin():
        objective = await owned_objective(session, objective_id, ALICE, lock=True)
        old = await session.get(SwarmReview, old_review_id)
        fresh = await session.get(SwarmReview, fresh_id)
        assert old is not None and fresh is not None
        await advance_review(session, objective, old)
        assert old.status == "superseded"
        assert fresh.revision == 2
        assert fresh.snapshot["evidence"][0]["content"] == "Corrected stock: 700000 units."
        assert objective.definition["evidence"][0]["content"] == "Stock: 200000 units."
        assert await session.scalar(select(func.count()).select_from(SwarmEntry).where(SwarmEntry.kind == "feedback")) == 1
        assert await session.scalar(select(func.count()).select_from(SwarmEntry).where(SwarmEntry.kind == "assessment")) == 0


async def test_pause_prevents_dispatch_and_late_publication(maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch) -> None:
    objective_id, review_id = await setup_review(maker)
    submit = AsyncMock()
    monkeypatch.setattr(swarm_dispatch.executor, "submit", submit)
    async with maker() as session, session.begin():
        objective = await owned_objective(session, objective_id, ALICE, lock=True)
        await set_objective_status(session, objective, "paused", ALICE)
        task_ids = list((await session.scalars(select(SwarmTask.task_id))).all())
    for task_id in task_ids:
        await swarm_dispatch.dispatch_task(task_id)
    submit.assert_not_awaited()
    async with maker() as session:
        assert (await session.get(SwarmReview, review_id)).status == "cancelled"
        assert set((await session.scalars(select(SwarmTask.status))).all()) == {"cancelled"}


async def test_expired_review_and_invalid_evidence_are_not_published(maker: async_sessionmaker[AsyncSession]) -> None:
    objective_id, review_id = await setup_review(maker)
    await finish_specialists(maker, review_id)
    async with maker() as session, session.begin():
        objective = await owned_objective(session, objective_id, ALICE, lock=True)
        review = await session.get(SwarmReview, review_id)
        assert review is not None
        review.deadline = datetime.now(UTC) - timedelta(seconds=1)
        await advance_review(session, objective, review)
        assert review.status == "expired"
        assert await session.scalar(select(func.count()).select_from(SwarmEntry).where(SwarmEntry.kind == "assessment")) == 0


async def test_http_scope_validation_and_resumable_board(maker: async_sessionmaker[AsyncSession]) -> None:
    app = FastAPI()
    app.include_router(router)
    current_user = ALICE

    async def session_override() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[require_auth] = lambda: current_user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/swarm/v1/objectives", json=objective_input().model_dump(mode="json"))
        assert response.status_code == 200
        objective_id = response.json()["objective_id"]
        prefix = f"/swarm/v1/objectives/{objective_id}"
        started = await client.post(prefix + "/reviews", json={"request_key": "review-1"})
        assert started.status_code == 200
        detail = await client.get(prefix + "/reviews/" + started.json()["review_id"])
        assert detail.json()["input_snapshot"]["evidence"][0]["content"] == "Stock: 200000 units."
        entries = (await client.get(prefix + "/board", params={"limit": 2})).json()
        following = (await client.get(prefix + "/board", params={"after": entries[-1]["sequence"]})).json()
        assert following[0]["sequence"] == entries[-1]["sequence"] + 1
        assert (await client.post(prefix + "/reviews", json={"request_key": "review-2"})).status_code == 409
        assert (await client.post(prefix + "/feedback", json={"request_key": "f", "kind": "outcome", "content": "x", "author": "forged"})).status_code == 422
        current_user = BOB
        for suffix in ("", "/board", "/reviews", "/reviews/unknown", "/reviews/unknown/tasks"):
            assert (await client.get(prefix + suffix)).status_code == 404
        current_user = User(identity="staff", permissions=["Staff"])
        assert (await client.get("/swarm/v1/objectives")).status_code == 403


@pytest.mark.parametrize("bad_reference", [False, True])
async def test_failed_or_invalid_workers_cannot_create_a_final_assessment(
    maker: async_sessionmaker[AsyncSession], bad_reference: bool,
) -> None:
    objective_id, review_id = await setup_review(maker)
    async with maker() as session, session.begin():
        runs = (await session.scalars(select(Run))).all()
        for run in runs:
            value = assessment()
            value["findings"][0]["evidence_ids"] = ["not-in-snapshot"]
            run.status = "success" if bad_reference else "error"
            run.output = {"assessment": value}
        objective = await owned_objective(session, objective_id, ALICE, lock=True)
        review = await session.get(SwarmReview, review_id)
        assert review is not None
        await session.flush()
        await advance_review(session, objective, review)
        assert review.status == "failed"
        assert await session.scalar(select(func.count()).select_from(SwarmEntry).where(SwarmEntry.kind == "assessment")) == 0


async def test_concurrent_duplicate_review_has_one_revision_and_two_tasks(maker: async_sessionmaker[AsyncSession]) -> None:
    async with maker() as session, session.begin():
        objective_id = (await create_objective(session, ALICE, objective_input())).objective_id

    async def begin() -> str:
        async with maker() as session, session.begin():
            objective = await owned_objective(session, objective_id, ALICE, lock=True)
            return (await start_review(session, objective, ALICE, ReviewRequest(request_key="same-review"))).review_id

    ids = await asyncio.gather(begin(), begin(), begin())
    assert len(set(ids)) == 1
    async with maker() as session:
        assert await session.scalar(select(func.count()).select_from(SwarmReview)) == 1
        assert await session.scalar(select(func.count()).select_from(SwarmTask)) == 2


async def test_future_evidence_rolls_back_review_creation(maker: async_sessionmaker[AsyncSession]) -> None:
    future = datetime.now(UTC) + timedelta(days=1)
    request = objective_input()
    request = request.model_copy(update={"evidence": (request.evidence[0].model_copy(update={"available_at": future}),)})
    async with maker() as session, session.begin():
        objective_id = (await create_objective(session, ALICE, request)).objective_id
    with pytest.raises(HTTPException, match="not yet available"):
        async with maker() as session, session.begin():
            objective = await owned_objective(session, objective_id, ALICE, lock=True)
            await start_review(session, objective, ALICE, ReviewRequest(request_key="future-review"))
    async with maker() as session:
        assert await session.scalar(select(func.count()).select_from(SwarmReview)) == 0


async def test_local_restart_recovers_running_jobs_without_creating_new_tasks(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch,
) -> None:
    await setup_review(maker)
    async with maker() as session, session.begin():
        runs = (await session.scalars(select(Run))).all()
        original_ids = {run.run_id for run in runs}
        for run in runs:
            run.status = "running"
    monkeypatch.setattr(swarm_dispatch.settings.redis, "REDIS_BROKER_ENABLED", False)
    await swarm_dispatch.recover_local_runs()
    async with maker() as session:
        runs = (await session.scalars(select(Run))).all()
        assert {run.run_id for run in runs} == original_ids
        assert {run.status for run in runs} == {"pending"}
        assert await session.scalar(select(func.count()).select_from(SwarmTask)) == 2


def coordinator_answer(action: str = "none", account_id: str = "") -> dict[str, Any]:
    return {"text": "Here is what the recorded evidence supports.", "action": action,
            "feedback_kind": "additional_information", "account_id": account_id, "cycles": 1, "interval_seconds": 300}


async def new_turn(maker: async_sessionmaker[AsyncSession]) -> tuple[str, str, str]:
    async with maker() as session, session.begin():
        objective = await create_objective(session, ALICE, objective_input())
        turn = await coordinator.create_turn(session, objective, ALICE, CoordinatorMessage(request_key="message-1", message="Explain the evidence."))
        return objective.objective_id, turn.turn_id, turn.run_id


async def test_coordinator_message_deduplication_and_single_active_turn(maker: async_sessionmaker[AsyncSession]) -> None:
    objective_id, turn_id, _ = await new_turn(maker)
    async with maker() as session, session.begin():
        objective = await owned_objective(session, objective_id, ALICE, lock=True)
        same = await coordinator.create_turn(session, objective, ALICE, CoordinatorMessage(request_key="message-1", message="Explain the evidence."))
        assert same.turn_id == turn_id
        with pytest.raises(HTTPException, match="different message"):
            await coordinator.create_turn(session, objective, ALICE, CoordinatorMessage(request_key="message-1", message="Changed"))
        with pytest.raises(HTTPException, match="previous message"):
            await coordinator.create_turn(session, objective, ALICE, CoordinatorMessage(request_key="message-2", message="Next"))
        with pytest.raises(HTTPException, match="not found"):
            await owned_objective(session, objective_id, BOB)
        assert await session.scalar(select(func.count()).select_from(CoordinatorTurn)) == 1


async def test_coordinator_dispatch_gap_and_action_receipt_are_idempotent(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, turn_id, run_id = await new_turn(maker)
    submit = AsyncMock(side_effect=OSError("delivery unavailable"))
    monkeypatch.setattr(coordinator.executor, "submit", submit)
    with pytest.raises(OSError):
        await coordinator.tick_turn(turn_id)
    async with maker() as session, session.begin():
        turn = await session.get(CoordinatorTurn, turn_id)
        assert turn is not None
        turn.submitted_at = datetime.now(UTC) - timedelta(seconds=40)
    submit.side_effect = None
    await coordinator.tick_turn(turn_id)
    assert {call.args[0].identity.run_id for call in submit.call_args_list} == {run_id}
    async with maker() as session, session.begin():
        run = await session.get(Run, run_id)
        assert run is not None
        run.status, run.output = "success", {"answer": coordinator_answer("review"), "model": "fixture-model"}
    await coordinator.tick_turn(turn_id)
    await coordinator.tick_turn(turn_id)
    async with maker() as session:
        turn = await session.get(CoordinatorTurn, turn_id)
        assert turn is not None and turn.result is not None
        assert turn.result["receipt"]["status"] == "applied"
        assert await session.scalar(select(func.count()).select_from(SwarmReview)) == 1
        assert await session.scalar(select(func.count()).select_from(SwarmEntry).where(SwarmEntry.kind == "coordinator_reply")) == 1


@pytest.mark.parametrize("foreign_scope", [False, True])
async def test_coordinator_paper_controls_are_scoped_to_linked_owned_accounts(
    maker: async_sessionmaker[AsyncSession], foreign_scope: bool,
) -> None:
    objective_id, turn_id, run_id = await new_turn(maker)
    async with maker() as session, session.begin():
        session.add(PaperAccount(account_id="paper-1", user_id="bob" if foreign_scope else "alice",
                                 request_key="paper-1", policy={"objective_id": objective_id}, cash=10000, enabled=True, remaining=3))
        run = await session.get(Run, run_id)
        assert run is not None
        run.status, run.output = "success", {"answer": coordinator_answer("paper_stop", "paper-1")}
    await coordinator.tick_turn(turn_id)
    async with maker() as session:
        account = await session.get(PaperAccount, "paper-1")
        turn = await session.get(CoordinatorTurn, turn_id)
        assert account is not None and turn is not None and turn.result is not None
        assert account.enabled is foreign_scope
        assert turn.result["receipt"]["status"] == ("refused" if foreign_scope else "applied")


async def test_coordinator_deadline_prevents_late_action_and_restart_preserves_run(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, turn_id, run_id = await new_turn(maker)
    monkeypatch.setattr(swarm_dispatch.settings.redis, "REDIS_BROKER_ENABLED", False)
    async with maker() as session, session.begin():
        run = await session.get(Run, run_id)
        assert run is not None
        run.status = "running"
    await swarm_dispatch.recover_local_runs()
    async with maker() as session, session.begin():
        run = await session.get(Run, run_id)
        turn = await session.get(CoordinatorTurn, turn_id)
        assert run is not None and turn is not None and run.status == "pending"
        run.status, run.output = "success", {"answer": coordinator_answer("review")}
        turn.deadline = datetime.now(UTC) - timedelta(seconds=1)
    await coordinator.tick_turn(turn_id)
    async with maker() as session:
        turn = await session.get(CoordinatorTurn, turn_id)
        assert turn is not None and turn.status == "failed"
        assert await session.scalar(select(func.count()).select_from(SwarmReview)) == 0


async def test_coordinator_refused_action_preserves_reply_and_prose_feedback_uses_exact_user_text(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    objective_id, turn_id, run_id = await new_turn(maker)
    async with maker() as session, session.begin():
        objective = await owned_objective(session, objective_id, ALICE, lock=True)
        objective.status = "paused"
        run = await session.get(Run, run_id)
        assert run is not None
        run.status, run.output = "success", {"answer": coordinator_answer("review")}
    await coordinator.tick_turn(turn_id)
    async with maker() as session, session.begin():
        turn = await session.get(CoordinatorTurn, turn_id)
        assert turn is not None and turn.result is not None
        assert turn.result["receipt"]["status"] == "refused"
        assert turn.result["text"]
        objective = await owned_objective(session, objective_id, ALICE, lock=True)
        objective.status = "active"
        next_turn = await coordinator.create_turn(session, objective, ALICE, CoordinatorMessage(request_key="feedback", message="The prior report is outdated."))
        await session.flush()
        next_run = await session.get(Run, next_turn.run_id)
        assert next_run is not None
        next_run.status, next_run.output = "success", {"answer": coordinator_answer("feedback")}
        next_id = next_turn.turn_id
    await coordinator.tick_turn(next_id)
    async with maker() as session:
        feedback = await session.scalar(select(SwarmEntry).where(SwarmEntry.kind == "feedback"))
        assert feedback is not None and feedback.content["content"] == "The prior report is outdated."
