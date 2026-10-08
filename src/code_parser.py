"""
代码解析器 - Tree-sitter 集成

支持 Java、Vue、JavaScript/TypeScript 及配置文件的解析与 chunk 切分
"""

import os
from pathlib import Path
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field, asdict

from src.logger import get_logger

logger = get_logger(__name__)


# ============================================================
# 数据结构
# ============================================================

@dataclass
class CodeChunk:
    """一个代码 chunk"""
    chunk_id: str
    file_path: str
    file_type: str          # java / vue / config
    language: str
    chunk_type: str         # class / method / http_endpoint / vue_component / config
    name: str = ""          # 类名/方法名/组件名
    qualified_name: str = ""  # 全限定名
    line_start: int = 0
    line_end: int = 0
    code_content: str = ""
    docstring: str = ""
    annotations: List[str] = field(default_factory=list)
    imports: List[str] = field(default_factory=list)
    called_methods: List[str] = field(default_factory=list)
    http_route: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ============================================================
# Tree-sitter 加载 (优雅降级)
# ============================================================

try:
    import tree_sitter_java
    from tree_sitter import Language, Parser

    JAVA_LANGUAGE = Language(tree_sitter_java.language())
    JAVA_AVAILABLE = True
except ImportError:
    JAVA_AVAILABLE = False
    logger.warning("tree-sitter-java 未安装, Java 解析不可用")

try:
    import tree_sitter_javascript
    JS_LANGUAGE = Language(tree_sitter_javascript.language())
    JS_AVAILABLE = True
except ImportError:
    JS_AVAILABLE = False
    logger.warning("tree-sitter-javascript 未安装, JS/Vue 解析不可用")


# ============================================================
# Java 解析器
# ============================================================

