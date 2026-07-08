"""
代码解析器 - Tree-sitter 集成

支持 Java 和 Vue (JavaScript) 代码的 AST 解析与 chunk 切分
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

        # 顶层: 找 class_declaration
        for node in tree.root_node.children:
            if node.type == "class_declaration":
                class_chunks = self._parse_class(node, content, file_path)
                chunks.extend(class_chunks)

        return chunks

    def _parse_class(
        self, class_node, content: str, file_path: Path
    ) -> List[CodeChunk]:
        """解析 class 内部"""
        chunks = []
        class_name = self._get_class_name(class_node, content)

        # 提取类注解
        class_annotations = self._extract_annotations(class_node, content)

        # 遍历 class_body
        for child in class_node.children:
            if child.type == "class_body":
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
            chunk_id=self._make_id(file_path, f"class:{class_name}"),
            file_path=str(file_path),
            file_type="java",
            language="java",
            chunk_type="class",
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
            chunk_id=self._make_id(file_path, f"method:{class_name}.{method_name}"),
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
        """提取方法体内的方法调用"""
        calls = []
        self._walk_calls(method_node, calls, content)
        return list(set(calls))[:50]  # 去重, 限制 50 个

    def _walk_calls(self, node, calls: List[str], content: str):
        """递归遍历找 method_invocation"""
        if node.type == "method_invocation":
            # 提取 调用名
            for child in node.children:
                if child.type == "identifier":
                    txt = self._get_text(child, content)
                    if txt:
                        calls.append(txt)
                    break
        if hasattr(node, 'children'):
            for child in node.children:
                self._walk_calls(child, calls, content)

    def _get_text(self, node, content: str) -> str:
        if not content:
            return ""
        try:
            return content[node.start_byte:node.end_byte]
        except Exception:
            return ""

    def _make_id(self, file_path: Path, suffix: str) -> str:
        # 用文件路径 hash + suffix
        import hashlib
        path_hash = hashlib.md5(str(file_path).encode()).hexdigest()[:8]
        return f"{path_hash}-{suffix}"


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
        """解析 .vue 文件"""
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            logger.warning(f"读取文件失败 {file_path}: {e}")
            return []

        # 提取 <script> 块
        import re
        script_match = re.search(
            r'<script[^>]*>(.*?)</script>', content, re.DOTALL
        )
        if not script_match:
            return []

        script_content = script_match.group(1)
        line_offset = content[:script_match.start(1)].count('\n')

        tree = self.parser.parse(bytes(script_content, "utf-8"))

        # 找 export default { ... }
        chunks = []
        for node in tree.root_node.children:
            if node.type == "export_statement":
                chunks.append(CodeChunk(
                    chunk_id=self._make_id(file_path, "vue:default"),
                    file_path=str(file_path),
                    file_type="vue",
                    language="vue",
                    chunk_type="vue_component",
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
                file_type="vue",
                language="vue",
                chunk_type="vue_script",
                name=file_path.stem,
                line_start=1,
                line_end=script_content.count('\n') + 1,
                code_content=script_content,
            ))

        return chunks

    def _get_text(self, node, content: str) -> str:
        try:
            return content[node.start_byte:node.end_byte]
        except Exception:
            return ""

    def _make_id(self, file_path: Path, suffix: str) -> str:
        import hashlib
        path_hash = hashlib.md5(str(file_path).encode()).hexdigest()[:8]
        return f"{path_hash}-{suffix}"


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
        else:
            # 不支持的文件类型, 返回空
            return []


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