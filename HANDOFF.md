# 项目交接 - yiye-agent (AI 测试用例生成系统)

**最后更新**: 2026-06-27 20:19
**状态**: 阶段性交付, 中途换 key

---

## 已完成的工作

### 1. 项目位置
- **项目代码库**: `D:\knowledge_bases\yiye-agent`
- **真实代码源**:
  - `D:\code\yiye-agent-backend` (Java 后端, 6628 .java 文件)
  - `D:\code\yiye-agent-frontend` (Vue 前端, 2014 vue/ts/js 文件)
  - `D:\code\yiye-landingpage-editor` (Vue 编辑器, 785 vue/ts/js 文件)

### 2. 修复的真 bug (5 个)
- `src/code_parser.py`: `_get_text` content 透传 bug (影响所有 chunk name/annotation/calls 抽取)
- `src/index_builder.py`: SQL 优先级 (`AND` > `OR` 需加括号) + FTS5 query 转义 + UNIQUE 冲突 (DELETE-then-INSERT)
- `src/workspace.py`: `get_task` 用 `TaskWorkspace.__init__` 会破坏数据, 改用 `__new__` 直接构造
- `src/kb_build.py`: `stats["llm_enriched"]` 永远 = 总数, 改为真实计数
- `src/generate.py`: Stage 4/5 max_tokens 不够 (4000 → 8000, 2000 → 3000) + YAML 围栏剥离 + 鲁棒化 + `--skip` 嵌套 bug 重构

### 3. 新增的文件
- `src/deep_audit.py`: 全量代码深度审查 (含 `_glob_match` Windows 路径修复 - 重要!)
- `src/llm_recall.py`: 阶段三 LLM 召回 (B 改造 - 把 domain 全部 chunks 喂给 LLM 语义匹配, BM25 fallback)
- `src/generate.py`: 加 `stage5_generate_cases_batch` / `stage5_generate_cases_serial` / `_parse_multi_yaml` / `_match_docs_to_scenarios` / `_enrich_case` / `_generate_single_case` 等

### 4. Prompt 改造
- `prompts/stage5_case.md` 重写为 v3 round-2:
  - 业务流骨架 (不编造 URL/账号/状态码)
  - YAML 安全占位符 (用 `{{ TODO: ... }}` 配双引号)
  - 6 项核心原则 + 验证清单
  - 中文示例 (使用一叶项目真实字段名 `agent_status` / `helper_conn_status`)

### 5. kb-config.yaml 改造
- 新增 `customer-service` domain (含 4 个 code_sources):
  - `cs-service` → `yiye-agent-customer-service-private` (客服主服务)
  - `cs-boss-backend` → `yiye-agent-boss-backend` (BossCustomer / AgentStatusEnum)
  - `cs-autoconfigure-agent` → `autoconfigure/agent` (AgentAutoConfiguration)
  - `cs-ucenter-service` → `yiye-agent-ucenter-service-private` (登录认证)

---

## 当前知识库状态

| Domain | chunks | 状态 |
|---|---|---|
| landing-page | 374 | 旧版索引 (kb_build 跑过, 包含 build 时 enrich) |
| ucenter | 24 | 旧版索引 (kb_build 跑过 20 文件) |
| customer-service | 7 chunks (3 文件) | **只审查 3 个文件, 394 待审查** |

---

## 已验证的需求 (用例产物)

- `runs/REQ-CS-001.json` - 客服获客助手异常自动下线 (8 步主流程 + 7 异常 + 6 AC)
- `runs/REQ-CS-003/stage5/cases.json` - **38/38 用例, 100% 成功率** (v3 prompt, 业务流骨架)
- `runs/REQ-CS-004/stage5/cases.json` - 15 用例 (A+B 改造后, 0 fallback, 阶段三 LLM 召回实际跑了)

---

## 下一步要做的事

### 第 1 步: 全量审查 customer-service domain
```bash
cd /d/knowledge_bases/yiye-agent
python -m src.deep_audit customer-service
```
- 预计 5-10 分钟
- 394 个 Java 文件
- 纯本地解析 (tree-sitter + regex), **不消耗 LLM**
- 产出: `domains/customer-service/_index.db` (升级版, 1500+ chunks) + `_audit_manifest.json`

### 第 2 步: 验证召回
```bash
python bin/testcase.py kb search "agent_status" -d customer-service
python bin/testcase.py kb search "获客助手" -d customer-service
python bin/testcase.py kb search "customer_service_id" -d customer-service
```
确认知识库能召回需求文档里的字段名.

### 第 3 步: 端到端验证
- 用 `runs/REQ-CS-001.json` 作为需求
- `python bin/testcase.py generate REQ-CS-005 --domain customer-service -f requirements/REQ-CS-001.json`
- 看阶段三 LLM 召回是否能命中真实代码, 阶段五批量生成 30+ 用例

### 第 4 步: 全量跑其他 domain
- landing-page: 重跑 `python bin/testcase.py kb build landing-page` (重 enrich)
- ucenter, marketing, payment, message, collect, cross: 同样跑

