"""
索引构建器 - SQLite + FTS5

建立 3 类索引:
1. 符号索引 (精确查找函数/类)
2. 倒排索引 (关键词全文检索)
3. 调用关系图 (调用链扩展)
"""

import sqlite3
import json
import re
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple, TYPE_CHECKING
from datetime import datetime

from src.logger import get_logger
from src.code_parser import CodeChunk
from src.comment_parser import extract_chinese_comments

if TYPE_CHECKING:  # 避免运行期循环导入
    from src.xml_parser import SqlStatement

logger = get_logger(__name__)


# ============================================================
# 索引 Schema
# ============================================================

SCHEMA_SQL = """
-- 符号表
CREATE TABLE IF NOT EXISTS symbols (
    chunk_id TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_type TEXT,
    language TEXT,
    chunk_type TEXT,
    name TEXT,
    qualified_name TEXT,
    line_start INTEGER,
    line_end INTEGER,
    http_route TEXT,
    business_tags TEXT,        -- JSON array
    summary TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_symbols_domain ON symbols(domain);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_qualified ON symbols(qualified_name);
CREATE INDEX IF NOT EXISTS idx_symbols_type ON symbols(chunk_type);
CREATE INDEX IF NOT EXISTS idx_symbols_http ON symbols(http_route);

-- 调用关系表
-- resolution 记录缝合可信度, 这是跨文件调用图的关键:
--   exact     方法名+包路径唯一确定
--   ambiguous 同名候选多个, 已记候选但未选定(绝不瞎选)
--   guess     仅方法名匹配, 包路径未验证
-- 宁可标 ambiguous 也不连错 —— 连错会生成指向错误表的用例, 比不连危害大得多。
CREATE TABLE IF NOT EXISTS relations (
    from_chunk_id TEXT NOT NULL,
    to_chunk_id TEXT NOT NULL,
    relation_type TEXT NOT NULL,    -- 'call' / 'inject' / 'extend' / 'feign'
    resolution TEXT DEFAULT 'exact',
    candidates TEXT,                -- 候选 chunk_id 逗号分隔(ambiguous 时)
    PRIMARY KEY (from_chunk_id, to_chunk_id, relation_type)
);

CREATE INDEX IF NOT EXISTS idx_relations_from ON relations(from_chunk_id);
CREATE INDEX IF NOT EXISTS idx_relations_to ON relations(to_chunk_id);

-- 全文检索 (FTS5 虚拟表)
-- comment_text: 中文注释/注解独立成列, 让业务词能被检索到。
-- 代码里本来就有大量中文注释(实测 551 处), 但混在 code_content 里
-- 会被英文标识符挤掉, 单独成列后中文业务语义才有召回路径。
CREATE VIRTUAL TABLE IF NOT EXISTS code_fts USING fts5(
    chunk_id UNINDEXED,
    domain UNINDEXED,
    file_path,
    name,
    qualified_name,
    code_content,
    summary,
    comment_text,
    tokenize = 'unicode61 remove_diacritics 2'
);

-- 元数据表 (chunk 完整内容)
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    file_path TEXT NOT NULL,
    chunk_data TEXT NOT NULL,       -- 完整 CodeChunk JSON
    indexed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_chunks_domain ON chunks(domain);

-- 索引元信息
CREATE TABLE IF NOT EXISTS index_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- SQL 语句表 (数据层证据的唯一来源)
--
-- 为什么必须有这张表: Mapper XML 不进代码树(tree-sitter 解析不了 XML 标签),
-- 之前只能全文 grep 'sql|mapper|mybatis', 实测结果全空 —— 结果是 LLM
-- 写用例时表名纯靠猜。抽出这张表后, 表名才有出处, 数据一致性断言才立得住。
CREATE TABLE IF NOT EXISTS sql_index (
    qualified_stmt TEXT PRIMARY KEY,  -- namespace.stmtId, 如 AgentConfMapper.updateStatusByAgentId
    domain TEXT NOT NULL,
    namespace TEXT,
    stmt_id TEXT,
    stmt_type TEXT,                   -- select / insert / update / delete
    sql_text TEXT,
    tables TEXT,                      -- CSV, 逗号分隔(已剔除 CTE 名)
    is_dynamic INTEGER DEFAULT 0,     -- 含 <if>/<foreach> 等动态标签
    has_external_include INTEGER DEFAULT 0,  -- 含 <include>, 表名在别处
    params TEXT,                      -- CSV, #{} 参数
    file_path TEXT,
    line_start INTEGER,
    table_confidence TEXT             -- high / low / none
);

CREATE INDEX IF NOT EXISTS idx_sql_domain ON sql_index(domain);
CREATE INDEX IF NOT EXISTS idx_sql_tables ON sql_index(tables);
CREATE INDEX IF NOT EXISTS idx_sql_conf ON sql_index(table_confidence);
"""


# ============================================================
# Fact 层 Schema
# ============================================================
#
# 为什么要有这一层:
#   现有索引的单元是"代码符号", 检索返回代码片段。实测需求里
#   「异常期间禁止自动上线」这类业务规则, 在代码里根本没有对应字面量,
#   导致 AI 只能围着一个孤立方法编技术边界用例, 业务规则 0 覆盖。
#
#   Fact 把"业务事实"提升为一等公民: 一条规则 = 条件 + 动作 + 约束 + 影响面,
#   并通过 fact_evidence 指向全部出处(需求段号 / 代码行 / SQL / 表)。
#   有了它, 用例生成变成"照事实翻译", 业务问答变成"事实 + 出处"。
#
# 核心硬约束 (由 FACT_MIN_EVIDENCE 强制):
#   evidence_count < 2 的事实不允许入库。同源证据不算数
#   (需求原文 + 它的翻译稿 = 1 条, 不是 2 条)。
#   这会把"不知道"显式暴露出来, 而不是藏在一份看起来很全、
#   实际全是猜的文档里。

FACT_MIN_EVIDENCE = 2


def _split_csv(raw: Any) -> List[str]:
    """
    拆逗号分隔列(sql_index.tables / params)。

    这两列存的是 CSV 而非 JSON(见 SqlStatement.to_row)。
    早期 schema 注释写成 JSON 数组, 读取侧就照 JSON 解析, 结果全部失败
    且静默返回空列表 —— 表现是"表名一个都查不到", 很容易误判成"没有表"。
    两种格式都兼容, 免得旧库里已经写进去的 JSON 行读不出来。
    """
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw if str(x).strip()]
    s = str(raw).strip()
    if s.startswith("["):
        try:
            arr = json.loads(s)
            return [str(x).strip() for x in arr if str(x).strip()]
        except (json.JSONDecodeError, TypeError):
            pass
    return [x.strip() for x in s.split(",") if x.strip()]


