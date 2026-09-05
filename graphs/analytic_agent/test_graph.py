"""Regression tests for analytic database discovery policy."""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from react_agent.state import State

from analytic_agent.graph import requires_initial_table_listing


def test_schema_request_requires_table_listing_before_model_can_answer() -> None:
    state = State(messages=[HumanMessage(content="What tables are available in the analytic database?")])

    assert requires_initial_table_listing(state)


def test_describe_request_requires_table_listing_before_model_can_answer() -> None:
    state = State(messages=[HumanMessage(content="Describe the columns in the sales table")])

    assert requires_initial_table_listing(state)


def test_existing_tool_result_allows_the_normal_tool_loop() -> None:
    state = State(
        messages=[
            HumanMessage(content="List the available tables"),
            AIMessage(content="", tool_calls=[{"name": "analytic_list_tables", "args": {}, "id": "call-1"}]),
            ToolMessage(content='{"tables":["sales"]}', tool_call_id="call-1"),
        ]
    )

    assert not requires_initial_table_listing(state)


def test_normal_question_does_not_force_table_discovery() -> None:
    state = State(messages=[HumanMessage(content="Explain gross margin")])

    assert not requires_initial_table_listing(state)
