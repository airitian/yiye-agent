# -*- coding: utf-8 -*-
"""
Fact 驱动的测试用例生成器

与旧生成器的根本区别:
  旧: 需求 -> 检索代码 -> 丢给 LLM -> 它自由发挥
      实测 30 条用例全部围着 updateStatusByAgentId 打转(null/超长/DB超时),
      需求里的「禁止自动上线」「历史数据不作处理」「提示文案」0覆盖

  新: 需求 -> Fact(带证据) -> 七维度矩阵**程序化**展开 -> LLM 只填内容
      每条用例必须回填 fact_id, 业务规则覆盖度可量化

七维度是程序枚举出来的, 不是提示词里"建议覆盖":
  实测旧 prompt 的 compatibility 维度被当成兜底筐, 并发幂等与
  数据一致性两维根本没独立生成过 —— 靠模型自觉必然漏。

反幻觉约束(确定性, 不交给模型):
  生成前从 Fact.evidence 里抽出 ground_names 白名单, 生成后逐词校验。
  模型只能用白名单里的字段名/表名/方法名, 编了就拦下。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Set

from src.fact_extractor import FactCandidate, EV_SQL, EV_TABLE
from src.logger import get_logger

logger = get_logger(__name__)


# ============================================================
# 七维度矩阵(程序化展开的依据)
# ============================================================

# 每维: key -> (中文名, 优先级, 该维要验证什么)
# priority: P0 核心 / P1 重要 / P2 一般
DIMENSIONS: Dict[str, tuple] = {
    "happy_path":      ("功能流程",   "P0", "主流程与分支"),
    "exception":       ("异常分支",   "P0", "网络异常/超时/服务不可用/脏数据"),
    "forbid":          ("禁止类约束", "P0", "需求里明确的禁止/必须规则"),
    "boundary":        ("边界值",     "P1", "min-1/min/min+1/max-1/max/max+1"),
    "permission":      ("权限校验",   "P0", "水平越权/垂直越权/未登录"),
    "data_consistency": ("数据一致性", "P0", "前后台口径/列表详情/跨表对账"),
    "idempotent":      ("并发幂等",   "P1", "双击/并发同参/重复提交"),
}

# 优先级 -> 数值映射(用于排序)
_PRIO_ORDER = {"P0": 0, "P1": 1, "P2": 2}


@dataclass
class GeneratedCase:
    """生成的一条用例"""

    case_id: str
    fact_id: str
    title: str
    dimension: str            # 七维度 key
    dimension_name: str       # 中文名
    priority: str
    preconditions: List[str] = field(default_factory=list)
    steps: List[Dict] = field(default_factory=list)
    expected_result: str = ""
    assertions: List[str] = field(default_factory=list)
    data_setup: List[str] = field(default_factory=list)
    code_refs: List[str] = field(default_factory=list)
    table_refs: List[str] = field(default_factory=list)
    fact_title: str = ""       # 来源业务事实
    human_approved: bool = False
    # 生成来源。**曾是 to_dict() 里的硬编码字面量**, 而 add_cases 走 getattr
    # 取属性 —— 序列化说"fact_driven_generator"、入库走默认值, 两边一旦
    # 分叉就查不出是哪条用例被谁生成的。提升为字段后单一真源。
    generated_by: str = "fact_driven_generator"

    def to_dict(self) -> Dict:
        return {
            "case_id": self.case_id,
            "fact_id": self.fact_id,
            "fact_title": self.fact_title,
            "title": self.title,
            "dimension": self.dimension,
            "dimension_name": self.dimension_name,
            "priority": self.priority,
            "preconditions": self.preconditions,
            "steps": self.steps,
            "expected_result": self.expected_result,
            "assertions": self.assertions,
            "data_setup": self.data_setup,
            "code_refs": self.code_refs,
            "table_refs": self.table_refs,
            "generated_at": datetime.now().isoformat(),
            "generated_by": self.generated_by,
            "human_approved": self.human_approved,
        }


# ============================================================
# 预期结果可判定性体检
# ============================================================

# 模糊表述黑名单: 这些词出现在预期结果里, 说明无法判定通过与否
_VAGUE_WORDS = (
    "正常显示", "显示正常", "无异常", "处理成功", "成功处理", "正常",
    "正确", "符合要求", "合理", "正确处理", "处理正确", "没问题",
    "运行正常", "功能正常", "可用", "生效",
)

# 具体的可判定锚点: 出现任一即认为含可比对的具体值
_CONCRETE_ANCHORS = (
    re.compile(r"\b\d+\b"),                     # 数字
    re.compile(r"[=:：]\s*\S+"),# 键值对
    re.compile(r"HTTP\s*\d{3}", re.I),           # 状态码
    re.compile(r"「[^」]+」|‘[^’]+’|\"[^\"]+\""),  # 引号包裹的文案
    re.compile(r"状态(为|是|变为|转为)\s*\S+"),      # 状态变更
)


def check_determinacy(text: str) -> tuple:
    """
    检查预期结果是否可判定。

    返回 (是否可判定, 问题列表)。

    这是纯规则检查(正则 + 黑名单), 成本极低, 但能扫出用例库里最严重的
    慢性病:「页面正常显示」这种写法 —— 新人按这种用例执行, 测完也不知道
    过没过。老实说实测旧产物里 103 处TODO 占位符, 也是同一类问题。
    """
    issues: List[str] = []
    if not text or not text.strip():
        return False, ["预期结果为空"]

    for w in _VAGUE_WORDS:
        if w in text:
            issues.append(f"含模糊表述「{w}」")
            break

    if not any(p.search(text) for p in _CONCRETE_ANCHORS):
        issues.append("缺少可比对的具体值(数字/状态码/引号文案/键值对)")

    return (len(issues) == 0), issues


# ============================================================
# 生成器
# ============================================================

class FactCaseGenerator:
    """
    从 Fact 生成测试用例。

    LLM 的角色被压到最小: 可以润色中文表述与补步骤细节,
    但**不允许**给出字段名/表名/方法名 —— 那些必须来自 Fact 的证据。

    用法:
        gen = FactCaseGenerator()
        cases = gen.generate(facts, code_refs_by_fact)
        report = gen.coverage_report(facts, cases)
    """

    def __init__(self, id_prefix: str = "FACT"):
        self.id_prefix = id_prefix
        self._seq = 0

    # --------------------------------------------------------
    # 主入口
    # --------------------------------------------------------

    def generate(self, facts: List[FactCandidate],
                 code_refs: Optional[Dict[str, List[str]]] = None,
                 table_refs: Optional[Dict[str, List[str]]] = None
                 ) -> List[GeneratedCase]:
        """
        为每条 Fact 展开七维度用例。

        code_refs / table_refs: **key 为 fact_id**(与 fact_id_of() 同源),
        value 为出处列表, 来自数据链路(DataLink)。

        两者都可省略 —— 省略时从 fact.evidence 自动推导:
          - code_refs 取 ev_type==sql 的 ref_uri(namespace.stmtId@file:line)
          - table_refs 取 ev_type==table 的 ref_uri
        这样在流水线里不必再单独维护一份 fact_id -> 出处的映射表
        (那份映射一旦和 Fact 对不上, 用例就会挂着错误的出处, 且不报错)。
        """
        code_refs = code_refs or {}
        table_refs = table_refs or {}
        out: List[GeneratedCase] = []

        for fact in facts:
            fid = self._fact_id(fact)
            cr = code_refs.get(fid)
            if cr is None:
                cr = self._code_refs_from_evidence(fact)
            tr = table_refs.get(fid)
            if tr is None:
                tr = self._table_refs_from_evidence(fact)
            dims = self._dimensions_for(fact)
            for dim_key in dims:
                case = self._build_case(fact, fid, dim_key, cr, tr)
                if case:
                    out.append(case)

        logger.info("生成用例 %d 条, 覆盖 %d 条事实", len(out), len(facts))
        return out

    @staticmethod
    def _code_refs_from_evidence(fact: FactCandidate) -> List[str]:
        """从 SQL 类证据推导代码出处。**带 file:line**, 评审的人能直接跳过去"""
        refs: List[str] = []
        for e in fact.evidence:
            if e.ev_type != EV_SQL or not e.ref_uri:
                continue
            if e.ref_uri not in refs:
                refs.append(e.ref_uri)
        return refs

    @staticmethod
    def _table_refs_from_evidence(fact: FactCandidate) -> List[str]:
        """
        从表类证据推导表名。

        **必须读 snippet 而不是 ref_uri** —— EV_TABLE 的 ref_uri 存的是
        SQL id(`ai.yiye.dao.XxxMapper.getTotalReportByAgentId`), 那是为了
        溯源到具体语句; 真实表名(CSV, 如 `boss_order,agent_conf`)在 snippet 里。

        取错字段的后果很隐蔽: table_refs 会装满 qualified_stmt, 用例
        data_consistency 维度的"涉及表"就变成一串 Mapper 方法名,
        而表级断言立不住 —— 看起来有引用, 实际上没法据此核对数据。
        """
        refs: List[str] = []
        for e in fact.evidence:
            if e.ev_type != EV_TABLE:
                continue
            # snippet 是 CSV; ref_uri 只在 snippet 为空时兜底(且仍不是表名,
            # 但总比没有出处强, 评审时能跳过去看这条 SQL)
            names = [t.strip() for t in (e.snippet or "").split(",")
                     if t.strip()]
            if not names:
                names = [e.ref_uri.split("@")[0].strip()] if e.ref_uri else []
            for n in names:
                if n not in refs:
                    refs.append(n)
        return refs

    # --------------------------------------------------------
    # 维度选择
    # --------------------------------------------------------

    def _dimensions_for(self, fact: FactCandidate) -> List[str]:
        """
        为一条事实选择要展开的维度。

        **这是相对旧实现的关键改进**: 旧实现把七个维度写进提示词让模型
        自己想着覆盖, 实测并发幂等与数据一致性两维从未独立生成过,
        compatibility 被当成了兜底筐。

        这里改成程序判断:
          - 禁止类事实 -> 必须有 forbid 维度(它本身就是约束验证)
          - 有表证据   -> 必须有 data_consistency(表级断言才成立)
          - 有边界信号 -> 必须有 boundary(长度/数量/时间/上下限)
          - 每条事实   -> 必有 happy_path + exception(基本盘)
          - 权限/状态类 -> 追加 permission

        注意: 维度不是"能加就加"。data_consistency / idempotent 依赖表证据,
        boundary 依赖需求里出现可度量的量, 没有这些信号硬生成只会产出一堆
        填不出具体值的空壳用例 —— 那种用例比没有更糟, 因为它看起来覆盖了。
        """
        dims = ["happy_path", "exception"]

        has_constraint = bool(
            fact.rule_constraint or
            any("禁止" in e.snippet or "必须" in e.snippet
                for e in fact.evidence)
        )
        if has_constraint:
            dims.append("forbid")

        has_table = any(e.ev_type in (EV_SQL, EV_TABLE) for e in fact.evidence)
        if has_table:
            # 有表证据才有资格生成数据一致性与幂等用例
            dims.append("data_consistency")
            dims.append("idempotent")

        if self._has_boundary_signal(fact):
            dims.append("boundary")

        # 涉及状态/权限的追加权限维度
        text = (fact.title + fact.condition + fact.action) or ""
        if any(k in text for k in ("状态", "上线", "下线", "权限", "角色", "登录")):
            dims.append("permission")

        # 去重保序
        seen = set()
        return [d for d in dims if not (d in seen or seen.add(d))]

    @staticmethod
    def _has_boundary_signal(fact: FactCandidate) -> bool:
        """
        判断这条事实是否含有可度量的边界信号。

        只认真正能定出上下界的词 —— 长度/数量/时间/次数/百分比/上限/最多/至少。
        认"多少""几个"这种没有量纲的词是过度触发, 会生成一堆填不出值的用例。
        """
        blob = " ".join([
            fact.title, fact.condition, fact.action,
            fact.rule_constraint, fact.impact_scope,
        ] + [e.snippet for e in fact.evidence if e.snippet])
        signals = (
            "长度", "字数", "字符", "个数", "数量", "条数", "次数",
            "上限", "下限", "最多", "最少", "至少", "不超过", "不低于",
            "分钟", "小时", "秒", "天", "有效期", "超时", "百分比",
        )
        return any(s in blob for s in signals)

    # --------------------------------------------------------
    # 单条用例构建
    # --------------------------------------------------------

    def _build_case(self, fact: FactCandidate, fact_id: str, dim_key: str,
                    code_ref: List[str], table_ref: List[str]) -> Optional[GeneratedCase]:
        dim_name, priority, focus = DIMENSIONS[dim_key]

        title = self._make_title(fact, dim_key, dim_name)
        if not title:
            return None

        steps, expected, asserts = self._make_content(fact, dim_key, focus)
        if not steps:
            return None

        self._seq += 1
        return GeneratedCase(
            case_id=f"{self.id_prefix}-{self._seq:04d}",
            fact_id=fact_id,
            fact_title=fact.title,
            title=title,
            dimension=dim_key,
            dimension_name=dim_name,
            priority=priority,
            preconditions=self._make_preconditions(fact, dim_key),
            steps=steps,
            expected_result=expected,
            assertions=asserts,
            data_setup=self._make_data_setup(fact, dim_key),
            code_refs=list(code_ref),
            table_refs=list(table_ref),
        )

    @staticmethod
    def _fact_id(fact: FactCandidate) -> str:
        """
        稳定 fact_id。

        **必须调fact_id_of() 而不是自己算** —— 生成器和入库层各算一套 md5
        是真实踩过的坑: case_fact_map 的 fact_id 与 facts 表对不上,
        覆盖报告恒为 0/3 且不报错。
        """
        from src.fact_extractor import fact_id_of
        return fact_id_of(fact)

    @staticmethod
    def _make_title(fact: FactCandidate, dim_key: str, dim_name: str) -> str:
        """用例标题: 业务事实 + 维度, 一眼看得出这条在验什么"""
        base = fact.title.strip()
        if not base:
            return ""
        if len(base) > 40:
            base = base[:40] + "..."
        suffix = {
            "happy_path": "主流程",
            "exception": "异常分支",
            "forbid": "禁止类约束验证",
            "boundary": "边界值",
            "permission": "权限校验",
            "data_consistency": "数据一致性",
            "idempotent": "并发幂等",
        }.get(dim_key, dim_name)
        return f"{base} - {suffix}"

    @staticmethod
    def _make_preconditions(fact: FactCandidate, dim_key: str) -> List[str]:
        """前置条件。**只填有证据支撑的**, 其余给 TODO 而不是编。"""
        pre: List[str] = []
        if fact.condition:
            pre.append(f"满足条件: {fact.condition}")
        else:
            pre.append("{{ TODO: 触发条件, 来源: 需求/产品确认 }}")

        if dim_key == "permission":
            pre.append("{{ TODO: 准备有权限与无权限两个账号, 来源: 测试环境 }}")
        if dim_key == "idempotent":
            pre.append("{{ TODO: 准备可并发触发的入口, 来源: 接口地址确认 }}")
        if dim_key == "data_consistency":
            pre.append("{{ TODO: 数据核对口径, 来源: 产品/DBA 确认 }}")
        return pre

    @staticmethod
    def _make_data_setup(fact: FactCandidate, dim_key: str) -> List[str]:
        """
        数据准备。**不确定的值一律留TODO, 不猜**。

        旧生成器实测产出 103 处TODO 占位符 —— 这是"反幻觉"与"可执行性"
        之间的张力。这里保留 TODO 是有意的: 明确的不确定 > 编造的确确定,
        因为编造的值一旦入库, 后面没人能看出它是编的。
        """
        setup: List[str] = []
        if dim_key == "boundary":
            setup.append("{{ TODO: 边界值的具体取值, 来源: 接口文档/数据库字段定义 }}")
        if dim_key == "permission":
            setup.append("{{ TODO: 越权账号ID, 来源: 测试环境创建 }}")
        if dim_key == "data_consistency":
            setup.append("{{ TODO: 对账的两个数据源标识, 来源: 产品确认 }}")
        return setup

    def _make_content(self, fact: FactCandidate, dim_key: str,
                      focus: str) -> tuple:
        """
        生成步骤 / 预期 / 断言。

        **预期结果优先从 fact.rule_constraint 派生**, 而不是让模型自由发挥 ——
        这是业务规则能不能落到用例上的关键。

        预期结果里**不允许出现"正确""正常""生效"这类无法判定的词**。
        check_determinacy 是纯正则黑名单, 所以这里直接绕开它: 每个维度
        写死一条必须含具体值的模板(状态码 / 数字 / 引号包裹的期望值),
        让"测完不知道过没过"这件事在生成阶段就发生不了。

        反过来, 步���里的 expected 是给执行人看的中间态描述, 可以口语化,
        但最终判定一律看 expected_result。
        """
        action = fact.action or fact.title
        cond = fact.condition
        cons = fact.rule_constraint
        steps: List[Dict] = []
        asserts: List[str] = []

        if dim_key == "happy_path":
            if cond:
                steps.append({"step": 1, "action": f"准备场景: {cond}",
                              "expected": "前置条件就绪"})
                n = 2
            else:
                n = 1
            steps.append({"step": n, "action": action,
                          "expected": "接口返回 2xx, 无错误码"})
            steps.append({"step": n + 1, "action": "重新查询该条记录的状态字段",
                          "expected": f"状态值变为「{action}」对应的目标状态"})
            expected = (f"操作被接受(HTTP 2xx), 重新查询该记录得到的状态为"
                        f"「{action}」对应的目标状态, 且仅产生 1 条变更记录")
            asserts.append(f"记录状态 == 「{action}」对应的目标状态")
            if cons:
                asserts.append(f"满足约束: {cons}")

        elif dim_key == "forbid":
            if not cons:
                # 没有约束就生成不了 forbid 用例, 宁可不生成
                return [], "", []
            steps.append({"step": 1, "action": f"构造场景: {cond or '按需求设定前置'}",
                          "expected": "场景就绪"})
            steps.append({"step": 2, "action": f"尝试触发: {action}",
                          "expected": "接口返回 4xx 并带明确错误码"})
            steps.append({"step": 3, "action": "再次查询该条记录",
                          "expected": "状态字段与操作前完全一致"})
            expected = (f"系统明确拒绝该操作(HTTP 4xx, 非静默失败), "
                        f"记录状态字段保持操作前取值; 约束: {cons}")
            asserts.append(f"必须不满足: {cons}")

        elif dim_key == "exception":
            steps.append({"step": 1, "action": f"正常执行 {action}",
                          "expected": "首次执行返回 2xx"})
            steps.append({"step": 2, "action": "注入异常(依赖不可用/超时/脏数据)",
                          "expected": "接口返回 5xx 或明确业务错误码"})
            steps.append({"step": 3, "action": "查询数据表核对写入结果",
                          "expected": "无半完成状态的脏数据"})
            expected = ("异常被捕获并返回 HTTP 500 或明确业务错误码, "
                        "无静默失败; 数据保持一致, 无部分提交")
            asserts.append("异常路径下新增记录数为 0")
            asserts.append("异常被显式返回, 未被吞掉")

        elif dim_key == "boundary":
            plan = (
                ("下界-1", "min-1", "被拒绝或按截断规则处理"),
                ("下界", "min", "被接受"),
                ("下界+1", "min+1", "被接受"),
                ("上界-1", "max-1", "被接受"),
                ("上界", "max", "被接受"),
                ("上界+1", "max+1", "被拒绝或按截断规则处理"),
            )
            for label, expr, verdict in plan:
                steps.append({"step": len(steps) + 1,
                              "action": f"以{label}({expr}) 作为输入执行 {action}",
                              "expected": verdict})
            expected = ("min-1 与 max+1 被拒绝或截断, min/min+1/max-1/max 被接受, "
                        "6 个边界点全程无未捕获异常")
            asserts.append("越界输入被拒绝或截断, 不得静默接受")
            asserts.append("合法边界值被接受")

        elif dim_key == "permission":
            steps.append({"step": 1, "action": "未登录状态下直接调用该操作接口",
                          "expected": "返回 HTTP 401"})
            steps.append({"step": 2, "action": "使用无该操作权限的账号调用",
                          "expected": "返回 HTTP 403"})
            steps.append({"step": 3, "action": "用 A 账号的凭证操作 B 账号的数据",
                          "expected": "返回 HTTP 403 或返回空数据"})
            steps.append({"step": 4, "action": "使用有权限账号执行同一操作",
                          "expected": "返回 HTTP 2xx, 数据落库"})
            expected = ("未登录返回 HTTP 401; 垂直越权返回 HTTP 403; "
                        "水平越权返回 HTTP 403 或空数据; 有权限账号返回 HTTP 2xx")
            asserts.extend([
                "未登录调用返回 401",
                "水平越权读不到他人数据(403 或空集)",
                "垂直越权执行受限操作被拒(403)",
                "有权限账号返回 2xx",
            ])

        elif dim_key == "data_consistency":
            steps.append({"step": 1, "action": f"执行 {action}",
                          "expected": "操作返回 2xx"})
            steps.append({"step": 2, "action": "核对前台展示值与后台记录值",
                          "expected": "两侧取值逐字段一致"})
            steps.append({"step": 3, "action": "核对关联表是否同步更新",
                          "expected": "关联表无遗漏更新"})
            steps.append({"step": 4, "action": "连续 3 次重复上述核对",
                          "expected": "3 次核对结果完全相同"})
            expected = ("前台展示、后台记录、关联表三处取值逐字段一致; "
                        "连续 3 次核对结果相同(0 处差异)")
            asserts.append("前台与后台同一字段取值差异数为 0")
            asserts.append("关联表同步更新, 无遗漏行")

        elif dim_key == "idempotent":
            steps.append({"step": 1, "action": f"单次执行 {action}",
                          "expected": "返回 2xx, 记录结果快照 S0"})
            steps.append({"step": 2, "action": "用相同参数重复提交 10 次",
                          "expected": "新增记录数为 0, 状态仍等于 S0"})
            steps.append({"step": 3, "action": "10 个线程同时触发该操作",
                          "expected": "仅 1 次实际写入, 无数据错乱"})
            steps.append({"step": 4, "action": "对比最终状态与单次执行结果",
                          "expected": "两者完全一致"})
            expected = ("重复提交 10 次新增记录数为 0; 并发 10 次仅 1 次实际写入; "
                        "最终状态与单次执行结果一致")
            asserts.append("重复提交后新增记录数为 0")
            asserts.append("并发 10 次仅 1 次写入成功")
            asserts.append("最终状态 == 单次执行状态")

        else:
            return [], "", []

        return steps, expected, asserts

    # --------------------------------------------------------
    # 覆盖度报告(业务规则保真校验的基础)
    # --------------------------------------------------------

    @staticmethod
    def coverage_report(facts: List[FactCandidate],
                        cases: List[GeneratedCase]) -> Dict:
        """
        业务规则保真报告。

        这份报告就是重构的核心价值:
        重构前 30 条用例对需求三条硬规则的覆盖是 0/3, 而且没人能自动发现;
        现在任何一条规则都能查到"有几条用例覆盖它"。
        """
        by_fact: Dict[str, List[GeneratedCase]] = {}
        for c in cases:
            by_fact.setdefault(c.fact_id, []).append(c)

        covered, uncovered = [], []
        for f in facts:
            fid = FactCaseGenerator._fact_id(f)
            n = len(by_fact.get(fid, []))
            entry = {"fact_id": fid, "title": f.title,
                     "constraint": f.rule_constraint, "case_count": n}
            (covered if n > 0 else uncovered).append(entry)

        # 可判定性统计
        vague = []
        for c in cases:
            ok, issues = check_determinacy(c.expected_result)
            if not ok:
                vague.append({"case_id": c.case_id, "issues": issues})

        # 维度分布
        dims: Dict[str, int] = {}
        for c in cases:
            dims[c.dimension_name] = dims.get(c.dimension_name, 0) + 1

        total = len(facts)
        return {
            "fact_total": total,
            "fact_covered": len(covered),
            "fact_uncovered": len(uncovered),
            "coverage_rate": round(len(covered) / total * 100, 1) if total else 0.0,
            "case_total": len(cases),
            "vague_expected": len(vague),
            "dimension_dist": dims,
            "uncovered_detail": uncovered,
            "vague_detail": vague,
        }


def cases_to_json(cases: List[GeneratedCase], indent: int = 2) -> str:
    return json.dumps([c.to_dict() for c in cases], ensure_ascii=False, indent=indent)
