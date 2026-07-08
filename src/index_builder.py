"""
索引构建器 - SQLite + FTS5

建立 3 类索引:
1. 符号索引 (精确查找函数/类)
2. 倒排索引 (关键词全文检索)
3. 调用关系图 (调用链扩展)
"""

import sqlite3
import json
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime

from src.logger import get_logger
from src.code_parser import CodeChunk

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
CREATE TABLE IF NOT EXISTS relations (
    from_chunk_id TEXT NOT NULL,
    to_chunk_id TEXT NOT NULL,
    relation_type TEXT NOT NULL,    -- 'call' / 'inject' / 'extend' / 'feign'
    PRIMARY KEY (from_chunk_id, to_chunk_id, relation_type)
);

CREATE INDEX IF NOT EXISTS idx_relations_from ON relations(from_chunk_id);
CREATE INDEX IF NOT EXISTS idx_relations_to ON relations(to_chunk_id);

-- 全文检索 (FTS5 虚拟表)
CREATE VIRTUAL TABLE IF NOT EXISTS code_fts USING fts5(
    chunk_id UNINDEXED,
    domain UNINDEXED,
    file_path,
    name,
    qualified_name,
    code_content,
    summary,
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
        self._conn.commit()
        logger.debug(f"索引已打开: {self.db_path}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._conn:
            self._conn.close()

    def clear_domain(self):
        """清空某个域的所有索引数据"""
        cur = self._conn.cursor()
        cur.execute("DELETE FROM symbols WHERE domain=?", (self.domain,))
        cur.execute(
            "DELETE FROM code_fts WHERE chunk_id IN "
            "(SELECT chunk_id FROM chunks WHERE domain=?)",
            (self.domain,),
        )
        cur.execute("DELETE FROM chunks WHERE domain=?", (self.domain,))
        # relations 涉及跨域, 不在这里清, 单独处理
        self._conn.commit()
        logger.info(f"已清空域 {self.domain} 的索引")

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
        cur.execute(
            """INSERT INTO code_fts
               (chunk_id, domain, file_path, name, qualified_name,
                code_content, summary)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (chunk.chunk_id, self.domain, chunk.file_path,
             chunk.name, chunk.qualified_name,
             chunk.code_content, summary),
        )

    def add_relation(self, from_chunk_id: str, to_chunk_id: str,
                     relation_type: str = "call"):
        """添加调用关系"""
        cur = self._conn.cursor()
        cur.execute(
            """INSERT OR IGNORE INTO relations
               (from_chunk_id, to_chunk_id, relation_type)
               VALUES (?, ?, ?)""",
            (from_chunk_id, to_chunk_id, relation_type),
        )

    def commit(self):
        if self._conn:
            self._conn.commit()

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
        - 去除 FTS5 运算符 (- + * " : ( ) NEAR AND OR NOT)
        - 多关键词用 OR 连接 (任一命中即可, 召回更宽)
        - 每个 token 用双引号包起来 (phrase match)
        - 空字符串返回空 (caller 会拿到空结果, 不会爆)

        v0.2 改进:
        - 之前用 AND (phrase), 召回过严 (multi-token 时 0 hits)
        - 现在用 OR, 让 LLM 召回 BM25 阶段召回足够候选
        """
        if not query or not query.strip():
            return ""
        import re as _re
        tokens = _re.findall(r'[\w\u4e00-\u9fff]+', query)
        if not tokens:
            return ""
        safe_tokens = [t.replace('"', '') for t in tokens if t.replace('"', '')]
        return " OR ".join(f'"{t}"' for t in safe_tokens)

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
