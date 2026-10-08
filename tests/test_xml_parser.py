# -*- coding: utf-8 -*-
"""
Mapper XML 解析器测试

重点保护三件事(都是反幻觉的关键):
  1. 主表/join 表抽准, 别名与关键字不误抓
  2. 动态 SQL 必须降级为 low, 不能给成 high
  3. CTE(WITH xxx AS)不能当成数据库表
"""

import pytest

from src.xml_parser import MapperXmlParser, MapperFile, SqlStatement


# ============================================================
# fixtures / 样本
# ============================================================

SIMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE mapper PUBLIC "-//mybatis.org//DTD Mapper 3.0//EN"
 "http://mybatis.org/dtd/mybatis-3-mapper.dtd">
<mapper namespace="ai.yiye.agent.boss.mapper.AgentConfMapper">
    <update id="updateStatusByAgentId">
        update agent_conf
        set status = #{status}
        where agent_id = #{agentId}
    </update>
    <select id="listAgentConfForOrg" resultType="ai.yiye.agent.boss.vo.AgentConfOrgVO">
        select ac.id, ac.agent_id from agent_conf ac
        left join boss_agent_conf_detail bacd on ac.agent_id = bacd.agent_id
    </select>
</mapper>
"""

DYNAMIC = """<?xml version="1.0" encoding="UTF-8"?>
<mapper namespace="ai.yiye.agent.m.AgentConfMapper">
    <select id="listDynamic">
        select * from agent_conf
        <where>
            <if test="status != null">and status = #{status}</if>
        </where>
    </select>
</mapper>
"""

WITH_CTE = """<?xml version="1.0" encoding="UTF-8"?>
<mapper namespace="ai.yiye.agent.m.DepartmentMapper">
    <select id="selectDeptTree">
        WITH RECURSIVE DEPARTMENNTS AS (
            SELECT ID, PARENT_ID FROM UCENTER_DEPARTMENT WHERE ID = #{id}
        )
        SELECT * FROM DEPARTMENNTS
    </select>
</mapper>
"""

DELETE_STMT = """<?xml version="1.0" encoding="UTF-8"?>
<mapper namespace="ai.yiye.agent.m.LogMapper">
    <delete id="purgeOldLogs">
        delete from ucenter_operation_log
        where created_at &lt; #{before}
    </delete>
