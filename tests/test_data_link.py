# -*- coding: utf-8 -*-
"""
Service -> Mapper -> SQL -> 表 链路缝合测试

这些用例全部来自真实项目的踩坑记录, 每条都对应一次真实的连错:

  1. AbstractCrudController.get -> MaterialLibraryMapper(marketing_material_library)
     根因: 泛型基类 AbstractCrudController<S extends IService<T>, T, K>,
           表由运行时泛型实参决定, 静态不可追
  2. CustomerService.updateReadStatusById -> MaterialLibraryMapper
     根因: 该方法调 baseMapper.update(entity, QueryWrapper<Customer>),
           走 MyBatis-Plus 泛型 CRUD
  3. CustomerController.listCustomerByParam -> LandingPageWidgetTemplateMapper
     根因: 只按方法名猜, 没有校验调用方是否引用了该 Mapper

核心原则: **宁可不成链, 不可连错链**。
连错会让用例断言到错误表的字段, 比不连危害大得多。
"""

from pathlib import Path

import pytest

from src.code_parser import CodeChunk
from src.data_link import DataLinkBuilder, DataLink, summarize_tables
from src.xml_parser import MapperXmlParser, MapperFile, SqlStatement


def _chunk(name, ftype="X.java", ct="method", called=None, imports=None,
           code="", cid=None):
    return CodeChunk(
        chunk_id=cid or f"{ftype}#{name}",
        file_path=ftype,
        file_type="java",
        language="java",
        chunk_type=ct,
        name=name,
        qualified_name=name,
        line_start=1,
        line_end=20,
        code_content=code,
        imports=list(imports or []),
        called_methods=list(called or []),
    )


def _stmt(stmt_id, ns, tables, sql="select 1", dynamic=False):
    return SqlStatement(
        stmt_id=stmt_id, stmt_type="select", namespace=ns,
        sql_text=sql, tables=list(tables), is_dynamic=dynamic,
        file_path=f"{ns.rsplit('.', 1)[-1]}.xml", line_start=5,
    )


# ============================================================
# 核心: 泛型 CRUD 不得被缝合
# ============================================================

@pytest.mark.parametrize("crud", [
    "update", "insert", "selectList", "getById", "save", "page",
    "list", "count", "getOne", "removeById",
])
def test_plus_crud_recognized(crud):
    """MyBatis-Plus 泛型 CRUD 方法名必须被识别"""
    assert DataLinkBuilder._is_plus_crud(crud) is True


def test_real_business_method_not_flagged_as_crud():
    """真实业务方法不能被误判成 CRUD, 否则链路会被误杀"""
    for real in ("updateStatusByAgentId", "queryEarliestCustomerTime",
                 "listCustomerByParam", "findWechatMerchantAccount"):
        assert DataLinkBuilder._is_plus_crud(real) is False


def test_generic_base_class_produces_no_link():
    """
    回归: AbstractCrudController.get 曾被缝到 MaterialLibraryMapper。
    根因是泛型基类整类都是泛型转发。
    """
    stmts = [_stmt("get", "a.mapper.MaterialLibraryMapper", ["marketing_material_library"])]
    caller = _chunk("get", "AbstractCrudController.java",
                    called=["service.getById(id)"])
    dlb = DataLinkBuilder([caller])
    dlb.by_stmt_id["get"] = stmts

    assert dlb.build() == [], "泛型基类方法不得产生链路"


def test_plus_crud_call_skipped_even_in_normal_service():
    """
    回归: CustomerService.updateReadStatusById 调 baseMapper.update(...,
    QueryWrapper<Customer>), 表由泛型实体决定, 曾被错缝到 MaterialLibraryMapper。
    """
    stmts = [_stmt("update", "a.mapper.MaterialLibraryMapper", ["marketing_material_library"])]
    caller = _chunk("updateReadStatusById", "CustomerService.java",
                    called=["baseMapper.update(customer, wrapper)",
                            "landingPageService.getById(id)"])
    dlb = DataLinkBuilder([caller])
    dlb.by_stmt_id["update"] = stmts

    assert dlb.build() == [], "baseMapper.update 属泛型 CRUD, 不得缝合"


# ============================================================
# 缝合必须有据
# ============================================================

