"""
工作区管理

负责:
1. 任务运行目录创建
2. Stage 产物落盘
3. 任务状态持久化 (SQLite)
"""

import json
import sqlite3
import uuid
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any, List, Iterable

from src.logger import get_logger, get_audit_logger

logger = get_logger(__name__)


# ============================================================
# 任务状态
# ============================================================

TASK_STATUSES = [
    "pending", "running", "success", "failed", "partial",
    # v0.5 新增: 审核态
    "awaiting_review_links",      # stage 3 完, 等审 links
    "awaiting_review_scenarios",  # stage 4 完, 等审 scenarios
    "awaiting_review_cases",      # stage 5 完, 等审 cases
]
STAGE_STATUSES = ["pending", "running", "success", "failed", "skipped"]


# v0.5: stage_name -> awaiting status 映射
STAGE_TO_REVIEW_STATUS = {
    "stage3": "awaiting_review_links",
    "stage4": "awaiting_review_scenarios",
    "stage5": "awaiting_review_cases",
}


class TaskWorkspace:
    """单个任务的运行工作区"""

    def __init__(self, root: Path, task_id: str, req_id: str):
        self.workspaces_dir = Path(root)
        self.req_root = self.workspaces_dir / req_id
        self.root = self.req_root / task_id
        self.task_id = task_id
        self.req_id = req_id
        self.created_at = datetime.now()
        self.root.mkdir(parents=True, exist_ok=True)

        # 创建子目录
        for stage in ["stage1", "stage2", "stage3", "stage4", "stage5"]:
            (self.root / stage).mkdir(exist_ok=True)

        # 状态库
        self.db_path = self.root / "task.db"
        self._init_db()

        logger.info(f"工作区已创建: {self.root} (task_id={task_id})")

    def _init_db(self):
        """初始化任务状态数据库 (v0.5.7: 兼容老 db 无 domain 列)"""
        with sqlite3.connect(self.db_path) as conn:
            # 老 db schema 升级: 补齐 v0.5.7 新增的 domain 列
            # (CREATE TABLE IF NOT EXISTS 不会改老表结构)
            try:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(task)").fetchall()}
                if "domain" not in cols:
                    conn.execute("ALTER TABLE task ADD COLUMN domain TEXT")
                    logger.info(f"task 表已加 domain 列: {self.db_path}")
            except Exception:
                # task 表还不存在, _init_db 后续会建
                pass
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS task (
                    task_id TEXT PRIMARY KEY,
                    req_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    domain TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    total_duration_ms INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS stage (
                    stage_name TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    duration_ms INTEGER DEFAULT 0,
                    error TEXT,
                    FOREIGN KEY (task_id) REFERENCES task(task_id)
                );

                CREATE TABLE IF NOT EXISTS node (
                    node_id TEXT PRIMARY KEY,
                    stage_name TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    duration_ms INTEGER DEFAULT 0,
                    input_size INTEGER,
                    output_size INTEGER,
                    error TEXT,
                    FOREIGN KEY (stage_name) REFERENCES stage(stage_name)
                );

                CREATE TABLE IF NOT EXISTS llm_call (
                    call_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    stage_name TEXT,
                    node_id TEXT,
                    model TEXT,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    duration_ms INTEGER,
                    status TEXT,
                    error TEXT,
                    timestamp TEXT,
                    FOREIGN KEY (task_id) REFERENCES task(task_id)
                );

                CREATE INDEX IF NOT EXISTS idx_stage_task ON stage(task_id);
                CREATE INDEX IF NOT EXISTS idx_node_stage ON node(stage_name);
                CREATE INDEX IF NOT EXISTS idx_llm_task ON llm_call(task_id);

                -- v0.5: 审计日志 (人工审核/确认/拒绝动作)
                CREATE TABLE IF NOT EXISTS audit_log (
                    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    stage TEXT,
                    case_id TEXT,
                    payload TEXT,
                    actor TEXT DEFAULT 'user',
                    timestamp TEXT NOT NULL,
                    FOREIGN KEY (task_id) REFERENCES task(task_id)
                );
                CREATE INDEX IF NOT EXISTS idx_audit_task ON audit_log(task_id);
                CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);
            """)

    def save_task(self, status: str = "running", domain: Optional[str] = None):
        """保存任务元数据 (v0.5.7: domain 用于 retry 时回填)"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO task
                   (task_id, req_id, status, domain, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (self.task_id, self.req_id, status, domain,
                 self.created_at.isoformat(),
                 datetime.now().isoformat()),
            )

    def update_task_status(self, status: str):
        """更新任务状态"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE task SET status=?, updated_at=? WHERE task_id=?",
                (status, datetime.now().isoformat(), self.task_id),
            )

    def save_stage(self, stage_name: str, status: str,
                   started_at: datetime, finished_at: Optional[datetime] = None,
                   error: Optional[str] = None):
        """保存 Stage 状态"""
        duration = 0
        if finished_at:
            duration = int((finished_at - started_at).total_seconds() * 1000)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO stage
                   (stage_name, task_id, status, started_at, finished_at, duration_ms, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (stage_name, self.task_id, status,
                 started_at.isoformat(),
                 finished_at.isoformat() if finished_at else None,
                 duration, error),
            )

    def save_node(self, node_id: str, stage_name: str, status: str,
                  started_at: datetime, finished_at: Optional[datetime] = None,
                  input_size: int = 0, output_size: int = 0,
                  error: Optional[str] = None):
        """保存节点状态"""
        duration = 0
        if finished_at:
            duration = int((finished_at - started_at).total_seconds() * 1000)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO node
                   (node_id, stage_name, task_id, status, started_at, finished_at,
                    duration_ms, input_size, output_size, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (node_id, stage_name, self.task_id, status,
                 started_at.isoformat(),
                 finished_at.isoformat() if finished_at else None,
                 duration, input_size, output_size, error),
            )

    def save_llm_call(self, call_id: str, model: str,
                      input_tokens: int, output_tokens: int,
                      duration_ms: int, status: str,
                      stage_name: Optional[str] = None,
                      node_id: Optional[str] = None,
                      error: Optional[str] = None):
        """保存 LLM 调用记录"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO llm_call
                   (call_id, task_id, stage_name, node_id, model,
                    input_tokens, output_tokens, duration_ms, status, error, timestamp)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (call_id, self.task_id, stage_name, node_id, model,
                 input_tokens, output_tokens, duration_ms, status, error,
                 datetime.now().isoformat()),
            )

    def save_audit(self, action: str, stage: Optional[str] = None,
                  case_id: Optional[str] = None, payload: Optional[dict] = None,
                  actor: str = "user"):
        """v0.5: 写入审计日志"""
        import datetime as _dt
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO audit_log
                   (task_id, action, stage, case_id, payload, actor, timestamp)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (self.task_id, action, stage, case_id,
                 json.dumps(payload, ensure_ascii=False) if payload else None,
                 actor, _dt.datetime.now().isoformat()),
            )

    def save_artifact(self, stage: str, name: str, data: Any):
        """保存 Stage 产物到磁盘"""
        path = self.root / stage / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, (dict, list)):
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        else:
            path.write_text(str(data), encoding="utf-8")
        logger.debug(f"产物已保存: {path}")

    def load_artifact(self, stage: str, name: str) -> Optional[Any]:
        """读取 Stage 产物"""
        path = self.root / stage / name
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def get_summary(self) -> Dict[str, Any]:
        """获取任务摘要"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            task = conn.execute(
                "SELECT * FROM task WHERE task_id=?",
                (self.task_id,),
            ).fetchone()
            stages = conn.execute(
                "SELECT * FROM stage WHERE task_id=? ORDER BY started_at",
                (self.task_id,),
            ).fetchall()
            llm_calls = conn.execute(
                "SELECT COUNT(*), SUM(input_tokens), SUM(output_tokens) "
                "FROM llm_call WHERE task_id=? AND status='success'",
                (self.task_id,),
            ).fetchone()

        return {
            "task": dict(task) if task else None,
            "stages": [dict(s) for s in stages],
            "llm_stats": {
                "total_calls": llm_calls[0] or 0,
                "total_input_tokens": llm_calls[1] or 0,
                "total_output_tokens": llm_calls[2] or 0,
            },
        }

    def get_last_success_stage(self) -> Optional[str]:
        """v0.5.7: 返回 task 内最后一个 status=success 的 stage_name (如 'stage3')

        retry 时从该 stage 的下一个开始重跑 (传 skip_stages=[stage1..stageN])
        没有成功 stage 时返回 None (重跑全部)
        """
        order = ["stage1", "stage2", "stage3", "stage4", "stage5"]
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT stage_name, status FROM stage WHERE task_id=?",
                (self.task_id,),
            ).fetchall()
        stage_status = {r[0]: r[1] for r in rows}
        last_ok = None
        for s in order:
            if stage_status.get(s) == "success":
                last_ok = s
        return last_ok


# ============================================================
# 任务管理器
# ============================================================

class WorkspaceManager:
    """任务工作区管理器"""

    def __init__(self, workspaces_dir: Path):
        self.workspaces_dir = Path(workspaces_dir)
        self.workspaces_dir.mkdir(parents=True, exist_ok=True)

    def _iter_task_db_files(self) -> Iterable[Path]:
        """遍历所有 task.db (兼容老结构 runs/<req_id>/task.db 与新结构 runs/<req_id>/<task_id>/task.db)."""
        return self.workspaces_dir.rglob("task.db")

    @staticmethod
    def _task_columns(conn: sqlite3.Connection) -> set[str]:
        return {row[1] for row in conn.execute("PRAGMA table_info(task)").fetchall()}

    @classmethod
    def _latest_task_row(cls, conn: sqlite3.Connection):
        conn.row_factory = sqlite3.Row
        cols = cls._task_columns(conn)
        domain_sql = "domain" if "domain" in cols else "NULL AS domain"
        return conn.execute(
            f"""SELECT task_id, req_id, status, {domain_sql}, created_at, updated_at
               FROM task
               ORDER BY COALESCE(updated_at, created_at) DESC, created_at DESC, rowid DESC
               LIMIT 1"""
        ).fetchone()

    def create_task(
        self,
        req_id: str,
        domain: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> TaskWorkspace:
        """创建新任务工作区 (v0.5.8: 支持指定 task_id, 供后台续跑先占位再异步执行)."""
        task_id = task_id or f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        workspace = TaskWorkspace(self.workspaces_dir, task_id, req_id)
        workspace.save_task(status="running", domain=domain)

        # 审计
        audit = get_audit_logger()
        audit.log(
            action="task.create",
            target=req_id,
            detail={"task_id": task_id, "domain": domain},
        )

        return workspace

    def list_tasks(self, limit: int = 50) -> List[Dict]:
        """列出最近任务"""
        tasks = []
        for db_file in self._iter_task_db_files():
            try:
                with sqlite3.connect(db_file) as conn:
                    row = self._latest_task_row(conn)
                    if row:
                        item = dict(row)
                        item["workspace"] = str(db_file.parent)
                        tasks.append(item)
            except Exception as e:
                logger.warning(f"读取任务失败 {db_file}: {e}")

        tasks.sort(
            key=lambda t: (t.get("updated_at") or t.get("created_at") or "", t.get("created_at") or ""),
            reverse=True,
        )
        return tasks[:limit]

    def get_latest_task_for_req(self, req_id: str, exclude_task_id: Optional[str] = None) -> Optional[TaskWorkspace]:
        """获取某 req 最新任务, 供 skip/retry 复用产物."""
        candidates = []
        for db_file in self._iter_task_db_files():
            try:
                with sqlite3.connect(db_file) as conn:
                    conn.row_factory = sqlite3.Row
                    row = conn.execute(
                        """SELECT task_id, req_id, created_at, updated_at
                           FROM task
                           WHERE req_id=?
                           ORDER BY COALESCE(updated_at, created_at) DESC, created_at DESC, rowid DESC
                           LIMIT 1""",
                        (req_id,),
                    ).fetchone()
                    if not row:
                        continue
                    if exclude_task_id and row["task_id"] == exclude_task_id:
                        continue
                    candidates.append((row["updated_at"] or row["created_at"] or "", row["created_at"] or "", row["task_id"]))
            except Exception:
                continue

        if not candidates:
            return None
        candidates.sort(reverse=True)
        return self.get_task(candidates[0][2])

    def get_task(self, task_id: str) -> Optional[TaskWorkspace]:
        """获取已存在的任务 (只读, 不重新初始化 DB)"""
        for db_file in self._iter_task_db_files():
            try:
                with sqlite3.connect(db_file) as conn:
                    conn.row_factory = sqlite3.Row
                    row = conn.execute(
                        "SELECT task_id, req_id FROM task WHERE task_id=? ORDER BY rowid DESC LIMIT 1",
                        (task_id,),
                    ).fetchone()
                    if row:
                        req_id = row["req_id"]
                        workspace = TaskWorkspace.__new__(TaskWorkspace)
                        workspace.workspaces_dir = self.workspaces_dir
                        expected_req_root = self.workspaces_dir / req_id
                        workspace.req_root = expected_req_root if db_file.is_relative_to(expected_req_root) else db_file.parent
                        workspace.root = db_file.parent
                        workspace.task_id = row["task_id"]
                        workspace.req_id = req_id
                        workspace.created_at = datetime.now()
                        workspace.db_path = db_file
                        return workspace
            except Exception:
                continue
        return None


if __name__ == "__main__":
    # 简单测试
    from src.logger import setup_logging
    setup_logging()

    mgr = WorkspaceManager(Path("./runs"))
    ws = mgr.create_task("REQ-2024-001")

    print(f"✅ 任务创建: {ws.task_id}")
    print(f"   工作区: {ws.root}")
    print(f"   DB: {ws.db_path}")

    # 保存测试产物
    ws.save_artifact("stage1", "test.json", {"hello": "world"})
    print(f"✅ 产物保存: stage1/test.json")

    loaded = ws.load_artifact("stage1", "test.json")
    print(f"✅ 产物读取: {loaded}")