</mapper>
"""


# ============================================================
# 基本解析
# ============================================================

def test_parses_namespace_and_statements():
    mf = MapperXmlParser().parse_string(SIMPLE, "AgentConfMapper.xml")

    assert mf is not None
    assert mf.namespace == "ai.yiye.agent.boss.mapper.AgentConfMapper"
    assert mf.mapper_name == "AgentConfMapper"
    assert mf.package == "ai.yiye.agent.boss.mapper"
    assert len(mf.statements) == 2


def test_non_mapper_xml_returns_none():
    """普通配置文件不能被误认成 Mapper"""
    parser = MapperXmlParser()
    assert parser.parse_string('<?xml version="1.0"?><settings><a>1</a></settings>') is None
    assert parser.parse_string('{"json": true}') is None


def test_malformed_xml_is_skipped_not_crashed():
    """损坏的 XML 不能让批量索引崩掉"""
    mf = MapperXmlParser().parse_string(
        '<mapper namespace="a.b.C"><select id="x">select 1', "broken.xml")
    assert mf is None


def test_statement_id_matched_to_line_number():
    """证据必须能定位到行号, 否则业务问答给不出出处"""
    content = SIMPLE
    mf = MapperXmlParser().parse_string(content, "AgentConfMapper.xml")
    by_id = {s.stmt_id: s for s in mf.statements}

    for st in mf.statements:
        expected = content[:content.find(f'<{st.stmt_type} id="{st.stmt_id}"')].count("\n") + 1
        assert st.line_start == expected, f"{st.stmt_id} 行号定位不准"

    assert by_id["listAgentConfForOrg"].line_start > by_id["updateStatusByAgentId"].line_start


# ============================================================
# 表名抽取(核心)
# ============================================================

def test_extracts_primary_table():
    mf = MapperXmlParser().parse_string(SIMPLE, "x.xml")
    st = {s.stmt_id: s for s in mf.statements}["updateStatusByAgentId"]

    assert st.tables == ["agent_conf"]
    assert st.table_confidence == "high"
    assert st.is_dynamic is False


def test_extracts_join_table_and_ignores_alias():
    """left join boss_agent_conf_detail bacd 中, 表名是前者, bacd 是别名不能抽"""
    mf = MapperXmlParser().parse_string(SIMPLE, "x.xml")
    st = {s.stmt_id: s for s in mf.statements}["listAgentConfForOrg"]

    assert "boss_agent_conf_detail" in st.tables
    assert "agent_conf" in st.tables
    # 别名不得被当成表
    assert "bacd" not in st.tables
    assert "ac" not in st.tables


def test_delete_statement_primary_table():
    mf = MapperXmlParser().parse_string(DELETE_STMT, "x.xml")
    st = mf.statements[0]

    assert st.stmt_type == "delete"
    assert st.tables == ["ucenter_operation_log"]


def test_extracts_params():
    mf = MapperXmlParser().parse_string(SIMPLE, "x.xml")
    st = {s.stmt_id: s for s in mf.statements}["updateStatusByAgentId"]

    assert "status" in st.params
    assert "agentId" in st.params


# ============================================================
# 反幻觉降级
# ============================================================

def test_dynamic_sql_marked_and_downgraded():
    """含 <if> 的 SQL 表名可能来自动态拼接, 必须标 low 不能标 high"""
    mf = MapperXmlParser().parse_string(DYNAMIC, "x.xml")
    st = mf.statements[0]

    assert st.is_dynamic is True
    assert st.table_confidence == "low"


def test_static_sql_is_high_confidence():
    """对照组: 无动态标签才允许 high"""
    mf = MapperXmlParser().parse_string(SIMPLE, "x.xml")
    st = {s.stmt_id: s for s in mf.statements}["updateStatusByAgentId"]
    assert st.table_confidence == "high"


# ============================================================
# CTE 必须被剔除
# ============================================================

def test_cte_name_not_treated_as_table():
    """WITH DEPARTMENNTS AS (...) 里的名字不是表, 抽了会污染反幻觉白名单"""
    mf = MapperXmlParser().parse_string(WITH_CTE, "x.xml")
    st = mf.statements[0]

    assert "departmennts" not in [t.lower() for t in st.tables]
    assert "data" not in [t.lower() for t in st.tables]


def test_real_table_in_cte_statement_is_kept():
    """CTE 语句里真正的表(UCENTER_DEPARTMENT)不能被误删"""
    mf = MapperXmlParser().parse_string(WITH_CTE, "x.xml")
    tables_lower = [t.lower() for t in mf.statements[0].tables]
    assert "ucenter_department" in tables_lower


# ============================================================
# 表名清洗
# ============================================================

@pytest.mark.parametrize("raw,expect_none", [
    ("${dynamicTable}", True),      # 动态拼接无法静态确定
    ("select", True),                # 关键字
    ("dual", True),                  # 伪表
    ("", True),
    ("1", True),                     # 纯数字
])
def test_dirty_table_names_rejected(raw, expect_none):
    cleaned = MapperXmlParser()._clean_table_name(raw)
    if expect_none:
        assert cleaned is None
    else:
        assert cleaned == raw


def test_backquoted_and_quoted_names_accepted():
    p = MapperXmlParser()
    assert p._clean_table_name("`agent_conf`") == "agent_conf"
    assert p._clean_table_name('"agent_conf"') == "agent_conf"


def test_schema_qualified_name_accepted():
    assert MapperXmlParser()._clean_table_name("public.agent_conf") == "public.agent_conf"


def test_skip_tables_respected():
    p = MapperXmlParser(skip_tables={"secret_table"})
    assert p._clean_table_name("secret_table") is None
    assert p._clean_table_name("agent_conf") == "agent_conf"


# ============================================================
# to_row (写库契约)
# ============================================================

def test_to_row_field_count_matches_ddl():
    """to_row 的值个数必须与 sql_index 表的列数一致, 否则写库直接失败"""
    mf = MapperXmlParser().parse_string(SIMPLE, "x.xml")
    row = mf.statements[0].to_row("customer-service")
    assert len(row) == 13
    # 前两个是 qualified_stmt 与 domain
    assert row[0] == "ai.yiye.agent.boss.mapper.AgentConfMapper.updateStatusByAgentId"
    assert row[1] == "customer-service"


def test_qualified_stmt_joins_namespace_and_id():
    mf = MapperXmlParser().parse_string(SIMPLE, "x.xml")
    st = mf.statements[0]
    assert st.qualified_stmt.endswith(".updateStatusByAgentId")
