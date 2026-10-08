# -*- coding: utf-8 -*-
"""
业务问答器测试

重点测三件事:
  1. 问句分类是确定性的(同一问题两次判定必须一致)
  2. 无出处不回答 —— 检索不到就明说, 不生成通用解释
  3. 不猜起跳点 —— 没给表名就不给表的影响面
"""
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.business_qa import (
    Answer, BusinessQA, Citation, QType, classify, _dedup_cites,
)
from src.index_builder import IndexBuilder


# ============================================================
# 夹具: 造一个够小但结构完整的索引库
# ============================================================

@pytest.fixture(scope="module")
def _kb_master(tmp_path_factory):
    """
    构造含 facts / fact_evidence / case_fact_map / sql_index 的索引库。
    刻意只放 2 条事实、1 条已覆盖 —— 让"未覆盖"分支能跑起来。

    scope="module": 建库要 executescript 11 张表, 实测约 1.7 秒,
    每个测例各建一份的话 40 个测试就是 70 秒。改成建一次、每例拷一份。
    """
    from src.fact_extractor import Evidence, FactCandidate, fact_id_of

    db = tmp_path_factory.mktemp("kb") / "_index.db"
    b = IndexBuilder(db, "demo")
    b.__enter__()

    def mk(title, cond, act, cons):
        f = FactCandidate(title=title, condition=cond, action=act,
                          rule_constraint=cons,
                          confidence=0.9, source_kind="requirement")
        f.evidence = [
            Evidence(ev_type="requirement", ref_uri="requirements/1.json#seg2",
                     snippet=cons or title,
                     ev_group=f"req-{fact_id_of(f)}", weight=1.0),
            Evidence(ev_type="code", ref_uri="AgentConfService.java:120",
                     snippet="updateStatusByAgentId",
                     ev_group=f"code-{fact_id_of(f)}", weight=1.0),
        ]
        return f

    f1 = mk("客服异常时自动下线", "客服连续异常", "自动下线",
            "禁止自动上线")
    f2 = mk("客服上线需人工确认", "提交上线申请", "人工点确认",
            "必须人工点确认才能上线")
    assert b.add_fact(f1)[0], "F1 应入库"
    assert b.add_fact(f2)[0], "F2 应入库"
    fid1, fid2 = fact_id_of(f1), fact_id_of(f2)
    b.link_cases_to_facts([("CASE-001", fid1)], task_id="t1")

    # sql_index: 一条高可信 + 一条动态(低可信)
    # 走 add_sql_statement, 与生产写入路径完全一致 —— 手写 INSERT 曾经
    # 把 tables 写成 JSON, 结果与真实数据(CSV)不一致, 测试全绿而线上失效。
    from src.xml_parser import SqlStatement
    b.add_sql_statement(SqlStatement(
        stmt_id="updateStatusByAgentId", stmt_type="update",
        namespace="AgentConfMapper",
        sql_text="UPDATE agent_conf SET status=? WHERE agent_id=?",
        tables=["agent_conf"], is_dynamic=False,
        has_external_include=False, params=["status", "agent_id"],
        file_path="AgentConfMapper.xml", line_start=42))
    b.add_sql_statement(SqlStatement(
        stmt_id="selectByCond", stmt_type="select",
        namespace="OrderMapper",
        sql_text="SELECT * FROM order_info WHERE <if>x</if>",
        tables=["order_info"], is_dynamic=True,
        has_external_include=False, params=[],
        file_path="OrderMapper.xml", line_start=10))
    b.commit()
    # 用于测试断言真实 fact_id(由标题哈希得出, 不是手写的 F-001)
    b.test_facts = {"下线": fid1, "上线": fid2}
    b._conn.close()
    yield db
    Path(db).unlink(missing_ok=True)


@pytest.fixture()
def kb(_kb_master, tmp_path):
    """每个测例拿一份独立拷贝 —— 测例会删 facts / 改 snippet, 不能共享"""
    import shutil
    dst = tmp_path / "_index.db"
    shutil.copy(_kb_master, dst)
    b = IndexBuilder(dst, "demo")
    b.__enter__()
    b.test_facts = {"下线": "?", "上线": "?"}
    # 拷贝后重新取 id(主库里算过一次, 这里不重复算)
    for f in b.get_facts(domain="demo"):
        if "下线" in f["title"]:
            b.test_facts["下线"] = f["fact_id"]
        if "上线" in f["title"]:
            b.test_facts["上线"] = f["fact_id"]
    yield b
    b.__exit__(None, None, None)


# ============================================================
# 问句分类
# ============================================================

