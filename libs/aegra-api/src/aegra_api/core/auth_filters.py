"""Compile authorization-handler metadata filters into JSONB predicates."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlalchemy import and_, or_
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

_MAX_FILTER_DEPTH = 2


def build_metadata_filter(
    column: InstrumentedAttribute[Any], filters: dict[str, Any] | None
) -> ColumnElement[bool] | None:
    """Build a bounded metadata predicate from an auth-handler filter."""
    if not filters:
        return None
    clauses = _compile(column, _unwrap_metadata_envelope(filters), depth=0)
    if not clauses:
        return None
    return and_(*clauses) if len(clauses) > 1 else clauses[0]


def _unwrap_metadata_envelope(filters: dict[str, Any]) -> dict[str, Any]:
    envelope = filters.get("metadata")
    if not isinstance(envelope, dict):
        return filters
    return {**envelope, **{key: value for key, value in filters.items() if key != "metadata"}}


def _compile(column: InstrumentedAttribute[Any], filters: dict[str, Any], *, depth: int) -> list[ColumnElement[bool]]:
    return [
        _compile_bool_op(column, key, value, depth=depth)
        if key in {"$or", "$and"}
        else _compile_field(column, key, value)
        for key, value in filters.items()
    ]


def _compile_bool_op(column: InstrumentedAttribute[Any], op: str, branches: Any, *, depth: int) -> ColumnElement[bool]:
    if depth >= _MAX_FILTER_DEPTH:
        raise HTTPException(500, f"Auth handler filter exceeds {_MAX_FILTER_DEPTH} levels")
    if not isinstance(branches, list) or len(branches) < 2:
        raise HTTPException(500, f"Auth handler filter '{op}' requires at least two branches")
    if any(not isinstance(branch, dict) or not branch for branch in branches):
        raise HTTPException(500, f"Auth handler filter '{op}' contains an invalid branch")
    compiled = [and_(*_compile(column, branch, depth=depth + 1)) for branch in branches]
    return or_(*compiled) if op == "$or" else and_(*compiled)


def _compile_field(column: InstrumentedAttribute[Any], key: str, value: Any) -> ColumnElement[bool]:
    if not isinstance(value, dict):
        return column.op("@>")({key: value})
    if len(value) != 1:
        raise HTTPException(500, "Auth handler field filters require exactly one operator")
    operator, operand = next(iter(value.items()))
    if operator == "$eq":
        return column.op("@>")({key: operand})
    if operator == "$contains":
        return column[key].op("@>")(operand if isinstance(operand, list) else [operand])
    raise HTTPException(500, f"Unsupported auth handler filter operator '{operator}'")
