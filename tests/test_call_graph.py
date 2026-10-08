# -*- coding: utf-8 -*-
"""
跨文件调用图测试

保护三件事:
  1. 跨文件能连上(原实现同文件限制, 跨文件为 0)
  2. 同名歧义时绝不乱连(连错比不连危害大得多)
  3. 接收者类型能正确消歧
"""

from pathlib import Path

import pytest

from src.call_graph import CallGraphBuilder, RES_EXACT, RES_GUESS, RES_AMBIGUOUS
from src.code_parser import CodeChunk


def _chunk(name, cid=None, ftype="X.java", chunk_type="method",
           called=None, line_start=1, line_end=10, code=""):
    return CodeChunk(
        chunk_id=cid or f"{ftype}:{name}",
        file_path=ftype,
        file_type="java",
        language="java",
        chunk_type=chunk_type,
        name=name,
        qualified_name=name,
        line_start=line_start,
        line_end=line_end,
        code_content=code,
        called_methods=list(called or []),
    )


def _name(cid):
    return cid.split(":")[1] if ":" in cid else cid


# ============================================================
# 跨文件(核心回归)
# ============================================================

def test_cross_file_relation_built():
    """
    回归保护: 原实现只在同文件内匹配(by_file[chunk.file_path]),
    导致跨文件关系为 0。这条用例保证以后不会退回去。
    """
    caller = _chunk("updateStatus", "A.java:caller", "A.java",
                    called=["mapper.save"])
    target = _chunk("save", "B.java:save", "B.java")

    rels = CallGraphBuilder([caller, target]).build()

    assert len(rels) == 1
    assert rels[0].from_chunk_id == "A.java:caller"
    assert rels[0].to_chunk_id == "B.java:save"


def test_global_unique_gets_guess():
    """全库唯一但包路径未验证 -> guess(不是 exact)"""
    caller = _chunk("go", "A.java:go", "A.java", called=["onlyOne"])
    target = _chunk("onlyOne", "B.java:onlyOne", "B.java")

    rels = CallGraphBuilder([caller, target]).build()

    assert len(rels) == 1
    assert rels[0].resolution == RES_GUESS


# ============================================================
# 歧义处理(反幻觉关键)
# ============================================================

def test_ambiguous_same_name_produces_no_edge():
    """
    同名方法有多个候选时, 绝不选一个连上。
    连错会把 A 模块的调用接到 B 模块的表上, 生成错误用例。
    """
    caller = _chunk("go", "A.java:go", "A.java", called=["list"])
    t1 = _chunk("list", "Order.java:list", "Order.java")
    t2 = _chunk("list", "Payment.java:list", "Payment.java")

    rels = CallGraphBuilder([caller, t1, t2]).build()

    assert rels == [], "同名歧义时不应产出任何关系"


def test_ambiguous_even_with_two_in_same_file():
    caller = _chunk("go", "A.java:go", "A.java", called=["list"])
    t1 = _chunk("list", "A.java:list1", "A.java")
    t2 = _chunk("list", "A.java:list2", "A.java")
    # 同名同文件 -> 无法区分
    rels = CallGraphBuilder([caller, t1, t2]).build()
    assert rels == []


# ============================================================
# 字段类型消歧(决定 ambiguous 能否变exact)
# ============================================================

def test_field_type_disambiguates():
    """
    调用 orderService.list() 时, 本类字段声明了 OrderService,
    应消歧到 OrderService 而不是 PaymentService。
    """
    cls_code = "public class AmbiguousCaller { private OrderService orderService; }"
    owner = _chunk("AmbiguousCaller", "C.java:cls", "C.java",
                   chunk_type="class", line_start=1, line_end=2, code=cls_code)
    caller = _chunk("go", "C.java:go", "C.java",
                    called=["orderService.list"], line_start=5, line_end=8)
    t1 = _chunk("list", "Order.java:list", "OrderService.java")
    t2 = _chunk("list", "Payment.java:list", "PaymentService.java")

    cg = CallGraphBuilder([owner, caller, t1, t2])
    rels = cg.build()

    assert len(rels) == 1, "有字段类型时不应歧义"
    assert rels[0].resolution == RES_EXACT
    assert rels[0].to_chunk_id == "Order.java:list"


def test_field_type_extraction():
    code = """
    public class Foo {
        private AgentConfMapper agentConfMapper;
        protected CacheService cacheService;
        public static final String NAME = "x";
    }
    """
    fields = CallGraphBuilder._extract_field_types(code)
    assert fields.get("agentConfMapper") == "AgentConfMapper"
    assert fields.get("cacheService") == "CacheService"
    assert "NAME" not in fields  # 常量不是字段依赖


# ============================================================
# 边界
# ============================================================

def test_self_call_not_linked():
    """自调用不该建边(否则图里出现自环)"""
    c = _chunk("foo", "A.java:foo", "A.java", called=["foo"])
    assert CallGraphBuilder([c]).build() == []


def test_empty_call_safe():
    c = _chunk("foo", "A.java:foo", "A.java", called=[])
    assert CallGraphBuilder([c]).build() == []


def test_unknown_method_yields_nothing():
    c = _chunk("foo", "A.java:foo", "A.java", called=["nothingLikeThis"])
    assert CallGraphBuilder([c]).build() == []


def test_malformed_call_safe():
    """不该因为奇怪的调用串崩溃"""
    for bad in ["....", "a.b.c.d", ".", "x."]:
        c = _chunk("foo", "A.java:foo", "A.java", called=[bad])
        CallGraphBuilder([c]).build()  # 不抛异常即可


def test_non_method_chunks_not_used_as_caller():
    """类 chunk 不应作为调用方(它不是可执行体)"""
    cls = _chunk("C", "A.java:cls", "A.java", chunk_type="class", called=["save"])
    target = _chunk("save", "B.java:save", "B.java")
    assert CallGraphBuilder([cls, target]).build() == []
