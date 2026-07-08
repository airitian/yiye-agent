"""
把 REQ-CS-001-e2e 36 个用例渲染成 HTML 报告.

设计:
- 顶部: summary (36 用例 / 0 失败 / 召回 7 / 高置信 3)
- 左侧: 卡片网格, 每张卡片是一个用例 (case_id, title, priority, type)
- 展开: 步骤 / 断言 / 数据 / 真实代码字段 vs LLM 编字段
- 搜索框: 实时过滤 case_id/title/steps
- 颜色: P0 红 / P1 橙 / P2 绿
"""
import json
import html
import re
import sys
from pathlib import Path
from datetime import datetime

PROJECT = Path(r'D:\knowledge_bases\yiye-agent')
CASES_PATH = PROJECT / 'runs' / 'REQ-CS-001-e2e' / 'stage5' / 'cases.json'
LINKS_PATH = PROJECT / 'runs' / 'REQ-CS-001-e2e' / 'stage3' / 'links.json'
SCENARIOS_PATH = PROJECT / 'runs' / 'REQ-CS-001-e2e' / 'stage4' / 'scenarios.json'
OUT = PROJECT / 'runs' / 'REQ-CS-001-e2e' / 'report.html'

# v0.3.5: 支持环境变量 (TASK_DIR 覆盖默认路径)
import os as _os
_TASK_DIR_OVERRIDE = _os.environ.get('TASK_DIR')
_REQ_ID_OVERRIDE = _os.environ.get('REQ_ID')
_TASK_ID_OVERRIDE = _os.environ.get('TASK_ID')
if _TASK_DIR_OVERRIDE:
    _task_dir = Path(_TASK_DIR_OVERRIDE)
    CASES_PATH = _task_dir / 'stage5' / 'cases.json'
    LINKS_PATH = _task_dir / 'stage3' / 'links.json'
    SCENARIOS_PATH = _task_dir / 'stage4' / 'scenarios.json'
    OUT = _task_dir / 'report.html'


def esc(s):
    return html.escape(str(s)) if s else ''


EMOJI_FALLBACKS = {
    "✅": "[OK]",
    "⚠️": "[WARN]",
    "⚠": "[WARN]",
    "❌": "[ERR]",
}


def _safe_print(text: str):
    stream = sys.stdout
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
        print(text)
    except UnicodeEncodeError:
        fallback = text
        for src, dst in EMOJI_FALLBACKS.items():
            fallback = fallback.replace(src, dst)
        print(fallback)


def _extract_steps_robust(raw_text):
    """从 raw_text 提取 steps 字段 (容忍 YAML 错误)

    策略:
    1. 替换 {{ ... }} 占位符 (避免未闭合引号破坏 YAML)
    2. 用 yaml.safe_load
    3. 失败时用正则匹配 "- step: N" 模式
    """
    import yaml as _y
    clean = re.sub(r'\{\{[^}]*\}\}', 'PLACEHOLDER', raw_text)
    try:
        doc = _y.safe_load(clean)
        if isinstance(doc, dict):
            s = doc.get('steps', [])
            if s:
                return s
    except Exception:
        pass
    # fallback: 正则
    m = re.search(r'^steps:\s*\n(.*?)(?=^\S|\Z)', clean, re.DOTALL | re.MULTILINE)
    if m:
        block = m.group(1)
        steps = []
        for line_block in re.split(r'\n(?=\s*-\s*step:)', block):
            step_match = re.search(r'-\s*step:\s*(\d+)', line_block)
            if not step_match:
                continue
            step_no = int(step_match.group(1))
            action_m = re.search(r'action:\s*(.+?)(?=\n\s*expected:|\n\s*data:|\Z)', line_block, re.DOTALL)
            expected_m = re.search(r'expected:\s*(.+?)(?=\n\s*data:|\Z)', line_block, re.DOTALL)
            steps.append({
                'step': step_no,
                'action': action_m.group(1).strip() if action_m else '',
                'expected': expected_m.group(1).strip() if expected_m else '',
            })
        return steps
    return []


def render_steps(steps, raw_text=None):
    # Fallback: 如果 steps 字段为空但 raw_text 有 YAML, 简单解析
    if not steps and raw_text:
        # v0.3.3: 更宽容的 fallback - 多种策略
        steps = _extract_steps_robust(raw_text)
    if not steps:
        return '<div class="empty">无步骤</div>'
    out = '<ol class="steps">'
    for s in steps:
        if isinstance(s, dict):
            n = s.get('step', s.get('no', ''))
            action = esc(s.get('action', s.get('operation', '')))
            expected = esc(s.get('expected', s.get('expected_result', '')))
            data = esc(s.get('data', ''))
            out += f'<li><strong>Step {esc(n)}</strong>'
            if action:
                out += f'<div class="action">操作: {action}</div>'
            if expected:
                out += f'<div class="expected">预期: {expected}</div>'
            if data:
                out += f'<div class="data">数据: {data}</div>'
            out += '</li>'
        else:
            out += f'<li>{esc(s)}</li>'
    out += '</ol>'
    return out


