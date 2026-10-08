# -*- coding: utf-8 -*-
"""
Fact 层回填工具 —— 不重建符号索引, 只重跑业务事实与用例

## 为什么单独做这个工具

符号索引(解析 9000+ 文件建 FTS5/调用图/数据链路)已经建好, 一次要几十分钟。
但改了 Fact 抽取逻辑、去重规则、或用例生成模板之后, 想验证效果时
**不需要重建符号索引** —— Fact 层要的 chunks 与 links 都能从已有索引库复原:

  - `chunks.chunk_data` 存着完整 CodeChunk 的 JSON
  - `sql_index` 存着每条 SQL 的字段, 足够拼回 SqlStatement
  - 再跑一次 DataLinkBuilder 把 Service -> Mapper -> SQL 重新缝起来

这条路径从"几十分钟"降到"几十秒", 才可能做到改一次规则立刻验证。
早期只有 `.tmp/backfill_sql_index.py` 走通了这个思路(当时是为补 sql_index),
现在把 Fact 层也纳入, 形成统一的回填入口。

## 用法
    python -m tools.backfill_facts                    # 全部域
    python -m tools.backfill_facts customer-service   # 指定域
    python -m tools.backfill_facts --rebuild-links    # 连数据链路一起重缝
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.code_parser import CodeChunk
from src.data_link import DataLink, DataLinkBuilder
from src.call_graph import RES_EXACT
from src.fact_case_generator import FactCaseGenerator
from src.fact_extractor import FactExtractor, filter_accepted
from src.index_builder import FACT_MIN_EVIDENCE, IndexBuilder
from src.logger import get_logger, setup_logging
from src.req_dedup import dedupe_requirements
from src.xml_parser import SqlStatement

logger = get_logger(__name__)


# ============================================================
# 从索引库复原内存对象
# ============================================================

def load_chunks(db_path: Path) -> List[CodeChunk]:
    """
    从 chunks.chunk_data 复原 CodeChunk 列表。

    注意 chunk_data 里**没有** business_tags / summary —— 那两个字段在
    symbols 表上, 不在 chunk_data 里。Fact 抽取用不到它们(证据靠
    code_content / annotations / imports), 所以这里不补。
    若将来某条规则改成依赖 summary, 必须同步补上, 否则会静默取到空值。

    **局限: chunk_data 是旧版解析器写的, called_methods 可能不含接收者。**
    实测 customer-service 库里的 called_methods 1249 条全是裸方法名
    (`updateComboCount`), 而当前解析器产出的是 `baseMapper.updateComboCount`
    —— 库是 `_walk_calls` 修好之前建的。接收者缺失会让 DataLink 的 exact
    分支(唯一可信的那条)永远走不到, 链路全降级成 guess。
    所以下面用 has_receiver_call() 显式检测, 不让回填工具静默降级。
    """
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT chunk_data FROM chunks").fetchall()
    finally:
        conn.close()

    out: List[CodeChunk] = []
    for (blob,) in rows:
        try:
            d = json.loads(blob)
        except (json.JSONDecodeError, TypeError):
            continue
        try:
            out.append(CodeChunk(
                chunk_id=d["chunk_id"],
                file_path=d.get("file_path", ""),
                file_type=d.get("file_type", ""),
                language=d.get("language", ""),
                chunk_type=d.get("chunk_type", ""),
                name=d.get("name", ""),
                qualified_name=d.get("qualified_name", ""),
                line_start=int(d.get("line_start") or 0),
                line_end=int(d.get("line_end") or 0),
                code_content=d.get("code_content", ""),
                docstring=d.get("docstring", ""),
                annotations=list(d.get("annotations") or []),
                imports=list(d.get("imports") or []),
                called_methods=list(d.get("called_methods") or []),
                http_route=d.get("http_route", ""),
            ))
        except (KeyError, TypeError, ValueError) as e:
            logger.debug("chunk 复原失败, 跳过: %s", e)
    return out


def has_receiver_call(chunks: List[CodeChunk]) -> bool:
    """
    检测索引里的 called_methods 是否带接收者。

    带接收者(`bossOrderMapper.updateX`)是 DataLink 走 exact 分支的前提。
    全部是裸方法名 -> 说明索引是旧解析器建的, 必须重建符号索引,
    否则回填出来的 Fact 全部缺 SQL/表证据, 而且**不报任何错**。
    """
    total = dotted = 0
    for c in chunks:
        for m in (c.called_methods or []):
            total += 1
            if "." in m:
                dotted += 1
    return dotted > 0


def index_is_stale(chunks: List[CodeChunk]) -> Optional[str]:
    """索引陈旧检测。返回原因, 正常则返回 None"""
    if not has_receiver_call(chunks):
        return ("索引里的 called_methods 全是裸方法名, 不含接收者 —— "
                "这是旧版解析器的产物, exact 数据链路必然全降级为 guess。"
                "需先跑 testcase kb build <domain> 重建符号索引。")
    return None


def load_statements(db_path: Path) -> List[SqlStatement]:
    """
    从 sql_index 复原 SqlStatement。

    tables / params 是 CSV 列(见 SqlStatement.to_row), 解析时必须用
    _split_csv 而不是 json.loads —— 早期就是这里格式对不上, 表现为
    "表名一个都查不到"且不报错。
    """
    from src.index_builder import _split_csv

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM sql_index").fetchall()
    finally:
        conn.close()

    out: List[SqlStatement] = []
    for r in rows:
        out.append(SqlStatement(
            stmt_id=r["stmt_id"] or "",
            stmt_type=r["stmt_type"] or "",
            namespace=r["namespace"] or "",
            sql_text=r["sql_text"] or "",
            tables=_split_csv(r["tables"]),
            is_dynamic=bool(r["is_dynamic"]),
            has_external_include=bool(r["has_external_include"]),
            params=_split_csv(r["params"]),
            file_path=r["file_path"] or "",
            line_start=int(r["line_start"] or 0),
        ))
    return out


def rebuild_links(chunks: List[CodeChunk], db_path: Path) -> List[DataLink]:
    """
    重跑 DataLinkBuilder 得到 Service -> Mapper -> SQL -> 表 链路。

    拿不到 chunks 就直接返回空: DataLinkBuilder 依赖 chunk 列表做
    Mapper 接口确认, 传空会连错链 —— 而连错链比不连危害大得多
    (用例会照着错出处去断言别的模块的字段)。

    **by_mapper 的键必须小写**: _stmt_in_mapper 用 `by_mapper.get(mapper_cls.lower())`
    取候选。历史上 `load_mappers()` 建的索引就是小写键, 这里若按原始大小写
    注入, A 分支(接收者锁定 Mapper, 最可信的那条)永远查不到 —— 表现为
    exact 链路恒为 0、全量降级成 guess, **而且不报任何错**。
    这是本函数最容易踩的一处, 下面有断言兜底。
    """
    if not chunks:
        logger.warning("  无 chunks, 跳过数据链路缝合(宁可不成链, 不可连错链)")
        return []
    try:
        builder = DataLinkBuilder(chunks, None)
        stmts = load_statements(db_path)
        for st in stmts:
            builder.by_stmt_id[st.stmt_id].append(st)
            builder.by_mapper[_mapper_simple_name(st.namespace).lower()].append(st)
        links = builder.build()
        n_exact = sum(1 for l in links if l.resolution == RES_EXACT)
        logger.info(f"  数据链路重缝: {len(links)} 条 (exact={n_exact})")
        if stmts and n_exact == 0:
            # 显式告警: 0 条 exact 通常意味着 Mapper 索引没建起来,
            # 而不是"这个域真的没有任何 Mapper 调用"。后者极其罕见。
            logger.warning(
                f"  ⚠ exact=0: 已加载 {len(stmts)} 条 SQL / "
                f"{len(builder.by_mapper)} 个 Mapper, 但无一条走 exact 分支。"
                "多半是 Mapper 索引键的规范化与查询侧不一致。"
            )
        return links
    except Exception as e:
        logger.warning(f"  数据链路缝合失败: {e}")
        return []


def _mapper_simple_name(namespace: str) -> str:
    """ai.yiye.dao.AgentConfMapper -> AgentConfMapper"""
    if not namespace:
        return ""
    return namespace.rsplit(".", 1)[-1]


# ============================================================
# 主流程
# ============================================================

def backfill_domain(domain: str, db_path: Path, req_files: List[Path],
                    rebuild: bool = True, allow_stale: bool = False
                    ) -> Dict[str, Any]:
    """
    对单个域重跑 Fact 层: 清旧事实 -> 重抽 -> 生成用例 -> 落库。

    **先清 facts / fact_evidence / cases / case_fact_map 再写**:
    只 INSERT 不清会累积历史事实, 而 fact_id 稳定意味着重跑会覆盖同一条 ——
    于是"上一版规则抽出的、这一版应该被淘汰的"那条会永远留在库里,
    覆盖报告的分母只增不减。
    """
    stats: Dict[str, Any] = {"domain": domain, "requirements": len(req_files)}
    if not db_path.exists():
        stats["error"] = f"索引库不存在: {db_path}"
        return stats
    if not req_files:
        stats["error"] = "无可用需求文档"
        return stats

    chunks = load_chunks(db_path)
    stats["chunks"] = len(chunks)
    if not chunks:
        stats["error"] = "chunks 表为空 —— 该域索引未构建"
        return stats

    # 陈旧索引必须在这里拦下, 不能带着 guess 链路继续跑:
    # Fact 的 SQL/表证据靠 exact 链路, 降级后门禁会拒收或给出错误出处,
    # 而两种情况都表现为"跑完了, 数字不好看", 不会报错。
    stale = index_is_stale(chunks)
    if stale and not allow_stale:
        stats["error"] = ("索引陈旧, 需先重建符号索引 —— " + stale)
        return stats
    if stale:
        logger.warning("  ⚠ 强行在陈旧索引上回填: %s", stale)

    links = rebuild_links(chunks, db_path) if rebuild else []
    stats["links"] = len(links)

    with IndexBuilder(db_path, domain) as b:
        # 只清 Fact 层, 符号索引与调用图必须留着(这正是本工具存在的意义)。
        # 统一走 IndexBuilder.clear_fact_layer 而不是内联复制清库逻辑 ——
        # 之前这里内联写了一遍而 clear_domain 只清符号索引, 两处各清一半,
        # 症状是重跑后同一 case_id 同时挂着新旧 fact_id。
        b.clear_fact_layer()

        ex = FactExtractor(chunks=chunks, links=links)
        accepted = []
        rejected_detail = []
        for rf in req_files:
            try:
                cands = ex.extract(rf)
            except Exception as e:
                logger.warning(f"  需求解析失败 {rf.name}: {e}")
                continue
            passed, rejected = filter_accepted(cands,
                                               min_evidence=FACT_MIN_EVIDENCE)
            for f in rejected:
                rejected_detail.append({
                    "title": f.title,
                    "evidence_count": f.evidence_count,
                    "source": rf.name,
                })
            for f in passed:
                ok, why = b.add_fact(f, min_evidence=FACT_MIN_EVIDENCE)
                if ok:
                    accepted.append(f)
                else:
                    rejected_detail.append({"title": f.title, "reason": why,
                                            "source": rf.name})

        stats["accepted"] = len(accepted)
        stats["rejected"] = len(rejected_detail)

        if accepted:
            gen = FactCaseGenerator(id_prefix=f"{domain}-FACT")
            cases = gen.generate(accepted)
            stats["cases"] = b.add_cases(cases, task_id=f"kb-build:{domain}")
            cov = FactCaseGenerator.coverage_report(accepted, cases)
            stats["case_coverage"] = {
                "fact_total": cov["fact_total"],
                "fact_covered": cov["fact_covered"],
                "coverage_rate": cov["coverage_rate"],
                "case_total": cov["case_total"],
                "vague_expected": cov["vague_expected"],
                "dimension_dist": cov["dimension_dist"],
            }
        b.commit()

    stats["rejected_detail"] = rejected_detail[:20]
    return stats


def main() -> int:
    setup_logging()
    ap = argparse.ArgumentParser(description="Fact 层回填(不重建符号索引)")
    ap.add_argument("domains", nargs="*", help="域名, 留空表示全部")
    ap.add_argument("--requirements-dir", default=str(ROOT / "requirements"))
    ap.add_argument("--no-rebuild-links", action="store_true",
                    help="不重缝数据链路(会显著降低 SQL/表证据强度)")
    ap.add_argument("--allow-stale", action="store_true",
                    help="允许在陈旧索引(缺接收者)上回填 —— 会降级为 guess 链路, "
                         "仅用于对比排查, 不要用于正式构建")
    args = ap.parse_args()

    req_files, dup_map = dedupe_requirements(Path(args.requirements_dir))
    if dup_map:
        logger.info("需求去重: %d 组 -> 保留 %d 份代表",
                    len(dup_map), len(req_files))
    if not req_files:
        print("❌ 没有可用需求文档")
        return 1

    domains_dir = ROOT / "domains"
    all_dbs = sorted(p.parent.name for p in domains_dir.glob("*/_index.db"))
    targets = args.domains or [d for d in all_dbs if d != "requirements"]

    print("=" * 64)
    print(f"Fact 层回填: {len(targets)} 个域 / 需求 {len(req_files)} 份")
    print("=" * 64)

    results = []
    for d in targets:
        logger.info(f"=== 域 [{d}] ===")
        st = backfill_domain(d, domains_dir / d / "_index.db", req_files,
                             rebuild=not args.no_rebuild_links,
                             allow_stale=args.allow_stale)
        results.append(st)
        if st.get("error"):
            print(f"  {d:20} ❌ {st['error']}")
            continue
        cc = st.get("case_coverage") or {}
        print(f"  {d:20} 事实={st.get('accepted', 0):3} "
              f"证据不足={st.get('rejected', 0):3} "
              f"用例={st.get('cases', 0):5} "
              f"规则覆盖={cc.get('fact_covered', 0)}/{cc.get('fact_total', 0)} "
              f"({cc.get('coverage_rate', 0)}%) "
              f"维度={len(cc.get('dimension_dist') or {})}")

    print("=" * 64)
    total_f = sum(r.get("accepted", 0) for r in results)
    total_c = sum(r.get("cases", 0) for r in results)
    failed = [r["domain"] for r in results if r.get("error")]
    print(f"合计: 事实 {total_f} 条 / 用例 {total_c} 条 / 失败域 {len(failed)}")
    if failed:
        print("失败: " + ", ".join(failed))
    # 导出每域的维度分布, 用来肉眼核对"七维是否都生成到了"
    for r in results:
        cc = r.get("case_coverage") or {}
        if cc.get("dimension_dist"):
            print(f"  {r['domain']:20} {cc['dimension_dist']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
