# -*- coding: utf-8 -*-
"""
Fact 驱动用例生成器测试

重点测三件事:
  1. 七维度是**程序枚举**的, 不是靠提示词 —— 同一条事实两次生成维度必须一致
  2. 预期结果必须可判定(不许出现"正确""正常""生效")
  3. 维度不会硬凑 —— 没表证据就不生成数据一致性, 没边界信号就不生成边界值
     (凑出来的空壳用例比没有更坏, 因为它看起来覆盖了)
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.fact_extractor import Evidence, FactCandidate
from src.fact_case_generator import (
    DIMENSIONS, FactCaseGenerator, check_determinacy, cases_to_json,
)


# ============================================================
# 夹具
# ============================================================

def mkfact(title="客服异常时自动下线", cond="客服连续异常",
           act="自动下线", cons="禁止自动上线",
           ev_types=("requirement", "code"), impact=""):
    f = FactCandidate(title=title, condition=cond, action=act,
                      rule_constraint=cons, impact_scope=impact,
                      confidence=0.9, source_kind="requirement")
    for i, t in enumerate(ev_types):
        f.evidence.append(Evidence(
            ev_type=t, ref_uri=f"src#seg{i}",
            snippet="updateStatusByAgentId 更新 agent_conf 状态",
            ev_group=f"g{i}", weight=1.0))
    return f


@pytest.fixture()
def gen():
    return FactCaseGenerator()


# ============================================================
# 可判定性体检
# ============================================================

class TestDeterminacy:

    @pytest.mark.parametrize("bad", [
        "执行成功, 结果正常", "各边界值均被正确处理",
        "无权限访问均被拒绝, 有权限账号正常",
        "结果可查询验证", "数据保持一致",
    ])
    def test_flags_vague(self, bad):
        ok, issues = check_determinacy(bad)
        assert not ok
        assert issues

    @pytest.mark.parametrize("good", [
        "返回 HTTP 200, 记录状态为「已上线」",
        "新增记录数为 0",
        "未登录返回 HTTP 401",
        "status = ONLINE",
    ])
    def test_accepts_concrete(self, good):
        ok, issues = check_determinacy(good)
        assert ok, issues

    def test_empty_is_undeterminable(self):
        ok, issues = check_determinacy("")
        assert not ok and "预期结果为空" in issues

    def test_must_have_concrete_anchor(self):
        """没有具体值的句子, 即使一个模糊词都没有, 也不可判定"""
        ok, issues = check_determinacy("系统拒绝该操作")
        assert not ok
        assert any("具体值" in i for i in issues)


# ============================================================
# 维度选择: 程序判定而非提示词
# ============================================================

class TestDimensionSelection:

    def test_base_dims_always(self, gen):
        """每条事实至少有主流程 + 异常"""
        dims = gen._dimensions_for(mkfact())
        assert "happy_path" in dims and "exception" in dims

    def test_constraint_adds_forbid(self, gen):
        assert "forbid" in gen._dimensions_for(mkfact(cons="禁止自动上线"))

    def test_no_constraint_no_forbid(self, gen):
        """没有约束硬生成 forbid 只会产出空壳"""
        f = mkfact(cons="")
        f.evidence = [Evidence(ev_type="requirement", ref_uri="r#1",
                               snippet="无约束的普通功能描述",
                               ev_group="g0")]
        assert "forbid" not in gen._dimensions_for(f)

    def test_table_evidence_adds_data_and_idempotent(self, gen):
        f = mkfact(ev_types=("requirement", "table"))
        dims = gen._dimensions_for(f)
        assert "data_consistency" in dims and "idempotent" in dims

    def test_no_table_no_data_dims(self, gen):
        """没表证据 -> 不生成依赖表的维度"""
        f = mkfact(ev_types=("requirement", "code"))
        dims = gen._dimensions_for(f)
        assert "data_consistency" not in dims
        assert "idempotent" not in dims

    def test_boundary_signal_adds_boundary(self, gen):
        f = mkfact(title="名称长度不得超过 20 个字符", cons="长度上限 20")
        assert "boundary" in gen._dimensions_for(f)

    def test_no_boundary_signal_no_boundary(self, gen):
        """没有可度量量 -> 不生成边界值用例"""
        f = mkfact(title="客服异常时自动下线", cons="禁止自动上线")
        assert "boundary" not in gen._dimensions_for(f)

    def test_state_fact_adds_permission(self, gen):
        f = mkfact(title="客服上线状态流转", act="上线")
        assert "permission" in gen._dimensions_for(f)

    def test_dims_deterministic(self, gen):
        """维度必须可复现, 否则覆盖报告没法审计"""
        f = mkfact(ev_types=("requirement", "table", "code"))
        runs = {tuple(gen._dimensions_for(f)) for _ in range(5)}
        assert len(runs) == 1

    def test_dims_no_duplicate(self, gen):
        f = mkfact(cons="禁止上线", ev_types=("requirement", "table", "code"))
        dims = gen._dimensions_for(f)
        assert len(dims) == len(set(dims))

    def test_all_dims_have_chinese_name(self):
        for k, (name, prio, focus) in DIMENSIONS.items():
            assert name and prio in ("P0", "P1", "P2"), k
            assert focus, k


# ============================================================
# 生成的用例必须可判定
# ============================================================

class TestGeneratedCasesAreDeterminable:

    """这一组是本次重构的核心验收点: 预期结果不许有"正确/正常" """

    def _facts_varied(self):
        return [
            mkfact(title="客服异常时自动下线", cons="禁止自动上线"),
            mkfact(title="客服上线需人工确认", cons="必须人工点确认才能上线",
                   ev_types=("requirement", "table")),
            mkfact(title="名称长度不得超过 20 个字符", cons="长度上限 20"),
            mkfact(title="订单支付状态流转", act="支付",
                   ev_types=("requirement", "sql")),
        ]

    def test_no_vague_expected(self, gen):
        cases = gen.generate(self._facts_varied())
        bad = []
        for c in cases:
            ok, issues = check_determinacy(c.expected_result)
            if not ok:
                bad.append((c.case_id, c.dimension, issues))
        assert not bad, f"{len(bad)} 条预期不可判定: {bad[:5]}"

    def test_vague_rate_is_zero(self, gen):
        facts = self._facts_varied()
        rep = gen.coverage_report(facts, gen.generate(facts))
        assert rep["vague_expected"] == 0, rep["vague_detail"][:5]

    def test_permission_has_status_codes(self, gen):
        f = mkfact(title="客服上线状态流转", act="上线")
        cases = [c for c in gen.generate([f]) if c.dimension == "permission"]
        assert cases, "权限维度未生成"
        for c in cases:
            assert "401" in c.expected_result and "403" in c.expected_result

    def test_idempotent_has_concrete_counts(self, gen):
        f = mkfact(ev_types=("requirement", "table"))
        cases = [c for c in gen.generate([f]) if c.dimension == "idempotent"]
        assert cases
        for c in cases:
            assert "0" in c.expected_result, c.expected_result

    def test_boundary_covers_six_points(self, gen):
        f = mkfact(title="名称长度不得超过 20 个字符", cons="长度上限 20")
        cases = [c for c in gen.generate([f]) if c.dimension == "boundary"]
        assert cases
        for c in cases:
            assert len(c.steps) >= 6
            for pt in ("min-1", "min+1", "max", "max+1"):
                assert pt in c.expected_result or pt in " ".join(
                    s["action"] for s in c.steps)

    def test_forbid_quotes_constraint(self, gen):
        """禁止类用例必须把约束原文带进预期, 否则测了也不知道测的啥"""
        f = mkfact(cons="禁止自动上线")
        cases = [c for c in gen.generate([f]) if c.dimension == "forbid"]
        assert cases
        for c in cases:
            assert "禁止自动上线" in c.expected_result

    def test_no_forbid_case_without_constraint(self, gen):
        f = mkfact(cons="")
        f.evidence = [Evidence(ev_type="requirement", ref_uri="r#1",
                               snippet="无约束描述", ev_group="g0")]
        cases = [c for c in gen.generate([f]) if c.dimension == "forbid"]
        assert not cases, "无约束却生成了 forbid 用例"


# ============================================================
# 覆盖度报告
# ============================================================

class TestCoverageReport:

    def test_full_coverage(self, gen):
        facts = [mkfact(title="规则A", cons="禁止A"),
                 mkfact(title="规则B", cons="禁止B")]
        rep = gen.coverage_report(facts, gen.generate(facts))
        assert rep["coverage_rate"] == 100.0
        assert rep["fact_uncovered"] == 0

    def test_reports_uncovered(self, gen):
        rep = gen.coverage_report([mkfact(title="规则A"), mkfact(title="规则B")],
                                  gen.generate([mkfact(title="规则A")]))
        assert rep["fact_uncovered"] == 1
        assert rep["uncovered_detail"][0]["title"] == "规则B"

    def test_dimension_dist_sums_to_total(self, gen):
        facts = [mkfact(title="规则A"), mkfact(title="长度不得超过 5 个字")]
        rep = gen.coverage_report(facts, gen.generate(facts))
        assert sum(rep["dimension_dist"].values()) == rep["case_total"]

    def test_empty_input_no_div_zero(self, gen):
        rep = gen.coverage_report([], [])
        assert rep["coverage_rate"] == 0.0
        assert rep["case_total"] == 0


# ============================================================
# 反幻觉: 用例里不能出现事实之外的标识符
# ============================================================

class TestNoHallucination:

    def test_no_invented_table_names(self, gen):
        """断言里不得出现证据外的表名"""
        f = mkfact()
        cases = gen.generate([f])
        allowed = f.ground_names()
        import re
        for c in cases:
            for token in re.findall(r"\b[a-z][a-z0-9]*_[a-z0-9_]+\b",
                                    " ".join(c.assertions)):
                assert token in allowed or "TODO" in c.assertions[0], \
                    f"{c.case_id} 编了表名 {token}"

    def test_todo_kept_when_unknown(self, gen):
        """
        不确定的值必须留 TODO, 不能猜。
        旧生成器有 103 处 TODO 被批评 —— 但那是因为它既没门禁也没出处;
        有门禁的 TODO 是显式缺口, 没门禁的编造是隐性污染, 危害更大。
        """
        f = mkfact(title="名称长度不得超过 20 个字符", cons="长度上限 20")
        cases = gen.generate([f])
        flat = [p for c in cases for p in c.preconditions + c.data_setup]
        assert any("TODO" in p for p in flat), "边界值未知时应留 TODO"

    def test_fact_id_matches_extractor(self, gen):
        """生成器的 fact_id 必须与入库层同源, 否则覆盖报告恒为 0"""
        from src.fact_extractor import fact_id_of
        f = mkfact()
        cases = gen.generate([f])
        assert cases
        assert all(c.fact_id == fact_id_of(f) for c in cases)


# ============================================================
# 序列化
# ============================================================

class TestSerialization:

    def test_case_ids_unique_and_sequential(self, gen):
        cases = gen.generate([mkfact(title="规则A"), mkfact(title="规则B")])
        ids = [c.case_id for c in cases]
        assert len(ids) == len(set(ids))

    def test_to_dict_shape(self, gen):
        d = gen.generate([mkfact()])[0].to_dict()
        for k in ("case_id", "fact_id", "title", "dimension", "priority",
                  "steps", "expected_result", "assertions",
                  "human_approved", "generated_by"):
            assert k in d, k
        assert d["human_approved"] is False, "生成物默认必须待人工审核"

    def test_cases_to_json_utf8(self, gen):
        s = cases_to_json(gen.generate([mkfact()]))
        assert "客服异常" in s, "中文必须原样输出不能转义"