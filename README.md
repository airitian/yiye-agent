# AI 测试用例生成系统

一叶项目 (yiye-agent) 自动化测试用例生成系统.

核心不是"让 AI 编用例", 而是**先把业务规则从需求里抽成带出处的事实(Fact),
再照事实展开测试维度**。所有结论都必须能指回需求段号 / 代码行 / SQL / 表,
指不回去的规则一律拒收, 而不是让模型编一条看起来合理的用例。

---

## ✨ 功能

### 索引层

- **知识库构建**: Tree-sitter 解析代码, SQLite + FTS5 全文索引
- **业务域管理**: 7 个业务域 + cross (landing-page / marketing / ucenter / payment / message / collect / cross)
- **跨文件调用图**: Controller → Service → Mapper 三级降级消歧, 同名歧义**不连边**
- **数据链路缝合**: Service → Mapper → SQL → 表 全链打通 (394 Mapper / 1427 条 SQL)
- **Mapper XML 解析**: 抽出高可信表名, 剔除 CTE 名, 动态标签降级标记
- **中文注释索引**: 551 处中文行/块/注解独立成列可检索

### 业务事实层 (Fact)

- **证据门禁**: 一条事实必须有 ≥2 组**独立**证据才允许入库 (需求原文 + 它的翻译稿算 1 组)
- **需求内容去重**: 46 份需求按内容哈希去重后只剩 8 份真实业务内容
- **七维用例展开**: 功能流程 / 异常 / 禁止约束 / 边界 / 权限 / 数据一致性 / 并发幂等
- **反幻觉**: 生成前从证据抽白名单, 生成后逐词校验, 编了就拦下
- **可判定性体检**: 预期结果必须含具体值 (状态码/数字/引号文案), "页面正常显示"这种写法会被标出

### 业务问答

- **五路问答**: 规则解释 / 影响面 / 覆盖度 / 代码定位 / 口径一致性, 按问句类型分路
- **铁律无出处不回答**: 检索不到就明确说"没查到", 不退化成通用解释
- **多跳影响面**: 表 → SQL → Mapper/符号 → Fact 四跳

### 工程

- **可观测 UI**: Web 界面 + SSE 实时推送
- **审计日志**: 所有写操作全程留痕
- **Token 安全**: API key 永不落盘, 日志自动脱敏
- **264 条自动化测试**: 覆盖每一条"本该有结果"的静默失效路径

---

## 📦 项目结构

```
yiye-agent/
├── .config/                     # 配置文件
│   ├── config.yaml              # 全局配置
│   └── kb-config.yaml           # 知识库配置 (7 业务域 + cross)
├── src/
│   ├── code_parser.py           # Tree-sitter 解析 (Java/Vue/JS)
│   ├── comment_parser.py        # 中文注释抽取
│   ├── xml_parser.py            # Mapper XML → SQL → 表
│   ├── call_graph.py            # 跨文件调用图 + 三级消歧
│   ├── data_link.py             # Service→Mapper→SQL→表 全链缝合
│   ├── fact_extractor.py        # 业务事实抽取 + 证据门禁 + 幻觉校验
│   ├── req_dedup.py             # 需求内容哈希去重
│   ├── fact_case_generator.py   # 七维用例程序化展开
│   ├── business_qa.py           # 五路业务问答
│   ├── index_builder.py         # SQLite + FTS5 索引 (含 Fact/Case 表)
│   ├── kb_build.py              # 知识库构建编排
│   ├── llm_client.py            # LLM 调用 (Anthropic / OpenAI 双协议)
│   ├── generate.py              # 传统 Stage 流水线
│   └── ui_server.py             # FastAPI Web UI
├── tools/
│   └── backfill_facts.py        # Fact 层回填 (不重建符号索引)
├── prompts/                     # Prompt 模板 (Prompt as Data)
├── bin/testcase.py              # CLI 入口
├── tests/                       # 264 条 pytest
├── requirements/                # 需求 JSON (自动按内容去重)
├── domains/                     # 各业务域索引库
├── runs/                        # 任务运行数据
└── .logs/                       # 日志
```

