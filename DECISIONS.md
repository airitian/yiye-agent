# 决策记录 - DECISIONS

> **记录所有重要的架构/技术/流程决策, 以及为什么这么选**
> **格式参考 ADR (Architecture Decision Records)**
> **每条决策一旦确定, 不要轻易变更; 真要变更, 写新条目覆盖**

最后更新: 2026-06-27

---

## 索引

| ID | 决策 | 状态 | 日期 |
|---|---|---|---|
| ADR-001 | LLM 厂商选 MiniMax M-series | ✅ Accepted | 2026-06-27 |
| ADR-002 | LLM 调用走 Anthropic SDK 协议 | ✅ Accepted | 2026-06-27 |
| ADR-003 | 知识库本地存储, 不上云 | ✅ Accepted | 2026-06-27 |
| ADR-004 | 业务域按业务划分, 不按前后端 | ✅ Accepted | 2026-06-27 |
| ADR-005 | UI 用内嵌 HTML, 不拆独立 Vue 3 项目 | ⚠️ Deviation | 2026-06-27 |
| ADR-006 | 代码解析用 Tree-sitter, javalang 备选 | ✅ Accepted | 2026-06-27 |
| ADR-007 | 索引用 SQLite + FTS5 | ✅ Accepted | 2026-06-27 |
| ADR-008 | 向量检索 Phase 1 不启用 | ✅ Accepted | 2026-06-27 |
| ADR-009 | Prompt 作为数据 (Markdown 模板) | ✅ Accepted | 2026-06-27 |
| ADR-010 | Phase 1 范围: Stage 2-5 | ✅ Accepted | 2026-06-27 |
| ADR-011 | Token 安全: 环境变量 + 严格日志 | ✅ Accepted | 2026-06-27 |
| ADR-012 | 用例必须人工审核 | ✅ Accepted | 2026-06-27 |
| ADR-013 | UI 用 FastAPI + 内嵌 HTML | ⚠️ Deviation | 2026-06-27 |

---

## ADR-001: LLM 厂商选 MiniMax M-series

### 状态
✅ Accepted (2026-06-27)

### 背景
需要选定一个 LLM 厂商作为默认调用方, 替代 Claude 原生 (因为 CCSwitch 转发不稳定).

### 决策
**主模型**: `MiniMax-M3` (1M 上下文, Agent 优化, 最新)
**备选**: `MiniMax-M2.7-highspeed` (快速), `MiniMax-M2` (编码)

### 理由
- ✅ 团队已采购 MiniMax, 成本可控
- ✅ M3 是 1M 上下文, 适合长 PRD + 代码分析
- ✅ 支持 Anthropic 协议 (项目里其他代码可以无缝接入)
- ✅ 中文场景表现良好

### 后果
- 绑定 MiniMax 厂商, 切换需重新评估
- 依赖 MiniMax API 稳定性

---

## ADR-002: LLM 调用走 Anthropic SDK 协议

### 状态
✅ Accepted (2026-06-27)

### 背景
MiniMax 支持多种协议 (Anthropic 兼容 / OpenAI 兼容), 该用哪个?

### 决策
**统一用 Anthropic SDK**, `base_url = https://api.minimaxi.com/anthropic`

### 理由
- ✅ Anthropic SDK 在团队项目里已有使用经验
- ✅ 工具调用 (tool use) 支持完整
- ✅ Streaming / Extended Thinking 支持
- ✅ 错误处理更友好

### 后果
- 环境变量名固定为 `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL`
- 不兼容 OpenAI 生态的库

---

## ADR-003: 知识库本地存储, 不上云

### 状态
✅ Accepted (2026-06-27)

### 背景
数据敏感性: 一叶项目代码和 PRD 不允许出内网.

### 决策
**所有数据存储在本地文件系统 + SQLite + Chroma (本地模式)**

### 理由
- ✅ 数据不出内网, 满足合规要求
- ✅ 无需额外部署服务
- ✅ Git 私有仓库管理配置, 易于备份

### 后果
- 不能跨机器共享知识库 (除非手动同步)
- 团队规模大时需要考虑分布式方案

---

## ADR-004: 业务域按业务划分, 不按前后端

### 状态
✅ Accepted (2026-06-27)

