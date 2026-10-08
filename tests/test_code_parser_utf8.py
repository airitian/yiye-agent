"""
代码解析器回归测试

核心覆盖 BUG: tree-sitter 的 start_byte/end_byte 是**字节**偏移,
而 Python str 是**字符**语义. 源码含中文时二者错位,导致符号名被切成
"从中间截断的代码片段".

实测症状 (customer-service 域1152 条符号):
    name  = 'aService {\\n\\n  '         应为 'CaptchaService'
    qname = 'tchaService implement.{\\n' 应为 'GraphicCaptchaService'
    name  = 'ption {\\n\\t\\tif('          应为 '某方法名'

影响: 符号名被污染 -> BM25 检索召回错误chunk -> LLM 拿到不存在的"方法名"
-> 反幻觉机制失效 (它相信这些代码符号是真实的)
"""

from pathlib import Path

from src.code_parser import JavaParser


JAVA_WITH_CHINESE = '''package ai.yiye.agent;

/**
 * 客服配置服务
 */
public class AgentConfService extends BaseService {
    // 更新客服状态
    public void updateStatusByAgentId(Long agentId) {
        if (agentId == null) { return; }
        cache.put(agentId, "在线");
    }

    /** 查询客服在线状态 */
    public String queryAgentStatus(Long agentId) {
        return cache.get(agentId);
    }
}
'''


def _write(tmp_path: Path, content: str, name: str = "AgentConfService.java") -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


def _assert_clean(chunks, label: str):
    """符号名不得含代码片段特征"""
    for c in chunks:
        for field in ("name", "qualified_name"):
            val = getattr(c, field, "") or ""
            assert "\n" not in val, f"{label}: {field} 含换行 -> {val!r}"
            assert "{" not in val, f"{label}: {field} 含 '{{' -> {val!r}"
            assert "}" not in val, f"{label}: {field} 含 '}}' -> {val!r}"
            assert ";" not in val, f"{label}: {field} 含 ';' -> {val!r}"
            assert val.strip() == val, f"{label}: {field} 首尾有空白 -> {val!r}"


def test_java_parse_with_chinese_no_name_corruption(tmp_path: Path):
    """
    核心回归: 中文注释 + 中文字符串的 Java 文件,
    符号名必须完整干净 (BUG 修复前会被截成'vice extends Bas' 之类)
    """
    path = _write(tmp_path, JAVA_WITH_CHINESE)
    chunks = JavaParser().parse_file(path)

    assert chunks, "应至少解析出 class + 2 个 method"
    _assert_clean(chunks, "中文源码")

    names = {c.name for c in chunks}
    assert "AgentConfService" in names, f"类名解析错误: {names}"
    assert "updateStatusByAgentId" in names, f"方法名解析错误: {names}"
    assert "queryAgentStatus" in names, f"方法名解析错误: {names}"


def test_java_qualified_name_contains_class_and_method(tmp_path: Path):
    path = _write(tmp_path, JAVA_WITH_CHINESE)
    chunks = JavaParser().parse_file(path)

    by_name = {c.name: c for c in chunks}
    svc = by_name["AgentConfService"]
    assert svc.qualified_name == "AgentConfService"

    method = by_name["updateStatusByAgentId"]
    assert method.qualified_name == "AgentConfService.updateStatusByAgentId"
    assert method.chunk_type == "method"
    assert method.line_start > 0 and method.line_end >= method.line_start


def test_chinese_only_before_symbol_shifts_offsets(tmp_path: Path):
    """
    大量中文在符号之前 -> 字符偏移与字节偏移错位最严重.
    修复前这里的符号名会被截断.
    """
    src = (
        "package ai.yiye.agent;\n\n"
        "/**\n"
        " * 这是一个用于测试的中文类注释.\n"
        " * 里面包含 lots of 中文文字 to shift byte offsets.\n"
        " * 目的: 让字节偏移远大于字符偏移.\n"
        " */\n"
        "public class 中文注释类 {\n"
        "    /** 更新状态 */\n"
        "    public void 刷新状态() { }\n"
        "\n"
        "    /** 查询状态 */\n"
        "    public String 查询状态() { return null; }\n"
        "}\n"
    )
    path = _write(tmp_path, src, "中文注释类.java")
    chunks = JavaParser().parse_file(path)

    assert chunks
    _assert_clean(chunks, "纯中文标识符")

    names = {c.name for c in chunks}
    assert "中文注释类" in names, f"中文类名解析错误: {names}"


def test_code_content_is_valid_utf8_not_mojibake(tmp_path: Path):
    """code_content 也走同一_get_text, 必须是合法中文而非乱码"""
    path = _write(tmp_path, JAVA_WITH_CHINESE)
    chunks = JavaParser().parse_file(path)

    for c in chunks:
        code = c.code_content or ""
        # 乱码典型特征: U+FFFD 替换字符
        assert "\ufffd" not in code, f"code_content 含解码错误字符: {code[:80]!r}"

    # 类体内的中文注释与中文字符串必须原样保留
    # (注: 类外的 javadoc 不属于 class_declaration 节点, 不在code_content 里)
    joined = "\n".join(c.code_content or "" for c in chunks)
    assert "更新客服状态" in joined, "中文注释丢失 (code_content 切片错位)"
    assert "在线" in joined, "中文字符串丢失 (code_content 切片错位)"


def test_pure_ascii_source_unaffected(tmp_path: Path):
    """纯 ASCII 源码行为不变 (ASCII 下字节偏移==字符偏移)"""
    src = (
        "package ai.yiye.agent;\n"
        "public class AgentConfService {\n"
        "    public void updateStatusByAgentId(Long agentId) {\n"
        "        cache.put(agentId, \"ONLINE\");\n"
        "    }\n"
        "}\n"
    )
    path = _write(tmp_path, src)
    chunks = JavaParser().parse_file(path)

    names = {c.name for c in chunks}
    assert "AgentConfService" in names
    assert "updateStatusByAgentId" in names
    _assert_clean(chunks, "纯 ASCII")


def test_chunk_ids_unique(tmp_path: Path):
    """中文场景下 chunk_id 也应唯一 (id 里含start_byte)"""
    path = _write(tmp_path, JAVA_WITH_CHINESE)
    chunks = JavaParser().parse_file(path)
    ids = [c.chunk_id for c in chunks]
    assert len(ids) == len(set(ids)), f"chunk_id 重复: {ids}"