# -*- coding: utf-8 -*-
"""
CLI 命令入口的回归测试

## 为什么专门写这个文件

本项目已经因为同一类问题栽了三次:
1. `kb search` 调 `searcher.search()` —— 该方法不存在, CLI 直接崩
2. `build_all_domains` 返回 list, CLI 却按 dict.items() 迭代
3. `kb list` / `kb stats` 引用 `list_domains` / `show_stats` —— **从未实现**

三次都是「用户实际会敲的命令」, 而 280 条测试一条都没覆盖到它们。
凡是 CLI 注册了的命令, 就必须有对应测试 —— 这是本文件的唯一职责。
"""
import inspect
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.fact_case_generator import FactCaseGenerator
from src.fact_extractor import Evidence, FactCandidate, EV_SQL, EV_TABLE
from src.index_builder import IndexBuilder
from src.kb_build import list_domains, show_stats
from src.xml_parser import SqlStatement


def _fact():
    return FactCandidate(
        title="客服异常自动下线", condition="心跳异常", action="自动下线",
        rule_constraint="禁止自动上线", confidence=0.8,
        evidence=[
            Evidence(ev_type="requirement", ref_uri="1.json#background:L1",
                     snippet="异常自动下线", ev_group="g1"),
            Evidence(ev_type=EV_SQL,
                     ref_uri="XxxMapper.updateStatus@XxxMapper.xml:12",
                     snippet="UPDATE agent_conf SET status=?",
                     ev_group="g2"),
            Evidence(ev_type=EV_TABLE, ref_uri="XxxMapper.updateStatus",
                     snippet="agent_conf", ev_group="g3"),
        ])


@pytest.fixture
def kb(tmp_path):
    """造一个最小可用的知识库根目录: 配置 + 一个已建索引的域"""
    cfg = tmp_path / ".config"
    cfg.mkdir(parents=True)
    (cfg / "kb-config.yaml").write_text(
        "version: \"0.1.0\"\n"
        "domains:\n"
        "  customer-service:\n"
        "    description: t\n"
        "    code_sources: []\n"
        "  payment:\n"
        "    description: t\n"
        "    code_sources: []\n",
        encoding="utf-8")

    d = tmp_path / "domains" / "customer-service"
    d.mkdir(parents=True)
    with IndexBuilder(d / "_index.db", "customer-service") as b:
        b.add_sql_statement(SqlStatement(
            stmt_id="updateStatus", stmt_type="update",
            namespace="ai.yiye.dao.XxxMapper",
            sql_text="UPDATE agent_conf SET status=?",
            tables=["agent_conf"], params=["status"],
            file_path="XxxMapper.xml", line_start=12))
        fact = _fact()
        ok, why = b.add_fact(fact)
        assert ok, why
        b.add_cases(FactCaseGenerator().generate([fact]))
        b.commit()
    return tmp_path


# ============================================================
# 接口存在性(第 3 次同类问题的直接防线)
# ============================================================

def test_cli_imported_functions_exist():
    """CLI 引用的函数必须真的存在 —— 这条断言就是为 ImportError 写的"""
    import src.kb_build as m
    for name in ("list_domains", "show_stats", "build_all_domains",
                 "build_domain"):
        assert hasattr(m, name), f"CLI 引用的 {name} 不存在"


def test_cli_kb_subcommands_registered():
    """CLI 注册的 kb 子命令都能被 click 解析出回调函数"""
    from click.testing import CliRunner
    sys.path.insert(0, str(ROOT / "bin"))
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_cli", ROOT / "bin" / "testcase.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    names = set(mod.kb.commands.keys())
    assert {"list", "stats", "build", "search"} <= names, names


# ============================================================
# list_domains
# ============================================================

def test_list_domains_from_config(kb):
    """列的是配置里定义的域, 不是仅已建索引的域"""
    got = list_domains(project_root=kb)
    assert got == ["customer-service", "payment"]


def test_list_domains_missing_config(tmp_path):
    """配置不存在时返回空列表, 不能抛异常"""
    assert list_domains(project_root=tmp_path) == []


def test_list_domains_bad_config(tmp_path):
    """配置损坏时不能崩 —— CLI 报错也要给个能读的说法"""
    cfg = tmp_path / ".config"
    cfg.mkdir(parents=True)
    (cfg / "kb-config.yaml").write_text("::: not yaml :::", encoding="utf-8")
    assert list_domains(project_root=tmp_path) == []


def test_real_project_domains_found():
    """真实项目配置能被解析出全部业务域"""
    domains = list_domains(project_root=ROOT)
    assert "customer-service" in domains
    assert "cross" in domains
    assert len(domains) >= 7


# ============================================================
# show_stats
# ============================================================

def test_show_stats_reads_live_db(kb):
    """
    统计必须直接查库, 不能读 index_meta 快照。

    Fact 层可以在不重建符号索引的情况下反复回填, meta 早就过期了 ——
    读 meta 会让用户看到"事实=0"而库里明明有 1 条。
    """
    st = show_stats(project_root=kb, domain="customer-service")
    assert st["facts"] == 1
    assert st["cases"] > 0
    assert st["fact_covered"] == 1
    assert st["coverage_rate"] == 100.0


def test_show_stats_dimension_dist(kb):
    st = show_stats(project_root=kb, domain="customer-service")
    assert st["dimension_dist"], "维度分布不能为空"
    assert sum(st["dimension_dist"].values()) == st["cases"]


def test_show_stats_sql_tables(kb):
    st = show_stats(project_root=kb, domain="customer-service")
    assert st["sql_tables"] >= 1


def test_show_stats_missing_domain(kb):
    """域不存在时返回空字典而不是抛异常 —— CLI 要能给出友好提示"""
    assert show_stats(project_root=kb, domain="no-such") == {}


def test_show_stats_counts_match_direct_query(kb):
    """交叉校验: 每个数字都要与直接 SQL 一致"""
    st = show_stats(project_root=kb, domain="customer-service")
    conn = sqlite3.connect(str(kb / "domains" / "customer-service" / "_index.db"))
    for key, table in (("facts", "facts"), ("cases", "cases"),
                       ("symbols", "symbols"), ("sql_statements", "sql_index"),
                       ("relations", "relations")):
        direct = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        assert st[key] == direct, f"{key} 与直接查询不一致"
    conn.close()


def test_show_stats_all_real_domains():
    """8 个域的 stats 都能跑通且数字非负 —— 防止某一个域的数据把命令搞崩"""
    for d in sorted(p.parent.name
                    for p in (ROOT / "domains").glob("*/_index.db")):
        if d == "requirements":
            continue
        st = show_stats(project_root=ROOT, domain=d)
        assert st, f"域 {d} 无统计结果"
        for k, v in st.items():
            if isinstance(v, int):
                assert v >= 0, f"域 {d} 的 {k} 为负: {v}"


# ============================================================
# build 参数
# ============================================================

def test_build_kb_build_has_llm_and_facts_options():
    """
    `--no-llm` / `--no-facts` 必须真的注册到命令上。

    注意 `kb_build` 是已被 @click.command 装饰过的 Command 对象,
    选项挂在 `.params` 上(不是 `__click_params__` —— 那是装饰前的形态)。
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_cli2", ROOT / "bin" / "testcase.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    params = {p.name for p in mod.kb_build.params}
    assert "llm" in params, "--llm/--no-llm 未注册"
    assert "facts" in params, "--facts/--no-facts 未注册"
