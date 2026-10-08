# -*- coding: utf-8 -*-
"""
业务事实抽取器 (Fact Extractor)

要解决的问题:
  实测 requirements/1.json 里 actors/main_flow/acceptance/constraints/edge_cases
  五个结构化字段**全为空**, 只有 background 一段自然语言:

      "客服获客助手连接状态从正常标记为异常时客服作下线处理
       标记为自动下线, 支持手动上线, 手动上线时提示: ...
       获客助手连接状态为异常+未触发手动上线期间: 禁止自动上线
       注: 历史数据不作处理, 仅上线后状态为异常进行下线处理"

  而 30 条生成用例里, 「手动上线」「禁止自动上线」「历史数据不处理」
  这三条业务规则的出现次数**全是 0** —— AI 看着代码里的
  updateStatusByAgentId 编了一堆 null/超长/DB 超时, 业务规则全丢。

本模块的三路合流:
  路径1 需求侧: 识别中文规则句式, 从散文里切出候选事实
  路径2 代码侧: 沿调用链 + 数据链路, 把"改什么"落到具体方法/表/字段
  路径3 数据侧: 表字段约束 + 枚举定义 + 动态SQL 标记

关键约束(整套设计的价值所在):
  **证据核对由确定性程序做, 不交给模型。**
  程序只回答"这条 Fact 声称的字段名/表名/方法名, 在证据里出现过吗",
  是/否逐字比对。把幻觉挡在结构里, 而不是靠提示词求模型自律。

另一条硬约束:
  **evidence_count < 2 的事实不入库**(见 FACT_MIN_EVIDENCE)。
  宁可只拿到 30% 的事实, 也不要 100% 的猜测。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from src.call_graph import CallGraphBuilder
from src.code_parser import CodeChunk
from src.comment_parser import extract_chinese_comments
from src.data_link import DataLink, DataLinkBuilder
from src.logger import get_logger
from src.xml_parser import SqlStatement

logger = get_logger(__name__)


# ============================================================
# 数据结构
# ============================================================

# 证据类型
EV_REQUIREMENT = "requirement"
EV_CODE = "code"
EV_SQL = "sql"
EV_TABLE = "table"
EV_ENUM = "enum"
EV_COMMENT = "comment"

# 同源分组前缀: 同组视为同一条证据(需求原文 + 其翻译稿 = 1 条)
# 代码注释里泛滥的通用词 —— 用它们做业务锚点只会连错。
# 「数据」尤其危险: getErrorAttributes 这类方法名完全不相关的方法,
# 只要注释里碰巧出现「数据」二字就会被误连。
_GENERIC_COMMENT_WORDS = frozenset({
    "数据", "信息", "处理", "操作", "结果", "内容", "参数", "返回",
    "获取", "设置", "创建", "更新", "查询", "保存", "状态", "字段",
    "方法", "对象", "列表", "记录", "类型", "数量", "时间", "用户",
    "业务", "系统", "服务", "配置", "功能", "支持", "使用", "根据",
    "如果", "或者", "以及", "并且", "这个", "那个", "可以通过",
})

GROUP_REQUIREMENT = "req"
GROUP_CODE = "code"
GROUP_SQL = "sql"

# 规则句式的优先级: 数字越小越具体, 用于同一句被多句式命中时去重
_KIND_PRIORITY = {
    "forbid": 0,       # 禁止类约束(AI 最容易丢的业务规则)
    "require": 1,      # 必须类约束
    "only_when": 2,    # 仅当...才
    "prompt": 3,       # 提示文案
    "note": 4,         # 备注/注
    "condition": 5,    # 条件
    "auto_action": 6,  # 自动动作
    "trigger": 7,      # 触发动作
    "support": 8,      # 支持手动操作
    "business": 9,     # 兜底
}


# 判定一段文本是否含"限制/否定"意味 —— 用于兜底句式回填约束
_NEGATIVE_HINT = re.compile(
    r"不|仅|只|限|禁止|不得|无需|无须|排除"
)

# 逗号后若以此开头, 说明是上一句的延续(而非新规则), 切分时须合并
_CONTINUATION_HINT = re.compile(
    r"^(?:不|仅|只|限|无|非|除了|其他|其它|除非)"
)


@dataclass
class Evidence:
    ev_type: str
    ref_uri: str
    snippet: str = ""
    ev_group: str = ""
    weight: float = 1.0

    def key(self) -> Tuple[str, str]:
        return (self.ev_type, self.ref_uri)


@dataclass
class FactCandidate:
    """
    候选业务事实。

    尚未入库 —— 必须先过 evidence_count门禁与verify_grounding 核对。
    """
    title: str                      # 业务说法(中文), 一句话说清一件事
    condition: str = ""             # 触发条件
    action: str = ""                # 系统动作
    rule_constraint: str = ""       # 禁止/必须 等强约束
    impact_scope: str = ""          # 影响面
    evidence: List[Evidence] = field(default_factory=list)
    confidence: float = 0.0
    source_kind: str = "auto"

    # ---- 证据相关 ----

    @property
    def evidence_groups(self) -> Set[str]:
        """独立证据源数(按 ev_group 去重)"""
        return {e.ev_group or e.ev_type for e in self.evidence}

    @property
    def evidence_count(self) -> int:
        return len(self.evidence_groups)

    def add_evidence(self, ev: Evidence):
        # 同 (类型, 引用) 去重
        for e in self.evidence:
            if e.key() == ev.key():
                return
        self.evidence.append(ev)

    # ---- 反幻觉白名单 ----

    def ground_names(self) -> Set[str]:
        """
        这条事实关联到的所有真实标识符(方法名/表名/字段名/枚举值)。

        用例生成时被限制只能用这些名字 —— 结构上不给模型编造机会。
        """
        names: Set[str] = set()
        for e in self.evidence:
            text = e.snippet or ""
            for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text):
                names.add(token)
            # 表名带下划线/点, 单独抓
            for token in re.findall(r"\b[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)*\b", text):
                names.add(token)
        return names

    def to_row(self, fact_id: str, domain: str) -> Tuple:
        return (
            fact_id, domain, self.title, self.condition, self.action,
            self.rule_constraint, self.impact_scope,
            round(self.confidence, 3), self.evidence_count,
            self.source_kind, "draft", "v1",
        )

    def to_dict(self) -> Dict:
        """序列化(供 LLM 参考与人工审阅)"""
        return {
            "title": self.title,
            "condition": self.condition,
            "action": self.action,
            "constraint": self.rule_constraint,
            "impact_scope": self.impact_scope,
            "confidence": round(self.confidence, 3),
            "evidence_count": self.evidence_count,
            "evidence": [
                {"type": e.ev_type, "ref": e.ref_uri, "group": e.ev_group}
                for e in self.evidence
            ],
        }


def fact_id_of(fact: FactCandidate) -> str:
    """
    稳定 fact_id: 由标题哈希得出。

    **必须是全系统唯一的 ID 来源**。曾经生成器和入库各算一套 md5,
    结果 case_fact_map 里的 fact_id 跟 facts 表对不上, 覆盖报告永远显示
    0/3 —— 而且不报错, 只是静默地全是 0, 极难发现。

    用标题而非随机数: 同一事实重跑多少次 id 都不变, 才能做增量更新和
    历史对比。代价是标题一改 id 就变, 所以改标题要同步迁移 case_fact_map。
    """
    import hashlib
    return "F-" + hashlib.md5(fact.title.encode("utf-8")).hexdigest()[:8]


# ============================================================
# 路径 1: 需求侧规则句式识别
# ============================================================

# 中文规则句式。每一类对应一种业务事实类型。
# 设计依据: 需求文档里表达业务规则的高频句式, 覆盖实测样本的全部形态。
_RULE_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    # 条件触发: "当...时" / "...时" / "若..." / "如果..."
    ("condition",
     re.compile(r"(?:当|若|如果)[^，。；\n]{2,40}?(?:时|则|后|的情况下)")),

    # 自动动作: "系统自动..." / "自动..." / "...触发..."
    ("auto_action",
     re.compile(r"(?:系统)?(?:自动|定时)[^，。；\n]{2,30}")),

    # 触发动作: 只取冒号后的动作部分。
    # 实测「获客助手连接状态为异常+未触发手动上线期间: 禁止自动线上」
    # 若匹配整句会与 forbid 重复, 且把条件吞进动作里 ——
    # 冒号前是条件、冒号后才是动作, 必须分开。
    ("trigger",
     re.compile(r"[：:]\s*([^，。；：\n]{2,40})")),

    # 禁止类约束(最关键, 也是 AI 最容易丢的)
    #
    # 必须**连主语一起捕获**: 实测「历史数据不作处理」若只从否定词开始匹配,
    # 得到的是「不作处理」—— 丢了「对谁禁止」这个最关键的信息,
    # 生成出来的用例没法知道该约束的对象。
    # 故先吃一段主语(限定长度, 避免吞整句), 再吃否定词及其后内容。
    ("forbid",
     re.compile(
         r"(?:[^，。；：\n]{0,20}?)?"
         r"(?:禁止|不允许|不能|不可|不得|不会|不处理|不作处理|不追溯|不支持|不涉及|不做)"
         r"[^，。；：\n]{0,40}"
     )),

    # 必须类约束
    ("require",
     re.compile(r"(?:必须|需要|要求|应当|应该)[^，。；\n]{2,40}")),

    # 仅当/只有...才: 强约束的另一种表达
    ("only_when",
     re.compile(r"(?:仅当|只有)[^，。；\n]{2,40}?(?:才|时)")),

    # 支持/可操作: 提示交互行为
    ("support",
     re.compile(r"(?:支持|可|可以)[^，。；\n]{0,10}(?:手动|人工)[^，。；\n]{2,30}")),

    # 提示文案: "提示:xxx" / "弹窗xxx"
    ("prompt",
     re.compile(r"(?:提示|弹窗|文案)[：:]\s*([^，。；\n]{2,60})")),

    # 关联/备注: 常含业务边界("历史数据不作处理")
    ("note",
     re.compile(r"[Nn]ote?\s*[:：]\s*([^，。；\n]{2,80})")),

    # 兜底: 包含动词且长度适中的业务句
    ("business",
     re.compile(r"[^，。；\n]{4,50}(?:处理|标记|上线|下线|更新|写入|记录)[^，。；\n]{0,20}")),
)


@dataclass
class RequirementSegment:
    """需求文档切分后的一段(带出处)"""

    index: int
    text: str
    ref_uri: str

    @property
    def group(self) -> str:
        return GROUP_REQUIREMENT


def _plain(item: Any) -> str:
    """
    把结构化需求项压成一行自然语言。

    结构化字段(main_flow / acceptance)的元素常常是 dict, 直接
    json.dumps 会得到 '{"action": "客服下线", "type": "auto"}' 这种串。
    拿它去跑规则句式匹配, 会抽出 '"action": "客服下线"}' 这种**带 JSON
    结构的碎片标题** —— 实测 23 条事实里有 11 条是这种, 业务方看到
    一条叫 '"客服" 的用例根本没法理解。

    取值顺序: 自然语言字段优先(step / desc / title / name / text /
    expected / action), 都取不到才退回整个对象的键值拼接。
    宁可只取 desc 一句, 也不要拼出一坨 JSON。
    """
    if not isinstance(item, dict):
        return str(item)
    for k in ("step", "desc", "description", "title", "name", "text",
              "expected", "expected_result", "action", "content",
              "rule", "note"):
        v = item.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    # 全是结构化字段: 拼接值, 跳过 key 噪声
    vals = [str(v).strip() for v in item.values()
            if isinstance(v, (str, int, float)) and str(v).strip()]
    return " ".join(vals)


class RequirementParser:
    """
    需求侧: 把需求文档切成带出处的段落, 并识别规则句式。

    实测教训: 文档里 \n 常被转义成字面量"\n", 直接按换行切会得到整块文本,
    证据定位就失去意义了。这里两种都处理。
    """

    @staticmethod
    def load(path: Path) -> Dict:
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def segment(req: Dict, ref_base: str) -> List[RequirementSegment]:
        """
        把需求文档切为段落。背景优先, 其次是各结构化字段。

        **背景必须按行拆段, 不能整块当一段**。
        实测 requirements/1.json 的 background 是一段用 \n 分隔的六行文本,
        里面藏着六条互相独立的业务规则(自动下线 / 手动上线 / 禁止自动上线 /
        历史数据不作处理 / ...)。整块作一段时, 六条规则共用同一个 ref_uri,
        于是每条事实的 evidence_count 恒为 1, 门禁把 309 条候选全拒 ——
        而且**不报任何错**, 只是静默地什么都不产出。

        段落粒度 = 独立证据源粒度, 这是整条链路能跑起来的前提。
        """
        segs: List[RequirementSegment] = []

        def _push(text: str, tag: str):
            if not text or not str(text).strip():
                return
            segs.append(RequirementSegment(
                index=len(segs),
                text=str(text).strip(),
                ref_uri=f"{ref_base}#{tag}",
            ))

        def _push_lines(text: str, tag: str):
            """多行文本按行拆段, 每行一个独立证据源"""
            for i, ln in enumerate(RequirementParser.split_lines(text)):
                if not ln:
                    continue
                segs.append(RequirementSegment(
                    index=len(segs),
                    text=ln,
                    ref_uri=f"{ref_base}#{tag}:L{i + 1}",
                ))

        _push_lines(req.get("background", ""), "background")

        # 结构化字段(目前样本全空, 但格式已定, 一旦补齐就自动生效)
        for field_name in ("main_flow", "acceptance", "constraints",
                           "edge_cases", "actors"):
            val = req.get(field_name)
            if isinstance(val, list):
                for j, item in enumerate(val):
                    txt = item if isinstance(item, str) else _plain(item)
                    # 列表项本身也可能含换行(一条需求写了好几行)
                    for k, ln in enumerate(RequirementParser.split_lines(txt)):
                        segs.append(RequirementSegment(
                            index=len(segs), text=ln,
                            ref_uri=f"{ref_base}#{field_name}:{j + 1}"
                                    + (f":L{k + 1}" if len(
                                        RequirementParser.split_lines(txt)) > 1 else ""),
                        ))
            elif val:
                _push_lines(val, field_name)

        return segs

    @staticmethod
    def split_lines(text: str) -> List[str]:
        """
        把可能是转义换行的文本还原成多行。

        **必须循环剥离直到收敛, 单次replace 不够**。
        需求 JSON 在反复导入/导出会层层套转义:
            一次转义  "处理\\n标记"      -> 2 字符
            二次转义  "处理\\\\n标记"    -> 4 字符
            四次转义  "处理\\\\\\\\n"-> 8 字符
        实测 requirements/ 目录里有 6 / 12 / 24 / 48 / 96 个反斜杠的版本,
        都是同一份需求。单次 replace("\n", "\n") 只认两字符形态,
        结果"禁止自动上线\\\\..." 被当成一个整句, 同一规则抽出 6 个
        反斜杠数量不同的变体 —— 标题不同 -> fact_id 不同 -> 库��出现 6 条
        内容其实一样的"业务事实"。这类脏数据比抽不到更坏。

        循环的收敛上限设8 层: 超过就说明数据本身不是转义问题, 应当保留原样
        让人去看, 而不是继续剥离把内容改得面目全非。
        """
        t = text or ""
        for _ in range(8):
            prev = t
            t = t.replace("\\r\\n", "\n").replace("\\n", "\n")
            t = t.replace("\\\\r\\\\n", "\n").replace("\\\\n", "\n")
            if t == prev:
                break
        t = t.replace("\r\n", "\n").replace("\r", "\n")
        lines = [ln.strip() for ln in t.split("\n") if ln.strip()]

        # 行内残留: 连续反斜杠(>=2)不是合法业务文本, 是转义没剥干净。
        # 成对剥离("\\" -> "")再清掉落单的那个, 循环到收敛。
        # 只处理 >=2 的连续串 —— 单个反斜杠可能是内容的一部分(如正则),
        # 一并删掉会改坏原文。
        out: List[str] = []
        for ln in lines:
            for _ in range(8):
                new_ln = re.sub(r"\\{2,}", "", ln)
                if new_ln == ln:
                    break
                ln = new_ln
            ln = ln.replace("\\", "").strip()
            if ln:
                out.append(ln)
        return out

    @classmethod
    def extract_rules(cls, seg: RequirementSegment) -> List[Tuple[str, str]]:
        """
        从一段需求里识别规则。返回 [(kind, matched_text)]。

        两级处理:
          1. 先按标点切成子句 —— 实测「获客助手连接状态为异常+未触发手动上线期间:
             禁止自动上线」整行被 trigger 句式吞掉, 必须拆开才能抽出 forbid
          2. 每个子句再跑全部句式, 同一文本被多句式命中时取优先级最高的 kind

        优先级: forbid/require 比 business 具体, 「禁止自动上线」既是
        forbid 也是 trigger, 但只有 forbid 才是我们要的业务约束。
        """
        best: Dict[str, Tuple[int, str]] = {}

        for line in cls.split_lines(seg.text):
            # 去掉「注:」「关联:」这类前缀标记, 让句式能命中正文
            line = re.sub(r"^\s*(?:注|备注|关联|说明)\s*[:：]\s*", "", line)

            for clause in cls._split_clauses(line):
                for kind, pattern in _RULE_PATTERNS:
                    pri = _KIND_PRIORITY.get(kind, 99)
                    for m in pattern.finditer(clause):
                        text = (m.group(1) if m.groups()
                                else m.group(0)).strip(" :：,，。;；")
                        if not text:
                            continue
                        if text not in best or pri < best[text][0]:
                            best[text] = (pri, kind)

        return [(kind, text) for text, (_pri, kind) in best.items()]

    @staticmethod
    def _split_clauses(line: str) -> List[str]:
        """
        把一行按标点切成子句。

        实测样本里一行常含多条规则:
          「标记为自动下线, 支持手动上线, 手动上线时提示: XXX」
            -> 应拆成 3 条, 而不是整行算一条

        但不能切得太碎 —— **逗号后紧跟否定词时必须与前半句合并**:
          「注: 历史数据不作处理, 仅上线后状态为异常进行下线处理」
            切成「历史数据」+「不作处理」就丢了主语,
            forbid 只匹配到「不作处理」, 规则失去「对谁禁止」的关键信息。
        规则:逗号后若以否定/限定词开头(不/仅/只/限/无/非/除了/其他),
        则视为同一句的后半段, 与前半句合并。
        """
        parts = re.split(r"[,，、;；]", line)
        if not parts:
            return []

        merged: List[str] = []
        for part in parts:
            p = part.strip()
            if not p:
                continue
            if merged and _CONTINUATION_HINT.match(p):
                # 承接上一句: 合并回去, 保住主语
                merged[-1] = f"{merged[-1]},{p}"
            else:
                merged.append(p)
        return merged


# ============================================================
# 路径 2 & 3: 代码侧 + 数据侧
# ============================================================

class CodeEvidenceCollector:
    """
    代码侧 + 数据侧证据收集。

    产出 Evidence 列表, 而不是 Fact —— 因为代码只知道自己做了什么,
    不知道业务上"应该"做什么(那部分来自需求)。两路在 FactExtractor 里合并。
    """

    def __init__(self, chunks: List[CodeChunk],
                 links: Optional[List[DataLink]] = None,
                 comments_by_chunk: Optional[Dict[str, str]] = None):
        self.chunks = chunks
        self.links = links or []
        self.by_id = {c.chunk_id: c for c in chunks}
        self.comments_by_chunk = comments_by_chunk or {}

    # --------------------------------------------------------
    # 代码侧
    # --------------------------------------------------------

    def collect_for_method(self, method_name: str) -> List[Evidence]:
        """
        找出所有名为 method_name 的方法, 连同它的注释与调用链, 产出代码证据。
        """
        out: List[Evidence] = []
        for c in self.chunks:
            if c.chunk_type != "method" or c.name != method_name:
                continue
            out.append(Evidence(
                ev_type=EV_CODE,
                ref_uri=f"{c.file_path}:{c.line_start}",
                snippet=c.qualified_name or c.name,
                # 按文件分组: 同一个文件里的多个同名方法不算多条证据
                ev_group=f"{GROUP_CODE}:{c.file_path}",
            ))
            comment = self.comments_by_chunk.get(c.chunk_id) or \
                extract_chinese_comments(c.code_content or "")
            if comment:
                out.append(Evidence(
                    ev_type=EV_COMMENT,
                    ref_uri=f"{c.file_path}:{c.line_start}#comment",
                    snippet=comment[:300],
                    # 注释与方法是同一处代码, 算 1 条证据
                    ev_group=f"{GROUP_CODE}:{c.file_path}",
                ))
        return out

    def collect_from_links(self, method_name: str) -> List[Evidence]:
        """
        数据侧: 沿数据链路拿到 SQL 与表名。

        这是「数据一致性」类用例唯一可能生成的前提 ——
        没有表名, LLM 只能编一个。
        """
        out: List[Evidence] = []
        for link in self.links:
            if method_name not in link.service_name:
                continue
            # 表与 SQL 来自同一条链路(同一个 stmt), 归为一组证据:
            # "这张表被这段 SQL 写了" 是一个观察, 不是两个。
            group = f"{GROUP_SQL}:{link.stmt.qualified_stmt}"
            if link.tables:
                out.append(Evidence(
                    ev_type=EV_TABLE,
                    ref_uri=link.qualified_table_ref,
                    snippet=" ".join(link.tables),
                    ev_group=group,
                ))
            out.append(Evidence(
                ev_type=EV_SQL,
                ref_uri=link.qualified_table_ref,
                snippet=link.stmt.sql_text[:400],
                ev_group=group,
            ))
        return out

    def collect_enums(self, enum_name: str) -> List[Evidence]:
        """枚举定义证据(状态值不许编的来源)"""
        out: List[Evidence] = []
        for c in self.chunks:
            if c.chunk_type == "enum" and enum_name.lower() in c.name.lower():
                out.append(Evidence(
                    ev_type=EV_ENUM,
                    ref_uri=f"{c.file_path}:{c.line_start}",
                    snippet=_extract_enum_values(c.code_content or ""),
                    ev_group=f"{GROUP_CODE}:{c.file_path}",
                ))
        return out


def _extract_enum_values(code: str) -> str:
    """从 enum 代码里抽枚举值: enum X { A, B, C } -> A, B, C"""
    m = re.search(r"enum\s+\w+\s*\{([^}]*)\}", code, re.S)
    if not m:
        return ""
    body = m.group(1)
    # 去掉参数(带括号的部分)
    body = re.sub(r"\([^)]*\)", "", body)
    vals = [v.strip() for v in body.split(",") if v.strip()]
    return ", ".join(vals[:20])


# ============================================================
# 三路合流
# ============================================================

class FactExtractor:
    """
    从需求 + 代码 + 数据三路合流, 产出候选 Fact。

    用法:
        fx = FactExtractor(chunks, links)
        facts = fx.extract(Path("requirements/1.json"), code_hint="updateStatusByAgentId")
        accepted = [f for f in facts if f.evidence_count >= FACT_MIN_EVIDENCE]

    **LLM 的角色被刻意压缩到最小**:
      - 可以做: 给候选事实起中文标题、补全 condition/action 的表述
      - 不可以做: 决定证据、给出字段名表名、判定事实是否成立
      后面两件事一旦交给模型, 幻觉就回来了。verify_grounding 用确定性
      逐字比对, 任何不在证据里的标识符都会被标出来。
    """

    # 需求里的业务词 -> 代码符号的常见映射(保守, 只覆盖高频模式)
    _TERM_PATTERNS = (
        ("状态", "Status"),
        ("下线", "Offline"),
        ("上线", "Online"),
        ("配置", "Conf"),
        ("客服", "Agent"),
        ("客户", "Customer"),
        ("订单", "Order"),
        ("支付", "Pay"),
        ("活动", "Activity"),
        ("落地页", "LandingPage"),
    )

    def __init__(self, chunks: List[CodeChunk],
                 links: Optional[List[DataLink]] = None):
        self.collector = CodeEvidenceCollector(chunks, links)
        self.chunks = chunks
        # 保留 links 引用: _anchors_from_sql 要靠它做「需求业务词 -> SQL 字段
        # -> stmt_id」反查。这是实测唯一稳定命中的锚点路径 —— 中文注释里
        # 「上线」0 命中, 但 status 字段在 12 条 SQL 里出现。
        self.links = links or []

    # --------------------------------------------------------
    # 主入口
    # --------------------------------------------------------

    def extract(self, req_path: Path, code_hint: str = "") -> List[FactCandidate]:
        """
        从一份需求文档抽取候选事实。

        code_hint 用来定位代码侧锚点(通常由 Stage3 检索得到的方法名)。
        为空时只用需求侧证据 —— 这些 Fact 会因证据不足被门禁拦下, 符合预期:
        没有代码佐证的业务规则, 只能存为 draft 供人工确认。
        """
        req = RequirementParser.load(req_path)
        segs = RequirementParser.segment(req, req_path.name)
        facts: List[FactCandidate] = []

        for seg in segs:
            rules = RequirementParser.extract_rules(seg)
            for kind, text in rules:
                fact = self._build_fact(kind, text, seg, code_hint)
                if fact:
                    facts.append(fact)

        logger.info("需求 %s: 切出 %d 段, 产出 %d 条候选事实",
                    req_path.name, len(segs), len(facts))
        return facts

    # --------------------------------------------------------
    # 单条事实构建
    # --------------------------------------------------------

    def _build_fact(self, kind: str, text: str,
                    seg: RequirementSegment, code_hint: str) -> Optional[FactCandidate]:
        # 兜底句式命中的一般性描述价值低, 除非含限制/否定意味, 否则丢弃。
        # 实测「历史数据不作处理」「仅上线后...处理」这类边界规则
        # 命中的都是 business, 若一律丢弃等于把业务边界扔了。
        if kind == "business" and not _NEGATIVE_HINT.search(text) and not code_hint:
            return None

        fact = FactCandidate(
            title=text[:200],
            source_kind="auto",
        )

        # 需求侧证据(必带, 这是事实的来源)
        #
        # ev_group 用**段号**而不是固定的 "req":
        #   固定值意味着同一份文档里的所有段落共用一个分组, 于是无论抽出
        #   多少条需求证据, evidence_count 永远是 1, 全部被门禁拒收 ——
        #   实测 309 条候选 0 条通过, 整条链路空转。
        # 同理, 需求原文与它的翻译稿仍应归为一段(同源), 这由
        # RequirementParser 在分段时保证, 不在这一层处理。
        fact.add_evidence(Evidence(
            ev_type=EV_REQUIREMENT,
            ref_uri=f"{seg.ref_uri}#rule",
            snippet=text,
            ev_group=f"{GROUP_REQUIREMENT}:{seg.ref_uri}",
        ))

        # 按 kind 填充字段
        if kind == "forbid":
            fact.rule_constraint = f"禁止: {text}"
        elif kind == "require":
            fact.rule_constraint = f"必须: {text}"
        elif kind == "only_when":
            fact.rule_constraint = f"仅当条件满足才允许: {text}"
        elif kind == "auto_action":
            fact.action = text
        elif kind == "trigger":
            fact.condition, fact.action = self._split_trigger(text)
        elif kind == "condition":
            fact.condition = text
        elif kind == "support":
            fact.action = f"支持{text}"
        elif kind == "prompt":
            fact.action = f"提示文案: {text}"
            fact.rule_constraint = f"提示文案必须为: {text}"
        elif kind == "note":
            fact.rule_constraint = f"备注: {text}"
        elif kind == "business":
            # 兜底句式命中但内容含否定/限定的, 仍是约束。
            # 实测「历史数据不作处理」既命中 business 也命中 forbid,
            # 这里保证它至少落在 rule_constraint 里 —— 因为用例的
            # 预期结果正是从constraint 来的, 只放 title 等于丢掉。
            if _NEGATIVE_HINT.search(text):
                fact.rule_constraint = f"限制: {text}"

        # ---- 代码侧证据 ----
        #
        # code_hint 为空时**自动推断锚点**, 不要求调用方一定传。
        # 理由: 实测调用方常常只有一个需求文件, 手里根本没有方法名,
        # 而 _TERM_PATTERNS 里那组中文业务词 -> 代码符号的映射就是
        # 为此准备的。让它只在外部显式传参时才生效, 等于白放。
        hints = [code_hint] if code_hint else []
        hints.extend(self._guess_code_hints(text))
        seen_hints = set()
        for h in hints:
            if not h or h in seen_hints:
                continue
            seen_hints.add(h)
            method_ev = self.collector.collect_for_method(h)
            for e in method_ev:
                fact.add_evidence(e)
            link_ev = self.collector.collect_from_links(h)
            for e in link_ev:
                fact.add_evidence(e)
            enum_name = self._guess_enum(text)
            if enum_name:
                for e in self.collector.collect_enums(enum_name):
                    fact.add_evidence(e)

        fact.confidence = self._score(fact)
        return fact

    @staticmethod
    def _split_trigger(text: str) -> Tuple[str, str]:
        """"异常+未触发手动上线期间: 禁止自动上线" -> (条件, 动作)"""
        if ":" in text or "：" in text:
            head, tail = re.split(r"[：:]", text, maxsplit=1)
            return head.strip(), tail.strip()
        return text, ""

    @classmethod
    def _guess_enum(cls, text: str) -> str:
        """从需求文本猜相关枚举名(保守, 猜不到返回空)"""
        for cn, en in cls._TERM_PATTERNS:
            if cn in text and "Status" in en:
                return f"{en}Enum"
        return ""

    def _guess_code_hints(self, text: str) -> List[str]:
        """
        从需求文本反推可能的代码锚点(方法名)。

        **两条路径, 按可靠性排序**:

        1. 中文注释桥(主路径)
           需求里是中文("上线""下线""客服"), 代码方法上是英文标识符,
           两者之间唯一的真实连接点是**代码里的中文注释**。
           实测 customer-service 域有 18 处方法注释含这些业务词。
           这条路可靠, 因为比对的是同一门语言的原文, 不是翻译。

        2. 中文->英文符号映射(辅助, 保守)
           _TERM_PATTERNS 那组映射只在**全库唯一命中**时才用。
           「客服」-> Agent 是猜测; 库里若有 300 个 AgentXxx 方法,
           随便挑一个当证据等于伪造证据链。

        返回的每个 hint 都必须是**库中真实存在的符号**。
        这正是 verify_grounding 想守住的那条线: 证据是观察, 不是推断。
        """
        if not text or not self.chunks:
            return []
        names = {c.name for c in self.chunks if c.chunk_type == "method"}
        if not names:
            return []

        # ---- 路径 1: 中文注释里直接找
        #
        # 质量闸(必须有, 否则锚点全是噪音):
        #   滑窗 n-gram 会产生大量无区分度的碎片(「手连」「接状」「态为」),
        #   它们在任何中文注释里都可能偶然出现。用两个条件过滤:
        #     1. 命中词本身要有区分度 —— 排除「数据」「处理」「信息」这类
        #        在代码注释里泛滥的通用词;
        #     2. 命中率有下限 —— 只靠一两个碎片命中不算数。
        #   实测不加这个闸时,「获客助手...异常」会命中
        #   getErrorAttributes(只因注释里碰巧有「数据」), 属明显误连。
        words = [w for w in self._content_words(text)
                 if w not in _GENERIC_COMMENT_WORDS]
        hints: List[str] = []
        if words:
            top = set(words[:8])
            for c in self.chunks:
                if c.chunk_type != "method":
                    continue
                blob = ((c.docstring or "") + "\n" +
                        extract_chinese_comments(c.code_content or ""))
                if not blob or len(blob) >= 600:
                    continue      # 块级注释太长, 命中多半是巧合
                overlap = sum(1 for w in top if w in blob)
                if overlap >= 2:
                    hints.append(c.name)
        if hints:
            return list(dict.fromkeys(hints))[:3]

        # ---- 路径 2: 需求业务词 -> 表/字段 -> SQL -> stmt_id
        #
        # 这是实测唯一稳定命中的一条路, 值得放在注释匹配之后:
        #   「客服...上线/下线/状态」-> agent_conf.status
        #   -> UPDATE agent_conf SET status = #{status}
        #   -> AgentConfMapper.updateStatusByAgentId
        #
        # 关键在于它**不靠字面猜**:
        #   业务词先映射到候选字段名(Status), 再要求这些字段名真的出现在
        #   某条 DataLink 的 SQL 文本里。SQL 是结构化的、可精确比对的,
        #   比中文注释可靠得多 —— 注释是自由文本, 什么词都可能写进去。
        #   实测 customer-service 域: 「上线」在注释里 0 命中,
        #   但 status 字段在 12 条 SQL 里出现, 锚点稳稳落在
        #   updateStatusByAgentId 上。
        sql_hints = self._anchors_from_sql(text)
        if sql_hints:
            return sql_hints

        # ---- 路径 2: 保守的符号映射
        lowered = {n.lower(): n for n in names}
        for cn, en in self._TERM_PATTERNS:
            if cn not in text:
                continue
            for cand in (en, en + "Status", en + "Service"):
                real = lowered.get(cand.lower())
                if real:
                    hints.append(real)
                    break
            else:
                matches = [n for n in names if n.lower().startswith(en.lower())]
                if len(matches) == 1:
                    hints.append(matches[0])
        return list(dict.fromkeys(hints))[:3]

    def _anchors_from_sql(self, text: str) -> List[str]:
        """
        从数据链路反查代码锚点: 需求业务词 -> SQL 字段 -> stmt_id。

        **必须按表聚合后再取, 不能"任意含该字段的 SQL 都算"**。
        实测不做聚合时,「禁止自动上线」会挂上 UserMapper ——
        因为 user 表也有 status 字段, 而这条规则跟用户表毫无关系。
        证据挂对文件比不挂更危险: 用例会照着错误出处去断言。

        做法: 统计每张表被命中的业务词权重, 只取权重最高的那张表,
        再返回它的 stmt_id。权重按"命中几个不同的业务词"算 ——
        只命中一个词(status)的表竞争不过同时命中 status+agent 的表。
        """
        if not text or not self.links:
            return []

        # 业务词 -> 候选英文字段(与 _TERM_PATTERNS 同源)
        want = {en.lower() for cn, en in self._TERM_PATTERNS if cn in text}
        if any(k in text for k in ("上线", "下线", "状态", "启用", "停用")):
            want.add("status")
        if not want:
            return []

        # 表 -> {命中字段集合: [stmt_id]}
        by_table: Dict[str, Dict[frozenset, List[str]]] = {}
        for link in self.links:
            sql = (link.stmt.sql_text or "").lower()
            if not sql:
                continue
            hit = frozenset(w for w in want if w in sql)
            if not hit:
                continue
            for tbl in (link.tables or []):
                slot = by_table.setdefault(tbl, {})
                slot.setdefault(hit, []).append(link.stmt.stmt_id)

        if not by_table:
            return []
        # 权重: 命中字段种类数优先, 其次 stmt 数量
        best_tbl = max(
            by_table,
            key=lambda t: (max(len(h) for h in by_table[t]),
                           sum(len(v) for v in by_table[t].values())),
        )
        best_hits = max(by_table[best_tbl], key=len)
        out: List[str] = []
        for sid in by_table[best_tbl][best_hits]:
            if sid not in out:
                out.append(sid)
        return out[:3]

    @staticmethod
    def _content_words(text: str) -> List[str]:
        """
        取需求里的中文 n-gram(2-4 字), 用于与注释做字面比对。

        **必须用滑窗而不是贪婪切词**:
        把「获客助手连接状态为异常+未触发手动上线期间」用 {2,6} 贪婪切,
        会得到 ['获客助手连接', '状态为异常', '未触发手动上', '线期间'],
        而代码注释里写的是「客服」「上线」这种 2-3 字短词, 两者永远对不上
        —— 实测锚点命中率 0, 于是所有事实只剩 1 条需求证据, 全被门禁拒收。

        改成滑窗后, 「未触发手动上线期间」会切出「未触发」「触发手」「动手上」
        「动上线」「上线」..., 「上线」就命中了。这才是能实际 work 的粒度。
        不引入分词器是因为这里只需要最粗的字面判断, 分词反而带来切分歧义。
        """
        cjk = re.compile("[\u4e00-\u9fff]+")
        grams: List[str] = []
        seen = set()
        for run in cjk.findall(text or ""):
            for n in (2, 3, 4):
                for i in range(len(run) - n + 1):
                    g = run[i:i + n]
                    if g not in seen:
                        seen.add(g)
                        grams.append(g)
        # 短片段优先(「上线」「客服」这类 2 字词最稳定, 也最像注释里的写法)
        noise = ("进行", "需要", "如果", "因为", "所以", "但是", "这个",
                 "那个", "一个", "以及", "或者", "可以", "应当", "不得",
                 "情况", "时候", "相关", "并且", "然后", "之后", "之前")
        grams.sort(key=lambda g: (g in noise, len(g)))
        return grams[:40]

    @staticmethod
    def _score(fact: FactCandidate) -> float:
        """
        置信度打分。

        刻意保守: 只有需求侧证据时给 0.4 ——
        这类事实会被 evidence_count>=2 门禁拦下, 分数只用于人工排序。
        """
        base = 0.4
        if fact.evidence_count >= 2:
            base += 0.2
        if any(e.ev_type == EV_SQL for e in fact.evidence):
            base += 0.2
        if any(e.ev_type == EV_COMMENT for e in fact.evidence):
            base += 0.1
        if fact.rule_constraint:
            base += 0.1      # 有明确约束的事实对用例更有价值
        return min(base, 1.0)


# ============================================================
# 证据核对(确定性, 不交给模型)
# ============================================================

def verify_grounding(fact: FactCandidate) -> Tuple[bool, List[str]]:
    """
    核对事实里的标识符是否都在证据中出现。

    返回 (是否通过, 违规标识符列表)。

    这是整套反幻觉的确定性关口:
    **不判断业务逻辑对不对, 只判断名字是不是编的。**
    LLM 可以自由改写中文表述, 但只要它写出的字段名/表名/方法名
    不在证据里, 这里就会拦下。

    用法:
        ok, bad = verify_grounding(fact)
        if not ok:
            raise ValueError(f"幻觉标识符: {bad}")
    """
    allowed = fact.ground_names()
    # 中文词无法与英文标识符比对, 只核对 ASCII 标识符
    suspects = set()
    for text in (fact.title, fact.condition, fact.action,
                 fact.rule_constraint, fact.impact_scope):
        for token in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b", text or ""):
            if token.lower() not in {a.lower() for a in allowed}:
                suspects.add(token)
    # 常见英文虚词不计入幻觉
    suspects -= _COMMON_WORDS
    return (len(suspects) == 0), sorted(suspects)


# 描述性英文里必然出现但不构成幻觉的词
_COMMON_WORDS = {
    "the", "and", "for", "when", "then", "must", "should", "not",
    "status", "user", "id", "api", "url", "http", "https", "json",
    "true", "false", "null", "none", "int", "str", "list", "map",
}


def filter_accepted(facts: List[FactCandidate], min_evidence: int = 2
                    ) -> Tuple[List[FactCandidate], List[FactCandidate]]:
    """
    按证据数门禁过滤。

    返回 (通过, 被拒)。被拒的不是垃圾, 而是"我们知道但证据不足"的知识缺口,
    应当作为待办推动人工补充, 不能静默丢弃。
    """
    passed = [f for f in facts if f.evidence_count >= min_evidence]
    rejected = [f for f in facts if f.evidence_count < min_evidence]
    return passed, rejected
