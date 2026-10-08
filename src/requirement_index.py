"""需求文档知识库：JSON 分块 + SQLite FTS5。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List


DEFAULT_MAX_CHARS = 2000


def _split_text(text: str, max_chars: int) -> List[str]:
    if len(text) <= max_chars:
        return [text]
    chunks = []
    for start in range(0, len(text), max_chars):
        chunks.append(text[start:start + max_chars])
    return chunks


def chunk_requirement_file(path: Path, max_chars: int = DEFAULT_MAX_CHARS) -> List[Dict[str, Any]]:
    """把一个需求 JSON 按字段分块，保留来源和需求元数据。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        data = {"content": data}
    req_id = str(data.get("req_id") or path.stem)
    title = str(data.get("title") or "")
    domain = str(data.get("domain") or "")
    result: List[Dict[str, Any]] = []
    for field, value in data.items():
        if field in {"req_id", "title", "domain"}:
            continue
        rendered = json.dumps(value, ensure_ascii=False, indent=2)
        text = f"需求: {req_id}\n标题: {title}\n业务域: {domain}\n字段: {field}\n内容:\n{rendered}"
        for part_index, part in enumerate(_split_text(text, max_chars)):
            raw_id = f"{path.resolve()}|{field}|{part_index}|{part}"
            result.append({
                "chunk_id": hashlib.sha256(raw_id.encode("utf-8")).hexdigest(),
                "req_id": req_id,
                "title": title,
                "domain": domain,
                "source_path": str(path),
                "section": field,
                "part_index": part_index,
                "content": part,
            })
    if not result:
        text = f"需求: {req_id}\n标题: {title}\n业务域: {domain}"
        result.append({
            "chunk_id": hashlib.sha256(f"{path.resolve()}|empty".encode()).hexdigest(),
            "req_id": req_id,
            "title": title,
            "domain": domain,
            "source_path": str(path),
            "section": "metadata",
            "part_index": 0,
            "content": text,
        })
    return result


def build_requirement_index(
    requirements_dir: Path,
    db_path: Path,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> Dict[str, int]:
    """重建需求 FTS5 索引，不调用 LLM、不产生大上下文请求。"""
    files = sorted(Path(requirements_dir).glob("*.json"))
    all_chunks: List[Dict[str, Any]] = []
    parse_errors = 0
    for path in files:
        try:
            all_chunks.extend(chunk_requirement_file(path, max_chars=max_chars))
        except (OSError, json.JSONDecodeError, UnicodeError):
            parse_errors += 1

    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
        DROP TABLE IF EXISTS requirement_chunks;
        DROP TABLE IF EXISTS requirement_fts;
        CREATE TABLE requirement_chunks (
            chunk_id TEXT PRIMARY KEY,
            req_id TEXT NOT NULL,
            title TEXT,
            domain TEXT,
            source_path TEXT NOT NULL,
            section TEXT,
            part_index INTEGER NOT NULL,
            content TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE requirement_fts USING fts5(
            chunk_id UNINDEXED,
            req_id UNINDEXED,
            source_path UNINDEXED,
            section UNINDEXED,
            content,
            tokenize = 'unicode61 remove_diacritics 2'
        );
        """)
        conn.executemany(
            "INSERT INTO requirement_chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(
                c["chunk_id"], c["req_id"], c["title"], c["domain"],
                c["source_path"], c["section"], c["part_index"], c["content"],
            ) for c in all_chunks],
        )
        conn.executemany(
            "INSERT INTO requirement_fts(chunk_id, req_id, source_path, section, content) VALUES (?, ?, ?, ?, ?)",
            [(c["chunk_id"], c["req_id"], c["source_path"], c["section"], c["content"]) for c in all_chunks],
        )
        conn.commit()
    return {
        "files_scanned": len(files),
        "chunks_created": len(all_chunks),
        "parse_errors": parse_errors,
    }