def render_assertions(asserts):
    if not asserts:
        return ''
    out = '<div class="asserts"><h4>断言</h4><ul>'
    for a in asserts:
        if isinstance(a, dict):
            out += f'<li>{esc(a.get("description", a.get("name", "")))}</li>'
        else:
            out += f'<li>{esc(a)}</li>'
    out += '</ul></div>'
    return out


def render_test_data(data):
    if not data:
        return ''
    if isinstance(data, dict):
        out = '<div class="data"><h4>测试数据</h4>'
        for k, v in data.items():
            out += f'<div><span class="key">{esc(k)}:</span> <span class="val">{esc(v)}</span></div>'
        out += '</div>'
        return out
    return f'<div class="data"><h4>测试数据</h4><pre>{esc(data)}</pre></div>'


def render_case(c):
    cid = esc(c.get('case_id', '?'))
    title = esc(c.get('title', '(无标题)'))
    prio = esc(c.get('priority', 'P2'))
    scenario_type = esc(c.get('scenario_type', ''))
    precond = c.get('preconditions', [])
    steps = c.get('steps', [])
    asserts = c.get('assertions', c.get('expected_result', []))
    data = c.get('test_data_setup', {})
    desc = esc(c.get('description', ''))

    # Priority color
    prio_class = {'P0': 'p0', 'P1': 'p1', 'P2': 'p2'}.get(prio, 'p2')

    raw_text = c.get('raw_text', '')

    # v0.3.2: fallback - 如果 steps/assertions/preconditions 为空, 尝试从 raw_text 解析
    import yaml as _y
    import re as _re
    if raw_text and (not precond or not steps or not asserts):
        clean = _re.sub(r'\{\{[^}]*\}\}', 'PLACEHOLDER', raw_text)
        try:
            doc = _y.safe_load(clean)
            if isinstance(doc, dict):
                if not precond:
                    precond = doc.get('preconditions', []) or doc.get('preconditions_detail', [])
                if not asserts:
                    asserts = doc.get('assertions', []) or doc.get('expected_result', [])
                if not data:
                    data = doc.get('test_data_setup', {})
        except Exception:
            pass

    out = f'''
    <div class="case" data-prio="{prio}" data-type="{scenario_type}">
      <div class="case-header">
        <span class="case-id">{cid}</span>
        <span class="badge {prio_class}">{prio}</span>
        <span class="badge type">{scenario_type}</span>
      </div>
      <h3 class="case-title">{title}</h3>
      {f'<div class="desc">{desc}</div>' if desc else ''}
      <div class="meta">
        {f'<div class="preconds"><h4>前置条件</h4><ul>' + ''.join(f'<li>{esc(p)}</li>' for p in precond) + '</ul></div>' if precond else ''}
        {render_test_data(data)}
        {render_steps(steps, raw_text=raw_text)}
        {render_assertions(asserts)}
      </div>
    </div>
    '''
    return out


