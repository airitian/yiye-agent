"""
Chunk renderer - 把召回的 chunk metadata + code_content 渲染成 LLM 可读的文本块.

RAG fix (反幻觉 P0):
之前 stage3 / stage4 / stage5 (单条 + 批量) 4 个调用点各自拼 chunks 文本,
且全都不含 code_content - 召回的代码"看不到"给 LLM, 反幻觉 prompt 形同虚设.

本模块提供 build_chunks_text 公共函数, 4 个 stage 共用:
- 自动 SELECT 含 code_content
- 累计字符数不超过 total_budget (防 prompt 撑爆)
- 单 chunk code_content 截断到 code_per_chunk
- chunks 之间去重 (同一 chunk_id 多次出现只保留第一次)

设计:
- 输入: links 列表 (来自 stage3 输出, 字段: chunk_id, reason, ...)
        + db_path (域名索引 SQLite 路径)
- 输出: 渲染好的多行文本, 每个 chunk 4 段:
  - 头: chunk_id
  - name: qualified_name (类/方法/字段名)
  - file: file:line (定位)
  - summary: 1-2 句简介
  - code: code_content 截断 (反幻觉关键!)
  - link_reason: stage3 关联理由
"""

import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional


def _escape_yaml_scalar(s: str) -> str:
    """YAML 缩进/特殊字符转义. 防 LLM 输出被 YAML 解析误读."""
    if not s:
        return ""
    # 把多行缩进统一, 避免破坏 YAML 结构
    return s.replace("\r\n", "\n").replace("\r", "\n")


def build_chunks_text(
    links: List[Dict[str, Any]],
    db_path: Optional[Path],
    max_chunks: int = 10,
    code_per_chunk: int = 400,
    total_budget: int = 4000,
) -> str:
    """
    把 links 渲染成 LLM 可读的多 chunk 文本块.

    Args:
        links: 关联列表, 每项至少含 chunk_id; 可选 reason 字段.
        db_path: domain SQLite 路径 (含 _index.db, 含 code_content 列).
                 None 或不存在时, 跳过 DB 查询, 只用 links 已有字段.
        max_chunks: 最多渲染几个 chunk (防 LLM 上下文超限).
        code_per_chunk: 单 chunk code_content 截断字符数.
        total_budget: 输出总字符数上限 (含 metadata + code).

    Returns:
        多行文本, 格式:
            - chunk_id: <id>
              name: <qualified_name or name>
              file: <basename>:<line>
              summary: <summary 截断>
              code: <code_content 截断>
              link_reason: <reason 截断>
        (无召回时返回 "(无召回)")
    """
    if not links:
        return "(无召回)"

    # 累计输出 + 单 chunk 渲染
    out_lines: List[str] = []
    total_chars = 0
    seen_chunk_ids: set = set()

    for link in links[:max_chunks]:
        cid = link.get("chunk_id", "")
        if not cid or cid in seen_chunk_ids:
            continue
        seen_chunk_ids.add(cid)

        # 默认值 (DB 查不到时用 links 已有字段)
        meta_name = link.get("qualified_name") or link.get("name") or "?"
        meta_fpath = "?"
        meta_line = "?"
        meta_summary = link.get("summary", "") or ""
        code_preview = ""

        # 查 DB 拿完整 metadata + code_content
        if db_path is not None and Path(db_path).exists():
            try:
                with sqlite3.connect(str(db_path)) as conn:
                    conn.row_factory = sqlite3.Row
                    row = conn.execute(
                        "SELECT name, qualified_name, file_path, line_start, "
                        "summary, code_content "
                        "FROM symbols WHERE chunk_id = ?",
                        (cid,),
                    ).fetchone()
                    if row:
                        meta_name = (
                            row["qualified_name"] or row["name"] or "?"
                        )[:60]
                        raw_fpath = row["file_path"] or "?"
                        # basename only, 跨平台
                        meta_fpath = raw_fpath.split("/")[-1].split("\\")[-1]
                        meta_line = str(row["line_start"] or "?")
                        meta_summary = (row["summary"] or meta_summary)[:120]
                        code_preview = (row["code_content"] or "")[:code_per_chunk]
            except Exception:
                # 静默继续, 用 link 已有字段
                pass

        reason = (link.get("reason", "") or "")[:80]
        # 截断 code_preview, 保留换行结构 (反幻觉: 枚举值/方法签名/字段类型可见)
        code_preview = _escape_yaml_scalar(code_preview)

        # 拼单 chunk 块
        chunk_block = (
            f"- chunk_id: {cid}\n"
            f"  name: {meta_name}\n"
            f"  file: {meta_fpath}:{meta_line}\n"
            f"  summary: {meta_summary}\n"
        )
        if code_preview:
            # 缩进 code 内容, 保持可读性
            code_indented = "\n".join(
                f"    {line}" for line in code_preview.split("\n")
            )
            chunk_block += f"  code:\n{code_indented}\n"
        if reason:
            chunk_block += f"  link_reason: {reason}"

        # 累计检查总预算
        if total_chars + len(chunk_block) > total_budget:
            # 截断 code 段
            remaining = total_budget - total_chars - len(
                f"- chunk_id: {cid}\n  name: {meta_name}\n  file: {meta_fpath}:{meta_line}\n  summary: {meta_summary}\n"
            )
            if remaining > 100 and code_preview:
                truncated_code = code_preview[:max(0, remaining - 80)][:remaining - 80]
                code_indented = "\n".join(
                    f"    {line}" for line in truncated_code.split("\n")
                )
                chunk_block = (
                    f"- chunk_id: {cid}\n"
                    f"  name: {meta_name}\n"
                    f"  file: {meta_fpath}:{meta_line}\n"
                    f"  summary: {meta_summary}\n"
                    f"  code:\n{code_indented}\n"
                    f"  link_reason: {reason}"
                )
            else:
                break  # 预算用尽, 不再添加

        out_lines.append(chunk_block)
        total_chars += len(chunk_block)

    return "\n".join(out_lines) if out_lines else "(无召回)"
