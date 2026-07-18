"""Tests for RunCreate model validation."""

import pytest
from pydantic import ValidationError

from aegra_api.models.runs import RunCreate


class TestRunCreateValidation:
    """Tests for RunCreate input/command validation."""

    def test_allows_checkpoint_only_payload(self) -> None:
        """Checkpoint-only payloads preserve None input for graph resume."""
        run_create = RunCreate(
            assistant_id="agent",
            checkpoint={"checkpoint_id": "chk-1", "checkpoint_ns": ""},
        )

        assert run_create.input is None
        assert run_create.command is None

    def test_rejects_payload_without_input_command_or_checkpoint(self) -> None:
        """Ensure payloads with no input, command, or checkpoint are rejected."""
        with pytest.raises(ValueError, match="Must specify at least one of 'input', 'command', or 'checkpoint'"):
            RunCreate(assistant_id="agent")

    @pytest.mark.parametrize("value", [{"nested": True}, ["item"], None])
    def test_rejects_non_primitive_metadata(self, value: object) -> None:
        with pytest.raises(ValidationError, match="must be str/int/float/bool"):
            RunCreate(assistant_id="agent", input={}, metadata={"invalid": value})

    def test_rejects_invalid_metadata_key(self) -> None:
        with pytest.raises(ValidationError, match="must match"):
            RunCreate(assistant_id="agent", input={}, metadata={"invalid.key": "value"})

    def test_rejects_excessive_metadata(self) -> None:
        with pytest.raises(ValidationError, match="exceeds 32 keys"):
            RunCreate(assistant_id="agent", input={}, metadata={f"key_{index}": index for index in range(33)})

    def test_accepts_bounded_primitive_metadata(self) -> None:
        request = RunCreate(
            assistant_id="agent",
            input={},
            metadata={"tenant": "onyx", "attempt": 2, "ratio": 0.5, "cached": True},
        )

        assert request.metadata == {"tenant": "onyx", "attempt": 2, "ratio": 0.5, "cached": True}
