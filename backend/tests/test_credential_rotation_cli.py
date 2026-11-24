import base64
import json

from sqlmodel import Session

from app import cli
from app.core import db
from app.models.knowledge import UserSetting
from app.services import credential_store as store


def _rings(monkeypatch):
    keys = {"v1": b"o" * 32, "v2": b"n" * 32}
    monkeypatch.setenv(
        "EASYPAPER_CREDENTIAL_ENCRYPTION_KEYS",
        json.dumps({version: base64.urlsafe_b64encode(key).decode() for version, key in keys.items()}),
    )
    monkeypatch.setenv("EASYPAPER_CREDENTIAL_ENCRYPTION_ACTIVE_VERSION", "v2")
    return store.Keyring(keys, "v1"), store.Keyring(keys, "v2")


def _row(row_id, encrypted, key="ai_provider_secret:provider"):
    return UserSetting(id=row_id, user_id=7, key=key, value_json=json.dumps({"ciphertext": encrypted}))


def test_cli_dry_run_and_rotation_scan_past_current_and_invalid_rows(engine, monkeypatch, capsys):
    old, active = _rings(monkeypatch)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "init_db", lambda: (_ for _ in ()).throw(AssertionError("must not migrate")))
    previous = store.encrypt_secret(7, "provider", "mock-provider-secret", keyring=old)
    current = store.encrypt_secret(7, "provider", "current-fixture", keyring=active)
    with Session(engine) as session:
        for row in (
            _row("01", current),
            _row("02", "broken"),
            _row("03", previous),
            _row("04", "unrelated", key="aiXproviderXsecret:provider"),
        ):
            session.add(row)
        session.commit()

    args = ["rewrap-ai-credentials", "--batch-size", "1"]
    assert cli.main([*args, "--dry-run"]) == 1
    stdout, stderr = capsys.readouterr()
    assert "processed=3 rewrapped=0 would_rewrap=1 skipped=1 failed=1 conflicts=0" in stdout
    assert not stderr
    with Session(engine) as session:
        assert json.loads(session.get(UserSetting, "03").value_json)["ciphertext"] == previous

    assert cli.main(args) == 1
    stdout, stderr = capsys.readouterr()
    assert "processed=3 rewrapped=1 would_rewrap=0 skipped=1 failed=1 conflicts=0" in stdout
    assert not stderr
    assert all(value not in stdout for value in ("mock-provider-secret", "provider", previous))
    with Session(engine) as session:
        rotated = json.loads(session.get(UserSetting, "03").value_json)["ciphertext"]
        assert store.decrypt_secret(7, "provider", rotated, keyring=store.Keyring({"v2": active.keys["v2"]}, "v2")) == (
            "mock-provider-secret"
        )
        session.delete(session.get(UserSetting, "02"))
        session.commit()
    assert cli.main(args) == 0
    assert "processed=2 rewrapped=0 would_rewrap=0 skipped=2 failed=0 conflicts=0" in capsys.readouterr().out


def test_rotation_does_not_overwrite_concurrent_credential_change(engine, monkeypatch):
    old, active = _rings(monkeypatch)
    stale = store.encrypt_secret(7, "provider", "stale", keyring=old)
    replacement = store.encrypt_secret(7, "provider", "replacement", keyring=active)
    with Session(engine) as session:
        session.add(_row("one", stale))
        session.commit()

    original_rewrap = store.rewrap_secret

    def concurrent_write(*args, **kwargs):
        with Session(engine) as writer:
            row = writer.get(UserSetting, "one")
            row.value_json = _row("one", replacement).value_json
            writer.commit()
        return original_rewrap(*args, **kwargs)

    monkeypatch.setattr(store, "rewrap_secret", concurrent_write)
    with Session(engine) as session:
        counts = store.rewrap_user_settings(session, UserSetting)
        assert counts["conflicts"] == 1
        assert counts["rewrapped"] == 0
        assert json.loads(session.get(UserSetting, "one").value_json)["ciphertext"] == replacement
