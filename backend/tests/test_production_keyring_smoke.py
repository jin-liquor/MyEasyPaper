import base64
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
RING = "EASYPAPER_CREDENTIAL_ENCRYPTION_KEYS"
ACTIVE = "EASYPAPER_CREDENTIAL_ENCRYPTION_ACTIVE_VERSION"
LEGACY = "EASYPAPER_CREDENTIAL_ENCRYPTION_KEY"


def test_production_compose_keyring_boot_and_offline_dry_run(tmp_path):
    compose = yaml.safe_load((ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    for variable in (RING, ACTIVE, LEGACY):
        assert services["backend"]["environment"][variable] == "${" + variable + ":-}"
        assert all(
            variable not in service.get("environment", {}) for name, service in services.items() if name != "backend"
        )
    prepare = runpy.run_path(str(ROOT / "scripts" / "prepare_server.py"))["prepare"]
    dotenv = prepare(tmp_path, "paper.example.com")
    generated = dict(line.split("=", 1) for line in dotenv.read_text().splitlines() if "=" in line)
    assert generated[ACTIVE] == "v1"
    first_ring = json.loads(generated[RING].strip("'"))
    assert len(base64.urlsafe_b64decode(first_ring["v1"])) == 32
    with pytest.raises(FileExistsError):
        prepare(tmp_path, "paper.example.com")

    config = yaml.safe_load((ROOT / "deploy" / "config.production.yaml").read_text(encoding="utf-8"))
    config["storage"].update(temp_dir=str(tmp_path / "temp"), library_dir=str(tmp_path / "library"))
    config["logging"]["file"] = str(tmp_path / "app.log")
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.safe_dump(config), encoding="utf-8")
    ring = {**first_ring, "v2": base64.urlsafe_b64encode(b"x" * 32).decode()}
    supplied = {RING: json.dumps(ring), ACTIVE: "v2", LEGACY: ""}
    env = {key: value for key, value in os.environ.items() if not key.startswith(("EASYPAPER_", "LLM_", "PYTHONPATH"))}
    env.update(
        {
            **supplied,
            "APP_ENV": services["backend"]["environment"]["APP_ENV"],
            "APP_CONFIG_PATH": str(config_file),
            "DATABASE_URL": f"sqlite:///{(tmp_path / 'db.sqlite').as_posix()}",
            "EASYPAPER_SECRET_KEY": generated["EASYPAPER_SECRET_KEY"],
        }
    )
    smoke = """
import json
from fastapi.testclient import TestClient
from sqlmodel import Session
from app import cli
from app.core.db import engine
from app.main import app, config
from app.models.knowledge import UserSetting
from app.services import credential_store as store
assert not config.llm.is_configured
assert store.load_keyring().active_version == "v2"
with TestClient(app) as client:
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/api/auth/features").json()["ai_available"] is False
    assert client.post("/api/auth/register").status_code == 403
provider_id = "smoke-provider"
key = "ai_provider_secret:" + provider_id
ring = store.load_keyring()
old_ciphertext = store.encrypt_secret(1, provider_id, "smoke-secret", keyring=store.Keyring(ring.keys, "v1"))
with Session(engine) as session:
    session.add(UserSetting(id=key, user_id=1, key=key, value_json=json.dumps({"ciphertext": old_ciphertext})))
    session.commit()
assert cli.main(["rewrap-ai-credentials", "--dry-run"]) == 0
with Session(engine) as session:
    assert json.loads(session.get(UserSetting, key).value_json)["ciphertext"] == old_ciphertext
"""
    run_options = dict(cwd=ROOT / "backend", env=env, capture_output=True, text=True, timeout=40)
    boot = subprocess.run([sys.executable, "-c", smoke], **run_options, check=False)
    assert boot.returncode == 0, (boot.stdout, boot.stderr)
    assert "processed=1 rewrapped=0 would_rewrap=1 skipped=0 failed=0 conflicts=0" in boot.stdout
    assert all(value not in boot.stdout + boot.stderr for value in (*ring.values(), "smoke-secret"))

    run_options["env"] = {**env, ACTIVE: "missing-version"}
    rejected = subprocess.run([sys.executable, "-c", "from app.main import app"], **run_options, check=False)
    assert rejected.returncode != 0
    assert "credential keyring is misconfigured" in rejected.stderr
    assert all(value not in rejected.stdout + rejected.stderr for value in ring.values())
