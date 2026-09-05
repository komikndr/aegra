import base64
import io

import pytest
from aegra_message_gateway.images import InvalidImageError, decode_images
from aegra_message_gateway.schemas import TransportMessage
from PIL import Image
from pydantic import ValidationError


def _source() -> dict[str, str]:
    return {
        "platform": "telegram",
        "user_id": "42",
        "chat_id": "42",
        "chat_type": "dm",
        "message_id": "1001",
        "session_id": "telegram:42",
    }


def _png_data_url() -> str:
    output = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(output, format="PNG")
    return f"data:image/png;base64,{base64.b64encode(output.getvalue()).decode()}"


def test_transport_message_accepts_text_and_images() -> None:
    request = TransportMessage.model_validate(
        {
            "source": _source(),
            "text": "current",
            "image_data_urls": [_png_data_url()],
        }
    )

    assert request.text == "current"
    assert len(request.image_data_urls) == 1


def test_request_requires_stable_message_identity() -> None:
    source = _source()
    source.pop("message_id")
    with pytest.raises(ValidationError):
        TransportMessage.model_validate({"source": source, "text": "hello"})


def test_image_decoder_uses_actual_file_type() -> None:
    image = decode_images([_png_data_url()])[0]
    assert image.mime_type == "image/png"
    assert image.data.startswith(b"\x89PNG")


@pytest.mark.parametrize("url", ["https://example.com/image.png", "file:///etc/passwd", "data:image/png;base64,%%%%"])
def test_image_decoder_rejects_remote_paths_and_bad_data(url: str) -> None:
    with pytest.raises(InvalidImageError):
        decode_images([url])
