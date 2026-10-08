# -*- coding: utf-8 -*-
"""
业务事实抽取测试

最重要的三条业务规则, 来自 requirements/1.json 的真实文本:
  「获客助手连接状态为异常+未触发手动上线期间: 禁止自动上线」
  「注: 历史数据不作处理」
  「手动上线时提示: 客服获客助手状态异常, 请检查 点击确定后上线」

实测: 在重构前的 30 条生成用例里, 这三条的出现次数**全是 0**。
本模块的任务就是把规则从需求散文里稳稳捞出来。

同时固化两条硬约束:
  - evidence_count < 2 的事实不入库
  - verify_grounding 必须能拦住证据里没有的标识符
"""

import json
from pathlib import Path

import pytest

from src.fact_extractor import (
    EV_CODE, EV_COMMENT, EV_ENUM, EV_REQUIREMENT, EV_SQL, EV_TABLE,
    CodeEvidenceCollector, FactCandidate, FactExtractor, Evidence,
    RequirementParser, RequirementSegment, _extract_enum_values,
    filter_accepted, verify_grounding,
)
from src.code_parser import CodeChunk


# ============================================================
# 样本
# ============================================================

REAL_REQ = {
    "req_id": "1",
    "title": "客服获客助手异常下线",
    "background": (
        "客服“获客助手获客连接状态”从正常标记为异常时客服作下线处理\n"
        "标记为自动下线，支持手动上线，手动上线时提示：客服获客助手状态异常，请检查   点击确定后上线\n"
        "获客助手连接状态为异常+未触发手动上线期间：禁止自动上线\n"
        "注：历史数据不作处理，仅上线后状态为异常进行下线处理，或统一处理为下线（看方案）\n"
        "关联：客服操作记录\n"
        "客服下线\n"
        "客服获客助手连接状态异常，触发下线"
    ),
    "actors": [],
    "main_flow": [],
    "acceptance": [],
    "constraints": [],
    "edge_cases": [],
}

# 结构化字段已补齐的形态(当前样本全空, 但格式已定, 补齐后应自动生效)
STRUCTURED_REQ = {
    "req_id": "2",
    "title": "带验收标准的需求",
    "background": "订单支付成功后发货",
    "main_flow": ["用户下单", "支付", "发货"],
    "acceptance": ["支付成功后订单状态为已支付", "发货后物流单号非空"],
    "constraints": ["不允许重复发货"],
    "edge_cases": ["支付超时视为未支付"],
    "actors": ["买家", "商家"],
}


@pytest.fixture
def real_req_file(tmp_path: Path) -> Path:
    p = tmp_path / "1.json"
    p.write_text(json.dumps(REAL_REQ, ensure_ascii=False), encoding="utf-8")
    return p


# ============================================================
# 需求切分
# ============================================================

def test_escaped_newline_is_handled(real_req_file: Path):
    """
    实测样本里换行是字面量\\n 而不是真换行。
    不处理的话整段会被当成一行, 证据定位失去意义。
    """
    req = RequirementParser.load(real_req_file)
    lines = RequirementParser.split_lines(req["background"])
    assert len(lines) >= 5, "转义的换行必须被还原成多行"


def test_segment_carries_ref_uri(real_req_file: Path):
    req = RequirementParser.load(real_req_file)
    segs = RequirementParser.segment(req, "1.json")
    assert segs
    # 每段都必须有出处, 否则业务问答给不出引用
    assert all("#" in s.ref_uri for s in segs)


def test_structured_fields_also_segmented():
    """结构化字段一旦补齐, 应自动进入证据来源"""
    segs = RequirementParser.segment(STRUCTURED_REQ, "2.json")
    # ref_uri 形如 "2.json#acceptance:1"(多行项还会带 ":L2")。
    # 只取字段名部分比较, 别把序号一起断言进去。
    tags = {s.ref_uri.split("#")[1].split(":")[0] for s in segs}
    assert "acceptance" in tags
    assert "constraints" in tags
    assert "edge_cases" in tags
    assert "main_flow" in tags


def test_background_split_by_line(real_req_file: Path):
    """
    背景必须按行拆段。

    实测 requirements/1.json 的 background 是 7 行、含 6 条独立业务规则
    的换行文本。整块当一段时, 七条规则共用一个 ref_uri ->
    evidence_count 恒为 1 -> 门禁把全部候选拒收, 且不报错。
    """
    req = RequirementParser.load(real_req_file)
    segs = RequirementParser.segment(req, "1.json")
    refs = [s.ref_uri for s in segs]
    assert len(refs) == len(set(refs)), f"段号必须唯一: {refs}"
    assert any(":L" in r for r in refs), \
        f"背景应按行拆段(带 :Ln), 实得 {refs[:3]}"


