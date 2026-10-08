#!/usr/bin/env python
"""
testcase - AI 测试用例生成系统 CLI

用法:
  testcase init                              初始化项目
  testcase kb build [domain]                 构建知识库
  testcase kb list                           列出所有业务域
  testcase kb stats <domain>                 查看域统计
  testcase kb search <query> --domain <d>    代码检索
  testcase generate <req-id> --domain <d>    生成测试用例
  testcase tasks                             查看历史任务
  testcase task <task-id>                    查看任务详情
  testcase ui                                启动可观测 UI
"""

import sys
import json
import click
from pathlib import Path

# 确保 src 可导入
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.logger import setup_logging, get_logger
from dotenv import load_dotenv

logger = get_logger(__name__)

# 让 init / 全局检查能读到 .env 里的 key (override=False: 系统 env 优先)
load_dotenv(dotenv_path=PROJECT_ROOT / ".env", override=False)

# P0-5 fix: 启动时收紧 .env 权限 (默认 644 改 600).
# 跨平台: POSIX 真实生效, Windows 走 NTFS ACL (Python 工具无能为力, 显式跳过).
from src.path_utils import secure_file  # noqa: E402
secure_file(PROJECT_ROOT / ".env", 0o600)


# v0.5.9: 把常见 ASCII 兼容 emoji 映射到 [TAG] 形式, 防止 Windows GBK
# stdout 在打印含 emoji 时抛 UnicodeEncodeError. 表中未覆盖的新 emoji
# 仍会通过 errors="replace" 兜底被替换为 '?', 永不抛异常.
EMOJI_FALLBACKS = {
    "✅": "[OK]",
    "❌": "[ERR]",
    "⚠️": "[WARN]",
    "⚠": "[WARN]",
    "ℹ️": "[INFO]",
    "ℹ": "[INFO]",
    "🚀": "[RUN]",
    "🔁": "[RETRY]",
    "⏳": "[WAIT]",
    "👉": "[TIP]",
    "📦": "[DOMAIN]",
    # v0.5.9+: 新增 --require-review-* gate 与多任务场景使用的 emoji
    "🛑": "[STOP]",
    "🚦": "[LIGHT]",
    "⏰": "[TIME]",
    "🔍": "[SEARCH]",
    "💡": "[HINT]",
    "📊": "[CHART]",
    "📈": "[UP]",
    "📉": "[DOWN]",
    "📋": "[LIST]",
    "🔄": "[SYNC]",
    "🎯": "[TARGET]",
    "🏷": "[TAG]",
    "🛠": "[TOOL]",
    "🧪": "[TEST]",
    "🧬": "[DNA]",
    "🏃": "[RUN]",     # alias
    "🐞": "[BUG]",
    "🐛": "[BUG]",     # alias
    "🚫": "[NO]",
    "🟢": "[ON]",
    "🔴": "[OFF]",
    "🟡": "[PEND]",
    "🟠": "[WARN]",     # alias
    "🟣": "[PURPLE]",
    "⚪": "[O]",
    "⚫": "[X]",
    "🔵": "[BLUE]",
    "🟤": "[BROWN]",
    "🤖": "[AI]",
    "👤": "[USER]",
    "👥": "[USERS]",
    "📌": "[PIN]",
    "📎": "[ATTACH]",
    "📁": "[FOLDER]",
    "📂": "[FOLDER_OPEN]",
    "📝": "[NOTE]",
    "🗑": "[DEL]",
    "🔑": "[KEY]",
    "🔐": "[LOCK]",
    "🔓": "[UNLOCK]",
    "🔒": "[LOCKED]",
    "🔏": "[PRIVACY]",
    "⚡": "[FAST]",
    "🔥": "[HOT]",
    "💥": "[BOOM]",
    "✨": "[NEW]",
    "🌟": "[STAR]",
    "⭐": "[STAR_S]",
    "🌈": "[RAINBOW]",
    "❤️": "[LOVE]",
    "💔": "[BROKEN]",
}


_ORIG_CLICK_ECHO = click.echo


