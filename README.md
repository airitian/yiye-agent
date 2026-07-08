# AI 测试用例生成系统

一叶项目 (yiye-agent) 自动化测试用例生成系统.

通过 AI 分析需求、检索代码、推导场景、生成用例, 让 QA 工作更高效.

---

## ✨ 功能

- **知识库构建**: Tree-sitter 解析代码, SQLite + FTS5 全文索引
- **业务域管理**: 6 个业务域 (landing-page / marketing / ucenter / payment / message / collect)
- **AI 增强**: LLM 自动打业务标签、生成摘要、推导场景
- **测试用例生成**: YAML / JSON / Gherkin / pytest 多格式输出
- **可观测 UI**: Web 界面 + SSE 实时推送
- **审计日志**: 所有写操作全程留痕
- **Token 安全**: API key 永不落盘, 日志自动脱敏

---

## 📦 项目结构

```
yiye-agent/
├── .config/                # 配置文件
│   ├── config.yaml         # 全局配置
│   └── kb-config.yaml      # 知识库配置 (6 业务域)
├── src/                    # 核心代码
│   ├── llm_client.py       # LLM 调用封装
│   ├── logger.py           # 日志 + 审计
│   ├── prompt_loader.py    # Prompt 模板加载
│   ├── workspace.py        # 任务工作区
│   ├── code_parser.py      # Tree-sitter 解析
│   ├── index_builder.py    # SQLite + FTS5 索引
│   ├── kb_build.py         # 知识库构建
│   ├── generate.py         # 用例生成编排
│   └── ui_server.py        # FastAPI Web UI
├── prompts/                # Prompt 模板
│   ├── stage2_extract_keywords.md
│   ├── stage3_link.md
│   ├── stage4_scenarios.md
│   └── stage5_case.md
├── bin/
│   └── testcase.py         # CLI 入口
├── runs/                   # 任务运行数据
├── domains/                # 各业务域索引
├── .logs/                  # 日志
└── README.md
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
ANTHROPIC_API_KEY=sk-cp-你的真key
# 可选: ANTHROPIC_BASE_URL=https://api.minimaxi.com/anthropic
```

**优先级**：`.env` 文件 < 系统环境变量（已有 env 不会被 .env 覆盖，方便 CI 调试）。

**如果 key 泄露**：立即去 MiniMax 控制台撤销并重新签发，旧的 PowerShell 历史 / 对话记录 / 备份文件里都可能有。

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
| `testcase kb build [domain]` | 构建知识库 |
| `testcase kb stats <domain>` | 查看域统计 |
| `testcase kb search <query> -d <domain>` | 代码检索 |
| `testcase kb build --max-files 50` | 限制文件数 (测试) |
| `testcase kb build --no-llm` | 跳过 LLM 增强 |

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

每个 Stage 都有:
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

⚠️ **key 泄露应急**：去 MiniMax 控制台撤销旧 key + 重新签发 + 改 `.env`。

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

- 检查 `ANTHROPIC_API_KEY` 环境变量是否设置
- 确认 key 未撤销 (去 MiniMax 控制台)

### API 调用 404

- 确认模型名: `MiniMax-M3` (1M 上下文, 最新)
- 备选: `MiniMax-M2.7-highspeed`

### 知识库构建慢

- 加 `--no-llm` 跳过 LLM 增强
- 减少 `--max-files` 限制文件数
- 减少 `--workers` 降低并发

---

## 📝 License

内部项目, 仅供一叶团队使用.

---

## 🙏 致谢

- LLM: [MiniMax](https://api.minimaxi.com)
- 代码解析: [Tree-sitter](https://tree-sitter.github.io/)
- Web 框架: [FastAPI](https://fastapi.tiangolo.com/)
- CLI: [Click](https://click.palletsprojects.com/)