def render_summary(cases, links, scenarios):
    n = len(cases)
    n_p0 = sum(1 for c in cases if c.get('priority') == 'P0')
    n_p1 = sum(1 for c in cases if c.get('priority') == 'P1')
    n_p2 = sum(1 for c in cases if c.get('priority') == 'P2')
    types = {}
    for c in cases:
        t = c.get('scenario_type', 'unknown')
        types[t] = types.get(t, 0) + 1

    # 真实代码字段 vs LLM 编字段
    real_fields = ['onlineStatus', 'offlineStatus', 'DISABLE', 'AUTO',
                   'acquisitionLinkAnomalyOffline', 'WorkWechatCustomerAcquisitionLinkListener']
    llm_fields = ['agent_status', 'helper_conn_status', 'auto_offline']
    real_count = {}
    llm_count = {}
    for f in real_fields:
        n_real = sum(1 for c in cases if f in json.dumps(c, ensure_ascii=False))
        if n_real:
            real_count[f] = (n_real, f'{100*n_real/n:.0f}%')
    for f in llm_fields:
        n_llm = sum(1 for c in cases if f in json.dumps(c, ensure_ascii=False))
        if n_llm:
            llm_count[f] = (n_llm, f'{100*n_llm/n:.0f}%')

    type_rows = ''.join(
        f'<tr><td>{esc(t)}</td><td>{cnt}</td></tr>'
        for t, cnt in sorted(types.items(), key=lambda x: -x[1])
    )

    real_rows = ''.join(
        f'<tr><td><code>{esc(f)}</code></td><td>{cnt[0]}/{n}</td><td>{cnt[1]}</td></tr>'
        for f, cnt in real_count.items()
    )
    llm_rows = ''.join(
        f'<tr><td><code>{esc(f)}</code></td><td>{cnt[0]}/{n}</td><td>{cnt[1]}</td></tr>'
        for f, cnt in llm_count.items()
    )

    n_links = len(links)
    high_conf = sum(1 for l in links if l.get('relevance', 0) >= 4)
    n_scenarios = len(scenarios)

    return f'''
    <div class="summary-grid">
      <div class="summary-card">
        <h3>📊 用例</h3>
        <div class="big-num">{n}</div>
        <div class="sub">P0={n_p0} · P1={n_p1} · P2={n_p2}</div>
        <table class="types">
          <tr><th>类型</th><th>数</th></tr>
          {type_rows}
        </table>
      </div>
      <div class="summary-card">
        <h3>🔗 召回 (Stage 3)</h3>
        <div class="big-num">{n_links}</div>
        <div class="sub">高置信≥4: {high_conf}</div>
        <p style="font-size:12px;color:#888;">{n_scenarios} 场景 / 0 失败</p>
      </div>
      <div class="summary-card real-fields">
        <h3>✅ 真实代码字段</h3>
        <table>
          <tr><th>字段</th><th>用例</th><th>比例</th></tr>
          {real_rows}
        </table>
      </div>
      <div class="summary-card llm-fields">
        <h3>⚠️ LLM 编字段</h3>
        <table>
          <tr><th>字段</th><th>用例</th><th>比例</th></tr>
          {llm_rows}
        </table>
      </div>
    </div>
    '''


