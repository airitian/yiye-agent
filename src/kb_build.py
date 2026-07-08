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
import yaml
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from src.logger import setup_logging, get_logger, get_audit_logger
from src.code_parser import CodeParser, CodeChunk
from src.index_builder import IndexBuilder
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
        # 解析 JSON
        import re
        json_match = re.search(r'\{.*\}', response, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group(0))
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

        # 1. 扫描所有源的文件
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

        # 2. 解析所有文件 (并行)
        all_chunks: List[CodeChunk] = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(parser.parse, f): f
                for f in all_files
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

        # 4. 构建调用关系 (从 chunk.called_methods)
        # 简化: 同文件内同名方法建立关系
        _build_relations(all_chunks, builder)

        # 5. LLM 增强 (可选)
        if enrich_with_llm and all_chunks:
            logger.info(f"  开始 LLM 增强 ({len(all_chunks)} chunks, 并发 {max_workers})")
            enriched = _enrich_with_llm(all_chunks, builder, max_workers)
            stats["llm_enriched"] = enriched

        builder.set_meta("built_at", datetime.now().isoformat())
        builder.set_meta("domain", domain_name)
        builder.set_meta("stats", stats)
        builder.commit()

    elapsed = (datetime.now() - start).total_seconds()
    stats["build_duration_sec"] = round(elapsed, 2)

    logger.info(
        f"✅ 域 [{domain_name}] 构建完成: "
        f"{stats['files_scanned']} 文件 / {stats['chunks_created']} chunks / "
        f"{elapsed:.1f}s"
    )
    return stats


def _build_relations(chunks: List[CodeChunk], builder: IndexBuilder):
    """从 called_methods 构建简单的调用关系 (同文件内)"""
    # 按文件分组
    by_file: Dict[str, Dict[str, str]] = {}
    for c in chunks:
        if c.chunk_type not in ("method", "http_endpoint"):
            continue
        by_file.setdefault(c.file_path, {})[c.name] = c.chunk_id

    relations_added = 0
    for chunk in chunks:
        for called in chunk.called_methods:
            # 查找同文件内是否定义了被调用的方法
            file_methods = by_file.get(chunk.file_path, {})
            target = file_methods.get(called)
            if target and target != chunk.chunk_id:
                builder.add_relation(chunk.chunk_id, target, "call")
                relations_added += 1
    logger.debug(f"  构建 {relations_added} 条调用关系")


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
) -> List[Dict[str, Any]]:
    """构建所有域"""

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