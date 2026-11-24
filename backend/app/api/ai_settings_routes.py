from __future__ import annotations

import json
from datetime import datetime
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlmodel import Session

from ..core.db import engine
from ..models.knowledge import UserSetting
from ..models.user import User
from ..services.credential_store import CredentialStoreError
from ..services.credential_store import decrypt_secret as _decrypt_credential
from ..services.credential_store import encrypt_secret as _encrypt_credential
from ..services.credential_store import load_keyring as _load_credential_keyring
from ..services.outbound import OutboundError, request_json, validate_base_url
from ..services.reasoning import ReasoningError, reasoning_payload
from .deps import get_current_user


class _SafeValidationRoute(APIRoute):
    def get_route_handler(self):
        handle = super().get_route_handler()

        async def safe_handle(request):
            try:
                return await handle(request)
            except RequestValidationError:
                # Validation errors (including JSON decoding) may retain the whole body.
                return JSONResponse(
                    status_code=422,
                    content={"detail": {"code": "AI_SETTINGS_INVALID", "message": "AI 设置请求格式无效"}},
                )

        return safe_handle


ROUTER = APIRouter(prefix="/api/settings/ai", tags=["ai-settings"], route_class=_SafeValidationRoute)
CONFIG_KEY = "ai_providers"
DEFAULT_KEY = "ai_default_model"
SECRET_PREFIX = "ai_provider_secret:"


class ReasoningLevel(BaseModel):
    label: str = Field(min_length=1, max_length=40)
    api_value: str = Field(min_length=1, max_length=80)


class ReasoningCapability(BaseModel):
    supported: bool = False
    levels: list[ReasoningLevel] = Field(default_factory=list)
    default_level: str | None = None
    parameter: str = "reasoning_effort"

    @model_validator(mode="after")
    def validate_levels(self):
        labels = {item.label for item in self.levels}
        if not self.supported and self.levels:
            raise ValueError("不支持思考能力时 levels 必须为空")
        if self.supported and not self.levels:
            raise ValueError("支持思考能力时必须提供至少一个档位")
        if self.supported and self.default_level is None:
            self.default_level = self.levels[0].label
        if self.default_level is not None and self.default_level not in labels:
            raise ValueError("default_level 必须匹配已配置的思考档位")
        if self.parameter not in {"reasoning_effort", "reasoning.effort"}:
            raise ValueError("思考参数仅支持 reasoning_effort 或 reasoning.effort")
        return self


class ModelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = None
    model_name: str = Field(min_length=1, max_length=200)
    context_window: int = Field(gt=0, le=10_000_000)
    max_output_tokens: int = Field(gt=0, le=1_000_000)
    input_types: list[Literal["text", "image"]] = Field(min_length=1)
    reasoning: ReasoningCapability = Field(default_factory=ReasoningCapability)

    @model_validator(mode="after")
    def validate_output_window(self):
        if self.max_output_tokens > self.context_window:
            raise ValueError("max_output_tokens 不能大于 context_window")
        if len(set(self.input_types)) != len(self.input_types):
            raise ValueError("input_types 不能重复")
        return self


class ProviderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    protocol: Literal["chat_completions", "responses"]
    base_url: str = Field(min_length=1, max_length=2000)
    retry_policy: Literal["temporary_errors"] = "temporary_errors"
    api_key: str = Field(min_length=1, max_length=4000)
    models: list[ModelInput] = Field(default_factory=list)

    @field_validator("api_key")
    @classmethod
    def trim_secret(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("API Key 不能为空")
        return value


class ProviderPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=100)
    protocol: Literal["chat_completions", "responses"] | None = None
    base_url: str | None = Field(default=None, min_length=1, max_length=2000)
    retry_policy: Literal["temporary_errors"] | None = None
    enabled: bool | None = None
    api_key: str | None = Field(default=None, max_length=4000)

    @field_validator("api_key")
    @classmethod
    def reject_empty_secret(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("api_key 不能为空；请使用 secret DELETE 端点显式删除")
        return value.strip() if value is not None else None


class SecretInput(BaseModel):
    api_key: str = Field(min_length=1, max_length=4000)

    @field_validator("api_key")
    @classmethod
    def reject_empty_secret(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("api_key 不能为空")
        return value


class DefaultModelInput(BaseModel):
    model_id: str = Field(min_length=1, max_length=100)


class ProviderTestInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_id: str = Field(min_length=1, max_length=100)


class DiscoverInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    apply: bool = False
    model_ids: list[str] = Field(default_factory=list, max_length=200)


class ModelCreate(ModelInput):
    provider_id: str = Field(min_length=1, max_length=100)


class ModelPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_name: str | None = Field(default=None, min_length=1, max_length=200)
    context_window: int | None = Field(default=None, gt=0, le=10_000_000)
    max_output_tokens: int | None = Field(default=None, gt=0, le=1_000_000)
    input_types: list[Literal["text", "image"]] | None = Field(default=None, min_length=1)
    reasoning: ReasoningCapability | None = None


def _setting_id(user_id: int, key: str) -> str:
    return f"user:{user_id}:{key}"


def _get_json(session: Session, user_id: int, key: str, default):
    row = session.get(UserSetting, _setting_id(user_id, key))
    if not row:
        return default
    try:
        return json.loads(row.value_json)
    except (TypeError, json.JSONDecodeError):
        raise HTTPException(500, "AI 设置数据损坏，请联系管理员") from None


def _put_json(session: Session, user_id: int, key: str, value) -> None:
    setting_id = _setting_id(user_id, key)
    row = session.get(UserSetting, setting_id)
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if row is None:
        row = UserSetting(id=setting_id, user_id=user_id, key=key, value_json=encoded)
    else:
        row.value_json = encoded
        row.updated_at = datetime.utcnow()
    session.add(row)


def _encrypt_secret(user_id: int, provider_id: str, secret: str) -> str:
    try:
        return _encrypt_credential(user_id, provider_id, secret)
    except CredentialStoreError as exc:
        raise HTTPException(status_code=503, detail={"code": exc.code, "message": "服务器凭据加密配置无效"}) from None


def decrypt_secret(user_id: int, provider_id: str, encrypted: str) -> str:
    try:
        return _decrypt_credential(user_id, provider_id, encrypted)
    except CredentialStoreError as exc:
        status_code = 503 if exc.code == "CREDENTIAL_KEY_MISCONFIGURED" else 503
        raise HTTPException(
            status_code=status_code, detail={"code": exc.code, "message": "账户 AI 凭据无法解密"}
        ) from None


def _providers(session: Session, user_id: int) -> list[dict]:
    return _get_json(session, user_id, CONFIG_KEY, [])


def _public(session: Session, user_id: int, providers: list[dict]) -> dict:
    public_providers = []
    for provider in providers:
        public_provider = dict(provider)
        public_provider["models"] = [dict(model) for model in provider.get("models", [])]
        public_provider["secret_configured"] = (
            session.get(UserSetting, _setting_id(user_id, SECRET_PREFIX + provider["id"])) is not None
        )
        public_providers.append(public_provider)
    return {"providers": public_providers, "default_model_id": _get_json(session, user_id, DEFAULT_KEY, None)}


def _provider_or_404(providers: list[dict], provider_id: str) -> dict:
    provider = next((item for item in providers if item["id"] == provider_id), None)
    if not provider:
        raise HTTPException(404, "供应商不存在")
    return provider


def _commit(session: Session, user_id: int, providers: list[dict]) -> dict:
    _put_json(session, user_id, CONFIG_KEY, providers)
    session.commit()
    return _public(session, user_id, providers)


@ROUTER.get("/credential-status")
def read_credential_status(user: User = Depends(get_current_user)) -> dict[str, str | bool]:
    del user
    try:
        _load_credential_keyring()
    except CredentialStoreError:
        return {
            "ready": False,
            "code": "CREDENTIAL_ENCRYPTION_NOT_READY",
            "message": "服务器凭据加密尚未就绪。请联系管理员配置并验证服务器凭据密钥环。",
        }
    return {
        "ready": True,
        "code": "CREDENTIAL_ENCRYPTION_READY",
        "message": "服务器凭据加密已就绪。",
    }


@ROUTER.get("")
def read_ai_settings(user: User = Depends(get_current_user)) -> dict:
    with Session(engine) as session:
        return _public(session, user.id, _providers(session, user.id))


@ROUTER.post("/providers", status_code=status.HTTP_201_CREATED)
def create_provider(body: ProviderCreate, user: User = Depends(get_current_user)) -> dict:
    with Session(engine) as session:
        providers = _providers(session, user.id)
        provider_id = str(uuid4())
        try:
            base_url = validate_base_url(body.base_url)
        except OutboundError as exc:
            raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)}) from None
        provider = {
            "id": provider_id,
            "name": body.name,
            "protocol": body.protocol,
            "base_url": base_url,
            "retry_policy": body.retry_policy,
            "enabled": True,
            "models": [{**model.model_dump(), "id": model.id or str(uuid4())} for model in body.models],
        }
        secret_id = _setting_id(user.id, SECRET_PREFIX + provider_id)
        session.add(
            UserSetting(
                id=secret_id,
                user_id=user.id,
                key=SECRET_PREFIX + provider_id,
                value_json=json.dumps({"ciphertext": _encrypt_secret(user.id, provider_id, body.api_key)}),
            )
        )
        providers.append(provider)
        result = _commit(session, user.id, providers)
        return result


