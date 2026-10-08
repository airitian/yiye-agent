"""
JSON utilities - LLM 输出解析的健壮工具.

P0-3 fix: 之前 generate.py 用 ``re.search(r'\\{.*\\}', response, re.DOTALL)``
贪婪匹配, LLM 输出含多个 JSON 块 (举例 + 真值) 时会把多块缝一起,
``json.loads`` 抛 JSONDecodeError → except 吞掉 → fallback 默认值静默成功.

修法: 用 stdlib 的 ``json.JSONDecoder().raw_decode()``, 它从任意位置开始
解析**一个**完整的 JSON 值 (object/array/scalar), 返回 (value, end_pos).
遇到前缀非空白非 JSON 起始字符直接抛 StopIteration.

跨 stage 共用, 不依赖 generate.py 的私有状态.
"""

import json
from typing import Any, Optional, Tuple


def extract_json(text: str) -> Optional[Any]:
    """
    从文本中提取**第一个**完整 JSON 值 (object / array / scalar).

    跳过前导空白; 找到第一个 ``[`` ``{`` 或 scalar, 解析到对应闭合位置.

    Args:
        text: 原始文本, 可含前言/后语/围栏/多 JSON 块.

    Returns:
        解析得到的 Python 对象 (dict / list / str / int / float / bool / None).
        找不到合法 JSON 时返回 None (不抛异常).

    Examples:
        >>> extract_json("prefix {\\"a\\":1} suffix")
        {'a': 1}
        >>> extract_json("[1, 2, 3] trailing")
        [1, 2, 3]
        >>> extract_json("hello world")
        None
        >>> extract_json('```json\\n{"k": "v"}\\n```')
        {'k': 'v'}
    """
    if not text:
        return None
    decoder = json.JSONDecoder()
    # 扫描可能的 JSON 起始位置: 跳过前导空白, 找到 { [ " ' digit - + (true/false/null)
    i = 0
    n = len(text)
    while i < n:
        # 跳过空白
        while i < n and text[i] in " \t\n\r":
            i += 1
        if i >= n:
            return None
        ch = text[i]
        # JSON 起始符
        if ch in "{[" or ch in "\"'-0123456789tfn":
            try:
                value, _end = decoder.raw_decode(text, i)
                return value
            except json.JSONDecodeError:
                # 这个起始位置不是合法 JSON, 继续往后找
                i += 1
                continue
        # 跳过非 JSON 字符 (前言文字等)
        i += 1
    return None


def extract_json_strict(text: str) -> Tuple[Optional[Any], Optional[str]]:
    """
    extract_json 的"带错误信息"版本. 用于 caller 想打 warning 时.

    Returns:
        (value, None)  成功
        (None, reason) 失败, reason 是简短原因
    """
    if not text or not text.strip():
        return None, "empty"
    decoder = json.JSONDecoder()
    stripped = text.lstrip()
    leading_ws = len(text) - len(stripped)
    try:
        value, _end = decoder.raw_decode(stripped, 0)
        return value, None
    except json.JSONDecodeError as e:
        return None, f"json decode error at pos {e.pos}: {e.msg}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
