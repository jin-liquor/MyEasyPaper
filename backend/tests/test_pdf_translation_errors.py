from __future__ import annotations

import asyncio
import concurrent.futures
from types import SimpleNamespace

import pytest

from app.core.config import AppConfig, LLMConfig
from app.services.ai_client import AIError
from app.services.document_processor import DocumentProcessingError, DocumentProcessor, TranslationAborted
from app.services.pdf2zh_codex import CodexTranslationExecutor, PDFTranslationAbort


def _failed_future(error: BaseException) -> concurrent.futures.Future:
    future = concurrent.futures.Future()
    future.set_exception(error)
    return future


def test_pdf_translation_failure_uses_generic_account_provider_message(monkeypatch):
    config = SimpleNamespace(provider="api", request_timeout=1)
    ai = SimpleNamespace(config=config, complete=lambda *_args: object())
    monkeypatch.setattr(
        "app.services.pdf2zh_codex.asyncio.run_coroutine_threadsafe",
        lambda *_args: _failed_future(RuntimeError("provider internals")),
    )

    with pytest.raises(PDFTranslationAbort) as caught:
        CodexTranslationExecutor(ai, loop=object()).translate("translate this")

    message = str(caught.value)
    assert message.startswith("AI_PDF_TRANSLATION_FAILED：")
    assert "当前账户 AI 供应商翻译失败" in message
    assert "请检查模型配置或稍后重试" in message
    assert "Codex" not in message
    assert "Cursor" not in message


def test_pdf_translation_preserves_ai_error_code_and_guidance(monkeypatch):
    config = SimpleNamespace(provider="api", request_timeout=1)
    ai = SimpleNamespace(config=config, complete=lambda *_args: object())
    error = AIError("AI_UPSTREAM_UNAVAILABLE：AI 服务暂时不可用，请稍后重试。", retryable=True)
    monkeypatch.setattr(
        "app.services.pdf2zh_codex.asyncio.run_coroutine_threadsafe",
        lambda *_args: _failed_future(error),
    )

    with pytest.raises(PDFTranslationAbort) as caught:
        CodexTranslationExecutor(ai, loop=object()).translate("translate this")

    assert str(caught.value) == str(error)


def test_document_processor_exposes_safe_pdf_translation_error(monkeypatch):
    class TaskManagerStub:
        error = None

        def update_progress(self, *_args, **_kwargs):
            pass

        def set_error(self, _task_id, error):
            self.error = error

    manager = TaskManagerStub()
    processor = DocumentProcessor(AppConfig(llm=LLMConfig()), manager, ai_client=object())
    monkeypatch.setattr(DocumentProcessor, "_ai_for_scope", staticmethod(lambda _user_id, fallback: fallback))

    def abort_translation(*_args, **_kwargs):
        raise TranslationAborted("AI_PDF_TRANSLATION_FAILED：当前账户 AI 供应商翻译失败，请检查模型配置或稍后重试。")

    monkeypatch.setattr(processor, "_translate_with_pdf2zh", abort_translation)
    asyncio.run(processor.process("task-1", b"", "paper.pdf", user_id=1))

    assert manager.error == ("AI_PDF_TRANSLATION_FAILED：当前账户 AI 供应商翻译失败，请检查模型配置或稍后重试。")


def test_document_processor_rejects_pdf_without_account_context(monkeypatch):
    def unexpected_for_user(_user_id):
        raise AssertionError("account lookup must not run without user context")

    monkeypatch.setattr("app.services.document_processor.AIClient.for_user", unexpected_for_user)

    with pytest.raises(DocumentProcessingError, match="缺少账户上下文"):
        DocumentProcessor._ai_for_scope(None, fallback=object())
