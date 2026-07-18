import json

import pytest

from . import model_config


@pytest.fixture(autouse=True)
def reset_config_cache() -> None:
    model_config._CONFIG_CACHE = None


def test_pipeline_models_expose_only_opaque_metadata(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "models.json"
    config_path.write_text(
        json.dumps(
            {
                "pipeline_models": {
                    "safe-default": {
                        "label": "Safe Default",
                        "model": "openai/internal-model",
                        "base_url": "http://internal.test/v1",
                        "api_key": "secret",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_MODELS_CONFIG", str(config_path))

    assert model_config.list_pipeline_models() == [{"id": "safe-default", "label": "Safe Default"}]


def test_pipeline_model_resolution_uses_only_server_config(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "models.json"
    config_path.write_text(
        json.dumps(
            {
                "pipeline_models": {
                    "safe-default": {
                        "model": "openai/internal-model",
                        "base_url": "http://internal.test/v1",
                        "api_key_env": "PIPELINE_MODEL_KEY",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_MODELS_CONFIG", str(config_path))
    monkeypatch.setenv("PIPELINE_MODEL_KEY", "server-secret")

    resolved = model_config.resolve_pipeline_model("safe-default")

    assert resolved.model == "openai/internal-model"
    assert resolved.base_url == "http://internal.test/v1"
    assert resolved.api_key == "server-secret"


def test_unknown_pipeline_model_is_rejected(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "models.json"
    config_path.write_text('{"pipeline_models": {}}', encoding="utf-8")
    monkeypatch.setenv("AGENT_MODELS_CONFIG", str(config_path))

    with pytest.raises(ValueError, match="Unknown pipeline model id"):
        model_config.resolve_pipeline_model("attacker-model")