def _sanitize_console_text(message, err: bool = False):
    """v0.5.9+: emoji + UTF-8 双重兜底, 永不抛 UnicodeEncodeError."""
    if not isinstance(message, str):
        return message
    stream = sys.stderr if err else sys.stdout
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        message.encode(encoding)
        return message
    except UnicodeEncodeError:
        # 1) 先按表替换已知 emoji, 保留可读语义
        for src, dst in EMOJI_FALLBACKS.items():
            message = message.replace(src, dst)
        # 2) v0.5.9+: 即使替换后仍有不可编码字符 (新 emoji 未来加入),
        # 用 errors='replace' 把不可编码字符替换成 '?', 永不抛 UnicodeEncodeError.
        try:
            message.encode(encoding)
            return message
        except UnicodeEncodeError:
            return message.encode(encoding, errors="replace").decode(encoding, errors="replace")


def _safe_click_echo(message=None, **kwargs):
    err = bool(kwargs.get("err", False))
    return _ORIG_CLICK_ECHO(_sanitize_console_text(message, err=err), **kwargs)


click.echo = _safe_click_echo


# ============================================================
# CLI Group
# ============================================================

@click.group()
@click.option('--verbose', '-v', is_flag=True, help='详细输出')
def cli(verbose):
    """AI 测试用例生成系统"""
    level = "DEBUG" if verbose else "INFO"
    setup_logging(level=level)
    if verbose:
        click.echo(f"项目根: {PROJECT_ROOT}", err=True)


# ============================================================
# init 命令
# ============================================================

@cli.command()
def init():
    """初始化项目 (检查依赖、配置)"""
    click.echo("=" * 60)
    click.echo("AI 测试用例生成系统 - 初始化检查")
    click.echo("=" * 60)

    # 1. 检查 Python 版本
    import sys
    py_version = f"{sys.version_info.major}.{sys.version_info.minor}"
    click.echo(f"✅ Python: {py_version}")

    # 2. 检查依赖
    deps = [
        ("anthropic", "anthropic"),
        ("yaml", "pyyaml"),
        ("dotenv", "python-dotenv"),
        ("click", "click"),
        ("fastapi", "fastapi"),
        ("uvicorn", "uvicorn"),
    ]
    click.echo()
    click.echo("依赖检查:")
    for module, package in deps:
        try:
            __import__(module)
            click.echo(f"   ✅ {package}")
        except ImportError:
            click.echo(f"   ❌ {package} (未安装: pip install {package})")

    # 3. 检查配置
    click.echo()
    click.echo("配置检查:")
    config_path = PROJECT_ROOT / ".config" / "config.yaml"
    if config_path.exists():
        click.echo(f"   ✅ config.yaml")
    else:
        click.echo(f"   ❌ config.yaml (缺失)")

    kb_config = PROJECT_ROOT / ".config" / "kb-config.yaml"
    if kb_config.exists():
        click.echo(f"   ✅ kb-config.yaml")
    else:
        click.echo(f"   ❌ kb-config.yaml (缺失)")

    # 4. 检查 API key
    click.echo()
    click.echo("API Key 检查:")
    import os
    api_key = os.environ.get("RIGHTAPI_API_KEY")
    if api_key:
        click.echo(f"   ✅ RIGHTAPI_API_KEY ({len(api_key)} 字符)")
    else:
        click.echo(f"   ❌ RIGHTAPI_API_KEY 未设置")
        click.echo(f"      两种设置方式:")
        click.echo(f"      1. 在项目根 .env 文件加: RIGHTAPI_API_KEY=你的key")
        click.echo(f"      2. 或设置系统环境变量: set RIGHTAPI_API_KEY=你的key")

    # 5. 检查数据目录
    click.echo()
    click.echo("数据目录:")
    dirs = ["domains", "runs", "requirements", "prompts"]
    for d in dirs:
        p = PROJECT_ROOT / d
        if p.exists():
            click.echo(f"   ✅ {d}/")
        else:
            click.echo(f"   ❌ {d}/ (缺失)")

    click.echo()
    click.echo("初始化完成!")


# ============================================================
# kb 命令组
# ============================================================

@cli.group()
def kb():
    """知识库管理"""
    pass