@ROUTER.patch("/providers/{provider_id}")
def patch_provider(provider_id: str, body: ProviderPatch, user: User = Depends(get_current_user)) -> dict:
    with Session(engine) as session:
        providers = _providers(session, user.id)
        provider = _provider_or_404(providers, provider_id)
        updates = body.model_dump(exclude_unset=True)
        secret = updates.pop("api_key", None)
        for key, value in updates.items():
            if value is not None:
                if key == "base_url":
                    try:
                        value = validate_base_url(value)
                    except OutboundError as exc:
                        raise HTTPException(
                            status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)}
                        ) from None
                provider[key] = value.rstrip("/") if key == "base_url" else value
        if secret is not None:
            row_id = _setting_id(user.id, SECRET_PREFIX + provider_id)
            row = session.get(UserSetting, row_id)
            value_json = json.dumps({"ciphertext": _encrypt_secret(user.id, provider_id, secret)})
            if row:
                row.value_json = value_json
                row.updated_at = datetime.utcnow()
            else:
                row = UserSetting(id=row_id, user_id=user.id, key=SECRET_PREFIX + provider_id, value_json=value_json)
            session.add(row)
        return _commit(session, user.id, providers)


@ROUTER.delete("/providers/{provider_id}")
def delete_provider(provider_id: str, user: User = Depends(get_current_user)) -> dict:
    with Session(engine) as session:
        providers = _providers(session, user.id)
        provider = _provider_or_404(providers, provider_id)
        default_model_id = _get_json(session, user.id, DEFAULT_KEY, None)
        if any(model["id"] == default_model_id for model in provider["models"]):
            raise HTTPException(409, detail={"code": "DEFAULT_MODEL_IN_USE", "message": "请先更换默认模型"})
        providers.remove(provider)
        row = session.get(UserSetting, _setting_id(user.id, SECRET_PREFIX + provider_id))
        if row:
            session.delete(row)
        return _commit(session, user.id, providers)


@ROUTER.post("/providers/{provider_id}/secret")
def rotate_provider_secret(provider_id: str, body: SecretInput, user: User = Depends(get_current_user)) -> dict:
    with Session(engine) as session:
        _provider_or_404(_providers(session, user.id), provider_id)
        row_id = _setting_id(user.id, SECRET_PREFIX + provider_id)
        row = session.get(UserSetting, row_id)
        encrypted = json.dumps({"ciphertext": _encrypt_secret(user.id, provider_id, body.api_key)})
        if row:
            row.value_json = encrypted
            row.updated_at = datetime.utcnow()
        else:
            row = UserSetting(id=row_id, user_id=user.id, key=SECRET_PREFIX + provider_id, value_json=encrypted)
        session.add(row)
        session.commit()
        return {"secret_configured": True}


@ROUTER.delete("/providers/{provider_id}/secret")
def delete_provider_secret(provider_id: str, user: User = Depends(get_current_user)) -> dict:
    with Session(engine) as session:
        _provider_or_404(_providers(session, user.id), provider_id)
        row = session.get(UserSetting, _setting_id(user.id, SECRET_PREFIX + provider_id))
        if row:
            session.delete(row)
            session.commit()
        return {"secret_configured": False}


def _safe_upstream_error(exc: OutboundError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": str(exc), "retryable": exc.retryable},
    )


def _provider_secret(session: Session, user_id: int, provider_id: str) -> str:
    row = session.get(UserSetting, _setting_id(user_id, SECRET_PREFIX + provider_id))
    if row is None:
        raise HTTPException(
            status_code=503, detail={"code": "AI_NOT_CONFIGURED", "message": "当前供应商尚未配置 API Key"}
        )
    try:
        value = json.loads(row.value_json)
        encrypted = value["ciphertext"]
    except (TypeError, ValueError, KeyError):
        raise HTTPException(
            status_code=503, detail={"code": "CREDENTIAL_DECRYPTION_FAILED", "message": "账户 AI 凭据无法解密"}
        ) from None
    return decrypt_secret(user_id, provider_id, encrypted)