def main():
    cases = json.load(open(CASES_PATH, encoding='utf-8'))
    links = json.load(open(LINKS_PATH, encoding='utf-8'))
    scenarios = json.load(open(SCENARIOS_PATH, encoding='utf-8'))

    summary = render_summary(cases, links, scenarios)
    cards = '\n'.join(render_case(c) for c in cases)
    n = len(cases)

    generated_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    html_doc = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>REQ-CS-001 测试用例报告</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif;
         margin: 0; padding: 20px; background: #f5f7fa; color: #2c3e50; }}
  .container {{ max-width: 1400px; margin: 0 auto; }}
  h1 {{ color: #2c3e50; border-bottom: 3px solid #3498db; padding-bottom: 10px;
        margin-bottom: 24px; }}
  .toolbar {{ background: white; padding: 16px; border-radius: 8px;
              margin-bottom: 16px; display: flex; gap: 12px; flex-wrap: wrap;
              box-shadow: 0 2px 4px rgba(0,0,0,0.06); }}
  .toolbar input, .toolbar select {{ padding: 8px 12px; border: 1px solid #ddd;
              border-radius: 6px; font-size: 14px; }}
  .toolbar input {{ flex: 1; min-width: 200px; }}
  .summary-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
                  gap: 16px; margin-bottom: 24px; }}
  .summary-card {{ background: white; padding: 18px; border-radius: 8px;
                  box-shadow: 0 2px 4px rgba(0,0,0,0.06); }}
  .summary-card h3 {{ margin: 0 0 8px 0; font-size: 14px; color: #555; }}
  .big-num {{ font-size: 36px; font-weight: 700; color: #3498db; }}
  .sub {{ font-size: 13px; color: #888; margin-top: 4px; }}
  .summary-card table {{ width: 100%; margin-top: 8px; font-size: 12px;
                          border-collapse: collapse; }}
  .summary-card th, .summary-card td {{ padding: 4px 6px; text-align: left;
                                          border-bottom: 1px solid #f0f0f0; }}
  .summary-card th {{ color: #888; font-weight: 500; }}
  .real-fields {{ border-left: 4px solid #27ae60; }}
  .llm-fields {{ border-left: 4px solid #e67e22; }}
  code {{ background: #f4f6f9; padding: 2px 6px; border-radius: 3px;
          font-family: "SF Mono", Consolas, monospace; font-size: 12px; }}
  .case {{ background: white; border-radius: 8px; padding: 16px 20px;
          margin-bottom: 12px; box-shadow: 0 2px 4px rgba(0,0,0,0.06);
          border-left: 4px solid #ddd; }}
  .case[data-prio="P0"] {{ border-left-color: #e74c3c; }}
  .case[data-prio="P1"] {{ border-left-color: #f39c12; }}
  .case[data-prio="P2"] {{ border-left-color: #27ae60; }}
  .case-header {{ display: flex; align-items: center; gap: 8px;
                  margin-bottom: 8px; flex-wrap: wrap; }}
  .case-id {{ font-family: "SF Mono", Consolas, monospace; font-size: 12px;
              color: #555; background: #f4f6f9; padding: 2px 8px; border-radius: 4px; }}
  .badge {{ display: inline-block; padding: 2px 10px; border-radius: 10px;
            font-size: 11px; font-weight: 600; }}
  .badge.p0 {{ background: #fde8e8; color: #c0392b; }}
  .badge.p1 {{ background: #fff3cd; color: #d68910; }}
  .badge.p2 {{ background: #d5f5e3; color: #229954; }}
  .badge.type {{ background: #ebf5fb; color: #2874a6; }}
  .case-title {{ margin: 0 0 8px 0; font-size: 16px; color: #2c3e50; }}
  .desc {{ font-size: 13px; color: #555; margin-bottom: 8px;
            font-style: italic; }}
  .meta {{ font-size: 13px; }}
  .meta h4 {{ margin: 10px 0 4px 0; font-size: 12px; color: #888;
              text-transform: uppercase; letter-spacing: 0.5px; }}
  .preconds ul, .asserts ul {{ margin: 4px 0; padding-left: 20px; }}
  .preconds li, .asserts li {{ margin-bottom: 2px; }}
  .steps {{ padding-left: 20px; }}
  .steps li {{ margin-bottom: 8px; padding: 6px 10px; background: #f8f9fa;
                border-radius: 4px; border-left: 3px solid #3498db; }}
  .action {{ color: #2c3e50; }}
  .expected {{ color: #27ae60; margin-top: 2px; font-size: 12px; }}
  .data {{ color: #7f8c8d; margin-top: 2px; font-size: 12px; }}
  .key {{ color: #2874a6; font-weight: 500; }}
  .empty {{ color: #999; font-style: italic; }}
  pre {{ background: #2c3e50; color: #ecf0f1; padding: 8px; border-radius: 4px;
        font-size: 12px; overflow-x: auto; }}
  .data pre {{ background: #f4f6f9; color: #2c3e50; }}
  .footer {{ text-align: center; color: #888; font-size: 12px; margin-top: 40px;
              padding: 16px; }}
  .hidden {{ display: none; }}
</style>
</head>
<body>
<div class="container">
  <h1>🧪 REQ-CS-001 客服获客助手连接异常自动下线 - 测试用例报告</h1>
  <div class="toolbar">
    <input type="text" id="filter" placeholder="🔍 搜索 case_id / title / 步骤内容..." />
    <select id="prioFilter">
      <option value="">所有优先级</option>
      <option value="P0">P0 (最高)</option>
      <option value="P1">P1</option>
      <option value="P2">P2</option>
    </select>
    <select id="typeFilter">
      <option value="">所有类型</option>
    </select>
  </div>
  {summary}
  <h2 style="color:#2c3e50;">📋 测试用例 ({n} 个)</h2>
  <div id="cases">
    {cards}
  </div>
  <div class="footer">
    生成时间: {generated_at} · 数据源: REQ-CS-001-e2e run-20260629-110251-e7f11cb0
  </div>
</div>
<script>
const filterInput = document.getElementById('filter');
const prioFilter = document.getElementById('prioFilter');
const typeFilter = document.getElementById('typeFilter');

// 自动填充类型选项
const types = new Set();
document.querySelectorAll('.case').forEach(c => {{
  const t = c.dataset.type;
  if (t) types.add(t);
}});
[...types].sort().forEach(t => {{
  const opt = document.createElement('option');
  opt.value = t;
  opt.textContent = t;
  typeFilter.appendChild(opt);
}});

function applyFilters() {{
  const q = filterInput.value.toLowerCase();
  const prio = prioFilter.value;
  const type = typeFilter.value;
  let visible = 0;
  document.querySelectorAll('.case').forEach(c => {{
    const text = c.textContent.toLowerCase();
    const ok = (!q || text.includes(q)) && (!prio || c.dataset.prio === prio) && (!type || c.dataset.type === type);
    c.classList.toggle('hidden', !ok);
    if (ok) visible++;
  }});
}}
filterInput.addEventListener('input', applyFilters);
prioFilter.addEventListener('change', applyFilters);
typeFilter.addEventListener('change', applyFilters);
</script>
</body>
</html>
'''

    with open(OUT, 'w', encoding='utf-8') as f:
        f.write(html_doc)

    _safe_print(f"✅ 已生成: {OUT}")
    _safe_print(f"   大小: {OUT.stat().st_size:,} 字节")
    _safe_print(f"   用例: {len(cases)}")
    _safe_print(f"   打开: file:///{OUT.as_posix()}")


if __name__ == '__main__':
    main()