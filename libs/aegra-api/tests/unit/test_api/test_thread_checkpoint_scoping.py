"""Tests for server-owned LangGraph configuration keys."""

from aegra_api.utils.run_utils import strip_pinned_config_keys


def test_strip_pinned_config_keys_removes_identity_and_routing_keys() -> None:
    cleaned = strip_pinned_config_keys(
        {
            "thread_id": "victim-thread",
            "run_id": "victim-run",
            "user_id": "victim-user",
            "user_display_name": "Victim",
            "langgraph_auth_user": {"identity": "victim-user"},
            "checkpoint_id": "checkpoint-1",
        }
    )

    assert cleaned == {"checkpoint_id": "checkpoint-1"}
