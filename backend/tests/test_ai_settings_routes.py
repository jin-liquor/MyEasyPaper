from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, create_engine

from app.models.knowledge import UserSetting


@pytest.fixture
def settings_client(monkeypatch):
    from contextlib import asynccontextmanager

    from fastapi.testclient import TestClient

    from app.api import ai_settings_routes as settings
    from app.api.deps import get_current_user
    from app.main import app

    @asynccontextmanager
    async def no_lifespan(_app):
        yield

    monkeypatch.setattr(app.router, "lifespan_context", no_lifespan)

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(settings, "engine", engine)
    monkeypatch.setenv(
        "EASYPAPER_CREDENTIAL_ENCRYPTION_KEY", __import__("base64").urlsafe_b64encode(b"x" * 32).decode()
    )
    current = {"user": SimpleNamespace(id=101)}
    app.dependency_overrides[get_current_user] = lambda: current["user"]
    with TestClient(app) as client:
        yield client, current
    app.dependency_overrides.pop(get_current_user, None)
    SQLModel.metadata.drop_all(engine)
    engine.dispose()


def _provider_payload(name="Provider A", key="never-return-this-secret"):
    return {
        "name": name,
        "protocol": "chat_completions",
        "base_url": "https://example.invalid/v1",
        "api_key": key,
        "models": [
            {
                "model_name": "model-a",
                "context_window": 8192,
                "max_output_tokens": 1024,
                "input_types": ["text"],
                "reasoning": {"supported": True, "levels": [{"label": "Low", "api_value": "low"}]},
            }
        ],
    }


def test_ai_settings_crud_isolation_encryption_and_redaction(settings_client):
    client, current = settings_client
    created = client.post("/api/settings/ai/providers", json=_provider_payload())
    assert created.status_code == 201
    provider = created.json()["providers"][0]
    provider_id, model_id = provider["id"], provider["models"][0]["id"]
    assert provider["secret_configured"] is True
    assert "never-return-this-secret" not in created.text

    from sqlmodel import Session

    from app.api import ai_settings_routes as settings

    # Verify ciphertext in storage and decrypts only with the matching owner/AAD.
    with Session(settings.engine) as session:
        row = session.get(UserSetting, settings._setting_id(101, settings.SECRET_PREFIX + provider_id))
        secret_record = json.loads(row.value_json)
        assert "never-return-this-secret" not in row.value_json
        assert settings.decrypt_secret(101, provider_id, secret_record["ciphertext"]) == "never-return-this-secret"
        with pytest.raises(HTTPException):
            settings.decrypt_secret(202, provider_id, secret_record["ciphertext"])

    assert client.patch("/api/settings/ai/default-model", json={"model_id": model_id}).status_code == 200
    assert client.get("/api/settings/ai").json()["default_model_id"] == model_id
    assert client.patch(f"/api/settings/ai/providers/{provider_id}", json={"name": "Renamed"}).status_code == 200
    assert (
        client.patch(f"/api/settings/ai/providers/{provider_id}", json={"api_key": "rotated-secret"}).status_code == 200
    )
    assert "rotated-secret" not in client.get("/api/settings/ai").text
    assert client.delete(f"/api/settings/ai/models/{model_id}").status_code == 409
    assert client.delete(f"/api/settings/ai/providers/{provider_id}").status_code == 409

    current["user"] = SimpleNamespace(id=202)
    assert client.get("/api/settings/ai").json() == {"providers": [], "default_model_id": None}
    assert client.patch(f"/api/settings/ai/providers/{provider_id}", json={"name": "steal"}).status_code == 404
    assert (
        client.post("/api/settings/ai/providers", json=_provider_payload("Provider B", "second-secret")).status_code
        == 201
    )
    assert "never-return-this-secret" not in client.get("/api/settings/ai").text


def test_ai_settings_model_crud_secret_delete_and_default(settings_client):
    client, _ = settings_client
    provider_id = client.post("/api/settings/ai/providers", json=_provider_payload()).json()["providers"][0]["id"]
    new_model = client.post(
        "/api/settings/ai/models",
        json={
            "provider_id": provider_id,
            "model_name": "model-b",
            "context_window": 4096,
            "max_output_tokens": 512,
            "input_types": ["text"],
        },
    )
    assert new_model.status_code == 201
    model_id = new_model.json()["id"]
    assert client.patch(f"/api/settings/ai/models/{model_id}", json={"context_window": 8192}).status_code == 200
    assert client.patch("/api/settings/ai/default-model", json={"model_id": model_id}).status_code == 200
    assert client.delete(f"/api/settings/ai/providers/{provider_id}/secret").json() == {"secret_configured": False}
    assert client.get("/api/settings/ai").json()["providers"][0]["secret_configured"] is False
    assert client.delete(f"/api/settings/ai/models/{model_id}").status_code == 409


def test_credential_status_reports_only_safe_readiness(settings_client, monkeypatch):
    client, _ = settings_client

    monkeypatch.delenv("EASYPAPER_CREDENTIAL_ENCRYPTION_KEYS", raising=False)
    monkeypatch.delenv("EASYPAPER_CREDENTIAL_ENCRYPTION_ACTIVE_VERSION", raising=False)
    ready = client.get("/api/settings/ai/credential-status")
    assert ready.status_code == 200
    assert ready.json() == {
        "ready": True,
        "code": "CREDENTIAL_ENCRYPTION_READY",
        "message": "服务器凭据加密已就绪。",
    }

    monkeypatch.setenv(
        "EASYPAPER_CREDENTIAL_ENCRYPTION_KEYS",
        json.dumps({"test-version": base64.urlsafe_b64encode(b"n" * 32).decode()}),
    )
    monkeypatch.setenv("EASYPAPER_CREDENTIAL_ENCRYPTION_ACTIVE_VERSION", "test-version")
    versioned_ready = client.get("/api/settings/ai/credential-status")
    assert versioned_ready.status_code == 200
    assert versioned_ready.json() == ready.json()
    assert "test-version" not in versioned_ready.text

    marker = "sensitive-key-marker-never-return"
    monkeypatch.setenv("EASYPAPER_CREDENTIAL_ENCRYPTION_KEYS", json.dumps({"private-kid-marker": marker}))
    monkeypatch.setenv("EASYPAPER_CREDENTIAL_ENCRYPTION_ACTIVE_VERSION", "missing-version-marker")
    not_ready = client.get("/api/settings/ai/credential-status")
    assert not_ready.status_code == 200
    assert not_ready.json() == {
        "ready": False,
        "code": "CREDENTIAL_ENCRYPTION_NOT_READY",
        "message": "服务器凭据加密尚未就绪。请联系管理员配置并验证服务器凭据密钥环。",
    }
    assert marker not in not_ready.text
    assert "private-kid-marker" not in not_ready.text
    assert "missing-version-marker" not in not_ready.text
    assert "EASYPAPER_CREDENTIAL_ENCRYPTION_KEYS" not in not_ready.text

    from app.api.deps import get_current_user
    from app.main import app

    original_override = app.dependency_overrides.pop(get_current_user)
    try:
        unauthorized = client.get("/api/settings/ai/credential-status")
        assert unauthorized.status_code == 401
    finally:
        app.dependency_overrides[get_current_user] = original_override


def test_public_settings_do_not_mutate_stored_provider_data():
    from app.api.ai_settings_routes import _public

    providers = [{"id": "p", "models": []}]

    # A tiny session double avoids requiring the application DB.
    class SessionStub:
        def get(self, *_args):
            return None

    result = _public(SessionStub(), 1, providers)
    assert result["providers"][0]["secret_configured"] is False
    assert "secret_configured" not in providers[0]
