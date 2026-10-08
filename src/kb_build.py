"""
知识库构建 - 把代码仓库转换为可检索的索引

主要功能:
1. 扫描 kb-config.yaml 中定义的代码源
2. Tree-sitter 解析代码为 chunks
3. LLM 自动打业务标签 + 摘要
4. 入库 SQLite + FTS5
5. 构建调用关系图
"""

import json
import re
import yaml
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from src.logger import setup_logging, get_logger, get_audit_logger
from src.code_parser import CodeParser, CodeChunk
from src.index_builder import IndexBuilder, FACT_MIN_EVIDENCE
from src.call_graph import CallGraphBuilder, summarize_by_resolution
from src.xml_parser import MapperXmlParser
from src.data_link import DataLinkBuilder
from src.call_graph import RES_EXACT
from src.fact_extractor import FactExtractor, filter_accepted
from src.fact_case_generator import FactCaseGenerator
from src.req_dedup import dedupe_requirements, dedupe_report
from src.llm_client import get_default_client
from src.path_utils import glob_match as _glob_match  # v0.5.9: 跨平台 glob 共享

logger = get_logger(__name__)


# ============================================================
# 配置加载
# ============================================================

def load_kb_config(config_path: Path) -> Dict[str, Any]:
    """加载 kb-config.yaml"""
    with open(config_path, encoding='utf-8') as f:
        return yaml.safe_load(f)


def load_main_config(config_path: Path) -> Dict[str, Any]:
    """加载 config.yaml"""
    with open(config_path, encoding='utf-8') as f:
        return yaml.safe_load(f)


# ============================================================
# 文件枚举
# ============================================================

def scan_files(source: Dict[str, Any], excludes: Dict[str, Any]) -> List[Path]:
    """扫描一个代码源的所有文件"""
    path = Path(source["path"])
    if not path.exists():
        logger.warning(f"路径不存在: {path}")
        return []

    patterns = excludes.get("patterns", [])
    max_size = excludes.get("max_file_size", 1048576)
    binary_exts = set(excludes.get("binary_extensions", []))

    files = []
    for f in path.rglob("*"):
        if not f.is_file():
            continue

        # 排除规则
        rel = str(f.relative_to(path))
        if any(_glob_match(rel, p) for p in patterns):
            continue

        # 大小限制
        try:
            if f.stat().st_size > max_size:
                continue
        except OSError:
            continue

        # 二进制排除
        if f.suffix.lower() in binary_exts:
            continue

        # 只保留代码文件
        if f.suffix.lower() not in {".java", ".vue", ".js", ".ts", ".xml", ".yml", ".yaml", ".properties"}:
            continue

        files.append(f)

    return files


# v0.5.9: 原生简陋的 _glob_match 已废弃, 改用 src/path_utils.py 跨平台版本
# (支持 Windows 路径 \\, ** 通配符, drive letter 前缀剥离).
# 本地 stub 仅用于向后兼容异常路径.
def _glob_match(path: str, pattern: str) -> bool:  # pragma: no cover
    from src.path_utils import glob_match
    return glob_match(path, pattern)


# ============================================================
# Chunk 增强 (LLM 打标签 + 摘要)
# ============================================================

def enrich_chunk(chunk: CodeChunk, context: str = "") -> Dict[str, str]:
    """
    用 LLM 给 chunk 打业务标签 + 生成摘要

    Returns:
        {"tags": "...", "summary": "..."}
    """
    client = get_default_client()

    code_preview = chunk.code_content[:1500]
    if len(chunk.code_content) > 1500:
        code_preview += "\n... (truncated)"

    prompt = f"""请分析以下代码片段, 输出严格的 JSON:

```json
{{
  "tags": ["业务标签1", "业务标签2", "业务标签3"],
  "summary": "一句话描述这个代码的功能"
}}
```

要求:
1. tags: 1-3 个中文业务标签 (如: "订单", "支付", "落地页创建")
2. summary: 一句话 (30 字以内) 说明这个函数/类的功能
3. 只输出 JSON, 不要其他内容

代码信息:
- 文件: {Path(chunk.file_path).name}
- 类型: {chunk.chunk_type}
- 名称: {chunk.name}
- HTTP 路由: {chunk.http_route or '无'}

代码内容:
```
{code_preview}
```
"""

    try:
        response = client.call(
            prompt=prompt,
            system="你是代码分析专家, 输出严格 JSON",
            max_tokens=200,
            temperature=0.3,
        )
        # P0-3 fix: 用 raw_decode 替代贪婪 re.search
        from src.json_utils import extract_json
        data = extract_json(response)
        if not isinstance(data, dict):
            return None
        return {
            "tags": data.get("tags", []),
            "summary": data.get("summary", ""),
            }
    except Exception as e:
        logger.warning(f"LLM 增强失败 ({chunk.chunk_id}): {e}")

    return {"tags": [], "summary": ""}