@kb.command(name='list')
def kb_list():
    """列出所有业务域"""
    from src.kb_build import list_domains
    domains = list_domains(project_root=PROJECT_ROOT)
    if not domains:
        click.echo("⚠️  无业务域")
        return

    click.echo(f"📦 业务域列表 ({len(domains)} 个):")
    for d in domains:
        click.echo(f"   - {d}")


@kb.command(name='stats')
@click.argument('domain')
def kb_stats(domain):
    """查看域统计"""
    from src.kb_build import show_stats
    show_stats(project_root=PROJECT_ROOT, domain=domain)


@kb.command(name='build')
@click.argument('domain', required=False)
@click.option('--facts/--no-facts', default=True,
              help='是否抽取业务事实并生成用例(需要 requirements/ 下的需求文档)')
@click.option('--llm/--no-llm', default=True,
              help='是否用 LLM 给 chunk 打业务标签/摘要。'
                   '全量 8 域上万个 chunk 时建议 --no-llm: '
                   '实测 payment 域 266 chunks 就花了 90s, 全量会跑数小时')
def kb_build(domain, facts, llm):
    """
    构建知识库索引 (不传 domain 跑全部)

    同一份需求会对每个域各跑一遍 Fact 抽取 —— 这是有意为之:
    需求说「客服异常自动下线」, 实现可能在 customer-service 也可能在 cross,
    哪个域能凑够 2 组证据, 门禁就自动把这条事实收进哪个域。
    """
    from src.kb_build import build_all_domains
    req_dir = PROJECT_ROOT / "requirements"
    if facts and not req_dir.exists():
        click.echo(f"⚠️  {req_dir} 不存在, 本次只建符号索引, 不抽 Fact")
        facts = False
    if not llm:
        click.echo("ℹ️  已关闭 LLM 增强(chunk 业务标签/摘要将为空, "
                   "但不影响符号/调用图/Fact 抽取)")

    results = build_all_domains(
        project_root=PROJECT_ROOT,
        kb_root=PROJECT_ROOT,
        only_domain=domain,
        requirements_dir=req_dir if facts else None,
        enrich_with_llm=llm,
    )
    click.echo()
    click.echo("=" * 60)
    click.echo("✅ 构建完成")
    # build_all_domains 返回 list[dict] (失败项也带 error 字段),
    # 这里按列表遍历 —— 原代码按 dict.items() 迭代, 跑完 1 小时才在
    # 打印阶段崩掉, 索引其实早就建好了, 报错极具误导性。
    for st in results:
        name = st.get("domain", "?")
        if st.get("error"):
            click.echo(f"   {name}: ❌ {st['error']}")
            continue
        bits = [f"{st.get('chunks_created', 0)} chunks",
                f"{st.get('files_scanned', 0)} 文件"]
        for key, label in (("sql_statements", "SQL"),
                           ("data_links", "链路")):
            v = st.get(key)
            if isinstance(v, dict):
                bits.append(f"{label}={v.get('accepted', len(v))}")
            elif v:
                bits.append(f"{label}={v}")
        f = st.get("facts")
        if isinstance(f, dict):
            # 事实层要显式打印三个数: 入库数 / 证据不足数 / 规则覆盖率。
            # 只打印"事实=13"看不出 13 条里有没有覆盖, 也看不出拒收了多少 ——
            # 而拒收的那些恰恰是要去补需求/补代码的清单。
            bits.append(f"事实={f.get('accepted', 0)}")
            rej = len(f.get("rejected_detail") or [])
            if rej:
                bits.append(f"证据不足={rej}")
            cc = f.get("case_coverage") or {}
            if cc:
                bits.append(f"用例={cc.get('case_total', 0)}")
                bits.append(f"规则覆盖={cc.get('fact_covered', 0)}/"
                            f"{cc.get('fact_total', 0)}")
        click.echo(f"   {name}: " + ", ".join(bits))


@kb.command(name='build-requirements')
def kb_build_requirements():
    """构建需求文档知识库 (JSON 分块 + SQLite FTS5)"""
    from src.requirement_index import build_requirement_index

    stats = build_requirement_index(
        PROJECT_ROOT / "requirements",
        PROJECT_ROOT / "domains" / "requirements" / "_index.db",
    )
    click.echo(
        f"✅ 需求知识库完成: {stats['files_scanned']} 文件 / "
        f"{stats['chunks_created']} chunks / "
        f"{stats['parse_errors']} 解析错误"
    )


