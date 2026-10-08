# -*- coding: utf-8 -*-
"""
需求内容去重的回归测试

每一条断言都对应一个真实的踩坑, 不是"测一下函数返回非空"这种
自欺欺人的测试 —— 本项目历史上 7 个 bug 里有 6 个是静默失效,
这类 bug 只能靠"断言它本该有结果"的测试拦住。
"""
import json
from pathlib import Path

import pytest

from src.req_dedup import (
    IDENTITY_KEYS,
    canonicalize,
    content_hash,
    dedupe_requirements,
    is_damaged,
    load_requirement,
    payload_size,
    unescape_layers,
)


def _write(d: Path, name: str, obj) -> Path:
    p = d / name
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return p


BASE = {
    "req_id": "x",
    "title": "客服获客助手异常自动下线",
    "background": "获客助手连接状态异常时下线\n禁止自动上线",
    "main_flow": ["客服上线", "心跳异常", "自动下线"],
    "acceptance": ["状态变更为下线"],
    "actors": ["客服"],
}


# ============================================================
# 规范化
# ============================================================

def test_identity_keys_excluded_from_hash():
    """
    核心断言: 只改 req_id / title, 内容哈希必须不变。

    真实数据 ce001.json 与 1.json 的 background 完全一致, 只差 req_id 和
    title —— 如果把这两个字段算进哈希, 26 份同内容需求一份都合并不掉。
    """
    a = dict(BASE, req_id="ce001", title="获客助手")
    b = dict(BASE, req_id="1", title="1111")
    assert content_hash(a) == content_hash(b)


def test_domain_excluded_from_hash():
    """domain 是归属而非内容, 同一份需求登记到不同域不该算两份"""
    assert content_hash(dict(BASE, domain="payment")) == content_hash(BASE)


def test_real_content_change_changes_hash():
    """反向断言: 业务内容真的变了, 哈希必须变 —— 否则去重就成瞎合并"""
    b = dict(BASE)
    b["background"] = BASE["background"] + "\n历史数据不作处理"
    assert content_hash(b) != content_hash(BASE)


def test_list_order_significant():
    """
    列表顺序本身承载语义(main_flow 是步骤), 不能排序。
    若按集合处理, 「先上线后下线」与「先下线后上线」会被判成同一份需求。
    """
    a = dict(BASE, main_flow=["a", "b"])
    b = dict(BASE, main_flow=["b", "a"])
    assert content_hash(a) != content_hash(b)


def test_whitespace_insensitive():
    """格式化工具插入的换行与缩进不是语义差异"""
    a = dict(BASE, background="获客助手  异常\n下线")
    b = dict(BASE, background="获客助手异常下线")
    assert content_hash(a) == content_hash(b)


def test_dict_key_order_insensitive():
    """key 顺序由格式化工具决定, 不是内容的一部分"""
    a = {"x": 1, "y": 2, "z": 3}
    b = {"z": 3, "y": 2, "x": 1}
    assert content_hash(a) == content_hash(b)


def test_canonicalize_drops_identity_keys_recursively():
    """身份字段要在任意层级都剥掉, 不只是顶层"""
    obj = {"a": {"req_id": "1", "title": "t", "keep": "v"}, "req_id": "2"}
    canon = canonicalize(obj)
    assert "req_id" not in canon
    assert "title" not in canon["a"]
    assert canon["a"]["keep"] == "v"


# ============================================================
# 多层转义还原
# ============================================================

def test_unescape_converges_multilayer():
    """
    6 层转义必须还原干净 —— 只剥一层时同一段文字产生 6 个不同哈希,
    这正是历史上 6/12/24/48/96 层重复 Fact 的根因。
    """
    text = "获客助手\\n下线"
    for _ in range(6):
        text = text.replace("\\n", "\\\\n")
    assert unescape_layers(text) == "获客助手\n下线"


def test_damaged_detected():
    """连续 3 个以上反斜杠 = 数据损坏, 不是内容特征"""
    assert is_damaged("a\\\\\\\\n") is True
    assert is_damaged('{"background": "a\\nb"}') is False


def test_damaged_file_not_adopted(tmp_path):
    """损坏副本即使能 loads 成功也不参与去重 —— 内容已不可信"""
    bad = tmp_path / "bad.json"
    bad.write_text('{"req_id":"b","background":"x\\\\\\\\\\\\n"}', encoding="utf-8")
    good = _write(tmp_path, "good.json", BASE)
    keep, _ = dedupe_requirements(tmp_path)
    names = [p.name for p in keep]
    assert "good.json" in names
    assert "bad.json" not in names


# ============================================================
# 文件级去重
# ============================================================

def test_dedupe_merges_same_content(tmp_path):
    """26 份同内容 -> 1 份。这是本模块存在的唯一理由"""
    for i in range(5):
        _write(tmp_path, f"r{i}.json", dict(BASE, req_id=f"r{i}",
                                            title=f"标题{i}"))
    keep, dup = dedupe_requirements(tmp_path)
    assert len(keep) == 1
    assert len(dup[keep[0].name]) == 4