def _load_json_list(raw: Any) -> List[Any]:
    """
    解码 cases 表里的 JSON 数组列。

    与 _split_csv 成对存在: 写入侧一律 json.dumps, 读取侧必须照同样格式解,
    格式不一致时 json.loads 会抛异常 —— 早期 `tables` 列存 CSV 而读取侧按
    JSON 解析就是这个坑的原型(静默 0 命中, 表现为"这个表没被碰过")。
    """
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        return list(raw)
    try:
        v = json.loads(str(raw))
    except (json.JSONDecodeError, TypeError):
        return []
    return v if isinstance(v, list) else []

FACT_SCHEMA_SQL = """
-- 业务事实表
CREATE TABLE IF NOT EXISTS facts (
    fact_id TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    title TEXT NOT NULL,           -- 业务说法(中文), 如「客服异常自动下线」
    condition TEXT,                -- 触发条件 (condition 也不是保留字)
    action TEXT,                   -- 系统动作
    rule_constraint TEXT,-- 禁止/必须等强约束, 用例的预期结果主要来源
                                    -- 列名避开 constraint(是 SQL 标准保留字)
    impact_scope TEXT,              -- 影响面(改这里会影响谁)
    confidence REAL DEFAULT 0,     -- 0~1, 由证据强度与冲突决定
    evidence_count INTEGER DEFAULT 0,  -- 独立证据源数, < 2 拒收
    source_kind TEXT,              -- manual / auto / merged
    status TEXT DEFAULT 'draft',   -- draft / reviewed / verified / deprecated
    version TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_facts_domain ON facts(domain);
CREATE INDEX IF NOT EXISTS idx_facts_status ON facts(status);
CREATE INDEX IF NOT EXISTS idx_facts_title ON facts(title);

-- 事实 <-> 证据关联。一条事实可挂多条证据, 每条都带文件与行号。
CREATE TABLE IF NOT EXISTS fact_evidence (
    fact_id TEXT NOT NULL,
    ev_type TEXT NOT NULL,   -- requirement / code / sql / table / enum / annotation / comment / defect
    ref_uri TEXT,            -- 路径:行号, 或 需求文件#段号
    snippet TEXT,            -- 原文片段, 供业务问答直接引用
    ev_group TEXT,           -- 同源标识: 同一组视为 1 条证据
    weight REAL DEFAULT 1.0,
    PRIMARY KEY (fact_id, ev_type, ref_uri)
);

CREATE INDEX IF NOT EXISTS idx_fev_fact ON fact_evidence(fact_id);
CREATE INDEX IF NOT EXISTS idx_fev_type ON fact_evidence(ev_type);
CREATE INDEX IF NOT EXISTS idx_fev_group ON fact_evidence(ev_group);

-- 业务术语映射: 中文业务词 <-> 代码符号
-- 取代原llm_recall.py 里硬编码的 20 个业务词(只覆盖一个域,换域即失效)
CREATE TABLE IF NOT EXISTS term_alias (
    alias_id TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    term TEXT NOT NULL,           -- 中文业务词, 如「获客助手」
    symbol TEXT,                  -- 代码符号, 如 AcquisitionAgentService
    symbol_kind TEXT,             -- class / method / table / field / enum
    source TEXT,                  -- auto_comment / auto_annotation / manual
    confidence REAL DEFAULT 0.5,
    created_at TEXT,
    UNIQUE (domain, term, symbol)
);

CREATE INDEX IF NOT EXISTS idx_alias_term ON term_alias(term);
CREATE INDEX IF NOT EXISTS idx_alias_symbol ON term_alias(symbol);
CREATE INDEX IF NOT EXISTS idx_alias_domain ON term_alias(domain);

-- 用例 <-> 事实回填。业务规则保真校验的唯一抓手:
-- 每条用例必须回填 fact_id, 否则无法回答「哪条业务规则没被测到」。
CREATE TABLE IF NOT EXISTS case_fact_map (
    case_id TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    task_id TEXT,
    coverage TEXT,               -- covered / partial / not_covered
    created_at TEXT,
    PRIMARY KEY (case_id, fact_id)
);

CREATE INDEX IF NOT EXISTS idx_cfm_fact ON case_fact_map(fact_id);
CREATE INDEX IF NOT EXISTS idx_cfm_case ON case_fact_map(case_id);

-- 用例正文表
--
-- 为什么必须有: case_fact_map 只存 case_id -> fact_id 的映射, 用例本身
-- 在内存里生成完就丢了。后果是"这条规则到底被哪条用例测了、怎么测的"
-- 答不出来 —— 业务问答里「有没有测过」「怎么验证的」这两问会退化成
-- 只能给一个计数, 给不出用例标题与预期结果。
CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    task_id TEXT,
    fact_id TEXT,                     -- 来源业务事实 (外键语义, 不强制)
    title TEXT NOT NULL,
    dimension TEXT,                   -- 七维度 key
    dimension_name TEXT,              -- 中文名
    priority TEXT,                    -- P0 / P1 / P2
    preconditions TEXT,               -- JSON array
    steps TEXT,                       -- JSON array
    expected_result TEXT,
    assertions TEXT,                  -- JSON array
    data_setup TEXT,                  -- JSON array
    code_refs TEXT,                   -- CSV
    table_refs TEXT,                  -- CSV
    fact_title TEXT,
    determinacy TEXT,                 -- ok / vague / empty (生成时体检结论)
    determinacy_issues TEXT,          -- CSV, 模糊表述/缺具体值的问题
    human_approved INTEGER DEFAULT 0,
    generated_by TEXT,                -- fact_driven_generator / llm_assisted
    created_at TEXT,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_cases_domain ON cases(domain);
CREATE INDEX IF NOT EXISTS idx_cases_fact ON cases(fact_id);
CREATE INDEX IF NOT EXISTS idx_cases_dim ON cases(dimension);
CREATE INDEX IF NOT EXISTS idx_cases_task ON cases(task_id);
CREATE INDEX IF NOT EXISTS idx_cases_determinacy ON cases(determinacy);
"""


# ============================================================
# 索引构建器
# ============================================================