# ============================================================
# 域构建
# ============================================================

def build_domain(
    domain_name: str,
    domain_config: Dict[str, Any],
    excludes: Dict[str, Any],
    kb_root: Path,
    max_files: Optional[int] = None,
    enrich_with_llm: bool = True,
    max_workers: int = 5,
    fact_sources: Optional[List[Path]] = None,
) -> Dict[str, Any]:
    """
    构建单个业务域的索引

    Args:
        domain_name: 业务域名
        domain_config: 域配置 (含 code_sources)
        excludes: 排除规则
        kb_root: 知识库根目录
        max_files: 限制文件数 (测试用)
        enrich_with_llm: 是否用 LLM 增强
        max_workers: 并发数
        fact_sources: 需求文档路径列表。为空则跳过 Fact 层抽取
            —— 代码域没有需求文档时这是正确的, 不是缺陷。

    Returns:
        构建统计
    """
    logger.info(f"开始构建域 [{domain_name}]")

    parser = CodeParser()
    db_path = kb_root / "domains" / domain_name / "_index.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    stats = {
        "domain": domain_name,
        "files_scanned": 0,
        "chunks_created": 0,
        "by_type": {},
        "llm_enriched": 0,
        "build_duration_sec": 0,
    }

    start = datetime.now()

    with IndexBuilder(db_path, domain_name) as builder:
        builder.clear_domain()

        # 1. 扫描所有源的文件 (含 Mapper XML —— 数据层证据来源)
        all_files: List[Path] = []
        for source in domain_config.get("code_sources", []):
            files = scan_files(source, excludes)
            logger.info(f"  源 [{source['id']}]: {len(files)} 文件")
            all_files.extend(files)

        # 去重
        all_files = list(set(all_files))
        stats["files_scanned"] = len(all_files)

        if max_files:
            all_files = all_files[:max_files]
            logger.info(f"  限制为前 {max_files} 个文件 (测试模式)")

        # 1b. Mapper XML 单独解析 (不进代码树, 单独入 sql_index)
        mapper_files = [f for f in all_files
                        if f.suffix.lower() == ".xml" and "mapper" in f.name.lower()]
        code_files = [f for f in all_files if f not in set(mapper_files)]
        mapper_parser = MapperXmlParser()
        sql_added = 0
        for mf_file in mapper_files:
            mf = mapper_parser.parse_file(mf_file)
            if not mf:
                continue
            for st in mf.statements:
                if builder.add_sql_statement(st):
                    sql_added += 1
        stats["sql_statements"] = sql_added
        logger.info(f"  Mapper XML: {len(mapper_files)} 文件 / {sql_added} 条 SQL")

        # 2. 解析所有文件 (并行, 只跑代码文件)
        all_chunks: List[CodeChunk] = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(parser.parse, f): f
                for f in code_files
            }
            for future in as_completed(futures):
                file = futures[future]
                try:
                    chunks = future.result()
                    all_chunks.extend(chunks)
                except Exception as e:
                    logger.warning(f"解析失败 {file}: {e}")

        logger.info(f"  解析得到 {len(all_chunks)} chunks")
        stats["chunks_created"] = len(all_chunks)

        # 3. 入库 (不带 LLM 增强)
        for chunk in all_chunks:
            builder.add_chunk(chunk)
            t = chunk.chunk_type
            stats["by_type"][t] = stats["by_type"].get(t, 0) + 1

        # 4. 构建调用关系 (跨文件, 三级降级消歧)
        _build_relations(all_chunks, builder)

        # 4b. Service -> Mapper -> SQL -> 表 全链缝合
        # mapper_root 用 mapper_files 所在目录, DataLinkBuilder 自己扫 XML。
        # 必须在这里做: Fact 抽取要靠链路拿到 SQL/表证据, 否则门禁拒收。
        links: List[Any] = []
        if mapper_files:
            roots = {f.parent for f in mapper_files}
            for root in roots:
                try:
                    links.extend(DataLinkBuilder(all_chunks, root).build())
                except Exception as e:
                    logger.warning(f"  数据链路缝合失败 {root}: {e}")
        stats["data_links"] = len(links)
        if links:
            n_exact = sum(1 for l in links if l.resolution == RES_EXACT)
            logger.info(f"  数据链路: {len(links)} 条 (exact={n_exact})")

        # 5. LLM 增强 (可选)
        if enrich_with_llm and all_chunks:
            logger.info(f"  开始 LLM 增强 ({len(all_chunks)} chunks, 并发 {max_workers})")
            enriched = _enrich_with_llm(all_chunks, builder, max_workers)
            stats["llm_enriched"] = enriched

        builder.set_meta("built_at", datetime.now().isoformat())
        builder.set_meta("domain", domain_name)
        builder.set_meta("stats", stats)
        builder.commit()

        # 6. Fact 层: 从需求 + 代码 + 数据三路抽取业务事实
        # 放在最后: 需要 code_fts / relations / sql_index 都已就位,
        # 否则事实只能拿到需求侧单条证据, 会被门禁全部拒收。
        if fact_sources:
            fstats = _build_facts(fact_sources, db_path, domain_name,
                                  all_chunks, links)
            stats["facts"] = fstats

    elapsed = (datetime.now() - start).total_seconds()
    stats["build_duration_sec"] = round(elapsed, 2)

    logger.info(
        f"✅ 域 [{domain_name}] 构建完成: "
        f"{stats['files_scanned']} 文件 / {stats['chunks_created']} chunks / "
        f"{elapsed:.1f}s"
    )
    return stats