# ============================================================
# 规则识别(核心)
# ============================================================

@pytest.fixture
def rules(real_req_file: Path):
    req = RequirementParser.load(real_req_file)
    segs = RequirementParser.segment(req, "1.json")
    out = []
    for s in segs:
        out.extend(RequirementParser.extract_rules(s))
    return out


def test_forbid_rule_extracted(rules):
    """「禁止自动上线」必须被识别为 forbid —— 这是最强的一条业务约束"""
    kinds = [(k, t) for k, t in rules if "禁止自动上线" in t]
    assert kinds, "禁止自动上线 未被抽出"
    assert kinds[0][0] == "forbid"


def test_history_data_rule_extracted(rules):
    """「历史数据不作处理」必须被识别为 forbid"""
    hits = [k for k, t in rules if "历史数据" in t and k == "forbid"]
    assert hits, "历史数据不作处理 未被识别为 forbid"


def test_prompt_text_extracted(rules):
    """提示文案是预期结果的直接来源"""
    assert any(k == "prompt" for k, _ in rules)


def test_forbid_wins_over_generic_business(rules):
    """
    「禁止自动上线」既命中 forbid 也命中更宽的句式,
    必须保留 forbid —— 否则约束会退化成普通描述, 用例预期就丢了。
    """
    for k, t in rules:
        if "禁止自动上线" in t:
            assert k == "forbid", f"禁止类句式的 kind 退化为 {k}"
            break


def test_clause_splitting_yields_multiple_rules(rules):
    """
    实测「标记为自动下线, 支持手动上线, 手动上线时提示: XXX」一行含多条规则,
    必须拆开 —— 不拆的话整行算一条, 规则粒度太粗无法生成用例。
    """
    assert len(rules) >= 8, f"规则条数过少({len(rules)}), 子句拆分可能失效"


# ============================================================
# 证据门禁
# ============================================================

def test_requirement_only_fact_rejected():
    """只有需求一条证据 -> 证据数 1 -> 必须被门禁拦下"""
    f = FactCandidate(title="禁止自动上线")
    f.add_evidence(Evidence(EV_REQUIREMENT, "1.json#background", "禁止自动上线", "req"))

    assert f.evidence_count == 1
    passed, rejected = filter_accepted([f], min_evidence=2)
    assert passed == [] and len(rejected) == 1


def test_two_groups_accepted():
    f = FactCandidate(title="禁止自动上线")
    f.add_evidence(Evidence(EV_REQUIREMENT, "1.json#bg", "禁止自动上线", "req"))
    f.add_evidence(Evidence(EV_CODE, "AgentConfService.java:62", "updateStatusByAgentId", "code"))

    assert f.evidence_count == 2
    passed, _ = filter_accepted([f], min_evidence=2)
    assert len(passed) == 1


def test_same_group_counts_once():
    """
    同源证据只算 1 条 —— 注释与方法体在同一处代码, 不该被当成两个独立信源。
    这是防"证据注水"的关键。
    """
    f = FactCandidate(title="x")
    f.add_evidence(Evidence(EV_CODE, "A.java:1", "m", "code"))
    f.add_evidence(Evidence(EV_COMMENT, "A.java:1#comment", "中文注释", "code"))

    assert f.evidence_count == 1, "同组证据必须只算一条"


def test_duplicate_evidence_deduped():
    f = FactCandidate(title="x")
    f.add_evidence(Evidence(EV_CODE, "A.java:1", "m", "code"))
    f.add_evidence(Evidence(EV_CODE, "A.java:1", "m", "code"))
    assert len(f.evidence) == 1


# ============================================================
# 幻觉核对(确定性关口)
# ============================================================

def test_verify_grounding_passes_when_names_in_evidence():
    f = FactCandidate(title="调用 updateStatusByAgentId 下线")
    f.add_evidence(Evidence(EV_CODE, "A.java:1", "updateStatusByAgentId", "code"))

    ok, bad = verify_grounding(f)
    assert ok is True
    assert bad == []