### 背景
有 6 个候选方案:
1. 按前后端划分 (frontend-knowledge / backend-knowledge)
2. 按业务域划分 (landing-page / marketing / ...)
3. 按微服务划分 (每个服务一个库)
4. 不划分 (一个大库)

### 决策
**按业务域划分, 6 业务域 + cross**

### 理由
- ✅ 业务域天然有清晰的归属 (landing-page / marketing / payment ...)
- ✅ 跨域影响面分析有基础
- ✅ 多人可按域分工
- ✅ 跟团队实际组织结构对齐

### 6 个业务域
```
landing-page    → 落地页 (3 后端 + 1 前端模块 + 1 独立编辑器)
marketing       → 营销 (6 后端服务)
ucenter         → 用户中心 (1 后端 + login 前端)
payment         → 支付 (1 后端)
message         → 消息 (2 后端)
collect         → 采集 (5 后端)
+ cross         → 跨域公共 (网关/SDK/前端公共)
```

### 后果
- 业务域划分后, 不能跨域检索 (Phase 2)
- 域的数量可能随业务变化 (调整 kb-config.yaml)

---

## ADR-005: UI 形态选内嵌 HTML, 不拆独立 Vue 3 项目 ⚠️

### 状态
⚠️ Deviation (2026-06-27)

### 背景
原计划: FastAPI + 独立 Vue 3 前端项目 (npm run dev)

### 决策
**实际实现: FastAPI + 内嵌 HTML 单文件**

### 偏差原因
- MVP 阶段优先保证可运行, 减少构建链
- 内嵌 HTML 已能完成任务面板/历史列表/实时推送
- 避免引入 npm 生态 (团队不熟悉 Vue 3 + TypeScript)

### 影响
- ✅ 功能等价 (任务进度 + 历史追溯 + 域统计)
- ❌ 前端工程化缺失 (无组件复用, 无独立部署)
- ❌ 无法做 PWA / 移动端

### 何时回滚
- 团队要多人协作维护 UI
- 需要做复杂交互 (图表/拖拽)
- 要部署到生产环境给非技术用户

---

## ADR-006: 代码解析用 Tree-sitter, javalang 备选

### 状态
✅ Accepted (2026-06-27)

### 背景
Java 代码解析方案选型.

### 决策
**主选: tree-sitter + tree-sitter-java**
**备选: javalang** (注释中, 不实际启用)

### 理由
- ✅ Tree-sitter 跨语言, 未来加 Python/Go 也方便
- ✅ Tree-sitter 0.21+ 支持 Python 3.12
- ✅ javalang 对 Java 信息抽取更准, 但生态较窄

### 实际包依赖
```
tree-sitter>=0.21.0
tree-sitter-java>=0.21.0
tree-sitter-javascript>=0.21.0  # 用于解析 Vue <script>
```

**注意**: 不引入 `tree-sitter-vue` (PyPI 上不存在)

### 后果
- 反射/AOP 解析有限 (Phase 2 加)
- <template>/<style> 暂不解析 (Phase 2 加 @vue/compiler-sfc)

---

## ADR-007: 索引用 SQLite + FTS5

### 状态
✅ Accepted (2026-06-27)

### 背景
代码索引存储方案选型.

### 决策
**SQLite + FTS5 全文检索**

### 理由
- ✅ 零运维, 单文件
- ✅ FTS5 中文分词友好
- ✅ 几百万 chunk 量级够用
- ✅ Python 内置 sqlite3, 无额外依赖

### 索引结构
```
symbols      → 符号表 (函数/类/字段)
relations    → 调用关系图
code_fts     → FTS5 倒排索引
chunks       → 完整 chunk 数据
index_meta   → 元信息
```

### 何时升级
- chunk > 1000 万 → 考虑 Postgres
- 需要复杂聚合 → 考虑 DuckDB

---

## ADR-008: 向量检索 Phase 1 不启用

### 状态
✅ Accepted (2026-06-27)

### 背景
要不要用 embedding 做语义检索?

### 决策
**Phase 1 不启用**, 用 AST + FTS5 已够 MVP.

### 理由
- ✅ MiniMax embedding 实际能力未验证
- ✅ AST 符号检索 + FTS5 关键词 + 业务标签检索已经 3 路召回
- ✅ 召回率目标 MVP 60-70%, 不需要向量