def test_dedupe_keeps_distinct_content(tmp_path):
    """真不同的需求一份都不能合并"""
    _write(tmp_path, "a.json", BASE)
    _write(tmp_path, "b.json", dict(BASE, background="完全不同的背景规则"))
    _write(tmp_path, "c.json", dict(BASE, main_flow=["只有一步"]))
    keep, dup = dedupe_requirements(tmp_path)
    assert len(keep) == 3
    assert dup == {}


def test_representative_is_cleanest(tmp_path):
    """
    同组内选**原始文本最短**(转义层数最少)的那份。

    这条测试是被一次失败的设计推翻后重写的。原判据是"选信息量最大的",
    但 payload_size 衡量的是规范化后的长度, 而组内哈希相同 ⟹ 规范化结果
    逐字节相同 ⟹ 该值恒等, 完全没有区分力, 等于按文件名字典序取第一个。
    组内真正的差异只有转义层数/缩进, 而转义层数越多, 下游再解析时
    越容易还原失败 —— 该挑最干净的, 挑最脏的是反向操作。
    """
    clean = _write(tmp_path, "zzz.json", BASE)       # 单层转义, 文本最短
    # 双层转义副本: 与 clean 规范化后同内容, 但原始文本更长
    esc = tmp_path / "aaa.json"
    esc.write_text(
        json.dumps(BASE, ensure_ascii=False).replace("\\n", "\\\\n"),
        encoding="utf-8")
    assert esc.read_text(encoding="utf-8") != clean.read_text(encoding="utf-8")

    keep, dup = dedupe_requirements(tmp_path)
    assert [p.name for p in keep] == ["zzz.json"]
    assert dup == {"zzz.json": ["aaa.json"]}


def test_representative_not_by_name_order(tmp_path):
    """
    判别性断言: 字母序最前的那份是**最脏**的, 代表仍不能是它。

    若实现退化成"按文件名取第一个", 这里会选 aaa.json 而失败。
    """
    # aaa.json: 内容同 BASE 但叠了双层转义 -> 原始文本最长(最脏)
    dirty = tmp_path / "aaa.json"
    dirty.write_text(
        json.dumps(BASE, ensure_ascii=False).replace("\\n", "\\\\n"),
        encoding="utf-8")
    # z0.json: 单层转义, 原始文本最短(最干净), 但字母序排最后
    _write(tmp_path, "z0.json", BASE)

    keep, dup = dedupe_requirements(tmp_path)
    assert [p.name for p in keep] == ["z0.json"]
    assert dup == {"z0.json": ["aaa.json"]}


def test_dedupe_deterministic(tmp_path):
    """同输入必须同输出 —— 否则覆盖报告每次跑数字都不一样, 无法审计"""
    for n in ["m.json", "a.json", "z.json"]:
        _write(tmp_path, n, dict(BASE, req_id=n))
    r1 = [p.name for p in dedupe_requirements(tmp_path)[0]]
    r2 = [p.name for p in dedupe_requirements(tmp_path)[0]]
    assert r1 == r2 == ["a.json"]


def test_missing_dir_returns_empty():
    assert dedupe_requirements(None) == ([], {})
    assert dedupe_requirements(Path("/nonexistent/xyz")) == ([], {})


def test_unparsable_file_skipped_not_crash(tmp_path):
    """坏文件跳过而不是让整批失败 —— 否则一个错文件阻塞全部 8 个域构建"""
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    good = _write(tmp_path, "ok.json", BASE)
    keep, _ = dedupe_requirements(tmp_path)
    assert [p.name for p in keep] == [good.name]


def test_load_requirement_returns_none_on_damage(tmp_path):
    p = tmp_path / "d.json"
    p.write_text('{"a": "x\\\\\\\\\\\\n"}', encoding="utf-8")
    assert load_requirement(p) is None


def test_payload_size_is_constant_within_group():
    """
    payload_size 在同一去重组内**恒等** —— 这正是它不能当组内选取依据的原因。

    组内哈希相同 ⟹ 规范化结果逐字节相同。这条测试把这个事实钉死,
    防止后人再拿它做代表选取(曾经的 bug: 排序键恒等, 选取逻辑静默失效)。
    """
    a = dict(BASE, req_id="a", title="很长很长很长很长的标题")
    b = dict(BASE, req_id="b", title="短")
    assert content_hash(a) == content_hash(b)          # 同组
    assert payload_size(a) == payload_size(b)          # 但度量值必然相同


def test_real_requirements_dir_dedupes():
    """
    真实数据回归: requirements/ 下 46 个文件, 去重后必须显著少于原数。
    若哪天有人改坏规范化逻辑, 这个断言会先炸。
    """
    d = Path(__file__).resolve().parents[1] / "requirements"
    if not d.exists():
        pytest.skip("requirements/ 不存在")
    keep, dup = dedupe_requirements(d)
    assert len(keep) > 0
    assert len(keep) < len(list(d.glob("*.json"))), "真实数据一个都没合并, 去重失效"