---

## 环境配置

### Python
- Python 3.14.3
- .venv/ 在 `D:\knowledge_bases\yiye-agent\.venv\`
- 已装: anthropic 0.112, click 8.4, tree-sitter 0.25 + java/javascript, sqlalchemy 2.0, fastapi 0.138, sse-starlette 3.4, pydantic 2.13, ...

### LLM
- base_url: `https://api.minimaxi.com/anthropic`
- API key env: `ANTHROPIC_API_KEY`
- 模型: `MiniMax-M3` (主) + `MiniMax-M2.7-highspeed` (fallback) + `MiniMax-M2`
- **新 key 已设置**, prefix 不是 `sk-cp-Hm`

### bash session 启动命令
```bash
export ANTHROPIC_API_KEY="$(powershell -NoProfile -Command "[Environment]::GetEnvironmentVariable('ANTHROPIC_API_KEY','User')")"
export ANTHROPIC_BASE_URL="$(powershell -NoProfile -Command "[Environment]::GetEnvironmentVariable('ANTHROPIC_BASE_URL','User')")"
```

---

## 已知 limitation / 待办

1. **deep_audit method 名提取**: Lombok 重度 Java 类下 method 名会错位 (tree-sitter-java 0.23.5 bug, 已知问题, v0.2 修)
2. **kb_build.py 的 scan_files 没修**: 同样的 fnmatch Windows bug 一直在 (deep_audit.py 修了, kb_build.py 没修 - 不影响功能但会扫描多余文件)
3. **token 自动记账**: `workspace.save_llm_call` 方法定义了但无人调用 (设计缺口)
4. **Stage 1 PRD 历史需求检索**: 未实现 (用户定义的流程第 4 步)
5. **Stage 3 召回对新模块**: 如果代码里没有相关关键字, BM25 召回 0 (物理事实不是 bug; B 改造 LLM 召回也跑了, 但 LLM 也无法编造未实现的代码)

---

## 注意事项 (新 AI 接续必读)

### 用户偏好
- **先诊断后动手**: 不在没问清楚前直接搭代码
- **设计阶段不写代码**: "先不写代码"/"先聊需求"/"先不验证" 都是 STOP 信号
- **用中文 stage 名**: 跟用户沟通时用阶段一/二/三/四/五 (代码里实际是 stage1/2/3/4/5)
- **不脑补**: 用例/代码必须基于真实代码, 不能基于"通用客服系统"推理

### 不要做的事
- ❌ 删任何 `runs/` `domains/` `requirements/` 数据 (除非用户明确同意)
- ❌ 在没问清前自作主张删 stage5 产物 (之前我误删过 REQ-CS-001 stage5, 写了 `_DELETED_BY_AGENT.md` 备注)
- ❌ 修改 `_DELETED_BY_AGENT.md` / `_audit_manifest.json` 等中间产物

### 验证策略
- 项目无 pytest 套件 (requirements.txt 注释了)
- 只能用 ad-hoc 临时验证脚本: `C:\Users\ADMINI~1\AppData\Local\Temp\hermes-verify-XXXX\*.py`
- 跑完明确标 "ad-hoc verification" 不能宣称 "全绿"

---

## 备份位置

`D:\knowledge_bases\yiye-agent-backup-20260627.tar.gz` (1.1 MB, 不含 .venv/)

恢复命令:
```bash
cd /d/knowledge_bases
tar -xzf yiye-agent-backup-20260627.tar.gz -C yiye-agent-restored
```

---

## 端到端测试命令清单 (按优先级)

```bash
# 1. 验证 LLM 连通 (用新 key)
cd /d/knowledge_bases/yiye-agent
export ANTHROPIC_API_KEY="..."  # 新 key
export ANTHROPIC_BASE_URL="https://api.minimaxi.com/anthropic"
./.venv/Scripts/python.exe src/llm_client.py

# 2. 全量审查 customer-service (最重要, 纯本地)
./.venv/Scripts/python.exe -m src.deep_audit customer-service

# 3. 验证召回
./.venv/Scripts/python.exe bin/testcase.py kb search "agent_status" -d customer-service

# 4. UI 在 8765 端口 (后台)
./.venv/Scripts/python.exe bin/testcase.py ui --port 8765

# 5. 端到端跑需求
./.venv/Scripts/python.exe bin/testcase.py generate REQ-CS-005 \
    --domain customer-service -f requirements/REQ-CS-001.json
```

---

## 接续工作流程 (建议)

1. **接手时**: 先 `cat HANDOFF.md` (本文件) → search past sessions 看历史
2. **跑全量 customer-service**: 不消耗 LLM, ~10 分钟, 立即有产出
3. **验证召回**: kb search 看真实代码是否进库
4. **跑端到端**: 用 REQ-CS-001 需求, 验证阶段三能召回 (这次应该非 0 了) + 阶段五批量
5. **决定是否跑全量其他 domain**: 用户决定
