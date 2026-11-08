"""Separate deployment connection configuration from caller-controlled run context."""

from typing import Any


def sanitize_execution_context(context: dict[str, Any] | None) -> dict[str, Any]:
    """KMS connection, index and TLS settings come only from the worker environment.

    Apply this both when preparing and executing jobs, including jobs persisted
    before the boundary was introduced. Context dataclasses load server defaults.
    """
    return {key: value for key, value in (context or {}).items() if not key.startswith("kms_")}


def sanitize_execution_config(config: dict[str, Any] | None) -> dict[str, Any]:
    """Remove the same protected fields from legacy configurable inputs."""
    sanitized = dict(config or {})
    if isinstance(sanitized.get("configurable"), dict):
        sanitized["configurable"] = sanitize_execution_context(sanitized["configurable"])
    return sanitized