def _find_model(provider: dict, model_id: str) -> dict:
    model = next((item for item in provider.get("models", []) if item["id"] == model_id), None)
    if model is None:
        raise HTTPException(status_code=404, detail={"code": "MODEL_NOT_FOUND", "message": "模型不存在"})
    return model


@ROUTER.post("/providers/{provider_id}/test")
def test_provider(body: ProviderTestInput, provider_id: str, user: User = Depends(get_current_user)) -> dict:
    with Session(engine) as session:
        provider = _provider_or_404(_providers(session, user.id), provider_id)
        if not provider.get("enabled", True):
            raise HTTPException(status_code=422, detail={"code": "AI_NOT_CONFIGURED", "message": "供应商已停用"})
        model = _find_model(provider, body.model_id)
        api_key = _provider_secret(session, user.id, provider_id)
    budget = min(16, int(model["max_output_tokens"]))
    protocol = provider.get("protocol")
    if protocol == "responses":
        payload = {
            "model": model["model_name"],
            "max_output_tokens": budget,
            "input": [{"role": "user", "content": [{"type": "input_text", "text": "Reply with OK."}]}],
        }
        endpoint = provider["base_url"].rstrip("/") + "/responses"
    elif protocol == "chat_completions":
        payload = {
            "model": model["model_name"],
            "max_tokens": budget,
            "messages": [{"role": "user", "content": "Reply with OK."}],
        }
        endpoint = provider["base_url"].rstrip("/") + "/chat/completions"
    else:
        raise HTTPException(status_code=422, detail={"code": "PROTOCOL_UNSUPPORTED", "message": "不支持的供应商协议"})
    try:
        payload.update(reasoning_payload(protocol, model))
    except ReasoningError:
        raise HTTPException(422, detail={"code": "AI_SETTINGS_INVALID", "message": "模型思考能力配置无效"}) from None
    try:
        _, latency_ms = __import__("asyncio").run(
            request_json("POST", endpoint, headers={"Authorization": f"Bearer {api_key}"}, json=payload)
        )
    except OutboundError as exc:
        raise _safe_upstream_error(exc) from None
    return {
        "ok": True,
        "provider_id": provider_id,
        "model_id": body.model_id,
        "protocol": protocol,
        "latency_ms": latency_ms,
    }


@ROUTER.post("/providers/{provider_id}/models/discover")
def discover_models(body: DiscoverInput, provider_id: str, user: User = Depends(get_current_user)) -> dict:
    with Session(engine) as session:
        providers = _providers(session, user.id)
        provider = _provider_or_404(providers, provider_id)
        api_key = _provider_secret(session, user.id, provider_id)
    try:
        data, _ = __import__("asyncio").run(
            request_json(
                "GET",
                provider["base_url"].rstrip("/") + "/models",
                headers={"Authorization": f"Bearer {api_key}"},
                json={},
            )
        )
    except OutboundError as exc:
        raise _safe_upstream_error(exc) from None
    raw_candidates = data.get("data")
    if not isinstance(raw_candidates, list):
        raise HTTPException(
            status_code=422, detail={"code": "MODEL_DISCOVERY_UNSUPPORTED", "message": "供应商不支持模型目录"}
        )
    configured = {model["id"] for model in provider.get("models", [])}
    requested = set(body.model_ids)
    candidates = []
    for item in raw_candidates:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"].strip():
            continue
        model_id = item["id"].strip()
        candidates.append({"id": model_id, "model_name": model_id, "needs_manual_capabilities": True})
    selected = {item["id"] for item in candidates} & requested if body.apply else set()
    added: list[str] = []
    skipped: list[str] = []
    if body.apply and selected:
        with Session(engine) as session:
            providers = _providers(session, user.id)
            provider = _provider_or_404(providers, provider_id)
            configured = {model["id"] for model in provider.get("models", [])}
            for model_id in selected:
                if model_id in configured:
                    skipped.append(model_id)
                    continue
                provider.setdefault("models", []).append(
                    {
                        "id": model_id,
                        "model_name": model_id,
                        "context_window": 1,
                        "max_output_tokens": 1,
                        "input_types": ["text"],
                        "reasoning": {"supported": False, "levels": []},
                        "needs_manual_capabilities": True,
                    }
                )
                added.append(model_id)
            _commit(session, user.id, providers)
    warnings = ["目录只提供模型 ID；请手动补齐能力后再设为默认模型。"] if candidates else []
    return {"candidates": candidates, "added": added, "skipped": skipped, "warnings": warnings}


