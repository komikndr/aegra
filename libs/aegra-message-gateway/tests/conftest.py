import base64

import pytest
from aegra_message_gateway.settings import GatewaySettings


@pytest.fixture
def settings() -> GatewaySettings:
    key = base64.b64encode(b"k" * 32).decode()
    return GatewaySettings(
        database_url="postgresql://user:pass@localhost/aegra",
        encryption_key=key,
        hmac_key=key,
        aegra_url="http://aegra.test",
        keycloak_internal_issuer="http://keycloak.test/realms/aegra",
        keycloak_browser_issuer="https://login.test/realms/aegra",
        telegram_bot_token="telegram-token",
        telegram_allowed_users="42",
    )