class TestClassify:

    @pytest.mark.parametrize("q,expect", [
        # 影响面必须排在规则前面: 「为什么改表会影响上线」要答影响面
        ("改 agent_conf 表影响哪些功能", QType.IMPACT),
        ("动了这个字段会波及哪些模块", QType.IMPACT),
        ("为什么不能自动上线", QType.RULE),
        ("凭什么禁止自动上线", QType.RULE),
        ("这个操作允许吗", QType.RULE),
        ("有没有测过自动下线", QType.COVERAGE),
        ("这些用例覆盖了吗", QType.COVERAGE),
        ("前台和后台对不上", QType.CONSISTENCY),
        ("updateStatusByAgentId 在哪", QType.LOCATE),
        ("随便聊聊", QType.UNKNOWN),
    ])
    def test_classify(self, q, expect):
        assert classify(q) == expect

    def test_classify_is_deterministic(self):
        """分类必须可复现 —— 否则同一问题两次问得到两个答案"""
        qs = ["改 agent_conf 表影响哪些功能", "为什么不能自动上线",
              "有没有测过自动下线", "前台和后台对不上"]
        for q in qs:
            assert len({classify(q) for _ in range(5)}) == 1, q

    def test_impact_beats_rule_when_both_match(self):
        """混合问句必须判成影响面, 否则答非所问"""
        assert classify("为什么改这个表会影响自动上线") == QType.IMPACT


# ============================================================
# 铁律: 无出处不回答
# ============================================================

class TestNoGroundingNoAnswer:

    def test_unknown_question_does_not_fabricate(self, kb):
        """分不了类 + 无命中 -> 必须明确说没底, 不能编"""
        ans = BusinessQA(kb).ask("这个东西是怎么运作的")
        assert not ans.answer or "未能判定" in ans.answer
        assert ans.confidence in ("none", "low")
        assert ans.unresolved, "无出处时必须列出未解决的问题"

    def test_no_table_guess_for_impact(self, kb):
        """问题里没表名 -> 不能瞎给一张表的影响面"""
        ans = BusinessQA(kb).ask("改了会影响什么")
        assert ans.qtype == QType.IMPACT
        # 要么明确报"没识别出起跳点", 要么给出的表确实在问句里
        assert ans.unresolved or ans.citations

    def test_unresolved_when_no_facts(self, kb):
        """库里一条 fact 都没有 -> 覆盖度问题必须报"尚未建立业务事实" """
        import src.index_builder as ib
        cur = kb._conn.cursor()
        cur.execute("DELETE FROM facts")
        cur.execute("DELETE FROM case_fact_map")
        kb.commit()
        ans = BusinessQA(kb).ask("有没有测过自动下线")
        assert any("尚未建立业务事实" in u for u in ans.unresolved)


# ============================================================
# 规则解释路
# ============================================================

class TestRuleQA:

    def test_answer_uses_constraint_not_code(self, kb):
        """答案主体必须是需求约束句, 不是模型对代码的解释"""
        ans = BusinessQA(kb).ask("为什么不能自动上线")
        assert ans.qtype == QType.RULE
        assert "禁止自动上线" in ans.answer
        assert ans.confidence == "high"

    def test_cites_requirement_source(self, kb):
        """必须带需求出处 —— 业务方要能拿去对账"""
        ans = BusinessQA(kb).ask("为什么不能自动上线")
        kinds = {c.kind for c in ans.citations}
        assert "fact" in kinds
        assert any("requirements/" in c.ref for c in ans.citations)

    def test_flags_fact_without_constraint(self, kb):
        """命中事实但没约束句 -> 必须承认答不了「为什么」"""
        import src.index_builder as ib
        cur = kb._conn.cursor()
        cur.execute("UPDATE facts SET rule_constraint=''")
        kb.commit()
        ans = BusinessQA(kb).ask("为什么不能自动上线")
        assert any("没有显式约束句" in u for u in ans.unresolved)
        assert ans.confidence != "high"


# ============================================================
# 影响面路(多跳)
# ============================================================

