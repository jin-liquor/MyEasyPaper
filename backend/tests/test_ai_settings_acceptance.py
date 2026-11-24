from __future__ import annotations

import base64
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, create_engine

from app.services import ai_client
from app.services.outbound import OutboundError


@pytest.fixture
def ai_acceptance_client(monkeypatch):
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
        "EASYPAPER_CREDENTIAL_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(b"a" * 32).decode(),
    )
    current = {"user": SimpleNamespace(id=301)}
    app.dependency_overrides[get_current_user] = lambda: current["user"]
    try:
        with TestClient(app) as client:
            yield client, current, settings
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        SQLModel.metadata.drop_all(engine)
        engine.dispose()


def _provider(name: str, protocol: str, key: str, model_name: str) -> dict:
    return {
        "name": name,
        "protocol": protocol,
        "base_url": "https://provider.invalid/v1",
        "api_key": key,
        "models": [
            {
                "model_name": model_name,
                "context_window": 4096,
                "max_output_tokens": 1024,
                "input_types": ["text"],
                "reasoning": {
                    "supported": True,
                    "levels": [{"label": "High", "api_value": "high"}, {"label": "Low", "api_value": "low"}],
                    "default_level": "Low",
                },
            }
        ],
    }


def test_provider_test_route_maps_protocol_and_uses_only_requesting_users_key(ai_acceptance_client, monkeypatch):
    client, current, settings = ai_acceptance_client
    captured: list[tuple[str, str, dict, dict]] = []

    async def fake_request(method, endpoint, *, headers, json):
        captured.append((method, endpoint, headers, json))
        return ({"output_text": "OK"}, 23)

    monkeypatch.setattr(settings, "request_json", fake_request)
    created = client.post(
        "/api/settings/ai/providers",
        json=_provider("Account 301", "responses", "secret-301", "model-301"),
    )
    assert created.status_code == 201
    provider_301 = created.json()["providers"][0]
    model_301 = provider_301["models"][0]["id"]

    current["user"] = SimpleNamespace(id=302)
    created = client.post(
        "/api/settings/ai/providers",
        json=_provider("Account 302", "chat_completions", "secret-302", "model-302"),
    )
    assert created.status_code == 201
    provider_302 = created.json()["providers"][0]
    model_302 = provider_302["models"][0]["id"]

    current["user"] = SimpleNamespace(id=301)
    result_301 = client.post(f"/api/settings/ai/providers/{provider_301['id']}/test", json={"model_id": model_301})
    current["user"] = SimpleNamespace(id=302)
    result_302 = client.post(f"/api/settings/ai/providers/{provider_302['id']}/test", json={"model_id": model_302})

    assert result_301.status_code == result_302.status_code == 200
    assert result_301.json()["latency_ms"] == result_302.json()["latency_ms"] == 23
    assert captured == [
        (
            "POST",
            "https://provider.invalid/v1/responses",
            {"Authorization": "Bearer secret-301"},
            {
                "model": "model-301",
                "max_output_tokens": 16,
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "Reply with OK."}]}],
                "reasoning": {"effort": "low"},
            },
        ),
        (
            "POST",
            "https://provider.invalid/v1/chat/completions",
            {"Authorization": "Bearer secret-302"},
            {
                "model": "model-302",
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "Reply with OK."}],
                "reasoning_effort": "low",
            },
        ),
    ]


@pytest.mark.parametrize(
    ("method", "suffix", "body", "malformed"),
    [
        ("POST", "/providers", {"api_key": "fixture-sensitive-value", "protocol": "unknown"}, False),
        ("PATCH", "/providers/fixture", {"api_key": {"value": "fixture-sensitive-value"}}, False),
        ("POST", "/providers/fixture/secret", {"api_key": ["fixture-sensitive-value"]}, False),
        ("POST", "/providers", '{"api_key":"fixture-sensitive-value",', True),
    ],
)
def test_settings_validation_never_echoes_request_input(ai_acceptance_client, caplog, method, suffix, body, malformed):
    client, _, _ = ai_acceptance_client
    data = {"content": body, "headers": {"Content-Type": "application/json"}} if malformed else {"json": body}
    result = client.request(method, "/api/settings/ai" + suffix, **data)
    assert result.status_code == 422
    assert result.json() == {"detail": {"code": "AI_SETTINGS_INVALID", "message": "AI 设置请求格式无效"}}
    assert "fixture-sensitive-value" not in caplog.text


