"""Request and queued-job contexts cannot redirect server KMS credentials."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from aegra_api.models import RunCreate
from aegra_api.models.auth import User
from aegra_api.models.run_job import RunExecution, RunIdentity, RunJob
from aegra_api.services import run_executor, run_preparation


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_preparation_strips_connection_settings_before_persisting(legacy: bool) -> None:
    hostile = {"kms_opensearch_host": "https://untrusted.example", "kms_opensearch_verify_certs": False, "reasoning_effort": "high"}
    request = RunCreate(assistant_id="qa_chat", input={"messages": [{"role": "user", "content": "find this"}]}, config={"configurable": hostile}, context={} if legacy else hostile)
    assistant = SimpleNamespace(assistant_id="qa_chat", graph_id="qa_chat", config={}, context={"kms_opensearch_index": "other"})
    session = MagicMock(spec=AsyncSession)
    session.scalar = AsyncMock(side_effect=[None, assistant])
    session.commit = AsyncMock()
    service = MagicMock()
    service.list_graphs.return_value = {"qa_chat": "graph"}
    with (
        patch.object(run_preparation, "get_langgraph_service", return_value=service),
        patch.object(run_preparation, "_preflight_model_endpoint"),
        patch.object(run_preparation, "ensure_graph_access"),
        patch.object(run_preparation, "update_thread_metadata", new_callable=AsyncMock),
        patch.object(run_preparation, "set_thread_status", new_callable=AsyncMock),
        patch.object(run_preparation.executor, "submit", new_callable=AsyncMock),
    ):
        _, _, job = await run_preparation._prepare_run(session, "thread", request, User(identity="alice"), initial_status="pending")
    assert job.execution.context == {"reasoning_effort": "high"}
    assert job.execution.config["configurable"] == {"reasoning_effort": "high"}
    persisted = session.add.call_args.args[0]
    assert persisted.context == {"reasoning_effort": "high"}
    assert "untrusted.example" not in str(persisted.execution_params)


@pytest.mark.asyncio
async def test_existing_queued_job_is_sanitized_again_at_execution() -> None:
    hostile = {"kms_opensearch_host": "http://untrusted.example", "kms_opensearch_index": "private", "reasoning_effort": "high"}
    job = RunJob(identity=RunIdentity(run_id="run", thread_id="thread", graph_id="qa_chat"), user=User(identity="alice"), execution=RunExecution(context=hostile, config={"configurable": hostile}))
    with patch.object(run_executor, "_get_session_maker", side_effect=RuntimeError("offline")):
        context = await run_executor._build_execution_context(job)
    assert context == {"reasoning_effort": "high"}
    assert "kms_opensearch_host" not in run_executor._build_run_config(job)["configurable"]
