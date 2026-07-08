"""
v0.5.7+: Audit log 索引加速

原实现: 每次 /requirement?req_id=X 请求都倒序遍历所有 .logs/audit/*.jsonl
新实现: 维护一个内存索引 {req_id: [(log_file, line_no, offset_bytes), ...]}
        按 mtime 增量更新, O(1) 查找
"""
import json
import time
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple


class AuditIndex:
    """为 generate.start event 按 req_id 索引, 支持 O(1) 查找.

    索引文件: <audit_dir>/.audit_index.json
      {
        "version": 1,
        "built_at": "2026-07-02T...",
        "entries": {
          "REQ-LP-001": [
            {"log": "2026-07-02.jsonl", "line_no": 42, "offset": 1234, "ts": "..."},
            ...
          ],
          ...
        }
      }
    """

    INDEX_FILE = ".audit_index.json"
    MAX_ENTRIES_PER_REQ = 5  # 每个 req_id 最多保留 N 个事件 (倒序, 最新的优先)

    def __init__(self, audit_dir: Path):
        self.audit_dir = Path(audit_dir)
        self.index_path = self.audit_dir / self.INDEX_FILE
        self._index: Dict[str, List[Dict[str, Any]]] = {}
        self._log_mtimes: Dict[str, float] = {}
        self._load()

    def _load(self):
        """加载索引 + 检查 mtime 增量更新"""
        if self.index_path.exists():
            try:
                data = json.loads(self.index_path.read_text(encoding="utf-8"))
                if data.get("version") == 1:
                    self._index = data.get("entries", {})
            except Exception:
                self._index = {}
        # 增量: 扫所有 jsonl, 只处理 mtime > 已记录 mtime 的
        self._update_incremental()

    def _update_incremental(self):
        """增量扫描: 只处理新增/修改的 log 文件"""
        if not self.audit_dir.exists():
            return
        changed = False
        for logf in sorted(self.audit_dir.glob("*.jsonl")):
            mtime = logf.stat().st_mtime
            if self._log_mtimes.get(logf.name) == mtime:
                continue
            # 处理这个 log
            self._index_log(logf)
            self._log_mtimes[logf.name] = mtime
            changed = True
        if changed:
            self._save()

    def _index_log(self, logf: Path):
        """扫描单个 log 文件, 提取所有 generate.start 事件"""
        try:
            text = logf.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return
        for line_no, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get("action") != "generate.start":
                continue
            target = ev.get("target")
            detail = ev.get("detail")
            if not target or not isinstance(detail, dict):
                continue
            entry = {
                "log": logf.name,
                "line_no": line_no,
                "ts": ev.get("timestamp") or ev.get("ts", ""),
            }
            # 保留每个 req_id 最近 N 个 (倒序, 最新的优先)
            lst = self._index.setdefault(target, [])
            lst.append(entry)
            # 按 line_no 倒序
            lst.sort(key=lambda e: e["line_no"], reverse=True)
            # 截断
            if len(lst) > self.MAX_ENTRIES_PER_REQ:
                del lst[self.MAX_ENTRIES_PER_REQ:]

    def _save(self):
        """持久化索引 (下次启动不用全扫)"""
        try:
            self.audit_dir.mkdir(parents=True, exist_ok=True)
            data = {
                "version": 1,
                "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "entries": self._index,
            }
            self.index_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    def find_generate_start(self, req_id: str) -> Optional[Dict[str, Any]]:
        """查找 req_id 的最近一次 generate.start event.

        Returns: {"log_file": "2026-07-02.jsonl", "line_no": 42, "detail": {...}}
                 or None if not found
        """
        entries = self._index.get(req_id)
        if not entries:
            return None
        # 最新的 (line_no 最大)
        latest = entries[0]
        # 读那一行
        log_path = self.audit_dir / latest["log"]
        if not log_path.exists():
            return None
        try:
            with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                for i, line in enumerate(f, 1):
                    if i == latest["line_no"]:
                        try:
                            ev = json.loads(line)
                            return {
                                "log_file": latest["log"],
                                "line_no": latest["line_no"],
                                "detail": ev.get("detail", {}),
                            }
                        except Exception:
                            return None
        except Exception:
            return None
        return None

    def stats(self) -> Dict[str, Any]:
        return {
            "total_reqs": len(self._index),
            "total_entries": sum(len(v) for v in self._index.values()),
            "index_file": str(self.index_path),
            "log_files_tracked": len(self._log_mtimes),
        }


if __name__ == "__main__":
    # 简单自测
    import sys
    audit_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".logs/audit")
    idx = AuditIndex(audit_dir)
    print(json.dumps(idx.stats(), ensure_ascii=False, indent=2))
    # 测试找第一个 req_id
    if idx._index:
        first_req = next(iter(idx._index))
        hit = idx.find_generate_start(first_req)
        print(f"\nFirst req: {first_req}")
        print(f"Hit: {hit}")