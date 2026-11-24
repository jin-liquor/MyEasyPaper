from __future__ import annotations

import os

from app.core.config import AppConfig
from app.services.document_processor import DocumentProcessor
from app.services.task_manager import TaskManager


def test_pdf2zh_credentials_are_not_published_to_process_environment(monkeypatch):
    for key in ("OPENAILIKED_API_KEY", "OPENAILIKED_BASE_URL", "OPENAILIKED_MODEL"):
        monkeypatch.delenv(key, raising=False)

    DocumentProcessor(
        config=AppConfig(
            llm={
                "api_key": "fixture-secret",
                "base_url": "https://provider.example/v1",
                "model": "fixture-model",
            }
        ),
        task_manager=TaskManager(),
    )

    assert "OPENAILIKED_API_KEY" not in os.environ
    assert "OPENAILIKED_BASE_URL" not in os.environ
    assert "OPENAILIKED_MODEL" not in os.environ


def test_processor_has_no_global_translation_lock():
    # Regression guard: there must be no module-level lock that serializes
    # translations (the bug that made processing.max_concurrent meaningless).
    import app.services.document_processor as dp

    assert not hasattr(dp, "_PDF2ZH_ENV_LOCK")
