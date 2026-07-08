"""
深度代码审查脚本 - 全量代码入库 (不再脑补, 只记录真实代码里的事实)

设计原则:
1. 增量持久化: 每文件审查完立即落盘, 断点可续
2. 真实性: 只记录代码里实际存在的类/方法/路由/枚举值, 不编造
3. 可恢复: 每次启动读 manifest 跳过已完成文件
"""

import json
import hashlib
import re
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Any, Optional

from src.logger import get_logger
from src.path_utils import glob_match as _glob_match  # v0.5.9: 共享跨平台 glob

logger = get_logger(__name__)


# Java HTTP / 状态注解
JAVA_ANNOTATIONS_HTTP = {
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "DeleteMapping": "DELETE",
    "RequestMapping": "REQUEST",
    "PatchMapping": "PATCH",
}


def _make_chunk_id(file_path: Path, suffix: str) -> str:
    path_hash = hashlib.md5(str(file_path).encode()).hexdigest()[:8]
    return f"{path_hash}-{suffix}"


def _safe_text(node, content: str) -> str:
    try:
        return content[node.start_byte:node.end_byte]
    except Exception:
        return ""


def _extract_annotations(node, content: str) -> List[str]:
    annotations = []
    if hasattr(node, "children"):
        for child in node.children:
            if child.type in ("modifiers", "annotations"):
                if hasattr(child, "children"):
                    for ann in child.children:
                        if ann.type == "annotation":
                            txt = _safe_text(ann, content).strip()
                            if txt:
                                annotations.append(txt)
    return annotations


def _get_node_text_by_type(node, type_name: str, content: str) -> Optional[str]:
    if hasattr(node, "children"):
        for c in node.children:
            if c.type == type_name:
                return _safe_text(c, content)
    return None


def _find_child_by_type(node, type_name: str):
    if hasattr(node, "children"):
        for c in node.children:
            if c.type == type_name:
                return c
    return None


def _get_javadoc_above(node, content: str) -> str:
    line_start = node.start_point[0]
    if line_start == 0:
        return ""
    lines = content.split("\n")
    head = "\n".join(lines[max(0, line_start - 50):line_start])
    m = re.search(r"/\*\*(.*?)\*/", head[::-1], re.DOTALL)
    if m:
        text = m.group(1)[::-1]
        lines2 = [ln.strip().lstrip("*").strip() for ln in text.split("\n")]
        return " ".join(ln for ln in lines2 if ln).strip()
    return ""


def _extract_http_path(annotation_text: str) -> Optional[str]:
    m = re.search(r'"([^"]+)"', annotation_text)
    return m.group(1) if m else None


# ============================================================
# Java 审查
# ============================================================

def audit_java_file(file_path: Path) -> Dict[str, Any]:
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"error": f"read failed: {e}", "chunks": []}

    sha = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
    line_count = content.count("\n") + 1

    file_summary = ""
    m = re.search(r"/\*\*(.*?)\*/", content, re.DOTALL)
    if m:
        text = m.group(1)
        lines = [ln.strip().lstrip("*").strip() for ln in text.split("\n")]
        file_summary = " ".join(ln for ln in lines if ln).strip()

    chunks: List[Dict[str, Any]] = []
    try:
        import tree_sitter_java
        from tree_sitter import Language, Parser
        JAVA_LANGUAGE = Language(tree_sitter_java.language())
        parser = Parser(JAVA_LANGUAGE)
        tree = parser.parse(bytes(content, "utf-8"))
        chunks = _extract_java_chunks(file_path, content, tree.root_node)
    except Exception as e:
        chunks = [{
            "chunk_id": _make_chunk_id(file_path, "file"),
            "chunk_type": "file",
            "name": file_path.stem,
            "qualified_name": str(file_path),
            "line_start": 1,
            "line_end": line_count,
            "code_preview": content[:800],
            "summary": file_summary,
            "business_tags": "[]",
            "http_route": None,
        }]

    return {
        "file_path": str(file_path),
        "file_type": "java",
        "line_count": line_count,
        "sha256": sha,
        "summary": file_summary,
        "chunks": chunks,
    }


