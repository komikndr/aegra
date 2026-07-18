"""Tests for store namespace scoping / user isolation."""

import pytest
from fastapi import HTTPException

from aegra_api.api.store import apply_user_namespace_scoping


class TestApplyUserNamespaceScoping:
    """Verify that apply_user_namespace_scoping enforces per-user isolation."""

    def test_empty_namespace_defaults_to_user_prefix(self) -> None:
        result = apply_user_namespace_scoping("user-123", [])
        assert result == ["users", "user-123"]

    def test_own_namespace_passes_through(self) -> None:
        ns = ["users", "user-123", "documents"]
        result = apply_user_namespace_scoping("user-123", ns)
        assert result == ["users", "user-123", "documents"]

    def test_own_namespace_exact_passes_through(self) -> None:
        ns = ["users", "user-123"]
        result = apply_user_namespace_scoping("user-123", ns)
        assert result == ["users", "user-123"]

    def test_other_user_namespace_is_rejected(self) -> None:
        """A user cannot access another user's namespace."""
        ns = ["users", "victim-456", "secrets"]
        with pytest.raises(HTTPException) as exc_info:
            apply_user_namespace_scoping("user-123", ns)
        assert exc_info.value.status_code == 403

    def test_other_user_namespace_no_passthrough(self) -> None:
        """Reject an attacker-supplied namespace for another user."""
        ns = ["users", "victim-456"]
        with pytest.raises(HTTPException) as exc_info:
            apply_user_namespace_scoping("user-123", ns)
        assert exc_info.value.status_code == 403

    def test_arbitrary_namespace_is_scoped_under_user(self) -> None:
        """Non-user namespaces get prefixed with the caller's user scope."""
        ns = ["global", "shared-data"]
        result = apply_user_namespace_scoping("user-123", ns)
        assert result == ["users", "user-123", "global", "shared-data"]

    def test_single_element_namespace_is_scoped(self) -> None:
        ns = ["configs"]
        result = apply_user_namespace_scoping("user-123", ns)
        assert result == ["users", "user-123", "configs"]

    def test_users_prefix_without_id_is_rejected(self) -> None:
        """Reject an incomplete explicit user namespace."""
        ns = ["users"]
        with pytest.raises(HTTPException) as exc_info:
            apply_user_namespace_scoping("user-123", ns)
        assert exc_info.value.status_code == 403

    def test_users_prefix_with_wrong_id_is_rejected(self) -> None:
        ns = ["users", "other-user"]
        with pytest.raises(HTTPException) as exc_info:
            apply_user_namespace_scoping("user-123", ns)
        assert exc_info.value.status_code == 403

    def test_deeply_nested_own_namespace(self) -> None:
        ns = ["users", "user-123", "a", "b", "c"]
        result = apply_user_namespace_scoping("user-123", ns)
        assert result == ["users", "user-123", "a", "b", "c"]