def _req_files(requirements_dir: Optional[Path]) -> List[Path]:
    """
    列出需求文档(已按**内容**去重)。

    历史版本只按"是否含 3 个连续反斜杠"过滤转义损坏副本, 结果 47 → 36 份,
    但实测按内容分组后这 36 份里只有 **8 组**, 其中一组 26 个文件承载的是
    同一份「客服获客助手异常自动下线」需求 —— 差别只在 req_id 与 title。
    同一批规则被抽 26 遍, 覆盖报告的分母被灌水, 「覆盖 100%」是自欺欺人。

    判据是**内容**而不是文件名: 文件名完全不可靠, 真正的需求也可能叫
    `debug-test.json`, 而调试残留反而叫 `REQ-CS-001.json`。

    去重细节(为何排除 req_id/title、为何选信息量最大者做代表)见 src/req_dedup.py。
    """
    keep, dup_map = dedupe_requirements(requirements_dir)
    if dup_map:
        logger.info("  需求去重明细:\n%s", dedupe_report(dup_map))
    return keep


def _build_facts(req_files: List[Path], db_path: Path, domain_name: str,
                 chunks: List[CodeChunk], links: List[Any]) -> Dict[str, Any]:
    """
    抽取业务事实并入库(带证据门禁)。

    为什么单独一层而不是混在 chunk 索引里:
      chunk 索引的单元是"代码符号", 而业务问题问的是"规则是什么"。
      两者靠检索拼不起来 —— 实测需求里的「禁止自动上线」在代码里
      没有任何字面量, 检索必然 0 命中, 于是模型开始编。

    门禁(evidence_count >= 2)是这一层的全部价值:
      没通过的事实不删, 而是记进 rejected —— 那些是"我们知道存在但
      证据不足"的知识缺口, 是要去补需求/补代码的待办清单。
    """
    out = {"accepted": 0, "rejected": 0, "requirements": len(req_files),
           "rejected_detail": [], "cases": 0, "case_coverage": {}}
    if not req_files:
        return out

    with IndexBuilder(db_path, domain_name) as b:
        ex = FactExtractor(chunks=chunks, links=links)
        accepted_facts = []
        for rf in req_files:
            try:
                cands = ex.extract(rf)
            except Exception as e:
                logger.warning(f"  需求解析失败 {rf}: {e}")
                continue
            passed, rejected = filter_accepted(cands,
                                              min_evidence=FACT_MIN_EVIDENCE)
            for f in rejected:
                out["rejected_detail"].append({
                    "title": f.title,
                    "evidence_count": f.evidence_count,
                    "source": rf.name,
                })
            for f in passed:
                ok, why = b.add_fact(f, min_evidence=FACT_MIN_EVIDENCE)
                if ok:
                    out["accepted"] += 1
                    accepted_facts.append(f)
                else:
                    out["rejected"] += 1
                    out["rejected_detail"].append({
                        "title": f.title, "reason": why, "source": rf.name})

        # Fact -> 七维用例 -> 入库。
        # 必须与 add_fact 在同一个 IndexBuilder 里: add_cases 会自动回填
        # case_fact_map, 分两次开连接会让映射写不进去, 覆盖报告恒为 0 且不报错。
        if accepted_facts:
            gen = FactCaseGenerator(id_prefix=f"{domain_name}-FACT")
            cases = gen.generate(accepted_facts)
            out["cases"] = b.add_cases(cases, task_id=f"kb-build:{domain_name}")
            cov = FactCaseGenerator.coverage_report(accepted_facts, cases)
            out["case_coverage"] = {
                "fact_total": cov["fact_total"],
                "fact_covered": cov["fact_covered"],
                "coverage_rate": cov["coverage_rate"],
                "case_total": cov["case_total"],
                "vague_expected": cov["vague_expected"],
                "dimension_dist": cov["dimension_dist"],
            }
            logger.info(
                f"  用例: {out['cases']} 条 / 覆盖事实 "
                f"{cov['fact_covered']}/{cov['fact_total']} "
                f"({cov['coverage_rate']}%) / 维度分布 {cov['dimension_dist']}"
            )
        b.commit()

    logger.info(
        f"  业务事实: 入库 {out['accepted']} 条 / 证据不足待补 "
        f"{len(out['rejected_detail'])} 条"
    )
    return out


