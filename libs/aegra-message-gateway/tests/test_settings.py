import base64

import pytest
from aegra_message_gateway.settings import GatewaySettings
from pydantic import ValidationError


def _key() -> str:
    return base64.b64encode(b"s" * 32).decode()


def test_split_database_settings_encode_reserved_password_characters() -> None:
    settings = GatewaySettings(
        database_host="postgres",
        database_user="gateway",
        database_password="p@ss:/word",
        database_name="aegra",
        encryption_key=_key(),
        hmac_key=_key(),
    )

    assert settings.async_database_url == "postgresql+asyncpg://gateway:p%40ss%3A%2Fword@postgres:5432/aegra"


def test_processing_lease_must_outlive_stream_timeout() -> None:
    with pytest.raises(ValidationError, match="must exceed stream_timeout_seconds"):
        GatewaySettings(
            database_url="postgresql://user:pass@localhost/aegra",
            encryption_key=_key(),
            hmac_key=_key(),
            stream_timeout_seconds=1800,
            inbound_processing_lease_seconds=1800,
        )