def _extract_java_chunks(file_path: Path, content: str, root_node) -> List[Dict[str, Any]]:
    chunks: List[Dict[str, Any]] = []

    def walk(node):
        if node.type == "class_declaration":
            class_name = _get_node_text_by_type(node, "identifier", content) or file_path.stem
            class_javadoc = _get_javadoc_above(node, content)
            chunks.append({
                "chunk_id": _make_chunk_id(file_path, f"class:{class_name}"),
                "chunk_type": "class",
                "name": class_name,
                "qualified_name": class_name,
                "line_start": node.start_point[0] + 1,
                "line_end": node.end_point[0] + 1,
                "code_preview": _safe_text(node, content)[:1500],
                "summary": class_javadoc,
                "business_tags": "[]",
                "http_route": None,
            })
            class_body = _find_child_by_type(node, "class_body")
            if class_body:
                for member in class_body.children:
                    if member.type == "method_declaration":
                        mc = _extract_method_chunk(file_path, content, member, class_name)
                        if mc:
                            chunks.append(mc)
                    elif member.type == "field_declaration":
                        fc = _extract_field_chunk(file_path, content, member, class_name)
                        if fc:
                            chunks.append(fc)
        elif node.type == "interface_declaration":
            iface_name = _get_node_text_by_type(node, "identifier", content) or file_path.stem
            iface_javadoc = _get_javadoc_above(node, content)
            chunks.append({
                "chunk_id": _make_chunk_id(file_path, f"interface:{iface_name}"),
                "chunk_type": "interface",
                "name": iface_name,
                "qualified_name": iface_name,
                "line_start": node.start_point[0] + 1,
                "line_end": node.end_point[0] + 1,
                "code_preview": _safe_text(node, content)[:1500],
                "summary": iface_javadoc,
                "business_tags": "[]",
                "http_route": None,
            })
        elif node.type == "enum_declaration":
            enum_name = _get_node_text_by_type(node, "identifier", content) or file_path.stem
            enum_javadoc = _get_javadoc_above(node, content)
            enum_values = []
            enum_constants = []
            # 不用 tree-sitter 的 enum_constant 节点 (0.23.5 解析异常),
            # 直接 regex 提取 enum body 里的 IDENTIFIER(...) 模式
            enum_body_text = _safe_text(node, content)
            for m in re.finditer(
                r"(?m)^\s*([A-Z][A-Z0-9_]*)\s*\(",
                enum_body_text,
            ):
                name = m.group(1)
                if name not in enum_values:
                    enum_values.append(name)
                    # 找 javadoc 紧贴的上一行
                    ec_javadoc = ""
                    enum_constants.append({
                        "name": name,
                        "javadoc": ec_javadoc,
                    })
            chunks.append({
                "chunk_id": _make_chunk_id(file_path, f"enum:{enum_name}"),
                "chunk_type": "enum",
                "name": enum_name,
                "qualified_name": enum_name,
                "line_start": node.start_point[0] + 1,
                "line_end": node.end_point[0] + 1,
                "code_preview": _safe_text(node, content)[:1500],
                "summary": enum_javadoc or f"枚举值: {', '.join(enum_values[:10])}",
                "business_tags": json.dumps(enum_values[:20], ensure_ascii=False),
                "http_route": None,
                "enum_values": enum_values,
                "enum_constants": enum_constants,
            })
        else:
            if hasattr(node, "children"):
                for child in node.children:
                    walk(child)

    for child in root_node.children:
        walk(child)
    return chunks


def _extract_method_chunk(file_path, content, method_node, class_name) -> Optional[Dict]:
    # tree-sitter-java 0.23.5 + lombok bug: identifier node often points to
    # wrong token (string literal in body, cross-line expr, etc).
    # Fix: extract method name from the SIGNATURE LINE via regex (reliable).
    method_name = _extract_method_name_from_signature(content, method_node)
    if not method_name:
        return None
    annotations = _extract_annotations(method_node, content)
    javadoc = _get_javadoc_above(method_node, content)
    http_route = None
    for ann in annotations:
        ann_name = ann.split("(")[0].strip().lstrip("@")
        if ann_name in JAVA_ANNOTATIONS_HTTP:
            http_method = JAVA_ANNOTATIONS_HTTP[ann_name]
            path = _extract_http_path(ann)
            if path:
                http_route = f"{http_method} {path}"
                break
    return {
        "chunk_id": _make_chunk_id(file_path, f"method:{class_name}.{method_name}"),
        "chunk_type": "http_endpoint" if http_route else "method",
        "name": method_name,
        "qualified_name": f"{class_name}.{method_name}",
        "line_start": method_node.start_point[0] + 1,
        "line_end": method_node.end_point[0] + 1,
        "code_preview": _safe_text(method_node, content)[:800],
        "summary": javadoc,
        "business_tags": json.dumps(annotations, ensure_ascii=False),
        "http_route": http_route,
    }