def _build_relations(chunks: List[CodeChunk], builder: IndexBuilder):
    """
    构建调用关系 (跨文件)。

    历史版本只在同文件内匹配 (by_file[chunk.file_path]) ——
    实测 landing-page 8639 符号仅 409 条关系且跨文件为 0,
    Controller->Service->Mapper 这条链完全断开。

    现改为 CallGraphBuilder: 支持跨文件匹配, 并按
    字段类型/同文件/全库唯一 三级降级消歧。
    同名歧义时**不写边**(宁可召回少, 也不能连错到别的模块的表)。
    """
    relations = CallGraphBuilder(chunks).build()
    for rel in relations:
        builder.add_relation(
            rel.from_chunk_id, rel.to_chunk_id, rel.relation_type,
            resolution=rel.resolution, candidates=rel.candidates,
        )
    stats = summarize_by_resolution(relations)
    logger.debug(
        f"  构建 {len(relations)} 条调用关系 "
        f"(exact={stats['exact']} guess={stats['guess']} ambiguous={stats['ambiguous']})"
    )
    return stats


def _enrich_with_llm(
    chunks: List[CodeChunk],
    builder: IndexBuilder,
    max_workers: int = 5,
) -> int:
    """并发用 LLM 增强 chunks, 返回成功计数"""
    success_count = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(enrich_chunk, chunk): chunk
            for chunk in chunks
        }
        for future in as_completed(futures):
            chunk = futures[future]
            try:
                result = future.result()
                tags = result.get("tags", [])
                summary = result.get("summary", "")

                # 仅当有真实产出才算成功
                if tags or summary:
                    builder.add_chunk(
                        chunk,
                        business_tags=tags,
                        summary=summary,
                    )
                    success_count += 1
            except Exception as e:
                logger.warning(f"LLM 增强失败: {e}")
    return success_count


