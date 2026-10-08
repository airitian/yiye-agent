"""
LLM 召回 (B 改造 v0.2) - 二级检索: BM25 预筛 → LLM 语义挑选

替代单纯 LLM 召回. v0.1 把整个 domain chunks 喂给 LLM, 大域 (>500 chunks) 失败.
v0.2 改成二级检索:
  Level 1 (BM25): IndexSearcher.search_fulltext 用关键词召回 top 200
  Level 2 (LLM):  把 200 候选喂给 LLM 语义挑选 relevance >= 3
  Fallback: 召回 < 20 → 扩大到 top 500; BM25 失败 → 返回 None (caller fallback)
"""

import json
import re
import sqlite3
from pathlib import Path
from typing import Dict, List, Any, Optional

from src.logger import get_logger
from src.index_builder import IndexSearcher

logger = get_logger(__name__)


# 召回参数 (可调)
BM25_TOP_K_DEFAULT = 200
BM25_TOP_K_EXPANDED = 500
BM25_FALLBACK_THRESHOLD = 20  # 召回 < 20 时扩大


def get_domain_chunks_metadata(domain_db_path: Path, domain: str) -> List[Dict[str, Any]]:
    """
    从 domain 索引里读所有 chunks 的 metadata (不含完整 code_content).
    节省 tokens: 每个 chunk ~150 tokens metadata, 100 chunks ≈ 15K tokens.
    """
    if not domain_db_path.exists():
        return []

    chunks_meta = []
    try:
        with sqlite3.connect(str(domain_db_path)) as conn:
            conn.row_factory = sqlite3.Row
            for r in conn.execute(
                """SELECT chunk_id, name, qualified_name, file_path,
                          chunk_type, http_route, summary, business_tags,
                          line_start, line_end
                   FROM symbols WHERE domain = ?""",
                (domain,),
            ):
                chunks_meta.append({
                    "chunk_id": r["chunk_id"],
                    "name": r["name"] or "",
                    "qualified_name": r["qualified_name"] or "",
                    "file_path": r["file_path"] or "",
                    "chunk_type": r["chunk_type"] or "unknown",
                    "http_route": r["http_route"],
                    "summary": r["summary"] or "",
                    "business_tags": r["business_tags"] or "[]",
                    "line_start": r["line_start"],
                    "line_end": r["line_end"],
                })
    except Exception as e:
        logger.warning(f"读 domain chunks metadata 失败: {e}")
        return []

    return chunks_meta