def _extract_method_name_from_signature(content: str, method_node) -> Optional[str]:
    """
    Reliable method name extraction that bypasses tree-sitter-java
    lombok bug. Strategy:
    1. Take signature line (the line where method_declaration starts)
    2. Strip modifiers (public/private/static/final/abstract)
       and annotations (anything starting with @)
    3. Find the last identifier before '('
    4. Sanity check: must be valid Java identifier
    5. Fallback: try tree-sitter identifier node
    """
    import re as _re
    lines = content.split("\n")
    start_line = method_node.start_point[0]  # 0-indexed
    if start_line >= len(lines):
        return None
    # Get signature line(s) until we find '('
    sig_text = ""
    for i in range(start_line, min(start_line + 5, len(lines))):
        sig_text += " " + lines[i]
        if "(" in sig_text:
            break

    # Strip string literals to avoid matching identifiers inside them
    sig_no_strings = _re.sub(r'"[^"]*"', "", sig_text)

    # Strip annotations (@xxx)
    sig_no_ann = _re.sub(r"@\w+(?:\([^)]*\))?", "", sig_no_strings)

    # Find last identifier (alphanumeric + underscore, start with letter/_) before '('
    m = _re.search(r"([A-Za-z_]\w*)\s*\(", sig_no_ann)
    if m:
        candidate = m.group(1)
        # Reject Java keywords
        if candidate not in ("if", "for", "while", "switch", "return",
                              "new", "throw", "try", "catch", "synchronized",
                              "case", "default", "do", "else"):
            return candidate
    # Fallback: tree-sitter identifier node (may be wrong due to lombok bug,
    # but better than nothing)
    try:
        ts_name = _get_node_text_by_type(method_node, "identifier", content)
        if ts_name and _re.match(r"^[A-Za-z_]\w*$", ts_name):
            return ts_name
    except Exception:
        pass
    return None


def _extract_field_chunk(file_path, content, field_node, class_name) -> Optional[Dict]:
    annotations = _extract_annotations(field_node, content)
    if not annotations:
        return None
    field_name = _get_node_text_by_type(field_node, "identifier", content)
    if not field_name:
        return None
    return {
        "chunk_id": _make_chunk_id(file_path, f"field:{class_name}.{field_name}"),
        "chunk_type": "field",
        "name": field_name,
        "qualified_name": f"{class_name}.{field_name}",
        "line_start": field_node.start_point[0] + 1,
        "line_end": field_node.end_point[0] + 1,
        "code_preview": _safe_text(field_node, content)[:400],
        "summary": f"字段注解: {', '.join(annotations)}",
        "business_tags": json.dumps(annotations, ensure_ascii=False),
        "http_route": None,
    }


# ============================================================
# Vue/TS 审查
# ============================================================

