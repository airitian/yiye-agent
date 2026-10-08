import json
from pathlib import Path

from src.requirement_index import chunk_requirement_file, build_requirement_index


def test_chunk_requirement_file_preserves_requirement_metadata(tmp_path: Path):
    path = tmp_path / "REQ-001.json"
    path.write_text(
        json.dumps(
            {
                "req_id": "REQ-001",
                "title": "创建落地页",
                "domain": "landing-page",
                "main_flow": ["填写表单", "提交发布"],
                "acceptance": ["发布后可访问"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    chunks = chunk_requirement_file(path, max_chars=100)

    assert chunks
    assert all(c["req_id"] == "REQ-001" for c in chunks)
    assert any("创建落地页" in c["content"] for c in chunks)
    assert any("发布后可访问" in c["content"] for c in chunks)


def test_build_requirement_index_writes_fts_searchable_chunks(tmp_path: Path):
    requirements = tmp_path / "requirements"
    requirements.mkdir()
    (requirements / "REQ-001.json").write_text(
        json.dumps({"req_id": "REQ-001", "title": "落地页发布"}, ensure_ascii=False),
        encoding="utf-8",
    )
    db_path = tmp_path / "requirements.db"

    stats = build_requirement_index(requirements, db_path)

    assert stats["files_scanned"] == 1
    assert stats["chunks_created"] >= 1
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT content FROM requirement_fts WHERE requirement_fts MATCH ?",
            ("\"落地页发布\"",),
        ).fetchone()
    assert row and "落地页发布" in row[0]
