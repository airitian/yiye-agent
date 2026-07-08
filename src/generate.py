"""
测试用例生成 - 主流程编排

串联 Stage 2-5:
  Stage 2: 关键词提取
  Stage 3: 需求-代码关联
  Stage 4: 场景推导
  Stage 5: 用例生成

(Stage 1 暂时跳过, 等真实 PRD 接入)
"""

import json
import re
import sqlite3
import yaml
from pathlib import Path
from typing import Dict, List, Any, Optional
from datetime import datetime

from src.logger import get_logger, get_audit_logger
from src.workspace import WorkspaceManager, TaskWorkspace
from src.llm_client import get_default_client
from src.prompt_loader import get_prompt_loader
from src.index_builder import IndexSearcher

logger = get_logger(__name__)


# ============================================================
# 配置加载
# ============================================================

def _load_config(project_root: Path) -> Dict[str, Any]:
    config_path = project_root / ".config" / "config.yaml"
    with open(config_path, encoding='utf-8') as f:
        return yaml.safe_load(f)


# v0.5.9: 模块级缓存 LLMClient 和 PromptLoader, 各 stage 函数复用,
# 避免每个 stage 入口重复 get_default_client()/get_prompt_loader() 调用,
# 以及某一天 client 工厂抛异常时(例如 yaml 缺失)上溯打乱调用栈。
# 线程不安全, 但本项目所有 task 跑在 asyncio + to_thread 串行化下, 安全。
_cached_client = None
_cached_loader = None


def _get_client():
    global _cached_client
    if _cached_client is None:
        _cached_client = get_default_client()
    return _cached_client


def _get_loader():
    global _cached_loader
    if _cached_loader is None:
        _cached_loader = get_prompt_loader()
    return _cached_loader


# ============================================================
# Stage 2: 关键词提取
# ============================================================

def stage1_extract_requirement(
    workspace: TaskWorkspace,
    requirement: Dict[str, Any],
    domain: str,
) -> Dict[str, Any]:
    """v0.5: 从原始需求提炼结构化输入 (core_scenes / edge_cases / key_concepts)

    让 Stage 2 关键词抽取用提炼后的 key_concepts, 而不是从整 requirement 抽。
    Stage 1 产物: stage1/extract.yaml
    """
    started = datetime.now()
    workspace.save_node("stage1.1_extract_requirement", "stage1", "running", started)

    try:
        client = _get_client()
        loader = _get_loader()

        prompt = loader.render(
            "stage1_extract",
            requirement_json=json.dumps(requirement, ensure_ascii=False, indent=2)[:3000],
        )
        logger.info(f"[stage1] 提炼需求, prompt length={len(prompt)}")

        text = client.call(
            prompt=prompt,
            system="你是测试架构师, 擅长从需求文档提炼结构化测试输入。严格按 YAML 输出, 不要多余解释。",
            max_tokens=2000,
            temperature=0.3,
        )
        logger.info(f"[stage1] LLM response length={len(text)}")

        # 解析 YAML
        # 容忍 ```yaml ``` 包裹
        clean = re.sub(r"^```(?:yaml)?\s*", "", text.strip())
        clean = re.sub(r"\s*```$", "", clean)
        try:
            extract = yaml.safe_load(clean) or {}
        except yaml.YAMLError as e:
            logger.warning(f"[stage1] YAML parse failed: {e}, 用空 dict")
            extract = {"_raw_text": text, "_parse_error": str(e)}

        # 保底字段
        extract.setdefault("core_scenes", [])
        extract.setdefault("edge_cases", [])
        extract.setdefault("key_concepts", [])
        extract.setdefault("acceptance_criteria", [])

        finished = datetime.now()
        workspace.save_node(
            "stage1.1_extract_requirement", "stage1", "success",
            started, finished,
        )
        workspace.save_stage("stage1", "success", started, finished)
        workspace.save_artifact("stage1", "extract.yaml", extract)
        logger.info(
            f"[stage1] 完成: core_scenes={len(extract['core_scenes'])}, "
            f"edge_cases={len(extract['edge_cases'])}, "
            f"key_concepts={len(extract['key_concepts'])}"
        )
        return extract
    except Exception as e:
        finished = datetime.now()
        workspace.save_node(
            "stage1.1_extract_requirement", "stage1", "failed",
            started, finished, error=str(e),
        )
        workspace.save_stage("stage1", "failed", started, finished, error=str(e))
        # 失败时不阻塞, 返回空 dict 让 stage 2 走 fallback
        logger.error(f"[stage1] 失败: {e}, 返回空 dict")
        return {}


