"""Cryptographic boundaries for external identities and OAuth credentials."""

import hashlib
import hmac
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class SecretBox:
    """Versioned AES-GCM envelope encryption."""

    _VERSION = b"\x01"

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("AES-GCM key must be 32 bytes")
        self._cipher = AESGCM(key)

    def encrypt(self, plaintext: str, *, associated_data: str) -> bytes:
        nonce = secrets.token_bytes(12)
        ciphertext = self._cipher.encrypt(nonce, plaintext.encode(), associated_data.encode())
        return self._VERSION + nonce + ciphertext

    def decrypt(self, envelope: bytes, *, associated_data: str) -> str:
        if len(envelope) < 30 or envelope[:1] != self._VERSION:
            raise ValueError("unsupported encrypted credential envelope")
        try:
            plaintext = self._cipher.decrypt(envelope[1:13], envelope[13:], associated_data.encode())
            return plaintext.decode()
        except (InvalidTag, UnicodeDecodeError) as exc:
            raise ValueError("encrypted credential could not be decrypted") from exc


class ExternalIdentityHasher:
    """Create non-reversible stable references for transport identifiers."""

    def __init__(self, key: bytes) -> None:
        if len(key) < 32:
            raise ValueError("HMAC key must be at least 32 bytes")
        self._key = key

    def digest(self, namespace: str, value: str) -> str:
        payload = f"{namespace}\x00{value}".encode()
        return hmac.new(self._key, payload, hashlib.sha256).hexdigest()
