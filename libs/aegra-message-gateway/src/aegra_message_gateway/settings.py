"""Runtime settings for the message gateway."""

import base64
from functools import cached_property, lru_cache

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


class GatewaySettings(BaseSettings):
    """Validated environment configuration."""

    model_config = SettingsConfigDict(
        env_prefix="MESSAGE_GATEWAY_",
        env_file=None,
        case_sensitive=False,
        extra="ignore",
    )

    database_url: str | None = None
    database_host: str | None = None
    database_port: int = Field(default=5432, ge=1, le=65535)
    database_user: str | None = None
    database_password: SecretStr | None = None
    database_name: str | None = None
    encryption_key: SecretStr
    hmac_key: SecretStr

    telegram_bot_token: SecretStr | None = None
    telegram_allowed_users: str = ""
    telegram_stream_edit_interval_seconds: float = Field(default=1.0, ge=0.25, le=10.0)
    telegram_stream_min_edit_chars: int = Field(default=24, ge=1, le=512)
    telegram_max_concurrent_updates: int = Field(default=8, ge=1, le=64)
    telegram_max_concurrent_runs: int = Field(default=4, ge=1, le=32)
    telegram_min_request_interval_seconds: float = Field(default=0.5, ge=0.0, le=60.0)

    aegra_url: str = "http://aegra:2026"
    chart_renderer_url: str = "http://echarts-renderer:8787"
    chart_renderer_timeout_seconds: float = Field(default=20.0, ge=1.0, le=120.0)
    chart_renderer_max_image_bytes: int = Field(default=5 * 1024 * 1024, ge=1, le=10 * 1024 * 1024)
    keycloak_internal_issuer: str = "http://keycloak:8080/realms/aegra"
    keycloak_browser_issuer: str = "http://localhost:8080/realms/aegra"
    keycloak_client_id: str = "aegra-message-gateway"
    keycloak_scopes: str = "openid profile offline_access"

    request_timeout_seconds: float = Field(default=30.0, ge=1.0, le=300.0)
    stream_timeout_seconds: float = Field(default=1800.0, ge=30.0, le=7200.0)
    max_message_chars: int = Field(default=20_000, ge=1, le=100_000)
    max_assistant_results: int = Field(default=1000, ge=100, le=5000)
    inbound_processing_lease_seconds: int = Field(default=7500, ge=300, le=10800)
    inbound_retention_days: int = Field(default=30, ge=1, le=365)
    maintenance_interval_seconds: int = Field(default=21600, ge=300, le=86400)

    @property
    def telegram_allowed_user_ids(self) -> frozenset[int]:
        values: set[int] = set()
        for raw in self.telegram_allowed_users.split(","):
            item = raw.strip()
            if not item:
                continue
            try:
                values.add(int(item))
            except ValueError as exc:
                raise ValueError("TELEGRAM_ALLOWED_USERS must contain numeric Telegram user IDs") from exc
        return frozenset(values)

    @field_validator("aegra_url", "chart_renderer_url", "keycloak_internal_issuer", "keycloak_browser_issuer")
    @classmethod
    def normalize_url(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("encryption_key", "hmac_key")
    @classmethod
    def validate_encoded_key(cls, value: SecretStr) -> SecretStr:
        try:
            decoded = base64.b64decode(value.get_secret_value(), validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("must be valid base64") from exc
        if len(decoded) != 32:
            raise ValueError("must decode to exactly 32 bytes")
        return value

    @model_validator(mode="after")
    def validate_database_and_lease(self) -> "GatewaySettings":
        if self.database_url is None and not all(
            (self.database_host, self.database_user, self.database_password, self.database_name)
        ):
            raise ValueError(
                "database_url or database_host/database_user/database_password/database_name must be configured"
            )
        if self.inbound_processing_lease_seconds < self.stream_timeout_seconds + 60:
            raise ValueError(
                "inbound_processing_lease_seconds must exceed stream_timeout_seconds by at least 60 seconds"
            )
        return self

    @cached_property
    def encryption_key_bytes(self) -> bytes:
        return base64.b64decode(self.encryption_key.get_secret_value(), validate=True)

    @cached_property
    def hmac_key_bytes(self) -> bytes:
        return base64.b64decode(self.hmac_key.get_secret_value(), validate=True)

    @property
    def async_database_url(self) -> str:
        if self.database_url is not None:
            if self.database_url.startswith("postgresql+asyncpg://"):
                return self.database_url
            if self.database_url.startswith("postgresql://"):
                return self.database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
            return self.database_url
        password = self.database_password
        return URL.create(
            drivername="postgresql+asyncpg",
            username=self.database_user,
            password=password.get_secret_value() if password is not None else None,
            host=self.database_host,
            port=self.database_port,
            database=self.database_name,
        ).render_as_string(hide_password=False)


@lru_cache(maxsize=1)
def get_settings() -> GatewaySettings:
    return GatewaySettings()  # type: ignore[call-arg]
