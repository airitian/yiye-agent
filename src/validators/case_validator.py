"""case_validator.py - v0.5 反幻觉框架 (骨架版本).

设计目标:
- 让 stage 5 生成的每条用例, 在写入 cases.json 之前/之后做字段验证
- 检测 LLM 幻觉: 用例如提到的字段/方法/枚举 是否在召回 chunks 里出现

骨架阶段 (本版本):
- API 已定义, 函数签名已设计
- 函数返回空 dict (无幻觉检测)
- 等下一版 (B 阶段) 实装:
  * 字段名 fuzzy 匹配 (snake_case ↔ camelCase)
  * 方法签名反查 (chunk 里有这个 method?)
  * 枚举值校验 (AgentStatusEnum.ACTIVE 等)
  * 占位符检测 (TODO 标记)

调用位置:
- src/generate.py 的 stage5_generate_cases 调用 case_validator.validate_cases
- 如果有 warnings, 写 cases.json 时同时写 validation.json

Author: yiye-agent v0.5 framework reservation
"""

from __future__ import annotations
from pathlib import Path
from typing import Any, Dict, List, Optional
import json
import re
import logging

logger = logging.getLogger(__name__)


# ============================================================
# 类型定义 (供下版本实现)
# ============================================================


class ValidationIssue:
    """单个用例的验证问题.

    Attributes:
        case_id: 用例 ID
        severity: 'error' / 'warning' / 'info'
        category: 'unknown_field' / 'unknown_method' / 'unknown_enum' / 'placeholder_missing'
        field: 出问题的字段/方法/枚举名
        chunk_id: 期望召回的 chunk (如果相关)
        suggestion: 修复建议
    """

    def __init__(
        self,
        case_id: str,
        severity: str,
        category: str,
        field: str,
        chunk_id: Optional[str] = None,
        suggestion: str = "",
    ) -> None:
        self.case_id = case_id
        self.severity = severity
        self.category = category
        self.field = field
        self.chunk_id = chunk_id
        self.suggestion = suggestion

    def to_dict(self) -> Dict[str, Any]:
        return {
            "case_id": self.case_id,
            "severity": self.severity,
            "category": self.category,
            "field": self.field,
            "chunk_id": self.chunk_id,
            "suggestion": self.suggestion,
        }


class ValidationReport:
    """一组用例的验证报告.

    Attributes:
        total_cases: 总用例数
        validated_cases: 验证通过的用例数
        warnings: 警告列表 (字段/方法未验证)
        errors: 错误列表 (字段/方法在 chunk 里绝对找不到)
        placeholder_count: 含占位符 TODO 的用例数
    """

    def __init__(self) -> None:
        self.total_cases = 0
        self.validated_cases = 0
        self.warnings: List[ValidationIssue] = []
        self.errors: List[ValidationIssue] = []
        self.placeholder_count = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_cases": self.total_cases,
            "validated_cases": self.validated_cases,
            "warnings": [w.to_dict() for w in self.warnings],
            "errors": [e.to_dict() for e in self.errors],
            "placeholder_count": self.placeholder_count,
        }


# ============================================================
# 占位符检测 (基础版本 - 可用)
# ============================================================


def detect_placeholders(case: Dict[str, Any]) -> List[str]:
    """检测用例中的 {{ TODO }} 占位符 (用于标识 LLM 标记为'未知'的字段).

    Returns:
        占位符描述列表. 例: ['TODO: 测试账号, 来源: 测试环境']
    """
    txt = json.dumps(case, ensure_ascii=False)
    return re.findall(r'\{\{ TODO: ([^}]+) \}\}', txt)


# ============================================================
# Chunk 索引读取 (供下版本实现)
# ============================================================


