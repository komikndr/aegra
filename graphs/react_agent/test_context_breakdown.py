"""Focused tests for the context-breakdown report emitted by token trimming.

The report backs the composer's context usage breakdown. Semantics under test:

- ``pruned_messages`` / ``pruned_tokens`` count only what the token budget
  dropped from this model call's input; they are separate from used tokens.
- Absent tool schema information stays ``None`` (unknown), never zero.
- ``estimated`` is true unless a real tokenizer answered every count.
"""

from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from . import utils


class _HeuristicModel:
    """No get_num_tokens_from_messages, so counts fall back to estimates."""


class _TokenizerModel:
    def get_num_tokens_from_messages(self, messages: list[Any]) -> int:
        return 42


def _messages(count: int, size: int) -> list[HumanMessage]:
    return [HumanMessage(content=str(index) + "x" * size) for index in range(count)]


def _build_payload_and_report(
    model: Any,
    messages: list[Any],
    **overrides: Any,
) -> tuple[list[Any], dict[str, Any]]:
    report: dict[str, Any] = {}
    kwargs: dict[str, Any] = {
        "num_limit_token": 65536,
        "num_limit_response_reserve": 4096,
        "num_limit_safety_buffer": 1024,
        "num_limit_min_recent_messages": 2,
    }
    kwargs.update(overrides)
    payload = utils.build_token_limited_messages(
        model, "SYSTEM PROMPT TEXT", messages, context_report=report, **kwargs
    )
    return payload, report


def test_report_is_opt_in_and_never_changes_the_payload() -> None:
    messages = _messages(3, 40)
    without_report = utils.build_token_limited_messages(
        _HeuristicModel(),
        "SYSTEM PROMPT TEXT",
        messages,
        num_limit_token=65536,
        num_limit_response_reserve=4096,
        num_limit_safety_buffer=1024,
        num_limit_min_recent_messages=2,
    )
    report: dict[str, Any] = {}
    with_report = utils.build_token_limited_messages(
        _HeuristicModel(),
        "SYSTEM PROMPT TEXT",
        messages,
        num_limit_token=65536,
        num_limit_response_reserve=4096,
        num_limit_safety_buffer=1024,
        num_limit_min_recent_messages=2,
        context_report=report,
    )
    assert [str(message.content) for message in without_report] == [str(message.content) for message in with_report]


def test_report_within_budget_reports_zero_pruned_and_real_segments() -> None:
    messages = _messages(2, 40)
    payload, report = _build_payload_and_report(_HeuristicModel(), messages)

    assert report["schema_version"] == 1
    assert report["estimated"] is True
    assert report["pruned_messages"] == 0
    assert report["pruned_tokens"] == 0
    assert report["system_prompt_tokens"] > 0
    assert report["messages_tokens"] > 0
    # No introspectable tool schema on this stub: unknown, not zero.
    assert report["tool_definition_tokens"] is None
    assert len(payload) == 1 + len(messages)


def test_report_counts_pruned_messages_and_tokens() -> None:
    messages = _messages(10, 300)
    payload, report = _build_payload_and_report(
        _HeuristicModel(),
        messages,
        num_limit_token=1024,
        num_limit_response_reserve=128,
        num_limit_safety_buffer=64,
        num_limit_min_recent_messages=2,
    )

    retained = len(payload) - 1  # minus the system message
    assert report["pruned_messages"] == len(messages) - retained
    assert report["pruned_messages"] > 0
    assert 0 < retained < len(messages)
    assert report["pruned_tokens"] > 0
    # Pruned tokens are reported separately from the tokens that were sent.
    assert report["messages_tokens"] > 0

    # Re-running the same trim twice reports the same numbers.
    _, second = _build_payload_and_report(
        _HeuristicModel(),
        messages,
        num_limit_token=1024,
        num_limit_response_reserve=128,
        num_limit_safety_buffer=64,
        num_limit_min_recent_messages=2,
    )
    assert second == report


def test_empty_history_reports_zero_pruned() -> None:
    _, report = _build_payload_and_report(_HeuristicModel(), [])
    assert report["pruned_messages"] == 0
    assert report["pruned_tokens"] == 0
    assert report["system_prompt_tokens"] > 0


def test_tool_definition_tokens_estimated_from_bound_kwargs() -> None:
    class _BoundModel(_HeuristicModel):
        kwargs = {"tools": [{"name": "search", "description": "Search the web for facts"}]}

    _, report = _build_payload_and_report(_BoundModel(), _messages(1, 10))
    assert report["tool_definition_tokens"] is not None
    assert report["tool_definition_tokens"] > 0


def test_trusted_tokenizer_clears_the_estimated_flag() -> None:
    _, report = _build_payload_and_report(_TokenizerModel(), _messages(2, 10))
    assert report["estimated"] is False
    assert report["system_prompt_tokens"] == 42


def test_attach_context_breakdown_only_for_real_reports() -> None:
    response = AIMessage(content="ok")
    report = {"schema_version": 1, "pruned_messages": 3}
    utils.attach_context_breakdown(response, report)
    assert response.response_metadata["context_breakdown"] == report
    # The message gets a copy, so later mutation of the caller's dict is not shared.
    report["pruned_messages"] = 4
    assert response.response_metadata["context_breakdown"]["pruned_messages"] == 3

    untouched = AIMessage(content="fallback")
    utils.attach_context_breakdown(untouched, {})
    assert "context_breakdown" not in untouched.response_metadata
