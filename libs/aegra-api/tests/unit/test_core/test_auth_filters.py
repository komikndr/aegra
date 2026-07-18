"""Tests for authorization-handler metadata predicates."""

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from aegra_api.core.auth_filters import build_metadata_filter
from aegra_api.core.orm import Assistant as AssistantORM

_COLUMN = AssistantORM.metadata_dict


def _compile(filters: dict | None) -> tuple[str, dict]:
    clause = build_metadata_filter(_COLUMN, filters)
    if clause is None:
        return "", {}
    compiled = clause.compile(dialect=postgresql.dialect())
    return str(compiled), dict(compiled.params)


def test_empty_filter_returns_no_predicate() -> None:
    assert build_metadata_filter(_COLUMN, None) is None
    assert build_metadata_filter(_COLUMN, {}) is None


def test_equality_contains_and_metadata_envelope_compile() -> None:
    sql, params = _compile({"metadata": {"owner": "u1"}, "tags": {"$contains": ["x", "y"]}})
    assert " AND " in sql
    assert "@>" in sql
    assert {"owner": "u1"} in params.values()
    assert ["x", "y"] in params.values()


def test_bounded_boolean_filters_compile() -> None:
    sql, _ = _compile({"$or": [{"owner": {"$eq": "u1"}}, {"owner": "u2"}]})
    assert " OR " in sql


@pytest.mark.parametrize(
    "invalid",
    [
        {"$or": [{"owner": "u1"}]},
        {"$and": [{}, {"owner": "u1"}]},
        {"owner": {"$unknown": "u1"}},
        {"owner": {"$eq": "u1", "$contains": "u1"}},
        {"$or": [{"$or": [{"$or": [{"a": 1}, {"b": 2}]}, {"c": 3}]}, {"d": 4}]},
    ],
)
def test_invalid_filters_fail_closed(invalid: dict) -> None:
    with pytest.raises(HTTPException) as exc_info:
        build_metadata_filter(_COLUMN, invalid)
    assert exc_info.value.status_code == 500
