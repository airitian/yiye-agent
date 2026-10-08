# -*- coding: utf-8 -*-
"""
中文注释与注解抽取

为什么需要:
  代码里本来就有大量中文注释(实测 551 处), 它们是最贴近业务语义的信号 ——
  「客服异常自动下线」这种业务说法, 在英文标识符里根本找不到, 却常出现在
  注释和 @ApiOperation 注解里。

  但这些中文目前混在 code_content 全文里, FTS5 分词后被英文标识符挤掉,
  业务词召回为 0。本模块把它们抽出来单独成列, 让检索有中文路径。

抽取三类:
  1. 行注释  // 更新客服状态
  2. 块注释  /** 查询客服在线状态 */
  3. 中文注解值 @ApiOperation("客服下线") / @NotNull("客服ID不能为空")
"""

from __future__ import annotations

import re
from typing import List

# 中文字符区间(基本汉字 + 扩展A + 兼容)
_CJK = r"\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff"

# 行注释: // 到行尾。排除 URL 里的 //
_LINE_COMMENT = re.compile(r"^\s*//(?!/)(.*)$", re.M)

# 块注释: /* ... */ 与 /** ... */
_BLOCK_COMMENT = re.compile(r"/\*(.*?)\*/", re.S)

# 注解值: @Xxx("中文") / @Xxx(value = "中文")
_ANNOTATION = re.compile(
    r"@(\w+)\s*\(\s*(?:value\s*=\s*)?\"([^\"]*[" + _CJK + r"][^\"]*)\"", re.U
)

# javadoc 装饰符, 抽完要清掉
_JAVADOC_NOISE = re.compile(r"^\s*[*@/]+\s*", re.M)

# URL /包名等噪声: 不含中文的行直接丢
_HAS_CJK = re.compile(r"[" + _CJK + r"]")


def _clean_lines(text: str, max_len: int = 200) -> List[str]:
    """逐行清洗: 去装饰符、去URL、丢弃无中文的行、截断超长行"""
    out: List[str] = []
    for raw in text.splitlines():
        line = _JAVADOC_NOISE.sub("", raw).strip()
        # URL 整段剥离: 链接本身不是业务语义, 但同行可能还有中文说明,
        # 所以只移除 URL 子串而不是丢弃整行。
        line = re.sub(r"https?://\S+", "", line)
        line = re.sub(r"\s+", " ", line).strip(" \t-—|")
        if not line or not _HAS_CJK.search(line):
            continue
        out.append(line[:max_len])
    return out


def extract_chinese_comments(code: str) -> str:
    """
    从源码片段里抽出所有中文注释与中文注解值, 用换行拼成一个字符串。

    返回空字符串表示没有中文注释 —— 这是正常情况, 不是错误。
    """
    if not code or not _HAS_CJK.search(code):
        return ""

    parts: List[str] = []

    for m in _BLOCK_COMMENT.finditer(code):
        parts.extend(_clean_lines(m.group(1)))

    for m in _LINE_COMMENT.finditer(code):
        parts.extend(_clean_lines(m.group(1)))

    for m in _ANNOTATION.finditer(code):
        val = m.group(2).strip()
        if val and _HAS_CJK.search(val):
            parts.append(f"{m.group(1)}: {val}"[:200])

    if not parts:
        return ""

    # 去重但保序(同一句注释常在块注释里重复出现)
    seen = set()
    uniq: List[str] = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            uniq.append(p)

    return "\n".join(uniq)


def extract_terms_from_comments(comments: str) -> List[str]:
    """
    从中文注释里粗提业务术语, 供 term_alias 自动建表。

    刻意保守: 只取 2-6 字的中文词组, 且必须包含中文且不含标点。
    这批词是"候选", 仍需人工确认 —— 自动抽取的准确率不足以直接信任。
    """
    if not comments:
        return []

    candidates: List[str] = []
    # 按标点与空白切分
    for seg in re.split(r"[\s,，。;；:：、/|()\[\]{}<>\"'`~@#$%^&*+=\-!?]+", comments):
        seg = seg.strip()
        if not _HAS_CJK.search(seg):
            continue
        # 逐个中文连续片段
        for m in re.finditer(r"[" + _CJK + r"]{2,6}", seg):
            term = m.group(0)
            if term not in candidates:
                candidates.append(term)
    return candidates