def prefilter_chunks_with_bm25(
    domain_db_path: Path,
    domain: str,
    query: str,
    top_k: int = BM25_TOP_K_DEFAULT,
) -> List[Dict[str, Any]]:
    """
    Level 1: BM25/FTS5 预筛. 用 IndexSearcher.search_fulltext 召回 top_k chunks.

    Returns:
        List[chunk metadata dict]  (格式同 get_domain_chunks_metadata 输出)
        失败时返回 []

    v0.2.1: FTS5 中文 token boundary 限制 ('企微获客' / '获客链接' 不被识别).
    解决: 同时跑 name LIKE 查询 (LIKE 不受 token boundary 影响), 合并结果去重.
    """
    if not query or not query.strip():
        return []
    if not domain_db_path.exists():
        logger.warning(f"domain db 不存在: {domain_db_path}")
        return []

    seen_ids = set()
    results = []

    # Path 1: FTS5 召回
    try:
        with IndexSearcher(domain_db_path, domain) as s:
            rows = s.search_fulltext(query, limit=top_k)
        for r in rows:
            cid = r.get("chunk_id")
            if cid and cid not in seen_ids:
                seen_ids.add(cid)
                results.append(dict(r))
    except Exception as e:
        logger.warning(f"FTS5 召回失败: {e}")

    # Path 2: name LIKE 召回 (绕开 FTS5 中文 token 边界)
    # 抽取 query 里可能的 identifier token (英文 + 中文双字+)
    import re as _re
    tokens = _re.findall(r'[A-Za-z_][A-Za-z0-9_]*|[\u4e00-\u9fff]{2,}', query)
    if tokens:
        try:
            with sqlite3.connect(str(domain_db_path)) as conn:
                conn.row_factory = sqlite3.Row
                # 抓去重的 5 个 token (限制 SQL 复杂度)
                # 用 upper() + unaccent 实现 case-insensitive LIKE (驼峰类名匹配)
                for tok in tokens[:5]:
                    pattern = f"%{tok.upper()}%"
                    for r in conn.execute(
                        """SELECT chunk_id, name, qualified_name, file_path,
                                  chunk_type, http_route, summary, business_tags,
                                  line_start, line_end
                           FROM symbols
                           WHERE domain = ?
                             AND (upper(name) LIKE ? OR upper(qualified_name) LIKE ?)
                           LIMIT 50""",
                        (domain, pattern, pattern),
                    ).fetchall():
                        cid = r["chunk_id"]
                        if cid not in seen_ids:
                            seen_ids.add(cid)
                            results.append({
                                "chunk_id": r["chunk_id"],
                                "name": r["name"] or "",
                                "qualified_name": r["qualified_name"] or "",
                                "file_path": r["file_path"] or "",
                                "chunk_type": r["chunk_type"] or "unknown",
                                "http_route": r["http_route"],
                                "summary": r["summary"] or "",
                                "business_tags": r["business_tags"] or "[]",
                                "line_start": r["line_start"],
                                "line_end": r["line_end"],
                            })
                            if len(results) >= top_k * 2:
                                break
                    if len(results) >= top_k * 2:
                        break
        except Exception as e:
            logger.warning(f"LIKE 召回失败: {e}")

    # Path 3: business_terms 召回 (硬编码业务模块关键词, 绕开 token 限制)
    # v0.2.2 修复: LIMIT 提高到 100 (原来 30 截断了类名匹配)
    # v0.2.2 修复: upper() case-insensitive (驼峰类名匹配)
    business_terms = [
        # 客服域 - 关键业务 token (优先)
        "WorkWechatCustomerAcquisition", "EnterpriseWechatCustomer",
        "CustomerUpload", "WorkWechatCustomer",
        # 客服域 - 通用 token
        "Acquisition", "Customer", "Listener", "Service",
        # 通用业务
        "Offline", "Online", "Upload", "Report",
        # 异常/状态
        "Anomaly", "Status", "State", "Operation",
    ]
    try:
        with sqlite3.connect(str(domain_db_path)) as conn:
            conn.row_factory = sqlite3.Row
            for term in business_terms:
                # 查 name 或 qualified_name 包含此 term 的所有 chunks
                for r in conn.execute(
                    """SELECT chunk_id, name, qualified_name, file_path,
                              chunk_type, http_route, summary, business_tags,
                              line_start, line_end
                       FROM symbols
                       WHERE domain = ?
                         AND (upper(name) LIKE ? OR upper(qualified_name) LIKE ?)
                       LIMIT 100""",
                    (domain, f"%{term.upper()}%", f"%{term.upper()}%"),
                ).fetchall():
                    cid = r["chunk_id"]
                    if cid not in seen_ids:
                        seen_ids.add(cid)
                        results.append({
                            "chunk_id": r["chunk_id"],
                            "name": r["name"] or "",
                            "qualified_name": r["qualified_name"] or "",
                            "file_path": r["file_path"] or "",
                            "chunk_type": r["chunk_type"] or "unknown",
                            "http_route": r["http_route"],
                            "summary": r["summary"] or "",
                            "business_tags": r["business_tags"] or "[]",
                            "line_start": r["line_start"],
                            "line_end": r["line_end"],
                        })
                        if len(results) >= top_k * 3:
                            break
                if len(results) >= top_k * 3:
                    break
    except Exception as e:
        logger.warning(f"business_terms 召回失败: {e}")

    return results[:top_k * 2]


