"""
检索层回归测试

覆盖两个真实缺陷:
1. CLI kb search 调用不存在的 IndexSearcher.search() -> AttributeError
2. FTS5 unicode61 分词器不拆驼峰, 导致 AgentStatus / agent_status /
   onlineStatus 等符号查询全部 0 命中 (实测customer-service 1152 chunks)

外加: CLI 必须用 with 打开 IndexSearcher (连接在 __enter__ 里建)
"""

import sqlite3
from pathlib import Path

import pytest

from src.index_builder import SCHEMA_SQL, IndexSearcher


# ============================================================
# fixtures
# ============================================================

@pytest.fixture
def idx_db(tmp_path: Path) -> Path:
    """建一个含驼峰符号 + 中文摘要的小索引库"""
    db_path = tmp_path / "_index.db"

    rows = [
        # chunk_id, name, qualified_name, file_path, line_start, code, summary
        (
            "c1-class", "AgentConfService", "AgentConfService",
            "src/AgentConfService.java", 10,
            "class AgentConfService { void updateStatusByAgentId() {} }",
            "客服配置服务",
        ),
        (
            "c2-method", "updateStatusByAgentId",
            "AgentConfService.updateStatusByAgentId",
            "src/AgentConfService.java", 22,
            "public void updateStatusByAgentId(Long id) {}",
            "更新客服状态",
        ),
        (
            "c3-enum", "AgentStatusEnum", "AgentStatusEnum",
            "src/enums/AgentStatusEnum.java", 1,
            "enum AgentStatusEnum { ACTIVE, DEACTIVE }",
            "客服状态枚举",
        ),
    ]

    with sqlite3.connect(str(db_path)) as conn:
        conn.executescript(SCHEMA_SQL)
        for cid, name, qname, fpath, line, code, summary in rows:
            conn.execute(
                """INSERT INTO code_fts
                   (chunk_id, domain, file_path, name, qualified_name,
                    code_content, summary)
                   VALUES (?,?,?,?,?,?,?)""",
                (cid, "test-domain", fpath, name, qname, code, summary),
            )
            conn.execute(
                """INSERT INTO symbols
                   (chunk_id, domain, file_path, file_type, language,
                    chunk_type, name, qualified_name, line_start, line_end,
                    business_tags, summary)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (cid, "test-domain", fpath, "java", "java", "method",
                 name, qname, line, line + 5, '["客服"]', summary),
            )
        conn.commit()

    return db_path


# ============================================================
# 缺陷 1: IndexSearcher.search() 必须存在 (CLI 依赖它)
# ============================================================

def test_search_method_exists(idx_db: Path):
    """回归: CLI 曾调用不存在的 search() 导致 AttributeError"""
    with IndexSearcher(idx_db, "test-domain") as s:
        assert callable(s.search)


def test_search_returns_qualified_name_and_path(idx_db: Path):
    """search() 返回结果必须带 CLI 展示所需的字段"""
    with IndexSearcher(idx_db, "test-domain") as s:
        results = s.search("AgentConfService", limit=5)

    assert results
    for r in results:
        assert "qualified_name" in r
        assert "file_path" in r
        assert "line_start" in r
        assert "summary" in r
        assert "_score" not in r, "内部排序字段不应泄漏给调用方"


def test_search_empty_query_returns_empty(idx_db: Path):
    with IndexSearcher(idx_db, "test-domain") as s:
        assert s.search("") == []
        assert s.search("   ") == []


def test_search_dedupes_by_chunk_id(idx_db: Path):
    """同一chunk 命中多条路径时只保留一条"""
    with IndexSearcher(idx_db, "test-domain") as s:
        results = s.search("AgentConfService", limit=10)

    ids = [r["chunk_id"] for r in results]
    assert len(ids) == len(set(ids))


# ============================================================
# 缺陷 2: 驼峰/下划线查询必须能召回 (FTS5 unicode61 不拆词)
# ============================================================

@pytest.mark.parametrize("raw", [
    "AgentStatusEnum",
    "agentStatusEnum",
    "agent_status_enum",
    "AgentStatus",
    "agent_status",
])
def test_camel_and_snake_queries_hit(idx_db: Path, raw: str):
    """
    回归: 修复前以下查询全部 0 命中.
    根因: unicode61 把 AgentStatusEnum 整体切成 agentstatusenum,
    不拆驼峰也不保原大小写, 与 phrase match 查询对不上.
    """
    with IndexSearcher(idx_db, "test-domain") as s:
        results = s.search_fulltext(raw, limit=10)

    assert results, f"查询 [{raw}] 应命中, 实际 0 条"
    assert any("AgentStatusEnum" in r["qualified_name"] for r in results)


def test_sanitize_splits_camel_case():
    expr = IndexSearcher._sanitize_fts_query("updateStatusByAgentId")
    assert '"update"' in expr
    assert '"status"' in expr
    assert '"agent"' in expr


def test_sanitize_splits_underscore_and_lowercases():
    expr = IndexSearcher._sanitize_fts_query("agent_status")
    assert '"agent"' in expr
    assert '"status"' in expr
    # 不应出现原始大小写形式 (unicode61 大小写不敏感, 归一更稳)
    assert '"Agent' not in expr


def test_sanitize_strips_fts_operators():
    """FTS5 运算符不能穿透到 MATCH 表达式"""
    import re as _re

    for bad in ['foo" OR "bar', 'a AND b', 'NEAR(a b)', 'x*', '中文" OR',
                'foo NEAR/2 bar', 'a AND NOT b']:
        expr = IndexSearcher._sanitize_fts_query(bad)
        if not expr:
            continue
        # 每个 token 都必须是被双引号包裹的纯 word (可带前缀 * )
        for token in expr.split(" OR "):
            assert _re.fullmatch(r'"[\w\u4e00-\u9fff]+"\*?', token), \
                f"非法 token [{token}] 来自输入 [{bad}] -> {expr}"
        # 不能出现裸露的 FTS5 运算符
        assert not _re.search(r'(^|\s)(AND|NOT|NEAR)\s', expr), \
            f"运算符泄漏: {expr}"


def test_sanitize_returns_empty_for_punctuation_only():
    assert IndexSearcher._sanitize_fts_query('"""') == ""
    assert IndexSearcher._sanitize_fts_query("   ") == ""


# ============================================================
# 连接生命周期: 必须用 with
# ============================================================

def test_search_without_enter_raises_typeerror(idx_db: Path):
    """
    回归: CLI 曾直接 IndexSearcher(path) 后不 enter 就search,
    self._conn 为 None -> AttributeError.
    这里确保该错误是可辨识的 TypeError, 而非静默返回错结果.
    """
    s = IndexSearcher(idx_db, "test-domain")
    with pytest.raises((TypeError, AttributeError)):
        s.search("AgentConfService", limit=5)


def test_searcher_closes_connection_on_exit(idx_db: Path):
    with IndexSearcher(idx_db, "test-domain") as s:
        conn = s._conn
    assert conn is not None
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")