---

## 🚀 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置 API Key

**API key 写到项目根的 `.env` 文件里**（已在 .gitignore，不会进版本控制）。

```bash
# 复制模板
cp .env.example .env       # macOS/Linux
copy .env.example .env     # Windows cmd
```

编辑 `.env`：
```env
RIGHTAPI_API_KEY=你的 rightapi key
# 可选: RIGHTAPI_BASE_URL=https://www.rightapi.ai/codex/v1/responses
```

**优先级**：`.env` 文件 < 系统环境变量（已有 env 不会被 .env 覆盖，方便 CI 调试）。

- **如果 key 泄露**：立即去 rightapi 控制台撤销并重新签发，旧的 PowerShell 历史 / 对话记录 / 备份文件里都可能有。

### 3. 验证连通

```bash
python src\llm_client.py
```

预期输出:
```
✅ 客户端初始化成功
✅ 调用成功
   响应: OK
```

### 4. 初始化项目

```bash
python bin\testcase.py init
```

### 5. 构建第一个业务域

```bash
python bin\testcase.py kb build landing-page
```

首次构建可能需要 10-30 分钟 (含 LLM 增强).

### 6. 启动 UI

```bash
python bin\testcase.py ui
```

打开 http://127.0.0.1:18888

### 7. 生成测试用例

```bash
python bin\testcase.py generate REQ-001 --domain landing-page
```

---

## 📚 CLI 命令

### 知识库管理

| 命令 | 说明 |
|---|---|
| `testcase kb list` | 列出所有业务域 |
| `testcase kb build [domain]` | 构建知识库 + 抽 Fact + 生成用例 |
| `testcase kb build <domain> --no-llm` | 跳过 LLM 打标签 (全量构建强烈建议) |
| `testcase kb build <domain> --no-facts` | 只建符号索引, 不抽 Fact |
| `testcase kb stats <domain>` | 查看域统计 |
| `testcase kb search <query> -d <domain>` | 代码检索 |
| `testcase kb build --max-files 50` | 限制文件数 (测试) |
| `python -m tools.backfill_facts` | Fact 层回填, 不重建符号索引 |
| `python -m tools.backfill_facts <domain>` | 只回填指定域 |

### 测试用例生成

| 命令 | 说明 |
|---|---|
| `testcase generate <req-id> -d <domain>` | 生成用例 |
| `testcase generate REQ-001 -d lp --skip stage3` | 跳过 Stage 3 |
| `testcase generate REQ-001 -d lp -f req.json` | 从文件加载需求 |

### 任务管理

| 命令 | 说明 |
|---|---|
| `testcase tasks` | 查看历史任务 |
| `testcase task <task-id>` | 查看任务详情 |

### 可观测 UI

| 命令 | 说明 |
|---|---|
| `testcase ui` | 启动 Web UI (默认 18888 端口) |
| `testcase ui --port 9000` | 指定端口 |

---

## 🏗️ 架构

### 主线: Fact 驱动的用例生成 (推荐路径)

```
需求 JSON ─┬─→ 内容哈希去重 (46 份 → 8 份真实内容)
           │
           └─→ 段落切分 → 候选事实
                          │
              ┌───────────┴───────────┐
              │  证据门禁 ≥2 组独立证据 │  ← 不足则记入 rejected(知识缺口待办)
              │  幻觉校验 verify_grounding│
              └───────────┬───────────┘
                          ↓ 入库 facts + fact_evidence(带 ref_uri/snippet)
                          │
                    七维程序化展开
                          ↓
              cases(用例正文) + case_fact_map(覆盖映射)
                          ↓
              业务问答: 规则/影响面/覆盖度/定位/口径 五路检索
```

### 副线: Stage 流水线 (LLM 场景推导, 保留)