### 何时启用
- Phase 2, 当 AST + FTS5 召回率 < 80%
- 或 LLM 增强场景 (语义补全)

---

## ADR-009: Prompt 作为数据 (Markdown 模板)

### 状态
✅ Accepted (2026-06-27)

### 背景
Prompt 模板怎么管理?

### 决策
**Prompt 放在 `prompts/*.md` 独立管理, 用 `{{变量}}` 占位符**

### 理由
- ✅ Prompt 是数据, 不是代码
- ✅ 业务人员也能修改 Prompt (不需要懂 Python)
- ✅ 模板可独立迭代, 调 Prompt 不需要改代码
- ✅ Markdown 易读, 可版本对比

### 现有模板
```
prompts/stage2_extract_keywords.md  → 关键词提取
prompts/stage3_link.md              → 需求-代码关联
prompts/stage4_scenarios.md         → 场景推导
prompts/stage5_case.md              → 用例生成
```

---

## ADR-010: Phase 1 范围: Stage 2-5

### 状态
✅ Accepted (2026-06-27)

### 背景
MVP 应该包含哪些 Stage?

### 决策
**Phase 1 只做 Stage 2-5**, Stage 1 (PRD 提炼) 等真实 PRD 来了再做.

### 理由
- ✅ 没有真实 PRD, Stage 1 跑不通
- ✅ Stage 2-5 是核心价值链, 必须先打通
- ✅ MVP 验证可行后, Stage 1 是水到渠成

### Phase 2 待办
- Stage 1: PRD 提炼 + 缺口识别
- Stage 4.5: 业务场景初始清单 (从代码反向推导)
- 用例库管理: generated → approved → executed 状态流转
- 缺陷库接入

---

## ADR-011: Token 安全规范

### 状态
✅ Accepted (2026-06-27)

### 背景
API key 安全是硬约束.

### 决策
**6 条铁律**:
1. API key 只从环境变量读 (代码里无字面值)
2. 日志输出只用 `len()` + `[:8]` (永不全量 echo)
3. 异常信息自动脱敏 (regex 识别 Bearer/sk-...)
4. `.gitignore` 排除所有 `.env` / `*.key` / `*.token`
5. 审计日志记录所有 token 相关操作
6. 任何代码评审必查 token 规范

### 实现位置
```
src/llm_client.py:
  - mask_token(value) → str
  - redact_text(text) → str
  - safe_repr(obj) → str
```

### 违规案例
❌ 任何时候不要把 token 写到:
- 代码注释
- 配置文件
- 日志
- 测试数据
- 提交信息
- **对话历史** (即使是给 AI 看)

---

## ADR-012: 用例必须人工审核

### 状态
✅ Accepted (2026-06-27)

### 背景
AI 生成的用例能否直接执行?

### 决策
**必须人工审核**: generated → approved → executed

### 理由
- ✅ AI 幻觉不可避免, 100% 通过率会掩盖问题
- ✅ QA 经验沉淀到用例库
- ✅ 漏测责任不能全推给 AI

### 流程
```
AI 生成 (generated)
   ↓ QA 审核
通过 → approved
拒绝 → 删/修改 → approved
   ↓ 测试执行
   ↓
executed
```

### 关键审核点
1. 业务场景是否合理
2. 输入数据是否真实
3. 断言是否准确
4. 优先级是否合理

---

## ADR-013: UI 用 FastAPI + 内嵌 HTML

### 状态
⚠️ Deviation (2026-06-27)

(同 ADR-005, 此处不重复)

---

## 如何新增决策

```markdown
## ADR-XXX: 决策标题

### 状态
✅ Accepted / ⚠️ Deviation / ❌ Deprecated

### 背景
问题描述.

### 决策
最终方案.

### 理由
- ✅ 优点 1
- ✅ 优点 2
- ❌ 缺点 / 风险

### 后果
- 正面影响
- 负面影响

### 何时回滚
触发回滚的条件.
```

新增 ADR 时:
1. 在索引表加一行
2. 在底部加新章节
3. 编号递增
4. 状态变更不删旧 ADR, 写新 ADR 覆盖

---

**任何决策变更, 必须先修改本文件, 再改代码.**