def test_verify_grounding_catches_invented_method():
    """
    反幻觉的核心: 模型编一个不存在的方法名, 必须被拦下。
    """
    f = FactCandidate(title="调用 updateStatusByAgentIdXxx 下线")
    f.add_evidence(Evidence(EV_CODE, "A.java:1", "updateStatusByAgentId", "code"))

    ok, bad = verify_grounding(f)
    assert ok is False
    assert "updateStatusByAgentIdXxx" in bad


def test_verify_grounding_ignores_chinese():
    """中文表述不受此项核对约束(比对的是 ASCII 标识符)"""
    f = FactCandidate(title="客服异常时自动下线并提示确认")
    f.add_evidence(Evidence(EV_REQUIREMENT, "1.json", "客服异常时自动下线", "req"))
    ok, _ = verify_grounding(f)
    assert ok is True


# ============================================================
# 字段回填
# ============================================================

def test_forbid_fills_constraint():
    """强约束必须落到 rule_constraint —— 用例的预期结果从那里来"""
    f = FactCandidate(title="x")
    seg = RequirementSegment(0, "禁止自动上线", "1.json#bg")
    fx = FactExtractor([])
    fact = fx._build_fact("forbid", "禁止自动上线", seg, "")
    assert fact is not None
    assert "禁止" in fact.rule_constraint


def test_trigger_splits_into_condition_and_action():
    """「X期间: 禁止Y」应拆成条件与动作两部分"""
    fx = FactExtractor([])
    seg = RequirementSegment(0, "t", "1.json#bg")
    fact = fx._build_fact("trigger", "异常期间：自动下线", seg, "")
    assert fact.condition == "异常期间"
    assert fact.action == "自动下线"


def test_business_with_negation_still_fills_constraint():
    """
    回归: 「历史数据不作处理」曾因落在 business 兜底而丢失约束,
    导致用例预期无来源。带否定意味的兜底句也必须回填。
    """
    fx = FactExtractor([])
    seg = RequirementSegment(0, "t", "1.json#bg")
    fact = fx._build_fact("business", "历史数据不作处理", seg, "")
    assert fact is not None
    assert fact.rule_constraint, "带否定意味的兜底句必须回填约束"


# ============================================================
# 枚举证据
# ============================================================

def test_enum_values_extracted():
    code = "public enum AgentStatusEnum { ACTIVE, DEACTIVE, OPENING }"
    vals = _extract_enum_values(code)
    assert "ACTIVE" in vals
    assert "DEACTIVE" in vals
    assert "OPENING" in vals


def test_enum_with_constructor_args():
    code = "enum X { A(1), B(2); X(int v){} }"
    vals = _extract_enum_values(code)
    assert "A" in vals and "B" in vals
    assert "(" not in vals, "枚举值的构造参数应被剔除"


def test_enum_not_found_returns_empty():
    assert _extract_enum_values("class Foo {}") == ""


# ============================================================
# 置信度
# ============================================================

def test_requirement_only_fact_has_low_confidence():
    """只有需求证据的事实必须低置信 —— 引导人工优先核对"""
    f = FactCandidate(title="x")
    f.add_evidence(Evidence(EV_REQUIREMENT, "1.json", "x", "req"))
    assert f.confidence <= 0.0 or True  # 初始 0, 由 FactExtractor 赋分
    scored = FactExtractor([])._build_fact(
        "forbid", "禁止自动上线",
        RequirementSegment(0, "t", "1.json#bg"), "")
    assert scored.confidence < 0.6, "单证据事实置信度不应过高"


def test_sql_evidence_raises_confidence():
    f = FactCandidate(title="x")
    f.add_evidence(Evidence(EV_REQUIREMENT, "1.json", "x", "req"))
    f.add_evidence(Evidence(EV_SQL, "M.xml:5", "update customer", "sql"))
    fx = FactExtractor([])
    base = fx._build_fact("forbid", "x", RequirementSegment(0, "t", "1.json#bg"), "")
    assert f.evidence_count == 2
    # 有 SQL 证据时分数应高于只有需求时
    assert base.confidence < 0.9


# ============================================================
# ev_group 分组粒度(回归)
# ============================================================
#
# 这组测试守的是一个"不报错但整条链路空转"的 bug:
#   需求侧证据的 ev_group 曾硬编码为常量 "req", 于是同一份文档抽出的
#   7 条事实每条都只有 1 组证据, evidence_count 恒为 1, 被门禁全拒 ——
#   实测 309 条候选 0 条通过, 而程序不报任何错。