# ============================================================
# 主入口
# ============================================================

def build_all_domains(
    project_root: Path,
    kb_root: Path,
    only_domain: Optional[str] = None,
    max_files: Optional[int] = None,
    enrich_with_llm: bool = True,
    max_workers: int = 5,
    requirements_dir: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """
    构建所有域。

    requirements_dir 指定后, **每个域都会跑一遍 Fact 抽取**。
    同一个需求文件对多个域各跑一次是有意为之:
      需求说「客服异常自动下线」, 实现可能在 customer-service, 也可能在 cross。
      哪个域能凑够 2 组证据, 门禁就自动把这条事实收进哪个域 ——
      不靠人工判断"这条规则属于哪个模块"。
    """

    config_path = project_root / ".config" / "kb-config.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"kb-config.yaml 不存在: {config_path}")

    kb_config = load_kb_config(config_path)
    excludes = kb_config.get("excludes", {})
    domains = kb_config.get("domains", {})

    audit = get_audit_logger()
    results = []

    domain_list = [only_domain] if only_domain else list(domains.keys())
    for domain_name in domain_list:
        if domain_name not in domains:
            logger.warning(f"域 [{domain_name}] 不存在, 跳过")
            continue

        try:
            stats = build_domain(
                domain_name,
                domains[domain_name],
                excludes,
                kb_root,
                max_files=max_files,
                enrich_with_llm=enrich_with_llm,
                max_workers=max_workers,
                fact_sources=_req_files(requirements_dir),
            )
            results.append(stats)
            audit.log(
                action="kb.domain.build",
                target=domain_name,
                result="success",
                detail=stats,
            )
        except Exception as e:
            logger.error(f"域 [{domain_name}] 构建失败: {e}")
            audit.log(
                action="kb.domain.build",
                target=domain_name,
                result="failed",
                detail={"error": str(e)},
            )
            results.append({"domain": domain_name, "error": str(e)})

    return results


# ============================================================
# 查询接口
# ============================================================
#
# 这两个函数曾经被 bin/testcase.py 的 `kb list` / `kb stats` 引用,
# 但**从未实现** —— CLI 一敲就 ImportError。
# 教训与之前那次一样: 项目有 280 条测试, 却没一条覆盖用户实际会敲的
# `kb list`。凡是 CLI 注册了的命令, 就必须有对应的测试。

def list_domains(project_root: Path) -> List[str]:
    """
    列出配置里定义的所有业务域。

    返回配置里的域名(而非仅已建索引的域) —— 用户要问的是
    "这个项目该有哪些域", 而不是"我恰好建过哪几个"。
    """
    config_path = Path(project_root) / ".config" / "kb-config.yaml"
    if not config_path.exists():
        logger.warning(f"kb-config.yaml 不存在: {config_path}")
        return []
    try:
        kb_config = load_kb_config(config_path)
    except Exception as e:
        logger.warning(f"kb-config.yaml 解析失败: {e}")
        return []
    return list((kb_config.get("domains") or {}).keys())


def show_stats(project_root: Path, domain: str,
               kb_root: Optional[Path] = None) -> Dict[str, Any]:
    """
    汇总某个域的索引统计并打印。

    **统计直接查库, 不读 index_meta** —— 因为 meta 里存的 stats 是构建当时的
    快照, 而 Fact 层与用例可以在不重建符号索引的情况下反复回填
    (见 tools/backfill_facts.py), meta 早就过期了。读 meta 会让用户看到
    "事实=0"而库里明明有 13 条。
    """
    import sqlite3

    root = Path(kb_root or project_root)
    db_path = root / "domains" / domain / "_index.db"
    if not db_path.exists():
        print(f"⚠️  域 [{domain}] 索引不存在: {db_path}")
        print(f"    先跑: testcase kb build {domain}")
        return {}

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    out: Dict[str, Any] = {}
    try:
        def count(table: str) -> int:
            try:
                return conn.execute(
                    f"SELECT count(*) FROM {table}").fetchone()[0]
            except sqlite3.Error:
                return 0

        out["domain"] = domain
        out["symbols"] = count("symbols")
        out["chunks"] = count("chunks")
        out["relations"] = count("relations")
        out["sql_statements"] = count("sql_index")
        out["facts"] = count("facts")
        out["fact_evidence"] = count("fact_evidence")
        out["cases"] = count("cases")
        out["case_fact_map"] = count("case_fact_map")

        n_tables = conn.execute(
            "SELECT count(DISTINCT tables) FROM sql_index "
            "WHERE tables IS NOT NULL AND tables != ''"
        ).fetchone()[0]
        out["sql_tables"] = n_tables

        # 业务规则覆盖度 —— 只反映"有没有用例回填 fact_id",
        # 不代表用例断言本身写对了, 打印时必须带上这句提醒。
        rep = conn.execute(
            """SELECT count(*) AS total,
                      sum(CASE WHEN EXISTS (
                          SELECT 1 FROM case_fact_map m
                          WHERE m.fact_id = f.fact_id) THEN 1 ELSE 0 END) AS covered
               FROM facts f WHERE f.status='accepted'"""
        ).fetchone()
        total = rep["total"] or 0
        covered = rep["covered"] or 0
        out["fact_total"] = total
        out["fact_covered"] = covered
        out["coverage_rate"] = round(covered / total * 100, 1) if total else 0.0

        dims = conn.execute(
            "SELECT dimension_name, count(*) n FROM cases "
            "GROUP BY dimension_name ORDER BY n DESC"
        ).fetchall()
        out["dimension_dist"] = {r["dimension_name"]: r["n"] for r in dims}

        vague = conn.execute(
            "SELECT count(*) FROM cases WHERE determinacy != 'ok'"
        ).fetchone()[0]
        out["vague_expected"] = vague
        out["db_size_mb"] = round(db_path.stat().st_size / 1024 / 1024, 1)
    finally:
        conn.close()

    print(f"📊 域 [{domain}] 索引统计")
    print(f"   符号 {out['symbols']} / chunks {out['chunks']} / "
          f"关系 {out['relations']}")
    print(f"   SQL {out['sql_statements']} 条 / 涉及表 {out['sql_tables']} 个")
    print(f"   业务事实 {out['facts']} 条 / 证据 {out['fact_evidence']} 条")
    print(f"   用例 {out['cases']} 条 / 映射 {out['case_fact_map']} 条")
    print(f"   规则覆盖 {out['fact_covered']}/{out['fact_total']} "
          f"({out['coverage_rate']}%)  ← 只反映有无用例回填, "
          f"不代表断言正确")
    if out["dimension_dist"]:
        print(f"   维度分布 {out['dimension_dist']}")
    if out["vague_expected"]:
        print(f"   ⚠ 预期不可判定 {out['vague_expected']} 条(需人工补具体值)")
    print(f"   索引体积 {out['db_size_mb']} MB")
    return out


# ============================================================
# 命令行入口
# ============================================================

if __name__ == "__main__":
    import sys

    setup_logging(level="INFO")

    project_root = Path(__file__).parent.parent
    kb_root = project_root

    only = sys.argv[1] if len(sys.argv) > 1 else None
    max_files = int(sys.argv[2]) if len(sys.argv) > 2 else None

    logger.info(f"项目根: {project_root}")
    logger.info(f"知识库根: {kb_root}")
    if only:
        logger.info(f"仅构建域: {only}")
    if max_files:
        logger.info(f"限制文件数: {max_files}")

    results = build_all_domains(
        project_root=project_root,
        kb_root=kb_root,
        only_domain=only,
        max_files=max_files,
        enrich_with_llm=True,
    )

    print()
    print("=" * 60)
    print("构建结果汇总")
    print("=" * 60)
    for r in results:
        if "error" in r:
            print(f"❌ {r['domain']}: {r['error']}")
        else:
            print(f"✅ {r['domain']}: {r['files_scanned']} 文件 / {r['chunks_created']} chunks / {r['build_duration_sec']}s")