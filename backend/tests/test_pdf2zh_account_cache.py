from types import SimpleNamespace

from app.core.config import LLMConfig
from app.services.ai_client import AIClient
from app.services.pdf2zh_codex import _CONTEXT_KEY, pdf2zh_backend


def test_account_pdf_cache_never_reuses_global_or_other_account_provider_entries(tmp_path):
    from pdf2zh import converter
    from pdf2zh.cache import TranslationCache, _TranslationCache
    from peewee import SqliteDatabase

    cache_db = SqliteDatabase(str(tmp_path / "translations.sqlite"))
    calls = []

    def translate(user_id, provider_id, answer, *, key="fixture-api-key"):
        model = dict(id="model-id", model_name="shared-model", max_output_tokens=1024)
        provider = dict(id=provider_id, protocol="responses", base_url="https://provider.invalid/v1")
        ai = AIClient(
            LLMConfig(api_key=key, model="shared-model"),
            runtime={
                "user_id": user_id,
                "provider": provider,
                "model": model,
            },
        )
        with pdf2zh_backend(ai.config, ai=ai) as backend:
            executor = backend["envs"][_CONTEXT_KEY]
            assert executor.ai.runtime == ai.runtime
            assert executor.ai.config.api_key == key

            def fake_translation(prompt):
                calls.append((user_id, provider_id))
                return answer

            backend["envs"][_CONTEXT_KEY] = SimpleNamespace(ai=executor.ai, translate=fake_translation)
            translator = converter.OpenAIlikedTranslator("en", "zh", ai.model, envs=backend["envs"])
            return translator.translate("same source paragraph")

    with cache_db.bind_ctx([_TranslationCache]), cache_db:
        cache_db.create_tables([_TranslationCache])
        params = {"lang_in": "en", "lang_out": "zh", "model": "shared-model"}
        TranslationCache("openailiked", params).set("same source paragraph", "legacy global")
        TranslationCache("easypaper-codex", {**params, "prompt": "translation-v1", "reasoning_effort": "low"}).set(
            "same source paragraph",
            "legacy Codex",
        )
        assert translate(1, "p1", "first account") == "first account"
        assert translate(2, "p1", "second account") == "second account"
        assert translate(1, "p2", "second provider") == "second provider"
        assert translate(1, "p1", "unexpected miss", key="fixture-rotated-key") == "first account"
        assert calls == [(1, "p1"), (2, "p1"), (1, "p2")]
        rows = list(_TranslationCache.select().where(_TranslationCache.translate_engine == "easypaper-acct-v1"))
        assert len(rows) == 3
        assert all("fixture-" not in row.translate_engine_params for row in rows)