class TestImpactQA:

    def test_table_to_sql_to_fact(self, kb):
        """表 -> SQL -> 标识符 -> 事实 四跳都要有出处"""
        ans = BusinessQA(kb).ask("改 agent_conf 表影响哪些功能")
        assert ans.qtype == QType.IMPACT
        kinds = {c.kind for c in ans.citations}
        assert {"table", "sql", "fact"} <= kinds, f"缺引用类型: {kinds}"
        assert "AgentConfMapper.updateStatusByAgentId" in ans.answer
        assert ans.confidence == "high"

    def test_bridge_is_literal_not_semantic(self, kb):
        """
        桥接必须是字面匹配, 不能靠语义相似度。
        摘掉代码证据后桥接就该断 —— 断得干净比连错强。
        """
        assert kb.find_facts_by_symbols(["updateStatusByAgentId"])
        cur = kb._conn.cursor()
        cur.execute("UPDATE fact_evidence SET snippet='与该标识符无关的文本'")
        kb.commit()
        assert not kb.find_facts_by_symbols(["updateStatusByAgentId"])

    def test_flags_low_confidence_sql(self, kb):
        """动态 SQL 表名可信度低 -> 必须提示, 不能当确定结论"""
        ans = BusinessQA(kb).ask("改 order_info 表影响哪些功能")
        assert any("可信度" in u for u in ans.unresolved), ans.unresolved
        assert ans.confidence != "high"

    def test_unknown_table_returns_none(self, kb):
        """库里没有的表 -> 明确报低可信, 不编影响面"""
        ans = BusinessQA(kb).ask("改 not_exist_tbl 表影响哪些功能")
        assert ans.confidence in ("low", "none")
        assert ans.unresolved

    def test_impact_needs_jump_point(self, kb):
        """既无表名又无符号 -> 直接说识别不出起跳点"""
        ans = BusinessQA(kb).ask("这个改动波及哪些地方呢")
        assert ans.qtype == QType.IMPACT
        assert not ans.citations
        assert len(ans.unresolved) >= 2


# ============================================================
# 覆盖度路
# ============================================================

class TestCoverageQA:

    def test_reports_uncovered_fact(self, kb):
        """没回填用例的那条事实 -> 必须出现在未覆盖清单里"""
        ans = BusinessQA(kb).ask("有没有测过客服上线")
        assert ans.qtype == QType.COVERAGE
        assert kb.test_facts["上线"] in ans.answer

    def test_coverage_rate_computed(self, kb):
        ans = BusinessQA(kb).ask("这些用例覆盖了吗")
        m = re.search(r"(\d+)/(\d+)", ans.answer)
        assert m, ans.answer
        assert m.group(1) == "1" and m.group(2) == "2"

    def test_warns_coverage_is_not_quality(self, kb):
        """覆盖 != 断言正确 —— 这个区分必须说出来"""
        ans = BusinessQA(kb).ask("有没有测过自动下线")
        assert any("断言" in u for u in ans.unresolved)


# ============================================================
# 一致性路
# ============================================================

class TestConsistencyQA:

    def test_lists_access_points(self, kb):
        ans = BusinessQA(kb).ask("agent_conf 前台后台对不上")
        assert ans.qtype == QType.CONSISTENCY
        assert "updateStatusByAgentId" in ans.answer

    def test_refuses_to_judge_consistency(self, kb):
        """"不一致"是业务判断, 不能靠检索自动下结论"""
        ans = BusinessQA(kb).ask("agent_conf 前台后台对不上")
        assert any("人工" in u for u in ans.unresolved)


# ============================================================
# 证据门禁(加_fact 拒绝幻觉)
# ============================================================

class TestFactGate:

    def _mk_fact(self, ev_groups):
        from src.fact_extractor import Evidence, FactCandidate
        f = FactCandidate(title="updateStatusByAgentId 修改 agent_conf 状态",
                          confidence=1.0, source_kind="requirement")
        f.evidence = [
            Evidence(ev_type="requirement", ref_uri="r.json#s1",
                     snippet="updateStatusByAgentId 更新 agent_conf 状态",
                     ev_group=g, weight=1.0)
            for g in ev_groups
        ]
        return f

    def test_rejects_single_evidence(self, kb):
        """同源证据(原文+翻译稿)只算 1 条, 不足门槛必须拒收"""
        ok, why = kb.add_fact(self._mk_fact(["g1"]))
        assert not ok and "证据不足" in why

    def test_accepts_two_distinct_groups(self, kb):
        ok, why = kb.add_fact(self._mk_fact(["g1", "g2"]))
        assert ok, why

    def test_rejects_hallucinated_identifier(self, kb):
        """证据里没有的标识符 -> 幻觉, 拒收"""
        from src.fact_extractor import Evidence, FactCandidate
        f = FactCandidate(title="调用 nonexistentMethodXxx 处理支付回调",
                          confidence=1.0, source_kind="requirement")
        f.evidence = [
            Evidence(ev_type="requirement", ref_uri="r.json#s1",
                     snippet="支付回调由 order 表处理", ev_group="g1", weight=1.0),
            Evidence(ev_type="code", ref_uri="PayService.java:1",
                     snippet="payCallback", ev_group="g2", weight=1.0),
        ]
        ok, why = kb.add_fact(f)
        assert not ok and "幻觉" in why

    def test_rejected_fact_not_persisted(self, kb):
        """拒收的事实不能残留在库里"""
        from src.fact_extractor import fact_id_of
        f = self._mk_fact(["g1"])
        kb.add_fact(f)
        ids = {x["fact_id"] for x in kb.get_facts(domain="demo")}
        assert fact_id_of(f) not in ids, "拒收的事实不能残留在库里"