@kb.command(name='search')
@click.argument('query')
@click.option('--domain', '-d', required=True, help='业务域')
@click.option('--limit', '-l', default=10, help='返回数量')
def kb_search(query, domain, limit):
    """代码检索 (符号 + 全文 + 标签 三路合并)"""
    from src.index_builder import IndexSearcher

    db_path = PROJECT_ROOT / "domains" / domain / "_index.db"
    if not db_path.exists():
        click.echo(f"❌ 域 [{domain}] 索引不存在. 先跑 testcase kb build {domain}", err=True)
        sys.exit(1)

    # 必须用 with: IndexSearcher 的连接是在 __enter__ 里建的,
    # 直接构造后调search() 会因 self._conn 为 None 抛 AttributeError
    with IndexSearcher(db_path, domain) as searcher:
        results = searcher.search(query, limit=limit)

    if not results:
        click.echo(f"未找到匹配 [{query}]", err=True)
        return

    click.echo(f"🔍 [{query}] 在 {domain} 找到 {len(results)} 个匹配:")
    for i, r in enumerate(results, 1):
        click.echo(f"   [{i}] {r.get('qualified_name', '?')}")
        click.echo(f"       文件: {r.get('file_path', '?')}:{r.get('line_start', '?')}")
        if r.get('summary'):
            click.echo(f"       摘要: {r['summary'][:100]}")


# ============================================================
# generate 命令
# ============================================================

@cli.command()
@click.argument('req_id')
@click.option('--domain', '-d', required=True, help='业务域')
@click.option('--skip', multiple=True, help='跳过 Stage (如 stage1 stage2)')
@click.option('--requirement-file', '-f', default=None,
              help='需求 JSON 文件路径')
@click.option('--require-review-stage3', is_flag=True,
              help='Stage 3 完成后阻塞, 等人工审核 links 才结束')
@click.option('--require-review-stage4', is_flag=True,
              help='Stage 4 完成后阻塞, 等人工审核 scenarios 才结束')
@click.option('--require-review-stage5', is_flag=True,
              help='Stage 5 完成后阻塞, 等人工审核 cases 才结束 (强制终审)')