def audit_vue_file(file_path: Path) -> Dict[str, Any]:
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"error": f"read failed: {e}", "chunks": []}

    sha = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
    line_count = content.count("\n") + 1
    chunks = []
    name = file_path.stem

    script_match = re.search(r"<script[^>]*>(.*?)</script>", content, re.DOTALL)
    if script_match:
        script = script_match.group(1)
        ed_match = re.search(r"export\s+default\s*\{", script)
        if ed_match:
            name_match = re.search(r"name\s*:\s*['\"]([\w-]+)['\"]", script)
            if name_match:
                name = name_match.group(1)
            data_block = re.search(
                r"data\s*\(\s*\)\s*\{[^}]*return\s*\{(.*?)\}\s*\}",
                script, re.DOTALL,
            )
            data_fields = []
            if data_block:
                for fm in re.finditer(r"(\w+)\s*:", data_block.group(1)):
                    data_fields.append(fm.group(1))

            methods_block = re.search(
                r"methods\s*:\s*\{(.*?)\n\s*\}", script, re.DOTALL,
            )
            method_names = []
            if methods_block:
                for mm in re.finditer(r"(\w+)\s*\([^)]*\)\s*\{", methods_block.group(1)):
                    method_names.append(mm.group(1))

            chunks.append({
                "chunk_id": _make_chunk_id(file_path, f"vue:component:{name}"),
                "chunk_type": "vue_component",
                "name": name,
                "qualified_name": name,
                "line_start": 1,
                "line_end": line_count,
                "code_preview": content[:1500],
                "summary": (
                    f"Vue 组件 '{name}'. "
                    f"data: {', '.join(data_fields[:10])}. "
                    f"methods: {', '.join(method_names[:10])}"
                ),
                "business_tags": json.dumps(data_fields[:20], ensure_ascii=False),
                "http_route": None,
                "vue_data_fields": data_fields,
                "vue_methods": method_names,
            })
        else:
            chunks.append({
                "chunk_id": _make_chunk_id(file_path, f"vue:script:{name}"),
                "chunk_type": "vue_script",
                "name": name,
                "qualified_name": name,
                "line_start": 1,
                "line_end": line_count,
                "code_preview": script[:1500],
                "summary": "Vue script 块 (无 export default)",
                "business_tags": "[]",
                "http_route": None,
            })
    else:
        chunks.append({
            "chunk_id": _make_chunk_id(file_path, f"vue:template:{name}"),
            "chunk_type": "vue_template",
            "name": name,
            "qualified_name": name,
            "line_start": 1,
            "line_end": line_count,
            "code_preview": content[:1500],
            "summary": "Vue 模板组件 (无 script 块)",
            "business_tags": "[]",
            "http_route": None,
        })

    return {
        "file_path": str(file_path),
        "file_type": "vue",
        "line_count": line_count,
        "sha256": sha,
        "summary": "",
        "chunks": chunks,
    }


# ============================================================
# 调度器
# ============================================================

def scan_files(source: Dict, excludes: Dict) -> List[Path]:
    path = Path(source["path"])
    if not path.exists():
        return []
    patterns = excludes.get("patterns", [])
    max_size = excludes.get("max_file_size", 1048576)
    binary_exts = set(excludes.get("binary_extensions", []))
    files = []
    for f in path.rglob("*"):
        if not f.is_file():
            continue
        try:
            rel = str(f.relative_to(path))
        except ValueError:
            continue
        if any(_glob_match(rel, p) for p in patterns):
            continue
        try:
            if f.stat().st_size > max_size:
                continue
        except OSError:
            continue
        if f.suffix.lower() in binary_exts:
            continue
        if f.suffix.lower() not in {".java", ".vue", ".ts", ".js"}:
            continue
        files.append(f)
    return sorted(files)


# v0.5.9: 跨平台 glob_match 已抽到 src/path_utils.py 共享. 保留此函数
# 仅为向后兼容 (原类内调用方), 不再额外维护独立版本.
_glob_match = _glob_match


def load_manifest(domain_dir: Path) -> Dict[str, Any]:
    manifest_path = domain_dir / "_audit_manifest.json"
    if manifest_path.exists():
        try:
            with open(manifest_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "domain": domain_dir.name,
        "files": {},
        "stats": {"total": 0, "done": 0, "failed": 0, "pending": 0},
        "last_update": None,
    }


def save_manifest(domain_dir: Path, manifest: Dict[str, Any]):
    manifest_path = domain_dir / "_audit_manifest.json"
    manifest["last_update"] = time.time()
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)


