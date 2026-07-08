"""
Path utilities - 跨平台 glob 匹配等共用函数.

v0.5.9: 把 deep_audit.py + kb_build.py 各自维护的 _glob_match 提取到一处.
fnmatch 在 Windows 路径语义下不匹配 \\ 分隔符, ** 必须有前导分隔符才拆开.
本模块统一处理这些跨平台坑.
"""

import fnmatch
import re


def glob_match(path: str, pattern: str) -> bool:
    """跨平台 glob 匹配 (Windows / POSIX 都对).

    参数:
        path:   文件相对路径 (跨平台可能含 \\ 或 /)
        pattern: glob 模式 (例如 ``**/test/**/*.java``, ``*.py``)

    返回:
        True 表示匹配, False 表示不匹配.

    适配:
        1. path 中的 \\ 替换为 / (Windows 兼容)
        2. 去掉 path 可能的 Windows 盘符 (D:, E:)
        3. 检查 path 是否以 / 开头; 如果不是, 在 path 前加 / 重试 (兼容 ** 不拆开)
        4. 同步测试 "带前导 /" 和 "不带前导 /" 两种匹配方式.
    """
    normalized = path.replace("\\", "/")
    # 去 Windows 盘符
    normalized = re.sub(r"^[A-Za-z]:/", "/", normalized)

    candidates = [normalized]
    if not normalized.startswith("/"):
        candidates.append("/" + normalized)

    for cand in candidates:
        if fnmatch.fnmatch(cand, pattern):
            return True
    return False


# 兼容历史别名 (deep_audit.py 和旧 kb_build.py 都用过这个名字)
def _glob_match(path: str, pattern: str) -> bool:
    """DEPRECATED: 请用 glob_match. 保留仅用于向后兼容."""
    return glob_match(path, pattern)
