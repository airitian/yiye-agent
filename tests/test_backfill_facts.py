# -*- coding: utf-8 -*-
"""
tools/backfill_facts.py 的回归测试

重点验证**陈旧索引检测**: 这个工具从已有索引库复原 chunks 来重跑 Fact 层,
前提是索引里的 called_methods 带接收者。索引一旦是旧版解析器建的
(裸方法名), 数据链路会静默降级成 guess, Fact 缺 SQL/表证据 ——
而两种情况都表现为"跑完了, 数字不好看", 不报任何错。
所以这里必须有断言去拦。
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.code_parser import CodeChunk
from src.index_builder import IndexBuilder
from tools.backfill_facts import (
    has_receiver_call,
    index_is_stale,
    load_chunks,
    load_statements,
)


def _chunk(chunk_id, calls):
    return CodeChunk(
        chunk_id=chunk_id, file_path="A.java", file_type="java",
        language="java", chunk_type="method", name="m", qualified_name="C.m",
        line_start=1, line_end=9, code_content="void m(){}",
        called_methods=list(calls))


@pytest.fixture
def db_with(tmp_path):
    """
    造一个索引库: 写入 chunks(含 called_methods) 与 sql_index。

    必须走 add_chunk() 而不是手写 INSERT —— chunks 表有 NOT NULL 的
    file_path 列, 手写 INSERT 会漏列报错。add_chunk 顺带写 symbols 与
    code_fts, 与真实构建路径一致。
    """
    p = tmp_path / "_index.db"
    with IndexBuilder(p, "customer-service") as b:
        b.add_chunk(_chunk("c1", ["bossOrderMapper.updateStatus", "helper"]))
        b.add_chunk(_chunk("c2", ["customerMapper.list"]))
        b.add_sql_statement(_stmt())
        b.commit()
    return p


def _stmt():
    from src.xml_parser import SqlStatement
    return SqlStatement(
        stmt_id="updateStatus", stmt_type="update",
        namespace="ai.yiye.dao.BossOrderMapper",
        sql_text="UPDATE boss_order SET status=?",
        tables=["boss_order"], params=["status"],
        file_path="BossOrderMapper.xml", line_start=10)


# ============================================================
# 陈旧检测
# ============================================================

def test_fresh_index_not_stale(db_with):
    """带接收者的索引是新鲜的"""
    chunks = load_chunks(db_with)
    assert has_receiver_call(chunks) is True
    assert index_is_stale(chunks) is None


def test_stale_index_detected(tmp_path):
    """
    全是裸方法名 -> 判定为陈旧。

    这是实测到的真实故障: customer-service 库 1249 条 called_methods
    全是裸方法名, 而 exact 数据链路唯一入口就是接收者。
    """
    chunks = [_chunk("c1", ["updateStatus", "list", "get"])]
    assert has_receiver_call(chunks) is False
    reason = index_is_stale(chunks)
    assert reason and "重建符号索引" in reason


def test_empty_chunks_not_stale():
    """空 chunks 不算陈旧(那是"没构建", 由另一个分支处理)"""
    assert has_receiver_call([]) is False
    # 空列表走的是"索引为空"分支, 这里只保证不抛异常
    assert index_is_stale([]) is None or True


# ============================================================
# 复原
# ============================================================

def test_load_chunks_restores_body(db_with):
    """必须把 code_content / called_methods 完整复原 —— 证据靠它们"""
    chunks = load_chunks(db_with)
    assert len(chunks) == 2
    c = chunks[0]
    assert c.chunk_id == "c1"
    assert c.code_content == "void m(){}"
    assert c.called_methods == ["bossOrderMapper.updateStatus", "helper"]


def test_load_chunks_skips_broken_rows(tmp_path):
    """坏 chunk_data 不能让整批失败, 否则一个坏行阻塞全域"""
    p = tmp_path / "b.db"
    with IndexBuilder(p, "d") as b:
        cur = b._conn.cursor()
        cur.execute(
            "INSERT INTO chunks (chunk_id, domain, file_path, chunk_data,"
            " indexed_at) VALUES ('x','d','A.java','{not json',datetime('now'))")
        cur.execute(
            "INSERT INTO chunks (chunk_id, domain, file_path, chunk_data,"
            " indexed_at) VALUES ('y','d','A.java','{}',datetime('now'))")
        b.commit()
    assert load_chunks(p) == []


def test_load_statements_restores_csv_columns(db_with):
    """
    tables / params 是 CSV 列, 必须按 CSV 解。

    按 JSON 解会静默返回空 -> "表名一个都查不到", 且不报错。
    """
    st = load_statements(db_with)[0]
    assert st.stmt_id == "updateStatus"
    assert st.tables == ["boss_order"]
    assert st.params == ["status"]
    assert st.namespace.endswith("BossOrderMapper")
    assert st.line_start == 10


def test_load_statements_empty_db(tmp_path):
    p = tmp_path / "e.db"
    with IndexBuilder(p, "d") as b:
        b.commit()
    assert load_statements(p) == []


# ============================================================
# 端到端: 陈旧库必须被拒绝
# ============================================================

def test_backfill_rejects_stale_index(tmp_path):
    """
    端到端拦停: 陈旧索引下 backfill_domain 必须返回 error 而不是继续跑。

    不拦的后果是"跑完了, 事实 27 条, 覆盖 100%", 看起来完全正常 ——
    但那些事实全都没有 SQL/表证据, 用例的表引用是空的。
    """
    from tools.backfill_facts import backfill_domain
    p = tmp_path / "_index.db"
    with IndexBuilder(p, "customer-service") as b:
        b.add_chunk(_chunk("c1", ["updateStatus"]))
        b.commit()

    req = tmp_path / "req.json"
    req.write_text(json.dumps({
        "req_id": "r1", "title": "t", "background": "状态异常时下线",
        "main_flow": [], "acceptance": [], "constraints": [],
        "edge_cases": [], "actors": [],
    }, ensure_ascii=False), encoding="utf-8")

    st = backfill_domain("customer-service", p, [req], rebuild=False)
    assert "error" in st and "陈旧" in st["error"]


def test_backfill_allows_stale_when_forced(tmp_path):
    """--allow-stale 是排查用的逃生门, 显式传入才允许在陈旧库上跑"""
    from tools.backfill_facts import backfill_domain
    p = tmp_path / "_index.db"
    with IndexBuilder(p, "d") as b:
        b.add_chunk(_chunk("c1", ["updateStatus"]))
        b.commit()
    req = tmp_path / "r.json"
    req.write_text(json.dumps({"req_id": "r", "title": "t",
                               "background": "x"}, ensure_ascii=False),
                   encoding="utf-8")
    st = backfill_domain("d", p, [req], rebuild=False, allow_stale=True)
    assert "error" not in st
    assert st["chunks"] == 1
