"""Tests for explicit graph access policy."""

import pytest
from fastapi import HTTPException

from aegra_api.core.agent_access import configure_graph_access, ensure_graph_access
from aegra_api.models import User


def _user(*permissions: str) -> User:
    return User(identity="user-1", permissions=list(permissions))


def test_configure_graph_access_requires_complete_exact_mapping() -> None:
    with pytest.raises(ValueError, match="missing policies"):
        configure_graph_access(["public", "restricted"], {"public": []})

    with pytest.raises(ValueError, match="unknown graphs"):
        configure_graph_access(["public"], {"public": [], "typo": []})


def test_graph_access_is_explicit_and_unknown_graphs_are_denied() -> None:
    configure_graph_access(["public", "restricted"], {"public": [], "restricted": ["Staff"]})

    ensure_graph_access(_user(), None)
    ensure_graph_access(_user(), "public")
    ensure_graph_access(_user("staff"), "restricted")

    with pytest.raises(HTTPException) as wrong_role:
        ensure_graph_access(_user("Executive"), "restricted")
    assert wrong_role.value.status_code == 403

    with pytest.raises(HTTPException) as unknown:
        ensure_graph_access(_user(), "unknown")
    assert unknown.value.status_code == 403


def test_superadmin_is_explicit_global_bypass() -> None:
    configure_graph_access(["known"], {"known": ["Staff"]})

    ensure_graph_access(_user("SuperAdmin"), "unknown")