def load_chunk_index(domain: str, project_root: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """加载指定 domain 的 chunk 索引.

    Returns:
        {
          "chunk_id": {
            "qualified_name": "...",
            "name": "...",
            "file_path": "...",
            "line_start": 123,
            "chunk_data": "..."
          },
          ...
        }

    TODO (v0.5+ 实现):
        1. 打开 domains/{domain}/_index.db
        2. SELECT chunk_id, qualified_name, name, file_path, line_start, chunk_data FROM chunks
        3. 返回 dict
    """
    # 骨架版本: 返回空 dict
    logger.debug(f"[case_validator] load_chunk_index({domain}) - skeleton version, returning empty dict")
    return {}


# ============================================================
# 字段名 fuzzy 匹配 (供下版本实现)
# ============================================================


def field_names_match(case_field: str, chunk_field: str) -> bool:
    """判断两个字段名是否"同一个" (支持 snake ↔ camel 转换).

    规则:
    - 完全相同 → True
    - snake_case → camelCase: agent_status ↔ agentStatus → True
    - camelCase → snake_case: onlineStatus ↔ online_status → True

    TODO (v0.5+ 实现):
        1. 标准化: lowercase + 替换 _ 为空
        2. 比较
    """
    # 骨架版本: 仅做完全匹配
    return case_field == chunk_field


# ============================================================
# 主验证函数 (供下版本实现)
# ============================================================


def validate_case(
    case: Dict[str, Any],
    linked_chunks: List[Dict[str, Any]],
    chunk_index: Dict[str, Dict[str, Any]],
) -> List[ValidationIssue]:
    """验证单条用例是否引用了真实代码 (无幻觉).

    Args:
        case: 单条用例 dict (含 steps / assertions / data_setup / raw_text 等)
        linked_chunks: 这条用例关联的 chunks (来自 stage3 links)
        chunk_index: 整个 domain 的 chunk 索引 (keyed by chunk_id)

    Returns:
        ValidationIssue 列表. 空列表 = 无问题.

    TODO (v0.5+ 实现):
        1. 提取 case 中所有字段名/方法名/枚举值 (regex 模式)
        2. 对每个名字, 在 chunk_index 里查找 (含 fuzzy 匹配)
        3. 找到 → OK; 找不到 → 加 warning 或 error
        4. 占位符 {{ TODO }} 不参与验证
    """
    # 骨架版本: 返回空 (无幻觉检测)
    return []


def validate_cases(
    cases: List[Dict[str, Any]],
    links: List[Dict[str, Any]],
    domain: str,
    project_root: Optional[Path] = None,
) -> ValidationReport:
    """批量验证一组用例, 返回 ValidationReport.

    Args:
        cases: 用例列表 (来自 stage5 cases.json)
        links: 这批用例关联的 chunks (来自 stage3 links.json)
        domain: 业务域 (用于加载 chunk 索引)
        project_root: 项目根目录

    Returns:
        ValidationReport (含 warnings / errors / placeholder count)

    调用位置:
        src/generate.py stage5_generate_cases 末尾
        cases.json 写入时同时写 validation.json

    TODO (v0.5+ 实现):
        1. 加载 chunk_index = load_chunk_index(domain)
        2. 对每条 case: 关联到它专属的 linked_chunks (来自 stage3 links 的 chunk_id)
        3. validate_case(case, linked_chunks, chunk_index)
        4. 累计 warnings / errors
        5. 占位符统计 (call detect_placeholders)
    """
    report = ValidationReport()
    report.total_cases = len(cases)

    # 占位符统计 (基础版本 - 可用)
    for case in cases:
        placeholders = detect_placeholders(case)
        if placeholders:
            report.placeholder_count += 1

    # 骨架版本: 没有真正的字段验证
    # 下一版: implement validate_case for each case

    report.validated_cases = report.total_cases  # 骨架: 假设全部通过

    return report


def save_validation_report(
    report: ValidationReport,
    task_dir: Path,
) -> Optional[Path]:
    """保存 ValidationReport 到 task_dir/validation.json.

    Args:
        report: ValidationReport 实例
        task_dir: 任务目录 (例: runs/REQ-CS-001-e2e)

    Returns:
        写入的 validation.json 路径, 或 None (失败)
    """
    try:
        out_path = task_dir / "validation.json"
        out_path.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info(f"[case_validator] 验证报告已保存: {out_path}")
        return out_path
    except Exception as e:
        logger.warning(f"[case_validator] 保存验证报告失败: {e}")
        return None


# ============================================================
# __all__
# ============================================================

__all__ = [
    "ValidationIssue",
    "ValidationReport",
    "detect_placeholders",
    "load_chunk_index",
    "field_names_match",
    "validate_case",
    "validate_cases",
    "save_validation_report",
]