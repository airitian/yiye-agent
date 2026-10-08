# -*- coding: utf-8 -*-
"""
业务功能问答器 —— 把索引变成"能回答业务问题"的知识库

---
## 为什么单独写一个问答器, 而不是拿 LLM 直接问检索
---

实测旧版知识库的问题不是"检索不到", 而是**检索到了但答不对, 且答错了
还看不出来**。三个具体故障:

1. **问业务规则, 答技术细节**
   问「为什么不能自动上线」, 旧检索命中的是 updateStatusByAgentId 的代码,
   于是模型开始解释这个方法怎么改状态 —— 看着通顺, 但完全没回答问题。
   业务规则的答案在需求文档里, 不在代码里。

2. **编出处**
   模型被要求"给出文件:行号", 它就会给一个看起来像那么回事的行号。
   实测旧产物里 103 处 TODO 占位符, 还有大量编造的字段名。

3. **多跳问题直接崩**
   问「改这张表会影响哪些功能」, 需要 SQL -> 表 -> Service -> 用例 四跳。
   单轮检索给不出这个链。

---
## 设计: 按问句类型分路, 而不是一路检索
---

不同问题需要的东西根本不同, 用一个检索器兜全部必然顾此失彼:

| 问句类型 | 例句 | 检索策略 |
|---|---|---|
| 规则解释 | 为什么不能自动上线 | facts.rule_constraint 优先 |
| 影响面 | 改这里影响什么 | 调用图正/反向 + 表反查, 多跳 |
| 有没有测过 | 有没有测过 XX | case_fact_map + 历史用例 |
| 代码定位 | XX 在哪个文件 | 符号精确匹配 |
| 口径 | 前台和后台为什么对不上 | 表/字段跨层对照 |

判定问句类型用**确定性规则**, 不问 LLM: 问句里出现"为什么/为什么不能/凭什么"
就是规则解释, 出现"影响/波及/涉及哪些"就是影响面。这个判定必须可复现,
否则同一个问题两次问得到两个答案, 知识库的可信度就没了。

---
## 铁律: 无出处不回答
---

任何结论都必须带出处(需求段号 / 文件:行号 / SQL id / 用例号)。
检索不到就明确说"没查到", 而不是退化成一段听起来很像回事的通用解释。

这不是为了严谨, 是因为**一个没有出处的业务问答, 业务方无法验证, 就不敢用**。
测试问答系统尤其如此 —— 答错一条业务规则的测试用例, 会直接变成线上缺陷。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.index_builder import IndexBuilder
from src.logger import get_logger

logger = get_logger(__name__)


# ============================================================
# 问句分类
# ============================================================

class QType(str, Enum):
    RULE = "rule"                    # 规则解释: 为什么/凭什么/凭什么不能
    IMPACT = "impact"                # 影响面: 改这里影响什么
    COVERAGE = "coverage"            # 有没有测过
    LOCATE = "locate"                # 代码定位: 在哪
    CONSISTENCY = "consistency"      # 口径一致性
    UNKNOWN = "unknown"


# 顺序敏感: 命中即停。IMPACT 必须在 RULE 之前 ——
# 「为什么改这个表会影响自动上线」既像规则又像影响面, 答影响面才有用。
_RULES: List[Tuple[QType, re.Pattern]] = [
    (QType.IMPACT, re.compile(
        r"影响|波及|涉及哪些|会怎么样|改了.*会|副作用|牵连|牵连到|"
        r"上游|下游|受哪些|影响到")),
    # 「怎么验证/怎么测/怎么确认」与「有没有测过」是同一路(都要落到用例),
    # 必须放在 COVERAGE 之前或并列 —— 否则「手动上线怎么验证的」会掉到
    # UNKNOWN, 明明有 18 条用例却答不出来。
    (QType.COVERAGE, re.compile(
        r"有没有测|测过|测试用例|用例覆盖|覆盖了吗|测了吗|回归过|"
        r"有没有覆盖|漏测|怎么测|如何测|怎么验证|如何验证|"
        r"怎么确认|如何确认|验证方式|测哪些|测什么|怎么检查")),
    (QType.CONSISTENCY, re.compile(
        r"对不上|不一致|口径|为什么.*不一样|不同步|字段.*不符|差异")),
    (QType.RULE, re.compile(
        r"为什么|为何|凭什么|依据|规则是|约束是|能不能|可不可以|"
        r"允许|不允许|禁止|必须|要求")),
    (QType.LOCATE, re.compile(
        r"在哪|哪里|哪个文件|哪个类|哪个方法|定位|实现")),
]


def classify(question: str) -> QType:
    """
    判定问句类型。**纯规则, 不调模型**。

    必须是确定性的: 同一个问题两次判定必须一致, 否则知识库无法审计。
    """
    q = question.strip()
    for qt, pat in _RULES:
        if pat.search(q):
            return qt
    return QType.UNKNOWN


# ============================================================
# 答案与出处
# ============================================================

@dataclass
class Citation:
    """一条出处。**没有 citation 的 answer 段一律不允许输出**"""
    kind: str          # requirement / code / sql / table / case / fact
    ref: str           # 需求段号 / 文件:行号 / namespace.stmtId / 表名 / 用例号
    detail: str = ""

    def render(self) -> str:
        if self.detail:
            return f"[{self.kind}] {self.ref} — {self.detail}"
        return f"[{self.kind}] {self.ref}"


@dataclass
class Answer:
    question: str
    qtype: QType
    answer: str = ""
    citations: List[Citation] = field(default_factory=list)
    confidence: str = "none"     # high / medium / low / none
    unresolved: List[str] = field(default_factory=list)
    took_path: List[str] = field(default_factory=list)   # 走了哪几跳

    @property
    def grounded(self) -> bool:
        return bool(self.citations) and bool(self.answer)

    def render(self) -> str:
        head = f"[{self.qtype.value}] {self.question}"
        lines = [head, "=" * max(8, len(head))]
        if not self.answer:
            lines.append("未查到有出处的答案。")
        else:
            lines.append(self.answer)
        if self.citations:
            lines.append("")
            lines.append("出处:")
            for c in self.citations:
                lines.append(f"  - {c.render()}")
        if self.unresolved:
            lines.append("")
            lines.append("证据不足的部分(不猜):")
            for u in self.unresolved:
                lines.append(f"  ? {u}")
        lines.append("")
        lines.append(f"可信度: {self.confidence}")
        if self.took_path:
            lines.append(f"检索路径: {' -> '.join(self.took_path)}")
        return "\n".join(lines)


# ============================================================
# 问答器
# ============================================================

class BusinessQA:
    """
    业务功能问答。

    用法:
        with IndexBuilder(db_path, "customer-service") as b:
            qa = BusinessQA(b)
            ans = qa.ask("为什么不能自动上线")
            print(ans.render())

    **不用 LLM**。这一版的答案是从索引里"拼"出来的, 不是生成的。
    原因: 业务问答里 LLM 最大的风险不是说不通, 是把没有出处的话说得很通顺。
    先把确定性的部分做扎实(召回 + 出处 + 多跳), LLM 只在最后一步
    负责把这些确定素材组织成人话, 且**不允许引入素材外的标识符**。
    """

    def __init__(self, builder: IndexBuilder):
        self.b = builder

    # --------------------------------------------------------
    # 主入口
    # --------------------------------------------------------

    def ask(self, question: str, limit: int = 5) -> Answer:
        q = question.strip()
        if not q:
            return Answer(question=q, qtype=QType.UNKNOWN, confidence="none")

        qt = classify(q)
        handler = {
            QType.RULE: self._answer_rule,
            QType.IMPACT: self._answer_impact,
            QType.COVERAGE: self._answer_coverage,
            QType.LOCATE: self._answer_locate,
            QType.CONSISTENCY: self._answer_consistency,
        }.get(qt)

        if handler is None:
            return self._answer_fallback(q, limit)
        ans = handler(q, limit)
        ans.qtype = qt
        return ans

    # --------------------------------------------------------
    # 路 1: 规则解释
    # --------------------------------------------------------

    def _answer_rule(self, q: str, limit: int) -> Answer:
        """
        「为什么不能自动上线」这类问题。

        答案主体来自 facts.rule_constraint, 也就是**需求原文的约束句**,
        不是模型对代码的解释。这两者的可信度差一个数量级: 约束句可以拿
        去和产品对账, 模型解释对不了。
        """
        facts = self.b.search_facts(q, limit=limit)
        if not facts:
            # 退一步: 直接全文检索, 看看需求原文里有没有说过
            return self._answer_fallback(q, limit, why="业务事实层无命中")

        cites: List[Citation] = []
        parts: List[str] = []
        unresolved: List[str] = []

        for f in facts:
            title = f.get("title") or ""
            cons = f.get("rule_constraint") or ""
            cond = f.get("condition") or ""
            act = f.get("action") or ""
            if cond or cons:
                seg = f"· **{title}**"
                if cond:
                    seg += f"\n  条件: {cond}"
                if act:
                    seg += f"\n  动作: {act}"
                if cons:
                    seg += f"\n  约束: {cons}"
                parts.append(seg)

            cites.append(Citation("fact", f.get("fact_id", "?"),
                                  f"证据 {f.get('evidence_count', 0)} 条"))
            for ev in f.get("evidence", []):
                if ev.get("ev_type") == "requirement" and ev.get("ref_uri"):
                    cites.append(Citation("requirement", ev["ref_uri"],
                                          (ev.get("snippet") or "")[:80]))

        if not cons_any(facts):
            unresolved.append(
                "命中了相关事实, 但它们没有显式约束句 —— "
                "只有条件/动作, 无法回答「为什么」, 需要产品补充约束原文"
            )

        return Answer(
            question=q,
            qtype=QType.RULE,
            answer="\n".join(parts) if parts else "",
            citations=_dedup_cites(cites),
            confidence="high" if any(f.get("rule_constraint") for f in facts)
                       else "medium",
            unresolved=unresolved,
            took_path=["facts.rule_constraint", "fact_evidence(requirement)"],
        )

    # --------------------------------------------------------
    # 路 2: 影响面(多跳)
    # --------------------------------------------------------

    def _answer_impact(self, q: str, limit: int) -> Answer:
        """
        「改这里影响什么」需要正反向遍历, 这是旧检索完全做不到的。

        两种起跳点:
          - 给了表名  -> 表 -> SQL -> Mapper 方法 -> 上游 Service -> 事实
          - 给了符号  -> 符号 -> 下游(含表) + 上游 caller
        """
        table = self._guess_table(q)
        if table:
            return self._impact_from_table(q, table, limit)

        sym = self._guess_symbol(q)
        if sym:
            return self._impact_from_symbol(q, sym, limit)

        unresolved = []
        if not table:
            unresolved.append("问题里没有识别出表名, 无法走「表 -> SQL -> 上游」这条链")
        if not sym:
            unresolved.append("问题里没有识别出代码符号, 无法走「符号 -> 上下游」这条链")
        return Answer(
            question=q, qtype=QType.IMPACT,
            answer="",
            citations=[],
            confidence="none",
            unresolved=unresolved or ["无法定位起跳点"],
            took_path=["起跳点识别失败"],
        )

    def _impact_from_table(self, q: str, table: str, limit: int) -> Answer:
        stmts = self.b.find_sql_by_table(table, limit=limit)
        cites = [Citation("table", table, f"命中 {len(stmts)} 条 SQL")]
        cites += [
            Citation("sql", s["qualified_stmt"],
                     f"{s['stmt_type']} @ {s['file_path']}:{s['line_start']} "
                     f"[表名可信度={s['table_confidence']}]")
            for s in stmts
        ]

        # 表 -> SQL -> 标识符(stmtId / namespace) -> 业务事实
        #
        # 这一跳是确定性的 LIKE 桥接, 不是语义检索:
        # 表名是英文 snake_case, 事实标题是中文, 字面无交集;
        # 唯一连接点是 fact_evidence.snippet 里记录的那段代码原文。
        symbols: List[str] = []
        for s in stmts:
            qs = s["qualified_stmt"]              # ns.stmtId
            symbols.extend(qs.split("."))
            symbols.append(s["stmt_type"])
        symbols.append(table)
        facts = self.b.find_facts_by_symbols(symbols, limit=limit)

        parts = [f"表 `{table}` 被 {len(stmts)} 处 SQL 触及:"]
        for s in stmts:
            conf = s["table_confidence"]
            warn = "" if conf == "high" else "  ← 动态SQL/外部include, 表名可信度低"
            parts.append(f"  · {s['qualified_stmt']} ({s['stmt_type']}) "
                         f"{s['file_path']}:{s['line_start']}{warn}")

        if facts:
            parts.append("")
            parts.append("受影响的相关业务事实(经 SQL 标识符桥接):")
            for f in facts:
                cites.append(Citation("fact", f["fact_id"], f.get("title", "")))
                parts.append(f"  · {f.get('title')}  ({f['fact_id']})")
        else:
            parts.append("")
            parts.append("未桥接到业务事实 —— "
                         "这意味着改动的业务影响面无法自动判定, 必须人工评估。")

        unresolved = []
        low = [s for s in stmts if s["table_confidence"] != "high"]
        if low:
            unresolved.append(
                f"{len(low)} 条 SQL 的表名可信度为 low(动态标签或外部 include), "
                "实际访问的表可能不止这一张"
            )
        if not facts:
            unresolved.append(
                f"表 `{table}` 没有关联到任何已抽取的业务事实 —— "
                "链路断在 SQL 层。补法: 抽取事实时把该表相关 SQL 片段作为证据"
            )

        return Answer(
            question=q, qtype=QType.IMPACT,
            answer="\n".join(parts),
            citations=_dedup_cites(cites),
            confidence="high" if stmts and facts and not low else
                       ("medium" if stmts else "low"),
            unresolved=unresolved,
            took_path=["table", "sql_index", "stmtId/namespace",
                       "fact_evidence(桥接)", "facts"],
        )

    def _impact_from_symbol(self, q: str, sym: str, limit: int) -> Answer:
        hits = self.b.search(q, limit=limit) or []
        exact = [h for h in hits
                 if (h.get("name") == sym or h.get("qualified_name", "").endswith("." + sym))
                 ] or hits[:limit]

        cites: List[Citation] = []
        parts = [f"符号 `{sym}` 的上下游:"]

        for h in exact:
            cid = h.get("chunk_id")
            qn = h.get("qualified_name", "?")
            fp = h.get("file_path", "?")
            ls = h.get("line_start", "?")
            cites.append(Citation("code", f"{fp}:{ls}", qn))
            parts.append(f"  · 定义: {qn}  ({fp}:{ls})")

            try:
                callees = self.b.get_callees(cid, depth=2)
                callers = self.b.get_callers(cid, depth=2)
            except Exception as e:      # 老库缺 relations 列时降级
                logger.debug("链路遍历失败: %s", e)
                callees, callers = [], []

            if callees:
                parts.append(f"    下游 {len(callees)} 个:")
                for c in callees[:8]:
                    cf, cl = c.get("file_path", "?"), c.get("line_start", "?")
                    cites.append(Citation("code", f"{cf}:{cl}",
                                          f"下游 {c.get('qualified_name', '?')}"))
                    parts.append(f"      - {c.get('qualified_name')} ({cf}:{cl})")
            if callers:
                parts.append(f"    上游 {len(callers)} 个:")
                for c in callers[:8]:
                    cf, cl = c.get("file_path", "?"), c.get("line_start", "?")
                    cites.append(Citation("code", f"{cf}:{cl}",
                                          f"上游 {c.get('qualified_name', '?')}"))
                    parts.append(f"      - {c.get('qualified_name')} ({cf}:{cl})")

        return Answer(
            question=q, qtype=QType.IMPACT,
            answer="\n".join(parts) if parts else "",
            citations=_dedup_cites(cites),
            confidence="medium",
            unresolved=(["调用图存在 guess/ambiguous 边, "
                         "上下游可能不完整(同名歧义时按设计不连边)"]
                        if exact else ["未定位到该符号"]),
            took_path=["symbols", "relations(反向)", "relations(正向)"],
        )

    # --------------------------------------------------------
    # 路 3: 覆盖度
    # --------------------------------------------------------

    def _answer_coverage(self, q: str, limit: int) -> Answer:
        rep = self.b.fact_coverage_report()
        total = rep["fact_total"]
        facts = self.b.search_facts(q, limit=limit)

        cites: List[Citation] = []
        parts = [f"本域业务事实覆盖: {rep['fact_covered']}/{total} "
                 f"({rep['coverage_rate']}%)"]

        if facts:
            parts.append("")
            parts.append("与问题直接相关的规则:")
            for f in facts:
                fid = f["fact_id"]
                # 用 case_fact_map 反查该事实的用例数
                try:
                    n = self._case_count_for_fact(fid)
                except Exception:
                    n = 0
                mark = "已覆盖" if n > 0 else "未覆盖"
                parts.append(f"  · [{mark}] {f.get('title')}  ({fid}, {n} 条用例)")
                cites.append(Citation("fact", fid,
                                      f"{f.get('title')} / 用例 {n} 条"))

                # 有 cases 表就直接给出用例正文 —— 只给一个计数的话,
                # 业务方还得自己去别处找"到底哪条用例测了它"。
                for c in self.b.get_cases(fact_id=fid, limit=limit):
                    parts.append("")
                    parts.append(
                        f"      用例 [{c['dimension_name']}/{c['priority']}] "
                        f"{c['title']}  ({c['case_id']})")
                    exp = (c.get("expected_result") or "").strip()
                    if exp:
                        parts.append(f"        预期: {exp[:200]}")
                    if c.get("table_refs"):
                        parts.append(f"        涉及表: {', '.join(c['table_refs'])}")
                    if c.get("determinacy") and c["determinacy"] != "ok":
                        issues = ", ".join(c.get("determinacy_issues") or [])
                        parts.append(f"        ⚠ 预期可判定性: {c['determinacy']}"
                                     + (f" ({issues})" if issues else ""))
                    cites.append(Citation(
                        "case", c["case_id"],
                        f"{c['title']} / 维度 {c['dimension_name']}"))
        else:
            parts.append("")
            parts.append("问题未匹配到具体业务事实, 只能给出域级汇总。")

        if rep["uncovered_detail"]:
            parts.append("")
            parts.append("以下规则一条用例都没有(最该补的部分):")
            for u in rep["uncovered_detail"][:10]:
                parts.append(f"  ! {u['title']}  ({u['fact_id']})")
                cites.append(Citation("fact", u["fact_id"], "覆盖数=0"))

        unresolved = []
        if total == 0:
            unresolved.append("本域尚未建立业务事实, 无从判断覆盖情况 —— "
                              "需要先跑 Fact 抽取并入库")
        else:
            unresolved.append(
                "覆盖统计只反映「有没有用例回填 fact_id」, "
                "不代表用例断言本身写对了 —— 断言质量需另行体检"
            )

        return Answer(
            question=q, qtype=QType.COVERAGE,
            answer="\n".join(parts),
            citations=_dedup_cites(cites),
            confidence="high" if total else "none",
            unresolved=unresolved,
            took_path=["case_fact_map", "facts", "cases", "fact_coverage_report"],
        )

    def _case_count_for_fact(self, fact_id: str) -> int:
        cur = self.b._conn.cursor()
        return cur.execute(
            "SELECT count(*) FROM case_fact_map WHERE fact_id=?", (fact_id,)
        ).fetchone()[0]

    # --------------------------------------------------------
    # 路 4: 代码定位
    # --------------------------------------------------------

    def _answer_locate(self, q: str, limit: int) -> Answer:
        sym = self._guess_symbol(q)
        hits = self.b.search(q, limit=limit) or []
        if sym:
            exact = [h for h in hits if h.get("name") == sym]
            if exact:
                hits = exact + [h for h in hits if h not in exact]

        if not hits:
            return Answer(
                question=q, qtype=QType.LOCATE, answer="",
                citations=[], confidence="none",
                unresolved=[f"符号索引与全文索引均未命中「{sym or q}」"],
                took_path=["symbols", "code_fts"],
            )

        cites, parts = [], []
        for h in hits:
            fp = h.get("file_path", "?")
            ls = h.get("line_start", "?")
            qn = h.get("qualified_name", "?")
            cites.append(Citation("code", f"{fp}:{ls}", qn))
            summ = (h.get("summary") or "").strip()
            parts.append(f"· {qn}\n  位置: {fp}:{ls}")
            if summ:
                parts.append(f"  说明: {summ[:160]}")

        return Answer(
            question=q, qtype=QType.LOCATE,
            answer="\n".join(parts),
            citations=_dedup_cites(cites),
            confidence="high" if sym and any(h.get("name") == sym for h in hits)
                       else "medium",
            unresolved=([] if sym else
                        ["问题里没有明确的标识符, 结果为模糊匹配, 需人工确认"]),
            took_path=["symbols(精确)", "code_fts(全文)"],
        )

    # --------------------------------------------------------
    # 路 5: 口径一致性
    # --------------------------------------------------------

    def _answer_consistency(self, q: str, limit: int) -> Answer:
        table = self._guess_table(q)
        if not table:
            return Answer(
                question=q, qtype=QType.CONSISTENCY, answer="",
                citations=[], confidence="none",
                unresolved=["未从问题中识别出表名, 无法定位跨层口径差异"],
                took_path=["起跳点识别失败"],
            )

        stmts = self.b.find_sql_by_table(table, limit=limit)
        cites = [Citation("table", table, f"{len(stmts)} 条 SQL")]
        cites += [Citation("sql", s["qualified_stmt"], s["file_path"])
                  for s in stmts]

        parts = [f"表 `{table}` 的全部访问点(逐字段核对口径的依据):"]
        for s in stmts:
            parts.append(f"  · {s['qualified_stmt']}  {s['stmt_type']}  "
                         f"{s['file_path']}:{s['line_start']}")

        unresolved = [
            "本系统只能列出该表的全部访问点, **判定**哪两处口径不一致"
            "需要逐字段比对 SQL 投影 —— 这一步必须人工做, "
            "因为'不一致'是业务判断, 不是能靠检索自动得出的结论"
        ]
        return Answer(
            question=q, qtype=QType.CONSISTENCY,
            answer="\n".join(parts),
            citations=_dedup_cites(cites),
            confidence="medium",
            unresolved=unresolved,
            took_path=["table", "sql_index"],
        )

    # --------------------------------------------------------
    # 兜底
    # --------------------------------------------------------

    def _answer_fallback(self, q: str, limit: int,
                         why: str = "") -> Answer:
        """
        分不了类或没命中。

        这里**最关键的不做**: 不生成一段"通用解释"。
        旧版就是在这里开始编的 —— 检索没底, 模型照着问题本身编一段
        听起来很合理的话, 用户看不出真假。
        """
        try:
            hits = self.b.search(q, limit=limit) or []
        except Exception as e:
            logger.debug("兜底检索异常: %s", e)
            hits = []

        if not hits:
            return Answer(
                question=q, qtype=QType.UNKNOWN, answer="",
                citations=[], confidence="none",
                unresolved=[
                    why or "问句类型无法判定, 且全文检索无命中",
                    "知识库里没有能支撑这个问题的东西 —— 这本身就是个知识缺口, "
                    "应该去补需求/代码, 而不是让模型编一个",
                ],
                took_path=["classify", "symbols", "code_fts"],
            )

        cites, parts = [], []
        for h in hits[:limit]:
            fp = h.get("file_path", "?")
            ls = h.get("line_start", "?")
            qn = h.get("qualified_name", "?")
            cites.append(Citation("code", f"{fp}:{ls}", qn))
            parts.append(f"· {qn}  ({fp}:{ls})")

        return Answer(
            question=q, qtype=QType.UNKNOWN,
            answer="未能判定问句类型, 以下只是文本命中, **不代表问题被回答了**:\n"
                   + "\n".join(parts),
            citations=_dedup_cites(cites),
            confidence="low",
            unresolved=["建议把问题改写成更具体的问法"
                        "(例如「为什么不能自动上线」「改 X 表影响什么」), "
                        "或先去补齐对应知识"],
            took_path=["classify(未命中)", "code_fts"],
        )

    # --------------------------------------------------------
    # 起跳点识别
    # --------------------------------------------------------

    @staticmethod
    def _guess_table(q: str) -> Optional[str]:
        """
        从问句里认出表名。

        优先认引号包裹的(用户明确指出的), 再认 snake_case 词。
        **不猜没写出来的表名** —— 猜错表会给出完全无关的影响面,
        比说"没识别到"危险得多。
        """
        m = re.findall(r"[`\"']([a-z_][a-z0-9_]{2,})[`\"']", q)
        if m:
            return m[0]
        m = re.findall(r"\b([a-z][a-z0-9]*_[a-z0-9_]+)\b", q)
        return m[0] if m else None

    @staticmethod
    def _guess_symbol(q: str) -> Optional[str]:
        """认出标识符: 带点的驼峰 或 引号包裹的驼峰"""
        m = re.findall(r"\b([A-Z][A-Za-z0-9]{2,}(?:\.[A-Za-z0-9_]+)?)\b", q)
        return m[0] if m else None


# ============================================================
# 辅助
# ============================================================

def cons_any(facts: List[Dict[str, Any]]) -> bool:
    return any(f.get("rule_constraint") for f in facts)


def _dedup_cites(cites: List[Citation]) -> List[Citation]:
    seen, out = set(), []
    for c in cites:
        k = (c.kind, c.ref, c.detail)
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out


# ============================================================
# CLI: python -m src.business_qa <db> <问题>
# ============================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("用法: python -m src.business_qa <index.db> <问题>")
        sys.exit(1)
    db = Path(sys.argv[1])
    domain = db.parent.name
    with IndexBuilder(db, domain) as b:
        for question in sys.argv[2:]:
            print(BusinessQA(b).ask(question).render())
            print()