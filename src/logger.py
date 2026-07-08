"""
统一日志 + 审计系统

功能:
1. 控制台 + 文件双输出
2. JSON 结构化日志 (便于检索)
3. 审计日志 (所有写操作记录)
4. 自动脱敏 (Token 安全)
"""

import os
import sys
import json
import logging
import logging.handlers
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any

from src.llm_client import redact_text, mask_token


# ============================================================
# 自定义 Formatter
# ============================================================

class JSONFormatter(logging.Formatter):
    """JSON 格式日志, 便于检索"""
    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "timestamp": datetime.fromtimestamp(record.created).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # 附加字段
        for key in ("task_id", "stage", "node", "duration_ms", "extra"):
            if hasattr(record, key):
                log_data[key] = getattr(record, key)
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        # 脱敏
        text = json.dumps(log_data, ensure_ascii=False, default=str)
        return redact_text(text)


class SafeFormatter(logging.Formatter):
    """人类可读格式 + 脱敏"""
    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        return redact_text(text)


# ============================================================
# 审计 Logger
# ============================================================

class AuditLogger:
    """
    审计日志 - 记录所有写操作

    用法:
        audit = AuditLogger(audit_dir="./.logs/audit")
        audit.log(
            action="code.update",
            actor="张三",
            target="OrderService.java",
            result="success",
            detail={"chunks_added": 12}
        )
    """

    def __init__(self, audit_dir: Path):
        self.audit_dir = Path(audit_dir)
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        self.logger = logging.getLogger("audit")

    def log(
        self,
        action: str,
        actor: str = "system",
        target: str = "",
        result: str = "success",
        detail: Optional[Dict[str, Any]] = None,
    ):
        """记录一条审计日志"""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "action": action,
            "actor": actor,
            "target": target,
            "result": result,
            "detail": detail or {},
        }

        # 写到当日文件
        log_file = self.audit_dir / f"{datetime.now().strftime('%Y-%m-%d')}.jsonl"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        # 同时打到主 logger (INFO 级别)
        level = logging.INFO if result == "success" else logging.ERROR
        self.logger.log(
            level,
            f"[AUDIT] {action} | {actor} | {target} | {result}",
            extra={"extra": entry},
        )


# ============================================================
# 全局初始化
# ============================================================

_initialized = False


def setup_logging(
    log_dir: str = "./.logs",
    level: str = "INFO",
    console: bool = True,
    file: bool = True,
) -> Path:
    """
    初始化日志系统

    Returns:
        日志目录路径
    """
    global _initialized
    if _initialized:
        return Path(log_dir)

    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    # 根 logger
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # 清空已有 handler
    root.handlers.clear()

    # 控制台
    if console:
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.INFO)
        ch.setFormatter(SafeFormatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        ))
        root.addHandler(ch)

    # 文件 (按日滚动)
    if file:
        # Windows 下多个 Python 进程共用同一个 TimedRotatingFileHandler 容易在 rollover 时互相抢锁。
        # 改为按 PID 分文件，避免 app.log rename 冲突。
        log_file = log_path / f"app-{os.getpid()}.log"
        fh = logging.handlers.TimedRotatingFileHandler(
            log_file,
            when="midnight",
            interval=1,
            backupCount=30,
            encoding="utf-8",
            delay=True,
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(JSONFormatter())
        root.addHandler(fh)

    # 降级第三方库的日志级别
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.WARNING)

    _initialized = True
    return log_path


def get_logger(name: str) -> logging.Logger:
    """获取 logger"""
    return logging.getLogger(name)


def get_audit_logger(audit_dir: str = "./.logs/audit") -> AuditLogger:
    """获取审计 logger"""
    return AuditLogger(Path(audit_dir))


# ============================================================
# 命令行入口
# ============================================================

if __name__ == "__main__":
    log_path = setup_logging()
    audit = get_audit_logger()

    logger = get_logger(__name__)
    logger.info("日志系统初始化完成")
    logger.debug("Debug 级别日志")
    logger.warning("Warning 级别日志")

    audit.log(
        action="test.audit",
        actor="test_user",
        target="test_target",
        result="success",
        detail={"key": "value"},
    )

    print(f"\n✅ 日志目录: {log_path}")
    print(f"✅ 审计日志: {audit.audit_dir}")
    print(f"✅ 脱敏测试: {mask_token('sk-cp-test123456789')}")