def stage2_extract_keywords(
    workspace: TaskWorkspace,
    requirement: Dict[str, Any],
    domain: str,
    extract: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """从需求提取检索关键词"""
    started = datetime.now()
    workspace.save_node(
        "stage2.1_extract_keywords", "stage2", "running", started,
    )

    try:
        client = _get_client()
        loader = _get_loader()

        # 构造输入 (用需求结构的关键字段)
        req_text = json.dumps(requirement, ensure_ascii=False, indent=2)[:3000]

        # v0.5: 把 stage 1 提炼的 key_concepts 注入 prompt, 让 LLM 优先用这些概念
        extract_text = ""
        if extract:
            extract_text = json.dumps({
                "key_concepts": extract.get("key_concepts", []),
                "core_scenes": [s.get("name") for s in extract.get("core_scenes", []) if isinstance(s, dict)],
            }, ensure_ascii=False, indent=2)

        prompt = loader.render(
            "stage2_extract_keywords",
            requirement=req_text,
            extract=extract_text,
        )

        response = client.call(
            prompt=prompt,
            system="你是测试架构师, 输出严格 JSON",
            max_tokens=500,
            temperature=0.3,
        )

        # 解析 JSON
        json_match = re.search(r'\{.*\}', response, re.DOTALL)
        if json_match:
            keywords = json.loads(json_match.group(0))
        else:
            logger.warning(f"无法解析关键词响应: {response[:200]}")
            keywords = {"entities": [], "operations": [], "fields": [],
                        "business_concepts": [], "domain_hint": domain,
                        "search_queries": []}

        # 保存产物
        workspace.save_artifact("stage2", "keywords.json", keywords)

        finished = datetime.now()
        workspace.save_node(
            "stage2.1_extract_keywords", "stage2", "success",
            started, finished, input_size=len(req_text),
            output_size=len(json.dumps(keywords)),
        )
        workspace.save_stage("stage2", "success", started, finished)

        logger.info(f"[OK] Stage 2 关键词提取: {len(keywords.get('entities', []))} 实体")
        return keywords

    except Exception as e:
        finished = datetime.now()
        workspace.save_node(
            "stage2.1_extract_keywords", "stage2", "failed",
            started, finished, error=str(e),
        )
        workspace.save_stage("stage2", "failed", started, finished, error=str(e))
        logger.error(f"❌ Stage 2 失败: {e}")
        raise


# ============================================================
# Stage 3: 需求-代码关联
# ============================================================

def stage3_link(
    workspace: TaskWorkspace,
    requirement: Dict[str, Any],
    keywords: Dict[str, Any],
    domain: str,
    project_root: Path,
) -> List[Dict[str, Any]]:
    """需求-代码关联 + LLM 重排序"""
    started = datetime.now()
    workspace.save_node(
        "stage3.1_recall", "stage3", "running", started,
    )

    try:
        # 1. 多路召回 (B 改造: 先 LLM 召回, 失败 fallback BM25)
        db_path = project_root / "domains" / domain / "_index.db"
        if not db_path.exists():
            logger.warning(f"域 [{domain}] 索引不存在, 跳过关联")
            return []

        candidates: List[Dict[str, Any]] = []
        llm_recall_success = False
        llm_hits_raw: Optional[List[Dict[str, Any]]] = None  # 区分 None (LLM 失败) vs [] (LLM 命中 0) vs [...]

        # 路 0: LLM 召回 (B 改造 - 把全部 chunks 喂给 LLM 语义匹配)
        try:
            from src.llm_recall import llm_recall_chunks
            client = _get_client()
            llm_hits = llm_recall_chunks(db_path, domain, requirement, keywords, client)
            if llm_hits is not None:
                llm_hits_raw = llm_hits
                # v0.5.7+: LLM 命中 0 个 (空 list) 也算未成功, fallback 到 BM25
                #          (避免 LLM 抽风返回 [] 把任务全打断)
                if not llm_hits:
                    logger.warning("  LLM 召回空命中, fallback 到 BM25")
                else:
                    llm_recall_success = True
                # 拿到 LLM 召回的 chunk_ids, 去索引里查完整 metadata
                # v0.5.7+: LLM 偶尔返回简化 chunk_id (如 "AgentStatusEnum" 而不是
                #          "385bdcb6-enum:AgentStatusEnum"), 精确查会丢 100%.
                #          修法: 1) 精确查 chunk_id; 2) 查不到时按 name/qualified_name 模糊查
                hit_ids = {h["chunk_id"] for h in llm_hits}
                relevance_map = {h["chunk_id"]: h["relevance"] for h in llm_hits}
                # 抽出"可能的 name 段" (如 "AgentStatusEnum" 从 "385bdcb6-enum:AgentStatusEnum")
                name_candidates = set()
                for cid in hit_ids:
                    if ":" in cid:
                        # "385bdcb6-enum:AgentStatusEnum" -> "AgentStatusEnum"
                        name_candidates.add(cid.split(":", 1)[1])
                    elif "-" in cid and not cid.startswith("-"):
                        # 也可能是 hash-name 格式, 取最后一段
                        name_candidates.add(cid.rsplit("-", 1)[-1])
                    else:
                        # 整段当 name
                        name_candidates.add(cid)
                hit_reason_map = {h["chunk_id"]: h["reason"] for h in llm_hits}
                with IndexSearcher(db_path, domain) as searcher:
                    import sqlite3
                    try:
                        with sqlite3.connect(str(db_path)) as conn:
                            conn.row_factory = sqlite3.Row
                            # 1) 精确查 chunk_id
                            placeholders = ",".join("?" * len(hit_ids))
                            rows = conn.execute(
                                f"SELECT * FROM symbols WHERE chunk_id IN ({placeholders})",
                                list(hit_ids),
                            ).fetchall()
                            for r in rows:
                                row_dict = dict(r)
                                row_dict["relevance"] = relevance_map.get(r["chunk_id"], 3)
                                row_dict["reason"] = hit_reason_map.get(r["chunk_id"], "")
                                candidates.append(row_dict)
                            # 2) 模糊查 name/qualified_name (fallback)
                            for nc in name_candidates:
                                # 用 LIKE 找包含 nc 的 chunk
                                pat = f"%{nc}%"
                                name_rows = conn.execute(
                                    """SELECT * FROM symbols
                                       WHERE qualified_name LIKE ?
                                          OR name LIKE ?
                                       LIMIT 5""",
                                    (pat, pat),
                                ).fetchall()
                                for r in name_rows:
                                    # 避免重复
                                    if any(c.get("chunk_id") == r["chunk_id"] for c in candidates):
                                        continue
                                    row_dict = dict(r)
                                    row_dict["relevance"] = 3
                                    row_dict["reason"] = f"fuzzy match name={nc}"
                                    candidates.append(row_dict)
                    except Exception as e:
                        logger.warning(f"LLM 召回后查 chunks 失败: {e}")
                logger.info(f"  LLM 召回: 命中 {len(llm_hits)} chunk_ids, 拿到 {len(candidates)} 个 chunk metadata (精确+模糊)")
        except Exception as e:
            logger.warning(f"  LLM 召回入口异常, fallback: {e}")

        # 如果 LLM 召回未成功, fallback 到 BM25 多路
        if not llm_recall_success:
            with IndexSearcher(db_path, domain) as searcher:
                # 路 1: 符号检索
                for entity in keywords.get("entities", []):
                    results = searcher.search_symbols(entity, domain, limit=10)
                    candidates.extend(results)

                # 路 2: 全文检索
                for query in keywords.get("search_queries", []):
                    results = searcher.search_fulltext(query, domain, limit=15)
                    candidates.extend(results)

                # 路 3: 标签检索
                for concept in keywords.get("business_concepts", []):
                    results = searcher.search_by_tag(concept, domain, limit=10)
                    candidates.extend(results)

        # 去重
        seen = set()
        unique = []
        for c in candidates:
            cid = c.get("chunk_id")
            if cid and cid not in seen:
                seen.add(cid)
                unique.append(c)
        candidates = unique

        logger.info(f"  多路召回: {len(candidates)} 个候选 chunk (LLM={llm_recall_success})")

        # v0.5.7+: 0 候选时也要写 links.json (空 list) + 标记 stage 失败,
        #          不然 generate.gate.stage3 会误判成功, 让用户审核 0 召回 (无效)
        if not candidates:
            logger.warning("没有召回任何代码")
            workspace.save_artifact("stage3", "links.json", [])
            finished = datetime.now()
            workspace.save_node("stage3.1_recall", "stage3", "success",
                               started, finished, output_size=0)
            workspace.save_stage("stage3", "failed", started, finished,
                                 error="0 chunks 召回, 知识库无匹配")
            return []

        # 2. LLM 重排序
        finished_recall = datetime.now()
        workspace.save_node(
            "stage3.1_recall", "stage3", "success",
            started, finished_recall,
            output_size=len(candidates),
        )

        started_rerank = datetime.now()
        workspace.save_node(
            "stage3.2_rerank", "stage3", "running", started_rerank,
        )

        client = get_default_client()
        loader = get_prompt_loader()

        # 构造候选展示
        cand_text = ""
        for i, c in enumerate(candidates[:30], 1):
            cand_text += f"\n[{i}] chunk_id={c['chunk_id']}\n"
            cand_text += f"    类型: {c.get('chunk_type', '?')}\n"
            cand_text += f"    名称: {c.get('qualified_name', c.get('name', '?'))}\n"
            if c.get('http_route'):
                cand_text += f"    HTTP: {c['http_route']}\n"
            cand_text += f"    文件: {c.get('file_path', '?')}\n"
            if c.get('summary'):
                cand_text += f"    摘要: {c['summary']}\n"

        prompt = loader.render(
            "stage3_link",
            requirement=json.dumps(requirement, ensure_ascii=False)[:2000],
            candidates=cand_text,
        )

        response = client.call(
            prompt=prompt,
            system="你是测试架构师, 输出严格 JSON 数组",
            max_tokens=2000,
            temperature=0.3,
        )

        # 解析 - v0.2.2 容错: 剥 markdown 围栏 + 多 [{...},{...}] (贪婪匹配)
        # 之前简单 re.search(r'\[.*\]', ...) 在 LLM 输出格式乱时 fail
        text = response.strip()
        # 剥 ```json ... ``` 围栏
        fence = re.search(r'```(?:json)?\s*\n(.*?)\n```', text, re.DOTALL | re.IGNORECASE)
        if fence:
            text = fence.group(1).strip()
            json_match = re.search(r'\[.*\]', text, re.DOTALL)
        else:
            json_match = re.search(r'\[.*\]', text, re.DOTALL)
        # v0.2.2: 对前一个候选 (chunk_id 完整) 加 split by `}, {` 容错
        if json_match:
            raw = json_match.group(0)
            try:
                links = json.loads(raw)
            except json.JSONDecodeError:
                # 容错: 截断到最后一个完整对象 `}` 之前
                last_brace = raw.rstrip().rfind('}')
                if last_brace > 0:
                    truncated = raw[:last_brace + 1]
                    # 找到开头 `[`
                    bracket_start = truncated.find('[')
                    if bracket_start >= 0:
                        truncated = truncated[bracket_start:]
                        try:
                            links = json.loads(truncated)
                        except json.JSONDecodeError:
                            logger.warning(f"stage3.2 rerank JSON parse 容错也失败, 返回空")
                            links = []
                    else:
                        links = []
                else:
                    links = []
        else:
            links = []

        # 保留高置信度 (>= 3)
        if links:
            links = [l for l in links if isinstance(l, dict) and l.get("relevance", 0) >= 3]
        # v0.2.2-C 降级: 如果 rerank 完全 parse 失败, 用 stage3.1 candidates 兜底
        # - relevance 默认 3 (中位)
        # - 保证 stage 4 能拿到非空 links 继续跑
        if not links and candidates:
            logger.warning(
                f"stage3.2 rerank JSON parse 全失败, 用 stage3.1 {len(candidates)} candidates 兜底"
            )
            fallback = []
            for c in candidates[:30]:  # 截断 30
                fallback.append({
                    "chunk_id": c.get("chunk_id", ""),
                    "relevance": 3,
                    "link_type": "fallback",
                    "reason": f"auto-link from BM25 (reason v0.2.2 fallback): {c.get('summary', '')[:80]}",
                    "confidence": 0.5,
                })
            links = [l for l in fallback if l.get("chunk_id")] or []

        workspace.save_artifact("stage3", "links.json", links)

        finished = datetime.now()
        workspace.save_node(
            "stage3.2_rerank", "stage3", "success",
            started_rerank, finished,
            input_size=len(cand_text),
            output_size=len(links),
        )
        workspace.save_stage("stage3", "success", started, finished)

        logger.info(f"[OK] Stage 3 关联: {len(links)} 个高置信度关联")
        return links

    except Exception as e:
        finished = datetime.now()
        workspace.save_node(
            "stage3.2_rerank", "stage3", "failed",
            datetime.now(), finished, error=str(e),
        )
        workspace.save_stage("stage3", "failed", started, finished, error=str(e))
        logger.error(f"❌ Stage 3 失败: {e}")
        raise


# ============================================================
# LLM 输出解析容错
# ============================================================

def _save_debug_artifact(
    workspace: Optional[TaskWorkspace],
    stage: str,
    name: str,
    data: Any,
) -> None:
    """尽量保存调试产物，不因审计落盘失败影响主流程。"""
    if workspace is None:
        return
    try:
        workspace.save_artifact(stage, f"debug/{name}", data)
    except Exception as e:
        logger.warning(f"保存调试产物失败 [{stage}/debug/{name}]: {e}")


def _strip_markdown_fence(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return text
    fence = re.search(
        r"```(?:json|yaml|yml)?\s*\n?(.*?)\n?```",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if fence:
        text = fence.group(1).strip()
    return re.sub(r"^\s*```\s*$", "", text, flags=re.MULTILINE).strip()


def _normalize_llm_text(text: str) -> str:
    return (
        (text or "")
        .replace("﻿", "")
        .replace(" ", " ")
        .replace("“", '"')
        .replace("”", '"')
        .replace("‘", "'")
        .replace("’", "'")
    )


def _extract_balanced_json_array(text: str) -> Optional[str]:
    text = text or ""
    starts = [i for i, ch in enumerate(text) if ch == "["]
    for start in starts:
        depth = 0
        in_string = False
        quote_char = ""
        escaped = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\\\":
                    escaped = True
                elif ch == quote_char:
                    in_string = False
            else:
                if ch in ('"', "'"):
                    in_string = True
                    quote_char = ch
                elif ch == "[":
                    depth += 1
                elif ch == "]":
                    depth -= 1
                    if depth == 0:
                        return text[start:i + 1]
    return None


def _repair_common_yaml_scalar_lines(text: str) -> str:
    """v0.5.9+: 加 P0-3 兜底: 把"裸 {{ TODO: ... }}" 强制加双引号.

    LLM 即使知道 YAML 引号要求, 偶尔也会输出::

        expected: {{ TODO: 信息可见范围严格符合权限定义... }}

    YAML 把 ``{{`` 视为 flow mapping 起手 → mapping values are not allowed here.
    本函数在原 pre-process 之前先做这道兜底, 再走原 scalar 修复逻辑.
    """
    if not text:
        return text
    # 模式 1: 简单标量位置 `key: {{ TODO: ... }}` (任何 YAML 上下文:
    #          sequence/dict value, multiline string 后续行也可以)
    #         用 "key: \"{{ TODO ... }}\"" 包整个值
    pattern_bare_key = re.compile(
        r"(^|\n)([ \t-]*)([A-Za-z0-9_\u4e00-\u9fa5][\w\u4e00-\u9fa5-]*[ \t]*:[ \t]*)(\{\{[^}\n]*\}\})",
    )

    def _wrap_bare(m: re.Match) -> str:
        nl_or_start, ws_prefix, key_segment, raw_value = (
            m.group(1), m.group(2), m.group(3), m.group(4)
        )
        return f'{nl_or_start}{ws_prefix}{key_segment}"{raw_value}"'

    text = pattern_bare_key.sub(_wrap_bare, text)

    # 模式 2: 单独一行就是裸 {{ ... }} (sequence item, - 后面直接跟花括号)
    pattern_bare_seq = re.compile(
        r"(^|\n)([ \t]*(?:-[ \t]*))(\{\{[^}\n]*\}\})",
    )
    text = pattern_bare_seq.sub(
        lambda m: f'{m.group(1)}{m.group(2)}"{m.group(3)}"', text,
    )

    repaired = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            repaired.append(line)
            continue
        m = re.match(r"^(\s*(?:-\s*)?[^:#\n][^:\n]*:\s*)(.+?)\s*$", line)
        if not m:
            repaired.append(line)
            continue
        prefix, value = m.group(1), m.group(2).strip()
        if not value:
            repaired.append(line)
            continue
        if value.startswith(("'", '"', "[", "{", "|", ">", "!!")):
            repaired.append(line)
            continue
        if re.fullmatch(r"(?i:true|false|null|none|~|[-+]?\d+(?:\.\d+)?)", value):
            repaired.append(line)
            continue
        if ": " in value or "：" in value or value.endswith(":"):
            repaired.append(prefix + json.dumps(value, ensure_ascii=False))
            continue
        repaired.append(line)
    return "\n".join(repaired)


def _parse_json_array_loose(text: str) -> Optional[List[Any]]:
    text = _normalize_llm_text(_strip_markdown_fence(text))
    if not text:
        return None

    candidates: List[str] = []
    balanced = _extract_balanced_json_array(text)
    if balanced:
        candidates.append(balanced)
    candidates.append(text)

    seen = set()
    ordered_candidates = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            ordered_candidates.append(candidate)

    repaired_candidates: List[str] = []
    for candidate in ordered_candidates:
        repaired = re.sub(r",\s*([\]}])", r"\1", candidate)
        repaired = re.sub(r"\bTrue\b", "true", repaired)
        repaired = re.sub(r"\bFalse\b", "false", repaired)
        repaired = re.sub(r"\bNone\b", "null", repaired)
        repaired_candidates.append(repaired)

    for candidate in ordered_candidates + repaired_candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict) and isinstance(parsed.get("scenarios"), list):
                return parsed["scenarios"]
        except Exception:
            pass
        try:
            parsed = yaml.safe_load(candidate)
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict) and isinstance(parsed.get("scenarios"), list):
                return parsed["scenarios"]
        except Exception:
            pass
    return None


def _repair_json_array_with_llm(client, raw_text: str, label: str) -> Optional[str]:
    raw_text = (raw_text or "").strip()
    if not raw_text:
        return None
    prompt = (
        f"请把下面 {label} 内容修复为严格 JSON 数组。\n"
        "要求:\n"
        "1) 只输出 JSON 数组本体，不要 markdown，不要解释。\n"
        "2) 所有 key 和字符串都用双引号。\n"
        "3) 删除多余逗号/注释/前后说明文字。\n"
        "4) 保留原有语义；如果无法判断，尽量保守，不要编造字段。\n\n"
        "原始内容如下:\n"
        f"{raw_text[:12000]}"
    )
    try:
        return client.call(
            prompt=prompt,
            system="你是 JSON 修复器，只输出严格 JSON 数组。",
            max_tokens=8000,
            temperature=0.1,
        )
    except Exception as e:
        logger.warning(f"JSON 修复调用失败: {e}")
        return None


def _split_yaml_documents_by_case_id(text: str) -> List[str]:
    matches = list(re.finditer(r"(?m)^case_id\s*:", text or ""))
    if len(matches) <= 1:
        return []
    docs = []
    for i, match in enumerate(matches):
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        chunk = text[start:end].strip()
        if chunk:
            docs.append(chunk)
    return docs


def _repair_yaml_with_llm(
    client,
    raw_text: str,
    multi: bool = False,
    expected_docs: Optional[int] = None,
) -> Optional[str]:
    raw_text = (raw_text or "").strip()
    if not raw_text:
        return None
    mode_hint = "多文档输出，用 --- 分隔。" if multi else "输出单个 YAML 文档。"
    count_hint = f"共输出 {expected_docs} 份 YAML 文档。\n" if multi and expected_docs else ""
    prompt = (
        "请只修复下面内容的 YAML 格式，不要改业务语义。\n"
        "要求:\n"
        "1) 只输出 YAML，不要 markdown 围栏，不要解释。\n"
        f"2) {mode_hint}\n"
        "3) 含冒号的普通字符串必须加双引号；多行字符串用 | 或 >。\n"
        f"4) {count_hint}"
        "5) 不要丢字段。\n\n"
        "原始内容如下:\n"
        f"{raw_text[:12000]}"
    )
    try:
        return client.call(
            prompt=prompt,
            system="你是 YAML 修复器，只输出格式修复后的 YAML。",
            max_tokens=8000 if multi else 3000,
            temperature=0.1,
        )
    except Exception as e:
        logger.warning(f"YAML 修复调用失败: {e}")
        return None


# ============================================================
# Stage 4: 场景推导
# ============================================================

def stage4_scenarios(
    workspace: TaskWorkspace,
    requirement: Dict[str, Any],
    links: List[Dict[str, Any]],
    domain: str,
) -> List[Dict[str, Any]]:
    """推导业务场景"""
    started = datetime.now()
    workspace.save_node("stage4.1_scenarios", "stage4", "running", started)

    try:
        client = _get_client()
        loader = _get_loader()

        # 构造输入
        # v0.5.9: 截断 linked_text 防止 prompt 撑爆. 通常 6 个 chunk ×
        # 100 字 = 600 字, 加上 requirement 2000 字, 总约 2600 字, 给
        # LLM 充足 token budget 写场景内容.
        req_text = json.dumps(requirement, ensure_ascii=False)[:2000]
        linked_text = json.dumps(links[:10], ensure_ascii=False)[:4000]

        prompt = loader.render(
            "stage4_scenarios",
            requirement=req_text,
            linked_chunks=linked_text,
        )

        response = client.call(
            prompt=prompt,
            system="你是测试架构师, 输出严格 JSON 数组",
            max_tokens=12000,  # v0.5.9: 8000 → 12000 (失败 line 730 col 116
                                # 经验表明至少需要 ~10000 才能稳定输出完整 JSON)
            temperature=0.5,
        )
        _save_debug_artifact(workspace, "stage4", "raw_llm_response.txt", response)

        # 解析 + 自动修复重试
        scenarios = _parse_json_array_loose(response)
        repaired_response = None
        if scenarios is None:
            logger.warning("[stage4] 首次 JSON 解析失败, 尝试格式修复")
            repaired_response = _repair_json_array_with_llm(client, response, "stage4 场景")
            if repaired_response:
                _save_debug_artifact(workspace, "stage4", "repaired_llm_response.txt", repaired_response)
            scenarios = _parse_json_array_loose(repaired_response)
        if scenarios is None:
            raise ValueError("stage4 scenarios JSON parse failed after repair")

        workspace.save_artifact("stage4", "scenarios.json", scenarios)

        finished = datetime.now()
        workspace.save_node(
            "stage4.1_scenarios", "stage4", "success",
            started, finished,
            input_size=len(req_text) + len(linked_text),
            output_size=len(json.dumps(scenarios)),
        )
        workspace.save_stage("stage4", "success", started, finished)

        # 统计
        by_type = {}
        for s in scenarios:
            t = s.get("type", "unknown")
            by_type[t] = by_type.get(t, 0) + 1

        logger.info(f"[OK] Stage 4 场景: {len(scenarios)} 个 ({by_type})")
        return scenarios

    except Exception as e:
        finished = datetime.now()
        workspace.save_node(
            "stage4.1_scenarios", "stage4", "failed",
            started, finished, error=str(e),
        )
        workspace.save_stage("stage4", "failed", started, finished, error=str(e))
        logger.error(f"❌ Stage 4 失败: {e}")
        raise


# ============================================================
# Stage 5: 用例生成 (A 改造: 批量模式 + 单条 fallback)
# ============================================================

def stage5_generate_cases(
    workspace: TaskWorkspace,
    requirement: Dict[str, Any],
    scenarios: List[Dict[str, Any]],
    links: List[Dict[str, Any]],
    domain: str,
    project_root: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """主入口: 场景数 >= 8 时用批量 (A 改造), 否则用单条

    v0.3-D 修复: 把 batch_size 5 改成 3 (减少单 prompt 大小, 减少 LLM 截断风险);
    把 0.5 阈值改成 0.3 (放宽允许部分 batch 失败, 不强制退到 single).

    v0.5.9: project_root 在 entry 就地解析, 不依赖 caller 显式传, 避免
    dangling kwarg bug.
    """
    # v0.5.9: project_root 在 entry 就地 resolve, 子函数 (_generate_single_case)
    # 不再依赖外部传参, 杜绝 dangling kwarg chain.
    if project_root is None:
        project_root = Path(__file__).resolve().parent.parent
    # 批量阈值: 8 场景以上用批量 (更省 API 调用)
    BATCH_THRESHOLD = 8
    BATCH_SIZE = 3  # v0.3-D: 从 5 降到 3 (批更小, 单 prompt 字符数 -40%)
    BATCH_MIN_SUCCESS_RATIO = 0.3  # v0.3-D: 从 0.5 放宽到 0.3 (允许部分 batch 失败)

    if len(scenarios) >= BATCH_THRESHOLD:
        logger.info(
            f"  场景数 {len(scenarios)} >= {BATCH_THRESHOLD}, 尝试批量模式"
        )
        try:
            cases = stage5_generate_cases_batch(
                workspace, requirement, scenarios, links, domain,
                batch_size=BATCH_SIZE,
                project_root=project_root,
            )
            if cases and len(cases) >= len(scenarios) * BATCH_MIN_SUCCESS_RATIO:
                logger.info(
                    f"  批量模式成功: {len(cases)} 用例"
                )
                return cases
            logger.warning(
                f"  批量模式产出不足 ({len(cases)}/{len(scenarios)}), "
                f"补足单条"
            )
        except Exception as e:
            logger.warning(f"  批量模式异常, fallback 到单条: {e}")

    return stage5_generate_cases_serial(
        workspace, requirement, scenarios, links, domain,
        project_root=project_root,
    )


def stage5_generate_cases_batch(
    workspace: TaskWorkspace,
    requirement: Dict[str, Any],
    scenarios: List[Dict[str, Any]],
    links: List[Dict[str, Any]],
    domain: str,
    batch_size: int = 5,
    project_root: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """
    A 改造: 批量生成. 每 batch_size 个场景一次 LLM 调用.

    设计:
    - 把 batch_size 个场景拼成一个 prompt
    - LLM 输出 batch_size 个 YAML 用例 (用 --- 分隔, 或 单 YAML 多文档)
    - 解析每个 YAML, 补元数据
    - 解析失败的, 单独 retry (回到单条)
    - 每批结束增量保存
    """
    started = datetime.now()
    workspace.save_node("stage5.1_cases", "stage5", "running", started)

    try:
        client = _get_client()
        loader = _get_loader()

        cases = []
        parse_failures = []
        linked_chunks = [l.get("chunk_id") for l in links if l.get("chunk_id")]
        total = len(scenarios)

        # 把 scenarios 分批
        for batch_start in range(0, total, batch_size):
            batch = scenarios[batch_start:batch_start + batch_size]
            batch_num = batch_start // batch_size + 1
            total_batches = (total + batch_size - 1) // batch_size
            logger.info(
                f"  批次 {batch_num}/{total_batches}: 场景 {batch_start+1}-{batch_start+len(batch)}"
            )

            # 拼 prompt: 把 batch 内所有 scenario JSON 列出
            scenarios_block = []
            for j, sc in enumerate(batch, 1):
                global_idx = batch_start + j
                scenarios_block.append(
                    f"### 场景 {global_idx} (batch 内序号 {j}):\n"
                    f"```json\n{json.dumps(sc, ensure_ascii=False, indent=2)}\n```"
                )
            scenarios_text = "\n\n".join(scenarios_block)

            # 加载 stage5_case prompt 模板, 但把 scenarios 和 linked_chunks 都塞进去
            linked_text = "\n".join([f"- {cid}" for cid in linked_chunks[:20]])
            try:
                base_template = loader.load("stage5_case")
                prompt = (
                    base_template
                    .replace("{{ scenario }}", scenarios_text)
                    .replace("{{ linked_chunks }}", linked_text)
                    + "\n\n## 批量输出要求\n\n"
                    f"你正在为 {len(batch)} 个场景批量生成用例.\n"
                    f"输出 {len(batch)} 个独立的 YAML 文档, 用 `---` 分隔.\n"
                    "每个 YAML 文档必须以 `case_id` 开头, 跟单个输出的 schema 完全一致.\n"
                    f"case_id 中必须包含场景编号, 方便我对应: 场景 {batch_start + 1} 的用例 case_id 末尾用 _S{batch_start + 1}, "
                    f"场景 {batch_start + 2} 用 _S{batch_start + 2}, 以此类推.\n"
                    "不要 markdown 围栏 (不要 ``` 也不要 ```yaml), 不要任何解释文字.\n"
                )
            except Exception as e:
                logger.warning(f"  拼 prompt 失败: {e}")
                continue

            batch_ok = True
            response = None
            for attempt in (1, 2):
                try:
                    sys_msg = (
                        "你是测试工程师. 输出多个独立 YAML 文档, 用 --- 分隔, "
                        "不要 markdown 围栏, 不要任何解释."
                    ) if attempt == 1 else (
                        "上次输出无法解析. 这次请: "
                        "1) 每个 YAML 独立, 用 --- 分隔, "
                        "2) 不要 markdown 围栏, "
                        "3) 占位符必须带双引号, "
                        "4) 字符串含冒号时加引号."
                    )
                    response = client.call(
                        prompt=prompt,
                        system=sys_msg,
                        max_tokens=8000,  # 5 用例 × ~1500 tokens 上限
                        temperature=0.5,
                    )
                    _save_debug_artifact(
                        workspace,
                        "stage5",
                        f"batch_{batch_num:03d}_attempt_{attempt}_raw.yaml",
                        response,
                    )
                    # 解析多 YAML 文档 + 自动修复重试
                    parsed_docs = _parse_multi_yaml(response)
                    if parsed_docs is None:
                        repaired_response = _repair_yaml_with_llm(
                            client,
                            response,
                            multi=True,
                            expected_docs=len(batch),
                        )
                        if repaired_response:
                            _save_debug_artifact(
                                workspace,
                                "stage5",
                                f"batch_{batch_num:03d}_attempt_{attempt}_repaired.yaml",
                                repaired_response,
                            )
                        parsed_docs = _parse_multi_yaml(repaired_response)
                    if parsed_docs is None:
                        raise RuntimeError("multi YAML parse failed")
                    # 处理 list[dict] 形式: LLM 可能输出一个包含所有用例的 YAML 列表
                    if (len(parsed_docs) == 1
                            and isinstance(parsed_docs[0], list)
                            and len(parsed_docs[0]) == len(batch)):
                        parsed_docs = parsed_docs[0]
                    if len(parsed_docs) != len(batch):
                        logger.warning(
                            f"  批次 {batch_num}: LLM 输出 {len(parsed_docs)} 个 YAML, "
                            f"期望 {len(batch)}, 尝试逐个 fallback"
                        )
                        # 不算完全失败, 但要逐个 reparse 用 _parse_case_yaml
                        batch_cases = _match_docs_to_scenarios(
                            parsed_docs, batch, batch_start, domain,
                        )
                        if batch_cases and len(batch_cases) >= len(batch) * 0.6:
                            cases.extend(batch_cases)
                            batch_ok = True
                            break
                        raise RuntimeError(
                            f"docs={len(parsed_docs)} != scenarios={len(batch)}"
                        )
                    # 数量匹配, 直接对应
                    batch_cases = []
                    for j, (doc, sc) in enumerate(zip(parsed_docs, batch), 1):
                        global_idx = batch_start + j
                        case = _enrich_case(doc, sc, global_idx, domain)
                        batch_cases.append(case)
                    cases.extend(batch_cases)
                    batch_ok = True
                    break
                except Exception as e:
                    logger.warning(f"  批次 {batch_num} attempt {attempt} 异常: {e}")
                    batch_ok = False

            if not batch_ok:
                # 整批失败, 逐场景 fallback
                logger.warning(
                    f"  批次 {batch_num} 批量失败, 逐场景 fallback (单条 mode)"
                )
                for j, sc in enumerate(batch):
                    global_idx = batch_start + j
                    single = _generate_single_case(
                        client, loader, sc, links, domain, global_idx,
                        project_root=project_root,
                        workspace=workspace,
                    )
                    if single:
                        cases.append(single)
                    else:
                        parse_failures.append({
                            "scenario_index": global_idx,
                            "scenario_title": sc.get("title", sc.get("name", "?")),
                        })

            # 每批增量保存
            workspace.save_artifact("stage5", "cases.json", cases)
            workspace.save_artifact("stage5", "cases.yaml", _cases_to_yaml(cases))
            logger.info(
                f"    累计 {len(cases)} 成功 / {len(parse_failures)} 失败 / "
                f"已处理 {batch_start + len(batch)}/{total}"
            )

        # 最终保存
        workspace.save_artifact("stage5", "cases.json", cases)
        workspace.save_artifact("stage5", "cases.yaml", _cases_to_yaml(cases))
        if parse_failures:
            workspace.save_artifact("stage5", "parse_failures.json", parse_failures)

        finished = datetime.now()
        workspace.save_node(
            "stage5.1_cases", "stage5", "success",
            started, finished,
            input_size=len(scenarios),
            output_size=len(cases),
        )
        workspace.save_stage("stage5", "success", started, finished)

        total_count = len(cases) + len(parse_failures)
        rate = (len(cases) / total_count * 100) if total_count else 0
        logger.info(
            f"[OK] 批量 Stage 5: {len(cases)} 成功 / {len(parse_failures)} 失败 "
            f"({rate:.0f}%, 共 {total_count})"
        )
        return cases

    except Exception as e:
        finished = datetime.now()
        workspace.save_node(
            "stage5.1_cases", "stage5", "failed",
            started, finished, error=str(e),
        )
        workspace.save_stage("stage5", "failed", started, finished, error=str(e))
        logger.error(f"❌ 批量 Stage 5 失败: {e}")
        raise


def stage5_generate_cases_serial(
    workspace: TaskWorkspace,
    requirement: Dict[str, Any],
    scenarios: List[Dict[str, Any]],
    links: List[Dict[str, Any]],
    domain: str,
    project_root: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """原单条 mode (保留作 fallback 和小批量场景)."""
    started = datetime.now()
    workspace.save_node("stage5.1_cases", "stage5", "running", started)

    try:
        client = _get_client()
        loader = _get_loader()

        cases = []
        parse_failures = []
        linked_chunks = [l.get("chunk_id") for l in links if l.get("chunk_id")]

        for i, scenario in enumerate(scenarios, 1):
            case = _generate_single_case(
                client, loader, scenario, links, domain, i,
                project_root=project_root,
                workspace=workspace,
            )
            if case is not None:
                cases.append(case)
            else:
                parse_failures.append({
                    "scenario_index": i,
                    "scenario_title": scenario.get("title", scenario.get("name", "?")),
                })
                logger.warning(f"  场景 {i} 两次尝试均失败, 已跳过")

            if i % 5 == 0:
                workspace.save_artifact("stage5", "cases.json", cases)
                workspace.save_artifact("stage5", "cases.yaml", _cases_to_yaml(cases))
                logger.info(
                    f"    已生成 {i}/{len(scenarios)} 用例 "
                    f"(累计 {len(cases)} 成功, {len(parse_failures)} 失败)"
                )

        workspace.save_artifact("stage5", "cases.json", cases)
        workspace.save_artifact("stage5", "cases.yaml", _cases_to_yaml(cases))
        if parse_failures:
            workspace.save_artifact("stage5", "parse_failures.json", parse_failures)

        finished = datetime.now()
        workspace.save_node(
            "stage5.1_cases", "stage5", "success",
            started, finished,
            input_size=len(scenarios),
            output_size=len(cases),
        )
        workspace.save_stage("stage5", "success", started, finished)

        total = len(cases) + len(parse_failures)
        rate = (len(cases) / total * 100) if total else 0
        logger.info(
            f"[OK] 单条 Stage 5: {len(cases)} 成功 / {len(parse_failures)} 失败 "
            f"({rate:.0f}%, 共 {total})"
        )
        return cases

    except Exception as e:
        finished = datetime.now()
        workspace.save_node(
            "stage5.1_cases", "stage5", "failed",
            started, finished, error=str(e),
        )
        workspace.save_stage("stage5", "failed", started, finished, error=str(e))
        logger.error(f"❌ 单条 Stage 5 失败: {e}")
        raise


def _generate_single_case(
    client, loader, scenario: Dict, links: List[Dict],
    domain: str, idx: int,
    project_root: Optional[Path] = None,
    workspace: Optional[TaskWorkspace] = None,
) -> Optional[Dict[str, Any]]:
    """单场景生成用例 (含 retry + 逐个 fallback).

    v0.2.3: linked_chunks 现在传给 LLM 时含 name + file_path + summary,
    而不仅仅是 chunk_id. 这样 LLM 能看到真实字段名和上下文,
    而不是从 chunk_id hash 里猜.

    v0.2.3 fix: project_root 参数化 (避免 __file__ in module path bug).
    caller 应该传入 project_root; 缺省 fallback 到 cwd (向后兼容).

    Note: 实际从 caller 处传 workspace.root, 不依赖 __file__.
    """
    # 收集 chunk metadata: 根据 chunk_id 从 DB 查 (name, qualified_name, file_path, line, summary)
    if project_root is None:
        # Fallback: caller 没传时用 cwd (向后兼容)
        project_root = Path.cwd()
    db_path = project_root / "domains" / domain / "_index.db"
    chunks_lines = []  # noqa
    for l in links[:10]:  # 最多 10 个 link
        cid = l.get("chunk_id", "")
        if not cid:
            continue
        # 从 DB 查 metadata
        meta_name = "?"
        meta_fpath = "?"
        meta_line = "?"
        meta_summary = ""
        if db_path.exists():
            try:
                with sqlite3.connect(str(db_path)) as conn:
                    conn.row_factory = sqlite3.Row
                    row = conn.execute(
                        "SELECT name, qualified_name, file_path, line_start, summary "
                        "FROM symbols WHERE chunk_id = ?",
                        (cid,),
                    ).fetchone()
                    if row:
                        meta_name = (row["qualified_name"] or row["name"] or "?")[:50]
                        meta_fpath = (row["file_path"] or "").split("/")[-1].split("\\")[-1]
                        meta_line = row["line_start"] or "?"
                        meta_summary = (row["summary"] or "")[:120]
            except Exception:
                pass
        reason = l.get("reason", "")[:80]
        chunks_lines.append(
            f"- chunk_id: {cid}\n"
            f"  name: {meta_name}\n"
            f"  file: {meta_fpath}:{meta_line}\n"
            f"  summary: {meta_summary}\n"
            f"  link_reason: {reason}"
        )
    chunks_text = "\n".join(chunks_lines) if chunks_lines else "(无召回)"

    prompt = loader.render(
        "stage5_case",
        scenario=json.dumps(scenario, ensure_ascii=False, indent=2),
        linked_chunks=chunks_text,
    )

    for attempt in (1, 2):
        try:
            sys_msg = (
                "你是测试工程师. 只输出严格 YAML, 不要任何 markdown 围栏, "
                "不要解释性文字."
            ) if attempt == 1 else (
                "上次输出无法解析. 这次: "
                "1) 严格 YAML 语法, "
                "2) 不要任何 markdown 围栏, "
                "3) 多行字符串用 | 或 > 块, "
                "4) 含 : 的字符串加双引号."
            )
            response = client.call(
                prompt=prompt,
                system=sys_msg,
                max_tokens=3000,
                temperature=0.5,
            )
            _save_debug_artifact(
                workspace,
                "stage5",
                f"case_{idx:03d}_attempt_{attempt}_raw.yaml",
                response,
            )
            case = _parse_case_yaml(response, scenario, idx, domain)
            if case is None:
                repaired_response = _repair_yaml_with_llm(client, response, multi=False)
                if repaired_response:
                    _save_debug_artifact(
                        workspace,
                        "stage5",
                        f"case_{idx:03d}_attempt_{attempt}_repaired.yaml",
                        repaired_response,
                    )
                case = _parse_case_yaml(repaired_response, scenario, idx, domain)
            if case is not None:
                return case
        except Exception as e:
            logger.warning(f"  场景 {idx} attempt {attempt} 异常: {e}")
    return None


def _parse_multi_yaml(response: str) -> Optional[List[Any]]:
    """解析多 YAML 文档 (用 --- 分隔). 返回 list[dict|list|...] 或 None."""
    if not response or not response.strip():
        return None

    text = _normalize_llm_text(_strip_markdown_fence(response))
    if not text:
        return None

    candidates = [text, _repair_common_yaml_scalar_lines(text)]
    parsed_docs: Optional[List[Any]] = None

    for candidate in candidates:
        try:
            docs = list(yaml.safe_load_all(candidate))
            docs = [d for d in docs if d is not None]
            if len(docs) == 1 and not isinstance(docs[0], list):
                try:
                    single = yaml.safe_load(candidate)
                    if isinstance(single, list):
                        docs = [d for d in single if d is not None]
                except yaml.YAMLError:
                    pass
            case_markers = len(re.findall(r"(?m)^case_id\s*:", candidate))
            if docs and not (case_markers > 1 and len(docs) == 1 and isinstance(docs[0], dict)):
                parsed_docs = docs
                break
        except yaml.YAMLError as e:
            logger.warning(f"  multi-YAML 解析失败: {e}")

    if parsed_docs is None:
        json_docs = _parse_json_array_loose(text)
        if isinstance(json_docs, list) and json_docs:
            parsed_docs = json_docs

    if parsed_docs is None:
        for candidate in candidates:
            chunks = _split_yaml_documents_by_case_id(candidate)
            if not chunks:
                continue
            docs = []
            ok = True
            for chunk in chunks:
                try:
                    doc = yaml.safe_load(_repair_common_yaml_scalar_lines(chunk))
                except yaml.YAMLError:
                    ok = False
                    break
                if doc is not None:
                    docs.append(doc)
            if ok and docs:
                parsed_docs = docs
                break

    return parsed_docs


def _match_docs_to_scenarios(
    docs: List[Any], batch: List[Dict], batch_start: int, domain: str,
) -> List[Dict]:
    """当 LLM 输出 YAML 数量 != 期望时, 按 case_id 末尾的 _S{数字} 后缀匹配场景."""
    cases = []
    used_scenarios = set()

    for doc in docs:
        if not isinstance(doc, dict):
            continue
        cid = str(doc.get("case_id", ""))
        # 找末尾的 _S{数字}
        m = re.search(r"_S(\d+)\b", cid)
        if not m:
            # 没匹配, 跳过
            continue
        try:
            idx_in_batch = int(m.group(1))
        except ValueError:
            continue
        global_idx = batch_start + idx_in_batch
        if global_idx < batch_start + 1 or global_idx > batch_start + len(batch):
            continue
        sc_offset = global_idx - batch_start - 1  # 0-based in batch
        if sc_offset in used_scenarios:
            continue
        sc = batch[sc_offset]
        case = _enrich_case(doc, sc, global_idx, domain)
        cases.append(case)
        used_scenarios.add(sc_offset)
    return cases


def _enrich_case(doc: Any, scenario: Dict, idx: int, domain: str) -> Dict:
    """从 LLM 输出 (dict 或 list) 包成完整 case."""
    if isinstance(doc, dict):
        case = doc
    elif isinstance(doc, list):
        case = {"steps": doc, "_wrapped_from_list": True}
    else:
        case = {"raw_text": str(doc), "_fallback": True}

    case["case_id"] = case.get("case_id") or f"CASE-{domain.upper()}-{idx:03d}"
    case.setdefault("title", scenario.get("title", scenario.get("name", f"场景 {idx}")))
    case.setdefault("scenario_type", scenario.get("type", "unknown"))
    case.setdefault("priority", scenario.get("priority", "P1"))
    case["generated_at"] = datetime.now().isoformat()
    case["generated_by"] = "CaseGen-v0.1-batch"
    case["human_approved"] = False
    return case



def _parse_case_yaml(response: str, scenario: Dict, idx: int, domain: str):
    """
    解析 LLM 返回的 YAML 用例 (鲁棒版).
    优先级:
    1) 剥 markdown 围栏
    2) 尝试 JSON / YAML / 修复后 YAML
    3) 若是 dict: 补元数据, 返回
    4) 若是 list: 用 {"steps": list} 包成 dict
    5) 若是 str / 其它: 用 fallback 字段包成 dict (raw_text 留存)
    6) 解析异常返回 None
    """
    text = _normalize_llm_text(_strip_markdown_fence(response))
    if not text:
        return None

    candidates = [text]
    repaired = _repair_common_yaml_scalar_lines(text)
    if repaired != text:
        candidates.append(repaired)

    json_doc = _parse_json_array_loose(text)
    if isinstance(json_doc, list) and len(json_doc) == 1 and isinstance(json_doc[0], dict):
        candidates.append(json.dumps(json_doc[0], ensure_ascii=False))
    else:
        try:
            parsed_json = json.loads(text)
            if isinstance(parsed_json, (dict, list)):
                candidates.append(json.dumps(parsed_json, ensure_ascii=False))
        except Exception:
            pass

    parsed = None
    last_error = None
    for candidate in candidates:
        try:
            parsed = yaml.safe_load(candidate)
            break
        except yaml.YAMLError as e:
            last_error = e
            continue

    if parsed is None:
        try:
            parsed = yaml.safe_load("raw_text: |\n  " + text.replace("\n", "\n  "))
        except yaml.YAMLError:
            logger.warning(f"  场景 {idx} YAML 解析失败: {last_error}")
            return None

    if isinstance(parsed, dict):
        case = parsed
    elif isinstance(parsed, list):
        case = {"steps": parsed, "_wrapped_from_list": True}
    elif isinstance(parsed, str):
        case = {"raw_text": parsed, "_fallback": True}
    else:
        case = {"raw_text": str(parsed) if parsed is not None else "", "_fallback": True}

    case["case_id"] = case.get("case_id") or f"CASE-{domain.upper()}-{idx:03d}"
    case.setdefault("title", scenario.get("title", scenario.get("name", f"场景 {idx}")))
    case.setdefault("scenario_type", scenario.get("type", "unknown"))
    case.setdefault("priority", scenario.get("priority", "P1"))
    case["generated_at"] = datetime.now().isoformat()
    case["generated_by"] = "CaseGen-v0.1"
    case["human_approved"] = False
    return case


def _cases_to_yaml(cases: List[Dict]) -> str:
    """cases 转 YAML"""
    return yaml.dump(
        {"cases": cases},
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )


# ============================================================
# Retry: 重试失败的 task
# ============================================================

def _resolve_retry_context(
    old_task_id: str,
    project_root: Path,
    domain_override: Optional[str] = None,
) -> Dict[str, Any]:
    """解析 retry/续跑所需上下文, 但不实际执行 generate."""
    audit = get_audit_logger()
    workspaces_dir = project_root / "runs"

    mgr = WorkspaceManager(workspaces_dir)
    old_ws = mgr.get_task(old_task_id)
    if not old_ws:
        return {"ok": False, "error": f"task 不存在: {old_task_id}"}

    # 读原 task 的 req_id + domain
    # v0.5.7: 三路 fallback - requirement 文件 (带 domain 字段) → 旧 task.db → 日志 grep → 用户手动
    req_id = old_ws.req_id
    domain: Optional[str] = None

    req_file = project_root / "requirements" / f"{req_id}.json"
    if req_file.exists():
        try:
            with open(req_file, encoding="utf-8") as f:
                requirement = json.load(f)
            domain = requirement.get("domain") or domain
        except Exception as e:
            logger.warning(f"读 requirement 文件失败, 用最小需求: {e}")
            requirement = None
    else:
        requirement = None

    # Fallback 2: 从 task.db 读 domain (新 schema 才有, 老 task 没存)
    if not domain:
        try:
            with sqlite3.connect(old_ws.db_path) as _c:
                _c.row_factory = sqlite3.Row
                _row = _c.execute(
                    "SELECT domain FROM task WHERE task_id=?", (old_task_id,)
                ).fetchone()
                if _row:
                    domain = _row["domain"] or domain
        except Exception:
            pass

    # Fallback 3: 从 frontend_run_*.log grep --domain <name>
    if not domain:
        log_glob = project_root / "runs" / req_id / "frontend_run_*.log"
        for log in log_glob.parent.glob(log_glob.name):
            try:
                first_line = log.read_text(encoding="utf-8", errors="ignore").split("\n", 1)[0]
                m = re.search(r'''--domain\s+([^\s'"]+)''', first_line)
                if m:
                    domain = m.group(1)
                    logger.info(f"从 {log.name} 推断 domain={domain}")
                    break
            except Exception:
                continue

    # Fallback 4: 用户显式覆盖 (CLI --domain / API body.domain)
    if domain_override:
        domain = domain_override
        logger.info(f"使用 domain_override={domain}")

    if not requirement:
        requirement = {
            "req_id": req_id,
            "title": f"重试需求 {req_id}",
            "main_flow": [{"step": 1, "action": "示例流程", "actor": "用户"}],
            "exception_flows": [],
            "business_rules": [],
        }

    # 决定 skip_stages (last_ok 之前全部跳过)
    last_ok = old_ws.get_last_success_stage()
    if last_ok:
        order = ["stage1", "stage2", "stage3", "stage4", "stage5"]
        idx = order.index(last_ok)
        skip_stages = order[: idx + 1]
    else:
        skip_stages = []

    # domain 必填 (generate 不接受空), 没拿到时记 warning 让用户补
    if not domain:
        return {
            "ok": False,
            "error": "无法确定 domain (requirements 文件不在且 task.db 无记录). 请手动指定 domain 重跑.",
            "req_id": req_id,
        }

    return {
        "ok": True,
        "old_task_id": old_task_id,
        "req_id": req_id,
        "domain": domain,
        "requirement": requirement,
        "skipped_stages": skip_stages,
        "last_success_stage": last_ok,
        "artifact_source_task_id": old_task_id,
    }


def prepare_retry_task(
    old_task_id: str,
    project_root: Path,
    domain_override: Optional[str] = None,
    new_task_id: Optional[str] = None,
) -> Dict[str, Any]:
    """预创建续跑 task, 立即返回 new_task_id; 真正执行可异步继续."""
    ctx = _resolve_retry_context(old_task_id, project_root, domain_override=domain_override)
    if not ctx.get("ok"):
        return ctx

    mgr = WorkspaceManager(project_root / "runs")
    if new_task_id:
        prepared_ws = mgr.get_task(new_task_id)
        if not prepared_ws:
            prepared_ws = mgr.create_task(ctx["req_id"], domain=ctx["domain"], task_id=new_task_id)
    else:
        prepared_ws = mgr.create_task(ctx["req_id"], domain=ctx["domain"])

    ctx["new_task_id"] = prepared_ws.task_id
    return {
        "ok": True,
        "old_task_id": old_task_id,
        "new_task_id": prepared_ws.task_id,
        "req_id": ctx["req_id"],
        "domain": ctx["domain"],
        "skipped_stages": ctx["skipped_stages"],
        "last_success_stage": ctx["last_success_stage"],
    }


def _continue_retry_with_context(ctx: Dict[str, Any], project_root: Path) -> Dict[str, Any]:
    """使用 prepare/resolve 后的上下文继续真正执行 generate."""
    audit = get_audit_logger()
    old_task_id = ctx["old_task_id"]
    req_id = ctx["req_id"]
    domain = ctx["domain"]
    skip_stages = ctx["skipped_stages"]
    new_task_id = ctx["new_task_id"]

    audit.log(
        action="task.retry.start",
        target=req_id,
        detail={
            "old_task_id": old_task_id,
            "new_task_id": new_task_id,
            "skipped_stages": skip_stages,
            "last_success_stage": ctx.get("last_success_stage"),
        },
    )

    try:
        result = generate(
            req_id=req_id,
            requirement=ctx["requirement"],
            domain=domain,
            project_root=project_root,
            skip_stages=skip_stages,
            artifact_source_task_id=ctx.get("artifact_source_task_id") or old_task_id,
            task_id_override=new_task_id,
        )
        audit.log(
            action="task.retry.success",
            target=req_id,
            detail={
                "old_task_id": old_task_id,
                "new_task_id": result["task_id"],
                "skipped_stages": skip_stages,
            },
        )
        return {
            "ok": True,
            "new_task_id": result["task_id"],
            "old_task_id": old_task_id,
            "req_id": req_id,
            "domain": domain,
            "skipped_stages": skip_stages,
        }
    except Exception as e:
        audit.log(
            action="task.retry.failed",
            target=req_id,
            result="failed",
            detail={"old_task_id": old_task_id, "new_task_id": new_task_id, "error": str(e)},
        )
        return {
            "ok": False,
            "error": str(e),
            "old_task_id": old_task_id,
            "new_task_id": new_task_id,
            "req_id": req_id,
            "skipped_stages": skip_stages,
        }


def continue_retry_task(
    old_task_id: str,
    new_task_id: str,
    project_root: Path,
    domain_override: Optional[str] = None,
) -> Dict[str, Any]:
    """继续执行一个已预创建的续跑 task (供 UI/API 后台异步调用)."""
    ctx = _resolve_retry_context(old_task_id, project_root, domain_override=domain_override)
    if not ctx.get("ok"):
        ctx["new_task_id"] = new_task_id
        try:
            mgr = WorkspaceManager(project_root / "runs")
            failed_ws = mgr.get_task(new_task_id)
            if failed_ws:
                failed_ws.update_task_status("failed")
        except Exception:
            pass
        return ctx
    ctx["new_task_id"] = new_task_id
    return _continue_retry_with_context(ctx, project_root)


def retry_failed_task(
    old_task_id: str,
    project_root: Path,
    domain_override: Optional[str] = None,
) -> Dict[str, Any]:
    """v0.5.8: 同步重试一个 task; 内部先占位新 task, 再执行 generate."""
    prepared = prepare_retry_task(old_task_id, project_root, domain_override=domain_override)
    if not prepared.get("ok"):
        return prepared
    return continue_retry_task(
        old_task_id=old_task_id,
        new_task_id=prepared["new_task_id"],
        project_root=project_root,
        domain_override=domain_override,
    )


# ============================================================
# 主入口
# ============================================================

def generate(
    req_id: str,
    requirement: Dict[str, Any],
    domain: str,
    project_root: Path,
    skip_stages: List[str] = None,
    artifact_source_task_id: Optional[str] = None,
    task_id_override: Optional[str] = None,
) -> Dict[str, Any]:
    """
    测试用例生成主流程 (A+B 改造: 阶段三 LLM 召回, 阶段五批量)

    Args:
        req_id: 需求 ID
        requirement: 需求结构
        domain: 业务域
        project_root: 项目根目录
        skip_stages: 跳过的 Stage 列表
    """
    skip_stages = skip_stages or []
    workspaces_dir = project_root / "runs"

    mgr = WorkspaceManager(workspaces_dir)
    if task_id_override:
        workspace = mgr.get_task(task_id_override)
        if workspace:
            workspace.update_task_status("running")
        else:
            workspace = mgr.create_task(req_id, domain=domain, task_id=task_id_override)
    else:
        workspace = mgr.create_task(req_id, domain=domain)  # v0.5.7: 写入 domain 给 retry 用
    artifact_source_ws = None
    if artifact_source_task_id:
        artifact_source_ws = mgr.get_task(artifact_source_task_id)
    elif skip_stages:
        artifact_source_ws = mgr.get_latest_task_for_req(req_id, exclude_task_id=workspace.task_id)
    audit = get_audit_logger()

    audit.log(
        action="generate.start",
        target=req_id,
        detail={"domain": domain},
    )

    results = {
        "task_id": workspace.task_id,
        "req_id": req_id,
        "domain": domain,
        "stages": {},
    }

    # 每个 stage 独立判断 skip, 并从已有产物加载数据 (跳过时不重跑)
    def _load_or_none(stage: str, name: str):
        if stage in skip_stages:
            data = workspace.load_artifact(stage, name)
            if data is not None:
                return data
            if artifact_source_ws:
                data = artifact_source_ws.load_artifact(stage, name)
                if data is not None:
                    logger.info(
                        f"复用旧任务产物: source_task={artifact_source_ws.task_id} / {stage}/{name}"
                    )
                    # v0.5.8: 把复用产物落到新 task, 便于后续再次续跑时继续复用完整链路
                    workspace.save_artifact(stage, name, data)
                    try:
                        now = datetime.now()
                        workspace.save_stage(stage, "success", now, now)
                    except Exception as e:
                        logger.warning(f"记录复用 stage={stage} 成功态失败: {e}")
                    return data
            logger.warning(f"skip {stage} 但无产物: {name}, 将重新生成")
        return None

    # v0.5: 解析 review gates (从 requirement.review_gates 读)
    # v0.5.7+: 字段缺失时默认全开, 强制每个 stage 完成后等审核
    def _review_gates(req: Dict[str, Any]) -> List[str]:
        gates = req.get("review_gates")
        if gates is None:
            return ["stage3", "stage4", "stage5"]  # 默认全开
        return gates

    try:
        # Stage 1 (PRD 提炼) - 把 requirement 结构化给 stage 2 用
        extract = _load_or_none("stage1", "extract.yaml")
        if extract is None:
            extract = stage1_extract_requirement(workspace, requirement, domain)
        results["stages"]["stage1"] = extract

        # Stage 2
        keywords = _load_or_none("stage2", "keywords.json")
        if keywords is None:
            keywords = stage2_extract_keywords(workspace, requirement, domain, extract)
        results["stages"]["stage2"] = keywords

        # Stage 3
        links = _load_or_none("stage3", "links.json")
        stage3_reused = links is not None
        if links is None:
            links = stage3_link(workspace, requirement, keywords, domain, project_root)
        results["stages"]["stage3"] = links

        # v0.5.7+: 0 召回时不进 gate, 直接 failed
        # (空 gate 让用户审核 "0 关联" 没意义)
        if not links:
            workspace.update_task_status("failed")
            audit.log(
                action="generate.failed.stage3_empty",
                target=req_id,
                detail={"task_id": workspace.task_id,
                        "message": "Stage 3 召回 0 chunks, 知识库无匹配, 任务终止"},
            )
            logger.warning(f"[WARN] Stage 3 召回 0 chunks, 任务终止 (不进 gate)")
            return results

        # v0.5: 审核 gate (stage 3 完成后)
        # v0.5.8: 如果是从已审核 task 复用 stage3 产物继续跑, 不应再次卡回 stage3 gate
        if "stage3" in _review_gates(requirement) and not stage3_reused:
            workspace.update_task_status("awaiting_review_links")
            audit.log(
                action="generate.gate.stage3",
                target=req_id,
                detail={"task_id": workspace.task_id, "message": "Stage 3 完, 等审 links"},
            )
            logger.info(
                f"[GATE] Stage 3 完成, 进入审核态 awaiting_review_links. "
                f"用户需在 /flows/{workspace.task_id} 批准后才会进 Stage 4"
            )
            results["review_status"] = "awaiting_review_links"
            return results

        # Stage 4
        scenarios = _load_or_none("stage4", "scenarios.json")
        stage4_reused = scenarios is not None
        if scenarios is None:
            scenarios = stage4_scenarios(workspace, requirement, links, domain)
        results["stages"]["stage4"] = scenarios

        # v0.5: 审核 gate (stage 4 完成后)
        # v0.5.8: 如果是从已审核 task 复用 stage4 产物继续跑, 不应再次卡回 stage4 gate
        if "stage4" in _review_gates(requirement) and not stage4_reused:
            workspace.update_task_status("awaiting_review_scenarios")
            audit.log(
                action="generate.gate.stage4",
                target=req_id,
                detail={"task_id": workspace.task_id, "message": "Stage 4 完, 等审 scenarios"},
            )
            logger.info(
                f"[GATE] Stage 4 完成, 进入审核态 awaiting_review_scenarios. "
                f"用户需在 /flows/{workspace.task_id} 批准后才会进 Stage 5"
            )
            results["review_status"] = "awaiting_review_scenarios"
            return results

        # Stage 5
        cases = _load_or_none("stage5", "cases.json")
        stage5_reused = cases is not None
        if cases is None:
            cases = stage5_generate_cases(
                workspace, requirement, scenarios, links, domain,
                project_root=project_root,
            )
        results["stages"]["stage5"] = cases

        # v0.5: 审核 gate 检查 (stage 5 完成后)
        if "stage5" in _review_gates(requirement) and not stage5_reused:
            workspace.update_task_status("awaiting_review_cases")
            audit.log(
                action="generate.gate.stage5",
                target=req_id,
                detail={"task_id": workspace.task_id, "message": "Stage 5 完, 等审核"},
            )
            logger.info(
                f"[GATE] Stage 5 完成, 进入审核态 awaiting_review_cases. "
                f"用户需在 /review/{workspace.task_id} 批准后才会变 success"
            )
            results["review_status"] = "awaiting_review_cases"
            return results

        workspace.update_task_status("success")
        audit.log(
            action="generate.success",
            target=req_id,
            detail={"task_id": workspace.task_id},
        )
        return results

    except Exception as e:
        workspace.update_task_status("failed")
        audit.log(
            action="generate.failed",
            target=req_id,
            result="failed",
            detail={"error": str(e)},
        )
        raise


# ============================================================
# 命令行入口
# ============================================================

if __name__ == "__main__":
    import sys
    from src.logger import setup_logging

    setup_logging()

    project_root = Path(__file__).parent.parent
    kb_root = project_root

    only = sys.argv[1] if len(sys.argv) > 1 else None
    max_files = int(sys.argv[2]) if len(sys.argv) > 2 else None

    logger.info(f"项目根: {project_root}")
    logger.info(f"知识库根: {kb_root}")
    if only:
        logger.info(f"仅构建域: {only}")
    if max_files:
        logger.info(f"限制文件数: {max_files}")

    results = build_all_domains(
        project_root=project_root,
        kb_root=kb_root,
        only_domain=only,
        max_files=max_files,
        enrich_with_llm=True,
    )

    print()
    print("=" * 60)
    print("构建结果汇总")
    print("=" * 60)
    for r in results:
        if "error" in r:
            print(f"❌ {r['domain']}: {r['error']}")
        else:
            print(f"✅ {r['domain']}: {r['files_scanned']} 文件 / {r['chunks_created']} chunks / {r['build_duration_sec']}s")
