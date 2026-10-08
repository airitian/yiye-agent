"""
Path utilities - 跨平台 glob 匹配等共用函数.

v0.5.9: 把 deep_audit.py + kb_build.py 各自维护的 _glob_match 提取到一处.
fnmatch 在 Windows 路径语义下不匹配 \\ 分隔符, ** 必须有前导分隔符才拆开.
本模块统一处理这些跨平台坑.

v0.5.10: 加 secure_file() 跨平台文件权限收紧工具 (P0-5 fix).
"""

import fnmatch
import os
import re
import sys
from pathlib import Path


def glob_match(path: str, pattern: str) -> bool:
    """
    跨平台 glob 匹配 (Windows / POSIX 都对).

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


def secure_file(path, mode: int = 0o600) -> bool:
    """
    收紧文件权限, 防止其他用户读取敏感文件 (.env, 审计日志等).

    P0-5 fix: 之前 .env / .logs/audit/*.jsonl 默认 644 (其他用户可读),
    共享主机场景下任何用户能 cat 出 API key / 审计数据.

    跨平台行为:
    - POSIX (Linux/Mac): 真实生效, 0o600 = owner read+write only.
    - Windows: NTFS 走 ACL 不走 POSIX mode bit, os.chmod 是只读接口;
      调 chmod 不报错也不生效. Windows 上保持原权限 (已经是 NTFS 默认 ACL),
      不抛异常. 共享 Windows 场景的真正防护靠 NTFS ACL 设置.

    Args:
        path: 文件路径 (str / Path / None)
        mode: 目标权限位 (默认 0o600 = owner only)

    Returns:
        True  = 收紧成功 (POSIX) 或 跳过 (Windows)
        False = 失败 (文件不存在 / 权限不足 / 平台不支持)
    """
    if path is None:
        return False
    try:
        p = Path(path)
        if not p.exists():
            return False
        if sys.platform == "win32":
            # NTFS: 走 ACL, 我们的 Python 工具无能为力; 显式跳过不抛异常
            return True
        os.chmod(str(p), mode)
        return True
    except (OSError, PermissionError):
        return False