@ROUTER.post("/models", status_code=status.HTTP_201_CREATED)
def add_model(body: ModelCreate, user: User = Depends(get_current_user)) -> dict:
    provider_id = body.provider_id
    with Session(engine) as session:
        providers = _providers(session, user.id)
        provider = _provider_or_404(providers, provider_id)
        if not provider.get("enabled", True):
            raise HTTPException(status_code=422, detail={"code": "AI_NOT_CONFIGURED", "message": "供应商已停用"})
        model = {**body.model_dump(exclude={"id", "provider_id"}), "id": body.id or str(uuid4())}
        if any(existing["id"] == model["id"] for item in providers for existing in item["models"]):
            raise HTTPException(409, "模型 ID 已存在")
        provider["models"].append(model)
        _commit(session, user.id, providers)
        return model


@ROUTER.patch("/models/{model_id}")
def patch_model(model_id: str, body: ModelPatch, user: User = Depends(get_current_user)) -> dict:
    with Session(engine) as session:
        providers = _providers(session, user.id)
        model = next((m for p in providers for m in p["models"] if m["id"] == model_id), None)
        if not model:
            raise HTTPException(404, "模型不存在")
        model.update(body.model_dump(exclude_unset=True, exclude_none=True))
        try:
            ModelInput.model_validate(model)
        except Exception as exc:
            raise HTTPException(422, "模型能力配置无效") from exc
        _commit(session, user.id, providers)
        return model


@ROUTER.delete("/models/{model_id}")
def delete_model(model_id: str, user: User = Depends(get_current_user)) -> dict:
    with Session(engine) as session:
        providers = _providers(session, user.id)
        found = next(((p, m) for p in providers for m in p["models"] if m["id"] == model_id), None)
        if not found:
            raise HTTPException(404, "模型不存在")
        if _get_json(session, user.id, DEFAULT_KEY, None) == model_id:
            raise HTTPException(409, detail={"code": "DEFAULT_MODEL_IN_USE", "message": "请先更换默认模型"})
        found[0]["models"].remove(found[1])
        return _commit(session, user.id, providers)


@ROUTER.patch("/default-model")
def set_default_model(body: DefaultModelInput, user: User = Depends(get_current_user)) -> dict:
    with Session(engine) as session:
        providers = _providers(session, user.id)
        target = next(
            (
                (p, m)
                for p in providers
                for m in p["models"]
                if p.get("enabled", True) and m["id"] == body.model_id and not m.get("needs_manual_capabilities")
            ),
            None,
        )
        if target is None:
            raise HTTPException(
                status_code=422,
                detail={"code": "INVALID_DEFAULT_MODEL", "message": "默认模型必须属于已启用供应商且已补齐能力"},
            )
        _provider_secret(session, user.id, target[0]["id"])
        _put_json(session, user.id, DEFAULT_KEY, body.model_id)
        session.commit()
        return {"default_model_id": body.model_id}


def account_llm_config(user_id: int, session: Session | None = None) -> dict | None:
    """Resolve only this user's explicitly selected provider, without an upstream request."""
    if session is None:
        with Session(engine) as owned_session:
            return account_llm_config(user_id, owned_session)
    model_id = _get_json(session, user_id, DEFAULT_KEY, None)
    if not model_id:
        return None
    for provider in _providers(session, user_id):
        if not provider.get("enabled", True):
            continue
        for model in provider.get("models", []):
            if model["id"] == model_id and not model.get("needs_manual_capabilities"):
                api_key = _provider_secret(session, user_id, provider["id"])
                if api_key.strip() and api_key != "YOUR_API_KEY":
                    return {"provider": provider, "model": model, "api_key": api_key}
    return None


def require_account_llm(user_id: int, session: Session) -> None:
    if account_llm_config(user_id, session) is None:
        raise HTTPException(
            503, detail={"code": "AI_NOT_CONFIGURED", "message": "当前账户尚未配置可用的默认 AI 模型。"}
        )
