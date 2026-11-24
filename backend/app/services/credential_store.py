"""Server-side credential keyring and versioned AES-GCM envelopes.

The keyring is deployment configuration, never user data. Legacy single-key
ciphertexts remain readable during a controlled migration, but all new writes
use the active versioned key.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select, update


class CredentialStoreError(RuntimeError):
    def __init__(self, code: str, message: str = "凭据加密配置无效") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Keyring:
    keys: dict[str, bytes]
    active_version: str
    legacy: bool = False


def _decode_key(value: str) -> bytes:
    try:
        raw = base64.b64decode(value.encode("ascii"), altchars=b"-_", validate=True)
    except (ValueError, UnicodeEncodeError):
        return b""
    return raw if len(raw) == 32 else b""


def load_keyring(environ: dict[str, str] | None = None) -> Keyring:
    env = os.environ if environ is None else environ
    raw = env.get("EASYPAPER_CREDENTIAL_ENCRYPTION_KEYS", "").strip()
    if raw:
        try:
            values = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise CredentialStoreError("CREDENTIAL_KEY_MISCONFIGURED", "凭据密钥环配置不是有效 JSON") from exc
        if not isinstance(values, dict) or not values:
            raise CredentialStoreError("CREDENTIAL_KEY_MISCONFIGURED", "凭据密钥环不能为空")
        keys = {str(version): _decode_key(str(value)) for version, value in values.items()}
        if any(len(key) != 32 for key in keys.values()):
            raise CredentialStoreError("CREDENTIAL_KEY_MISCONFIGURED", "凭据密钥必须是 Base64 编码的 32 字节随机值")
        active = env.get("EASYPAPER_CREDENTIAL_ENCRYPTION_ACTIVE_VERSION", "").strip()
        if not active or active not in keys:
            raise CredentialStoreError("CREDENTIAL_KEY_MISCONFIGURED", "凭据密钥环缺少有效 active version")
        return Keyring(keys, active)

    # One release of the application used this name. Keep it readable while
    # deployments move to the durable, versioned keyring configuration.
    legacy = _decode_key(env.get("EASYPAPER_CREDENTIAL_ENCRYPTION_KEY", ""))
    if len(legacy) == 32:
        return Keyring({"v1": legacy}, "v1", legacy=True)
    raise CredentialStoreError("CREDENTIAL_KEY_MISCONFIGURED", "服务器尚未正确配置凭据加密密钥")


def validate_keyring(environ: dict[str, str] | None = None, *, production: bool = False) -> Keyring | None:
    try:
        return load_keyring(environ)
    except CredentialStoreError:
        if production:
            raise
        return None


def encrypt_secret(user_id: int, provider_id: str, secret: str, *, keyring: Keyring | None = None) -> str:
    ring = keyring or load_keyring()
    nonce = secrets.token_bytes(12)
    aad = f"{user_id}:{provider_id}".encode()
    ciphertext = AESGCM(ring.keys[ring.active_version]).encrypt(nonce, secret.encode("utf-8"), aad)
    return json.dumps(
        {
            "version": 2,
            "kid": ring.active_version,
            "alg": "A256GCM",
            "nonce": base64.urlsafe_b64encode(nonce).decode("ascii"),
            "ciphertext": base64.urlsafe_b64encode(ciphertext).decode("ascii"),
        },
        separators=(",", ":"),
    )


def _b64(value: str) -> bytes:
    try:
        return base64.b64decode(value.encode("ascii"), altchars=b"-_", validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise CredentialStoreError("CREDENTIAL_DECRYPTION_FAILED", "账户 AI 凭据无法解密") from exc


def decrypt_secret(user_id: int, provider_id: str, encrypted: str, *, keyring: Keyring | None = None) -> str:
    ring = keyring or load_keyring()
    aad = f"{user_id}:{provider_id}".encode()
    try:
        value: Any = json.loads(encrypted)
    except (TypeError, json.JSONDecodeError):
        value = None
    try:
        if isinstance(value, dict) and value.get("version") == 2:
            kid = str(value["kid"])
            key = ring.keys[kid]
            nonce = _b64(str(value["nonce"]))
            ciphertext = _b64(str(value["ciphertext"]))
        else:
            # Legacy format was nonce+ciphertext in one URL-safe Base64 value.
            # It is deliberately tried only against the explicitly named v1 key.
            key = ring.keys.get("v1")
            if key is None:
                raise CredentialStoreError("CREDENTIAL_DECRYPTION_FAILED", "账户 AI 凭据无法解密")
            raw = _b64(encrypted)
            nonce, ciphertext = raw[:12], raw[12:]
        if len(nonce) != 12 or not ciphertext:
            raise ValueError
        return AESGCM(key).decrypt(nonce, ciphertext, aad).decode("utf-8")
    except CredentialStoreError:
        raise
    except Exception as exc:
        raise CredentialStoreError("CREDENTIAL_DECRYPTION_FAILED", "账户 AI 凭据无法解密") from exc


def rewrap_secret(user_id: int, provider_id: str, encrypted: str, *, keyring: Keyring | None = None) -> str:
    ring = keyring or load_keyring()
    try:
        envelope = json.loads(encrypted)
    except (TypeError, json.JSONDecodeError):
        envelope = None
    if isinstance(envelope, dict) and envelope.get("version") == 2 and envelope.get("kid") == ring.active_version:
        decrypt_secret(user_id, provider_id, encrypted, keyring=ring)
        return encrypted
    plaintext = decrypt_secret(user_id, provider_id, encrypted, keyring=ring)
    return encrypt_secret(user_id, provider_id, plaintext, keyring=ring)


def rewrap_user_settings(
    session,
    setting_model,
    *,
    secret_prefix: str = "ai_provider_secret:",
    batch_size: int = 100,
    dry_run: bool = False,
) -> dict[str, int]:
    """Scan bounded pages and commit each successful compare-and-swap independently."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    ring = load_keyring()
    columns = setting_model
    base = select(columns.id, columns.user_id, columns.key, columns.value_json).where(
        columns.key.startswith(secret_prefix, autoescape=True)
    )
    cursor = None
    result = dict.fromkeys(("processed", "rewrapped", "would_rewrap", "skipped", "failed", "conflicts"), 0)
    while True:
        query = base if cursor is None else base.where(columns.id > cursor)
        rows = session.execute(query.order_by(columns.id).limit(batch_size)).all()
        session.rollback()  # End the read transaction before trying any writes.
        if not rows:
            return result
        cursor = rows[-1].id
        for row_id, user_id, key, value_json in rows:
            result["processed"] += 1
            try:
                value = json.loads(value_json)
                encrypted = value.get("ciphertext") if isinstance(value, dict) else None
                provider_id = key.removeprefix(secret_prefix)
                if not isinstance(encrypted, str) or not encrypted or not provider_id:
                    result["failed"] += 1
                    continue
                updated = rewrap_secret(user_id, provider_id, encrypted, keyring=ring)
            except (CredentialStoreError, TypeError, ValueError):
                result["failed"] += 1
                continue
            if updated == encrypted:
                result["skipped"] += 1
                continue
            if dry_run:
                result["would_rewrap"] += 1
                continue
            value["ciphertext"] = updated
            write = (
                update(columns)
                .where(
                    columns.id == row_id,
                    columns.user_id == user_id,
                    columns.key == key,
                    columns.value_json == value_json,
                )
                .values(value_json=json.dumps(value, separators=(",", ":")))
            )
            changed = session.execute(write).rowcount
            session.commit()
            result["rewrapped" if changed == 1 else "conflicts"] += 1


def generate_key() -> str:
    """Generate one URL-safe Base64 32-byte deployment key without persisting it."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