def test_unique_stmt_id_is_linked():
    """全库唯一的 stmt_id 可以缝(guess)"""
    caller = _chunk("queryEarliestCustomerTime", "SubmitDataService.java",
                    called=["submitDataMapper.queryEarliestCustomerTime()"])
    st = _stmt("queryEarliestCustomerTime", "a.mapper.SubmitDataMapper",
               ["submit_data"])
    dlb = DataLinkBuilder([caller])
    dlb.by_stmt_id["queryEarliestCustomerTime"] = [st]

    links = dlb.build()
    assert len(links) == 1
    assert links[0].tables == ["submit_data"]
    assert links[0].mapper_name == "SubmitDataMapper"


def test_ambiguous_stmt_id_not_linked():
    """同名 stmt_id 出现在多个 Mapper 时, 无直接证据就不缝"""
    caller = _chunk("handle", "SomeJob.java",
                    called=["mapper.handle()"],
                    imports=["a.mapper.OtherMapper"])
    s1 = _stmt("handle", "a.mapper.MaterialLibraryMapper", ["marketing_material_library"])
    s2 = _stmt("handle", "a.mapper.CustomerMapper", ["customer"])
    dlb = DataLinkBuilder([caller])
    dlb.by_stmt_id["handle"] = [s1, s2]

    assert dlb.build() == [], "同名且无引用证据时不得缝合"


def test_caller_import_disambiguates():
    """
    调用方 import 了 CustomerMapper, 应从多个同名候选里选中它。
    这是"有据可依"的正向场景, 不能被误杀。
    """
    caller = _chunk("handle", "CustomerJob.java",
                    called=["customerMapper.handle()"],
                    imports=["a.mapper.CustomerMapper"])
    s1 = _stmt("handle", "a.mapper.MaterialLibraryMapper", ["marketing_material_library"])
    s2 = _stmt("handle", "a.mapper.CustomerMapper", ["customer"])
    dlb = DataLinkBuilder([caller])
    dlb.by_stmt_id["handle"] = [s1, s2]

    links = dlb.build()
    assert len(links) == 1
    assert links[0].tables == ["customer"], "应选中 import 里声明的那个 Mapper"


# ============================================================
# 引用检测
# ============================================================

def test_mapper_refs_from_imports():
    caller = _chunk("x", "A.java",
                    imports=["a.mapper.AgentConfMapper", "java.util.List"])
    refs = DataLinkBuilder._mapper_refs_of(caller)
    assert "agentconfmapper" in refs
    assert "list" not in refs


def test_mapper_refs_from_field_declaration():
    caller = _chunk("x", "A.java",
                    code="public class A { private CustomerMapper customerMapper; }")
    refs = DataLinkBuilder._mapper_refs_of(caller)
    assert "customermapper" in refs


def test_mapper_refs_empty_when_none():
    assert DataLinkBuilder._mapper_refs_of(_chunk("x", "A.java")) == set()


# ============================================================
# 产物字段
# ============================================================

def test_link_exposes_traceable_ref():
    """业务问答要引用出处, 所以链路必须能给出 file:line"""
    caller = _chunk("queryEarliestCustomerTime", "SubmitDataService.java",
                    called=["submitDataMapper.queryEarliestCustomerTime()"])
    st = _stmt("queryEarliestCustomerTime", "a.mapper.SubmitDataMapper",
               ["submit_data"])
    dlb = DataLinkBuilder([caller])
    dlb.by_stmt_id["queryEarliestCustomerTime"] = [st]

    ref = dlb.build()[0].qualified_table_ref
    assert "SubmitDataMapper" in ref
    assert ".xml:5" in ref


def test_summarize_tables_only_high_confidence():
    """低可信度(动态SQL)的表不能进反幻觉白名单"""
    links = [
        DataLink("s1", "m", "f", "M", _stmt("a", "n.M", ["good_table"]), "guess"),
        DataLink("s2", "m", "f", "M", _stmt("b", "n.M", ["risky_table"], dynamic=True), "guess"),
    ]
    tables = summarize_tables(links)
    assert "good_table" in tables
    assert "risky_table" not in tables


def test_empty_candidate_safe():
    dlb = DataLinkBuilder([_chunk("x", "A.java", called=["nothing()"])])
    assert dlb.build() == []
