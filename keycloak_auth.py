"""Keycloak authentication handler for Aegra.

Configure the auth path in `aegra.json`:

{
  "auth": {
    "path": "./keycloak_auth.py:auth"
  }
}

Required environment variables:
- KEYCLOAK_ISSUER or (KEYCLOAK_URL + KEYCLOAK_REALM)

Optional environment variables:
- KEYCLOAK_JWKS_URL
- KEYCLOAK_AUDIENCE
- KEYCLOAK_CLIENT_ID
"""

import os
from functools import lru_cache

import jwt
from jwt import InvalidTokenError, PyJWKClient
from jwt.exceptions import PyJWKClientError
from langgraph_sdk import Auth

auth = Auth()


def _env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _issuer() -> str:
    issuer = _env("KEYCLOAK_ISSUER")
    if issuer:
        return issuer.rstrip("/")

    base_url = _env("KEYCLOAK_URL")
    realm = _env("KEYCLOAK_REALM")
    if base_url and realm:
        return f"{base_url.rstrip('/')}/realms/{realm}"

    raise ValueError(
        "Keycloak is not configured. Set KEYCLOAK_ISSUER or KEYCLOAK_URL and KEYCLOAK_REALM."
    )


def _jwks_url(issuer: str) -> str:
    configured = _env("KEYCLOAK_JWKS_URL")
    if configured:
        return configured
    return f"{issuer}/protocol/openid-connect/certs"


@lru_cache(maxsize=1)
def _jwks_client() -> PyJWKClient:
    return PyJWKClient(_jwks_url(_issuer()))


def _extract_permissions(claims: dict) -> list[str]:
    permissions: set[str] = set()

    realm_access = claims.get("realm_access", {})
    realm_roles = (
        realm_access.get("roles", []) if isinstance(realm_access, dict) else []
    )
    permissions.update(role for role in realm_roles if isinstance(role, str))

    resource_access = claims.get("resource_access", {})
    if isinstance(resource_access, dict):
        for client_data in resource_access.values():
            if not isinstance(client_data, dict):
                continue
            client_roles = client_data.get("roles", [])
            permissions.update(role for role in client_roles if isinstance(role, str))

    scope = claims.get("scope", "")
    if isinstance(scope, str) and scope:
        permissions.update(scope.split())

    return sorted(permissions)


@auth.authenticate
async def authenticate(headers: dict) -> dict:
    auth_header = headers.get("authorization", "") or headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise Auth.exceptions.HTTPException(
            status_code=401,
            detail="Missing or invalid Authorization header",
        )

    token = auth_header[7:].strip()
    if not token:
        raise Auth.exceptions.HTTPException(
            status_code=401, detail="Missing bearer token"
        )

    try:
        issuer = _issuer()
        signing_key = _jwks_client().get_signing_key_from_jwt(token).key

        audience = _env("KEYCLOAK_AUDIENCE") or _env("KEYCLOAK_CLIENT_ID")
        decode_options = {"verify_aud": bool(audience)}

        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            issuer=issuer,
            audience=audience,
            options=decode_options,
        )
    except ValueError as exc:
        raise Auth.exceptions.HTTPException(status_code=500, detail=str(exc)) from exc
    except (InvalidTokenError, PyJWKClientError) as exc:
        raise Auth.exceptions.HTTPException(
            status_code=401, detail="Invalid token"
        ) from exc

    identity = claims.get("sub")
    if not identity:
        raise Auth.exceptions.HTTPException(
            status_code=401, detail="Token missing subject"
        )

    display_name = (
        claims.get("preferred_username")
        or claims.get("name")
        or claims.get("email")
        or identity
    )

    return {
        "identity": identity,
        "display_name": display_name,
        "is_authenticated": True,
        "permissions": _extract_permissions(claims),
        "email": claims.get("email", ""),
        "username": claims.get("preferred_username", ""),
        "issuer": claims.get("iss", ""),
        "realm": _env("KEYCLOAK_REALM") or "",
    }