def generate(req_id, domain, skip, requirement_file,
             require_review_stage3, require_review_stage4, require_review_stage5):
    """生成测试用例 (v0.5 支持 stage-level 审核 gate)"""
    from src.generate import generate as _do_generate  # v0.5.9: 修正 v0.5.7 改名漏 import.
    import json

    # 加载需求文件
    if requirement_file:
        req_path = Path(requirement_file)
        if not req_path.exists():
            click.echo(f"❌ 需求文件不存在: {requirement_file}", err=True)
            sys.exit(1)
        requirement = json.loads(req_path.read_text(encoding="utf-8"))
    else:
        # 用示例需求 (实际应从 PRD 解析)
        requirement = {
            "req_id": req_id,
            "title": f"需求 {req_id}",
            "main_flow": [{"step": 1, "action": "示例流程", "actor": "用户"}],
            "exception_flows": [],
            "business_rules": [],
        }
        click.echo(f"⚠️  未指定需求文件, 使用示例需求", err=True)

    # v0.5: 把 review flags 注入 requirement (供 generate 内部读)
    # v0.5.7+: 优先级: CLI flag > requirement 文件 > 默认全开
    #          只要任一 CLI flag 传了, 就以 CLI 为准 (空 = 跳过所有 gate)
    #          CLI flag 都没传时, 读 requirement 文件里的 review_gates
    #          都没有时, 默认全开 (v0.5.7+ 新行为, 强制审核)
    any_cli_flag = (require_review_stage3 or require_review_stage4
                    or require_review_stage5)
    if any_cli_flag:
        review_gates = []
        if require_review_stage3:
            review_gates.append("stage3")
        if require_review_stage4:
            review_gates.append("stage4")
        if require_review_stage5:
            review_gates.append("stage5")
    elif requirement.get("review_gates"):
        # 从 requirement 文件读
        review_gates = list(requirement["review_gates"])
    else:
        # 默认全开 (v0.5.7+ 新行为, 强制审核)
        review_gates = ["stage3", "stage4", "stage5"]
    requirement["review_gates"] = review_gates

    click.echo(f"🚀 生成测试用例: req_id={req_id}, domain={domain}")
    click.echo(f"   跳过 Stage: {skip or '无'}")
    if review_gates:
        click.echo(f"   🛑 审核 gate: {', '.join(review_gates)} (完成后阻塞等审)")
    else:
        click.echo(f"   审核 gate: 无 (自动跑完)")

    try:
        result = _do_generate(
            req_id=req_id,
            requirement=requirement,
            domain=domain,
            project_root=PROJECT_ROOT,
            skip_stages=list(skip),
        )
        click.echo()
        click.echo("=" * 60)
        click.echo("✅ 生成完成")
        click.echo(f"   task_id: {result['task_id']}")
        click.echo(f"   Stage 2: {len(result['stages'].get('stage2', {}).get('entities', []))} 实体")
        # v0.3-F: Stage 3 显示 chunk 召回 + 高置信度关联 (而不是仅 dict 长度)
        stage3 = result['stages'].get('stage3', [])
        if stage3:
            # 从 result dict 拿 (新设计: chunk id + relevance + reason)
            high_conf = [l for l in stage3 if l.get("relevance", 0) >= 4]
            click.echo(f"   Stage 3: {len(stage3)} 关联 (高置信 >=4: {len(high_conf)})")
            # v0.3-F: 列出前 3 个 chunk name 让用户立刻知道召回是否真工作
            # 用全局 PROJECT_ROOT (L23 module-level), 不重定义
            db_path = PROJECT_ROOT / "domains" / domain / "_index.db"
            if db_path.exists():
                import sqlite3
                for i, l in enumerate(stage3[:3], 1):
                    cid = l.get("chunk_id", "")
                    name = cid.split("method:")[-1].split("class:")[-1][:50] if ":" in cid else cid[:50]
                    if db_path.exists():
                        try:
                            with sqlite3.connect(str(db_path)) as conn:
                                conn.row_factory = sqlite3.Row
                                row = conn.execute(
                                    "SELECT qualified_name, file_path, line_start "
                                    "FROM symbols WHERE chunk_id = ?", (cid,)
                                ).fetchone()
                                if row:
                                    qn = (row["qualified_name"] or name)[:50]
                                    fpath = (row["file_path"] or "").split("/")[-1].split("\\")[-1]
                                    name = f"{qn} ({fpath}:{row['line_start']})"
                        except Exception:
                            pass
                    click.echo(f"      [{i}] {name}")
        else:
            click.echo(f"   Stage 3: 0 关联 ⚠️ (无召回)")
        click.echo(f"   Stage 4: {len(result['stages'].get('stage4', []))} 场景")
        click.echo(f"   Stage 5: {len(result['stages'].get('stage5', []))} 用例")
    except Exception as e:
        click.echo(f"❌ 生成失败: {e}", err=True)
        sys.exit(1)


# ============================================================
# retry 命令
# ============================================================

@cli.command()
@click.argument('task_id')
@click.option('--domain', '-d', default=None,
              help='覆盖 domain (fallback 全失败时手动指定)')