class JavaParser:
    """Java 代码 AST 解析"""

    ANNOTATION_HTTP = {
        "GetMapping": "GET",
        "PostMapping": "POST",
        "PutMapping": "PUT",
        "DeleteMapping": "DELETE",
        "RequestMapping": "REQUEST",
        "PatchMapping": "PATCH",
    }

    def __init__(self):
        if not JAVA_AVAILABLE:
            raise ImportError("tree-sitter-java 未安装")
        self.parser = Parser(JAVA_LANGUAGE)

    def parse_file(self, file_path: Path) -> List[CodeChunk]:
        """解析一个 Java 文件, 返回所有 chunk"""
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            logger.warning(f"读取文件失败 {file_path}: {e}")
            return []

        tree = self.parser.parse(bytes(content, "utf-8"))
        chunks = []

        # 顶层类型: class / interface / enum / annotation
        for node in tree.root_node.children:
            if node.type in {
                "class_declaration",
                "interface_declaration",
                "enum_declaration",
                "annotation_type_declaration",
            }:
                chunks.extend(self._parse_type(node, content, file_path))

        # 被整文件注释、空文件或语法不完整的源码仍需进入知识库，
        # 否则扫描数量与索引数量不一致，检索时会静默漏掉文件。
        if not chunks:
            import hashlib
            digest = hashlib.md5(str(file_path).encode()).hexdigest()[:8]
            chunks.append(CodeChunk(
                chunk_id=f"{digest}-java_source:{file_path.stem}",
                file_path=str(file_path),
                file_type="java",
                language="java",
                chunk_type="java_source",
                name=file_path.name,
                line_start=1,
                line_end=content.count("\n") + 1,
                code_content=content,
            ))

        return chunks

    def _parse_type(
        self, class_node, content: str, file_path: Path
    ) -> List[CodeChunk]:
        """解析 Java 顶层类型及其方法。"""
        chunks = []
        class_name = self._get_class_name(class_node, content)
        type_name = {
            "class_declaration": "class",
            "interface_declaration": "interface",
            "enum_declaration": "enum",
            "annotation_type_declaration": "annotation",
        }.get(class_node.type, "java_type")

        # 提取类注解
        class_annotations = self._extract_annotations(class_node, content)

        # 遍历 class_body
        for child in class_node.children:
            if child.type in (
                "class_body", "interface_body", "enum_body",
                "annotation_type_body",
            ):
                for member in child.children:
                    if member.type == "method_declaration":
                        chunk = self._parse_method(
                            member, content, file_path, class_name, class_annotations
                        )
                        if chunk:
                            chunks.append(chunk)
                    elif member.type == "field_declaration":
                        # 字段也可以成 chunk, 但优先级低
                        pass

        # 也为整个类生成一个 overview chunk
        class_chunk = CodeChunk(
            chunk_id=self._make_id(
                file_path,
                f"{type_name}:{class_name}:{class_node.start_point[0] + 1}",
            ),
            file_path=str(file_path),
            file_type="java",
            language="java",
            chunk_type=type_name,
            name=class_name,
            qualified_name=self._get_qualified_name(class_node, class_name),
            line_start=class_node.start_point[0] + 1,
            line_end=class_node.end_point[0] + 1,
            code_content=self._get_text(class_node, content),
            annotations=class_annotations,
        )
        chunks.insert(0, class_chunk)

        return chunks

    def _parse_method(
        self, method_node, content: str, file_path: Path,
        class_name: str, class_annotations: List[str]
    ) -> Optional[CodeChunk]:
        """解析方法"""
        method_name = self._get_method_name(method_node, content)
        if not method_name:
            return None

        annotations = self._extract_annotations(method_node, content)

        # 检测 HTTP endpoint
        http_route = None
        chunk_type = "method"
        for ann in annotations:
            ann_name = ann.split("(")[0].strip()
            if ann_name in self.ANNOTATION_HTTP:
                http_method = self.ANNOTATION_HTTP[ann_name]
                path = self._extract_http_path(ann)
                if path:
                    http_route = f"{http_method} {path}"
                    chunk_type = "http_endpoint"
                    break

        # 提取方法调用
        called_methods = self._extract_method_calls(method_node, content)

        chunk = CodeChunk(
            chunk_id=self._make_id(
                file_path,
                f"method:{class_name}.{method_name}:{method_node.start_byte}",
            ),
            file_path=str(file_path),
            file_type="java",
            language="java",
            chunk_type=chunk_type,
            name=method_name,
            qualified_name=f"{class_name}.{method_name}",
            line_start=method_node.start_point[0] + 1,
            line_end=method_node.end_point[0] + 1,
            code_content=self._get_text(method_node, content),
            annotations=annotations,
            called_methods=called_methods,
            http_route=http_route,
        )
        return chunk

    def _get_class_name(self, node, content: str) -> str:
        """从 class_declaration 节点提取类名 (找第一个直接子 identifier, 跳过 modifiers)"""
        for child in node.children:
            if child.type == "identifier":
                return self._get_text(child, content)
        return ""

    def _get_method_name(self, node, content: str) -> str:
        """从 method_declaration 节点提取方法名"""
        for child in node.children:
            if child.type == "identifier":
                return self._get_text(child, content)
        return ""

    def _get_qualified_name(self, node, name: str) -> str:
        # 简化: 从 package declaration 提取
        # TODO: 解析 package + imports
        return name

    def _extract_annotations(self, node, content: str) -> List[str]:
        """从节点提取注解 (含 modifiers 里的注解)"""
        annotations = []
        if hasattr(node, 'children'):
            for child in node.children:
                if child.type in ("modifiers", "annotations"):
                    for ann in child.children:
                        if ann.type == "annotation":
                            txt = self._get_text(ann, content).strip()
                            if txt:
                                annotations.append(txt)
        return annotations

    def _extract_http_path(self, annotation_text: str) -> Optional[str]:
        """从 @GetMapping("/path") 提取路径"""
        import re
        m = re.search(r'"([^"]+)"', annotation_text)
        return m.group(1) if m else None

    def _extract_method_calls(self, method_node, content: str) -> List[str]:
        """
        提取方法体内的方法调用。

        返回格式统一为 "receiver.method" 或 "method" (无接收者时)。
        带上接收者是为跨文件调用图消歧服务的:
          agentConfService.updateStatusByAgentId()  ->  "agentConfService.updateStatusByAgentId"
          updateStatusByAgentId()                   ->  "updateStatusByAgentId"

        只保留裸方法名会导致全库同名方法无法消歧(实测 list/save/update
        重复率极高), 缝合时会大量连错。
        """
        calls: List[str] = []
        self._walk_calls(method_node, calls, content)
        # 去重保序
        seen = set()
        out: List[str] = []
        for c in calls:
            if c not in seen:
                seen.add(c)
                out.append(c)
        return out[:80]  # 上限 80, 避免超大方法把索引撑爆

    def _walk_calls(self, node, calls: List[str], content: str):
        """
        递归遍历找 method_invocation, 同时提取调用接收者。

        tree-sitter-java 里 method_invocation 的孩子顺序:
            [接收者...]  "."  方法名  实参
        例如 agentConfMapper.updateStatusByAgentId(a, b):
            identifier(agentConfMapper) | "." | identifier(updateStatusByAgentId) | argument_list
        """
        if node.type == "method_invocation":
            kids = list(node.children)
            dot_idx = -1
            for i, child in enumerate(kids):
                if child.type == ".":
                    dot_idx = i
                    break

            if dot_idx > 0:
                # "." 之前是接收者, "." 之后第一个 identifier 是方法名
                method_node = None
                for child in kids[dot_idx + 1:]:
                    if child.type in ("identifier", "field_access"):
                        method_node = child
                        break
                if method_node is not None:
                    method_name = self._get_text(method_node, content)
                    receiver = self._get_text(kids[0], content)
                    if receiver.startswith("this"):
                        receiver = ""  # this.xxx() 不带消歧信息
                    if method_name:
                        calls.append(
                            f"{receiver}.{method_name}" if receiver else method_name
                        )
            else:
                # 无 ".": 可能是 field_access(this.a.b()) 或纯调用
                for child in kids:
                    if child.type == "identifier":
                        calls.append(self._get_text(child, content))
                        break
            return  # 已处理本节点, 不再下钻避免重复统计

        if hasattr(node, "children"):
            for child in node.children:
                self._walk_calls(child, calls, content)

    def _get_text(self, node, content: str) -> str:
        if not content:
            return ""
        try:
            # BUG fix: tree-sitter 的 start_byte/end_byte 是**字节**偏移,
            # 而 content 是 Python str (字符语义). 源文件含中文/emoji 等
            # 多字节字符时, 二者错位 -> 切出"从中间截断的代码片段".
            #
            # 实测症状 (customer-service 1152 条符号):
            #   name = 'aService {\n\n  '          <- 应为 'CaptchaService'
            #   qname= 'tchaService implement.{\n'  <- 应为 'GraphicCaptchaService'
            # 表现为符号名里含 '{' '}' ';' 和换行, 严重污染检索结果.
            #
            # 修法: 用原始 bytes 按字节切片再解码, 保证偏移语义一致.
            raw = content.encode("utf-8")
            return raw[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
        except Exception:
            return ""

    def _make_id(self, file_path: Path, suffix: str) -> str:
        # 用文件路径 hash + suffix
        # BUG-14 fix: suffix 里可能含 \n/* 等特殊字符 (Java 方法体解析)
        # 这些会破坏 link_id (URL 路径参数). 改 sanitize: 去所有换行 + URL 特殊字符 + 截断
        import hashlib
        import re
        path_hash = hashlib.md5(str(file_path).encode()).hexdigest()[:8]
        suffix_hash = hashlib.md5(suffix.encode()).hexdigest()[:8]
        # 1) 替换所有换行/制表为单空格
        clean = " ".join(suffix.split())
        # 2) 去掉 URL 路径不允许的字符 (* ? \ " < > | 等)
        clean = re.sub(r'[\\/*?"<>|]', '', clean)
        # 3) 多个连续空格压成单空格
        clean = re.sub(r' +', ' ', clean)
        # 4) 截断到 60 字符
        if len(clean) > 60:
            clean = clean[:60]
        # 5) 去尾部非 word 字符
        clean = clean.rstrip("/\\:*?\"<>| \t\n")
        return f"{path_hash}-{clean}-{suffix_hash}"


# ============================================================
# Vue / JS 解析器
# ============================================================

class VueParser:
    """Vue 单文件组件解析 (简化版)"""

    def __init__(self):
        if not JS_AVAILABLE:
            raise ImportError("tree-sitter-javascript 未安装")
        self.parser = Parser(JS_LANGUAGE)

    def parse_file(self, file_path: Path) -> List[CodeChunk]:
        """解析 Vue SFC 或独立 JS/TS 文件。"""
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            logger.warning(f"读取文件失败 {file_path}: {e}")
            return []

        is_vue = file_path.suffix.lower() == ".vue"
        if not is_vue:
            return self._parse_script(content, file_path, 0)

        # 提取 <script> 块
        import re
        script_match = re.search(
            r'<script[^>]*>(.*?)</script>', content, re.DOTALL
        )
        if not script_match:
            return [CodeChunk(
                chunk_id=self._make_id(file_path, "vue:template"),
                file_path=str(file_path),
                file_type="vue",
                language="vue",
                chunk_type="vue_template",
                name=file_path.stem,
                line_start=1,
                line_end=content.count("\n") + 1,
                code_content=content,
            )]

        script_content = script_match.group(1)
        line_offset = content[:script_match.start(1)].count('\n')

        return self._parse_script(script_content, file_path, line_offset)

    def _parse_script(
        self, script_content: str, file_path: Path, line_offset: int
    ) -> List[CodeChunk]:
        tree = self.parser.parse(bytes(script_content, "utf-8"))

        # 找 export default { ... }
        chunks = []
        for export_index, node in enumerate(tree.root_node.children):
            if node.type == "export_statement":
                chunks.append(CodeChunk(
                    chunk_id=self._make_id(
                        file_path, f"javascript:export:{export_index}:{node.start_byte}"
                    ),
                    file_path=str(file_path),
                    file_type=file_path.suffix.lower().lstrip("."),
                    language="vue" if file_path.suffix.lower() == ".vue" else "javascript",
                    chunk_type="vue_component" if file_path.suffix.lower() == ".vue" else "javascript_module",
                    name=file_path.stem,
                    line_start=line_offset + node.start_point[0] + 1,
                    line_end=line_offset + node.end_point[0] + 1,
                    code_content=self._get_text(node, script_content),
                ))

        if not chunks:
            # 整个 script 作为 chunk
            chunks.append(CodeChunk(
                chunk_id=self._make_id(file_path, "vue:script"),
                file_path=str(file_path),
                file_type=file_path.suffix.lower().lstrip("."),
                language="vue" if file_path.suffix.lower() == ".vue" else "javascript",
                chunk_type="vue_script" if file_path.suffix.lower() == ".vue" else "javascript_module",
                name=file_path.stem,
                line_start=1,
                line_end=script_content.count('\n') + 1,
                code_content=script_content,
            ))

        return chunks

    def _get_text(self, node, content: str) -> str:
        # BUG fix: 同Java 版 _get_text, 按字节偏移切片后再解码.
        # 中文源码下用字符偏移直切会得到截断的代码片段.
        try:
            raw = content.encode("utf-8")
            return raw[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
        except Exception:
            return ""

    def _make_id(self, file_path: Path, suffix: str) -> str:
        # BUG-14 fix: 同 JavaParser._make_id, 用正则去所有 URL 特殊字符
        import hashlib
        import re
        path_hash = hashlib.md5(str(file_path).encode()).hexdigest()[:8]
        suffix_hash = hashlib.md5(suffix.encode()).hexdigest()[:8]
        clean = " ".join(suffix.split())
        clean = re.sub(r'[\\/*?"<>|]', '', clean)
        clean = re.sub(r' +', ' ', clean)
        if len(clean) > 60:
            clean = clean[:60]
        clean = clean.rstrip("/\\:*?\"<>| \t\n")
        return f"{path_hash}-{clean}-{suffix_hash}"


# ============================================================
# 统一入口
# ============================================================

class CodeParser:
    """代码解析统一入口"""

    def __init__(self):
        self.java = JavaParser() if JAVA_AVAILABLE else None
        self.vue = VueParser() if JS_AVAILABLE else None

    def parse(self, file_path: Path) -> List[CodeChunk]:
        """根据文件类型分发"""
        suffix = file_path.suffix.lower()

        if suffix == ".java" and self.java:
            return self.java.parse_file(file_path)
        elif suffix == ".vue" and self.vue:
            return self.vue.parse_file(file_path)
        elif suffix in (".js", ".ts") and self.vue:
            return self.vue.parse_file(file_path)
        elif suffix in (".xml", ".yml", ".yaml", ".properties"):
            return self._parse_config(file_path)
        else:
            return []

    @staticmethod
    def _parse_config(file_path: Path) -> List[CodeChunk]:
        """将 XML/YAML/Properties 作为完整文件 chunk 保留，避免配置丢失。"""
        import hashlib

        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            logger.warning(f"读取配置文件失败 {file_path}: {e}")
            return []
        suffix = file_path.suffix.lower().lstrip(".") or "config"
        digest = hashlib.md5(str(file_path).encode()).hexdigest()[:8]
        return [CodeChunk(
            chunk_id=f"{digest}-config:{file_path.stem}",
            file_path=str(file_path),
            file_type=suffix,
            language=suffix,
            chunk_type="config",
            name=file_path.name,
            line_start=1,
            line_end=content.count("\n") + 1,
            code_content=content,
        )]


if __name__ == "__main__":
    from src.logger import setup_logging
    setup_logging()

    parser = CodeParser()

    # 测试: 解析当前项目中的一个 Java 文件
    test_file = Path(
        r"D:\code\yiye-agent-backend\yiye-agent-private\yiye-agent-ucenter-service-private"
        r"\src\main\java\ai\yiye\agent\ucenter\web\UserController.java"
    )

    if test_file.exists():
        chunks = parser.parse(test_file)
        print(f"✅ 解析 {test_file.name}: {len(chunks)} chunks")
        for c in chunks[:5]:
            print(f"   [{c.chunk_type}] {c.name} (lines {c.line_start}-{c.line_end})")
            if c.http_route:
                print(f"      HTTP: {c.http_route}")
            if c.called_methods:
                print(f"      Calls: {c.called_methods[:5]}")
    else:
        print(f"⚠️  测试文件不存在: {test_file}")