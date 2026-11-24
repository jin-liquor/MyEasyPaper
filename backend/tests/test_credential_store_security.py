import base64
import json

import pytest

from app.services.credential_store import (
    CredentialStoreError,
    Keyring,
    decrypt_secret,
    encrypt_secret,
    generate_key,
    load_keyring,
    rewrap_secret,
)


def test_generate_key_is_a_32_byte_urlsafe_value() -> None:
    encoded = generate_key()
    assert len(base64.urlsafe_b64decode(encoded)) == 32


def test_keyring_rotation_rewraps_old_ciphertext_and_is_idempotent() -> None:
    old_ring = Keyring({"v1": b"o" * 32}, "v1")
    rotated_ring = Keyring({"v1": b"o" * 32, "v2": b"n" * 32}, "v2")
    encrypted = encrypt_secret(7, "openai", "test-secret", keyring=old_ring)

    rotated = rewrap_secret(7, "openai", encrypted, keyring=rotated_ring)
    envelope = json.loads(rotated)
    assert envelope["kid"] == "v2"
    assert decrypt_secret(7, "openai", rotated, keyring=rotated_ring) == "test-secret"
    assert rewrap_secret(7, "openai", rotated, keyring=rotated_ring) == rotated


def test_aad_prevents_cross_account_or_provider_decryption() -> None:
    ring = Keyring({"v1": b"k" * 32}, "v1")
    encrypted = encrypt_secret(7, "openai", "test-secret", keyring=ring)

    with pytest.raises(CredentialStoreError) as account_error:
        decrypt_secret(8, "openai", encrypted, keyring=ring)
    assert account_error.value.code == "CREDENTIAL_DECRYPTION_FAILED"

    with pytest.raises(CredentialStoreError) as provider_error:
        decrypt_secret(7, "anthropic", encrypted, keyring=ring)
    assert provider_error.value.code == "CREDENTIAL_DECRYPTION_FAILED"


def test_keyring_rejects_non_strict_base64() -> None:
    invalid = {
        "EASYPAPER_CREDENTIAL_ENCRYPTION_KEYS": json.dumps({"v1": "!" + "A" * 43}),
        "EASYPAPER_CREDENTIAL_ENCRYPTION_ACTIVE_VERSION": "v1",
    }
    with pytest.raises(CredentialStoreError) as error:
        load_keyring(invalid)
    assert error.value.code == "CREDENTIAL_KEY_MISCONFIGURED"
