"""Regression tests for PostgreSQL JSONB NULL-byte sanitization."""

from unittest.mock import MagicMock

import pytest

from aegra_api.core import orm
from aegra_api.core.orm import JsonbSafe, _strip_jsonb_null_bytes


def test_strip_jsonb_null_bytes_recursively() -> None:
    value = {"key\x00": ["a\x00b", {"nested": "\x00value"}], "clean": 3}

    assert _strip_jsonb_null_bytes(value) == {
        "key": ["ab", {"nested": "value"}],
        "clean": 3,
    }


def test_jsonb_safe_processes_bind_values() -> None:
    assert JsonbSafe().process_bind_param({"output": "a\x00b"}, MagicMock()) == {"output": "ab"}


def test_key_collision_is_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    warning = MagicMock()
    monkeypatch.setattr(orm.logger, "warning", warning)

    assert _strip_jsonb_null_bytes({"a\x00": 1, "a": 2}) == {"a": 2}
    warning.assert_called_once_with("jsonb_sanitize_key_collision", key="a")
