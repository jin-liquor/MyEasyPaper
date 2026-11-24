from __future__ import annotations

import json

from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, create_engine

from app.models import task as task_model  # noqa: F401
from app.models import user as user_model  # noqa: F401
from app.models.task import TaskResult, TaskStatus
from app.services import task_manager as task_manager_module
from app.services.task_manager import TaskManager


def test_set_result_persists_pdf_alignment_and_task_state_with_utf8(monkeypatch, tmp_path):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(task_manager_module, "engine", engine)

    manager = TaskManager()
    manager.config.storage.temp_dir = str(tmp_path)
    task = manager.create_task("paper.pdf", user_id=1)
    records = [{"sentence_id": "p1_s1", "text": "中文翻译内容。"}]

    manager.set_result(
        task.task_id,
        TaskResult(pdf_bytes=b"%PDF-test", translation_records=records),
    )

    updated = manager.get_task(task.task_id)
    assert updated is not None
    assert updated.status == TaskStatus.COMPLETED
    assert updated.result_pdf_path is not None
    result_path = tmp_path / f"{task.task_id}.pdf"
    alignment_path = tmp_path / f"{task.task_id}.alignment.json"
    assert result_path.read_bytes() == b"%PDF-test"
    assert alignment_path.read_text(encoding="utf-8")
    assert json.loads(alignment_path.read_text(encoding="utf-8")) == records
    assert updated.result_pdf_path == str(result_path)