def build_recall_prompt(
    requirement: Dict[str, Any],
    keywords: Dict[str, Any],
    chunks_meta: List[Dict[str, Any]],
) -> str:
    """
    拼装 LLM 召回 prompt.

    设计:
    - 顶部: 需求摘要 + 关键词 (实体/操作/字段/概念)
    - 中部: chunks 列表 (id + name + http_route + summary + business_tags + file:line)
    - 底部: 要求 LLM 输出 JSON 数组 [{chunk_id, relevance, reason}]
    """
    req_text = json.dumps(requirement, ensure_ascii=False, indent=2)[:2000]
    kw_text = json.dumps(keywords, ensure_ascii=False, indent=2)[:1500]

    # 构建 chunks 列表 (紧凑格式)
    chunks_lines = []
    for i, c in enumerate(chunks_meta, 1):
        tag = c.get("http_route") or c.get("chunk_type", "")
        summary = (c.get("summary") or "")[:100]
        tags = c.get("business_tags", "[]")
        if tags and tags != "[]":
            try:
                tag_list = json.loads(tags)
                if tag_list:
                    summary = (summary + " | tags: " + ",".join(tag_list[:3]))[:150]
            except Exception:
                pass
        fname = c.get("file_path", "").split("/")[-1].split("\\")[-1]
        chunks_lines.append(
            f"[{i}] id={c['chunk_id']} | {c.get('chunk_type', '?')} | "
            f"{c.get('name', '?')[:40]} | {tag or '-'} | "
            f"{fname}:{c.get('line_start', '?')} | "
            f"{summary}"
        )
    chunks_text = "\n".join(chunks_lines)

    prompt = f"""你是测试架构师. 你的任务是从代码知识库里, 找出与需求相关的代码 chunks.

## 需求
```
{req_text}
```

## 关键词
```
{kw_text}
```

## 代码 Chunks (共 {len(chunks_meta)} 个, 来自 BM25 预筛)

```
{chunks_text}
```

## 任务

阅读上面的需求和代码 chunks, 找出**与需求功能直接相关**的 chunks.

判断标准:
- relevance=5: 需求明确要求的功能/接口, 在代码里能找到对应实现 (例如: 需求提到登录, 代码里有 LoginController)
- relevance=4: 需求的关键依赖/调用链上的代码
- relevance=3: 间接相关 (例如: 同模块的同类功能, 可参考其实现)
- relevance=1-2: 弱相关 (同领域但功能无关)
- relevance=0: 无关

## 输出要求

输出严格的 JSON 数组, 每个元素:
```json
[
  {{"chunk_id": "<chunk_id>", "relevance": <0-5>, "reason": "<一句话说明为什么相关>"}}
]
```

**只输出 relevance >= 3 的 chunks**. 如果都不相关, 输出 `[]`.

不要输出 markdown 围栏 (不要 ```json), 不要其他解释文字.
"""
    return prompt


def parse_recall_response(response: str) -> Optional[List[Dict[str, Any]]]:
    """
    解析 LLM 召回响应. 返回 [{chunk_id, relevance, reason}] 或 None (失败).
    """
    if not response or not response.strip():
        return None

    text = response.strip()

    # 剥 markdown 围栏
    fence = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()

    # P0-3 fix: 用 raw_decode 替代贪婪 re.search 找 JSON 数组
    from src.json_utils import extract_json
    parsed = extract_json(text)
    if parsed is None:
        logger.warning("LLM 召回响应里找不到 JSON")
        return None

    # 容错: LLM 偶尔漏 [], 拿到 dict 时包成 list
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        logger.warning(f"LLM 召回响应不是 list, 实际类型: {type(parsed).__name__}")
        return None

    # 校验每项是 dict 且有 chunk_id
    cleaned = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        cid = item.get("chunk_id")
        rel = item.get("relevance", 0)
        if not cid:
            continue
        try:
            rel = int(rel)
        except (ValueError, TypeError):
            rel = 0
        if rel >= 3:
            cleaned.append({
                "chunk_id": cid,
                "relevance": rel,
                "reason": item.get("reason", ""),
            })
    return cleaned


