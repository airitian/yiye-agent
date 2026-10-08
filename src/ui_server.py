"""
可观测 UI - FastAPI 后端

提供:
1. 任务列表 API
2. 任务详情 API (含节点时序)
3. SSE 实时推送
4. 业务域统计
5. 代码检索 API
"""

import asyncio
import json
import os
import sqlite3
import subprocess as _sp
import uuid as _uuid
from pathlib import Path
from typing import List, Optional, Dict, Any
from datetime import datetime
from html import escape as html_escape

from fastapi import FastAPI, HTTPException, Query, Body
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

import sys
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# BUG-5 fix: 全局后台进程注册表 (避免 fire-and-forget 进程孤儿化)
import atexit
_BG_PROCESSES: Dict[int, Dict[str, Any]] = {}

def _cleanup_bg_processes():
    """atexit 钩子: uvicorn 关闭时清理所有后台子进程 + 关闭 log fd (BUG-10)"""
    for pid, info in list(_BG_PROCESSES.items()):
        try:
            proc = info.get("_proc")
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except Exception:
                    proc.kill()
        except Exception:
            pass
        try:
            log_h = info.get("_log_handle")
            if log_h:
                log_h.close()
        except Exception:
            pass
    _BG_PROCESSES.clear()
atexit.register(_cleanup_bg_processes)

from src.logger import setup_logging
from src.workspace import WorkspaceManager, STAGE_TO_REVIEW_STATUS

setup_logging()


# ============================================================
# App
# ============================================================

app = FastAPI(
    title="AI 测试用例生成系统 - 可观测 UI",
    version="0.1.0",
)


# ============================================================
# 数据访问层
# ============================================================

def list_tasks(limit: int = 50) -> List[Dict[str, Any]]:
    """列出所有任务"""
    mgr = WorkspaceManager(PROJECT_ROOT / "runs")
    return mgr.list_tasks(limit=limit)


def get_task_detail(task_id: str) -> Optional[Dict[str, Any]]:
    """获取任务详情"""
    mgr = WorkspaceManager(PROJECT_ROOT / "runs")
    ws = mgr.get_task(task_id)
    if not ws:
        return None

    with sqlite3.connect(ws.db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM task WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if not row:
            return None

        task_meta = dict(row)
        task_meta["workspace"] = str(ws.root)

        stages = conn.execute(
            "SELECT * FROM stage WHERE task_id=? ORDER BY started_at",
            (task_id,),
        ).fetchall()
        nodes = conn.execute(
            """SELECT n.* FROM node n
               JOIN stage s ON n.stage_name = s.stage_name AND n.task_id = s.task_id
               WHERE s.task_id = ?
               ORDER BY n.started_at""",
            (task_id,),
        ).fetchall()
        llm = conn.execute(
            """SELECT * FROM llm_call WHERE task_id=?
               ORDER BY timestamp""",
            (task_id,),
        ).fetchall()

        stages_data = [dict(s) for s in stages]
        nodes_data = [dict(n) for n in nodes]
        llm_data = [dict(l) for l in llm]

        detail = dict(task_meta)
        detail["stages"] = stages_data
        detail["nodes"] = nodes_data
        detail["llm_calls"] = llm_data
        detail["task"] = dict(task_meta)  # 兼容旧调用方期待 detail.task
        detail.update(_attach_continuation(detail))
        return detail


def _attach_continuation(detail: Dict[str, Any]) -> Dict[str, Any]:
    """
    给 partial 任务补上"续跑任务"线索.

    背景: advance-stage 审核通过后, 会把旧任务状态写成 partial 并派生一个
    新 task_id 承接后续 stage (见 api_advance_stage). 这是设计如此, 不是 bug.
    但 API 只返回 partial, 外部编排者 (脚本/CI) 无从知道该跟哪个 task_id,
    只能靠轮询猜 —— 实测会把"等待审核"误判成"卡死".

    这里从 audit_log 里把最近一次 stage_advance 记录里的 new_task_id读出来,
    以continuation_task_id / is_terminal 两个字段暴露给调用方.
    """
    out: Dict[str, Any] = {}
    status = detail.get("status")
    task_id = detail.get("task_id")

    out["continuation_task_id"] = None
    out["is_terminal"] = status in ("success", "failed")

    if status != "partial" or not task_id:
        return out

    ws = detail.get("workspace")
    if not ws:
        return out

    db_path = Path(ws) / "task.db"
    if not db_path.exists():
        return out

    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT payload FROM audit_log "
                "WHERE action='stage_advance' ORDER BY rowid DESC LIMIT 5"
            ).fetchall()
    except sqlite3.Error:
        return out

    for r in rows:
        try:
            payload = json.loads(r["payload"]) if r["payload"] else {}
        except (json.JSONDecodeError, TypeError):
            continue
        new_tid = payload.get("new_task_id")
        if new_tid and new_tid != task_id:
            out["continuation_task_id"] = new_tid
            break

    return out


def _empty_debug_artifacts(task_dir: Optional[Path] = None) -> Dict[str, Any]:
    task_dir_rel = None
    if task_dir is not None:
        try:
            task_dir_rel = task_dir.relative_to(PROJECT_ROOT).as_posix()
        except Exception:
            task_dir_rel = str(task_dir).replace('\\', '/')
    stages: Dict[str, Dict[str, Any]] = {}
    for stage in ("stage4", "stage5"):
        stages[stage] = {
            "raw": [],
            "repaired": [],
            "other": [],
            "has_any": False,
            "count": 0,
        }
    return {
        "task_dir": task_dir_rel,
        "available": False,
        "stages": stages,
        "all": [],
    }