def test_discovery_upstream_failure_preserves_manual_model_fallback(ai_acceptance_client, monkeypatch):
    client, _, settings = ai_acceptance_client
    created = client.post(
        "/api/settings/ai/providers",
        json=_provider("Manual fallback", "chat_completions", "secret", "existing-model"),
    )
    provider_id = created.json()["providers"][0]["id"]
    calls = []

    async def failed_discovery(method, endpoint, *, headers, json):
        calls.append((method, endpoint))
        raise OutboundError("AI_UPSTREAM_UNREACHABLE", "无法连接 AI 服务", retryable=True)

    monkeypatch.setattr(settings, "request_json", failed_discovery)
    failed = client.post(
        f"/api/settings/ai/providers/{provider_id}/models/discover",
        json={"apply": True, "model_ids": ["discovered-model"]},
    )
    assert failed.status_code == 502
    assert failed.json()["detail"]["code"] == "AI_UPSTREAM_UNREACHABLE"
    assert failed.json()["detail"]["retryable"] is True
    assert calls == [("GET", "https://provider.invalid/v1/models")]
    assert [item["model_name"] for item in client.get("/api/settings/ai").json()["providers"][0]["models"]] == [
        "existing-model"
    ]

    manual = client.post(
        "/api/settings/ai/models",
        json={
            "provider_id": provider_id,
            "model_name": "manually-added-model",
            "context_window": 8192,
            "max_output_tokens": 1024,
            "input_types": ["text"],
        },
    )
    assert manual.status_code == 201
    assert manual.json()["model_name"] == "manually-added-model"


@pytest.mark.asyncio
async def test_account_clients_send_only_their_own_default_configuration(ai_acceptance_client, monkeypatch):
    client, current, _ = ai_acceptance_client

    def set_default(name, protocol, secret, model_name):
        response = client.post("/api/settings/ai/providers", json=_provider(name, protocol, secret, model_name))
        assert response.status_code == 201
        model_id = response.json()["providers"][0]["models"][0]["id"]
        assert client.patch("/api/settings/ai/default-model", json={"model_id": model_id}).status_code == 200

    set_default("Account 301", "responses", "fixture-user-301", "model-301")
    current["user"] = SimpleNamespace(id=302)
    set_default("Account 302", "chat_completions", "fixture-user-302", "model-302")

    captured = []

    async def fake_request(method, endpoint, *, headers, json, timeout):
        captured.append((endpoint, headers["Authorization"], json))
        if endpoint.endswith("/responses"):
            return {"output_text": "account 301"}, 10
        return {"choices": [{"message": {"content": "account 302"}}]}, 10

    monkeypatch.setattr(ai_client, "request_json", fake_request)
    first = ai_client.AIClient.for_user(301)
    second = ai_client.AIClient.for_user(302)
    assert await first.complete("System", "Input", max_tokens=2048) == "account 301"
    assert await second.complete("System", "Input", max_tokens=2048) == "account 302"
    with pytest.raises(ai_client.AIError, match="AI_NOT_CONFIGURED"):
        ai_client.AIClient.for_user(303)

    assert len(captured) == 2
    response_endpoint, response_auth, response_payload = captured[0]
    chat_endpoint, chat_auth, chat_payload = captured[1]
    assert response_endpoint.endswith("/responses")
    assert response_auth == "Bearer fixture-user-301"
    assert response_payload["model"] == "model-301"
    assert response_payload["max_output_tokens"] == 1024
    assert response_payload["reasoning"] == {"effort": "low"}
    assert chat_endpoint.endswith("/chat/completions")
    assert chat_auth == "Bearer fixture-user-302"
    assert chat_payload["model"] == "model-302"
    assert chat_payload["max_tokens"] == 1024
    assert chat_payload["reasoning_effort"] == "low"
