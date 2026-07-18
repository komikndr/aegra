"""Tests for checkpoint-only run response serialization."""

from datetime import UTC, datetime

from aegra_api.models.runs import Run


def test_run_accepts_null_input_for_checkpoint_resume() -> None:
    now = datetime.now(UTC)

    run = Run(
        run_id="run-1",
        thread_id="thread-1",
        assistant_id="assistant-1",
        status="pending",
        input=None,
        user_id="user-1",
        created_at=now,
        updated_at=now,
    )

    assert run.input is None