def _build_task_debug_artifacts(task_dir: Optional[Path]) -> Dict[str, Any]:
    """收集 stage4/stage5 下的调试产物，给 workflow/report/UI 统一复用。"""
    result = _empty_debug_artifacts(task_dir)
    if task_dir is None or not task_dir.exists():
        return result

    for stage in ("stage4", "stage5"):
        stage_bucket = result["stages"][stage]
        debug_dir = task_dir / stage / "debug"
        if not debug_dir.exists():
            continue

        files = sorted(
            [p for p in debug_dir.rglob("*") if p.is_file()],
            key=lambda p: (p.stat().st_mtime, str(p).lower()),
            reverse=True,
        )
        for file_path in files:
            stat = file_path.stat()
            rel_path = file_path.relative_to(task_dir).as_posix()
            name = file_path.name
            name_lower = name.lower()
            if "repair" in name_lower:
                variant = "repaired"
            elif "raw" in name_lower or "original" in name_lower:
                variant = "raw"
            else:
                variant = "other"

            item = {
                "stage": stage,
                "variant": variant,
                "file_name": name,
                "relative_path": rel_path,
                "size_bytes": stat.st_size,
                "updated_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            }
            stage_bucket[variant].append(item)
            result["all"].append(item)

        stage_bucket["has_any"] = bool(
            stage_bucket["raw"] or stage_bucket["repaired"] or stage_bucket["other"]
        )
        stage_bucket["count"] = (
            len(stage_bucket["raw"])
            + len(stage_bucket["repaired"])
            + len(stage_bucket["other"])
        )

    result["available"] = bool(result["all"])
    return result


def _summarize_debug_artifact_counts(debug_artifacts: Dict[str, Any]) -> Dict[str, Dict[str, int]]:
    summary: Dict[str, Dict[str, int]] = {}
    for stage in ("stage4", "stage5"):
        stage_data = (debug_artifacts.get("stages") or {}).get(stage, {})
        summary[stage] = {
            "raw": len(stage_data.get("raw", [])),
            "repaired": len(stage_data.get("repaired", [])),
            "other": len(stage_data.get("other", [])),
            "total": int(stage_data.get("count", 0) or 0),
        }
    return summary


def _resolve_task_dir(task_id: str, workspace: Optional[str] = None) -> Optional[Path]:
    if workspace:
        try:
            workspace_path = Path(workspace)
            if workspace_path.exists():
                return workspace_path
        except Exception:
            pass

    ws_mgr = WorkspaceManager(PROJECT_ROOT / "runs")
    ws = ws_mgr.get_task(task_id)
    return ws.root if ws else None


def _build_debug_artifact_payload(
    task_dir: Path,
    stage: str,
    variant: str,
    limit: int = 200000,
) -> Dict[str, Any]:
    stage = stage if stage in ("stage4", "stage5") else "stage4"
    variant = variant if variant in ("raw", "repaired", "other") else "raw"
    debug_artifacts = _build_task_debug_artifacts(task_dir)
    files = ((debug_artifacts.get("stages") or {}).get(stage, {}) or {}).get(variant, [])

    payload_files: List[Dict[str, Any]] = []
    for item in files:
        file_path = task_dir / item["relative_path"]
        preview = _read_text_preview(file_path, limit=limit)
        payload_files.append({
            **item,
            **preview,
            "line_count": preview["content"].count("\n") + (1 if preview["content"] else 0),
        })

    stage_titles = {"stage4": "Stage 4 场景", "stage5": "Stage 5 用例"}
    variant_titles = {"raw": "原始输出", "repaired": "修复后输出", "other": "其它调试文件"}
    return {
        "task_dir": debug_artifacts.get("task_dir"),
        "available": bool(payload_files),
        "stage": stage,
        "stage_title": stage_titles.get(stage, stage),
        "variant": variant,
        "variant_title": variant_titles.get(variant, variant),
        "count": len(payload_files),
        "files": payload_files,
    }


def _enrich_task_debug_summary(task: Dict[str, Any]) -> Dict[str, Any]:
    enriched = dict(task)
    task_dir = _resolve_task_dir(enriched.get("task_id", ""), enriched.get("workspace"))
    debug_artifacts = _build_task_debug_artifacts(task_dir)
    debug_artifact_counts = _summarize_debug_artifact_counts(debug_artifacts)
    enriched["has_debug_artifacts"] = debug_artifacts.get("available", False)
    enriched["debug_artifact_counts"] = debug_artifact_counts
    enriched["has_debug_repairs"] = any(
        (debug_artifact_counts.get(stage, {}).get("repaired", 0) or 0) > 0
        for stage in ("stage4", "stage5")
    )
    enriched["has_debug_other"] = any(
        (debug_artifact_counts.get(stage, {}).get("other", 0) or 0) > 0
        for stage in ("stage4", "stage5")
    )
    return enriched


def _render_debug_artifact_links(
    task_id: str,
    debug_artifacts: Dict[str, Any],
    inline_preview: bool = False,
) -> str:
    stage_titles = {
        "stage4": "Stage 4 场景",
        "stage5": "Stage 5 用例",
    }
    safe_task_id = html_escape(task_id)
    stage_cards: List[str] = []
    stages = debug_artifacts.get("stages") or {}
    for stage in ("stage4", "stage5"):
        stage_data = stages.get(stage) or {}
        if not stage_data.get("has_any"):
            continue

        buttons: List[str] = []
        for variant, label in (("raw", "查看原始输出"), ("repaired", "查看修复后输出"), ("other", "查看其它调试文件")):
            count = len(stage_data.get(variant, []))
            if count:
                if inline_preview:
                    buttons.append(
                        f'<button type="button" class="dbg-btn dbg-preview-trigger" data-task-id="{safe_task_id}" data-stage="{stage}" data-variant="{variant}">{label} ({count})</button>'
                    )
                else:
                    buttons.append(
                        f'<a class="dbg-btn" target="_blank" href="/artifact-view/{safe_task_id}?stage={stage}&variant={variant}">{label} ({count})</a>'
                    )
            else:
                buttons.append(f'<span class="dbg-btn disabled">{label}</span>')

        stage_cards.append(
            '<div class="dbg-stage">'
            f'<div class="dbg-stage-title">{stage_titles.get(stage, stage)}</div>'
            f'<div class="dbg-stage-actions">{"".join(buttons)}</div>'
            '</div>'
        )

    if not stage_cards:
        return ""

    body = ''.join(stage_cards)
    helper_text = (
        '可在当前页弹窗预览 stage4/stage5 的原始 LLM 输出与修复后版本，便于排错审计。'
        if inline_preview else
        '可直接查看 stage4/stage5 的原始 LLM 输出与修复后版本，便于排错审计。'
    )
    return (
        '<style>'
        '.dbg-card{background:white;border-radius:10px;box-shadow:0 2px 8px rgba(0,0,0,.06);padding:16px 18px;margin:16px 0;}'
        '.dbg-title{font-size:16px;font-weight:700;color:#1f2937;}'
        '.dbg-help{margin-top:6px;color:#6b7280;font-size:13px;}'
        '.dbg-body{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px;margin-top:14px;}'
        '.dbg-stage{border:1px solid #e5e7eb;border-radius:8px;padding:14px;background:#f8fafc;}'
        '.dbg-stage-title{font-weight:700;color:#334155;margin-bottom:10px;}'
        '.dbg-stage-actions{display:flex;flex-wrap:wrap;gap:8px;}'
        '.dbg-btn{display:inline-flex;align-items:center;justify-content:center;padding:8px 12px;border-radius:8px;text-decoration:none;background:#eff6ff;color:#1d4ed8;border:1px solid #bfdbfe;font-size:12px;font-weight:600;cursor:pointer;font-family:inherit;}'
        '.dbg-btn.disabled{background:#f3f4f6;color:#9ca3af;border-color:#e5e7eb;cursor:not-allowed;}'
        '.dbg-empty{color:#6b7280;padding:12px 4px;}'
        '</style>'
        '<div class="dbg-card">'
        '<div class="dbg-title">🧪 Debug 输出审计</div>'
        f'<div class="dbg-help">{helper_text}</div>'
        f'<div class="dbg-body">{body}</div>'
        '</div>'
    )


def _render_debug_preview_modal() -> str:
    return """
<div class="dbg-modal-mask" id="debug-preview-modal" hidden>
  <div class="dbg-modal" role="dialog" aria-modal="true" aria-labelledby="debug-preview-title">
    <div class="dbg-modal-header">
      <div>
        <div class="dbg-modal-title" id="debug-preview-title">Debug 输出预览</div>
        <div class="dbg-modal-subtitle" id="debug-preview-subtitle">加载中...</div>
      </div>
      <button type="button" class="dbg-modal-close" data-debug-close>×</button>
    </div>
    <div class="dbg-modal-body" id="debug-preview-body">
      <div class="dbg-modal-loading">加载中...</div>
    </div>
  </div>
</div>
<style>
  .dbg-modal-mask{position:fixed;inset:0;background:rgba(15,23,42,.58);display:flex;align-items:center;justify-content:center;padding:24px;z-index:1200;}
  .dbg-modal-mask[hidden]{display:none;} /* v0.5.9 fix: HTML5 hidden 缺 CSS display:none, mask 仍占位拦截 pointer events 致 preview button 永远点不到 */
  .dbg-modal{width:min(1200px,100%);max-height:calc(100vh - 48px);background:#f8fafc;border-radius:14px;box-shadow:0 24px 60px rgba(15,23,42,.35);display:flex;flex-direction:column;overflow:hidden;}
  .dbg-modal-header{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;padding:18px 20px;border-bottom:1px solid #e5e7eb;background:white;}
  .dbg-modal-title{font-size:18px;font-weight:700;color:#0f172a;}
  .dbg-modal-subtitle{margin-top:6px;color:#64748b;font-size:13px;}
  .dbg-modal-close{border:none;background:#eef2ff;color:#1e3a8a;border-radius:999px;width:32px;height:32px;font-size:20px;cursor:pointer;}
  .dbg-modal-body{padding:18px 20px;overflow:auto;background:#f8fafc;}
  .dbg-modal-loading,.dbg-modal-empty,.dbg-modal-error{padding:24px;border-radius:10px;background:white;color:#475569;text-align:center;border:1px dashed #cbd5e1;}
  .dbg-modal-error{color:#b91c1c;border-color:#fecaca;background:#fef2f2;}
  .dbg-modal-file{background:white;border:1px solid #e5e7eb;border-radius:10px;margin-bottom:12px;overflow:hidden;}
  .dbg-modal-file summary{cursor:pointer;list-style:none;padding:14px 16px;font-weight:700;color:#0f172a;display:flex;flex-wrap:wrap;gap:8px;justify-content:space-between;align-items:center;}
  .dbg-modal-file summary::-webkit-details-marker{display:none;}
  .dbg-modal-file[open] summary{border-bottom:1px solid #eef2f7;}
  .dbg-modal-file-meta{font-weight:400;color:#64748b;font-size:12px;}
  .dbg-modal-file pre{margin:0;padding:16px;background:#0f172a;color:#e2e8f0;font-size:12px;line-height:1.58;white-space:pre-wrap;word-break:break-word;overflow:auto;}
  .dbg-modal-file-note{padding:10px 16px;background:#fff7ed;color:#9a3412;border-bottom:1px solid #fdba74;font-size:12px;}
</style>
<script>
(function () {
  if (window.__debugPreviewInit) return;
  window.__debugPreviewInit = true;

  function escapeHtml(value) {
    if (value === null || value === undefined) return '';
    return String(value).replace(/[&<>"']/g, function (ch) {
      return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[ch] || ch;
    });
  }

  function prettyStage(stage) {
    return stage === 'stage5' ? 'Stage 5 用例' : 'Stage 4 场景';
  }

  function prettyVariant(variant) {
    if (variant === 'repaired') return '修复后输出';
    if (variant === 'other') return '其它调试文件';
    return '原始输出';
  }

  function renderFiles(files) {
    if (!files || files.length === 0) {
      return '<div class="dbg-modal-empty">当前分类下暂无调试文件。</div>';
    }
    return files.map(function (file, index) {
      const meta = `${escapeHtml(file.relative_path || '')} · ${escapeHtml(file.size_bytes || 0)} bytes · ${escapeHtml(file.updated_at || '')}`;
      const note = file.truncated
        ? '<div class="dbg-modal-file-note">内容过长，已截断展示前 200000 字符。</div>'
        : '';
      return `
        <details class="dbg-modal-file" ${index === 0 ? 'open' : ''}>
          <summary>
            <span>${escapeHtml(file.file_name || 'artifact')}</span>
            <span class="dbg-modal-file-meta">${meta}</span>
          </summary>
          ${note}
          <pre>${escapeHtml(file.content || '')}</pre>
        </details>
      `;
    }).join('');
  }

  const modal = document.getElementById('debug-preview-modal');
  if (!modal) return;
  const titleEl = document.getElementById('debug-preview-title');
  const subtitleEl = document.getElementById('debug-preview-subtitle');
  const bodyEl = document.getElementById('debug-preview-body');

  function closeModal() {
    modal.hidden = true;
    document.body.style.overflow = '';
  }

  function openModal() {
    modal.hidden = false;
    document.body.style.overflow = 'hidden';
  }

  async function openPreview(taskId, stage, variant) {
    titleEl.textContent = `${prettyStage(stage)} · ${prettyVariant(variant)}`;
    subtitleEl.textContent = '加载中...';
    bodyEl.innerHTML = '<div class="dbg-modal-loading">正在加载调试输出...</div>';
    openModal();
    try {
      const resp = await fetch(`/api/tasks/${encodeURIComponent(taskId)}/artifacts/debug?stage=${encodeURIComponent(stage)}&variant=${encodeURIComponent(variant)}`);
      const data = await resp.json();
      if (!resp.ok) {
        throw new Error(data.detail || resp.status || '加载失败');
      }
      titleEl.textContent = `${data.stage_title || prettyStage(stage)} · ${data.variant_title || prettyVariant(variant)}`;
      subtitleEl.textContent = `Task ${taskId} · 共 ${data.count || 0} 个文件`;
      bodyEl.innerHTML = renderFiles(data.files || []);
    } catch (error) {
      subtitleEl.textContent = `Task ${taskId}`;
      bodyEl.innerHTML = `<div class="dbg-modal-error">加载失败：${escapeHtml(error.message || error)}</div>`;
    }
  }

  document.addEventListener('click', function (event) {
    const trigger = event.target.closest('.dbg-preview-trigger');
    if (trigger) {
      event.preventDefault();
      openPreview(trigger.dataset.taskId, trigger.dataset.stage, trigger.dataset.variant);
      return;
    }
    if (event.target === modal || event.target.closest('[data-debug-close]')) {
      closeModal();
    }
  });

  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape' && !modal.hidden) {
      closeModal();
    }
  });
})();
</script>
"""


def _read_text_preview(path: Path, limit: int = 200000) -> Dict[str, Any]:
    data = path.read_text(encoding="utf-8", errors="replace")
    truncated = len(data) > limit
    if truncated:
        data = data[:limit] + "\n\n... [truncated] ..."
    return {"content": data, "truncated": truncated}


def list_domains() -> List[Dict[str, Any]]:
    """列出所有业务域及其统计"""
    import yaml
    kb_config_path = PROJECT_ROOT / ".config" / "kb-config.yaml"
    domains_info = []

    if not kb_config_path.exists():
        return domains_info

    with open(kb_config_path, encoding='utf-8') as f:
        kb_cfg = yaml.safe_load(f)

    for name, conf in kb_cfg.get("domains", {}).items():
        db_path = PROJECT_ROOT / "domains" / name / "_index.db"
        info = {
            "name": name,
            "description": conf.get("description", ""),
            "sources": len(conf.get("code_sources", [])),
            "indexed": db_path.exists(),
            "stats": None,
        }
        if db_path.exists():
            try:
                with sqlite3.connect(db_path) as conn:
                    cnt = conn.execute(
                        "SELECT COUNT(*) FROM symbols"
                    ).fetchone()[0]
                    info["stats"] = {"total_chunks": cnt}
            except Exception:
                pass
        domains_info.append(info)
    return domains_info


# ============================================================
# API 路由
# ============================================================

@app.get("/")
async def root():
    """返回简单的 HTML UI"""
    html_content = """
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>AI 测试用例生成系统</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         margin: 0; padding: 20px; background: #f5f5f5; }
  .container { max-width: 1200px; margin: 0 auto; }
  .top-nav { display: flex; justify-content: space-between; align-items: center;
             background: #2c3e50; color: white; padding: 12px 20px;
             border-radius: 8px; margin-bottom: 20px; }
  .top-nav h1 { margin: 0; font-size: 18px; border: none; padding: 0; color: white; }
  .top-nav-buttons { display: flex; gap: 8px; }
  .nav-btn { background: #3498db; color: white; text-decoration: none;
             padding: 8px 16px; border-radius: 4px; font-size: 14px;
             transition: background 0.2s; cursor: pointer; border: none; }
  .nav-btn:hover { background: #2980b9; }
  .nav-btn.green { background: #27ae60; }
  .nav-btn.green:hover { background: #229954; }
  .nav-btn.orange { background: #f39c12; }
  .nav-btn.orange:hover { background: #d68910; }
  .nav-btn.purple { background: #9b59b6; }
  .nav-btn.purple:hover { background: #8e44ad; }
  .nav-btn.red { background: #e74c3c; }
  .nav-btn.red:hover { background: #c0392b; }
  h1 { color: #2c3e50; border-bottom: 3px solid #3498db; padding-bottom: 10px; }
  .card { background: white; border-radius: 8px; padding: 20px;
          margin-bottom: 16px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
  .badge { display: inline-block; padding: 4px 10px; border-radius: 12px;
           font-size: 12px; font-weight: 600; }
  .badge-success { background: #d4edda; color: #155724; }
  .badge-running { background: #fff3cd; color: #856404; }
  .badge-failed { background: #f8d7da; color: #721c24; }
  .badge-pending { background: #e2e3e5; color: #383d41; }
  .debug-badges { display: flex; flex-wrap: wrap; gap: 6px; min-width: 180px; }
  .debug-chip { display: inline-flex; align-items: center; padding: 3px 8px;
                border-radius: 999px; font-size: 11px; font-weight: 700;
                border: 1px solid transparent; white-space: nowrap; }
  .debug-chip.none { background: #f3f4f6; color: #9ca3af; }
  .debug-chip.raw { background: #eff6ff; color: #1d4ed8; border-color: #bfdbfe; }
  .debug-chip.repaired { background: #fff7ed; color: #c2410c; border-color: #fdba74; }
  .debug-chip.other { background: #fef2f2; color: #b91c1c; border-color: #fecaca; }
  table { width: 100%; border-collapse: collapse; }
  th, td { padding: 10px; text-align: left; border-bottom: 1px solid #eee; }
  th { background: #f8f9fa; font-weight: 600; color: #555; }
  a { color: #3498db; text-decoration: none; }

  /* v0.5.6: Toast notification (右下角弹窗) */
  .toast-notification {
    position: fixed; bottom: 20px; right: 20px; z-index: 9999;
    background: white; padding: 16px 20px; border-radius: 8px;
    box-shadow: 0 4px 16px rgba(0,0,0,0.2);
    border-left: 4px solid #f39c12;
    min-width: 280px; max-width: 360px;
    animation: slideInRight 0.3s ease-out;
  }
  .toast-notification strong { color: #f39c12; font-size: 14px; }
  /* v0.5.6+: 待审核按钮闪烁提示 */
  .nav-btn.pulse { animation: reviewPulse 1.5s ease-in-out infinite;
                   box-shadow: 0 0 0 0 rgba(243, 156, 18, 0.7); }
  @keyframes reviewPulse {
    0%   { box-shadow: 0 0 0 0 rgba(243, 156, 18, 0.7); }
    70%  { box-shadow: 0 0 0 8px rgba(243, 156, 18, 0); }
    100% { box-shadow: 0 0 0 0 rgba(243, 156, 18, 0); }
  }
  .toast-notification .toast-link {
    display: inline-block; margin-top: 8px; padding: 4px 10px;
    background: #3498db; color: white !important; border-radius: 3px;
    text-decoration: none; font-size: 12px;
  }
  .toast-notification .toast-close {
    float: right; color: #999; text-decoration: none; padding: 0 4px;
    font-size: 14px;
  }
  .toast-notification .toast-close:hover { color: #333; }
  .toast-notification .toast-ignore { color: #999; font-size: 11px; text-decoration: none; margin-left: 8px; }
  .toast-notification .toast-ignore:hover { color: #e74c3c; }
  @keyframes slideInRight {
    from { transform: translateX(110%); opacity: 0; }
    to   { transform: translateX(0); opacity: 1; }
  }
  a:hover { text-decoration: underline; }
  .empty { text-align: center; padding: 40px; color: #999; }
  .timeline { position: relative; padding-left: 30px; }
  .timeline-item { position: relative; padding-bottom: 16px; }
  .timeline-item:before { content: ''; position: absolute; left: -22px; top: 6px;
                          width: 10px; height: 10px; border-radius: 50%;
                          background: #3498db; }
  .timeline-item:after { content: ''; position: absolute; left: -18px; top: 16px;
                         bottom: -10px; width: 2px; background: #ddd; }
  .timeline-item:last-child:after { display: none; }
</style>
</head>
<body>
<div class="container">
  <div class="top-nav">
    <h1>🧪 AI 测试用例生成系统 - 可观测 UI</h1>
    <div class="top-nav-buttons">
      <a class="nav-btn green" href="/requirements">📝 新建需求</a>
      <button class="nav-btn orange" onclick="goToCurrent('flows')">🔄 状态流程</button>
      <button class="nav-btn purple" id="nav-review-btn" onclick="goToReview()">👤 人工审核</button>
      <button class="nav-btn" onclick="goToCurrent('task')">📋 测试用例</button>
    </div>
  </div>
  <div class="card">
    <h2>📦 业务域</h2>
    <div id="domains">加载中...</div>
  </div>
  <div class="card" id="review-card" style="display:none;background:#fff8e1;border-left:4px solid #f39c12;">
      <h2>🔔 待审核 (<span id="review-count">0</span>)</h2>
      <div id="review-list">加载中...</div>
    </div>
    <div class="card">
      <h2>📋 最近任务</h2>
      <div id="tasks">加载中...</div>
    </div>
  <div class="card">
    <h2>ℹ️ 系统信息</h2>
    <p>API 文档: <a href="/docs">/docs</a></p>
    <p>实时推送: <code>GET /events</code> (SSE)</p>
  </div>
</div>
<script>
async function loadDomains() {
  const r = await fetch('/api/domains');
  const domains = await r.json();
  const html = domains.length === 0
    ? '<div class="empty">暂无业务域, 请运行 <code>testcase kb build landing-page</code></div>'
    : '<table><tr><th>名称</th><th>描述</th><th>代码源</th><th>Chunks</th><th>索引状态</th></tr>' +
      domains.map(d => `
        <tr>
          <td><strong>${d.name}</strong></td>
          <td>${d.description}</td>
          <td>${d.sources}</td>
          <td>${d.stats ? d.stats.total_chunks : '-'}</td>
          <td><span class="badge ${d.indexed ? 'badge-success' : 'badge-pending'}">
            ${d.indexed ? '已索引' : '未索引'}
          </span></td>
        </tr>
      `).join('') + '</table>';
  document.getElementById('domains').innerHTML = html;
}

async function loadTasks() {
  try {
    const r = await fetch('/api/tasks?limit=20');
    const tasks = await r.json();
    applyTasks(tasks);
  } catch (e) {
    console.warn('loadTasks failed:', e);
  }
}

function renderDebugStatus(task) {
  const counts = task.debug_artifact_counts || {};
  const chips = [];
  for (const [stageKey, stageLabel] of [['stage4', 'S4'], ['stage5', 'S5']]) {
    const stage = counts[stageKey] || {};
    if (stage.raw) chips.push(`<span class="debug-chip raw">${stageLabel} 原始 ${stage.raw}</span>`);
    if (stage.repaired) chips.push(`<span class="debug-chip repaired">${stageLabel} 修复 ${stage.repaired}</span>`);
    if (stage.other) chips.push(`<span class="debug-chip other">${stageLabel} 其它 ${stage.other}</span>`);
  }
  return chips.length
    ? `<div class="debug-badges">${chips.join('')}</div>`
    : '<span class="debug-chip none">—</span>';
}

function applyTasks(tasks) {
  // v0.5.7: extracted render logic, SSE 也调用此函数
  console.log('[debug-journal] applyTasks called, tasks count:', tasks.length);
  window.__lastTasks = tasks;
  window.__currentTaskId = tasks.length > 0 ? tasks[0].task_id : null;

  // v0.5.7+: 同步刷新待审核专区
  const awaiting = tasks.filter(t => (t.status || '').startsWith('awaiting_review'));
  console.log('[debug-journal] awaiting items count:', awaiting.length, awaiting.map(t => t.req_id));
  renderAwaitingReview(awaiting);

  // v0.5.6: 状态变更检测 + 主动通知 (toast + 浏览器推送)
  if (!window.__taskHistory) window.__taskHistory = {};
  const currentMap = {};
  for (const t of tasks) {
    currentMap[t.task_id] = t.status;
    const oldStatus = window.__taskHistory[t.task_id];
    // 状态从非审核态变成审核态 -> 触发通知
    if (oldStatus && oldStatus !== t.status && t.status && t.status.startsWith('awaiting_review')) {
      notifyReview(t);
    } else if (!oldStatus && t.status && t.status.startsWith('awaiting_review')) {
      // 首次发现已在审核态 (历史 task) 也通知
      notifyReview(t);
    }
  }
  window.__taskHistory = currentMap;

  const html = tasks.length === 0
    ? '<div class="empty">暂无任务</div>'
    : '<table><tr><th>Task ID</th><th>需求</th><th>状态</th><th>创建时间</th><th>Debug</th><th>操作</th></tr>' +
      tasks.map(t => {
        const reviewUrl = reviewUrlForStatus(t.status, t.task_id);
        const reviewCls = t.status && t.status.startsWith('awaiting_review') ? 'pulse' : '';
        const reviewLabel = t.status === 'awaiting_review_links' ? '🗂️'
          : t.status === 'awaiting_review_scenarios' ? '🎬'
          : '👤';
        // v0.5.7+: failed 状态显示「编辑重提」按钮 - 回填到编辑页, 不是 pipeline retry
                // 也对 success/其他状态开放编辑入口 (改完重提会新建 task)
                const editReqBtn = `<a class="nav-btn blue" style="padding:4px 10px;font-size:12px;" href="/requirement?req_id=${encodeURIComponent(t.req_id)}&task_id=${encodeURIComponent(t.task_id)}" title="把此需求的原始参数回填到编辑页, 修改后重新提交">📝</a>`;
                return `
                <tr>
                  <td><a href="/task/${t.task_id}">${t.task_id.substring(0, 30)}...</a></td>
                  <td>${t.req_id}</td>
                  <td><span class="badge badge-${t.status === 'success' ? 'success' : t.status === 'failed' ? 'failed' : t.status === 'running' ? 'running' : 'pending'}">${t.status}</span></td>
                  <td>${t.created_at}</td>
                  <td>${renderDebugStatus(t)}</td>
                  <td style="white-space:nowrap;">
                    <a class="nav-btn" style="padding:4px 10px;font-size:12px;" href="/task/${t.task_id}">📋</a>
                    <a class="nav-btn orange" style="padding:4px 10px;font-size:12px;" href="/flows/${t.task_id}">🔄</a>
                    <a class="nav-btn purple ${reviewCls}" style="padding:4px 10px;font-size:12px;" href="${reviewUrl}" title="去 ${t.status} 审核">${reviewLabel}</a>
                    ${editReqBtn}
                  </td>
                </tr>
              `;
      }).join('') + '</table>';
  document.getElementById('tasks').innerHTML = html;
}

function renderAwaitingReview(items) {
  // v0.5.7+: 渲染首页'待审核'专区
  console.log('[debug-journal] renderAwaitingReview called, items:', items.length, items.map(t => t.req_id));
  const card = document.getElementById('review-card');
  const list = document.getElementById('review-list');
  const cnt = document.getElementById('review-count');
  if (!card || !list) { console.warn('[debug-journal] review-card or review-list not found in DOM'); return; }
  cnt.textContent = items.length;
  if (items.length === 0) {
    card.style.display = 'none';
    return;
  }
  card.style.display = 'block';
  const stageLabel = {
    'awaiting_review_links': '🗂️ Stage 3 召回',
    'awaiting_review_scenarios': '🎬 Stage 4 场景',
    'awaiting_review_cases': '👤 Stage 5 用例',
  };
  list.innerHTML = items.map(t => {
    const reviewUrl = reviewUrlForStatus(t.status, t.task_id);
    return `<div style="padding:6px 0;border-bottom:1px dashed #eee;">
      <b>${stageLabel[t.status] || t.status}</b> ·
      <a href="/task/${t.task_id}">${t.task_id.substring(0, 30)}...</a>
      (req: <code>${t.req_id}</code>) ·
      <a href="${reviewUrl}" style="color:#e74c3c;font-weight:600;">立即处理 →</a>
    </div>`;
  }).join('');
}

async function retryTask(taskId) {
  // v0.5.7: 重试失败的 task
  if (!confirm(`重试 task ${taskId.substring(0, 24)}...?\n\n将:\n- 复用已成功 stage 的产物\n- 只重跑失败的 stage\n- 新建一个 task_id (原失败 task 保留作审计)`)) {
    return;
  }
  try {
    const r = await fetch(`/api/tasks/${taskId}/retry`, { method: 'POST' });
    const data = await r.json();
    if (!r.ok || !data.ok) {
      alert('重试启动失败: ' + (data.detail || JSON.stringify(data)));
      return;
    }
    alert(`✅ 后台已启动重试\nPID: ${data.pid}\n日志: ${data.log_path}\n\n新 task 完成后会出现在首页列表`);
    loadTasks();  // 立刻刷新一次
  } catch (e) {
    alert('请求失败: ' + e.message);
  }
}

function reviewUrlForStatus(status, taskId) {
  // v0.5.6+: 根据 task.status 路由到对应 stage 审核页
  // 任意 awaiting_review_* 态 -> 精确路由;非审核态 -> 默认 Stage 5
  if (status === 'awaiting_review_links') return '/review_links/' + taskId;
  if (status === 'awaiting_review_scenarios') return '/review_scenarios/' + taskId;
  if (status === 'awaiting_review_cases') return '/review/' + taskId;
  return '/review/' + taskId;
}

function goToReview() {
  // 优先用 __currentTaskId (有当前 task 时),否则找最新一个 awaiting_review 的 task
  let tid = window.__currentTaskId;
  let targetStatus = null;
  if (tid && window.__lastTasks) {
    const cur = window.__lastTasks.find(t => t.task_id === tid);
    if (cur) targetStatus = cur.status;
  }
  // 如果当前 task 不在审核态,看 __lastTasks 里有没有等待审核的 task (有就跳那个)
  if (!targetStatus || !targetStatus.startsWith('awaiting_review')) {
    if (window.__lastTasks) {
      const pending = window.__lastTasks.find(t => t.status && t.status.startsWith('awaiting_review'));
      if (pending) { tid = pending.task_id; targetStatus = pending.status; }
    }
  }
  if (!tid) {
    alert('暂无任务, 请先创建需求');
    location.href = '/requirements';
    return;
  }
  location.href = reviewUrlForStatus(targetStatus, tid);
}

function notifyReview(task) {
  // v0.5.6: 双层通知 - toast + 浏览器推送
  // v0.5.7+: 增加「忽略 1 小时」按钮 (localStorage 持久化)
  console.log('[debug-journal] notifyReview called for task:', task.task_id, 'status:', task.status);
  const stageMap = {
    'awaiting_review_links': 'Stage 3 召回',
    'awaiting_review_scenarios': 'Stage 4 场景',
    'awaiting_review_cases': 'Stage 5 用例'
  };
  const stage = stageMap[task.status] || task.status;
  const url = reviewUrlForStatus(task.status, task.task_id);

  // 检查是否在 ignore 列表里 (本 task_id+status 1 小时内)
  if (isTaskIgnored(task.task_id, task.status)) {
    return;  // 静默跳过, 不弹 toast/通知
  }

  // Toast 弹窗
  const toast = document.createElement('div');
  toast.className = 'toast-notification';
  toast.innerHTML = '<strong>🔔 ' + stage + ' 需要审核</strong><br>' +
    '<span style="font-size:12px;">Task: ' + task.task_id.substring(0, 30) + '...</span><br>' +
    '<a href="' + url + '" class="toast-link">去审核 →</a> ' +
    '<a href="#" class="toast-ignore" data-ignore-task="' + task.task_id + '" data-ignore-status="' + task.status + '">忽略 1 小时</a> ' +
    '<a href="#" class="toast-close" onclick="this.parentElement.remove(); return false;">✕</a>';
  document.body.appendChild(toast);
  // 绑定忽略按钮
  const ignoreBtn = toast.querySelector('.toast-ignore');
  if (ignoreBtn) {
    ignoreBtn.addEventListener('click', function(e) {
      e.preventDefault();
      ignoreTask(task.task_id, task.status, 3600);
      toast.remove();
    });
  }
  // 10 秒后自动消失
  setTimeout(function() {
    if (toast.parentElement) toast.remove();
  }, 10000);

  // 浏览器系统通知 (如果用户授权)
  if (typeof Notification !== 'undefined' && Notification.permission === 'granted') {
    try {
      new Notification('🔔 ' + stage + ' 需要审核', {
        body: 'Task ' + task.task_id.substring(0, 40) + ' / ' + stage + ' - 点击查看并审核',
        icon: '/favicon.ico',
        tag: 'review-' + task.task_id
      }).onclick = function() {
        window.open(url, '_blank');
      };
    } catch (e) {
      console.warn('Notification failed:', e);
    }
  }
}

// v0.5.7+: 任务级「忽略 1 小时」管理 (localStorage)
// 存储: { "ignored_tasks": { "<task_id>|<status>": <expires_unix_ts> } }
function getIgnoreStore() {
  try {
    const raw = localStorage.getItem('ignored_tasks');
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    // 清理过期项
    const now = Math.floor(Date.now() / 1000);
    const cleaned = {};
    for (const k of Object.keys(parsed)) {
      if (parsed[k] > now) cleaned[k] = parsed[k];
    }
    return cleaned;
  } catch (e) {
    return {};
  }
}

function saveIgnoreStore(store) {
  try {
    localStorage.setItem('ignored_tasks', JSON.stringify(store));
  } catch (e) {
    console.warn('localStorage save failed:', e);
  }
}

function ignoreTask(taskId, status, seconds) {
  const key = taskId + '|' + status;
  const store = getIgnoreStore();
  store[key] = Math.floor(Date.now() / 1000) + seconds;
  saveIgnoreStore(store);
}

function isTaskIgnored(taskId, status) {
  const key = taskId + '|' + status;
  const store = getIgnoreStore();
  return store[key] && store[key] > Math.floor(Date.now() / 1000);
}

function goToCurrent(kind) {
  if (!window.__currentTaskId) {
    alert('暂无任务, 请先创建需求');
    location.href = '/requirements';
    return;
  }
  location.href = '/' + kind + '/' + window.__currentTaskId;
}

loadDomains();
loadTasks();
// v0.5.6: 请求浏览器通知权限 (用户首次访问)
if (typeof Notification !== 'undefined' && Notification.permission === 'default') {
  // 延迟 3 秒请求权限, 避免立刻打扰
  setTimeout(function() {
    Notification.requestPermission().then(function(perm) {
      console.log('Notification permission:', perm);
    });
  }, 3000);
  }
  // v0.5.7+: 用 SSE 替代 5s polling, 实时响应 task 变化
  // v0.5.9+: 把 setInterval ID 提到 module 顶部, 避免 onerror + catch 双路径
  // 创建多个 timer 导致轮询风暴 (历史表现为日志内 30 秒出现 17 次轮询).
  const __loadTasksTimer = { current: null };
  let __sseFailed = false;

  function startPolling() {
    if (__loadTasksTimer.current !== null) return;
    __loadTasksTimer.current = setInterval(() => loadTasks(), 5000);
    console.log('[debug-journal] polling started, every 5s');
  }

  function stopPolling() {
    if (__loadTasksTimer.current !== null) {
      clearInterval(__loadTasksTimer.current);
      __loadTasksTimer.current = null;
      console.log('[debug-journal] polling cleared');
    }
  }

  function startSSE() {
    if (__sseFailed) return;
    try {
      const es = new EventSource('/events');
      console.log('[debug-journal] SSE connection opened');
      es.addEventListener('tasks_update', function(ev) {
        console.log('[debug-journal] SSE tasks_update received, data len:', ev.data.length);
        try {
          const tasks = JSON.parse(ev.data);
          applyTasks(tasks);
        } catch (e) { console.warn('SSE parse error:', e); }
      });
      es.onerror = function() {
        console.warn('[debug-journal] SSE connection lost, falling back to polling');
        try { es.close(); } catch (e) {}
        __sseFailed = true;
        startPolling();
      };
      window.__sse = es;
    } catch (e) {
      console.warn('[debug-journal] SSE init failed, using polling:', e);
      __sseFailed = true;
      startPolling();
    }
  }

  // 启动 SSE (失败则自动 fallback 到 polling)
  startSSE();
</script>
</body>
</html>
"""
    return HTMLResponse(content=html_content)


@app.get("/api/tasks/awaiting-review")
async def api_awaiting_review_tasks(limit: int = Query(20, ge=1, le=100)):
    """v0.5.7+: 列出所有 awaiting_review_* 状态的任务, 用于首页'待审核'专区."""
    from src.workspace import TASK_STATUSES
    all_tasks = list_tasks(limit=200)
    awaiting = [t for t in all_tasks
                if t.get("status", "").startswith("awaiting_review")]
    return awaiting[:limit]


@app.get("/api/tasks")
async def api_tasks(
    limit: int = Query(50, ge=1, le=500),
    status: Optional[str] = Query(None, description="v0.5.7+: 过滤 status (支持前缀匹配, e.g. 'awaiting' 匹配所有 awaiting_review_*)"),
):
    """获取任务列表 (v0.5.7+: 支持 ?status= 前缀过滤, 附带 debug 产物摘要)"""
    tasks = list_tasks(limit=200)  # 多取一些, 过滤后给 limit
    if status:
        tasks = [t for t in tasks
                 if (t.get("status", "") or "").startswith(status)]
    tasks = tasks[:limit]
    return [_enrich_task_debug_summary(t) for t in tasks]


# ============================================================
# Stage / node 解析工具 (P1 状态流程页用)
# ============================================================

def parse_workflow_from_task(task_id: str) -> Optional[Dict[str, Any]]:
    """解析 task 的 workflow 数据 - 给前端流程图用.

    Returns:
        {
          "task_id", "req_id", "status",
          "stages": [
            {"stage_name": "stage2_keywords", "status": "success",
             "started_at", "finished_at", "duration_ms",
             "nodes": [{"node_id", "status", "started_at", "finished_at",
                       "duration_ms", "error"}],
             "artifacts": ["stage2/keywords.json"]},
            ...
          ],
          "llm_calls": [
            {"timestamp", "model", "purpose", "input_tokens",
             "output_tokens", "duration_ms"},
            ...
          ],
          "recalls": [
            {"chunk_id", "name", "qualified_name", "file_path",
             "line_start", "relevance"},
            ...
          ],
          "scenarios": [
            {"scenario_id", "title", "scenario_type", "priority"},
            ...
          ],
          "cases_summary": {"total": 36, "by_priority": {"P0": 12, ...}}
        }
    """
    ws_mgr = WorkspaceManager(PROJECT_ROOT / "runs")
    ws = ws_mgr.get_task(task_id)
    if not ws:
        return None

    task_dir = ws.root

    # Stages + nodes
    stages_data = []
    with sqlite3.connect(ws.db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM task WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if not row:
            return None
        task_meta = dict(row)
        req_id = task_meta.get("req_id") or task_dir.parent.name
        conn.row_factory = sqlite3.Row
        stages = conn.execute(
            "SELECT * FROM stage WHERE task_id=? ORDER BY started_at",
            (task_id,)
        ).fetchall()

        for s in stages:
            sd = dict(s)
            nodes = conn.execute(
                """SELECT n.* FROM node n
                   JOIN stage s2 ON n.stage_name = s2.stage_name AND n.task_id = s2.task_id
                   WHERE s2.task_id=? AND s2.stage_name=?
                   ORDER BY n.started_at""",
                (task_id, sd["stage_name"])
            ).fetchall()
            sd["nodes"] = [dict(n) for n in nodes]
            # 计算每个 node duration
            for n in sd["nodes"]:
                if n.get("started_at") and n.get("finished_at"):
                    try:
                        from datetime import datetime as _dt
                        s_time = _dt.fromisoformat(n["started_at"])
                        f_time = _dt.fromisoformat(n["finished_at"])
                        n["duration_ms"] = int((f_time - s_time).total_seconds() * 1000)
                    except Exception:
                        pass
                else:
                    n["duration_ms"] = None
            # 找 artifact 文件
            stage_dir = task_dir / sd["stage_name"].split('.')[0]
            sd["artifacts"] = []
            if stage_dir.exists():
                sd["artifacts"] = [f.name for f in stage_dir.iterdir() if f.is_file()]
            # stage duration
            if sd.get("started_at") and sd.get("finished_at"):
                try:
                    from datetime import datetime as _dt
                    s_t = _dt.fromisoformat(sd["started_at"])
                    f_t = _dt.fromisoformat(sd["finished_at"])
                    sd["duration_ms"] = int((f_t - s_t).total_seconds() * 1000)
                except Exception:
                    sd["duration_ms"] = None
            stages_data.append(sd)

        # LLM calls
        llms = conn.execute(
            "SELECT * FROM llm_call WHERE task_id=? ORDER BY timestamp",
            (task_id,)
        ).fetchall()
        llm_calls = []
        for l in llms:
            ld = dict(l)
            if ld.get("duration_ms"):
                ld["duration_ms"] = int(ld["duration_ms"])
            llm_calls.append(ld)

    # Recalls from stage3/links.json
    recalls = []
    links_path = task_dir / "stage3" / "links.json"
    if links_path.exists():
        try:
            links = json.loads(links_path.read_text(encoding="utf-8"))
            for l in links:
                cid = l.get("chunk_id", "")
                recalls.append({
                    "chunk_id": cid,
                    "relevance": l.get("relevance"),
                    "link_type": l.get("link_type", ""),
                    "reason": l.get("reason", "")[:120],
                })
        except Exception:
            pass

    # Scenarios from stage4/scenarios.json
    scenarios = []
    scen_path = task_dir / "stage4" / "scenarios.json"
    if scen_path.exists():
        try:
            data = json.loads(scen_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data = data.get("scenarios", data.get("items", []))
            if not isinstance(data, list):
                data = []
            for s in data:
                if isinstance(s, dict):
                    scenarios.append({
                        "scenario_id": s.get("scenario_id", ""),
                        "title": s.get("title", "")[:80],
                        "scenario_type": s.get("scenario_type", ""),
                        "priority": s.get("priority", ""),
                        "related_chunks_count": len(s.get("related_chunks", [])),
                    })
        except Exception:
            pass

    # Cases summary from stage5/cases.json
    cases_summary = {"total": 0, "by_priority": {}, "success": 0, "failed": 0}
    cases_path = task_dir / "stage5" / "cases.json"
    if cases_path.exists():
        try:
            cases = json.loads(cases_path.read_text(encoding="utf-8"))
            cases_summary["total"] = len(cases)
            for c in cases:
                p = c.get("priority", "P2")
                cases_summary["by_priority"][p] = cases_summary["by_priority"].get(p, 0) + 1
            cases_summary["success"] = sum(1 for c in cases if not c.get("raw_text"))
            cases_summary["fallback"] = sum(1 for c in cases if c.get("raw_text"))
        except Exception:
            pass

    debug_artifacts = _build_task_debug_artifacts(task_dir)

    return {
        "task_id": task_id,
        "req_id": req_id,
        "status": task_meta.get("status", ""),
        "created_at": task_meta.get("created_at", ""),
        "updated_at": task_meta.get("updated_at", ""),
        "total_duration_ms": task_meta.get("total_duration_ms"),
        "stages": stages_data,
        "llm_calls": llm_calls,
        "recalls": recalls,
        "scenarios": scenarios,
        "cases_summary": cases_summary,
        "debug_artifacts": debug_artifacts,
        "task_dir": str(task_dir.relative_to(PROJECT_ROOT)),
    }


# ============================================================
# 人工审核 (P3 用)
# ============================================================

def get_cases_for_review(task_id: str) -> List[Dict[str, Any]]:
    """获取 task 的用例列表 (含 human_approved 状态)

    用 parse_workflow_from_task 解析正确的 task_dir,
    避免把 task_id 当成 task_dir 错误拼接.
    """
    cases_path = _resolve_cases_path(task_id)
    if not cases_path or not cases_path.exists():
        return []
    try:
        cases = json.loads(cases_path.read_text(encoding="utf-8"))
        for c in cases:
            c.setdefault("human_approved", False)
            c.setdefault("review_comment", "")
        return cases
    except Exception:
        return []


def _resolve_cases_path(task_id: str) -> Optional[Path]:
    """task_id → task_dir/stage5/cases.json 的解析"""
    workflow = parse_workflow_from_task(task_id)
    if workflow and workflow.get("task_dir"):
        return PROJECT_ROOT / workflow["task_dir"] / "stage5" / "cases.json"
    if "runs" in Path(task_id).parts or "/" in task_id or "\\" in task_id:
        return PROJECT_ROOT / task_id / "stage5" / "cases.json"
    return PROJECT_ROOT / "runs" / task_id / "stage5" / "cases.json"


def _save_approved_cases(task_id: str, cases: List[Dict[str, Any]]) -> None:
    """保存批准后的 cases 到 cases.json"""
    workflow = parse_workflow_from_task(task_id)
    if not workflow or not workflow.get("task_dir"):
        return
    cases_path = PROJECT_ROOT / workflow["task_dir"] / "stage5" / "cases.json"
    if not cases_path.exists():
        return
    cases_path.write_text(
        json.dumps(cases, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def get_links_for_review(task_id: str) -> List[Dict[str, Any]]:
    """v0.5.4: 获取 stage3 links 列表 (含 human_approved)"""
    workflow = parse_workflow_from_task(task_id)
    if not workflow or not workflow.get("task_dir"):
        return []
    links_path = Path(PROJECT_ROOT) / workflow["task_dir"] / "stage3" / "links.json"
    if not links_path.exists():
        return []
    try:
        links = json.loads(links_path.read_text(encoding="utf-8"))
        for link in links:
            link.setdefault("human_approved", False)
            link.setdefault("review_comment", "")
        return links
    except Exception:
        return []


def get_scenarios_for_review(task_id: str) -> List[Dict[str, Any]]:
    """v0.5.4: 获取 stage4 scenarios 列表 (含 human_approved)"""
    workflow = parse_workflow_from_task(task_id)
    if not workflow or not workflow.get("task_dir"):
        return []
    sc_path = Path(PROJECT_ROOT) / workflow["task_dir"] / "stage4" / "scenarios.json"
    if not sc_path.exists():
        return []
    try:
        scenarios = json.loads(sc_path.read_text(encoding="utf-8"))
        for sc in scenarios:
            sc.setdefault("human_approved", False)
            sc.setdefault("review_comment", "")
        return scenarios
    except Exception:
        return []


def _save_approved_links(task_id: str, links: List[Dict[str, Any]]) -> None:
    """v0.5.4: 保存 links back to disk"""
    workflow = parse_workflow_from_task(task_id)
    if not workflow or not workflow.get("task_dir"):
        return
    links_path = Path(PROJECT_ROOT) / workflow["task_dir"] / "stage3" / "links.json"
    if not links_path.exists():
        return
    links_path.write_text(
        json.dumps(links, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def _save_approved_scenarios(task_id: str, scenarios: List[Dict[str, Any]]) -> None:
    """v0.5.4: 保存 scenarios back to disk"""
    workflow = parse_workflow_from_task(task_id)
    if not workflow or not workflow.get("task_dir"):
        return
    sc_path = Path(PROJECT_ROOT) / workflow["task_dir"] / "stage4" / "scenarios.json"
    if not sc_path.exists():
        return
    sc_path.write_text(
        json.dumps(scenarios, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def _launch_retry_background(
    old_task_id: str,
    new_task_id: str,
    domain_override: Optional[str] = None,
) -> Dict[str, Any]:
    """启动续跑后台进程, 立即返回 pid/log_path."""
    log_path = PROJECT_ROOT / "runs" / f"retry_{new_task_id[:24]}_{_uuid.uuid4().hex[:6]}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    env_extra = {**os.environ}
    env_extra["PYTHONIOENCODING"] = "utf-8"
    env_extra["PYTHONUTF8"] = "1"
    env_extra["YIYE_RETRY_OLD_TASK_ID"] = old_task_id
    env_extra["YIYE_RETRY_NEW_TASK_ID"] = new_task_id
    if domain_override:
        env_extra["YIYE_RETRY_DOMAIN"] = str(domain_override)

    cmd = [
        str(PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"),
        "-u",
        "-c",
        (
            f"import os, sys; sys.path.insert(0, r'{PROJECT_ROOT}'); "
            f"from pathlib import Path; "
            f"from src.generate import continue_retry_task; "
            f"r = continue_retry_task("
            f"os.environ['YIYE_RETRY_OLD_TASK_ID'], "
            f"os.environ['YIYE_RETRY_NEW_TASK_ID'], "
            f"Path(r'{PROJECT_ROOT}'), "
            f"domain_override=os.environ.get('YIYE_RETRY_DOMAIN')); "
            f"print('RETRY_RESULT:', r); "
            f"sys.exit(0 if r.get('ok') else 1)"
        ),
    ]
    log_path.write_text(f"$ {' '.join(cmd)}\n", encoding="utf-8")
    log_file_handle = open(log_path, "a", encoding="utf-8")  # BUG-5/10 fix
    proc = _sp.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        stdout=log_file_handle,
        stderr=_sp.STDOUT,
        env=env_extra,
        creationflags=0x00000008,
    )
    # BUG-5 fix: 注册到全局表
    _BG_PROCESSES[proc.pid] = {
        "kind": "retry",
        "pid": proc.pid,
        "task_id": new_task_id,
        "old_task_id": old_task_id,
        "started_at": datetime.now().isoformat(),
        "log_path": str(log_path.relative_to(PROJECT_ROOT)),
        "_proc": proc,
        "_log_handle": log_file_handle,  # BUG-10 fix
    }
    return {
        "pid": proc.pid,
        "log_path": str(log_path.relative_to(PROJECT_ROOT)),
    }

@app.post("/api/tasks/{task_id}/advance-stage")
async def api_advance_stage(task_id: str, payload: Dict[str, Any]):
    """v0.5: 审核通过 + 推进到下一 stage.

    Body: { "stage": "stage3" | "stage4" | "stage5" | "auto", "approved": true/false, "background"?: true }

    Returns:
        { "ok": true, "new_status": "...", "task_id": "..." }

    Logic:
    1. 检查 task 状态是否为 awaiting_review_*
    2. 用户批准 (approved=True) → stage3/4 创建 continuation task, stage5 变 success
    3. 用户拒绝 (approved=False) → 状态保持 + audit log
    """
    requested_stage = payload.get("stage", "auto")
    approved = bool(payload.get("approved", True))
    background = bool(payload.get("background", True))
    domain_override = payload.get("domain")

    ws_mgr = WorkspaceManager(PROJECT_ROOT / "runs")
    ws = ws_mgr.get_task(task_id)
    if not ws:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")

    task_db = ws.db_path
    try:
        con = sqlite3.connect(str(task_db))
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM task WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            con.close()
            raise HTTPException(status_code=404, detail="task not in db")
        current_status = row["status"]
        req_id_db = row["req_id"]
        con.close()
    except sqlite3.OperationalError as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e}")

    status_to_stage = {v: k for k, v in STAGE_TO_REVIEW_STATUS.items()}
    current_stage = status_to_stage.get(current_status)
    if not current_stage:
        raise HTTPException(
            status_code=400,
            detail=f"task 不在审核态. current_status={current_status}",
        )

    if requested_stage not in ("auto", current_stage):
        raise HTTPException(
            status_code=400,
            detail=f"stage 不匹配. requested={requested_stage}, current={current_stage}",
        )

    if not approved:
        return {
            "ok": True,
            "approved": False,
            "new_status": current_status,
            "stage": current_stage,
            "task_id": task_id,
            "req_id": req_id_db,
            "message": "审核未通过, 状态保持不变. 修复后再次提交.",
        }

    partial_summary = None
    if current_stage == "stage5":
        try:
            cases = get_cases_for_review(task_id)
            n_ok = sum(1 for c in cases if c.get("human_approved") is True)
            n_no = sum(1 for c in cases if c.get("human_approved") is False and c.get("reviewed_at"))
            n_pending = sum(1 for c in cases if c.get("human_approved") not in (True, False))
            partial_summary = {"approved": n_ok, "rejected": n_no, "pending": n_pending}
        except Exception as e:
            print(f"[partial_summary] failed: {e}")

    new_task_id = None
    skipped_stages = []
    effective_task_id = task_id
    continuation_error = None
    launch_info: Dict[str, Any] = {}

    if current_stage in ("stage3", "stage4"):
        try:
            from src.generate import prepare_retry_task, retry_failed_task

            if background:
                prepared = prepare_retry_task(task_id, PROJECT_ROOT, domain_override=domain_override)
                if not prepared.get("ok"):
                    continuation_error = prepared.get("error", "未知错误")
                else:
                    new_task_id = prepared.get("new_task_id")
                    skipped_stages = prepared.get("skipped_stages", [])
                    effective_task_id = new_task_id or task_id
                    try:
                        launch_info = _launch_retry_background(
                            old_task_id=task_id,
                            new_task_id=new_task_id,
                            domain_override=domain_override,
                        )
                    except Exception as launch_exc:
                        continuation_error = str(launch_exc)
                        try:
                            failed_ws = ws_mgr.get_task(new_task_id)
                            if failed_ws:
                                failed_ws.update_task_status("failed")
                        except Exception:
                            pass
            else:
                ret = retry_failed_task(task_id, PROJECT_ROOT, domain_override=domain_override)
                if not ret.get("ok"):
                    continuation_error = ret.get("error", "未知错误")
                else:
                    new_task_id = ret.get("new_task_id")
                    skipped_stages = ret.get("skipped_stages", [])
                    effective_task_id = new_task_id or task_id
        except Exception as e:
            continuation_error = str(e)

        if continuation_error:
            raise HTTPException(
                status_code=500,
                detail=f"审核已记录, 但继续执行失败: {continuation_error}",
            )

        new_status = "partial"
        if background:
            message = (
                f"✅ Stage {current_stage} 审核通过, 已创建续跑任务 {new_task_id} 并转入后台执行 "
                f"(跳过 {', '.join(skipped_stages) or '无'})"
            )
        else:
            message = (
                f"✅ Stage {current_stage} 审核通过, 已创建续跑任务 {new_task_id} "
                f"(跳过 {', '.join(skipped_stages) or '无'})"
            )
    else:
        new_status = "success"
        message = f"✅ Stage {current_stage} 审核通过, 状态变 {new_status}"
        if partial_summary:
            n_ok = partial_summary.get("approved", 0)
            n_no = partial_summary.get("rejected", 0)
            n_pd = partial_summary.get("pending", 0)
            if n_no > 0 or n_pd > 0:
                details = []
                if n_ok:
                    details.append(f"{n_ok} 批准")
                if n_no:
                    details.append(f"{n_no} 拒绝")
                if n_pd:
                    details.append(f"{n_pd} 待定")
                message = f"✅ Stage {current_stage} 审核通过 (含 {' / '.join(details)}), 状态变 {new_status}"

    try:
        con = sqlite3.connect(str(task_db))
        con.execute(
            "UPDATE task SET status=?, updated_at=? WHERE task_id=?",
            (new_status, datetime.now().isoformat(), task_id),
        )
        con.commit()
        con.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB write failed: {e}")

    try:
        ws.save_audit(
            action="stage_advance",
            stage=current_stage,
            payload={
                "approved": approved,
                "partial": partial_summary is not None and (
                    partial_summary.get("rejected", 0) > 0 or partial_summary.get("pending", 0) > 0
                ),
                "partial_summary": partial_summary,
                "previous_status": current_status,
                "new_status": new_status,
                "requested_stage": requested_stage,
                "background": background,
                "new_task_id": new_task_id,
                "effective_task_id": effective_task_id,
                "skipped_stages": skipped_stages,
                "pid": launch_info.get("pid"),
                "log_path": launch_info.get("log_path"),
                "comment": payload.get("comment", "")[:200],
            }
        )
    except Exception as e:
        print(f"[audit] write failed: {e}")

    return {
        "ok": True,
        "approved": True,
        "new_status": new_status,
        "previous_status": current_status,
        "stage": current_stage,
        "task_id": task_id,
        "effective_task_id": effective_task_id,
        "req_id": req_id_db,
        "partial_summary": partial_summary,
        "new_task_id": new_task_id,
        "skipped_stages": skipped_stages,
        "background": background and current_stage in ("stage3", "stage4"),
        "pid": launch_info.get("pid"),
        "log_path": launch_info.get("log_path"),
        "message": message,
    }
@app.post("/api/tasks/{task_id}/cases/{case_id}/approve")
async def api_approve_case(task_id: str, case_id: str, payload: Dict[str, Any]):
    """批准 / 拒绝用例 (写回 cases.json)"""
    cases_path = _resolve_cases_path(task_id)
    if not cases_path or not cases_path.exists():
        raise HTTPException(status_code=404, detail="cases.json not found")

    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    # v0.5.1 fix: 用 list 收集所有匹配的 case (避免只改第一个)
    targets = [c for c in cases if c.get("case_id") == case_id]
    if not targets:
        raise HTTPException(status_code=404, detail=f"case_id {case_id} not found")

    # 批准操作影响所有同 case_id 的 entry (LLM 偶尔生成重复)
    approved = bool(payload.get("approved", False))
    comment = payload.get("comment", "")[:500]
    reviewed_at = datetime.now().isoformat()
    for target in targets:
        target["human_approved"] = approved
        target["review_comment"] = comment
        target["reviewed_at"] = reviewed_at

    cases_path.write_text(
        json.dumps(cases, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # v0.5: 写入审计日志
    try:
        ws_mgr = WorkspaceManager(PROJECT_ROOT / "runs")
        ws = ws_mgr.get_task(task_id)
        if ws:
            ws.save_audit(
                action="case_approve" if approved else "case_reject",
                case_id=case_id,
                payload={"approved": approved, "comment": comment, "affected_count": len(targets)}
            )
    except Exception as e:
        print(f"[audit] write failed: {e}")

    return {"ok": True, "case_id": case_id, "approved": approved,
            "affected_count": len(targets)}


@app.post("/api/tasks/{task_id}/links/{link_id}/approve")
async def api_approve_link(task_id: str, link_id: str, payload: Dict[str, Any]):
    """v0.5.4: 批准 / 拒绝 link (写回 links.json)"""
    links = get_links_for_review(task_id)
    target = None
    for link in links:
        if link.get("chunk_id") == link_id or str(link.get("link_index", "")) == str(link_id):
            target = link
            break
    if not target:
        raise HTTPException(status_code=404, detail=f"link_id {link_id} not found")
    approved = bool(payload.get("approved", False))
    comment = payload.get("comment", "")[:500]
    target["human_approved"] = approved
    target["review_comment"] = comment
    target["reviewed_at"] = datetime.now().isoformat()
    _save_approved_links(task_id, links)
    # v0.5: 审计日志
    try:
        ws_mgr = WorkspaceManager(PROJECT_ROOT / "runs")
        ws = ws_mgr.get_task(task_id)
        if ws:
            ws.save_audit(
                action="link_approve" if approved else "link_reject",
                stage="stage3",
                case_id=link_id,
                payload={"approved": approved, "comment": comment}
            )
    except Exception as e:
        print(f"[audit] write failed: {e}")
    return {"ok": True, "link_id": link_id, "approved": approved}


@app.post("/api/tasks/{task_id}/scenarios/{sc_id}/approve")
async def api_approve_scenario(task_id: str, sc_id: str, payload: Dict[str, Any]):
    """v0.5.4: 批准 / 拒绝 scenario (写回 scenarios.json)"""
    scenarios = get_scenarios_for_review(task_id)
    targets = [s for s in scenarios if s.get("scenario_id") == sc_id]
    if not targets:
        raise HTTPException(status_code=404, detail=f"scenario_id {sc_id} not found")
    approved = bool(payload.get("approved", False))
    comment = payload.get("comment", "")[:500]
    reviewed_at = datetime.now().isoformat()
    for target in targets:
        target["human_approved"] = approved
        target["review_comment"] = comment
        target["reviewed_at"] = reviewed_at
    _save_approved_scenarios(task_id, scenarios)
    # v0.5: 审计日志
    try:
        ws_mgr = WorkspaceManager(PROJECT_ROOT / "runs")
        ws = ws_mgr.get_task(task_id)
        if ws:
            ws.save_audit(
                action="scenario_approve" if approved else "scenario_reject",
                stage="stage4",
                case_id=sc_id,
                payload={"approved": approved, "comment": comment, "affected_count": len(targets)}
            )
    except Exception as e:
        print(f"[audit] write failed: {e}")
    return {"ok": True, "scenario_id": sc_id, "approved": approved,
            "affected_count": len(targets)}


# ============================================================
# 需求输入 (P2 用)
# ============================================================

@app.post("/api/requirements/generate")
async def api_requirements_generate(payload: Dict[str, Any] = Body(default_factory=dict)):
    """从前端创建需求 + 触发 generate 命令

    Body: { req_id, domain, title, background, actors, main_flow, ... }
    """
    import subprocess as _sp
    import tempfile as _tmp
    import uuid as _uuid

    req_id = payload.get("req_id", "").strip()
    domain = payload.get("domain", "").strip()
    if not req_id or not domain:
        raise HTTPException(status_code=400, detail="req_id 和 domain 必填")

    # 验证 domain 存在
    kb_cfg = PROJECT_ROOT / ".config" / "kb-config.yaml"
    if kb_cfg.exists():
        import yaml
        cfg = yaml.safe_load(kb_cfg.read_text(encoding="utf-8"))
        if domain not in cfg.get("domains", {}):
            raise HTTPException(
                status_code=400,
                detail=f"Domain '{domain}' 不存在. 可用: {list(cfg.get('domains',{}).keys())}",
            )

    # 写 requirements/{req_id}.json
    req_dir = PROJECT_ROOT / "requirements"
    req_dir.mkdir(exist_ok=True)
    req_path = req_dir / f"{req_id}.json"

    req_payload = {
        "req_id": req_id,
        "title": payload.get("title", ""),
        "background": payload.get("background", ""),
        "actors": payload.get("actors", []),
        "main_flow": payload.get("main_flow", []),
        "acceptance": payload.get("acceptance", []),
        "constraints": payload.get("constraints", []),
        "edge_cases": payload.get("edge_cases", []),
        # v0.5.7+: domain 字段 (供 retry 重新生成时回读, 之前缺失导致 retry 报"无法确定 domain")
        "domain": payload.get("domain", ""),
        # v0.5.7+: review_gates 列表, 默认全开, 控制哪些 stage 完成后等人工审核
        "review_gates": payload.get("review_gates", ["stage3", "stage4", "stage5"]),
    }
    req_path.write_text(
        json.dumps(req_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 启动 generate 命令 (后台)
    # v0.5.7+: 把 review_gates 翻译成 CLI flag, 避免依赖 testcase.py 的 fallback 逻辑
    run_id = f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{_uuid.uuid4().hex[:8]}"
    log_path = PROJECT_ROOT / "runs" / req_id / f"frontend_run_{run_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        str(PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"),
        "-u",
        str(PROJECT_ROOT / "bin" / "testcase.py"),
        "generate",
        req_id,
        "--domain", domain,
        "-f", str(req_path),
    ]
    # v0.5.7+: 显式传 CLI flag (双保险, 不依赖 requirement 文件读取逻辑)
    for gate in req_payload.get("review_gates", []):
        if gate == "stage3":
            cmd.append("--require-review-stage3")
        elif gate == "stage4":
            cmd.append("--require-review-stage4")
        elif gate == "stage5":
            cmd.append("--require-review-stage5")
    log_path.write_text(f"$ {cmd}\n", encoding="utf-8")
    log_file_handle = open(log_path, "a", encoding="utf-8")  # BUG-4/10 fix
    proc = _sp.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        stdout=log_file_handle,
        stderr=_sp.STDOUT,
        creationflags=0x00000008,  # DETACHED_PROCESS on Windows
    )
    # BUG-4 fix: 注册到全局表
    _BG_PROCESSES[proc.pid] = {
        "kind": "generate",
        "pid": proc.pid,
        "task_id": run_id,
        "old_task_id": req_id,
        "started_at": datetime.now().isoformat(),
        "log_path": str(log_path.relative_to(PROJECT_ROOT)),
        "_proc": proc,
        "_log_handle": log_file_handle,
    }

    return {
        "ok": True,
        "req_id": req_id,
        "domain": domain,
        "req_path": str(req_path.relative_to(PROJECT_ROOT)),
        "log_path": str(log_path.relative_to(PROJECT_ROOT)),
        "pid": proc.pid,
    }


@app.post("/api/tasks/{task_id}/retry")
async def api_retry_task(task_id: str, payload: Dict[str, Any] = Body(default_factory=dict)):
    """v0.5.8: 重试失败的 task (默认后台跑).

    Body: { background?: bool = true, domain?: str }
    Returns: { ok, new_task_id, old_task_id, req_id, domain, skipped_stages, log_path, pid }
    """
    from src.generate import prepare_retry_task, retry_failed_task

    background = bool(payload.get("background", True))
    domain_override = payload.get("domain")  # 手动指定覆盖 (fallback 全失败时用)

    ws_mgr = WorkspaceManager(PROJECT_ROOT / "runs")
    ws = ws_mgr.get_task(task_id)
    if not ws:
        raise HTTPException(status_code=404, detail=f"task 不存在: {task_id}")
    try:
        with sqlite3.connect(ws.db_path) as con:
            row = con.execute(
                "SELECT status FROM task WHERE task_id=?", (task_id,),
            ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"task 不存在: {task_id}")
        if row[0] != "failed" and not row[0].startswith("awaiting_review_"):
            raise HTTPException(
                status_code=400,
                detail=f"task 状态={row[0]}, 只允许重试 failed 或 awaiting_review_* 的 task",
            )
    except HTTPException:
        raise

    if not background:
        return retry_failed_task(task_id, PROJECT_ROOT, domain_override=domain_override)

    prepared = prepare_retry_task(task_id, PROJECT_ROOT, domain_override=domain_override)
    if not prepared.get("ok"):
        return prepared

    new_task_id = prepared["new_task_id"]
    try:
        launch_info = _launch_retry_background(
            old_task_id=task_id,
            new_task_id=new_task_id,
            domain_override=domain_override,
        )
    except Exception as launch_exc:
        try:
            failed_ws = ws_mgr.get_task(new_task_id)
            if failed_ws:
                failed_ws.update_task_status("failed")
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"后台启动失败: {launch_exc}")

    return {
        "ok": True,
        "background": True,
        "old_task_id": task_id,
        "new_task_id": new_task_id,
        "effective_task_id": new_task_id,
        "req_id": prepared.get("req_id"),
        "domain": prepared.get("domain"),
        "skipped_stages": prepared.get("skipped_stages", []),
        "last_success_stage": prepared.get("last_success_stage"),
        "log_path": launch_info.get("log_path"),
        "pid": launch_info.get("pid"),
        "message": "后台启动成功, 已创建新 task 并继续执行",
    }


# ============================================================
# 报告相关 (v0.3.5: 前端直接看测试用例)
# ============================================================

@app.get("/api/tasks/{task_id}/report")
async def api_task_report(task_id: str):
    """获取任务的测试用例 HTML 报告 (直接嵌入到 UI 页面)

    Returns:
        dict with report summary and either html content or html_path
    """
    ws_mgr = WorkspaceManager(PROJECT_ROOT / "runs")
    ws = ws_mgr.get_task(task_id)
    if not ws:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")

    task_dir = ws.root
    req_id = ws.req_id
    report_html_path = task_dir / "report.html"
    cases_json = task_dir / "stage5" / "cases.json"
    debug_artifacts = _build_task_debug_artifacts(task_dir)
    debug_artifact_counts = _summarize_debug_artifact_counts(debug_artifacts)

    def _build_summary() -> Dict[str, Any]:
        cases: List[Dict[str, Any]] = []
        if cases_json.exists():
            try:
                loaded = json.loads(cases_json.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    cases = loaded
            except Exception:
                cases = []
        return {
            "task_id": task_id,
            "req_id": req_id,
            "task_dir": str(task_dir.relative_to(PROJECT_ROOT)),
            "total_cases": len(cases),
            "n_p0": sum(1 for c in cases if c.get("priority") == "P0"),
            "n_p1": sum(1 for c in cases if c.get("priority") == "P1"),
            "n_p2": sum(1 for c in cases if c.get("priority") == "P2"),
            "real_fields": {
                "acquisitionLinkAnomalyOffline": sum(
                    1 for c in cases if "acquisitionLinkAnomalyOffline" in json.dumps(c, ensure_ascii=False)
                ),
                "WorkWechatCustomerAcquisitionLinkListener": sum(
                    1 for c in cases if "WorkWechatCustomerAcquisitionLinkListener" in json.dumps(c, ensure_ascii=False)
                ),
            },
            "has_cases_json": cases_json.exists(),
            "has_report_html": report_html_path.exists(),
            "has_debug_artifacts": debug_artifacts.get("available", False),
            "debug_artifact_counts": debug_artifact_counts,
        }

    def _response_from_existing_report(extra_summary: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        summary = _build_summary()
        if extra_summary:
            summary.update(extra_summary)
        return {
            "summary": summary,
            "html": report_html_path.read_text(encoding="utf-8"),
            "html_path": str(report_html_path.relative_to(PROJECT_ROOT)),
            "debug_artifacts": debug_artifacts,
            "task": {
                "task_id": task_id,
                "req_id": req_id,
                "task_dir": str(task_dir.relative_to(PROJECT_ROOT)),
            },
        }

    # 兼容老任务: 只有 report.html 时直接返回历史报告
    if report_html_path.exists() and not cases_json.exists():
        return _response_from_existing_report({"report_source": "existing_report_only"})

    if not cases_json.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Stage 5 cases.json not found for task {task_id} (dir: {task_dir.name})",
        )

    import subprocess
    import sys as _sys
    import os as _os

    render_script = PROJECT_ROOT / "src" / "task_report_renderer.py"
    if not render_script.exists():
        if report_html_path.exists():
            return _response_from_existing_report({"render_warning": "task_report_renderer.py not found"})
        raise HTTPException(status_code=500, detail="task_report_renderer.py not found")

    env = _os.environ.copy()
    env["TASK_DIR"] = str(task_dir)
    env["REQ_ID"] = req_id or task_dir.parent.name
    env["TASK_ID"] = task_id
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")

    r = subprocess.run(
        [_sys.executable, str(render_script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(PROJECT_ROOT),
        timeout=60,
        env=env,
    )
    if r.returncode != 0:
        if report_html_path.exists():
            msg = (r.stderr or r.stdout or "render failed")[:200]
            return _response_from_existing_report({"render_warning": msg})
        raise HTTPException(status_code=500, detail=f"render failed: {(r.stderr or r.stdout)[:200]}")

    if not report_html_path.exists():
        raise HTTPException(status_code=500, detail="report.html not generated")

    return _response_from_existing_report({"report_source": "rendered"})


@app.get("/api/tasks/{task_id}/artifacts/debug")
async def api_task_debug_artifacts(
    task_id: str,
    stage: str = Query("stage4"),
    variant: str = Query("raw"),
):
    """返回指定 task 的 stage4/stage5 debug 产物内容，给 review 页弹窗预览用。"""
    task_dir = _resolve_task_dir(task_id)
    if not task_dir:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
    payload = _build_debug_artifact_payload(task_dir, stage=stage, variant=variant)
    return {"task_id": task_id, **payload}


@app.get("/artifact-view/{task_id}", response_class=HTMLResponse)
async def artifact_view_page(
    task_id: str,
    stage: str = Query("stage4"),
    variant: str = Query("raw"),
):
    """查看 task 下 stage4/stage5 debug 产物内容。"""
    ws_mgr = WorkspaceManager(PROJECT_ROOT / "runs")
    ws = ws_mgr.get_task(task_id)
    if not ws:
        return HTMLResponse(
            content=(
                "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='UTF-8'>"
                f"<title>Artifact Not Found - {html_escape(task_id)}</title></head>"
                "<body style='font-family:-apple-system,sans-serif;padding:24px;'>"
                f"<h1>Task 不存在</h1><p>{html_escape(task_id)}</p></body></html>"
            ),
            status_code=404,
        )

    stage = stage if stage in ("stage4", "stage5") else "stage4"
    variant = variant if variant in ("raw", "repaired", "other") else "raw"
    debug_artifacts = _build_task_debug_artifacts(ws.root)
    stage_titles = {"stage4": "Stage 4 场景", "stage5": "Stage 5 用例"}
    variant_titles = {"raw": "原始输出", "repaired": "修复后输出", "other": "其它调试文件"}

    nav_links: List[str] = []
    for nav_stage in ("stage4", "stage5"):
        stage_data = (debug_artifacts.get("stages") or {}).get(nav_stage, {})
        for nav_variant in ("raw", "repaired", "other"):
            count = len(stage_data.get(nav_variant, []))
            cls = "artifact-nav-link"
            if nav_stage == stage and nav_variant == variant:
                cls += " active"
            if count:
                nav_links.append(
                    f'<a class="{cls}" href="/artifact-view/{html_escape(task_id)}?stage={nav_stage}&variant={nav_variant}">'
                    f'{stage_titles.get(nav_stage, nav_stage)} · {variant_titles.get(nav_variant, nav_variant)} ({count})</a>'
                )
            else:
                nav_links.append(
                    f'<span class="{cls} disabled">{stage_titles.get(nav_stage, nav_stage)} · {variant_titles.get(nav_variant, nav_variant)} (0)</span>'
                )

    files = ((debug_artifacts.get("stages") or {}).get(stage, {}) or {}).get(variant, [])
    file_blocks: List[str] = []
    for idx, item in enumerate(files):
        file_path = ws.root / item["relative_path"]
        preview = _read_text_preview(file_path)
        meta = f'{item["relative_path"]} · {item["size_bytes"]} bytes · {item["updated_at"]}'
        trunc_note = '<div class="artifact-trunc">内容过长，已截断展示前 200000 字符。</div>' if preview["truncated"] else ''
        file_blocks.append(
            f'<details class="artifact-item" {"open" if idx == 0 else ""}>'
            f'<summary>{html_escape(item["file_name"])}<span class="artifact-meta">{html_escape(meta)}</span></summary>'
            f'{trunc_note}<pre>{html_escape(preview["content"])}</pre></details>'
        )

    body_html = ''.join(file_blocks) if file_blocks else '<div class="artifact-empty">当前分类下暂无文件。</div>'
    page = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>Debug Artifact - {html_escape(task_id)}</title>
<style>
  body {{ margin:0; font-family:-apple-system,sans-serif; background:#f5f7fa; color:#2c3e50; }}
  .topbar {{ background:#2c3e50; color:white; padding:14px 20px; display:flex; justify-content:space-between; align-items:center; gap:12px; }}
  .topbar a {{ color:#8fd3ff; text-decoration:none; }}
  .container {{ max-width:1400px; margin:0 auto; padding:20px; }}
  .card {{ background:white; border-radius:10px; box-shadow:0 2px 8px rgba(0,0,0,.06); padding:16px 18px; margin-bottom:16px; }}
  .artifact-nav {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; }}
  .artifact-nav-link {{ text-decoration:none; color:#1f6feb; border:1px solid #cfe2ff; background:#f7fbff; padding:6px 10px; border-radius:999px; font-size:12px; }}
  .artifact-nav-link.active {{ background:#1f6feb; color:white; border-color:#1f6feb; }}
  .artifact-nav-link.disabled {{ color:#999; background:#f3f4f6; border-color:#e5e7eb; cursor:not-allowed; }}
  .artifact-item {{ background:white; border:1px solid #e5e7eb; border-radius:8px; margin-bottom:12px; overflow:hidden; }}
  .artifact-item summary {{ cursor:pointer; list-style:none; padding:14px 16px; font-weight:600; display:flex; justify-content:space-between; gap:12px; align-items:center; }}
  .artifact-item summary::-webkit-details-marker {{ display:none; }}
  .artifact-item[open] summary {{ border-bottom:1px solid #eef2f7; }}
  .artifact-meta {{ font-weight:400; color:#6b7280; font-size:12px; margin-left:12px; }}
  .artifact-item pre {{ margin:0; padding:16px; white-space:pre-wrap; word-break:break-word; background:#0f172a; color:#e5eef9; font-size:12px; line-height:1.55; overflow:auto; }}
  .artifact-empty {{ color:#6b7280; padding:18px; text-align:center; }}
  .artifact-trunc {{ padding:10px 16px; color:#92400e; background:#fff7ed; border-bottom:1px solid #fed7aa; font-size:12px; }}
</style>
</head>
<body>
<div class="topbar">
  <div>
    <div style="font-size:18px;font-weight:700;">🧪 Debug Artifact Viewer</div>
    <div style="font-size:12px;opacity:.85;">Task {html_escape(task_id)} · {stage_titles.get(stage, stage)} · {variant_titles.get(variant, variant)}</div>
  </div>
  <div><a href="/flows/{html_escape(task_id)}">返回流程页</a> · <a href="/task/{html_escape(task_id)}">返回报告页</a></div>
</div>
<div class="container">
  <div class="card">
    <div style="font-size:16px;font-weight:700;">文件导航</div>
    <div style="color:#6b7280;font-size:13px;margin-top:6px;">按 stage / 输出类型查看原始 LLM 输出、修复后输出及其它调试文件。</div>
    <div class="artifact-nav">{''.join(nav_links)}</div>
  </div>
  <div class="card">
    <div style="font-size:16px;font-weight:700;">{stage_titles.get(stage, stage)} · {variant_titles.get(variant, variant)}</div>
    <div style="color:#6b7280;font-size:13px;margin-top:6px;">共 {len(files)} 个文件</div>
  </div>
  {body_html}
</div>
</body>
</html>'''
    return HTMLResponse(content=page)


@app.get("/task/{task_id}", response_class=HTMLResponse)
async def task_page(task_id: str):
    """任务详情页 - 显示测试用例报告 (v0.3.5)

    复用 report.html 内容, 但嵌入到 UI 框架里.

    v0.5.7+: 如果没有 report.html (新 pipeline 没生成), fallback 渲染
    stage5/cases.json 用例列表, 或重定向到 /flows/{task_id} 看流程图.
    """
    from fastapi.responses import RedirectResponse
    # 找 task_dir (复用 parse_workflow 的逻辑)
    workflow = parse_workflow_from_task(task_id)
    task_dir_rel = workflow.get("task_dir") if workflow else None
    task_dir = PROJECT_ROOT / task_dir_rel if task_dir_rel else None
    debug_artifacts = (workflow or {}).get("debug_artifacts") or _build_task_debug_artifacts(task_dir)
    debug_links_html = _render_debug_artifact_links(task_id, debug_artifacts)

    if task_dir and (task_dir / "report.html").exists():
            report_html = (task_dir / "report.html").read_text(encoding='utf-8')

            # 包装一层 (顶部加返回链接 + 标题)
            wrapped = f"""<!DOCTYPE html>
    <html lang="zh-CN">
    <head>
    <meta charset="UTF-8">
    <title>Task {task_id} - 测试用例报告</title>
    <style>
      body {{ margin: 0; padding: 0; background: #f5f7fa; }}
      .topbar {{ background: #2c3e50; color: white; padding: 12px 20px;
                  display: flex; justify-content: space-between; align-items: center;
                  position: sticky; top: 0; z-index: 100; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
      .topbar a {{ color: #3498db; text-decoration: none; padding: 6px 12px;
                    background: rgba(255,255,255,0.1); border-radius: 4px; }}
      .topbar a:hover {{ background: rgba(255,255,255,0.2); }}
      .topbar h1 {{ margin: 0; font-size: 16px; font-weight: 500; }}
      .topbar .task-id {{ font-family: monospace; background: rgba(255,255,255,0.1);
                           padding: 4px 8px; border-radius: 4px; font-size: 12px; }}
    </style>
    </head>
    <body>
    <div class="topbar">
      <h1>🧪 测试用例报告</h1>
      <span class="task-id">{task_id}</span>
      <a href="/">← 返回首页</a>
    </div>
    <div style="max-width:1200px;margin:0 auto;padding:0 20px;">{debug_links_html}</div>
    {report_html}
    </body>
    </html>"""
            return HTMLResponse(content=wrapped)

        # ====== v0.5.7+ Fallback: 没有 report.html, 直接渲染 stage5/cases.json 用例列表 ======
    cases_file = (task_dir / "stage5" / "cases.json") if task_dir else None
    if cases_file and cases_file.exists():
        try:
            import json as _json
            cases = _json.loads(cases_file.read_text(encoding="utf-8"))
        except Exception:
            cases = []

        # 统计 priority
        from collections import Counter
        prio = Counter(c.get("priority", "?") for c in cases)

        # v0.5.7+: 渲染 stage 摘要 (从 parse_workflow_from_task 拿)
        # 给 fallback 页面内嵌一份阶段状态摘要, 不用跳 /flows 也能看到
        stage_summary_html = ""
        try:
            wf = parse_workflow_from_task(task_id) or {}
            stages_list = wf.get("stages", [])
            llm_count = len(wf.get("llm_calls", []))
            if stages_list:
                stage_rows = []
                for s in stages_list:
                    cls = s.get("status", "")
                    dur = s.get("duration_ms") or 0
                    dur_str = f"{dur/1000:.1f}s" if dur else "-"
                    badge_cls = "ok" if cls == "success" else "fail" if cls == "failed" else "run"
                    err = s.get("error", "")
                    stage_rows.append(
                        f'<tr><td>{s.get("stage_name","")}</td>'
                        f'<td><span class="badge {badge_cls}">{cls}</span></td>'
                        f'<td>{dur_str}</td><td>{len(s.get("nodes",[]))}</td></tr>'
                    )
                    if err:
                        err_escaped = err.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                        stage_rows.append(
                            f'<tr><td colspan="4" style="padding:2px 12px;color:#c0392b;font-size:11px;">└ {err_escaped}</td></tr>'
                        )
                stage_summary_html = (
                    f'<div class="card" style="background:#f8f9fa;">'
                    f'<h3>🔄 Stage 状态</h3>'
                    f'<table style="width:100%;font-size:13px;">'
                    f'<tr style="background:#e9ecef;"><th>Stage</th><th>状态</th><th>耗时</th><th>节点</th></tr>'
                    f'{"".join(stage_rows)}'
                    f'</table>'
                    f'<div style="margin-top:8px;font-size:12px;color:#666;">'
                    f'🤖 LLM 调用 <b>{llm_count}</b> 次 · '
                    f'前往 <a href="/flows/{task_id}">流程图页</a> 看完整时序</div>'
                    f'</div>'
                )
        except Exception:
            stage_summary_html = ""  # 容错: workflow 解析失败不影响主流程

        # 渲染每条用例 (折叠卡片)
        case_cards = []
        for i, c in enumerate(cases, 1):
            steps_html = ""
            for st in c.get("steps", []) or []:
                if isinstance(st, dict):
                    steps_html += f"<li><b>{st.get('step','')}</b>. {st.get('action','')}</li>"
                else:
                    steps_html += f"<li>{st}</li>"
            precond_html = ""
            for p in c.get("preconditions", []) or []:
                if isinstance(p, dict):
                    precond_html += f"<li>{' / '.join(f'{k}: {v}' for k,v in p.items())}</li>"
                else:
                    precond_html += f"<li>{p}</li>"
            approved = c.get("human_approved")
            approved_badge = ('<span class="badge approved">✓ 已批准</span>' if approved is True
                              else '<span class="badge pending">⏳ 待审</span>' if approved is None
                              else '<span class="badge rejected">✗ 不通过</span>')

            case_cards.append(f"""
            <div class="case-card" data-priority="{c.get('priority','?')}">
              <div class="case-header" onclick="this.parentElement.classList.toggle('open')">
                <span class="case-no">#{i}</span>
                <span class="case-id">{c.get('case_id','')}</span>
                <span class="case-title">{c.get('title','')}</span>
                <span class="badge prio-{c.get('priority','?')}">{c.get('priority','?')}</span>
                {approved_badge}
                <span class="toggle">▼</span>
              </div>
              <div class="case-body">
                <div class="meta-row">
                  <b>场景 ID:</b> {c.get('scenario_id','')} ·
                  <b>类型:</b> {c.get('scenario_type','')}
                </div>
                <div class="section">
                  <h4>前置条件</h4>
                  <ul>{precond_html or '<li class="empty">无</li>'}</ul>
                </div>
                <div class="section">
                  <h4>步骤</h4>
                  <ol>{steps_html or '<li class="empty">无</li>'}</ol>
                </div>
                <div class="section">
                  <h4>预期结果</h4>
                  <pre>{c.get('expected_result','')}</pre>
                </div>
              </div>
            </div>""")

        fallback_html = f"""<!DOCTYPE html>
    <html lang="zh-CN">
    <head>
    <meta charset="UTF-8">
    <title>Task {task_id} - 测试用例</title>
    <style>
      body {{ margin: 0; padding: 0; background: #f5f7fa; font-family: -apple-system, sans-serif; }}
      .topbar {{ background: #2c3e50; color: white; padding: 12px 20px;
              display: flex; justify-content: space-between; align-items: center;
              position: sticky; top: 0; z-index: 100; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
      .topbar a {{ color: #3498db; text-decoration: none; padding: 6px 12px;
                background: rgba(255,255,255,0.1); border-radius: 4px; }}
      .topbar h1 {{ margin: 0; font-size: 16px; font-weight: 500; }}
      .topbar .task-id {{ font-family: monospace; background: rgba(255,255,255,0.1);
                       padding: 4px 8px; border-radius: 4px; font-size: 12px; }}
      .container {{ max-width: 1100px; margin: 0 auto; padding: 20px; }}
      .summary {{ background: white; padding: 16px; border-radius: 8px; margin-bottom: 16px;
              box-shadow: 0 1px 3px rgba(0,0,0,0.06); }}
      .summary .stat {{ display: inline-block; margin-right: 24px; font-size: 14px; }}
      .summary .stat b {{ color: #2c3e50; font-size: 20px; }}
      .case-card {{ background: white; border-radius: 8px; margin-bottom: 8px;
                box-shadow: 0 1px 3px rgba(0,0,0,0.06); overflow: hidden; }}
      .case-header {{ padding: 12px 16px; cursor: pointer; display: flex; align-items: center;
                  gap: 12px; user-select: none; }}
      .case-header:hover {{ background: #f8f9fa; }}
      .case-no {{ color: #888; font-size: 12px; min-width: 30px; }}
      .case-id {{ font-family: monospace; font-size: 12px; color: #666; min-width: 130px; }}
      .case-title {{ flex: 1; font-weight: 500; }}
      .toggle {{ color: #999; font-size: 10px; transition: transform 0.2s; }}
      .case-card.open .toggle {{ transform: rotate(180deg); }}
      .case-body {{ display: none; padding: 16px; border-top: 1px solid #eee;
                background: #fafbfc; }}
      .case-card.open .case-body {{ display: block; }}
      .meta-row {{ font-size: 13px; color: #555; margin-bottom: 12px; }}
      .section {{ margin-bottom: 14px; }}
      .section h4 {{ margin: 0 0 6px; font-size: 13px; color: #2c3e50; }}
      .section ul, .section ol {{ margin: 0; padding-left: 20px; font-size: 13px; line-height: 1.7; }}
      .section pre {{ margin: 0; padding: 10px; background: white; border-radius: 4px;
                  white-space: pre-wrap; word-break: break-word; font-size: 13px; }}
      .empty {{ color: #aaa; font-style: italic; }}
      .badge {{ padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }}
      .badge.prio-P0 {{ background: #fee; color: #c0392b; }}
      .badge.prio-P1 {{ background: #fef5e7; color: #d68910; }}
      .badge.prio-P2 {{ background: #e8f4f8; color: #2874a6; }}
      .badge.approved {{ background: #d5f5e3; color: #229954; }}
      .badge.pending {{ background: #fcf3cf; color: #b7950b; }}
      .badge.rejected {{ background: #fadbd8; color: #c0392b; }}
      .info-banner {{ background: #fff3cd; border-left: 4px solid #f39c12; padding: 10px 16px;
                  border-radius: 4px; font-size: 13px; margin-bottom: 16px; }}
    </style>
    </head>
    <body>
    <div class="topbar">
      <h1>🧪 测试用例 (v0.5.7 fallback)</h1>
      <span class="task-id">{task_id}</span>
      <a href="/flows/{task_id}">🔄 流程图</a>
      <a href="/">← 返回首页</a>
    </div>
    <div class="container">
      {debug_links_html}
      <div class="info-banner">
    ℹ️ 此 task 没有 report.html, 已 fallback 渲染 stage5/cases.json。前往
    <a href="/flows/{task_id}">流程图页</a> 可看 stage/node/LLM 调用完整信息。
      </div>
      <div class="summary">
        <div class="stat"><b>{len(cases)}</b> 用例</div>
        <div class="stat">P0: <b>{prio.get('P0', 0)}</b></div>
        <div class="stat">P1: <b>{prio.get('P1', 0)}</b></div>
        <div class="stat">P2: <b>{prio.get('P2', 0)}</b></div>
      </div>
      {stage_summary_html}
      {''.join(case_cards) or '<div class="empty">无用例数据</div>'}
    </div>
    <script>
    // 默认展开第一个
    document.querySelector('.case-card')?.classList.add('open');
    </script>
    </body>
    </html>"""
        return HTMLResponse(content=fallback_html)

        # ====== 都没有: 真没找到 ======
    return HTMLResponse(content=f"""
    <!DOCTYPE html>
    <html><head><meta charset="UTF-8"><title>Task Not Found</title></head>
    <body>
    <h1>Task Not Found</h1>
    <p>Task ID: {task_id}</p>
    <p>report.html / stage5/cases.json 都不存在. <a href="/">返回首页</a></p>
    </body></html>
    """)


# ============================================================
# v0.4: 新增 3 个页面 - 状态流程 / 需求输入 / 人工审核
# ============================================================

@app.get("/flows/{task_id}", response_class=HTMLResponse)
async def flows_page(task_id: str):
    """P1: 状态流程查看页 - 显示 task 的 stage/node/llm_call 时序"""
    workflow = parse_workflow_from_task(task_id)
    if not workflow:
        return HTMLResponse(content="""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>Task Not Found</title>
<style>body{font-family:-apple-system,sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;background:#f5f5f5}
.box{background:white;padding:40px;border-radius:8px;text-align:center;box-shadow:0 2px 8px rgba(0,0,0,.1)}
h1{margin:0 0 12px;color:#e74c3c}p{color:#666;margin:8px 0}
.btn{display:inline-block;margin-top:16px;padding:10px 20px;background:#3498db;color:white;text-decoration:none;border-radius:4px}</style>
</head><body><div class="box">
<h1>Task Not Found</h1><p>任务 ID: <code>""" + task_id + """</code> 不存在或已删除</p>
<p>2 秒后自动跳转首页...</p>
<a class="btn" href="/">← 立即返回首页</a>
</div><script>setTimeout(function(){location.href="/"}, 2000);</script>
</body></html>""")
    # 先解析一次 workflow 拿到真实 len numbers (避免页面占位)
    n_llm = len(workflow.get("llm_calls", [])) if workflow else 0
    n_recalls = len(workflow.get("recalls", [])) if workflow else 0
    n_scenarios = len(workflow.get("scenarios", [])) if workflow else 0
    debug_links_html = _render_debug_artifact_links(task_id, workflow.get("debug_artifacts", {}))
    return HTMLResponse(content=f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>状态流程 - {task_id}</title>
<style>
  body {{ margin: 0; font-family: -apple-system, sans-serif; background: #f5f7fa; }}
  .topbar {{ background: #2c3e50; color: white; padding: 12px 20px;
              display: flex; justify-content: space-between; align-items: center;
              position: sticky; top: 0; z-index: 100; }}
  .topbar h1 {{ margin: 0; font-size: 16px; }}
  .topbar a {{ color: #3498db; text-decoration: none; padding: 6px 12px;
                background: rgba(255,255,255,0.1); border-radius: 4px; }}
  .container {{ max-width: 1400px; margin: 0 auto; padding: 20px; }}
  .summary {{ background: white; padding: 20px; border-radius: 8px;
              margin-bottom: 16px; box-shadow: 0 2px 4px rgba(0,0,0,0.06); }}
  .summary-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                    gap: 16px; margin-top: 12px; }}
  .summary-card {{ padding: 12px; background: #f8f9fa; border-radius: 6px; }}
  .summary-card .label {{ color: #888; font-size: 12px; }}
  .summary-card .value {{ font-size: 24px; font-weight: 700; color: #2c3e50; }}
  .stages {{ background: white; padding: 20px; border-radius: 8px;
             margin-bottom: 16px; }}
  .stage {{ border: 1px solid #e0e6ed; border-radius: 8px; padding: 16px;
            margin-bottom: 12px; border-left: 4px solid #3498db; }}
  .stage.success {{ border-left-color: #27ae60; }}
  .stage.failed {{ border-left-color: #e74c3c; }}
  .stage.running {{ border-left-color: #f39c12; background: #fff8e1; }}
  .stage-header {{ display: flex; justify-content: space-between;
                    align-items: center; margin-bottom: 8px; }}
  .stage-name {{ font-weight: 600; font-size: 16px; }}
  .badge {{ display: inline-block; padding: 2px 10px; border-radius: 10px;
            font-size: 11px; font-weight: 600; }}
  .badge-success {{ background: #d5f5e3; color: #229954; }}
  .badge-failed {{ background: #fde8e8; color: #c0392b; }}
  .badge-running {{ background: #fff3cd; color: #d68910; }}
  .badge-skipped {{ background: #e2e3e5; color: #383d41; }}
  .duration {{ color: #888; font-size: 12px; }}
  .nodes {{ margin-left: 20px; margin-top: 8px; }}
  .node {{ padding: 6px 10px; background: #f8f9fa; border-radius: 4px;
           margin-bottom: 4px; font-family: monospace; font-size: 12px;
           display: flex; justify-content: space-between; }}
  .node-id {{ color: #555; }}
  .node.success {{ border-left: 3px solid #27ae60; }}
  .node.failed {{ border-left: 3px solid #e74c3c; }}
  .node.running {{ border-left: 3px solid #f39c12; }}
  .section-title {{ font-size: 14px; font-weight: 600; color: #555;
                    margin: 20px 0 8px; text-transform: uppercase; }}
  .llm-table, .recall-table, .scenario-table {{ width: 100%;
              border-collapse: collapse; font-size: 13px; }}
  .llm-table th, .llm-table td, .recall-table th, .recall-table td,
  .scenario-table th, .scenario-table td {{ padding: 8px;
              text-align: left; border-bottom: 1px solid #eee; }}
  .llm-table th, .recall-table th, .scenario-table th {{ color: #555;
              background: #f8f9fa; font-weight: 600; }}
  .empty {{ color: #999; font-style: italic; padding: 20px; text-align: center; }}
  a.nav {{ color: #3498db; text-decoration: none; }}
  .sub-nav {{ background: white; padding: 12px; border-radius: 8px;
              margin-bottom: 16px; display: flex; gap: 12px; }}
  .sub-nav a {{ color: #555; text-decoration: none; padding: 6px 14px;
                border-radius: 4px; }}
  .sub-nav a:hover {{ background: #f0f0f0; }}
  .sub-nav a.active {{ background: #3498db; color: white; }}
  .review-banner {{ padding: 16px 20px; border-radius: 8px; margin-bottom: 16px;
                     display: flex; align-items: center; justify-content: space-between;
                     gap: 16px; }}
  .review-banner.awaiting {{ background: linear-gradient(90deg, #fff3cd, #ffe69c);
                              border: 2px solid #d68910; }}
  .review-banner.success {{ background: linear-gradient(90deg, #d5f5e3, #abebc6);
                             border: 2px solid #229954; }}
  .review-banner.failed {{ background: #fde8e8; border: 2px solid #c0392b; }}
  .banner-msg {{ flex: 1; }}
  .banner-msg h3 {{ margin: 0 0 4px; font-size: 16px; }}
  .banner-msg p {{ margin: 0; color: #555; font-size: 13px; }}
  .banner-actions {{ display: flex; gap: 8px; }}
  .btn-approve {{ padding: 8px 16px; background: #27ae60; color: white;
                  border: none; border-radius: 4px; cursor: pointer; font-weight: 600; }}
  .btn-approve:hover {{ background: #229954; }}
  .btn-reject {{ padding: 8px 16px; background: #e74c3c; color: white;
                 border: none; border-radius: 4px; cursor: pointer; font-weight: 600; }}
  .btn-reject:hover {{ background: #c0392b; }}
  .status-pill {{ display: inline-block; padding: 2px 8px; border-radius: 10px;
                  font-size: 11px; font-weight: 600;
                  background: rgba(0,0,0,0.1); }}
</style>
</head>
<body>
<div class="topbar">
  <h1>🔄 状态流程</h1>
  <span style="font-family:monospace;font-size:12px;">{task_id}</span>
  <a href="/">← 返回首页</a>
</div>
<div class="container">
  <div class="sub-nav">
    <a href="/task/{task_id}">📋 测试用例</a>
    <a href="/flows/{task_id}" class="active">🔄 状态流程</a>
    <a href="/review/{task_id}" id="flows-review-link">👤 人工审核</a>
  </div>
  <div id="review-banner" style="display:none;"></div>
  <div id="summary">加载中...</div>
  {debug_links_html}
  <div class="stages" id="stages">加载中...</div>
  <div class="section-title">🤖 LLM 调用 ({n_llm} 次)</div>
  <div id="llm-calls">加载中...</div>
  <div class="section-title">🔗 召回 ({n_recalls} 关联)</div>
  <div id="recalls">加载中...</div>
  <div class="section-title">📝 场景 ({n_scenarios} 个)</div>
  <div id="scenarios">加载中...</div>
</div>
<script>
async function load() {{
  const r = await fetch('/api/tasks/{task_id}/workflow');
  const data = await r.json();
  if (!data.task_id) {{
    document.querySelector('.container').innerHTML = '<div class="empty">任务不存在</div>';
    return;
  }}

  // v0.5: 审核 banner
  const banner = document.getElementById('review-banner');
  const status = data.status || '';
  // v0.5.6+: sub-nav "人工审核" 链接也按 status 路由
  const reviewLink = document.getElementById('flows-review-link');
  if (reviewLink) {{
    const s = status;
    if (s === 'awaiting_review_links') {{
      reviewLink.href = '/review_links/{task_id}';
      reviewLink.textContent = '🗂️ 召回审核';
      reviewLink.classList.add('pulse');
    }} else if (s === 'awaiting_review_scenarios') {{
      reviewLink.href = '/review_scenarios/{task_id}';
      reviewLink.textContent = '🎬 场景审核';
      reviewLink.classList.add('pulse');
    }} else if (s === 'awaiting_review_cases') {{
      reviewLink.href = '/review/{task_id}';
      reviewLink.textContent = '👤 用例审核';
      reviewLink.classList.add('pulse');
    }} else {{
      reviewLink.href = '/review/{task_id}';
      reviewLink.textContent = '👤 人工审核';
      reviewLink.classList.remove('pulse');
    }}
  }}
  if (status === 'awaiting_review_links') {{
    banner.style.display = 'flex';
    banner.className = 'review-banner awaiting';
    banner.innerHTML = `
      <div class="banner-msg">
        <h3>🔔 Stage 3 召回 (links) 等您审核</h3>
        <p>请在下方 <b>召回 ({n_recalls} 关联)</b> 区域查看 chunk + 置信度, <a href="/review_links/{task_id}">🗂️ 去召回审核页</a> 逐条精审, 或确认后点击右方按钮。</p>
      </div>
      <div class="banner-actions">
        <button class="btn-approve" onclick="advanceStage('stage3', true)">✓ 批准并继续 Stage 4</button>
        <button class="btn-reject" onclick="advanceStage('stage3', false)">✗ 不通过</button>
      </div>
    `;
  }} else if (status === 'awaiting_review_scenarios') {{
    banner.style.display = 'flex';
    banner.className = 'review-banner awaiting';
    banner.innerHTML = `
      <div class="banner-msg">
        <h3>🔔 Stage 4 场景 (scenarios) 等您审核</h3>
        <p>请在下方 <b>场景 ({n_scenarios} 个)</b> 区域查看, <a href="/review_scenarios/{task_id}">🎬 去场景审核页</a> 逐条精审, 或确认后点击右方按钮。</p>
      </div>
      <div class="banner-actions">
        <button class="btn-approve" onclick="advanceStage('stage4', true)">✓ 批准并继续 Stage 5</button>
        <button class="btn-reject" onclick="advanceStage('stage4', false)">✗ 不通过</button>
      </div>
    `;
  }} else if (status === 'awaiting_review_cases') {{
    banner.style.display = 'flex';
    banner.className = 'review-banner awaiting';
    banner.innerHTML = `
      <div class="banner-msg">
        <h3>🔔 Stage 5 用例 (cases) 等您终审</h3>
        <p>请到 <a href="/review/{task_id}">人工审核页</a> 审, 全部批准后返回点击右方按钮。</p>
      </div>
      <div class="banner-actions">
        <button class="btn-approve" onclick="advanceStage('stage5', true)">✓ 全部批准 (终审)</button>
        <button class="btn-reject" onclick="advanceStage('stage5', false)">✗ 不通过</button>
      </div>
    `;
  }} else if (status === 'success') {{
    banner.style.display = 'flex';
    banner.className = 'review-banner success';
    banner.innerHTML = `
      <div class="banner-msg">
        <h3>✅ 任务完成</h3>
        <p>所有 stage 完成, 任务成功结束。</p>
      </div>
    `;
  }} else if (status === 'failed') {{
    banner.style.display = 'flex';
    banner.className = 'review-banner failed';
    banner.innerHTML = `
      <div class="banner-msg">
        <h3>❌ 任务失败</h3>
        <p>查看下方 stage 错误信息。</p>
      </div>
    `;
  }} else {{
    banner.style.display = 'none';
  }}

  // Summary
  let summary_html = `
    <div class="summary">
      <h2>${{escapeHtml(data.req_id)}}</h2>
      <div class="summary-grid">
        <div class="summary-card"><div class="label">状态</div><div class="value">${{escapeHtml(data.status)}}</div></div>
        <div class="summary-card"><div class="label">总耗时</div><div class="value">${{(data.total_duration_ms/1000).toFixed(1)}}s</div></div>
        <div class="summary-card"><div class="label">Stage 数</div><div class="value">${{data.stages.length}}</div></div>
        <div class="summary-card"><div class="label">用例</div><div class="value">${{data.cases_summary.total}}</div></div>
        <div class="summary-card"><div class="label">P0/P1/P2</div>
          <div class="value" style="font-size:14px;">${{data.cases_summary.by_priority.P0||0}}/${{data.cases_summary.by_priority.P1||0}}/${{data.cases_summary.by_priority.P2||0}}</div>
        </div>
      </div>
    </div>
  `;
  document.getElementById('summary').innerHTML = summary_html;

  // Stages
  const stages_html = data.stages.map(s => {{
    const cls = s.status === 'success' ? 'success' : s.status === 'failed' ? 'failed' : s.status === 'running' ? 'running' : '';
    const dur = s.duration_ms ? `${{(s.duration_ms/1000).toFixed(1)}}s` : '...';
    const nodes_html = (s.nodes || []).map(n => {{
      const ncls = n.status === 'success' ? 'success' : n.status === 'failed' ? 'failed' : '';
      return `<div class="node ${{ncls}}"><span class="node-id">${{escapeHtml(n.node_id)}}</span><span>${{escapeHtml(n.status)}}</span></div>`;
    }}).join('');
    return `
      <div class="stage ${{cls}}">
        <div class="stage-header">
          <div class="stage-name">${{escapeHtml(s.stage_name)}}</div>
          <div><span class="badge badge-${{s.status}}">${{s.status}}</span> <span class="duration">${{dur}}</span></div>
        </div>
        <div class="nodes">${{nodes_html || '<div class="empty">无节点</div>'}}</div>
        ${{s.artifacts && s.artifacts.length ? `<div style="margin-top:8px;color:#888;font-size:12px;">产物: ${{s.artifacts.join(', ')}}</div>` : ''}}
      </div>
    `;
  }}).join('') || '<div class="empty">无 stage</div>';
  document.getElementById('stages').innerHTML = stages_html;

  // LLM calls
  const llm_html = data.llm_calls.length === 0 ? '<div class="empty">无 LLM 调用</div>' :
    `<table class="llm-table">
       <tr><th>时间</th><th>用途</th><th>模型</th><th>Tokens</th><th>耗时</th></tr>
       ${{data.llm_calls.map(l => `
         <tr>
           <td style="font-family:monospace;color:#888;">${{escapeHtml(l.timestamp).substring(11, 19)}}</td>
           <td>${{escapeHtml(l.purpose || '-')}}</td>
           <td>${{escapeHtml(l.model || '-')}}</td>
           <td>${{l.input_tokens}}/→${{l.output_tokens}}</td>
           <td>${{(l.duration_ms/1000).toFixed(1)}}s</td>
         </tr>
       `).join('')}}
     </table>`;
  document.getElementById('llm-calls').innerHTML = llm_html;

  // Recalls
  const recall_html = data.recalls.length === 0 ? '<div class="empty">无召回</div>' :
    `<table class="recall-table">
       <tr><th>Chunk ID</th><th>类型</th><th>置信度</th><th>原因</th></tr>
       ${{data.recalls.map(r => `
         <tr>
           <td style="font-family:monospace;font-size:11px;">${{escapeHtml(r.chunk_id.split(':').pop())}}</td>
           <td>${{escapeHtml(r.link_type)}}</td>
           <td>${{r.relevance}}</td>
           <td style="font-size:11px;color:#888;">${{escapeHtml(r.reason)}}</td>
         </tr>
       `).join('')}}
     </table>`;
  document.getElementById('recalls').innerHTML = recall_html;

  // Scenarios
  const scen_html = data.scenarios.length === 0 ? '<div class="empty">无场景</div>' :
    `<table class="scenario-table">
       <tr><th>ID</th><th>标题</th><th>类型</th><th>优先级</th><th>关联</th></tr>
       ${{data.scenarios.map(s => `
         <tr>
           <td style="font-family:monospace;">${{escapeHtml(s.scenario_id)}}</td>
           <td>${{escapeHtml(s.title)}}</td>
           <td>${{escapeHtml(s.scenario_type)}}</td>
           <td>${{escapeHtml(s.priority)}}</td>
           <td>${{s.related_chunks_count}}</td>
         </tr>
       `).join('')}}
     </table>`;
  document.getElementById('scenarios').innerHTML = scen_html;
}}

function escapeHtml(s) {{
  if (!s) return '';
  const _esc = {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}};
  return String(s).replace(/[&<>"']/g, c => _esc[c]);
}}

async function advanceStage(stage, approved) {{
  if (!approved) {{
    // 拒绝: 仍用 advance-stage 写 audit
    const r = await fetch('/api/tasks/{task_id}/advance-stage', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ stage, approved: false }}),
        }});
    if (r.ok) {{
      const data = await r.json();
      alert(data.message || '已拒绝');
      load();
    }} else {{
      const data = await r.json();
      alert('失败: ' + (data.detail || 'unknown'));
    }}
    return;
  }}

  // 批准: v0.5.7+ advance-stage API 不启动后续 stage 进程, 改用 retry API
  // retry 会基于旧 task 的 last_ok stage 跳过已成功的, 从下一 stage 开始重跑 (新 task_id)
  // 用 background=false 同步模式, 立即返回 new_task_id, 然后跳首页
  if (!confirm('批准后将从下一 stage 开始重跑 (基于当前 task 复用已成功 stage 的产物). 确认?')) {{
    return;
  }}
  const r = await fetch('/api/tasks/{task_id}/retry', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ background: false }}),
    }});
  if (r.ok) {{
    const data = await r.json();
    if (data.ok && data.new_task_id) {{
      alert("✅ 审核已批准, 重跑已启动. 新 task: " + data.new_task_id + " · 跳过的 stage: " + (data.skipped_stages || []).join(', '));
    }} else {{
      alert("⚠️ retry 返回: " + JSON.stringify(data).substring(0, 200));
    }}
    setTimeout(() => location.href = '/', 1000);
  }} else {{
    const data = await r.json();
    alert('失败: ' + (data.detail || JSON.stringify(data)));
  }}
}}

load();
// 每 3 秒自动刷新
setInterval(load, 3000);
</script>
</body>
</html>""")


@app.get("/api/tasks/{task_id}/workflow")
async def api_task_workflow(task_id: str):
    """P1: 返回 task 的 workflow 数据 (stage/node/llm_call/recall/scenario)"""
    data = parse_workflow_from_task(task_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
    return data


@app.get("/review/{task_id}", response_class=HTMLResponse)
async def review_page(task_id: str):
    """P3: 人工审核页 - 用例列表 + 批准/拒绝"""
    workflow = parse_workflow_from_task(task_id) or {}
    debug_links_html = _render_debug_artifact_links(task_id, workflow.get("debug_artifacts", {}), inline_preview=True)
    debug_modal_html = _render_debug_preview_modal()
    return HTMLResponse(content=f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>人工审核 - {task_id}</title>
<style>
  body {{ margin: 0; font-family: -apple-system, sans-serif; background: #f5f7fa; }}
  .topbar {{ background: #2c3e50; color: white; padding: 12px 20px;
              display: flex; justify-content: space-between; align-items: center;
              position: sticky; top: 0; z-index: 100; }}
  .topbar h1 {{ margin: 0; font-size: 16px; }}
  .topbar a {{ color: #3498db; text-decoration: none; padding: 6px 12px;
                background: rgba(255,255,255,0.1); border-radius: 4px; }}
  .container {{ max-width: 1400px; margin: 0 auto; padding: 20px; }}
  .toolbar {{ background: white; padding: 16px; border-radius: 8px;
              margin-bottom: 16px; display: flex; gap: 12px; }}
  .toolbar input {{ flex: 1; padding: 8px 12px; border: 1px solid #ddd;
                    border-radius: 6px; font-size: 14px; }}
  .summary {{ display: grid; grid-template-columns: repeat(4, 1fr);
              gap: 12px; margin-bottom: 16px; }}
  .summary-card {{ background: white; padding: 16px; border-radius: 8px;
                    text-align: center; }}
  .summary-card .v {{ font-size: 28px; font-weight: 700; }}
  .sub-nav {{ background: white; padding: 12px; border-radius: 8px;
              margin-bottom: 16px; display: flex; gap: 12px; }}
  .sub-nav a {{ color: #555; text-decoration: none; padding: 6px 14px;
                border-radius: 4px; }}
  .sub-nav a:hover {{ background: #f0f0f0; }}
  .sub-nav a.active {{ background: #3498db; color: white; }}
  .case-card {{ background: white; border-radius: 8px; padding: 16px;
                margin-bottom: 12px; border-left: 4px solid #ddd; }}
  .case-card.approved {{ border-left-color: #27ae60; background: #f0fff4; }}
  .case-card.rejected {{ border-left-color: #e74c3c; background: #fff5f5; }}
  .case-card.pending {{ border-left-color: #f39c12; }}
  .case-header {{ display: flex; justify-content: space-between;
                  align-items: center; margin-bottom: 8px; flex-wrap: wrap; gap: 8px; }}
  .case-id {{ font-family: monospace; background: #f4f6f9; padding: 2px 8px;
              border-radius: 4px; font-size: 12px; }}
  .case-title {{ font-weight: 600; margin: 8px 0; }}
  .actions {{ display: flex; gap: 8px; }}
  button {{ padding: 6px 14px; border: none; border-radius: 4px;
            cursor: pointer; font-size: 13px; font-weight: 500; }}
  .btn-approve {{ background: #27ae60; color: white; }}
  .btn-reject {{ background: #e74c3c; color: white; }}
  .btn-pending {{ background: #f39c12; color: white; }}
  button:hover {{ opacity: 0.85; }}
  .comment-input {{ width: 100%; margin-top: 8px; padding: 6px;
                    border: 1px solid #ddd; border-radius: 4px;
                    box-sizing: border-box; font-size: 13px; }}
  .meta {{ color: #888; font-size: 12px; margin-top: 4px; }}
  pre {{ background: #2c3e50; color: #ecf0f1; padding: 8px; border-radius: 4px;
          font-size: 12px; overflow-x: auto; max-height: 200px; }}
  .empty {{ color: #999; font-style: italic; padding: 20px; text-align: center; }}
</style>
</head>
<body>
<div class="topbar">
  <h1>👤 人工审核</h1>
  <span style="font-family:monospace;font-size:12px;">{task_id}</span>
  <a href="/">← 返回首页</a>
</div>
<div class="container">
  <div class="sub-nav">
    <a href="/task/{task_id}">📋 测试用例</a>
    <a href="/flows/{task_id}">🔄 状态流程</a>
    <a href="/review/{task_id}" class="active">👤 人工审核</a>
  </div>
  {debug_links_html}
  {debug_modal_html}
  <div class="toolbar">
    <input type="text" id="filter" placeholder="🔍 搜索 case_id / title..." />
  </div>
  <div class="summary" id="summary">加载中...</div>
  <div id="cases">加载中...</div>
</div>
<script>
let allCases = [];
async function load() {{
  const r = await fetch('/api/tasks/{task_id}/cases');
  const cases = await r.json();
  if (!Array.isArray(cases)) {{
    document.getElementById('cases').innerHTML = '<div class="empty">未找到用例</div>';
    return;
  }}
  allCases = cases;
  render();
}}

function render() {{
  const q = document.getElementById('filter').value.toLowerCase();
  const filtered = allCases.filter(c => !q ||
    (c.case_id||'').toLowerCase().includes(q) ||
    (c.title||'').toLowerCase().includes(q));
  // Summary
  const n_total = allCases.length;
  const n_approved = allCases.filter(c => c.human_approved === true).length;
  const n_rejected = allCases.filter(c => c.human_approved === false && c.reviewed_at).length;
  const n_pending = n_total - n_approved - n_rejected;
  document.getElementById('summary').innerHTML = `
    <div class="summary-card"><div class="v" id="sum_total">0</div><div>总用例</div></div>
    <div class="summary-card" style="color:#27ae60;"><div class="v" id="sum_approved">0</div><div>已批准</div></div>
    <div class="summary-card" style="color:#e74c3c;"><div class="v" id="sum_rejected">0</div><div>已拒绝</div></div>
    <div class="summary-card" style="color:#f39c12;"><div class="v" id="sum_pending">0</div><div>待审核</div></div>
  `;
  document.getElementById('sum_total').textContent = String(n_total);
  document.getElementById('sum_approved').textContent = String(n_approved);
  document.getElementById('sum_rejected').textContent = String(n_rejected);
  document.getElementById('sum_pending').textContent = String(n_pending);
  // v0.5.2: 终审确认按钮 (在 summary 后, 调用 advance-stage)
  document.getElementById('summary').insertAdjacentHTML('beforeend', `
    <div class="confirm-bar" style="margin-top: 12px; display: flex; gap: 8px; align-items: center;">
      <button class="btn-approve" id="btn-confirm-all" style="padding: 10px 20px; font-size: 14px;">
        ✓ 确认终审 (全部批准, 任务变 success)
      </button>
      <span style="color: #888; font-size: 12px;">
        终审将 task 状态从 <b>awaiting_review_cases</b> 推到 <b>success</b>
      </span>
    </div>
  `);
  const btnConfirm = document.getElementById('btn-confirm-all');
  if (btnConfirm && !btnConfirm._listenerAttached) {{
    btnConfirm._listenerAttached = true;
        btnConfirm.addEventListener('click', async () => {{
          // v0.5.5 partial mode (dynamic confirm with case status summary)
          var nOk=0,nNo=0,nPd=0;
          for (var i=0;i<allCases.length;i++){{
            var c=allCases[i];
            if (c.human_approved===true) nOk++;
            else if (c.human_approved===false && c.reviewed_at) nNo++;
            else nPd++;
          }}
          var lines=[];
          if (nOk) lines.push("OK "+nOk);
          if (nNo) lines.push("NO "+nNo);
          if (nPd) lines.push("PD "+nPd);
          var isPartial=(nNo>0 || nPd>0);
          var msg=isPartial ? "partial: "+lines.join(",") : "confirm all "+nOk;
          if (!confirm(msg)) return;
          const r = await fetch('/api/tasks/{task_id}/advance-stage', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ stage: 'stage5', approved: true }}),
          }});
          if (r.ok) {{
            const data = await r.json();
            alert(data.message || '已确认终审');
            location.href = '/flows/{task_id}';
          }} else {{
            const data = await r.json();
            alert('确认失败: ' + (data.detail || r.status));
          }}
          }});
          }}
          // Cases: 直接 map 渲染 (避免嵌套 function 定义)
  const html = filtered.map(c => {{
    const status = c.human_approved === true ? '已批准' :
                   c.human_approved === false && c.reviewed_at ? '已拒绝' : '待审核';
    return `
      <div class="case-card">
        <div class="case-header">
          <span class="case-id">${{escapeHtml(c.case_id)}}</span>
          <span style="color:#888;font-size:12px;">${{escapeHtml(c.priority)}}</span>
          <span style="color:#888;font-size:12px;">${{escapeHtml(c.scenario_type || '')}}</span>
          <div class="actions">
            <button class="btn-approve" data-cid="${{c.case_id}}" data-approved="true">✓ 批准</button>
            <button class="btn-reject" data-cid="${{c.case_id}}" data-approved="false">✗ 拒绝</button>
            <button class="btn-pending" data-cid="${{c.case_id}}" data-approved="null">↻ 待定</button>
          </div>
        </div>
        <div class="case-title">${{escapeHtml(c.title || '(无标题)')}}</div>
        <div class="meta">状态: ${{status}}</div>
        ${{c.review_comment ? `<div class="meta">评论: ${{escapeHtml(c.review_comment)}}</div>` : ''}}
        <textarea class="comment-input" placeholder="审核意见..." id="comment-${{c.case_id}}">${{escapeHtml(c.review_comment || '')}}</textarea>
      </div>
    `;
  }}).join('') || '<div class="empty">无匹配用例</div>';
  document.getElementById('cases').innerHTML = html;

  // v0.5.1: event delegation - 一次性给所有 button 绑 listener
  document.querySelectorAll('#cases button[data-cid]').forEach(btn => {{
    if (btn._listenerAttached) return;
    btn._listenerAttached = true;
    btn.addEventListener('click', () => {{
      const cid = btn.dataset.cid;
      const approvedStr = btn.dataset.approved;
      const approved = approvedStr === 'true' ? true :
                        approvedStr === 'false' ? false : null;
      setApproved(cid, approved);
    }});
  }});
}}

async function setApproved(case_id, approved) {{
  // 通过 case_id 找对应的 card 和 comment textarea
  const btn = document.querySelector(`button[data-cid="${{case_id.replace(/"/g, '&quot;')}}"]`);
  const card = btn ? btn.closest('.case-card') : null;
  const commentEl = card ? card.querySelector('textarea') : null;
  const comment = commentEl ? commentEl.value : '';
  const r = await fetch('/api/tasks/{task_id}/cases/' + encodeURIComponent(case_id) + '/approve', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{ approved, comment }}),
  }});
  if (r.ok) {{
    // Update local state
    const c = allCases.find(c => c.case_id === case_id);
    if (c) {{
      c.human_approved = approved;
      c.review_comment = comment;
      c.reviewed_at = approved === null ? null : new Date().toISOString();
    }}
    render();
  }} else {{
    const data = await r.json();
    console.error('approve failed', data);
    alert('批准失败: ' + (data.detail || r.status));
  }}
}}

function escapeHtml(s) {{
  if (!s) return '';
  const _esc = {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}};
  return String(s).replace(/[&<>"']/g, c => _esc[c]);
}}

document.getElementById('filter').addEventListener('input', render);
load();
</script>
</body>
</html>""")


@app.get("/api/tasks/{task_id}/cases")
async def api_task_cases(task_id: str):
    """P3: 获取 task 的用例列表 (含 human_approved)"""
    return get_cases_for_review(task_id)
@app.get("/review_links/{task_id}", response_class=HTMLResponse)
async def review_links_page(task_id: str):
    """v0.5.4: Stage 3 召回 (links) 逐条审核页"""
    workflow = parse_workflow_from_task(task_id) or {}
    debug_links_html = _render_debug_artifact_links(task_id, workflow.get("debug_artifacts", {}), inline_preview=True)
    debug_modal_html = _render_debug_preview_modal()
    return HTMLResponse(content=f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>召回审核 - {task_id}</title>
<style>
  body {{ font-family: -apple-system, sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }}
  .container {{ max-width: 1200px; margin: 0 auto; }}
  .topbar {{ background: #2c3e50; color: white; padding: 12px 20px;
             border-radius: 8px; display: flex; justify-content: space-between;
             align-items: center; margin-bottom: 16px; }}
  .topbar h1 {{ margin: 0; font-size: 18px; }}
  .topbar a {{ color: #3498db; text-decoration: none; padding: 6px 12px;
               border-radius: 4px; background: rgba(255,255,255,0.1); margin-left: 8px; }}
  .summary-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 16px; }}
  .summary-card {{ background: white; padding: 16px; border-radius: 8px; text-align: center; }}
  .summary-card .v {{ font-size: 28px; font-weight: 700; }}
  .summary-card .label {{ color: #888; font-size: 12px; margin-top: 4px; }}
  .item {{ background: white; padding: 16px; margin-bottom: 12px; border-radius: 8px;
           box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
  .item.approved {{ border-left: 4px solid #27ae60; }}
  .item.rejected {{ border-left: 4px solid #e74c3c; opacity: 0.7; }}
  .id {{ font-family: monospace; background: #ecf0f1; padding: 4px 8px; border-radius: 3px; font-size: 13px; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: 11px;
            margin-right: 4px; color: white; }}
  .b-direct {{ background: #3498db; }}
  .b-indirect {{ background: #95a5a6; }}
  .b-conf {{ background: #9b59b6; }}
  .b-p0 {{ background: #e74c3c; }}
  .b-p1 {{ background: #f39c12; }}
  .b-p2 {{ background: #3498db; }}
  .actions {{ margin-top: 10px; display: flex; gap: 8px; }}
  .btn-approve, .btn-reject, .btn-pending {{ color: white; border: none;
                  padding: 6px 12px; border-radius: 4px; cursor: pointer; font-size: 13px; }}
  .btn-approve {{ background: #27ae60; }}
  .btn-reject {{ background: #e74c3c; }}
  .btn-pending {{ background: #95a5a6; }}
  textarea {{ width: 100%; padding: 6px; margin-top: 6px; border: 1px solid #ddd;
              border-radius: 3px; box-sizing: border-box; font-size: 13px; }}
  .reason {{ color: #555; margin: 6px 0; line-height: 1.5; font-size: 14px; }}
  .confirm-bar {{ margin-top: 16px; background: white; padding: 16px;
                  border-radius: 8px; display: flex; gap: 12px; align-items: center; }}
  .confirm-bar button {{ padding: 10px 20px; font-size: 14px; }}
  .empty {{ text-align: center; padding: 40px; color: #888; }}
</style>
</head>
<body>
<div class="container">
  <div class="topbar">
    <h1>🔗 召回审核 (Stage 3 Links) - {task_id}</h1>
    <div>
      <a href="/flows/{task_id}">🔄 状态流程</a>
      <a href="/">← 首页</a>
    </div>
  </div>
  {debug_links_html}
  {debug_modal_html}
  <div id="summary" class="summary-grid">加载中...</div>
  <div id="items">加载中...</div>
  <div class="confirm-bar">
    <button class="btn-approve" id="btn-submit-current">✓ 提交当前审核 (推进 Stage 4)</button>
    <button class="btn-approve" id="btn-confirm-all" style="background:#27ae60;">✓ 全部批准 (推进 Stage 4)</button>
    <button class="btn-reject" id="btn-reject-all">✗ 全部拒绝</button>
    <span style="color: #888; font-size: 13px;">
      <b>提交当前</b>用现有审核结果推进 (部分批准也行) · 全部批准 = 一键全过
    </span>
  </div>
</div>
<script>
let allItems = [];

async function load() {{
  const r = await fetch('/api/tasks/{task_id}/links');
  if (r.ok) {{ allItems = await r.json(); }}
  else {{ allItems = []; }}
  allItems.forEach((it, idx) => {{
    if (it.human_approved === undefined) it.human_approved = null;
    if (!it.review_comment) it.review_comment = '';
    it._id = it.chunk_id || `link-${{idx}}`;
  }});
  render();
}}

function escapeHtml(s) {{
  if (!s) return '';
  const _esc = {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}};
  return String(s).replace(/[&<>"']/g, c => _esc[c]);
}}

function render() {{
  const total = allItems.length;
  const n_ok = allItems.filter(i => i.human_approved === true).length;
  const n_no = allItems.filter(i => i.human_approved === false && i.reviewed_at).length;
  const n_pd = total - n_ok - n_no;

  document.getElementById('summary').innerHTML = `
    <div class="summary-card"><div class="v">${{total}}</div><div class="label">总召回</div></div>
    <div class="summary-card" style="color:#27ae60;"><div class="v">${{n_ok}}</div><div class="label">已批准</div></div>
    <div class="summary-card" style="color:#e74c3c;"><div class="v">${{n_no}}</div><div class="label">已拒绝</div></div>
    <div class="summary-card" style="color:#f39c12;"><div class="v">${{n_pd}}</div><div class="label">待审核</div></div>
  `;

  if (total === 0) {{
    document.getElementById('items').innerHTML = '<div class="item"><div class="empty">暂无召回链接。可能 stage 3 还没跑或还没轮到该 task。</div></div>';
    return;
  }}

  const html = allItems.map((it, idx) => {{
    let cls = 'item';
    if (it.human_approved === true) cls += ' approved';
    if (it.human_approved === false && it.reviewed_at) cls += ' rejected';
    const lt = it.link_type || 'unknown';
    const ltc = lt === 'direct' ? 'b-direct' : 'b-indirect';
    const cf = (it.confidence ?? 0).toFixed(2);
    return `
      <div class="${{cls}}" data-id="${{escapeHtml(it._id)}}">
        <div>
          <span class="id">${{escapeHtml(it.chunk_id || `link-${{idx}}`)}}</span>
          <span class="badge ${{ltc}}">${{escapeHtml(lt)}}</span>
          <span class="badge b-conf">置信 ${{cf}}</span>
          <span style="color:#888;font-size:12px; margin-left:8px;">相关度 ${{it.relevance || '-'}}/5</span>
        </div>
        <div class="reason">${{escapeHtml(it.reason || '(无理由)')}}</div>
        <textarea class="comment-input" placeholder="审核意见 (可选)">${{escapeHtml(it.review_comment)}}</textarea>
        <div class="actions">
          <button class="btn-approve" data-approve="true">✓ 批准</button>
          <button class="btn-reject" data-approve="false">✗ 拒绝</button>
          <button class="btn-pending" data-approve="null">↻ 待定</button>
        </div>
      </div>
    `;
  }}).join('');
  document.getElementById('items').innerHTML = html;

  // 事件代理
  document.querySelectorAll('#items .item').forEach(card => {{
    if (card._listenerAttached) return;
    card._listenerAttached = true;
    card.addEventListener('click', async (e) => {{
      const btn = e.target.closest('button[data-approve]');
      if (!btn) return;
      const itemId = card.dataset.id;
      const item = allItems.find(i => i._id === itemId);
      const approvedAttr = btn.dataset.approve;
      const approved = approvedAttr === 'true' ? true :
                       approvedAttr === 'false' ? false : null;
      const comment = card.querySelector('textarea').value;
      const r = await fetch('/api/tasks/{task_id}/links/' + encodeURIComponent(itemId) + '/approve', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{ approved, comment }}),
      }});
      if (r.ok) {{
        item.human_approved = approved;
        item.review_comment = comment;
        item.reviewed_at = new Date().toISOString();
        render();
      }} else {{
        const e = await r.json();
        alert('失败: ' + (e.detail || r.status));
      }}
    }});
  }});
}}

async function bulkApprove(approved) {{
  if (approved && !confirm('确认全部批准全部 link? 此操作将 task 状态推到 running')) return;
  if (!approved && !confirm('确认全部拒绝?')) return;
  const ids = allItems.map(it => it._id);
  for (const id of ids) {{
    await fetch('/api/tasks/{task_id}/links/' + encodeURIComponent(id) + '/approve', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{ approved, comment: 'bulk ' + (approved ? 'approve' : 'reject') }}),
    }});
  }}
  if (approved) {{
    // 推进 stage
    const r = await fetch('/api/tasks/{task_id}/advance-stage', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{ stage: 'stage3', approved: true }}),
    }});
    if (r.ok) {{
      const data = await r.json();
      alert(data.message || '已推进');
      const nextTaskId = data.new_task_id || data.effective_task_id || '{task_id}';
      location.href = '/flows/' + encodeURIComponent(nextTaskId);
    }} else {{
      const data = await r.json();
      alert('推进失败: ' + (data.detail || r.status));
      load();
    }}
  }} else {{
    load();
  }}
}}

document.getElementById('btn-confirm-all').addEventListener('click', () => bulkApprove(true));
document.getElementById('btn-reject-all').addEventListener('click', () => bulkApprove(false));
// v0.5.7+: 提交当前审核 (部分批准也能推进)
document.getElementById('btn-submit-current').addEventListener('click', async () => {{
  if (!confirm('用当前审核结果推进 stage 4 (部分批准/拒绝也算提交)?')) return;
  const r = await fetch('/api/tasks/{task_id}/advance-stage', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{ stage: 'stage3', approved: true, comment: 'partial submit' }}),
  }});
  if (r.ok) {{
    const data = await r.json();
    alert("✅ 已提交当前审核 · " + (data.message || ''));
    const nextTaskId = data.new_task_id || data.effective_task_id || '{task_id}';
    location.href = '/flows/' + encodeURIComponent(nextTaskId);
  }} else {{
    const data = await r.json();
    alert('失败: ' + (data.detail || r.status));
  }}
}});

load();
setInterval(load, 5000);
</script>
</body>
</html>
""")


@app.get("/api/tasks/{task_id}/links")
async def api_get_links(task_id: str):
    """v0.5.4: 获取 stage3 links for review page"""
    return get_links_for_review(task_id)



@app.get("/review_scenarios/{task_id}", response_class=HTMLResponse)
async def review_scenarios_page(task_id: str):
    """v0.5.4: Stage 4 场景 (scenarios) 逐条审核页"""
    workflow = parse_workflow_from_task(task_id) or {}
    debug_links_html = _render_debug_artifact_links(task_id, workflow.get("debug_artifacts", {}), inline_preview=True)
    debug_modal_html = _render_debug_preview_modal()
    return HTMLResponse(content=f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>场景审核 - {task_id}</title>
<style>
  body {{ font-family: -apple-system, sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }}
  .container {{ max-width: 1200px; margin: 0 auto; }}
  .topbar {{ background: #2c3e50; color: white; padding: 12px 20px;
             border-radius: 8px; display: flex; justify-content: space-between;
             align-items: center; margin-bottom: 16px; }}
  .topbar h1 {{ margin: 0; font-size: 18px; }}
  .topbar a {{ color: #3498db; text-decoration: none; padding: 6px 12px;
               border-radius: 4px; background: rgba(255,255,255,0.1); margin-left: 8px; }}
  .summary-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 16px; }}
  .summary-card {{ background: white; padding: 16px; border-radius: 8px; text-align: center; }}
  .summary-card .v {{ font-size: 28px; font-weight: 700; }}
  .summary-card .label {{ color: #888; font-size: 12px; margin-top: 4px; }}
  .item {{ background: white; padding: 16px; margin-bottom: 12px; border-radius: 8px;
           box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
  .item.approved {{ border-left: 4px solid #27ae60; }}
  .item.rejected {{ border-left: 4px solid #e74c3c; opacity: 0.7; }}
  .id {{ font-family: monospace; background: #ecf0f1; padding: 4px 8px; border-radius: 3px; font-size: 13px; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: 11px;
            margin-right: 4px; color: white; }}
  .b-p0 {{ background: #e74c3c; }}
  .b-p1 {{ background: #f39c12; }}
  .b-p2 {{ background: #3498db; }}
  .b-type {{ background: #9b59b6; }}
  .actions {{ margin-top: 10px; display: flex; gap: 8px; }}
  .btn-approve, .btn-reject, .btn-pending {{ color: white; border: none;
                  padding: 6px 12px; border-radius: 4px; cursor: pointer; font-size: 13px; }}
  .btn-approve {{ background: #27ae60; }}
  .btn-reject {{ background: #e74c3c; }}
  .btn-pending {{ background: #95a5a6; }}
  textarea {{ width: 100%; padding: 6px; margin-top: 6px; border: 1px solid #ddd;
              border-radius: 3px; box-sizing: border-box; font-size: 13px; }}
  .reason {{ color: #555; margin: 6px 0; line-height: 1.5; font-size: 14px; }}
  .meta {{ color: #888; font-size: 13px; margin: 4px 0; }}
  .confirm-bar {{ margin-top: 16px; background: white; padding: 16px;
                  border-radius: 8px; display: flex; gap: 12px; align-items: center; }}
  .confirm-bar button {{ padding: 10px 20px; font-size: 14px; }}
  .empty {{ text-align: center; padding: 40px; color: #888; }}
</style>
</head>
<body>
<div class="container">
  <div class="topbar">
    <h1>🎬 场景审核 (Stage 4 Scenarios) - {task_id}</h1>
    <div>
      <a href="/flows/{task_id}">🔄 状态流程</a>
      <a href="/">← 首页</a>
    </div>
  </div>
  {debug_links_html}
  {debug_modal_html}
  <div id="summary" class="summary-grid">加载中...</div>
  <div id="items">加载中...</div>
  <div class="confirm-bar">
    <button class="btn-approve" id="btn-submit-current">✓ 提交当前审核 (推进 Stage 5)</button>
    <button class="btn-approve" id="btn-confirm-all" style="background:#27ae60;">✓ 全部批准 (推进 Stage 5)</button>
    <button class="btn-reject" id="btn-reject-all">✗ 全部拒绝</button>
    <span style="color: #888; font-size: 13px;">
      <b>提交当前</b>用现有审核结果推进 (部分批准也行) · 全部批准 = 一键全过
    </span>
  </div>
</div>
<script>
let allItems = [];

async function load() {{
  const r = await fetch('/api/tasks/{task_id}/scenarios');
  if (r.ok) {{ allItems = await r.json(); }}
  else {{ allItems = []; }}
  allItems.forEach(it => {{
    if (it.human_approved === undefined) it.human_approved = null;
    if (!it.review_comment) it.review_comment = '';
  }});
  render();
}}

function escapeHtml(s) {{
  if (!s) return '';
  const _esc = {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}};
  return String(s).replace(/[&<>"']/g, c => _esc[c]);
}}

function render() {{
  const total = allItems.length;
  const n_ok = allItems.filter(i => i.human_approved === true).length;
  const n_no = allItems.filter(i => i.human_approved === false && i.reviewed_at).length;
  const n_pd = total - n_ok - n_no;

  document.getElementById('summary').innerHTML = `
    <div class="summary-card"><div class="v">${{total}}</div><div class="label">总场景</div></div>
    <div class="summary-card" style="color:#27ae60;"><div class="v">${{n_ok}}</div><div class="label">已批准</div></div>
    <div class="summary-card" style="color:#e74c3c;"><div class="v">${{n_no}}</div><div class="label">已拒绝</div></div>
    <div class="summary-card" style="color:#f39c12;"><div class="v">${{n_pd}}</div><div class="label">待审核</div></div>
  `;

  if (total === 0) {{
    document.getElementById('items').innerHTML = '<div class="item"><div class="empty">暂无场景。可能 stage 4 还没跑或还没轮到该 task。</div></div>';
    return;
  }}

  const html = allItems.map((it, idx) => {{
    let cls = 'item';
    if (it.human_approved === true) cls += ' approved';
    if (it.human_approved === false && it.reviewed_at) cls += ' rejected';
    const pid = (it.priority || 'P?').toLowerCase();
    const pc = pid === 'p0' ? 'b-p0' : pid === 'p1' ? 'b-p1' : 'b-p2';
    const actors = (it.actors || []).join(', ');
    return `
      <div class="${{cls}}" data-id="${{escapeHtml(it.scenario_id || `sc-${{idx}}`)}}">
        <div>
          <span class="id">${{escapeHtml(it.scenario_id || `sc-${{idx}}`)}}</span>
          <span class="badge ${{pc}}">${{escapeHtml(it.priority || '-')}}</span>
          <span class="badge b-type">${{escapeHtml(it.type || '-')}}</span>
          <strong>${{escapeHtml(it.name || '(无标题)')}}</strong>
        </div>
        <div class="reason">${{escapeHtml(it.description || '')}}</div>
        <div class="meta">👤 ${{escapeHtml(actors)}} · 触发: ${{escapeHtml(it.trigger || '-')}}</div>
        <textarea class="comment-input" placeholder="审核意见 (可选)">${{escapeHtml(it.review_comment)}}</textarea>
        <div class="actions">
          <button class="btn-approve" data-approve="true">✓ 批准</button>
          <button class="btn-reject" data-approve="false">✗ 拒绝</button>
          <button class="btn-pending" data-approve="null">↻ 待定</button>
        </div>
      </div>
    `;
  }}).join('');
  document.getElementById('items').innerHTML = html;

  document.querySelectorAll('#items .item').forEach(card => {{
    if (card._listenerAttached) return;
    card._listenerAttached = true;
    card.addEventListener('click', async (e) => {{
      const btn = e.target.closest('button[data-approve]');
      if (!btn) return;
      const itemId = card.dataset.id;
      const item = allItems.find(i => i.scenario_id === itemId || i._id === itemId);
      const approvedAttr = btn.dataset.approve;
      const approved = approvedAttr === 'true' ? true :
                       approvedAttr === 'false' ? false : null;
      const comment = card.querySelector('textarea').value;
      const r = await fetch('/api/tasks/{task_id}/scenarios/' + encodeURIComponent(itemId) + '/approve', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{ approved, comment }}),
      }});
      if (r.ok) {{
        item.human_approved = approved;
        item.review_comment = comment;
        item.reviewed_at = new Date().toISOString();
        render();
      }} else {{
        const e = await r.json();
        alert('失败: ' + (e.detail || r.status));
      }}
    }});
  }});
}}

async function bulkApprove(approved) {{
  if (approved && !confirm('确认全部批准全部 scenario? 此操作将创建续跑任务并推进到 stage 5')) return;
  if (!approved && !confirm('确认全部拒绝?')) return;
  const ids = allItems.map(it => it.scenario_id);
  for (const id of ids) {{
    await fetch('/api/tasks/{task_id}/scenarios/' + encodeURIComponent(id) + '/approve', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{ approved, comment: 'bulk ' + (approved ? 'approve' : 'reject') }}),
    }});
  }}
  if (approved) {{
    const r = await fetch('/api/tasks/{task_id}/advance-stage', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{ stage: 'stage4', approved: true }}),
    }});
    if (r.ok) {{
      const data = await r.json();
      alert(data.message || '已推进');
      const nextTaskId = data.new_task_id || data.effective_task_id || '{task_id}';
      location.href = '/flows/' + encodeURIComponent(nextTaskId);
    }} else {{
      const data = await r.json();
      alert('推进失败: ' + (data.detail || r.status));
      load();
    }}
  }} else {{
    load();
  }}
}}

document.getElementById('btn-confirm-all').addEventListener('click', () => bulkApprove(true));
document.getElementById('btn-reject-all').addEventListener('click', () => bulkApprove(false));
// v0.5.7+: 提交当前审核 (部分批准也能推进)
document.getElementById('btn-submit-current').addEventListener('click', async () => {{
  if (!confirm('用当前审核结果推进 stage 5 (部分批准/拒绝也算提交)?')) return;
  const r = await fetch('/api/tasks/{task_id}/advance-stage', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{ stage: 'stage4', approved: true, comment: 'partial submit' }}),
  }});
  if (r.ok) {{
    const data = await r.json();
    alert("✅ 已提交当前审核 · " + (data.message || ''));
    const nextTaskId = data.new_task_id || data.effective_task_id || '{task_id}';
    location.href = '/flows/' + encodeURIComponent(nextTaskId);
  }} else {{
    const data = await r.json();
    alert('失败: ' + (data.detail || r.status));
  }}
}});

load();
setInterval(load, 5000);
</script>
</body>
</html>
""")


@app.get("/api/tasks/{task_id}/scenarios")
async def api_get_scenarios(task_id: str):
    """v0.5.4: 获取 stage4 scenarios for review page"""
    return get_scenarios_for_review(task_id)





@app.get("/requirement", response_class=HTMLResponse)
async def requirement_page(
    req_id: Optional[str] = None,
    task_id: Optional[str] = None,
):
    """v0.5.7+: 需求输入页, 支持从 query 回填老 task 的原始参数.

    回填源 (按优先级):
      1. requirements/<req_id>.json — 用户上次通过 /requirements 提交的完整数据
      2. audit log (.logs/audit/YYYY-MM-DD.jsonl) — 该 task 最早的 generate.start event
         (兜底, 老 task 可能没存 requirements 文件)
    """
    # 加载 domain 列表
    domains = []
    kb_cfg = PROJECT_ROOT / ".config" / "kb-config.yaml"
    if kb_cfg.exists():
        try:
            import yaml
            cfg = yaml.safe_load(kb_cfg.read_text(encoding="utf-8"))
            for name, conf in cfg.get("domains", {}).items():
                domains.append({"name": name, "description": conf.get("description", "")})
        except Exception:
            pass

    domains_js = json.dumps(domains, ensure_ascii=False)
    domains_html = "".join(
        f'<option value="{d["name"]}">{d["name"]} - {d["description"]}</option>'
        for d in domains
    )

    # ====== v0.5.7+: 回填老 task 的需求数据 ======
    prefilled: Dict[str, Any] = {}
    prefill_source = ""
    if req_id:
        # 1) 优先读 requirements/<req_id>.json
        req_file = PROJECT_ROOT / "requirements" / f"{req_id}.json"
        if req_file.exists():
            try:
                prefilled = json.loads(req_file.read_text(encoding="utf-8"))
                prefill_source = f"requirements/{req_id}.json"
            except Exception:
                prefilled = {}

        # 2) 兜底: audit log 找 generate.start event 的 detail 字段
        # v0.5.7+: 用 req_id 索引加速 (O(1) 查找, 而不是倒序全扫所有 log)
        if not prefilled and task_id:
            from src.audit_index import AuditIndex
            audit_idx = AuditIndex(PROJECT_ROOT / ".logs" / "audit")
            hit = audit_idx.find_generate_start(req_id)
            if hit:
                prefilled = hit["detail"]
                prefilled.setdefault("req_id", req_id)
                prefill_source = f"audit-index:{hit['log_file']}:{hit['line_no']}"

        # 3) v0.5.7+: 即使 req_id 没存 domain, 也从 task.db 取 (audit 索引可能没存 domain)
        if prefilled and not prefilled.get("domain") and task_id:
            try:
                from src.workspace import WorkspaceManager
                ws_mgr = WorkspaceManager(PROJECT_ROOT / "runs")
                ws = ws_mgr.get_task(task_id)
                if ws:
                    with sqlite3.connect(ws.db_path) as conn:
                        row = conn.execute(
                            "SELECT domain FROM task WHERE task_id=?", (task_id,),
                        ).fetchone()
                        if row and row[0]:
                            prefilled["domain"] = row[0]
                            prefill_source += f"+task.db"
            except Exception:
                pass  # 容错: 读不到不影响主流程

    def _js_str(v: Any) -> str:
        s = "" if v is None else (v if isinstance(v, str) else "\n".join(v))
        return s.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")

    prefilled_req_id = prefilled.get("req_id", req_id or "")
    prefilled_domain = prefilled.get("domain", "")
    prefilled_title = prefilled.get("title", "")
    prefilled_bg = _js_str(prefilled.get("background", ""))
    prefilled_actors = _js_str(prefilled.get("actors", []))
    prefilled_flow = _js_str(prefilled.get("main_flow", []))
    prefilled_acc = _js_str(prefilled.get("acceptance", []))
    prefilled_con = _js_str(prefilled.get("constraints", []))
    prefilled_edge = _js_str(prefilled.get("edge_cases", []))
    is_prefilled = bool(prefilled)
    prefill_banner = (
        f'<div class="card" style="background:#fff3cd;border-left:4px solid #f39c12;">'
        f'<b>📝 回填模式</b> — 从 {prefill_source} 读取老需求参数, '
        f'修改后提交会<b>新建一个 task</b>(原 task 保留). '
        f'<a href="/requirement">清空重填</a></div>'
        if is_prefilled else ''
    )
    return HTMLResponse(content=f"""<!DOCTYPE html>
    <html lang="zh-CN">
    <head>
    <meta charset="UTF-8">
    <title>{'编辑需求' if is_prefilled else '新建需求'}</title>
    <style>
      body {{ margin: 0; font-family: -apple-system, sans-serif; background: #f5f7fa; }}
  .topbar {{ background: #2c3e50; color: white; padding: 12px 20px;
              display: flex; justify-content: space-between; align-items: center;
              position: sticky; top: 0; z-index: 100; }}
  .topbar h1 {{ margin: 0; font-size: 16px; }}
  .topbar a {{ color: #3498db; text-decoration: none; padding: 6px 12px;
                background: rgba(255,255,255,0.1); border-radius: 4px; }}
  .container {{ max-width: 1000px; margin: 0 auto; padding: 20px; }}
  .card {{ background: white; padding: 20px; border-radius: 8px;
            margin-bottom: 16px; box-shadow: 0 2px 4px rgba(0,0,0,0.06); }}
  label {{ display: block; margin: 12px 0 4px; font-weight: 600;
            color: #555; font-size: 13px; }}
  input, select, textarea {{ width: 100%; padding: 10px; border: 1px solid #ddd;
            border-radius: 6px; font-size: 14px; box-sizing: border-box; }}
  textarea {{ min-height: 80px; font-family: monospace; }}
  button {{ padding: 10px 24px; border: none; border-radius: 6px;
            cursor: pointer; font-size: 14px; font-weight: 600;
            background: #3498db; color: white; margin-top: 16px; }}
  button:hover {{ background: #2980b9; }}
  .help {{ color: #888; font-size: 12px; margin-top: 4px; }}
  .recent {{ background: #f8f9fa; padding: 12px; border-radius: 6px;
              margin-top: 8px; }}
  .recent h4 {{ margin: 0 0 8px; font-size: 13px; color: #555; }}
  .recent li {{ font-size: 13px; padding: 4px 0; }}
  .status {{ padding: 12px; border-radius: 6px; margin-top: 12px;
              display: none; }}
  .status.ok {{ background: #d5f5e3; color: #229954; display: block; }}
  .status.err {{ background: #fde8e8; color: #c0392b; display: block; }}
</style>
</head>
<body>
<div class="topbar">
  <h1>{'📝 编辑需求' if is_prefilled else '📝 新建需求'}</h1>
  <a href="/">← 返回首页</a>
</div>
<div class="container">
  {prefill_banner}
  <div class="card">
    <h2>基本信息</h2>
    <label>需求 ID (req_id) *</label>
    <input type="text" id="req_id" placeholder="例: REQ-CS-002" value="{prefilled_req_id}" />
    <div class="help">唯一标识, 仅字母数字下划线. <b>回填模式下可改名新建</b></div>

    <label>业务域 (domain) *</label>
    <select id="domain">
      {domains_html}
    </select>

    <label>标题 *</label>
    <input type="text" id="title" placeholder="例: 客服排班管理" value="{prefilled_title}" />

    <label>背景</label>
    <textarea id="background" placeholder="业务背景 / 当前痛点...">{prefilled_bg}</textarea>
  </div>

  <div class="card">
    <h2>参与者 (actors)</h2>
    <div class="help">每行一个, 例: 客服 / 管理员 / 系统</div>
    <textarea id="actors" placeholder="客服\n管理员\n系统">{prefilled_actors}</textarea>
  </div>

  <div class="card">
    <h2>主流程 (main_flow)</h2>
    <div class="help">每行一段, 描述步骤</div>
    <textarea id="main_flow" placeholder="1. 客服登录\n2. 客服接到用户咨询\n3. ...">{prefilled_flow}</textarea>
  </div>

  <div class="card">
    <h2>验收标准 (acceptance)</h2>
    <textarea id="acceptance">{prefilled_acc}</textarea>

    <h2 style="margin-top:16px;">约束 (constraints)</h2>
    <textarea id="constraints">{prefilled_con}</textarea>

    <h2 style="margin-top:16px;">边界场景 (edge_cases)</h2>
    <textarea id="edge_cases">{prefilled_edge}</textarea>
  </div>

  <div class="card">
      <h2>审核 Gate (v0.5.7+)</h2>
      <div class="help">勾选哪些 stage 完成后要等人工审核。默认全勾 — 不勾 = 跑完直接 success。</div>
      <label style="font-weight:normal;cursor:pointer;display:block;padding:4px 0;">
        <input type="checkbox" id="gate_stage3" checked /> Stage 3 召回 — 完成后审核 chunk 关联
      </label>
      <label style="font-weight:normal;cursor:pointer;display:block;padding:4px 0;">
        <input type="checkbox" id="gate_stage4" checked /> Stage 4 场景 — 完成后审核测试场景
      </label>
      <label style="font-weight:normal;cursor:pointer;display:block;padding:4px 0;">
        <input type="checkbox" id="gate_stage5" checked /> Stage 5 用例 — 完成后审核最终用例
      </label>
    </div>

    <div class="card">
      <button onclick="submitRequirement()">{'🚀 修改并重新提交' if is_prefilled else '🚀 提交并生成'}</button>
      <div class="status" id="status"></div>
    </div>
  </div>
<script>
function splitLines(text) {{
  return text.split('\\n').map(s => s.trim()).filter(s => s);
}}

async function submitRequirement() {{
  const status = document.getElementById('status');
  status.className = 'status';
  status.innerHTML = '提交中...';
  status.style.display = 'block';

  const review_gates = [];
  if (document.getElementById('gate_stage3').checked) review_gates.push('stage3');
  if (document.getElementById('gate_stage4').checked) review_gates.push('stage4');
  if (document.getElementById('gate_stage5').checked) review_gates.push('stage5');

  const payload = {{
    req_id: document.getElementById('req_id').value.trim(),
    domain: document.getElementById('domain').value,
    title: document.getElementById('title').value.trim(),
    background: document.getElementById('background').value.trim(),
    actors: splitLines(document.getElementById('actors').value),
    main_flow: splitLines(document.getElementById('main_flow').value),
    acceptance: splitLines(document.getElementById('acceptance').value),
    constraints: splitLines(document.getElementById('constraints').value),
    edge_cases: splitLines(document.getElementById('edge_cases').value),
    review_gates: review_gates,
  }};

  try {{
    const r = await fetch('/api/requirements/generate', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify(payload),
    }});
    const data = await r.json();
    if (r.ok) {{
      status.className = 'status ok';
      status.innerHTML = `
        ✓ 已启动! PID: ${{data.pid}}<br/>
        需求: ${{data.req_id}} / ${{data.domain}}<br/>
        需求文件: ${{data.req_path}}<br/>
        日志: ${{data.log_path}}<br/>
        <br/>
        <a href="/">返回首页</a> · <a href="/">查看任务</a>
      `;
    }} else {{
      status.className = 'status err';
      status.innerHTML = '✗ ' + (data.detail || '提交失败');
    }}
  }} catch (e) {{
    status.className = 'status err';
    status.innerHTML = '✗ 网络错误: ' + e.message;
  }}
}}

// v0.5.7+: 回填模式时, 选中 domain option
(function() {{
  const want = '{prefilled_domain}';
  if (want) {{
    const sel = document.getElementById('domain');
    if (sel) {{
      for (const opt of sel.options) {{
        if (opt.value === want) {{ opt.selected = true; break; }}
      }}
    }}
  }}
}})();
</script>
</body>
</html>""")


# ============================================================
# 任务详情
# ============================================================


@app.get("/requirements", response_class=HTMLResponse)
async def requirements_page_redirect():
    """v0.5.7+: /requirements 重定向到 /requirement (新路由)."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/requirement", status_code=307)


@app.get("/api/tasks/{task_id}")
async def api_task_detail(task_id: str):
    """获取任务详情"""
    detail = get_task_detail(task_id)
    if not detail:
        raise HTTPException(status_code=404, detail="任务不存在")
    return detail


@app.get("/api/domains")
async def api_domains():
    """获取业务域列表"""
    return list_domains()


@app.get("/api/search")
async def api_search(
    q: str = Query(..., min_length=1),
    domain: str = Query(...),
    limit: int = Query(20, ge=1, le=100),
):
    """代码检索"""
    from src.index_builder import IndexSearcher

    db_path = PROJECT_ROOT / "domains" / domain / "_index.db"
    if not db_path.exists():
        raise HTTPException(status_code=404, detail=f"域 [{domain}] 索引不存在")

    with IndexSearcher(db_path, domain) as searcher:
        results = searcher.search_fulltext(q, domain, limit=limit)

    return {
        "query": q,
        "domain": domain,
        "count": len(results),
        "results": results,
    }


@app.get("/events")
async def events():
    """SSE 实时推送任务状态"""
    async def event_generator():
        last_tasks_hash = ""
        while True:
            try:
                tasks = list_tasks(limit=10)
                tasks_hash = json.dumps(tasks, sort_keys=True)
                if tasks_hash != last_tasks_hash:
                    last_tasks_hash = tasks_hash
                    yield {
                        "event": "tasks_update",
                        "data": json.dumps(tasks, ensure_ascii=False),
                    }
                else:
                    yield {
                        "event": "heartbeat",
                        "data": json.dumps({"ts": datetime.now().isoformat()}),
                    }
            except Exception as e:
                yield {
                    "event": "error",
                    "data": json.dumps({"error": str(e)}),
                }
            await asyncio.sleep(3)

    return EventSourceResponse(event_generator())


@app.get("/health")
async def health():
    """健康检查"""
    return {"status": "ok", "ts": datetime.now().isoformat()}


# v0.5.7+: Graceful shutdown - 通过 SIGTERM 或 /api/shutdown 触发
# 行为: (1) 停止接受新连接 (2) 等 in-flight request 完成 (3) 等后台 task 报告状态
# 注: uvicorn 默认 5s timeout, 这里只加显式 logging 让你看到关闭过程
import signal as _signal
import os as _os
_SHUTDOWN_TRIGGERED = False

def _graceful_shutdown_handler(signum, frame):
    global _SHUTDOWN_TRIGGERED
    if _SHUTDOWN_TRIGGERED:
        return
    _SHUTDOWN_TRIGGERED = True
    sig_name = _signal.Signals(signum).name if signum in _signal.Signals._value2member_map_ else str(signum)
    print(f"\n[v0.5.7+ graceful] 收到 {sig_name}, 准备关闭 UI server...", flush=True)
    print(f"[v0.5.7+ graceful] 等待 in-flight request 完成 (uvicorn 默认 5s timeout)...", flush=True)
    # 注: 后台 Popen 启动的 generate task 是独立进程, 不受 UI server 关闭影响

# 注册 SIGTERM/SIGINT handler (仅在非 Windows 下注册 SIGTERM, Windows 用 SIGBREAK)
try:
    _signal.signal(_signal.SIGTERM, _graceful_shutdown_handler)
except (AttributeError, ValueError):
    pass  # Windows 下 SIGTERM 不可用, 跳过
try:
    _signal.signal(_signal.SIGINT, _graceful_shutdown_handler)
except (AttributeError, ValueError):
    pass


@app.post("/api/shutdown")
async def api_shutdown():
    """v0.5.7+: 优雅关闭 UI server (dev 用, 等待 in-flight request)."""
    import os as _os_local
    pid = _os_local.getpid()
    print(f"[v0.5.7+ graceful] /api/shutdown 触发, 关闭进程 pid={pid}", flush=True)
    # 异步发信号给 uvicorn (它会 graceful 关闭)
    import asyncio as _asyncio
    _asyncio.get_event_loop().call_later(0.5, lambda: _os_local.kill(pid, _signal.SIGTERM))
    return {"ok": True, "pid": pid, "message": "shutdown signal sent, 等待 in-flight 完成 (max 5s)"}


if __name__ == "__main__":
    import uvicorn
    # v0.5.9: log_level="warning" 抑制 asyncio accept OSError 噪音
    # (WinError 64 指定的网络名不再可用 - 客户端 TCP 突然关闭, 属正常挥别, 不需 ERROR 等级).
    uvicorn.run(app, host="127.0.0.1", port=18888, log_level="warning")
