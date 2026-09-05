import pytest
from aegra_message_gateway.security import ExternalIdentityHasher, SecretBox


def test_secret_box_round_trip_and_aad_binding() -> None:
    box = SecretBox(b"e" * 32)
    envelope = box.encrypt("refresh-token", associated_data="telegram:user-a")

    assert b"refresh-token" not in envelope
    assert box.decrypt(envelope, associated_data="telegram:user-a") == "refresh-token"
    with pytest.raises(ValueError, match="could not be decrypted"):
        box.decrypt(envelope, associated_data="telegram:user-b")


def test_external_identity_hashes_are_stable_and_namespaced() -> None:
    hasher = ExternalIdentityHasher(b"h" * 32)

    assert hasher.digest("principal", "123") == hasher.digest("principal", "123")
    assert hasher.digest("principal", "123") != hasher.digest("conversation", "123")
    assert "123" not in hasher.digest("principal", "123")