# ============================================================
# sql_index 落地(P0 回归: 表从未建过)
# ============================================================

class TestSqlIndexPersisted:

    def test_sql_index_table_exists(self, kb):
        cur = kb._conn.cursor()
        n = cur.execute("SELECT count(*) FROM sql_index").fetchone()[0]
        assert n == 2

    def test_find_by_table(self, kb):
        rows = kb.find_sql_by_table("agent_conf")
        assert len(rows) == 1
        assert rows[0]["table_confidence"] == "high"
        assert "agent_conf" in rows[0]["tables"]

    def test_get_by_qualified_stmt(self, kb):
        rows = kb.get_tables_for_stmt("AgentConfMapper.updateStatusByAgentId")
        assert len(rows) == 1
        assert rows[0]["tables"] == ["agent_conf"]
        assert set(rows[0]["params"]) == {"status", "agent_id"}


# ============================================================
# Answer 渲染
# ============================================================

class TestAnswerRender:

    def test_render_includes_citations_and_confidence(self, kb):
        text = BusinessQA(kb).ask("为什么不能自动上线").render()
        assert "出处:" in text
        assert "可信度" in text
        assert "检索路径" in text

    def test_render_lists_unresolved(self, kb):
        text = BusinessQA(kb).ask("改 order_info 表影响哪些功能").render()
        assert "证据不足的部分" in text

    def test_empty_answer_says_so(self):
        a = Answer(question="q", qtype=QType.UNKNOWN)
        assert "未查到有出处的答案" in a.render()
        assert not a.grounded

    def test_dedup_cites(self):
        c = [Citation("code", "a:1", "x"), Citation("code", "a:1", "x"),
             Citation("code", "b:2", "y")]
        assert len(_dedup_cites(c)) == 2

# ============================================================
# sql_index 存储格式(回归)
# ============================================================
#
# 这组守的是一个静默失效的 bug:
#   tables 列存的是 CSV(见 SqlStatement.to_row), 早期 schema 注释却写成
#   JSON 数组, 读取侧照 JSON 解析 -> 全部失败 -> find_sql_by_table 永远
#   0 命中, 而程序不报错。表现是"改这张表影响什么"永远答不出,
#   很容易被误判成"这个表没被任何 SQL 碰过"。

class TestSqlStorageFormat:

    def test_tables_is_csv_not_json(self, kb):
        cur = kb._conn.cursor()
        raw = cur.execute(
            "SELECT tables FROM sql_index WHERE qualified_stmt=?",
            ("AgentConfMapper.updateStatusByAgentId",)).fetchone()[0]
        assert not raw.strip().startswith("["), \
            f"tables 应存 CSV, 实得 {raw!r}"
        assert raw == "agent_conf"

    def test_find_by_table_uses_token_boundary(self, kb):
        """
        必须按逗号边界匹配。
        用子串的话 LIKE '%order%' 会把 order_info 一起捞出来,
        得出"改 order 表影响 order_info"这种误导性结论。
        """
        kb.add_sql_statement(_mkstmt("M2.selectOrder", ["order", "user"]))
        kb.commit()
        hit = kb.find_sql_by_table("order")
        assert [h["qualified_stmt"] for h in hit] == ["M2.selectOrder"], \
            "order 不应匹配到只含 order_info 的那条"
        assert not kb.find_sql_by_table("orde"), \
            "表名必须完整匹配, 不能前缀命中"

    def test_prefix_table_not_matched(self, kb):
        """库里只有 order_info 时, 查 order 必须 0 命中"""
        assert not kb.find_sql_by_table("order")
        kb.add_sql_statement(_mkstmt("M3.x", ["order_info"]))
        kb.commit()
        assert not kb.find_sql_by_table("order")
        assert kb.find_sql_by_table("order_info")


def _mkstmt(qs, tables, conf="high"):
    from src.xml_parser import SqlStatement
    st = SqlStatement(stmt_id=qs.split(".")[-1], stmt_type="select",
                      namespace=qs.rsplit(".", 1)[0], sql_text="select 1",
                      tables=tables, is_dynamic=False,
                      has_external_include=False, params=[],
                      file_path="M.xml", line_start=1)
    return st