def update_db_with_chunks(domain_db_path: Path, domain: str, file_path: str, chunks: List[Dict]):
    from src.index_builder import IndexBuilder
    if not domain_db_path.exists():
        domain_db_path.parent.mkdir(parents=True, exist_ok=True)
        with IndexBuilder(domain_db_path, domain) as b:
            pass

    from datetime import datetime
    conn = sqlite3.connect(str(domain_db_path))
    cur = conn.cursor()
    now = datetime.now().isoformat()
    for ch in chunks:
        cur.execute(
            """INSERT OR REPLACE INTO symbols
               (chunk_id, domain, file_path, file_type, language, chunk_type,
                name, qualified_name, line_start, line_end, http_route,
                business_tags, summary, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ch["chunk_id"], domain, file_path,
                "java" if file_path.endswith(".java") else "vue",
                "java" if file_path.endswith(".java") else "vue",
                ch.get("chunk_type", "unknown"),
                ch.get("name", ""), ch.get("qualified_name", ""),
                ch.get("line_start", 0), ch.get("line_end", 0),
                ch.get("http_route"),
                ch.get("business_tags", "[]"),
                ch.get("summary", ""),
                now, now,
            ),
        )
        cur.execute("DELETE FROM code_fts WHERE chunk_id=?", (ch["chunk_id"],))
        cur.execute(
            """INSERT INTO code_fts
               (chunk_id, domain, file_path, name, qualified_name,
                code_content, summary)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                ch["chunk_id"], domain, file_path,
                ch.get("name", ""), ch.get("qualified_name", ""),
                ch.get("code_preview", ""),
                ch.get("summary", ""),
            ),
        )
        cur.execute("DELETE FROM chunks WHERE chunk_id=?", (ch["chunk_id"],))
        cur.execute(
            """INSERT INTO chunks
               (chunk_id, domain, file_path, chunk_data, indexed_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                ch["chunk_id"], domain, file_path,
                json.dumps(ch, ensure_ascii=False), now,
            ),
        )
    conn.commit()
    conn.close()


def run_domain_audit(
    domain_name: str,
    sources: List[Dict],
    excludes: Dict,
    domain_dir: Path,
    batch_size: int = 20,
    max_files: Optional[int] = None,
):
    domain_dir.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(domain_dir)

    all_files: List[Path] = []
    for source in sources:
        files = scan_files(source, excludes)
        logger.info(f"  source [{source['id']}]: {len(files)} 文件")
        all_files.extend(files)
    all_files = sorted(set(all_files))
    if max_files:
        all_files = all_files[:max_files]

    logger.info(f"  domain [{domain_name}]: 共 {len(all_files)} 文件待审查")

    for f in all_files:
        p = str(f)
        if p not in manifest["files"]:
            manifest["files"][p] = {"status": "pending"}
    manifest["stats"]["total"] = len(all_files)

    domain_db_path = domain_dir / "_index.db"
    pending = [
        f for f in all_files
        if manifest["files"].get(str(f), {}).get("status") != "done"
    ]
    logger.info(f"  待处理 (跳过 done): {len(pending)} 文件")

    done_count = manifest["stats"].get("done", 0)
    failed_count = manifest["stats"].get("failed", 0)

    for i, file_path in enumerate(pending, 1):
        rel = str(file_path)
        try:
            if file_path.suffix.lower() == ".java":
                audit_result = audit_java_file(file_path)
            elif file_path.suffix.lower() in {".vue", ".ts", ".js"}:
                audit_result = audit_vue_file(file_path)
            else:
                manifest["files"][rel] = {
                    "status": "skipped",
                    "reason": "unsupported",
                    "timestamp": time.time(),
                }
                save_manifest(domain_dir, manifest)
                continue

            if "error" in audit_result and not audit_result.get("chunks"):
                manifest["files"][rel] = {
                    "status": "failed",
                    "error": audit_result["error"],
                    "timestamp": time.time(),
                }
                failed_count += 1
            else:
                try:
                    update_db_with_chunks(
                        domain_db_path, domain_name, rel, audit_result["chunks"],
                    )
                except Exception as e:
                    logger.warning(f"  写 DB 失败 {rel}: {e}")

                manifest["files"][rel] = {
                    "status": "done",
                    "file_type": audit_result.get("file_type"),
                    "line_count": audit_result.get("line_count"),
                    "sha256": audit_result.get("sha256"),
                    "chunk_count": len(audit_result.get("chunks", [])),
                    "timestamp": time.time(),
                }
                done_count += 1

        except Exception as e:
            logger.error(f"  审查 {rel} 异常: {e}")
            manifest["files"][rel] = {
                "status": "failed",
                "error": str(e),
                "timestamp": time.time(),
            }
            failed_count += 1

        manifest["stats"]["done"] = done_count
        manifest["stats"]["failed"] = failed_count
        save_manifest(domain_dir, manifest)

        if i % batch_size == 0:
            logger.info(
                f"  进度: {i}/{len(pending)} "
                f"(done={done_count}, failed={failed_count})"
            )

    logger.info(
        f"✅ domain [{domain_name}] 审查完成: "
        f"done={done_count}, failed={failed_count}"
    )
    return manifest


# ============================================================
# CLI
# ============================================================

def show_progress(domain_names: List[str], project_root: Path):
    """显示所有 domain 的审查进度 (不消耗 LLM, 纯读 manifest)."""
    print("=" * 70)
    print(f"{'Domain':<22s} {'Total':>8s} {'Done':>8s} {'Failed':>8s} {'Pending':>8s} {'%':>6s}")
    print("-" * 70)
    grand_total = grand_done = grand_failed = 0
    for d in domain_names:
        domain_dir = project_root / "domains" / d
        manifest_path = domain_dir / "_audit_manifest.json"
        if manifest_path.exists():
            try:
                with open(manifest_path, encoding="utf-8") as f:
                    m = json.load(f)
                total = m.get("stats", {}).get("total", 0)
                done = m.get("stats", {}).get("done", 0)
                failed = m.get("stats", {}).get("failed", 0)
            except Exception:
                total = done = failed = 0
        else:
            total = done = failed = 0
        pending = max(0, total - done - failed)
        pct = (done * 100 // total) if total > 0 else 0
        print(f"{d:<22s} {total:>8d} {done:>8d} {failed:>8d} {pending:>8d} {pct:>5d}%")
        grand_total += total
        grand_done += done
        grand_failed += failed
    print("-" * 70)
    g_pending = max(0, grand_total - grand_done - grand_failed)
    g_pct = (grand_done * 100 // grand_total) if grand_total > 0 else 0
    print(f"{'TOTAL':<22s} {grand_total:>8d} {grand_done:>8d} {grand_failed:>8d} {g_pending:>8d} {g_pct:>5d}%")
    print("=" * 70)


if __name__ == "__main__":
    import sys
    import yaml
    from src.logger import setup_logging

    setup_logging()

    PROJECT_ROOT = Path(__file__).parent.parent
    config_path = PROJECT_ROOT / ".config" / "kb-config.yaml"
    with open(config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    excludes = cfg.get("excludes", {})
    domains = cfg.get("domains", {})

    # 特殊子命令: progress / status
    if len(sys.argv) >= 2 and sys.argv[1] in ("progress", "status"):
        all_domains = sorted(domains.keys())
        show_progress(all_domains, PROJECT_ROOT)
        sys.exit(0)

    # 特殊子命令: run-all (按顺序跑所有未完成的 domain)
    if len(sys.argv) >= 2 and sys.argv[1] == "run-all":
        all_domains = sorted(domains.keys())
        print(f"🚀 run-all: 顺序跑 {len(all_domains)} 个 domain")
        for d in all_domains:
            print(f"\n=== {d} ===")
            sources = domains[d].get("code_sources", [])
            domain_dir = PROJECT_ROOT / "domains" / d
            run_domain_audit(
                domain_name=d,
                sources=sources,
                excludes=excludes,
                domain_dir=domain_dir,
                batch_size=20,
                max_files=None,
            )
        print("\n" + "=" * 70)
        print("✅ run-all 全部完成, 最终进度:")
        show_progress(all_domains, PROJECT_ROOT)
        sys.exit(0)

    # 常规: 按 domain 名跑
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python -m src.deep_audit <domain_name> [--max-files N]")
        print("  python -m src.deep_audit progress    # 看所有 domain 进度")
        print("  python -m src.deep_audit run-all      # 顺序跑所有 domain (跳过已 done)")
        sys.exit(1)

    domain_name = sys.argv[1]
    max_files = None
    if "--max-files" in sys.argv:
        idx = sys.argv.index("--max-files")
        max_files = int(sys.argv[idx + 1])

    if domain_name not in domains:
        print(f"❌ domain [{domain_name}] 不存在")
        print(f"  可用: {list(domains.keys())}")
        sys.exit(1)

    sources = domains[domain_name].get("code_sources", [])
    domain_dir = PROJECT_ROOT / "domains" / domain_name

    run_domain_audit(
        domain_name=domain_name,
        sources=sources,
        excludes=excludes,
        domain_dir=domain_dir,
        batch_size=20,
        max_files=max_files,
    )
