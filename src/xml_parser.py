"""
MyBatis Mapper XML 解析器 - 数据层证据来源

为什么需要这个模块:
  现有 code_parser 只处理 Java/JS 源码, 对 Mapper XML 完全无感知
  (grep sql/mapper/mybatis 在 code_parser.py 中全空)。
  结果是: 系统里没有任何"表名"的真实来源, LLM 只能自己编表名。

本模块产出三样东西, 全部可追溯到文件与行号:
  1. namespace -> Java Mapper 接口全限定名 (ai.yiye.agent.boss.mapper.AgentConfMapper)
  2. 每条语句的 id / 类型 / SQL 正文
  3. SQL 里出现的表名 (from / join / update / insert into / delete from)

设计上的两个硬约束(反幻觉的关键):
  - 含动态标签(<if> / <foreach> / <choose> / <where>)的语句标记 is_dynamic=True,
    此时表名可能来自动态拼接, 一律标记 table_confidence='low'。
    宁可标记"不确定", 也不猜一个看起来对的表名。
  - 表名只接受纯标识符 (字母/数字/下划线/点/反引号/双引号/schema 前缀),
    遇到 ${} 拼接或子查询别名污染时丢弃该候选。

不做的事:
  - 不做 SQL 语义分析, 不推断 join 关系语义, 不猜索引
  - 不处理注解式 SQL (@Select), 数量极少(实测 9 个), 由调用方另行处理
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Set, Tuple
from xml.etree import ElementTree

from src.logger import get_logger

logger = get_logger(__name__)


# ============================================================
# 数据结构
# ============================================================

# MyBatis 动态标签 - 出现任一即认为该语句是动态的
DYNAMIC_TAGS = frozenset({"if", "foreach", "choose", "when", "otherwise",
                          "where", "set", "trim", "bind", "include"})

# 语句类型 -> 主表提取所用的正则(第一个捕获组即候选表名)
_TABLE_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    # insert into <table>
    ("insert", re.compile(r"\binsert\s+into\s+([A-Za-z_][\w$.`\"]*)", re.I)),
    # update <table>
    ("update", re.compile(r"\bupdate\s+([A-Za-z_][\w$.`\"]*)", re.I)),
    # delete from <table>
    ("delete", re.compile(r"\bdelete\s+from\s+([A-Za-z_][\w$.`\"]*)", re.I)),
    # from <table>
    ("from", re.compile(r"\bfrom\s+([A-Za-z_][\w$.`\"]*)", re.I)),
    # join <table>  (逐一匹配, 一个语句可能有多个 join)
    ("join", re.compile(r"\bjoin\s+([A-Za-z_][\w$.`\"]*)", re.I)),
)

# 明确不是表名的关键字/伪表
_TABLE_DENYLIST = frozenset({
    "select", "dual", "where", "values", "set", "table", "lateral",
    "unnest", "generate_series", "json_each", "information_schema",
})

# MySQL 风格的 hint / 优化器指令, 会被 from 误抓
_HINT_PREFIXES = ("/*+", "optimize", "force", "ignore", "straight_join")


@dataclass
class SqlStatement:
    """一条 SQL 语句(Mapper XML 内的一个 select/update/insert/delete 标签)"""

    # 语句 id, 与 Java Mapper 接口方法同名 -> 这是 Service 到 SQL 的缝合点
    stmt_id: str
    # select / update / insert / delete
    stmt_type: str
    # namespace, 通常是 Java Mapper 接口全限定名
    namespace: str
    # 归一化后的 SQL 正文(动态标签已剔除标记)
    sql_text: str
    # 抽取到的表名(已去重, 已剔除别名与关键字)
    tables: List[str] = field(default_factory=list)
    # 是否含动态标签 -> 表名不可全信
    is_dynamic: bool = False
    # 包含 <include refid="..."> 时为 True, 表名可能来自其他文件
    has_external_include: bool = False
    # 参数占位符, 如 #{status} / ${orderBy}
    params: List[str] = field(default_factory=list)
    # XML 文件绝对路径
    file_path: str = ""
    # 该标签在文件中的起始行(1-based)
    line_start: int = 0

    @property
    def table_confidence(self) -> str:
        """表名可信度: high 可直接作为反幻觉白名单; low 只能作为参考"""
        if self.is_dynamic or self.has_external_include:
            return "low"
        return "high" if self.tables else "none"

    @property
    def qualified_stmt(self) -> str:
        """namespace.stmt_id, 用于与 Java 侧符号精确对齐"""
        return f"{self.namespace}.{self.stmt_id}" if self.namespace else self.stmt_id

    def to_row(self, domain: str) -> Tuple:
        """转成写入 sql_index 表的行"""
        return (
            self.qualified_stmt,
            domain,
            self.namespace,
            self.stmt_id,
            self.stmt_type,
            self.sql_text,
            ",".join(self.tables),
            1 if self.is_dynamic else 0,
            1 if self.has_external_include else 0,
            ",".join(self.params),
            self.file_path,
            self.line_start,
            self.table_confidence,
        )


@dataclass
class MapperFile:
    """一个 Mapper XML 文件的解析结果"""

    file_path: str
    namespace: str
    statements: List[SqlStatement] = field(default_factory=list)

    @property
    def mapper_name(self) -> str:
        """namespace 最后一段, 如 AgentConfMapper"""
        return self.namespace.rsplit(".", 1)[-1] if self.namespace else ""

    @property
    def package(self) -> str:
        """namespace 中包名部分, 如 ai.yiye.agent.boss.mapper"""
        return self.namespace.rsplit(".", 1)[0] if "." in self.namespace else ""


# ============================================================
# 解析器
# ============================================================

class MapperXmlParser:
    """
    MyBatis Mapper XML 解析器

    用法:
        parser = MapperXmlParser()
        mf = parser.parse_file(Path(".../AgentConfMapper.xml"))
        if mf:
            for st in mf.statements:
                print(st.stmt_id, st.tables)

    刻意不用 XML 命名空间做严格校验: 部分项目把 mapper namespace
    写成非标准值, 宽松解析 + 显式告警比直接抛异常更适合做批量索引。
    """

    _SUPPORTED_SUFFIX = ".xml"

    def __init__(self, skip_tables: Optional[Set[str]] = None):
        # 额外的表名排除(如多租户系统里不想索引的影子表)
        self.skip_tables = set(skip_tables or ())

    # --------------------------------------------------------
    # 入口
    # --------------------------------------------------------

    def parse_file(self, path: Path) -> Optional[MapperFile]:
        """解析单个 XML。返回 None 表示不是 Mapper 文件或解析失败。"""
        if path.suffix.lower() != self._SUPPORTED_SUFFIX:
            return None
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("读取 Mapper 失败 %s: %s", path, exc)
            return None
        return self.parse_string(content, str(path))

    def parse_string(self, content: str, file_path: str = "") -> Optional[MapperFile]:
        """解析 XML 字符串内容。"""
        # 先做快速预筛, 避免为无关 XML 付出解析代价
        if "<mapper" not in content or "namespace=" not in content:
            return None
        try:
            root = ElementTree.fromstring(content.encode("utf-8"))
        except ElementTree.ParseError as exc:
            logger.warning("Mapper XML 解析失败 %s: %s", file_path, exc)
            return None

        if root.tag != "mapper":
            return None

        ns_raw = root.get("namespace") or ""
        mf = MapperFile(
            file_path=file_path,
            namespace=self._normalize_ns(ns_raw),
        )

        for child in root:
            if not isinstance(child.tag, str):
                continue  # 注释节点
            stmt_type = child.tag.lower()
            if stmt_type not in ("select", "update", "insert", "delete"):
                continue
            stmt_id = child.get("id")
            if not stmt_id:
                logger.warning("Mapper %s 存在无 id 的 <%s>, 已跳过",
                               file_path or "?", stmt_type)
                continue
            stmt = self._parse_statement(child, stmt_id, stmt_type, mf.namespace)
            stmt.file_path = file_path
            stmt.line_start = self._line_of(content, child)
            mf.statements.append(stmt)

        return mf

    def parse_dir(self, root: Path, pattern: str = "**/*Mapper.xml") -> List[MapperFile]:
        """批量解析一个目录树下的所有 Mapper。"""
        out: List[MapperFile] = []
        if not root.exists():
            logger.warning("Mapper 目录不存在: %s", root)
            return out
        for p in sorted(root.glob(pattern)):
            mf = self.parse_file(p)
            if mf and mf.statements:
                out.append(mf)
        logger.info("解析到 %d 个 Mapper 文件, %d 条 SQL",
                    len(out), sum(len(m.statements) for m in out))
        return out

    # --------------------------------------------------------
    # 内部实现
    # --------------------------------------------------------

    @staticmethod
    def _normalize_ns(ns: str) -> str:
        """归一化 namespace: 去掉前后空白与意外属性残留"""
        return ns.strip()

    @staticmethod
    def _line_of(content: str, el: ElementTree.Element) -> int:
        """
        估算元素在原文中的起始行(1-based)。

        ElementTree 不保留行号, 只能靠标签名在原文中的首次出现来定位。
        对 Mapper 这种结构高度规整的文件, 按 "标签名 + id" 精确匹配已足够;
        匹配不到时回退到 0(未知), 不猜。
        """
        tag = el.tag.rsplit("}", 1)[-1].lower()
        stmt_id = el.get("id") or ""
        if stmt_id:
            needle = f'<{tag} id="{stmt_id}"'
            idx = content.find(needle)
            if idx < 0:
                # 属性顺序可能不同, 退化为只找 id 值
                idx = content.find(f'"{stmt_id}"')
            if idx < 0:
                return 0
        else:
            idx = content.find(f"<{tag}")
            if idx < 0:
                return 0
        return content.count("\n", 0, idx) + 1

    def _parse_statement(self, el: ElementTree.Element, stmt_id: str,
                         stmt_type: str, namespace: str) -> SqlStatement:
        # SQL 正文: 直接取所有文本(含动态标签的静态文本部分)
        raw_sql = self._collect_text(el)
        sql_text = self._normalize_sql(raw_sql)

        tags = self._collect_dynamic_tags(el)
        stmt = SqlStatement(
            stmt_id=stmt_id,
            stmt_type=stmt_type,
            namespace=namespace,
            sql_text=sql_text,
            is_dynamic=bool(tags),
            has_external_include="include" in tags,
            params=self._extract_params(sql_text),
        )
        # CTE 名(WITH xxx AS)不是数据库表, 抽表名前必须先剔除
        cte_names = self._extract_cte_names(sql_text)
        stmt.tables = self._extract_tables(sql_text, stmt_type, cte_names=cte_names)
        return stmt

    @staticmethod
    def _extract_cte_names(sql: str) -> Set[str]:
        """
        抽取 CTE(公用表表达式)名称。

        WITH [RECURSIVE] name AS ( ... ) [, name2 AS ( ... ) ] SELECT ...
        CTE 名后面必然跟 AS (, 与表名的区别就在于它出现在 SELECT 之前。
        只需从整份 SQL 头部抽, 无需做完整语法分析。
        """
        names: Set[str] = set()
        # with [recursive] <ident> as (
        for m in re.finditer(r"\bwith\s+(?:recursive\s+)?([A-Za-z_][\w$]*)\s+as\s*\(",
                             sql, re.I):
            names.add(m.group(1).lower())
        # 后续逗号分隔的 CTE: , name as (
        for m in re.finditer(r",\s*([A-Za-z_][\w$]*)\s+as\s*\(", sql, re.I):
            names.add(m.group(1).lower())
        return names

    def _collect_text(self, el: ElementTree.Element) -> str:
        """
        收集元素下所有文本(含子元素 tail)。
        动态标签自身的内容也会被收进来 —— 这是刻意的:
        静态分支里的表名仍是有效线索, 只是整体可信度要靠 is_dynamic 标记降级。
        """
        parts = [el.text or ""]
        for child in el:
            parts.append(self._collect_text(child))
            parts.append(child.tail or "")
        return " ".join(p for p in parts if p)

    @staticmethod
    def _collect_dynamic_tags(el: ElementTree.Element) -> Set[str]:
        """递归收集出现的动态标签名"""
        found: Set[str] = set()
        for node in el.iter():
            if isinstance(node.tag, str):
                base = node.tag.rsplit("}", 1)[-1].lower()
                if base in DYNAMIC_TAGS:
                    found.add(base)
        return found

    @staticmethod
    def _normalize_sql(sql: str) -> str:
        """压缩空白, 便于存储与后续展示"""
        return re.sub(r"\s+", " ", sql).strip()

    @staticmethod
    def _extract_params(sql: str) -> List[str]:
        """
        抽取参数占位符。
        #{} 是预编译占位符 -> 参数名
        ${} 是字符串拼接 -> 有注入风险, 同样记录下来(供人工审查)
        """
        params: List[str] = []
        for m in re.finditer(r"[#$]\{([^}]+)\}", sql):
            name = m.group(1).strip()
            if name and name not in params:
                params.append(name)
        return params

    def _extract_tables(self, sql: str, stmt_type: str,
                        cte_names: Optional[Set[str]] = None) -> List[str]:
        """
        抽取表名。

        策略:
        1. 主表按语句类型用对应正则优先抽取(insert/update/delete 明确知道主表是谁)
        2. 再统一抽 from / join 作为补充
        3. 统一走 _clean_table_name 清洗, 别名(a AS b)会被正确去掉
        4. 剔除 CTE 名(WITH xxx AS), 它们不是数据库表
        """
        cte_names = {n.lower() for n in (cte_names or set())}
        found: List[str] = []

        def _add(raw: str) -> None:
            t = self._clean_table_name(raw)
            if t and t.lower() not in cte_names and t not in found:
                found.append(t)

        # 主表优先
        primary_key = {"insert": "insert", "update": "update", "delete": "delete"}.get(stmt_type)
        if primary_key:
            for kind, pattern in _TABLE_PATTERNS:
                if kind == primary_key:
                    for m in pattern.finditer(sql):
                        _add(m.group(1))

        # from / join 补充
        for kind, pattern in _TABLE_PATTERNS:
            if kind == primary_key:
                continue
            for m in pattern.finditer(sql):
                _add(m.group(1))

        return found

    def _clean_table_name(self, raw: str) -> Optional[str]:
        """
        清洗单个表名候选。返回 None 表示丢弃。

        丢弃情形:
        - 关键字 / 伪表
        - 含 $ 的动态拼接 (${tableName}) -> 无法静态确定
        - 纯数字或过短
        - 命中优化器 hint
        """
        if not raw:
            return None
        t = raw.strip().strip("`\"")
        if not t or "$" in t:
            return None
        low = t.lower()
        if low in _TABLE_DENYLIST:
            return None
        if any(low.startswith(h) for h in _HINT_PREFIXES):
            return None
        if len(t) < 2 or not re.match(r"^[A-Za-z_][\w$.]*$", t):
            return None
        # 允许 schema.table 形式, 但拒绝尾随点或连续点
        if ".." in t or t.endswith("."):
            return None
        if self.skip_tables and t in self.skip_tables:
            return None
        return t


# ============================================================
# 建表 DDL(供 index_builder.SCHEMA_SQL 拼接)
# ============================================================

SQL_INDEX_DDL = """
-- 数据层语句索引 (由 xml_parser 产出)
CREATE TABLE IF NOT EXISTS sql_index (
    qualified_stmt TEXT PRIMARY KEY,   -- namespace.stmt_id, 与 Java 方法同名即缝合点
    domain TEXT NOT NULL,
    namespace TEXT,                    -- Java Mapper 接口全限定名
    stmt_id TEXT,                -- 方法名
    stmt_type TEXT,                    -- select / update / insert / delete
    sql_text TEXT,
    tables TEXT,                        -- 逗号分隔的表名
    is_dynamic INTEGER DEFAULT 0,       -- 含 <if>/<foreach> 等动态标签
    has_external_include INTEGER DEFAULT 0,
    params TEXT,                        -- 占位符名, 逗号分隔
    file_path TEXT,
    line_start INTEGER,
    table_confidence TEXT               -- high / low / none
);

CREATE INDEX IF NOT EXISTS idx_sqlindex_domain ON sql_index(domain);
CREATE INDEX IF NOT EXISTS idx_sqlindex_tables ON sql_index(tables);
CREATE INDEX IF NOT EXISTS idx_sqlindex_stmttype ON sql_index(stmt_type);
"""