def _build_bm25_query(requirement: Dict[str, Any], keywords: Dict[str, Any]) -> str:
    """
    构造 BM25 召回 query (精简版).

    v0.2.1 修正: 之前把背景+所有关键词都拼进去, 800+ token query 会让 FTS5
    把所有 token 当 OR 条件, rank 混乱 (长 query 评分衰减, target 排到 500+).

    策略: 只用标题 + 5 个核心 entity + 5 个核心 fields.
    FTS5 短 query (10-15 token) 召回更精准.
    """
    parts = []

    # 1. 标题 (最重要, 反映核心业务)
    title = requirement.get("title", "")
    if title:
        parts.append(title)

    # 2. 核心 entity (前 5)
    kw_dict = keywords if isinstance(keywords, dict) else {}
    for key in ("entities", "operations", "actions"):
        items = kw_dict.get(key, [])
        if isinstance(items, list):
            for x in items[:5]:
                parts.append(str(x))
            break  # 只用第一个 key 的

    # 3. 核心 fields (前 3, 通常是英文 token, BM25 命中高)
    fields = kw_dict.get("fields", [])
    if isinstance(fields, list):
        for f in fields[:3]:
            parts.append(str(f))

    return " ".join(parts)


def llm_recall_chunks(
    domain_db_path: Path,
    domain: str,
    requirement: Dict[str, Any],
    keywords: Dict[str, Any],
    client,
) -> Optional[List[Dict[str, Any]]]:
    """
    v0.2: 二级检索 LLM 召回.
    Level 1: BM25 召回 top 200 (候选)
    Level 2: LLM 语义挑选 relevance >= 3
    Fallback: 召回 < 20 → 扩大到 top 500

    Returns:
        [{"chunk_id", "relevance", "reason"}] on success
        None on failure (caller 应 fallback 到纯 BM25)
        [] if domain is empty
    """
    # 1. 构造 BM25 query
    query = _build_bm25_query(requirement, keywords)
    if not query.strip():
        logger.warning("BM25 query 为空, 跳过 LLM 召回")
        return []

    # 2. Level 1: BM25 召回 top 200
    logger.info(f"  LLM 召回 (二级): domain={domain}, BM25 query len={len(query)}")
    chunks_meta = prefilter_chunks_with_bm25(domain_db_path, domain, query, top_k=BM25_TOP_K_DEFAULT)
    logger.info(f"  BM25 召回: {len(chunks_meta)} chunks")

    # 3. Fallback: 召回太少 → 扩大
    if len(chunks_meta) < BM25_FALLBACK_THRESHOLD:
        logger.info(
            f"  BM25 召回 {len(chunks_meta)} < {BM25_FALLBACK_THRESHOLD}, "
            f"扩大到 top {BM25_TOP_K_EXPANDED}"
        )
        chunks_meta = prefilter_chunks_with_bm25(
            domain_db_path, domain, query, top_k=BM25_TOP_K_EXPANDED
        )
        logger.info(f"  BM25 扩大召回: {len(chunks_meta)} chunks")

    if not chunks_meta:
        logger.warning("BM25 召回为空, LLM 召回跳过 (caller 应 fallback)")
        return None  # 区分"召回为空"(None) vs "召回成功但无相关"([])

    # 4. Level 2: LLM 语义挑选
    prompt = build_recall_prompt(requirement, keywords, chunks_meta)

    try:
        response = client.call(
            prompt=prompt,
            system=(
                "你是测试架构师. 只输出严格 JSON 数组, 不要 markdown 围栏, "
                "不要任何解释文字."
            ),
            max_tokens=4000,  # 候选 200, relevance>=3 通常 < 50 个
            temperature=0.2,
        )
    except Exception as e:
        logger.warning(f"LLM 召回调用失败: {e}")
        return None

    parsed = parse_recall_response(response)
    if parsed is None:
        logger.warning("LLM 召回响应解析失败, caller 应 fallback")
        return None

    logger.info(f"  LLM 召回: 命中 {len(parsed)} 个相关 chunks (relevance>=3)")
    return parsed
