"""Regression tests for the gateway's Keycloak client configuration."""

import json
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
REALM_CONFIGS = (
    REPOSITORY_ROOT / "keycloak-realm-aegra.json",
    REPOSITORY_ROOT / "ops/keycloak/import/keycloak-realm-aegra.json",
)


@pytest.mark.parametrize("realm_path", REALM_CONFIGS)
def test_message_gateway_allows_offline_access(realm_path: Path) -> None:
    realm = json.loads(realm_path.read_text())
    client = next(client for client in realm["clients"] if client["clientId"] == "aegra-message-gateway")

    assert client["publicClient"] is True
    assert client["attributes"]["oauth2.device.authorization.grant.enabled"] == "true"
    assert "offline_access" in client["optionalClientScopes"]
    assert all("offline_access" in user["realmRoles"] for user in realm["users"])


def test_existing_realm_initializer_assigns_offline_access() -> None:
    initializer = (REPOSITORY_ROOT / "ops/keycloak/init-minio-client.sh").read_text()

    assert 'ensure_optional_client_scope "$client_uuid" offline_access' in initializer
    assert "ensure_offline_access_for_users" in initializer