def retry(task_id, domain):
    """v0.5.7: 重试一个失败的 task (后台跑, 不阻塞).

    从旧 task.db 读最后成功的 stage, 自动跳过已成功的, 只重跑失败 stage.
    新建 task_id (保留原失败 task 作为审计痕迹).

    用法:
      testcase retry run-20260701-040034-3c2d6a83
      testcase retry run-20260701-040034-3c2d6a83 --domain customer-service
    """
    import subprocess as _sp
    import uuid as _uuid

    click.echo(f"🔁 重试 task: {task_id}")

    # 后台跑: 复用前端 generate 的 pattern (Popen + 日志文件)
    log_path = PROJECT_ROOT / "runs" / f"retry_{task_id[:24]}_{_uuid.uuid4().hex[:6]}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # 用环境变量传 domain_override (避免拼字符串注入)
    env_extra = {**__import__("os").environ}
    if domain:
        env_extra["YIYE_RETRY_DOMAIN"] = domain
    env_extra["YIYE_RETRY_OLD_TASK_ID"] = task_id
    env_extra["YIYE_RETRY_NEW_TASK_ID"] = "run-" + task_id[:24]  # 占位, 真正 ID 在 _resolve_retry_context 里生成

    # 后台跑: Popen (不阻塞当前 shell)
    cmd = [
        str(PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"),
        "-u",
        "-c",
        (
            "import os, sys; sys.path.insert(0, r'%s'); "
            "from pathlib import Path; "
            "from src.generate import continue_retry_task; "
            "r = continue_retry_task("
            "os.environ['YIYE_RETRY_OLD_TASK_ID'], "
            "os.environ['YIYE_RETRY_NEW_TASK_ID'] + '-' + __import__('uuid').uuid4().hex[:6], "
            "Path(r'%s'), "
            "domain_override=os.environ.get('YIYE_RETRY_DOMAIN')); "
            "print('RETRY_RESULT:', r); "
            "sys.exit(0 if r.get('ok') else 1)"
        ) % (str(PROJECT_ROOT), str(PROJECT_ROOT)),
    ]
    log_f = open(log_path, "w", encoding="utf-8")
    _sp.Popen(cmd, stdout=log_f, stderr=_sp.STDOUT, env=env_extra)
    click.echo(f"   ⏳ 后台跑, 日志: {log_path}")


# ============================================================
# tasks / task 命令
# ============================================================

@cli.command()
@click.option('--limit', '-l', default=20, help='返回多少条')
def tasks(limit):
    """查看历史任务"""
    from src.workspace import WorkspaceManager

    mgr = WorkspaceManager(PROJECT_ROOT / "runs")
    task_list = mgr.list_tasks(limit=limit)

    if not task_list:
        click.echo("⚠️  无任务历史")
        return

    click.echo(f"📋 历史任务 ({len(task_list)} 条):")
    for t in task_list:
        req = t.get("req_id", "?")
        status = t.get("status", "?")
        domain = t.get("domain", "?")
        tid = t.get("task_id", "?")
        click.echo(f"   {tid}  req={req}  domain={domain}  status={status}")


@cli.command()
@click.argument('task_id')
def task(task_id):
    """查看任务详情"""
    from src.workspace import WorkspaceManager

    mgr = WorkspaceManager(PROJECT_ROOT / "runs")
    ws = mgr.get_task(task_id)
    if not ws:
        click.echo(f"❌ 任务不存在: {task_id}", err=True)
        sys.exit(1)

    # 列出 stages
    stage_dir = ws.root
    click.echo(f"📦 task_id: {task_id}")
    click.echo(f"   req_id: {ws.req_id}")
    click.echo(f"   domain: {ws.domain}")
    click.echo(f"   status: {ws.status}")
    click.echo(f"   路径: {stage_dir}")

    stages = ["stage1", "stage2", "stage3", "stage4", "stage5"]
    for s in stages:
        d = stage_dir / s
        if d.exists():
            artifacts = [f.name for f in d.glob("**/*") if f.is_file()]
            click.echo(f"   {s}/ ({len(artifacts)} 个产物)")


# ============================================================
# ui 命令
# ============================================================

@cli.command()
@click.option('--host', default='127.0.0.1', help='监听地址')
@click.option('--port', '-p', default=8765, help='监听端口')
@click.option('--reload', is_flag=True, help='开发模式 (代码变更自动重启)')
def ui(host, port, reload):
    """启动可观测 Web UI"""
    import uvicorn
    from src.ui_server import app

    click.echo("=" * 60)
    click.echo(f"🌐 启动 Web UI: http://{host}:{port}")
    click.echo("=" * 60)
    click.echo(f"   🏠 http://{host}:{port}/ - 任务列表 (唯一列表页)")
    click.echo(f"   📊 /flows/<task_id> - 任务流程")
    click.echo(f"   🛑 /review/<task_id> - 用例审核")
    click.echo(f"   📝 /requirement - 新建需求")

    uvicorn.run(
        app,
        host=host,
        port=port,
        reload=reload,
        log_level="warning",  # v0.5.9: 默认 warning, 抑制 asyncio accept error 噪音
    )


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    cli()
