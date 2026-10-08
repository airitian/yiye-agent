# -*- coding: utf-8 -*-
"""
cases 表(用例正文落库)测试

## 为什么这层必须有

case_fact_map 只存 case_id -> fact_id, 用例正文在内存里生成完就丢。
后果: 业务问答里「这条规则有没有测过」「具体怎么测的」两问只能给一个计数,
给不出用例标题与预期结果 —— 而业务方要的就是后者。
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.fact_extractor import Evidence, FactCandidate, EV_SQL, EV_TABLE
from src.fact_case_generator import FactCaseGenerator
from src.index_builder import IndexBuilder


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    """scope=module 建一次库(11 张表 executescript 约 1.7s), 每例拷副本"""
    base = tmp_path_factory.mktemp("cases") / "_index.db"
    with IndexBuilder(base, "customer-service") as b:
        b.commit()
    return base


@pytest.fixture
def b(db, tmp_path):
    """每个测试一份独立副本 —— 避免用例之间互相污染计数"""
    p = tmp_path / "_index.db"
    p.write_bytes(db.read_bytes())
    with IndexBuilder(p, "customer-service") as builder:
        yield builder
        builder.commit()


def _fact(title="客服异常自动下线", cons="禁止自动上线", with_table=True):
    evs = [
        Evidence(ev_type="requirement", ref_uri="1.json#background:L1",
                 snippet="客服异常自动下线", ev_group="req:1.json#background:L1"),
        Evidence(ev_type=EV_SQL,
                 ref_uri="AgentConfMapper.updateStatusByAgentId@m.java:12",
                 snippet="UPDATE agent_conf SET status=?",
                 ev_group="sql:AgentConfMapper.updateStatusByAgentId"),
    ]
    if with_table:
        evs.append(Evidence(ev_type=EV_TABLE, ref_uri="agent_conf",
                            snippet="agent_conf", ev_group="tbl:agent_conf"))
    return FactCandidate(
        title=title, condition="心跳异常", action="自动下线",
        rule_constraint=cons, evidence=evs, confidence=0.8)


# ============================================================
# DDL: 这张表曾经根本不存在
# ============================================================

def test_cases_table_exists(b):
    """
    DDL 必须在。历史上 sql_index 就是"有 add_sql_statement 方法、没有建表语句",
    结果 1427 条 SQL 一条都落不了库, 且全程零报错。
    """
    names = {r[0] for r in b._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "cases" in names


def test_add_cases_api_exists(b):
    assert callable(getattr(b, "add_cases", None))
    assert callable(getattr(b, "get_cases", None))
    assert callable(getattr(b, "search_cases", None))


# ============================================================
# 写入
# ============================================================

def test_add_cases_persists_body(b):
    """用例正文必须真的落库 —— 只存映射的话"怎么测的"这问答不出来"""
    cases = FactCaseGenerator().generate([_fact()])
    assert cases, "生成器应至少产出一条用例"
    n = b.add_cases(cases, task_id="t-1")
    assert n == len(cases)

    got = b.get_cases()
    assert len(got) == len(cases)
    c = got[0]
    assert c["title"]
    assert c["expected_result"]
    assert c["dimension"]
    # JSON 列必须解码回 list, 不是字符串
    assert isinstance(c["steps"], list) and c["steps"]
    assert isinstance(c["assertions"], list) and c["assertions"]


def test_add_cases_auto_links_case_fact_map(b):
    """
    add_cases 必须自动回填 case_fact_map。

    只写 cases 不写映射 -> 覆盖报告全线报"未覆盖", 而且不报错。
    这是最隐蔽的一种静默失配, 所以绑在同一个方法里。
    """
    fact = _fact()
    ok, why = b.add_fact(fact)
    assert ok, why
    cases = FactCaseGenerator().generate([fact])
    b.add_cases(cases)

    rep = b.fact_coverage_report()
    assert rep["fact_total"] == 1
    assert rep["fact_covered"] == 1, "用例已入库却报未覆盖 = 映射没写"
    assert rep["coverage_rate"] == 100.0


def test_determinacy_computed_on_write(b):
    """生成期体检结论要落库, 否则该指标只能靠重跑生成才能拿到"""
    cases = FactCaseGenerator().generate([_fact()])
    b.add_cases(cases)
    for c in b.get_cases():
        assert c["determinacy"] in ("ok", "vague", "empty")


def test_vague_case_flagged(b):
    """含模糊表述的预期必须被标出来, 而不是与合格用例混在一起"""
    from src.fact_case_generator import GeneratedCase
    vague = GeneratedCase(
        case_id="V-1", fact_id="f1", title="t", dimension="happy_path",
        dimension_name="功能流程", priority="P0",
        expected_result="页面正常显示", assertions=["a"])
    ok = GeneratedCase(
        case_id="V-2", fact_id="f1", title="t", dimension="happy_path",
        dimension_name="功能流程", priority="P0",
        expected_result="HTTP 200 且 status 变为 3", assertions=["a"])
    b.add_cases([vague, ok])
    got = {c["case_id"]: c for c in b.get_cases()}
    assert got["V-1"]["determinacy"] == "vague"
    assert got["V-1"]["determinacy_issues"]
    assert got["V-2"]["determinacy"] == "ok"


def test_reinsert_is_idempotent(b):
    """重复生成同一批用例(重跑流水线)不能翻倍"""
    cases = FactCaseGenerator().generate([_fact()])
    b.add_cases(cases)
    b.add_cases(cases)
    assert len(b.get_cases()) == len(cases)


# ============================================================
# 引用回填: 靠 Fact 的证据, 不靠外部映射表
# ============================================================

def test_code_and_table_refs_from_evidence(b):
    """
    code_refs / table_refs 必须能从 Fact 证据自动推导。

    不这么做的后果: 流水线要额外维护一份 fact_id -> 出处 的映射表,
    那份映射一旦和 Fact 对不上, 用例就挂着错误出处且不报错 ——
    用例会照着错出处去断言真实业务字段。
    """
    fact = _fact()
    cases = FactCaseGenerator().generate([fact])
    b.add_cases(cases)
    c = b.get_cases()[0]
    assert "AgentConfMapper.updateStatusByAgentId@m.java:12" in c["code_refs"]
    assert "agent_conf" in c["table_refs"]


def test_explicit_refs_override_evidence(b):
    """显式传入的映射优先于自动推导"""
    from src.fact_extractor import fact_id_of
    fact = _fact()
    fid = fact_id_of(fact)
    cases = FactCaseGenerator().generate(
        [fact], code_refs={fid: ["XxxMapper.custom@m.java:1"]})
    b.add_cases(cases)
    assert b.get_cases()[0]["code_refs"] == ["XxxMapper.custom@m.java:1"]


# ============================================================
# 检索
# ============================================================

def test_search_cases_by_chinese(b):
    """
    中文检索必须能命中。

    FTS5 的 unicode61 分词器对中文整句切一个 token, 实测必然 0 命中,
    所以这里刻意不走 FTS。若有人改成 FTS, 这条测试会拦住。
    """
    cases = FactCaseGenerator().generate([_fact()])
    b.add_cases(cases)
    hits = b.search_cases("禁止自动上线")
    assert hits, "中文业务词检索必须命中, 0 命中会让问答器开始编"
    assert any("禁止自动上线" in (h["expected_result"] or "")
               or "禁止自动上线" in (h["title"] or "")
               or any("禁止自动上线" in a for a in h["assertions"])
               for h in hits)


def test_search_cases_titles_weighted(b):
    """标题命中的应排在前面 —— 用户问的就是标题里的说法"""
    from src.fact_case_generator import GeneratedCase
    a = GeneratedCase(case_id="C-1", fact_id="f1", title="无关内容",
                      dimension="happy_path", dimension_name="功能流程",
                      priority="P0", expected_result="HTTP 200 status=1",
                      assertions=["x"])
    bcase = GeneratedCase(case_id="C-2", fact_id="f2",
                          title="禁止自动上线校验",
                          dimension="forbid", dimension_name="禁止类约束",
                          priority="P0", expected_result="HTTP 403 拒绝",
                          assertions=["y"])
    b.add_cases([a, bcase])
    hits = b.search_cases("禁止自动上线")
    assert hits[0]["case_id"] == "C-2"


def test_search_cases_empty_query(b):
    assert b.search_cases("") == []
    assert b.search_cases("   ") == []


def test_search_cases_miss_returns_empty(b):
    """查不到必须返回空, 绝不能退化成"猜一条最相关的" """
    b.add_cases(FactCaseGenerator().generate([_fact()]))
    assert b.search_cases("完全无关的词zzzz") == []


# ============================================================
# 过滤
# ============================================================

def test_table_refs_read_snippet_not_ref_uri():
    """
    table_refs 必须取 snippet 里的真实表名, 不能取 ref_uri。

    真实字段布局: EV_TABLE 的 ref_uri = SQL id(为溯源),
    snippet = CSV 表名(space 逗号分隔)。取错字段的表现很隐蔽 ——
    table_refs 装满 Mapper 方法名, data_consistency 维度的"涉及表"
    变成一串 qualified_stmt, 表级断言立不住, 但看起来"有引用"。
    """
    fact = FactCandidate(
        title="状态变更", condition="心跳异常", action="下线",
        evidence=[
            Evidence(ev_type="requirement", ref_uri="1.json#background:L1",
                     snippet="状态异常时下线", ev_group="g1"),
            Evidence(ev_type=EV_SQL,
                     ref_uri="XxxMapper.updateStatus@XxxMapper.xml:12",
                     snippet="UPDATE t SET status=?",
                     ev_group="g2"),
            Evidence(ev_type=EV_TABLE,
                     ref_uri="ai.yiye.dao.XxxMapper.updateStatus",
                     snippet="agent_conf,boss_order", ev_group="g3"),
        ])
    cases = FactCaseGenerator().generate([fact])
    c = [x for x in cases if x.dimension == "data_consistency"][0]
    assert sorted(c.table_refs) == ["agent_conf", "boss_order"]
    assert not any("Mapper" in t for t in c.table_refs), \
        "table_refs 里混进了 Mapper 方法名"


def test_table_refs_fallback_to_ref_uri_when_snippet_empty():
    """snippet 为空时退回 ref_uri —— 总比没有出处强"""
    fact = FactCandidate(
        title="x", condition="c", action="a",
        evidence=[
            Evidence(ev_type=EV_TABLE, ref_uri="XxxMapper.q", snippet="",
                     ev_group="g1"),
            Evidence(ev_type=EV_SQL, ref_uri="XxxMapper.q@m.xml:1",
                     snippet="SELECT 1", ev_group="g2"),
        ])
    cases = FactCaseGenerator().generate([fact])
    c = [x for x in cases if x.dimension == "data_consistency"][0]
    assert c.table_refs == ["XxxMapper.q"]


def test_get_cases_filter_by_fact(b):
    cases = FactCaseGenerator().generate([_fact()])
    b.add_cases(cases)
    fid = cases[0].fact_id
    assert len(b.get_cases(fact_id=fid)) == len(cases)
    assert b.get_cases(fact_id="no-such-fact") == []


def test_get_cases_filter_by_dimension(b):
    b.add_cases(FactCaseGenerator().generate([_fact()]))
    happy = b.get_cases(dimension="happy_path")
    assert happy
    assert all(c["dimension"] == "happy_path" for c in happy)


def test_get_cases_filter_by_task(b):
    cases = FactCaseGenerator().generate([_fact()])
    b.add_cases(cases, task_id="task-A")
    assert b.get_cases(task_id="task-A")
    assert b.get_cases(task_id="task-B") == []


# ============================================================
# 格式一致性(CSV/JSON 混用的老坑)
# ============================================================

def test_csv_columns_roundtrip(b):
    """
    code_refs/table_refs/determinacy_issues 是 CSV 列, 读取侧必须按 CSV 解。

    早期 `tables` 列存 CSV 而读取侧按 JSON 解析, 永远 0 命中且不报错,
    表现成"这个表没被任何 SQL 碰过"。这条测试钉死读写格式一致。
    """
    from src.fact_case_generator import GeneratedCase
    c = GeneratedCase(case_id="R-1", fact_id="f1", title="t",
                      dimension="happy_path", dimension_name="功能流程",
                      priority="P0", expected_result="HTTP 200 x=1",
                      assertions=["a"], code_refs=["A.b@c:1", "D.e@f:2"],
                      table_refs=["t1", "t2"])
    b.add_cases([c])
    got = b.get_cases()[0]
    assert got["code_refs"] == ["A.b@c:1", "D.e@f:2"]
    assert got["table_refs"] == ["t1", "t2"]


def test_json_columns_survive_chinese(b):
    """中文 JSON 列往返不能乱码(历史上字节/字符偏移混用踩过同类坑)"""
    from src.fact_case_generator import GeneratedCase
    c = GeneratedCase(case_id="J-1", fact_id="f1", title="禁止自动上线",
                      dimension="forbid", dimension_name="禁止类约束",
                      priority="P0", expected_result="HTTP 403 拒绝上线",
                      assertions=["状态保持不变"],
                      preconditions=["满足条件: 心跳异常"],
                      steps=[{"step": 1, "action": "触发", "expected": "4xx"}])
    b.add_cases([c])
    got = b.get_cases()[0]
    assert got["steps"][0]["action"] == "触发"
    assert got["preconditions"][0] == "满足条件: 心跳异常"
    assert got["assertions"] == ["状态保持不变"]


def test_empty_expected_flagged_not_crashed(b):
    """空预期不能崩, 也不能被当成 ok"""
    from src.fact_case_generator import GeneratedCase
    c = GeneratedCase(case_id="E-1", fact_id="f1", title="t",
                      dimension="happy_path", dimension_name="功能流程",
                      priority="P0", expected_result="")
    b.add_cases([c])
    assert b.get_cases()[0]["determinacy"] == "empty"


def test_case_without_fact_id_not_linked(b):
    """没有 fact_id 的用例仍要入库正文, 只是不参与覆盖统计"""
    from src.fact_case_generator import GeneratedCase
    c = GeneratedCase(case_id="N-1", fact_id="", title="孤立用例",
                      dimension="happy_path", dimension_name="功能流程",
                      priority="P0", expected_result="HTTP 200 x=1")
    assert b.add_cases([c]) == 1
    assert len(b.get_cases()) == 1
    assert b.fact_coverage_report()["fact_total"] == 0


# ============================================================
# 重跑累计: 一条用例不得挂多个 fact_id
# ============================================================

def test_rerun_does_not_duplicate_fact_link(b):
    """
    重跑生成不得让一条用例同时挂两个 fact_id。

    真实故障: case_id 是 `FACT-0001` 这种序号, 每轮重新生成都会复用同一批
    编号; 而 fact_id 是内容哈希, 需求文件一变就变。case_fact_map 主键是
    (case_id, fact_id), INSERT OR REPLACE **覆盖不掉另一条**, 于是同一个
    FACT-0004 同时挂着上一轮和这一轮的 fact_id。
    实测 customer-service 重跑后 138 条用例里 98 条中招 ——
    而症状只是覆盖数虚高, 全程不报错。
    """
    from src.fact_case_generator import GeneratedCase
    def mk(fid):
        return GeneratedCase(case_id="R-0001", fact_id=fid, title="t",
                             dimension="happy_path", dimension_name="功能流程",
                             priority="P0", expected_result="HTTP 200 x=1")
    b.add_cases([mk("F-old")])
    b.add_cases([mk("F-new")])

    rows = b._conn.execute(
        "SELECT fact_id FROM case_fact_map WHERE case_id='R-0001'"
    ).fetchall()
    assert [r[0] for r in rows] == ["F-new"], "旧 fact_id 未被清掉"
    # 正文里的 fact_id 也必须跟着更新
    assert b.get_cases()[0]["fact_id"] == "F-new"


def test_each_case_maps_to_exactly_one_fact(b):
    """全库校验: 不存在一条用例挂多个 fact_id"""
    b.add_cases(FactCaseGenerator().generate([_fact()]))
    bad = b._conn.execute(
        "SELECT case_id, count(*) n FROM case_fact_map "
        "GROUP BY case_id HAVING n > 1"
    ).fetchall()
    assert bad == [], f"以下用例挂了多个 fact_id: {bad}"


def test_clear_fact_layer_keeps_symbols(tmp_path):
    """
    clear_fact_layer 只清 Fact 层, 符号索引必须留着。

    这是 backfill_facts 工具存在的意义: 解析 9000+ 文件建符号索引要几十分钟,
    重跑 Fact 层时不能把它一起清掉。
    """
    from src.code_parser import CodeChunk
    p = tmp_path / "_index.db"
    with IndexBuilder(p, "customer-service") as b:
        b.add_chunk(CodeChunk(
            chunk_id="s1", file_path="A.java", file_type="java",
            language="java", chunk_type="method", name="m",
            qualified_name="C.m", line_start=1, line_end=2,
            code_content="void m(){}"))
        b.add_fact(_fact())
        b.add_cases(FactCaseGenerator().generate([_fact()]))

        n = b.clear_fact_layer()

        assert n == 1, "应清掉 1 条事实"
        assert b._conn.execute("SELECT count(*) FROM symbols").fetchone()[0] == 1
        assert b._conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 1
        assert b._conn.execute("SELECT count(*) FROM facts").fetchone()[0] == 0
        assert b._conn.execute("SELECT count(*) FROM cases").fetchone()[0] == 0


def test_clear_domain_removes_facts_and_cases(tmp_path):
    """
    clear_domain 必须把 Fact 层一起清掉。

    不清的后果: 重跑构建后历史事实与悬挂映射全部残留, 覆盖报告的分母
    只增不减, 且没有任何报错提示。
    """
    p = tmp_path / "_index.db"
    with IndexBuilder(p, "customer-service") as b:
        fact = _fact()
        ok, _ = b.add_fact(fact)
        assert ok
        b.add_cases(FactCaseGenerator().generate([fact]))
        assert b.fact_coverage_report()["fact_total"] == 1

        b.clear_domain()

        for t in ("facts", "fact_evidence", "cases", "case_fact_map"):
            n = b._conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            assert n == 0, f"{t} 未被 clear_domain 清理, 残留 {n} 行"


def test_clear_domain_isolates_other_domains(tmp_path):
    """
    清一个域不能误删别的域的 Fact。

    注意不能嵌套开第二个 IndexBuilder —— 两个连接写同一个 SQLite 文件
    会互相锁死(OperationalError: database is locked)。
    这里借用同一连接的 _conn 临时改 self.domain 来模拟写入另一个域。
    """
    p = tmp_path / "_index.db"
    with IndexBuilder(p, "customer-service") as b:
        b.add_fact(_fact())
        # 临时切到 payment 域写入
        b.domain = "payment"
        b.add_fact(_fact(title="支付回调幂等"))
        b.domain = "customer-service"

        assert b._conn.execute(
            "SELECT count(*) FROM facts WHERE domain='payment'"
        ).fetchone()[0] == 1

        b.clear_domain()   # 只清 customer-service

        assert b._conn.execute(
            "SELECT count(*) FROM facts WHERE domain='customer-service'"
        ).fetchone()[0] == 0
        assert b._conn.execute(
            "SELECT count(*) FROM facts WHERE domain='payment'"
        ).fetchone()[0] == 1, "清一个域误删了另一个域的 Fact"
        assert b._conn.execute(
            "SELECT count(*) FROM fact_evidence WHERE fact_id NOT IN "
            "(SELECT fact_id FROM facts)"
        ).fetchone()[0] == 0, "留下了无主的证据行"