class IndexBuilder:
    """代码索引构建器"""

    def __init__(self, db_path: Path, domain: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.domain = domain
        self._conn: Optional[sqlite3.Connection] = None

    def __enter__(self):
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.executescript(SCHEMA_SQL)
        self._conn.executescript(FACT_SCHEMA_SQL)
        self._ensure_column_migrations()
        self._ensure_relation_columns()
        self._conn.commit()
        logger.debug(f"索引已打开: {self.db_path}")
        return self

    def _ensure_column_migrations(self):
        """
        向后兼容迁移。

        FTS5 虚拟表无法 ALTER ADD COLUMN, 所以 comment_text 只能在
        SCHEMA_SQL 里声明; 若索引库是旧版本建的, code_fts 没有这一列,
        这里检测到就重建整张 FTS 表(数据在 symbols 表里, 可重灌)。
        """
        cur = self._conn.cursor()
        fts_cols = {r[1] for r in cur.execute("PRAGMA table_info(code_fts)")}
        if not fts_cols or "comment_text" in fts_cols:
            return

        logger.info("检测到旧版 code_fts (缺 comment_text), 重建并重灌")
        cur.execute("DROP TABLE IF EXISTS code_fts")
        cur.execute(
            """CREATE VIRTUAL TABLE code_fts USING fts5(
                   chunk_id UNINDEXED, domain UNINDEXED, file_path,
                   name, qualified_name, code_content, summary, comment_text,
                   tokenize = 'unicode61 remove_diacritics 2'
               )"""
        )
        # 从 symbols 回灌(symbols 里没有 comment_text, 先留空由下次构建填)
        cur.execute(
            """INSERT INTO code_fts
                   (chunk_id, domain, file_path, name, qualified_name,
                    code_content, summary, comment_text)
               SELECT s.chunk_id, s.domain, s.file_path, s.name, s.qualified_name,
                      s.summary, s.summary, ''
               FROM symbols s"""
        )
        logger.info("code_fts 重建完成, 已回灌 %d 行",
                    cur.execute("SELECT count(*) FROM code_fts").fetchone()[0])

    def _ensure_relation_columns(self):
        """旧版 relations 表没有 resolution/candidates 两列, 补上"""
        cur = self._conn.cursor()
        cols = {r[1] for r in cur.execute("PRAGMA table_info(relations)")}
        if not cols or "resolution" in cols:
            return
        logger.info("检测到旧版 relations (缺 resolution 列), 执行迁移")
        cur.execute("ALTER TABLE relations ADD COLUMN resolution TEXT DEFAULT 'exact'")
        cur.execute("ALTER TABLE relations ADD COLUMN candidates TEXT")

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._conn:
            self._conn.close()

    def clear_fact_layer(self) -> int:
        """
        只清 Fact 层(facts / fact_evidence / cases / case_fact_map / term_alias),
        保留符号索引与调用图。

        单独拆出来是因为有两个调用方, 需求不同:
          - clear_domain(): 全量重建, 符号索引也要清
          - tools/backfill_facts.py: 只重跑 Fact 层, 符号索引得留着
        之前这两处各写了一遍清库逻辑, 结果各清一半 —— 全量重建那条把
        映射漏了, 重跑后同一个 case_id 同时挂着新旧 fact_id。
        返回被清掉的事实条数。

        facts / fact_evidence / case_fact_map 没有 domain 列, 只能靠
        facts.domain 关联清; 必须先删映射与证据再删 facts, 否则留下无主外键。
        """
        cur = self._conn.cursor()
        cur.execute("DELETE FROM cases WHERE domain=?", (self.domain,))
        fact_ids = [r[0] for r in cur.execute(
            "SELECT fact_id FROM facts WHERE domain=?", (self.domain,))]
        for fid in fact_ids:
            cur.execute("DELETE FROM fact_evidence WHERE fact_id=?", (fid,))
            cur.execute("DELETE FROM case_fact_map WHERE fact_id=?", (fid,))
        cur.execute("DELETE FROM facts WHERE domain=?", (self.domain,))
        cur.execute("DELETE FROM term_alias WHERE domain=?", (self.domain,))
        return len(fact_ids)

    def clear_domain(self):
        """
        清空某个域的所有索引数据。

        **Fact 层也必须一起清**(clear_fact_layer), 否则重跑构建会留下一堆
        指向旧事实的悬挂映射: `case_id` 每次生成都从 0001 重新编号
        (`FACT-0001`...), 而 fact_id 是内容哈希、需求文件一变就变。
        于是同一个 `FACT-0004` 会同时挂着上一轮的 fact_id 和这一轮的 ——
        `case_fact_map` 主键是 `(case_id, fact_id)`, INSERT OR REPLACE
        覆盖不掉另一条, 覆盖报告就会把一条用例算到两个业务规则头上。

        实测 customer-service 重跑后 138 条用例里有 98 条挂着 2 个 fact_id。
        **症状是数字变大而不是报错**, 所以只能靠"清干净"来防。
        """
        cur = self._conn.cursor()
        cur.execute("DELETE FROM symbols WHERE domain=?", (self.domain,))
        cur.execute(
            "DELETE FROM code_fts WHERE chunk_id IN "
            "(SELECT chunk_id FROM chunks WHERE domain=?)",
            (self.domain,),
        )
        cur.execute("DELETE FROM chunks WHERE domain=?", (self.domain,))
        n_facts = self.clear_fact_layer()
        # relations 涉及跨域, 不在这里清, 单独处理
        self._conn.commit()
        logger.info(f"已清空域 {self.domain} 的索引"
                    f"(含 {n_facts} 条业务事实)")

    def add_chunk(self, chunk: CodeChunk, business_tags: List[str] = None,
                  summary: str = ""):
        """添加一个 chunk 到索引"""
        cur = self._conn.cursor()
        now = datetime.now().isoformat()

        tags_json = json.dumps(business_tags or [], ensure_ascii=False)

        # symbols 表
        cur.execute(
            """INSERT OR REPLACE INTO symbols
               (chunk_id, domain, file_path, file_type, language, chunk_type,
                name, qualified_name, line_start, line_end, http_route,
                business_tags, summary, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (chunk.chunk_id, self.domain, chunk.file_path,
             chunk.file_type, chunk.language, chunk.chunk_type,
             chunk.name, chunk.qualified_name,
             chunk.line_start, chunk.line_end, chunk.http_route,
             tags_json, summary, now, now),
        )

        # chunks 表 (完整数据)
        cur.execute(
            """INSERT OR REPLACE INTO chunks
               (chunk_id, domain, file_path, chunk_data, indexed_at)
               VALUES (?, ?, ?, ?, ?)""",
            (chunk.chunk_id, self.domain, chunk.file_path,
             json.dumps(chunk.to_dict(), ensure_ascii=False), now),
        )

        # FTS5 索引 (允许重复: 先删旧的, 再插新的, 避免 UNIQUE 冲突)
        cur.execute(
            "DELETE FROM code_fts WHERE chunk_id=?",
            (chunk.chunk_id,),
        )
        comment_text = extract_chinese_comments(chunk.code_content or "")
        cur.execute(
            """INSERT INTO code_fts
               (chunk_id, domain, file_path, name, qualified_name,
                code_content, summary, comment_text)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (chunk.chunk_id, self.domain, chunk.file_path,
             chunk.name, chunk.qualified_name,
             chunk.code_content, summary, comment_text),
        )

    def add_sql_statement(self, stmt: "SqlStatement") -> bool:
        """
        写入一条 SQL 语句到 sql_index(数据层证据来源)。

        幂等: qualified_stmt 为主键, 重复写入覆盖。
        返回 True 表示写入成功。
        """
        if not stmt or not stmt.qualified_stmt:
            return False
        cur = self._conn.cursor()
        cur.execute(
            """INSERT OR REPLACE INTO sql_index
               (qualified_stmt, domain, namespace, stmt_id, stmt_type,
                sql_text, tables, is_dynamic, has_external_include,
                params, file_path, line_start, table_confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            stmt.to_row(self.domain),
        )
        return True

    def add_relation(self, from_chunk_id: str, to_chunk_id: str,
                     relation_type: str = "call",
                     resolution: str = "exact",
                     candidates: Optional[str] = None):
        """
        添加调用关系。

        resolution 说明缝合可信度:
          exact     - 方法名+包路径唯一确定
          ambiguous - 同名候选多个, 已记录候选但未选定(绝不瞎选)
          guess     - 仅方法名匹配, 跨文件, 包路径未验证
        """
        cur = self._conn.cursor()
        # 旧库可能没有这两列, 迁移时补上
        cols = {r[1] for r in cur.execute("PRAGMA table_info(relations)")}
        if "resolution" in cols:
            cur.execute(
                """INSERT OR REPLACE INTO relations
                   (from_chunk_id, to_chunk_id, relation_type,
                    resolution, candidates)
                   VALUES (?, ?, ?, ?, ?)""",
                (from_chunk_id, to_chunk_id, relation_type,
                 resolution, candidates),
            )
        else:
            cur.execute(
                """INSERT OR IGNORE INTO relations
                   (from_chunk_id, to_chunk_id, relation_type)
                   VALUES (?, ?, ?)""",
                (from_chunk_id, to_chunk_id, relation_type),
            )

    def commit(self):
        if self._conn:
            self._conn.commit()

    # ============================================================
    # Fact 层: 业务事实入库与检索
    # ============================================================

    def add_fact(self, fact, min_evidence: int = FACT_MIN_EVIDENCE
                 ) -> Tuple[bool, str]:
        """
        写入一条业务事实及其全部证据。

        返回 (是否入库, 原因)。**门禁在这里生效**:
          - 证据数(按 ev_group 去重) < min_evidence -> 拒收
          - verify_grounding 未通过 -> 拒收

        拒收的理由会原样返回, 因为"我们知道有这条规则但证据不足"
        本身就是要跟进的知识缺口, 不能静默丢弃, 也不能降级塞进去。
        """
        from src.fact_extractor import verify_grounding, fact_id_of

        if fact.evidence_count < min_evidence:
            return False, (f"证据不足: {fact.evidence_count} < {min_evidence}")

        ok, bad = verify_grounding(fact)
        if not ok:
            return False, f"幻觉标识符未在证据中出现: {bad}"

        from datetime import datetime as _dt
        now = _dt.now().isoformat()
        fact_id = fact_id_of(fact)
        cur = self._conn.cursor()
        cur.execute(
            """INSERT OR REPLACE INTO facts
               (fact_id, domain, title, condition, action, rule_constraint,
                impact_scope, confidence, evidence_count, source_kind,
                status, version, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (fact_id, self.domain, fact.title, fact.condition,
             fact.action, fact.rule_constraint, fact.impact_scope,
             fact.confidence, fact.evidence_count, fact.source_kind,
             "accepted", "v1", now, now),
        )
        # 证据整体替换(fact_id 稳定, 重跑不会残留旧证据)
        cur.execute("DELETE FROM fact_evidence WHERE fact_id=?", (fact_id,))
        for ev in fact.evidence:
            cur.execute(
                """INSERT OR REPLACE INTO fact_evidence
                   (fact_id, ev_type, ref_uri, snippet, ev_group, weight)
                   VALUES (?,?,?,?,?,?)""",
                (fact_id, ev.ev_type, ev.ref_uri or "",
                 ev.snippet or "", ev.ev_group or ev.ev_type, ev.weight),
            )
        return True, "accepted"

    def get_facts(self, domain: Optional[str] = None,
                  status: Optional[str] = "accepted") -> List[Dict[str, Any]]:
        """取业务事实(带证据)"""
        cur = self._conn.cursor()
        sql = "SELECT * FROM facts WHERE 1=1"
        args: List[Any] = []
        if domain:
            sql += " AND domain=?"
            args.append(domain)
        if status:
            sql += " AND status=?"
            args.append(status)
        rows = cur.execute(sql, args).fetchall()
        cols = [d[0] for d in cur.description]

        out = []
        for r in rows:
            f = dict(zip(cols, r))
            ev_rows = cur.execute(
                "SELECT ev_type, ref_uri, snippet, ev_group, weight "
                "FROM fact_evidence WHERE fact_id=?",
                (f["fact_id"],),
            ).fetchall()
            f["evidence"] = [
                {"ev_type": e[0], "ref_uri": e[1], "snippet": e[2],
                 "ev_group": e[3], "weight": e[4]}
                for e in ev_rows
            ]
            out.append(f)
        return out

    def search_facts(self, query: str, domain: Optional[str] = None,
                     limit: int = 10) -> List[Dict[str, Any]]:
        """
        业务事实检索(业务问答的第一路)。

        刻意不用 FTS5: facts 是自然语言短句, 字段是 title/condition/action/
        rule_constraint 而不是代码标识符, unicode61 分词器对中文几乎无效
        (整句成一个 token)。这里用"关键词 OR 命中 + 覆盖率打分", 对短文本
        比 FTS 更可靠, 且不依赖分词器配置。
        """
        terms = [t for t in re.split(r"[\s,，。；;、/]+", query) if t]
        # 中文没有空格, 再按 2-gram 切一刀兜底
        for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", query):
            if len(chunk) > 3:
                terms.extend(chunk[i:i + 2] for i in range(len(chunk) - 1))
        terms = list({t.lower() for t in terms if len(t) >= 2})[:24]
        if not terms:
            return []

        d = domain or self.domain
        facts = self.get_facts(domain=d)
        scored = []
        for f in facts:
            blob = " ".join(str(f.get(k) or "") for k in
                            ("title", "condition", "action",
                             "rule_constraint", "impact_scope")).lower()
            hit = sum(1 for t in terms if t in blob)
            if not hit:
                continue
            # 覆盖率为主, 标题命中加权(标题更接近用户问法)
            cov = hit / len(terms)
            title_hit = sum(1 for t in terms if t in (f["title"] or "").lower())
            score = cov * 2.0 + title_hit * 0.5 + f.get("evidence_count", 0) * 0.1
            scored.append((score, f))

        scored.sort(key=lambda x: -x[0])
        return [f for _, f in scored[:limit]]

    def get_tables_for_stmt(self, qualified_stmt: str
                            ) -> List[Dict[str, Any]]:
        """按 namespace.stmtId 取 SQL 与表名(数据一致性断言的依据)"""
        cur = self._conn.cursor()
        row = cur.execute(
            "SELECT * FROM sql_index WHERE qualified_stmt=?", (qualified_stmt,)
        ).fetchone()
        if not row:
            return []
        cols = [d[0] for d in cur.description]
        d = dict(zip(cols, row))
        # tables / params 存的是**逗号分隔**(见 SqlStatement.to_row),
        # 不是 JSON —— 早期 schema 注释写成 JSON, 导致 find_sql_by_table
        # 用 LIKE '%"tbl"%' 匹配, 永远 0 命中, 且不报错。
        for k in ("tables", "params"):
            d[k] = _split_csv(d.get(k))
        return [d]

    def find_sql_by_table(self, table: str, domain: Optional[str] = None,
                          limit: int = 10) -> List[Dict[str, Any]]:
        """
        按表名反查哪些 SQL 碰过它。

        这是"改这张表会影响什么"的查询入口 —— 业务问答里最常被问的一类问题。

        匹配用**逗号边界**而非子串: `LIKE '%order%'` 会把 order_info、
        order_item 全捞进来, 给出"改 order 影响 order_info"这种误导性结论。
        """
        cur = self._conn.cursor()
        d = domain or self.domain
        # 前后补逗号做边界: ",a,b," LIKE "%,tbl,%"
        rows = cur.execute(
            "SELECT qualified_stmt, stmt_type, tables, file_path, "
            "       line_start, table_confidence "
            "FROM sql_index WHERE domain=? AND ',' || tables || ',' LIKE ? "
            "ORDER BY table_confidence ASC LIMIT ?",
            (d, f"%,{table},%", limit),
        ).fetchall()
        out = []
        for q, st, tbls, fp, ls, conf in rows:
            out.append({"qualified_stmt": q, "stmt_type": st,
                        "tables": _split_csv(tbls),
                        "file_path": fp, "line_start": ls,
                        "table_confidence": conf})
        return out

    def find_facts_by_symbols(self, symbols: List[str],
                              limit: int = 10) -> List[Dict[str, Any]]:
        """
        按标识符反查业务事实(确定性桥接, 不是模糊检索)。

        为什么必须有这个方法: 表名是 snake_case 英文(agent_conf),
        业务事实标题是中文(「客服异常时自动下线」), 两者**字面毫无交集**。
        直接拿表名去 search_facts 必然 0 命中, 影响面链路走到 SQL 就断了。

        唯一真正的连接点是 fact_evidence.snippet —— 抽取事实时记录的代码
        /SQL 原文片段。所以这里做 LIKE 精确匹配, 而不是靠语义相似度:
        语义检索会给出"看起来相关"的事实, 那比不给更危险。
        """
        if not symbols:
            return []
        cur = self._conn.cursor()
        d = self.domain
        seen = set()
        out: List[Dict[str, Any]] = []
        for sym in symbols:
            if not sym or len(sym) < 3:
                continue
            rows = cur.execute(
                """SELECT DISTINCT f.fact_id
                   FROM fact_evidence e JOIN facts f ON f.fact_id = e.fact_id
                   WHERE f.domain=? AND e.snippet LIKE ?""",
                (d, f"%{sym}%"),
            ).fetchall()
            for (fid,) in rows:
                if fid not in seen:
                    seen.add(fid)
                    out.append(fid)
            if len(out) >= limit:
                break

        if not out:
            return []
        qs = ",".join("?" * len(out))
        rows = cur.execute(
            f"""SELECT * FROM facts WHERE domain=? AND fact_id IN ({qs})""",
            [d] + out,
        ).fetchall()
        cols = [x[0] for x in cur.description]
        result = []
        for r in rows:
            f = dict(zip(cols, r))
            ev = cur.execute(
                "SELECT ev_type, ref_uri, snippet, ev_group, weight "
                "FROM fact_evidence WHERE fact_id=?", (f["fact_id"],)
            ).fetchall()
            f["evidence"] = [
                {"ev_type": e[0], "ref_uri": e[1], "snippet": e[2],
                 "ev_group": e[3], "weight": e[4]} for e in ev
            ]
            result.append(f)
            if len(result) >= limit:
                break
        return result

    def add_cases(self, cases: List[Any], task_id: Optional[str] = None
                  ) -> int:
        """
        批量写入用例正文, 并自动回填 case_fact_map。

        刻意把两件事绑在一个方法里: 只写 cases 不写映射, 覆盖报告会因为
        "每条事实 0 条用例" 而全盘报未覆盖 —— 这类静默失配最难查。

        cases 元素须是 GeneratedCase(或任何带同名属性的对象)。返回写入条数。
        determinacy 在写入时现算并存库: 生成期体检结论要能事后复查,
        否则"预期不可判定"这种指标只能靠重跑生成才能拿到。
        """
        from src.fact_case_generator import check_determinacy

        cur = self._conn.cursor()
        now = datetime.now().isoformat()
        n = 0
        pairs: List[Tuple[str, str]] = []

        for c in cases:
            case_id = getattr(c, "case_id", None)
            if not case_id:
                continue
            # 先清该 case_id 的旧映射再写: case_id 是"FACT-0001"这种序号,
            # 每次重新生成都会复用同一批编号。若不先清, 新一轮的 fact_id
            # 会与上一轮的并排存在(主键是 (case_id, fact_id), 覆盖不掉),
            # 一条用例就被算到两个业务规则头上。**症状是覆盖数虚高, 不报错。**
            cur.execute("DELETE FROM case_fact_map WHERE case_id=?", (case_id,))
            expected = getattr(c, "expected_result", "") or ""
            ok, issues = check_determinacy(expected)
            determinacy = "ok" if ok else ("empty" if not expected.strip()
                                           else "vague")
            cur.execute(
                """INSERT OR REPLACE INTO cases
                   (case_id, domain, task_id, fact_id, title, dimension,
                    dimension_name, priority, preconditions, steps,
                    expected_result, assertions, data_setup, code_refs,
                    table_refs, fact_title, determinacy, determinacy_issues,
                    human_approved, generated_by, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    case_id, self.domain, task_id,
                    getattr(c, "fact_id", None) or "",
                    getattr(c, "title", "") or "",
                    getattr(c, "dimension", "") or "",
                    getattr(c, "dimension_name", "") or "",
                    getattr(c, "priority", "") or "",
                    json.dumps(getattr(c, "preconditions", []) or [],
                               ensure_ascii=False),
                    json.dumps(getattr(c, "steps", []) or [],
                               ensure_ascii=False),
                    expected,
                    json.dumps(getattr(c, "assertions", []) or [],
                               ensure_ascii=False),
                    json.dumps(getattr(c, "data_setup", []) or [],
                               ensure_ascii=False),
                    ",".join(getattr(c, "code_refs", []) or []),
                    ",".join(getattr(c, "table_refs", []) or []),
                    getattr(c, "fact_title", "") or "",
                    determinacy,
                    ",".join(issues),
                    1 if getattr(c, "human_approved", False) else 0,
                    getattr(c, "generated_by", "fact_driven_generator")
                    or "fact_driven_generator",
                    now, now,
                ),
            )
            n += 1
            fid = getattr(c, "fact_id", None)
            if fid:
                pairs.append((case_id, fid))

        if pairs:
            self.link_cases_to_facts(pairs, task_id=task_id)
        return n

    def get_cases(self, domain: Optional[str] = None,
                  fact_id: Optional[str] = None,
                  dimension: Optional[str] = None,
                  task_id: Optional[str] = None,
                  limit: int = 100) -> List[Dict[str, Any]]:
        """取用例正文(JSON 列已解码回 list)"""
        cur = self._conn.cursor()
        sql = "SELECT * FROM cases WHERE 1=1"
        args: List[Any] = []
        if domain:
            sql += " AND domain=?"
            args.append(domain)
        if fact_id:
            sql += " AND fact_id=?"
            args.append(fact_id)
        if dimension:
            sql += " AND dimension=?"
            args.append(dimension)
        if task_id:
            sql += " AND task_id=?"
            args.append(task_id)
        sql += " ORDER BY priority, case_id LIMIT ?"
        args.append(limit)
        rows = cur.execute(sql, args).fetchall()
        cols = [d[0] for d in cur.description]
        out = []
        for r in rows:
            d = dict(zip(cols, r))
            for col in ("preconditions", "steps", "assertions", "data_setup"):
                d[col] = _load_json_list(d.get(col))
            d["code_refs"] = _split_csv(d.get("code_refs"))
            d["table_refs"] = _split_csv(d.get("table_refs"))
            d["determinacy_issues"] = _split_csv(d.get("determinacy_issues"))
            out.append(d)
        return out

    def search_cases(self, query: str, domain: Optional[str] = None,
                     limit: int = 10) -> List[Dict[str, Any]]:
        """
        按自然语言检索用例正文(业务问答「这条规则怎么测的」那一路)。

        同样不走 FTS5 —— 中文短句 + unicode61 分词器必然 0 命中,
        理由同 search_facts。
        """
        terms = [t for t in re.split(r"[\s,，。；;、/]+", query) if t]
        for chunk in re.findall(r"[一-鿿]{2,}", query):
            if len(chunk) > 3:
                terms.extend(chunk[i:i + 2] for i in range(len(chunk) - 1))
        terms = list({t.lower() for t in terms if len(t) >= 2})[:24]
        if not terms:
            return []

        d = domain or self.domain
        cases = self.get_cases(domain=d, limit=1000)
        scored = []
        for c in cases:
            blob = " ".join([
                c.get("title") or "", c.get("expected_result") or "",
                c.get("fact_title") or "",
                " ".join(c.get("assertions") or []),
                " ".join(c.get("code_refs") or []),
            ]).lower()
            hit = sum(1 for t in terms if t in blob)
            if not hit:
                continue
            # 标题命中权重更高: 用户问的通常就是用例标题里的说法
            title_hit = sum(1 for t in terms
                            if t in (c.get("title") or "").lower())
            scored.append((hit / len(terms) + 0.3 * title_hit / len(terms), c))
        scored.sort(key=lambda kv: -kv[0])
        return [c for _, c in scored[:limit]]

    def link_cases_to_facts(self, pairs: List[Tuple[str, str]],
                            task_id: Optional[str] = None,
                            coverage: str = "covered") -> int:
        """
        回填 case_id -> fact_id 映射。

        这是业务规则保真校验的唯一抓手: 有了它才能回答
        「需求里这 N 条规则, 哪几条一条用例都没有」。
        """
        cur = self._conn.cursor()
        now = datetime.now().isoformat()
        for case_id, fact_id in pairs:
            cur.execute(
                """INSERT OR REPLACE INTO case_fact_map
                   (case_id, fact_id, task_id, coverage, created_at)
                   VALUES (?,?,?,?,?)""",
                (case_id, fact_id, task_id, coverage, now),
            )
        return len(pairs)

    def fact_coverage_report(self, domain: Optional[str] = None
                            ) -> Dict[str, Any]:
        """
        业务规则覆盖报告: 每条事实有几条用例。

        重构前这个报告不存在 —— 30 条用例对需求三条硬规则覆盖 0/3,
        而且**没有任何机制能自动发现这件事**。
        """
        cur = self._conn.cursor()
        d = domain or self.domain
        rows = cur.execute(
            """SELECT f.fact_id, f.title, f.rule_constraint,
                      count(c.case_id) AS n
               FROM facts f
               LEFT JOIN case_fact_map c ON c.fact_id = f.fact_id
               WHERE f.domain=? AND f.status='accepted'
               GROUP BY f.fact_id
               ORDER BY n ASC, f.fact_id""",
            (d,),
        ).fetchall()
        total = len(rows)
        covered = [r for r in rows if r[3] > 0]
        uncovered = [r for r in rows if r[3] == 0]
        return {
            "domain": d,
            "fact_total": total,
            "fact_covered": len(covered),
            "fact_uncovered": len(uncovered),
            "coverage_rate": round(len(covered) / total * 100, 1) if total else 0.0,
            "uncovered_detail": [
                {"fact_id": r[0], "title": r[1], "constraint": r[2]}
                for r in uncovered
            ],
        }

    def set_meta(self, key: str, value: Any):
        cur = self._conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
            (key, json.dumps(value)),
        )

    def get_meta(self, key: str) -> Optional[Any]:
        cur = self._conn.cursor()
        row = cur.execute(
            "SELECT value FROM index_meta WHERE key=?", (key,)
        ).fetchone()
        if row:
            return json.loads(row[0])
        return None


# ============================================================
# 索引检索器
# ============================================================

class IndexSearcher:
    """索引检索器"""

    def __init__(self, db_path: Path, domain: Optional[str] = None):
        self.db_path = Path(db_path)
        self.domain = domain
        self._conn: Optional[sqlite3.Connection] = None

    def __enter__(self):
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        return self

    def __exit__(self, *args):
        if self._conn:
            self._conn.close()

    # ============================================================
    # 路 1: 符号检索
    # ============================================================
    def search_symbols(
        self,
        name_pattern: str,
        domain: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """按函数/类名检索"""
        cur = self._conn.cursor()
        d = domain or self.domain
        # 注意: SQL 中 AND 优先级高于 OR, OR 条件必须加括号
        rows = cur.execute(
            """SELECT * FROM symbols
               WHERE (name LIKE ? OR qualified_name LIKE ?)
               AND (? IS NULL OR domain = ?)
               LIMIT ?""",
            (f"%{name_pattern}%", f"%{name_pattern}%", d, d, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ============================================================
    # 路 2: 全文检索 (FTS5)
    # ============================================================
    @staticmethod
    def _sanitize_fts_query(query: str) -> str:
        """
        把用户输入的搜索串转换为安全的 FTS5 MATCH 表达式.

        v0.2 改进:
        - 之前用 AND (phrase), 召回过严 (multi-token 时 0 hits)
        - 用 OR, 让 LLM 召回 BM25 阶段召回足够候选

        v0.3 改进 (修召回失效):
        实测 customer-service 域 1152 chunks, 对以下查询全部返回 0 命中:
            "AgentStatus" / "agentStatus" / "agent_status" / "onlineStatus"
        但 symbols 表里明明有 AgentStatus, code_content 里也有 8 处.
        根因: FTS5 的 unicode61 分词器把 AgentStatus 整体切成
        "agentstatus" 一个 token, 既不拆驼峰也不保留原大小写,
        而查询侧做了 phrase match ("AgentStatus"), 两边对不上 -> 0 命中.
        对中文业务词同样失效(代码里没有中文词, 且中文未被切分).

        本次修法 (不动分词器, 避免全量重建索引):
        1. 驼峰/下划线拆词: AgentStatus -> agent status online
        2. 全部小写归一 (unicode61 默认大小写不敏感, 保持一致)
        3. 每个 token 同时发phrase 与前缀匹配, 兼顾精确与召回
        """
        if not query or not query.strip():
            return ""
        import re as _re

        # 驼峰拆词: agentStatusXxx / HTTPServer -> agent status server
        #下划线/中划线/点号视为分隔符
        spaced = _re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', query)
        spaced = _re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', spaced)
        spaced = _re.sub(r'[_\-.]+', ' ', spaced)

        tokens = _re.findall(r'[\w\u4e00-\u9fff]+', spaced)
        seen = set()
        safe_tokens = []
        for t in tokens:
            t = t.replace('"', '').lower()
            if not t or t in seen:
                continue
            seen.add(t)
            safe_tokens.append(t)

        if not safe_tokens:
            return ""

        # phrase 精确 + 前缀匹配, OR 连接 (任一命中即可)
        parts = []
        for t in safe_tokens:
            parts.append(f'"{t}"')
            # 长度 >= 4 的 token 加前缀匹配, 提升召回
            if len(t) >= 4:
                parts.append(f'"{t}"*')
        return " OR ".join(parts)

    def search_fulltext(
        self,
        query: str,
        domain: Optional[str] = None,
        limit: int = 30,
    ) -> List[Dict[str, Any]]:
        """FTS5 全文检索 (自动转义查询字符串)"""
        safe_query = self._sanitize_fts_query(query)
        if not safe_query:
            return []
        cur = self._conn.cursor()
        d = domain or self.domain
        # 加括号, 避免 AND/OR 优先级混乱
        rows = cur.execute(
            """SELECT s.*, snippet(code_fts, 5, '<b>', '</b>', '...', 32) as snippet
               FROM code_fts
               JOIN symbols s ON code_fts.chunk_id = s.chunk_id
               WHERE code_fts MATCH ?
               AND (? IS NULL OR code_fts.domain = ?)
               ORDER BY rank
               LIMIT ?""",
            (safe_query, d, d, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ============================================================
    # 路 3: 标签检索
    # ============================================================
    def search_by_tag(
        self,
        tag: str,
        domain: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """按业务标签检索 (模糊匹配)"""
        cur = self._conn.cursor()
        d = domain or self.domain
        # 加括号: business_tags 单独, domain 单独 AND
        rows = cur.execute(
            """SELECT * FROM symbols
               WHERE business_tags LIKE ?
               AND (? IS NULL OR domain = ?)
               LIMIT ?""",
            (f"%{tag}%", d, d, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ============================================================
    # 统一检索 (CLI / API 入口)
    # ============================================================
    def search(
        self,
        query: str,
        domain: Optional[str] = None,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        统一检索: 合并 符号 / 全文 / 标签 三条路径, 按相关度排序去重.

        历史 bug: CLI (bin/testcase.py kb search) 调用了不存在的 search(),
        而本类只提供 search_symbols/search_fulltext/search_by_tag,
        导致 CLI 必然抛 AttributeError. 这里补上统一入口, 三路结果合并.

        排序规则 (分数越高越靠前):
          - 符号名精确匹配 / 前缀匹配: 10 / 6
          - 标签命中: 4
          -全文命中: 3
        同一chunk_id 只保留最高分那条.
        """
        if not query or not query.strip():
            return []

        d = domain or self.domain
        merged: Dict[str, Dict[str, Any]] = {}

        def _add(rows: List[Dict[str, Any]], base_score: int) -> None:
            for r in rows:
                cid = r.get("chunk_id")
                if not cid:
                    continue
                prev = merged.get(cid)
                if prev is None or base_score > prev["_score"]:
                    r = dict(r)
                    r["_score"] = base_score
                    merged[cid] = r

        # 路 1: 符号名 (再细分精确/前缀/包含)
        try:
            sym_rows = self.search_symbols(query, domain=d, limit=limit * 5)
        except sqlite3.Error:
            sym_rows = []
        lowered = query.lower()
        for r in sym_rows:
            name = (r.get("name") or "").lower()
            qname = (r.get("qualified_name") or "").lower()
            if name == lowered or qname == lowered:
                _add([r], 10)
            elif name.startswith(lowered) or qname.startswith(lowered):
                _add([r], 6)

        # 路 2: 业务标签
        try:
            tag_rows = self.search_by_tag(query, domain=d, limit=limit * 3)
        except sqlite3.Error:
            tag_rows = []
        _add(tag_rows, 4)

        # 路 3: 全文 (FTS5)
        try:
            ft_rows = self.search_fulltext(query, domain=d, limit=limit * 5)
        except sqlite3.Error:
            ft_rows = []
        _add(ft_rows, 3)

        results = sorted(
            merged.values(),
            key=lambda x: (-x["_score"], x.get("qualified_name") or ""),
        )[:limit]

        # 清理内部字段, 补 CLI 需要的展示字段
        out = []
        for r in results:
            r.pop("_score", None)
            r.setdefault("qualified_name", r.get("name", "?"))
            r.setdefault("file_path", "?")
            r.setdefault("line_start", "?")
            r.setdefault("summary", "")
            out.append(r)
        return out

    # ============================================================
    # 调用链扩展
    # ============================================================
    def get_callers(
        self,
        chunk_id: str,
        depth: int = 2,
    ) -> List[Dict[str, Any]]:
        """递归找所有 caller (谁调用了它)"""
        cur = self._conn.cursor()
        rows = cur.execute(
            """WITH RECURSIVE callers(chunk_id, depth, path) AS (
                SELECT to_chunk_id, 1, from_chunk_id
                FROM relations
                WHERE to_chunk_id = ?

                UNION ALL

                SELECT r.to_chunk_id, c.depth + 1,
                       c.path || '->' || r.from_chunk_id
                FROM relations r
                JOIN callers c ON r.to_chunk_id = c.chunk_id
                WHERE c.depth < ?
            )
            SELECT DISTINCT s.*, MIN(c.depth) as min_depth
            FROM callers c
            JOIN symbols s ON c.chunk_id = s.chunk_id
            GROUP BY s.chunk_id
            ORDER BY min_depth""",
            (chunk_id, depth),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_callees(
        self,
        chunk_id: str,
        depth: int = 1,
    ) -> List[Dict[str, Any]]:
        """递归找所有 callee (它调用了谁)"""
        cur = self._conn.cursor()
        rows = cur.execute(
            """WITH RECURSIVE callees(chunk_id, depth, path) AS (
                SELECT to_chunk_id, 1, from_chunk_id
                FROM relations
                WHERE from_chunk_id = ?

                UNION ALL

                SELECT r.to_chunk_id, c.depth + 1,
                       c.path || '->' || r.to_chunk_id
                FROM relations r
                JOIN callees c ON r.from_chunk_id = c.chunk_id
                WHERE c.depth < ?
            )
            SELECT DISTINCT s.*, MIN(c.depth) as min_depth
            FROM callees c
            JOIN symbols s ON c.chunk_id = s.chunk_id
            GROUP BY s.chunk_id
            ORDER BY min_depth""",
            (chunk_id, depth),
        ).fetchall()
        return [dict(r) for r in rows]

    # ============================================================
    # 综合查询
    # ============================================================
    def get_stats(self, domain: Optional[str] = None) -> Dict[str, Any]:
        """获取索引统计"""
        cur = self._conn.cursor()
        d = domain or self.domain
        stats = {
            "total_chunks": cur.execute(
                "SELECT COUNT(*) FROM symbols WHERE ? IS NULL OR domain=?",
                (d, d),
            ).fetchone()[0],
            "by_type": {},
            "total_relations": cur.execute(
                "SELECT COUNT(*) FROM relations"
            ).fetchone()[0],
        }
        # 按类型分布
        rows = cur.execute(
            """SELECT chunk_type, COUNT(*) as cnt
               FROM symbols
               WHERE ? IS NULL OR domain=?
               GROUP BY chunk_type""",
            (d, d),
        ).fetchall()
        for r in rows:
            stats["by_type"][r["chunk_type"] or "unknown"] = r["cnt"]
        return stats


# ============================================================
# 命令行入口
# ============================================================

if __name__ == "__main__":
    from src.logger import setup_logging
    setup_logging()

    # 简单测试: 创建空索引, 验证 schema
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = Path(tmp.name)

    with IndexBuilder(db_path, "test-domain") as builder:
        # 添加测试 chunk
        chunk = CodeChunk(
            chunk_id="test-001",
            file_path="/test/TestService.java",
            file_type="java",
            language="java",
            chunk_type="method",
            name="validateAmount",
            qualified_name="TestService.validateAmount",
            line_start=10,
            line_end=30,
            code_content="public boolean validateAmount(BigDecimal amount) { ... }",
            http_route=None,
        )
        builder.add_chunk(chunk, business_tags=["订单", "金额"], summary="校验订单金额")
        builder.add_relation("test-001", "test-002", "call")
        builder.commit()
        print(f"✅ 测试 chunk 已添加")

    # 测试检索
    with IndexSearcher(db_path) as searcher:
        print(f"\n=== 统计 ===")
        print(json.dumps(searcher.get_stats("test-domain"), indent=2, ensure_ascii=False))

        print(f"\n=== 符号检索: 'validateAmount' ===")
        results = searcher.search_symbols("validateAmount", "test-domain")
        for r in results:
            print(f"  {r['name']} ({r['qualified_name']}) @ {r['file_path']}:{r['line_start']}")

        print(f"\n=== 标签检索: '订单' ===")
        results = searcher.search_by_tag("订单", "test-domain")
        for r in results:
            print(f"  {r['name']} tags={r['business_tags']}")

    print(f"\n✅ 测试 DB: {db_path}")
    # 清理
    db_path.unlink()
