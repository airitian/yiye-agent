# -*- coding: utf-8 -*-
"""
kb_build 的 Fact 层接入测试

重点:
  1. 需求文件筛选必须按**内容**(转义损坏)而不是文件名 —— 真正的需求
     也可能叫 test001.json, 而损坏副本可能叫 1.json
  2. build_domain 的 fact_sources 不传时必须能正常构建(代码域没需求文档
     不是缺陷)
"""
import inspect
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.kb_build import _req_files


def _write(d: Path, name: str, bg: str) -> None:
    req = {"req_id": Path(name).stem, "title": "t", "background": bg,
           "main_flow": [], "acceptance": [], "constraints": [],
           "edge_cases": [], "actors": []}
    (d / name).write_text(json.dumps(req, ensure_ascii=False),
                         encoding="utf-8")


class TestReqFileFilter:

    def test_excludes_nested_escape_copies(self, tmp_path):
        """
        多层转义副本必须排除。
        实测 requirements/ 下 47 个文件里 11 个是这种损坏副本,
        不排除的话同一规则会被抽成几十条 fact, 覆盖报告分母直接失真。

        注意 _write 里经过 json.dumps 后反斜杠会再翻一倍,
        所以这里传的 \\*N 到文件里是 2N 个字符。阈值是 >=3 即排除,
        因此传 1 个就已经损坏。
        """
        _write(tmp_path, "clean.json", "客服异常自动下线")
        # 单层转义 \n 是**合法**的(实测 1.json 就是这样), 必须保留
        _write(tmp_path, "single.json", "客服异常自动下线" + "\\" + "n第二行")
        _write(tmp_path, "v4.json", "客服异常自动下线" + "\\\\" * 4 + "n第二行")
        got = {p.name for p in _req_files(tmp_path)}
        assert got == {"clean.json", "single.json"}, f"实际收到 {got}"
        assert "v4.json" not in got, "嵌套转义副本必须排除"

    def test_filter_by_content_not_filename(self, tmp_path):
        """
        判据是内容不是文件名:
          名字像测试的(clean 叫 test001.json)应当保留,
          名字像正式的(dirty 叫 1.json)若损坏也应排除。
        """
        _write(tmp_path, "test001.json", "客服异常自动下线")
        _write(tmp_path, "1.json", "客服异常自动下线" + "\\\\" * 6 + "nX")
        got = {p.name for p in _req_files(tmp_path)}
        assert got == {"test001.json"}, got

    def test_missing_dir_returns_empty(self, tmp_path):
        assert _req_files(tmp_path / "nope") == []

    def test_none_returns_empty(self):
        assert _req_files(None) == []

    def test_empty_dir_returns_empty(self, tmp_path):
        assert _req_files(tmp_path) == []

    def test_unreadable_file_skipped(self, tmp_path):
        """损坏到读不出来的文件跳过, 不能让整个构建挂掉"""
        (tmp_path / "broken.json").write_bytes(b"\xff\xfe\x00bad")
        _write(tmp_path, "ok.json", "正常需求")
        got = {p.name for p in _req_files(tmp_path)}
        assert got == {"ok.json"}, got


class TestBuildDomainFactOptional:

    def test_fact_sources_optional(self):
        """
        fact_sources 必须是可选参数。
        代码域没有需求文档是正常情况(比如纯后端服务), 不该因此构建失败。
        """
        import inspect
        from src.kb_build import build_domain
        sig = inspect.signature(build_domain)
        assert sig.parameters["fact_sources"].default is None

    def test_all_domains_accepts_requirements_dir(self):
        from src.kb_build import build_all_domains
        sig = inspect.signature(build_all_domains)
        assert "requirements_dir" in sig.parameters
