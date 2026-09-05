"""Bounded image decoding for direct messaging transports."""

import base64
import binascii
import io
import warnings
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

MAX_IMAGES = 4
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 12 * 1024 * 1024
MAX_PIXELS = 20_000_000
MAX_DIMENSION = 10_000
FORMAT_MIME = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


class InvalidImageError(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedImage:
    data: bytes
    mime_type: str


def decode_images(data_urls: list[str]) -> list[ValidatedImage]:
    if len(data_urls) > MAX_IMAGES:
        raise InvalidImageError("too many images")
    images: list[ValidatedImage] = []
    total = 0
    for data_url in data_urls:
        if not data_url.startswith("data:image/") or ";base64," not in data_url:
            raise InvalidImageError("only base64 data image URLs are accepted")
        encoded = data_url.split(";base64,", 1)[1]
        if len(encoded) > ((MAX_IMAGE_BYTES + 2) // 3) * 4 + 4:
            raise InvalidImageError("image is too large")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise InvalidImageError("invalid image encoding") from exc
        if not raw or len(raw) > MAX_IMAGE_BYTES:
            raise InvalidImageError("image is empty or too large")
        total += len(raw)
        if total > MAX_TOTAL_BYTES:
            raise InvalidImageError("image upload is too large")
        images.append(ValidatedImage(data=raw, mime_type=_validate_image(raw)))
    return images


def _validate_image(raw: bytes) -> str:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                image_format = (image.format or "").upper()
                width, height = image.size
                if image_format not in FORMAT_MIME:
                    raise InvalidImageError("unsupported image format")
                if (
                    width <= 0
                    or height <= 0
                    or width > MAX_DIMENSION
                    or height > MAX_DIMENSION
                    or width * height > MAX_PIXELS
                ):
                    raise InvalidImageError("image dimensions are too large")
                image.verify()
                return FORMAT_MIME[image_format]
    except InvalidImageError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise InvalidImageError("image dimensions are too large") from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise InvalidImageError("invalid image file") from exc