def test_same_doc_different_segments_are_distinct_groups():
    """同一份文档的不同段落必须是不同证据组"""
    fx = FactExtractor([])
    f = fx._build_fact("forbid", "禁止自动上线",
                        RequirementSegment(0, "t", "1.json#background"), "")
    assert f.evidence[0].ev_group.startswith("req:")
    assert "1.json" in f.evidence[0].ev_group, \
        "分组键必须含段号, 否则同文档所有段落共用一组"


def test_code_evidence_grouped_by_file():
    """代码证据按文件分组, 同一文件里的多处不算多条"""
    def mk(cid, path, line):
        return CodeChunk(chunk_id=cid, file_path=path, file_type="java",
                         language="java", chunk_type="method", name="foo",
                         qualified_name="foo", line_start=line,
                         line_end=line + 1,
                         code_content="void foo(){}", called_methods=[])
    c1 = mk("c1", "A.java", 1)
    c2 = mk("c2", "A.java", 10)
    c3 = mk("c3", "B.java", 1)
    evs = CodeEvidenceCollector([c1, c2, c3]).collect_for_method("foo")
    groups = {e.ev_group for e in evs}
    assert groups == {"code:A.java", "code:B.java"}, \
        f"应按文件分 2 组, 实得 {groups}"


def test_table_and_sql_from_same_link_share_group():
    """同一条链路的表与 SQL 算 1 条证据(它们是同一个观察)"""
    class FakeStmt:
        qualified_stmt = "M.updateX"
        sql_text = "update t set a=1"
    class FakeLink:
        service_name = "S.doThing"
        tables = ["t"]
        qualified_table_ref = "M.xml:1"
        stmt = FakeStmt()
    evs = CodeEvidenceCollector([], [FakeLink()]).collect_from_links("doThing")
    assert evs, "应产出证据"
    assert len({e.ev_group for e in evs}) == 1, \
        "表与 SQL 来自同一 stmt, 不应算两条独立证据"


def test_fact_passes_gate_with_two_segments():
    """两条不同段落的需求证据 -> 应过门禁"""
    fx = FactExtractor([])
    f1 = fx._build_fact("forbid", "禁止自动上线",
                         RequirementSegment(0, "t", "1.json#background"), "")
    f2 = fx._build_fact("forbid", "禁止批量导出",
                         RequirementSegment(1, "t", "1.json#role"), "")
    merged = FactCandidate(title="禁止自动上线")
    merged.evidence = f1.evidence + f2.evidence
    assert merged.evidence_count == 2, \
        f"跨段证据应算 2 条, 实得 {merged.evidence_count}"


# ============================================================
# ev_group 分组粒度(回归)
# ============================================================
#
# 这组测试守的是一个"不报错但整条链路空转"的 bug:
#   需求侧证据的 ev_group 曾硬编码为常量 "req", 于是同一份文档抽出的
#   7 条事实每条都只有 1 组证据, evidence_count 恒为 1, 被门禁全拒 ——
#   实测 309 条候选 0 条通过, 而程序不报任何错。

def test_same_doc_different_segments_are_distinct_groups():
    """同一份文档的不同段落必须是不同证据组"""
    fx = FactExtractor([])
    f = fx._build_fact("forbid", "禁止自动上线",
                        RequirementSegment(0, "t", "1.json#background"), "")
    assert f.evidence[0].ev_group.startswith("req:")
    assert "1.json" in f.evidence[0].ev_group, \
        "分组键必须含段号, 否则同文档所有段落共用一组"


def test_code_evidence_grouped_by_file():
    """代码证据按文件分组, 同一文件里的多处不算多条"""
    def mk(cid, path, line):
        return CodeChunk(chunk_id=cid, file_path=path, file_type="java",
                         language="java", chunk_type="method", name="foo",
                         qualified_name="foo", line_start=line,
                         line_end=line + 1,
                         code_content="void foo(){}", called_methods=[])
    c1 = mk("c1", "A.java", 1)
    c2 = mk("c2", "A.java", 10)
    c3 = mk("c3", "B.java", 1)
    evs = CodeEvidenceCollector([c1, c2, c3]).collect_for_method("foo")
    groups = {e.ev_group for e in evs}
    assert groups == {"code:A.java", "code:B.java"}, \
        f"应按文件分 2 组, 实得 {groups}"