```
用户输入 (需求/代码变更)
    ↓
Stage 2: 关键词提取 (LLM)
    ↓
Stage 3: 需求-代码关联 (多路召回 + LLM 重排序)
    ↓
Stage 4: 场景推导 (LLM)
    ↓
Stage 5: 用例生成 (LLM × N)
    ↓
人工审核 → 入库
```

### 三条设计铁律

1. **无出处不回答** —— 任何结论都要带 ref_uri(需求段号 / 文件:行号 / SQL id / 用例号),
   检索不到就明确说"没查到"。没有出处的业务问答, 业务方无法验证就不敢用。
2. **证据不足就拒收** —— 一条事实凑不够 2 组独立证据时不入库, 记进 `rejected`。
   这会把"不知道"显式暴露成待办清单, 而不是藏在一份看起来很全、实际全是猜的文档里。
3. **宁可不成链, 不可连错链** —— 调用图消歧失败、SQL 表名不可信时都不连。
   连错链会让用例照着错出处断言真实字段, 比不连危害大得多。

### 每个 Stage 都有
- 输入输出到 `runs/<req-id>/stage<N>/`
- 节点状态到 SQLite (`task.db`)
- 详细日志到 `.logs/`
- LLM 调用记录到 SQLite

---

## 🔒 安全

- ✅ API key 从项目 `.env` 文件读取（已在 .gitignore），不写入代码/config.yaml
- ✅ 日志输出 token 时只显示 `len() + prefix[:8]`
- ✅ `.gitignore` 排除 `.env` / `.env.*` / `*.key` / `*.secret` 等所有敏感文件
- ✅ 审计日志记录所有写操作
- ✅ 异常信息自动脱敏 (`redact_text()`)
- ✅ 优先级：`.env` < 系统环境变量（env 可临时覆盖，方便 CI 调试）

⚠️ **key 泄露应急**：去 rightapi 控制台撤销旧 key + 重新签发 + 改 `.env`。

---

## 📊 业务域

| 域 | 后端服务 | 前端 |
|---|---|---|
| **landing-page** | landing-page-service / show / action | 主项目 landing-page/ + 编辑器整项目 |
| **marketing** | marketing + 5 个 data-* + material-store | 主项目营销模块 |
| **ucenter** | ucenter-service | 主项目 login/ |
| **payment** | payment-service | (内部) |
| **message** | message + sms | (内部) |
| **collect** | 5 个 collect-* + ad-trace | (内部) |
| **cross** | gateway / domain / common / sdk | 主项目公共部分 |

详细配置见 `.config/kb-config.yaml`

---

## 🛠️ 开发

### 添加新的 Prompt 模板

1. 在 `prompts/` 创建 `stage<N>_<name>.md`
2. 用 `{{ 变量名 }}` 占位
3. 在代码中用 `loader.render("stage<N>_<name>", 变量名=值)`

### 添加新的 Stage

1. 在 `prompts/` 创建模板
2. 在 `src/generate.py` 加 stage 函数
3. 在 `src/workspace.py` 加 stage 状态表

---

## 🐛 故障排查

### ImportError: anthropic SDK 未安装

```bash
pip install anthropic
```

### API 调用 401

- 检查 `RIGHTAPI_API_KEY` 环境变量是否设置
- 确认 key 未撤销 (去 rightapi 控制台)

### API 调用 404

- 确认模型名: `gpt-5.6-luna`

### 知识库构建慢

- 加 `--no-llm` 跳过 LLM 增强
- 减少 `--max-files` 限制文件数
- 减少 `--workers` 降低并发

---

## 📝 License

内部项目, 仅供一叶团队使用.

---

## 🙏 致谢

- LLM: [rightapi](https://www.rightapi.ai)
- 代码解析: [Tree-sitter](https://tree-sitter.github.io/)
- Web 框架: [FastAPI](https://fastapi.tiangolo.com/)
- CLI: [Click](https://click.palletsprojects.com/)