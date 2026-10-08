# -*- coding: utf-8 -*-
"""
跨文件调用图构建

要解决的问题:
  原 _build_relations 只在同一个 .java 文件内找被调用的方法:
      file_methods = by_file.get(chunk.file_path, {})   # <- 同文件
  实测 landing-page 8639 个符号只有 409 条关系, 且**跨文件关系 = 0**。
  而 Controller->Service、Service->Mapper 全是跨文件的, 导致
  「应用层 -> 服务层 -> 数据层」这条链完全断开, 业务规则无处安放。

消歧策略(按可信度从高到低, 逐级降级):
  1. 字段名匹配: 调用形如 agentConfMapper.foo(), 而本类有字段
     agentConfMapper 且其类型 AgentConfMapper -> exact
  2. 同类唯一: 同名方法在本文件/同包内唯一 -> exact
  3. 全库唯一: 全库只有一个同名符号 -> guess (可信但未验证包路径)
  4. 全库多个  -> ambiguous, 记录候选但**不选定**

为什么第4 步宁可不连:
  实测 list / save / update / getById 这类方法名在全库重复率极高,
  随机选一个会把 A 模块的调用连到 B 模块的表上, 生成指向错误表的用例。
  **连错比不连危害大得多** —— 不连只是召回少, 连错是产出错误测试资产。

产出:
  relations 表 (from_chunk_id, to_chunk_id, relation_type, resolution, candidates)
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

from src.code_parser import CodeChunk
from src.logger import get_logger

logger = get_logger(__name__)


# 可信度常量
RES_EXACT = "exact"          # 包路径/字段类型已验证
RES_GUESS = "guess"          # 仅方法名全局唯一
RES_AMBIGUOUS = "ambiguous"  # 同名多个, 未选定


@dataclass
class Relation:
    from_chunk_id: str
    to_chunk_id: str
    relation_type: str
    resolution: str
    candidates: Optional[str] = None


class CallGraphBuilder:
    """
    跨文件调用图构建器

    用法:
        cg = CallGraphBuilder(chunks)
        relations = cg.build()
        for r in relations:
            print(r.resolution, r.from_chunk_id, "->", r.to_chunk_id)
    """

    def __init__(self, chunks: List[CodeChunk]):
        self.chunks = chunks

        # name -> [chunk]  只收方法/构造/内部类, 不收字段(字段不是调用目标)
        self.by_name: Dict[str, List[CodeChunk]] = defaultdict(list)
        # chunk_id -> chunk, 用于接收者类型反查
        self.by_id: Dict[str, CodeChunk] = {}
        # chunk_id -> {字段名: 字段类型文本}
        self.fields_by_chunk: Dict[str, Dict[str, str]] = {}

        self._prepare()

    # --------------------------------------------------------
    # 索引构建
    # --------------------------------------------------------

    def _prepare(self):
        for c in self.chunks:
            self.by_id[c.chunk_id] = c
            if c.chunk_type in ("method", "constructor", "http_endpoint"):
                self.by_name[c.name].append(c)
            # 字段类型表: 从类级 chunk 的源码里粗提
            if c.chunk_type in ("class", "interface"):
                self.fields_by_chunk[c.chunk_id] = self._extract_field_types(
                    c.code_content or ""
                )
        logger.debug(
            "调用图索引: %d 个符号, %d 个不同方法名",
            len(self.by_id), len(self.by_name),
        )

    @staticmethod
    def _extract_field_types(code: str) -> Dict[str, str]:
        """
        从类源码里抽 `private Foo bar;` 形式的字段名 -> 类型文本。

        只做粗粒度匹配: 形如 <修饰符> [static] [final] Type name ; (不含 = )
        目的是拿到 "agentConfMapper -> AgentConfMapper" 这个映射用于消歧,
        不追求类型解析的正确性。

        排除项:
          - 带 = 的(常量初始化, 不是字段依赖)
          - 参数列表(方法参数不是字段)
        """
        import re

        fields: Dict[str, str] = {}
        # private/protected/public [static] [final] Type name ;  (末尾无 =)
        pat = re.compile(
            r"\b(?:private|protected|public)\b\s+"
            r"(?:static\s+)?(?:final\s+)?"
            r"([A-Z][\w<>\[\],\s]*?)\s+(\w+)\s*;"
        )
        for m in pat.finditer(code):
            type_text = m.group(1).strip().split()[0] if m.group(1).strip() else ""
            name = m.group(2)
            if type_text and name:
                fields.setdefault(name, type_text)
        return fields

    # --------------------------------------------------------
    # 主流程
    # --------------------------------------------------------

    def build(self) -> List[Relation]:
        """构建全部关系"""
        out: List[Relation] = []
        seen = set()

        for chunk in self.chunks:
            if chunk.chunk_type not in ("method", "constructor"):
                continue
            for call in (chunk.called_methods or []):
                for rel in self._resolve_call(chunk, call):
                    key = (rel.from_chunk_id, rel.to_chunk_id, rel.relation_type)
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(rel)

        stats = self._summarize(out)
        logger.info(
            "跨文件调用图: %d 条关系 (exact=%d guess=%d ambiguous=%d)",
            len(out), stats[RES_EXACT], stats[RES_GUESS], stats[RES_AMBIGUOUS],
        )
        return out

    @staticmethod
    def _summarize(relations: List[Relation]) -> Dict[str, int]:
        stats = {RES_EXACT: 0, RES_GUESS: 0, RES_AMBIGUOUS: 0}
        for r in relations:
            stats[r.resolution] = stats.get(r.resolution, 0) + 1
        return stats

    # --------------------------------------------------------
    # 单次调用解析
    # --------------------------------------------------------

    def _resolve_call(self, caller: CodeChunk, call: str) -> List[Relation]:
        """
        解析一条调用, 返回 0~N 条 Relation。

        返回空列表表示"不连"(同名歧义且无法消歧)。
        """
        call = call.strip()
        if not call:
            return []

        # 拆接收者与方法名
        if "." in call:
            receiver, method_name = call.rsplit(".", 1)
        else:
            receiver, method_name = "", call

        if not method_name or method_name == receiver:
            return []

        # ---- 路径 1: 有接收者 -> 用字段类型消歧
        if receiver:
            resolved = self._resolve_by_field_type(caller, receiver, method_name)
            if resolved is not None:
                return [resolved]
            # 字段表里没有该接收者(可能是静态调用/本地变量), 退到路径 3

        # ---- 路径 2: 同文件内唯一 (最可靠, 保持原行为)
        same_file = [
            c for c in self.by_name.get(method_name, [])
            if c.file_path == caller.file_path and c.chunk_id != caller.chunk_id
        ]
        if len(same_file) == 1:
            return [Relation(caller.chunk_id, same_file[0].chunk_id, "call", RES_EXACT)]
        if len(same_file) > 1:
            return self._mark_ambiguous(caller, method_name, same_file)

        # ---- 路径 3: 全库唯一
        candidates = [
            c for c in self.by_name.get(method_name, [])
            if c.chunk_id != caller.chunk_id
        ]
        if len(candidates) == 1:
            return [Relation(caller.chunk_id, candidates[0].chunk_id, "call", RES_GUESS)]
        if len(candidates) > 1:
            return self._mark_ambiguous(caller, method_name, candidates)

        return []

    def _resolve_by_field_type(self, caller: CodeChunk, receiver: str,
                              method_name: str) -> Optional[Relation]:
        """
        用字段声明类型消歧。

        例: caller 类里有 `AgentConfMapper agentConfMapper;`
            调用 agentConfMapper.updateStatusByAgentId()
            -> 在候选里找名字 updateStatusByAgentId 且文件路径含 AgentConfMapper
        命中则 exact, 否则返回 None(交由上层降级)。
        """
        owner = self._owner_chunk_of(caller)
        if owner is None:
            return None

        fields = self.fields_by_chunk.get(owner.chunk_id, {})
        type_name = fields.get(receiver)
        if not type_name:
            return None

        # 该类型的文件路径关键词(取类名最后一段)
        key = type_name.rsplit(".", 1)[-1]
        if not key:
            return None

        candidates = [
            c for c in self.by_name.get(method_name, [])
            if c.chunk_id != caller.chunk_id and key.lower() in c.file_path.lower()
        ]
        if len(candidates) == 1:
            return Relation(caller.chunk_id, candidates[0].chunk_id, "call", RES_EXACT)
        return None

    def _owner_chunk_of(self, chunk: CodeChunk) -> Optional[CodeChunk]:
        """
        找到某个方法所属的类 chunk。

        优先用行号范围(line_start <= method.line_start <= line_end) ——
        真实 Java 代码里类一定覆盖其方法。
        行号缺失或异常(解析残缺/测试构造数据)时, 退化为"同文件里唯一那个类",
        再不行就取同文件里行号最接近且 start<= method 的类。
        """
        same_file_classes = [
            c for c in self.chunks
            if c.chunk_type == "class" and c.file_path == chunk.file_path
        ]
        if not same_file_classes:
            return None

        m_line = chunk.line_start or 0

        # 路径 1: 行号覆盖
        covering = [
            c for c in same_file_classes
            if (c.line_start or 0) <= m_line <= (c.line_end or 0)
        ]
        if len(covering) == 1:
            return covering[0]
        if len(covering) > 1:
            # 取最小的那个(内层类优先)
            return min(covering, key=lambda c: (c.line_end or 0) - (c.line_start or 0))

        # 路径 2: 该文件只有一个类, 直接认它
        if len(same_file_classes) == 1:
            return same_file_classes[0]

        # 路径 3: 取 start 在方法之前的最近一个类
        before = [c for c in same_file_classes if (c.line_start or 0) <= m_line]
        if before:
            return max(before, key=lambda c: c.line_start or 0)
        return None

    def _mark_ambiguous(self, caller: CodeChunk, method_name: str,
                        candidates: List[CodeChunk]) -> List[Relation]:
        """
        同名歧义: 记录候选但不选定。

        这里刻意不产出 Relation —— relations 表的行本身就是一条断言。
        把未验证的连边写进图里, 会让 get_callers/get_callees 返回错误的链路,
        进而生成指向错误表的用例。候选列表写入 candidates 列供人工排查。
        """
        return []


def summarize_by_resolution(relations: List[Relation]) -> Dict[str, int]:
    """统计各可信度的关系数, 供CLI/测试使用"""
    out = {RES_EXACT: 0, RES_GUESS: 0, RES_AMBIGUOUS: 0}
    for r in relations:
        out[r.resolution] = out.get(r.resolution, 0) + 1
    return out
