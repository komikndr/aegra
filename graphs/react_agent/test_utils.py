import pytest

from . import utils


@pytest.mark.parametrize("value", ["instant", "Instant", " none "])
def test_normalize_reasoning_effort_maps_instant_to_none(value: str) -> None:
    assert utils.normalize_reasoning_effort(value) == "none"


@pytest.mark.parametrize("owner", ["llamacpp", "llama.cpp", "llama_cpp"])
def test_payload_looks_like_llamacpp_accepts_known_owner_names(owner: str) -> None:
    assert utils._payload_looks_like_llamacpp({"data": [{"owned_by": owner}]}) is True


def test_llamacpp_instant_disables_thinking(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(utils, "is_vllm_base_url", lambda *args, **kwargs: False)
    monkeypatch.setattr(utils, "is_llamacpp_base_url", lambda *args, **kwargs: True)

    result = utils._reasoning_model_kwargs(
        "openai/model",
        "instant",
        base_url="http://llamacpp.test/v1",
    )

    assert result == {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}


def test_llamacpp_reasoning_mode_enables_thinking_with_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(utils, "is_vllm_base_url", lambda *args, **kwargs: False)
    monkeypatch.setattr(utils, "is_llamacpp_base_url", lambda *args, **kwargs: True)

    result = utils._reasoning_model_kwargs(
        "openai/model",
        "medium",
        base_url="http://llamacpp.test/v1",
    )

    assert result == {
        "extra_body": {
            "chat_template_kwargs": {"enable_thinking": True},
            "reasoning_budget_tokens": 1024,
            "thinking_budget_tokens": 1024,
        }
    }