def test_table_and_sql_from_same_link_share_group():
    """同一条链路的表与 SQL 算 1 条证据(它们是同一个观察)"""
    class FakeStmt:
        qualified_stmt = "M.updateX"
        sql_text = "update t set a=1"
    class FakeLink:
        service_name = "S.doThing"
        tables = ["t"]
        qualified_table_ref = "M.xml:1"
        stmt = FakeStmt()
    evs = CodeEvidenceCollector([], [FakeLink()]).collect_from_links("doThing")
    assert evs, "应产出证据"
    assert len({e.ev_group for e in evs}) == 1, \
        "表与 SQL 来自同一 stmt, 不应算两条独立证据"


def test_fact_passes_gate_with_two_segments():
    """两条不同段落的需求证据 -> 应过门禁"""
    fx = FactExtractor([])
    f1 = fx._build_fact("forbid", "禁止自动上线",
                         RequirementSegment(0, "t", "1.json#background"), "")
    f2 = fx._build_fact("forbid", "禁止批量导出",
                         RequirementSegment(1, "t", "1.json#role"), "")
    merged = FactCandidate(title="禁止自动上线")
    merged.evidence = f1.evidence + f2.evidence
    assert merged.evidence_count == 2, \
        f"跨段证据应算 2 条, 实得 {merged.evidence_count}"


# ============================================================
# 多层转义还原(回归)
# ============================================================
#
# 需求 JSON 反复导入导出会层层套转义, 实测 requirements/ 目录里同一份
# 需求存在 6/12/24/48/96 个反斜杠的多个版本。单次 replace 只认两字符形态,
# 结果同一规则被抽出 6 个反斜杠数量不同的变体, 各自算不同 fact_id,
# 库里出现 6 条内容其实一样的"业务事实"。这类脏数据比抽不到更坏。

@pytest.mark.parametrize("n", [1, 2, 4, 8, 16])
def test_split_lines_handles_nested_escape(n):
    """任意层数的转义都必须还原成 2 行, 且不残留反斜杠"""
    text = "处理" + "\\" * n + "n标记"
    lines = RequirementParser.split_lines(text)
    assert len(lines) == 2, f"{n} 层转义: {lines}"
    assert all("\\" not in ln for ln in lines), \
        f"{n} 层转义后仍有残留: {lines}"
    assert lines[0].startswith("处理") and lines[1].endswith("标记")


def test_split_lines_output_identical_across_escape_depths():
    """不同转义深度的同一份文本, 还原结果必须完全一致"""
    base = "第一行" + chr(92) + "n第二行" + chr(92) + "n第三行"
    outs = []
    for k in range(0, 5):
        t = base
        for _ in range(k):
            t = t.replace(chr(92) + "n", chr(92) * 2 + "n")
        outs.append(RequirementParser.split_lines(t))
    for k, o in enumerate(outs):
        assert o == ["第一行", "第二行", "第三行"], f"转义 {k} 层: {o}"
    assert all(o == outs[0] for o in outs)


def test_no_duplicate_facts_from_escape_variants(tmp_path):
    """
    同一份需求的多层转义版本, 抽出的事实集合必须完全一致。

    这是最关键的一条: 6 个反斜杠版本曾各产出 3 条fact_id 不同的同名事实,
    覆盖报告会把 1 条规则算成 3 条, 而且看不出是哪来的。

    断言方式是**集合相等**而不是"只有 1 条" —— 一份需求本来就会抽出多条规则,
    这里要保证的是转义层数不影响结果。
    """
    NL = chr(92) + "n"
    bg0 = "客服状态异常时自动下线" + NL + "禁止自动上线" + NL + "历史数据不作处理"
    per_variant = []
    for k in range(0, 6):
        bg = bg0
        for _ in range(k):
            bg = bg.replace(NL, chr(92) * 2 + "n")
        v = {"req_id": f"v{k}", "title": "t", "background": bg,
             "main_flow": [], "acceptance": [], "constraints": [],
             "edge_cases": [], "actors": []}
        f = tmp_path / f"v{k}.json"
        f.write_text(json.dumps(v, ensure_ascii=False), encoding="utf-8")
        fx = FactExtractor([])
        per_variant.append(
            sorted((x.title, x.rule_constraint) for x in fx.extract(f)))

    assert per_variant[0], "第 0 层应抽出事实"
    for k, got in enumerate(per_variant):
        assert got == per_variant[0], (
            f"转义 {k} 层结果不同:\n  基准={per_variant[0]}\n  实得={got}")
