# -*- coding: utf-8 -*-
"""
需求文档内容去重

## 为什么必须做

实测 `requirements/` 下有 46 个 json, 但按**内容**分组后只有 8 组,
其中一组 26 个文件(`1.json` / `ce001` / `测试这个.json` / `test002` ...)
承载的是同一份「客服获客助手异常自动下线」需求 —— 只是 req_id 与 title
被随手改过。

不���重的后果不是"文件多了点", 而是**统计口径直接失真**:
  - 同一批业务规则被抽成 N 遍, 事实表里出现大量重复 Fact
  - 覆盖报告的分母 / 分子都被灌水, 「业务规则覆盖 100%」变成自欺欺人
  - 生成出来的用例里同一个约束重复几十遍, 评审的人会以为测得很充分

## 去重键怎么算(这一步的每个取舍都有代价)

1. **排除身份字段**(`req_id` / `id` / `title` / `domain` / 时间戳 ...):
   实测 `ce001.json` 的 req_id 是 "ce001"、title 是 "获客助手",
   `1.json` 的 req_id 是 "1"、title 是 "1111", 但 background 完全一致。
   **只要把 req_id 算进哈希, 这 26 份就一个都合并不掉。**
   标题同理 —— title 是人随手填的, 不承载业务语义。

2. **还原多层转义后再算**: 需求 JSON 反复导入导出会产生
   6/12/24/48/96 层反斜杠变体(`\\n` vs `\\\\n`)。不还原的话,
   同一段文字在不同文件里是不同字节序列, 哈希必然不同。

3. **剥掉所有空白**: JSON 里 `"获客助手"` 与 `"获客助手 "` 语义相同,
   但格式化工具会插入换行和缩进。空白差异不该算作内容差异。

4. **只按内容分组, 不按文件名**: 文件名完全不可靠 ——
   真正的需求也可能叫 `debug-test.json`, 而调试残留反而叫 `REQ-CS-001.json`。

## 选谁做代表

同组内选**原始文本最短**的那份。

先说一个被推翻的直觉: "选字段最丰富/信息量最大的那份"。
这个指标叫 payload_size, 但它衡量的是**规范化后**的 payload ——
而组内文件的内容哈希相同, 就意味着规范化结果逐字节相同,
payload_size 在组内是**恒等**的, 一点区分力都没有。
拿它排序等于没排序, 最后还是取了文件名字典序第一的那个。

组内真正存在的差异只有三处: 转义层数、格式化缩进、以及被排除的
身份字段长度。其中**只有转义层数有风险** —— 下游 Fact 抽取还要再解析
一遍原始文本, 层数越多越容易在某一层还原失败。所以要挑最干净的,
挑最长的等于专门挑最脏的副本。度量见 raw_weight。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.logger import get_logger

logger = get_logger(__name__)


# ============================================================
# 规范化
# ============================================================

# 身份/环境字段: 不承载业务语义, 必须排除出哈希。
# 注意 domain 也在列 —— 同一份需求可能被登记到不同业务域下,
# 那是"归属"问题不是"内容"问题, 不该因此被判成两份需求。
IDENTITY_KEYS = frozenset({
    "req_id", "id", "_id", "uuid", "request_id", "title", "name",
    "domain", "module", "owner", "author",
    "created_at", "updated_at", "createdat", "updatedat",
    "timestamp", "time", "date", "version", "rev",
    "review_gates", "gates", "actors", "reviewer",
})


def unescape_layers(raw: str, max_passes: int = 8) -> str:
    """
    循环还原多层反斜杠转义, 直到收敛。

    单次 replace 只认两字符, 对 6/12/24 层转义无效 ——
    实测同一段 background 在不同文件里被还原成 6 个不同 fact_id 的重复事实,
    根因就是这里只剥了一层。

    **不能用 replace("\\\\\\\\", "\\\\") 来折叠孤立反斜杠**: 那一步会把
    "还没轮到 n 的反斜杠" 也对折掉。6 反斜杠 + n 的序列先折成 3 反斜杠 + n,
    再对折成 2 反斜杠 + 换行, 剩下的 2 个反斜杠永远配不上 n, 循环提前收敛
    并留下垃圾。正确做法是**先按"任意个反斜杠 + n"整体匹配成换行**,
    折叠放到最后单独做。
    """
    s = raw
    for _ in range(max_passes):
        new = re.sub(r"\\+n", "\n", s)
        if new == s:
            break
        s = new
    # 换行处理完后再折叠残留的孤立反斜杠对(此时不影响 n 的配对)
    return re.sub(r"\\{2,}", r"\\", s)


def canonicalize(obj: Any, drop_keys: frozenset = IDENTITY_KEYS) -> Any:
    """
    递归规范化需求对象, 用于计算内容哈希。

    - dict: 丢身份字段 + 按 key 排序(key 顺序是格式化工具的自由, 不是语义)
    - list: 保持原顺序(列表顺序本身可能承载语义, 如 main_flow 的步骤)
    - str: 还原转义 + 剥掉全部空白
    - 其他: 原样
    """
    if isinstance(obj, dict):
        return {
            k: canonicalize(v, drop_keys)
            for k, v in sorted(obj.items())
            if k.lower() not in drop_keys
        }
    if isinstance(obj, (list, tuple)):
        return [canonicalize(v, drop_keys) for v in obj]
    if isinstance(obj, str):
        return re.sub(r"\s+", "", unescape_layers(obj))
    return obj


def content_hash(obj: Any, drop_keys: frozenset = IDENTITY_KEYS) -> str:
    """需求对象的规范化内容哈希(16 位足够, 且短到能塞进日志)"""
    canon = canonicalize(obj, drop_keys)
    blob = json.dumps(canon, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def payload_size(obj: Any, drop_keys: frozenset = IDENTITY_KEYS) -> int:
    """
    规范化后 payload 的字符数。

    **注意: 这个量在同一去重组内是恒等的** —— 组内文件哈希相同就意味着
    规范化结果逐字节相同。它只能用于比较*不同*需求的信息量,
    不能用来在组内挑代表(曾经的 bug: 拿它当代表选取依据, 结果组内
    所有候选值都一样, "选信息量最大" 根本没生效, 只是碰巧取了排序第一个)。
    组内代表选取用的是 raw_weight。
    """
    return len(json.dumps(canonicalize(obj, drop_keys), ensure_ascii=False))


def raw_weight(path: Path) -> int:
    """
    原始文件文本长度 —— 用作**同组内挑代表**的度量。

    组内文件的内容完全一致, 差异只可能来自: 转义层数、格式化缩进、
    以及被排除的身份字段的长短。其中**转义层数是唯一有风险的那个** ——
    下游 Fact 抽取还要再解析一遍原始文本, 层数越多越容易在某一层
    还原失败, 历史上 6/12/24/48/96 层的重复 Fact 就是这么来的。

    所以组内应当挑**原始文本最短**的那份 = 转义层数最少 = 最干净。
    挑最长的那份等于专门去挑最脏的副本, 是反向操作。
    """
    try:
        return len(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return 10 ** 9


# ============================================================
# 文件级去重
# ============================================================

# 连续 3 个以上反斜杠 = 转义没剥干净 = 数据损坏副本。
# 这类文件即使能 json.loads 成功, 内容也已经不可信, 直接不参与去重。
_DAMAGED_RE = re.compile(r"\\{3,}")


def is_damaged(raw: str) -> bool:
    """多层转义残留判定 —— 这是数据损坏信号, 不是内容特征"""
    return bool(_DAMAGED_RE.search(raw))


def load_requirement(path: Path) -> Optional[Any]:
    """读需求文件; 损坏或不可解析时返回 None(而不是抛异常中断整批)"""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        logger.debug("需求文件读取失败 %s: %s", path.name, e)
        return None
    if is_damaged(raw):
        logger.debug("跳过转义损坏的需求副本: %s", path.name)
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        logger.debug("需求文件解析失败 %s: %s", path.name, e)
        return None


def dedupe_requirements(
    requirements_dir: Optional[Path],
) -> Tuple[List[Path], Dict[str, List[str]]]:
    """
    按内容哈希去重需求文件。

    Returns:
        (代表文件列表, {代表文件名: [被合并掉的文件名, ...]})

    代表文件选取规则: **同组内规范化 payload 最大的那份**。
    平手时按文件名排序取首个, 保证结果可复现(不依赖文件系统返回顺序)。
    """
    if not requirements_dir or not Path(requirements_dir).exists():
        return [], {}

    base = Path(requirements_dir)
    # {hash: [(raw_weight, name, path), ...]}
    groups: Dict[str, List[Tuple[int, str, Path]]] = {}

    for f in sorted(base.glob("*.json")):
        obj = load_requirement(f)
        if obj is None:
            continue
        if not isinstance(obj, (dict, list)):
            # 标量需求文件没有可去重的结构, 但也不是损坏 —— 仍然采用
            groups.setdefault(f"raw:{f.stem}", []).append((raw_weight(f),
                                                           f.name, f))
            continue
        h = content_hash(obj)
        groups.setdefault(h, []).append((raw_weight(f), f.name, f))

    keep: List[Path] = []
    dup_map: Dict[str, List[str]] = {}
    for h, items in groups.items():
        # 原始文本最短者优先(= 转义层数最少, 最干净);
        # 平手按文件名, 保证结果可复现(不依赖文件系统返回顺序)
        items.sort(key=lambda t: (t[0], t[1]))
        keep.append(items[0][2])
        others = [n for _, n, _ in items[1:]]
        if others:
            dup_map[items[0][1]] = others

    keep.sort(key=lambda p: p.name)
    total = sum(len(v) for v in groups.values())
    if dup_map:
        logger.info(
            "Fact 层: 需求文档 %d 份 -> 去重后 %d 份 (合并掉 %d 份同内容副本)",
            total, len(keep), total - len(keep),
        )
    else:
        logger.info("Fact 层: 采用 %d 份需求文档(无重复内容)", len(keep))
    return keep, dup_map


def dedupe_report(dup_map: Dict[str, List[str]]) -> str:
    """人类可读的去重报告, 供构建日志与提交说明引用"""
    if not dup_map:
        return "无重复需求"
    lines = []
    for keep, dups in sorted(dup_map.items()):
        lines.append(f"{keep}  <-  {', '.join(sorted(dups))}")
    return "\n".join(lines)
