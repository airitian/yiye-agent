from datetime import datetime
from pathlib import Path

from src.generate import _resolve_retry_context
from src.workspace import WorkspaceManager


def test_retry_reuses_last_successful_stage_artifact(tmp_path: Path):
    manager = WorkspaceManager(tmp_path / "runs")
    workspace = manager.create_task("REQ-001", domain="landing-page", task_id="old-task")
    now = datetime.now()

    for stage in ("stage1", "stage2", "stage3"):
        workspace.save_stage(stage, "success", now, now)
    workspace.save_artifact("stage3", "links.json", [{"chunk_id": "chunk-1"}])

    context = _resolve_retry_context("old-task", tmp_path)

    assert context["last_success_stage"] == "stage3"
    assert context["skipped_stages"] == ["stage1", "stage2", "stage3"]
    assert context["artifact_source_task_id"] == "old-task"
