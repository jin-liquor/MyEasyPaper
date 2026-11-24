from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.core.config import LLMConfig
from app.services.ai_client import AIClient, AIError, cursor_environment


def _fake_codex(tmp_path: Path, body: str) -> str:
    script = tmp_path / "fake-codex.py"
    script.write_text(
        "#!/usr/bin/env python3\nimport json, os, sys\n"
        "args=sys.argv\n"
        "out=args[args.index('--output-last-message')+1]\n"
        "sys.stdin.read()\n" + body + "\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    if os.name == "nt":
        wrapper = tmp_path / "fake-codex.cmd"
        wrapper.write_text(f'@echo off\n"{sys.executable}" "{script}" %*\n', encoding="utf-8")
        return str(wrapper)
    return str(script)


def _config(executable: str) -> LLMConfig:
    return LLMConfig(provider="codex", codex={"executable": executable, "timeout_seconds": 10})


@pytest.mark.asyncio
async def test_codex_client_returns_structured_text_without_api_credentials(tmp_path, monkeypatch):
    executable = _fake_codex(
        tmp_path,
        """
print(json.dumps({'type':'thread.started','thread_id':'fixture'}))
print(json.dumps({'type':'turn.started'}))
assert '--ignore-user-config' in args and '--ignore-rules' in args and '--ephemeral' in args
assert args[args.index('--sandbox')+1] == 'read-only'
overrides = dict(args[i+1].split('=', 1) for i, arg in enumerate(args[:-1]) if arg == '--config')
for feature in ['shell_tool', 'unified_exec', 'code_mode', 'multi_agent', 'hooks', 'plugins', 'apps']:
    assert overrides['features.'+feature] == 'false'
leaked = any(os.environ.get(key) for key in ['OPENAI_API_KEY', 'EASYPAPER_SECRET_KEY', 'DATABASE_URL'])
open(out, 'w').write(json.dumps({'text': json.dumps({'answer':'ok', 'credential_leaked': leaked})}))
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'fixture'}}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'cached_input_tokens':0,'output_tokens':1,'reasoning_output_tokens':0}}))
""",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-inherited")
    monkeypatch.setenv("EASYPAPER_SECRET_KEY", "must-not-be-inherited")
    monkeypatch.setenv("DATABASE_URL", "must-not-be-inherited")
    result = await AIClient(_config(executable)).complete_json("Return JSON.", "hello")
    assert result == {"answer": "ok", "credential_leaked": False}


def _fake_cursor(tmp_path: Path, body: str) -> str:
    script = tmp_path / "fake-cursor.py"
    script.write_text("#!/usr/bin/env python3\nimport os, sys\n" + body + "\n", encoding="utf-8")
    script.chmod(0o700)
    if os.name == "nt":
        wrapper = tmp_path / "fake-cursor.cmd"
        wrapper.write_text(f'@echo off\n"{sys.executable}" "{script}"\n', encoding="utf-8")
        return str(wrapper)
    return str(script)


def _cursor_config(executable: str) -> LLMConfig:
    return LLMConfig(
        provider="cursor", cursor={"executable": executable, "timeout_seconds": 10, "model": "fixture-model"}
    )


@pytest.mark.asyncio
async def test_api_client_passes_selected_model_and_timeout(monkeypatch):
    config = LLMConfig(provider="api", api_key="test-key", model="default-model")
    client = AIClient(config)
    captured = {}

    async def fake_api(system, user, image_bytes, max_tokens, json_mode, model, timeout_seconds):
        captured.update(model=model, timeout_seconds=timeout_seconds, max_tokens=max_tokens, json_mode=json_mode)
        return "ok"

    monkeypatch.setattr(client, "_api", fake_api)
    result = await client.complete(
        "system", "user", max_tokens=123, json_mode=True, model="selected-model", timeout_seconds=7
    )
    assert result == "ok"
    assert captured == {"model": "selected-model", "timeout_seconds": 7, "max_tokens": 123, "json_mode": True}


def test_cursor_environment_sets_user_when_missing(monkeypatch):
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.delenv("LOGNAME", raising=False)
    env = cursor_environment()
    assert env.get("USER")


@pytest.mark.asyncio
async def test_cursor_client_returns_text_without_application_credentials(tmp_path, monkeypatch):
    executable = _fake_cursor(
        tmp_path,
        """
leaked = any(os.environ.get(key) for key in ['OPENAI_API_KEY', 'EASYPAPER_SECRET_KEY', 'DATABASE_URL', 'CURSOR_API_KEY'])
print('{"answer":"ok","credential_leaked":%s}' % ('true' if leaked else 'false'))
""",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-inherited")
    monkeypatch.setenv("EASYPAPER_SECRET_KEY", "must-not-be-inherited")
    monkeypatch.setenv("DATABASE_URL", "must-not-be-inherited")
    monkeypatch.setenv("CURSOR_API_KEY", "must-not-be-inherited")
    result = await AIClient(_cursor_config(executable)).complete_json("Return JSON.", "hello")
    assert result == {"answer": "ok", "credential_leaked": False}


@pytest.mark.asyncio
async def test_cursor_client_reports_auth_failure_without_stderr(tmp_path):
    executable = _fake_cursor(
        tmp_path,
        """
print('401 secret-prompt-echo', file=sys.stderr)
sys.exit(1)
""",
    )
    with pytest.raises(AIError, match="登录失效") as caught:
        await AIClient(_cursor_config(executable)).complete("Return text.", "hello")
    assert "secret-prompt-echo" not in str(caught.value)


@pytest.mark.asyncio
async def test_codex_client_rejects_tool_events(tmp_path):
    executable = _fake_codex(
        tmp_path,
        """
print(json.dumps({'type':'item.started','item':{'type':'command_execution','command':'echo'}}))
""",
    )
    with pytest.raises(AIError, match="尝试执行阅读分析以外的操作"):
        await AIClient(_config(executable)).complete("system", "user")


@pytest.mark.asyncio
async def test_account_chat_runtime_maps_limits_and_reasoning(monkeypatch):
    request = AsyncMock(return_value=({"choices": [{"message": {"content": "ok"}}]}, 0.0))
    monkeypatch.setattr("app.services.ai_client.request_json", request)
    client = AIClient(
        LLMConfig(provider="api", api_key="account-secret", base_url="https://provider.test/v1"),
        runtime={
            "provider": {"protocol": "chat_completions"},
            "model": {
                "model_name": "account-model",
                "context_window": 1000,
                "max_output_tokens": 128,
                "input_types": ["text"],
                "reasoning": {
                    "supported": True,
                    "levels": [{"label": "low", "api_value": "low"}],
                    "parameter": "reasoning_effort",
                },
            },
        },
    )
    assert await client.complete("system", "user", max_tokens=999) == "ok"
    request.assert_awaited_once()
    assert request.await_args.args == ("POST", "https://provider.test/v1/chat/completions")
    assert request.await_args.kwargs["headers"]["Authorization"] == "Bearer account-secret"
    payload = request.await_args.kwargs["json"]
    assert payload["model"] == "account-model"
    assert payload["max_tokens"] == 128
    assert payload["reasoning_effort"] == "low"


@pytest.mark.asyncio
async def test_account_responses_runtime_maps_output_and_reasoning(monkeypatch):
    request = AsyncMock(return_value=({"output_text": "ok"}, 0.0))
    monkeypatch.setattr("app.services.ai_client.request_json", request)
    client = AIClient(
        LLMConfig(provider="api", api_key="account-secret", base_url="https://provider.test/v1"),
        runtime={
            "provider": {"protocol": "responses"},
            "model": {
                "model_name": "response-model",
                "context_window": 1000,
                "max_output_tokens": 128,
                "input_types": ["text"],
                "reasoning": {
                    "supported": True,
                    "levels": [{"label": "high", "api_value": "high"}],
                    "parameter": "reasoning.effort",
                },
            },
        },
    )
    assert await client.complete("system", "user", max_tokens=999, reasoning_level="high") == "ok"
    request.assert_awaited_once()
    assert request.await_args.args == ("POST", "https://provider.test/v1/responses")
    payload = request.await_args.kwargs["json"]
    assert payload["max_output_tokens"] == 128
    assert payload["reasoning"] == {"effort": "high"}
    assert payload["input"][0]["role"] == "system"


@pytest.mark.asyncio
async def test_account_runtime_rejects_unsupported_image_reasoning_and_context(monkeypatch):
    client = AIClient(
        LLMConfig(provider="api", api_key="account-secret"),
        runtime={
            "provider": {"protocol": "chat_completions"},
            "model": {
                "model_name": "text-model",
                "context_window": 100,
                "max_output_tokens": 50,
                "input_types": ["text"],
                "reasoning": {"supported": False, "levels": []},
            },
        },
    )
    with pytest.raises(AIError, match="图片输入"):
        await client.complete("system", "user", image_bytes=b"png")
    with pytest.raises(AIError, match="思考强度"):
        await client.complete("system", "user", reasoning_level="low")
    with pytest.raises(AIError, match="上下文窗口"):
        await client.complete("system", "user", max_tokens=50, context_tokens=51)
