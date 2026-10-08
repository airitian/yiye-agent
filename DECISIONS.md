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
| ADR-014 | 索引单元从「代码符号」升为「业务事实 Fact」 | ✅ Accepted | 2026-10-09 |
| ADR-015 | 业务事实必须过 ≥2 组独立证据门禁 | ✅ Accepted | 2026-10-09 |
| ADR-016 | 七维测试用例由程序枚举展开, 不靠提示词建议 | ✅ Accepted | 2026-10-09 |
| ADR-017 | 需求文档按内容哈希去重, 不按文件名 | ✅ Accepted | 2026-10-09 |

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

## ADR-014: 索引单元从「代码符号」升为「业务事实 Fact」

### 状态
✅ Accepted (2026-10-09)

### 背景
原系统的索引单元是**代码符号**, 检索返回代码片段。实测端到端跑通后
30 条用例里, 需求原文的三条硬规则覆盖 **0/3**:
「手动上线」「禁止自动上线」「历史数据不作处理」一次都没出现。

诊断发现责任环节在 Stage3 召回: 需求里的业务规则在代码中**根本没有对应字面量**
(`grep "上线"` 在 customer-service 域注释与符号里 0 命中),
所以检索必然召不回, 模型只能围着一个孤立方法编技术边界用例
(null/超长/DB超时)。

### 决策
新增 Fact 层, 把「业务事实」提升为一等索引单元:
一条规则 = 条件 + 动作 + 约束 + 影响面, 并通过 `fact_evidence`
指向全部出处(需求段号 / 代码行 / SQL / 表)。用例生成变成"照事实翻译",
业务问答变成"事实 + 出处"。

### 理由
- ✅ 业务规则有了承载单元, 不再依赖代码里是否存在字面量
- ✅ 每条结论可追溯到具体位置, 业务方能自行核验
- ✅ 覆盖度可量化: 「N 条规则各被几条用例覆盖」变成一条 SQL 查询
- ❌ 抽取质量依赖需求文档质量 —— 需求写得含糊, 事实就含糊
- ❌ 多了一层存储与同步(Fact 与符号索引可能不同步, 故有陈旧检测)

### 后果
- 正面影响: 业务规则覆盖从 0/3 到 100%, 且七维全部生成
- 负面影响: 构建变慢(需先解析代码再抽事实), 索引体积增大

### 何时回滚
若 Fact 抽取的准确率长期低于人工标注基线, 且人工复核成本高于收益。

---

## ADR-015: 业务事实必须过 ≥2 组独立证据门禁

### 状态
✅ Accepted (2026-10-09)

### 背景
反幻觉是本系统的核心价值, 但"有出处"这个要求本身不构成约束 ——
一条规则挂一个 `ref_uri` 就满足形式要求, 内容照样可能是编的。

### 决策
`FACT_MIN_EVIDENCE = 2`, 按 `ev_group` 计独立证据源:
- 需求按**段**分组 (`req:1.json#background:L3`)
- 代码按**文件**分组
- 同一 SQL 链路的 SQL 与表算**一组**

证据不足的事实不删, 记进 `rejected` —— 那些是"知道存在但证据不足"的
知识缺口, 是要去补需求/补代码的待办清单。

### 理由
- ✅ 把"不知道"显式暴露成待办, 而不是藏在一份看起来很全的文档里
- ✅ 同源证据不算数: 需求原文 + 它的翻译稿 = 1 组, 不是 2 组
- ❌ 会拒收一部分真实但冷门的规则 —— 这是**有意的代价**, 门禁没有为了好看而放水

### 后果
- 正面影响: 库里的每条事实都至少有两条独立路径支撑
- 负面影响: 实测 309 条候选拒收 9 条, 需人工判断是知识缺口还是抽取缺陷

### 何时回滚
不设回滚条件 —— 这是反幻觉的核心机制, 放宽等于放弃项目的立身之本。

---

## ADR-016: 七维测试用例由程序枚举展开, 不靠提示词建议

### 状态
✅ Accepted (2026-10-09)

### 背景
旧实现的七个测试维度写在提示词里让模型"建议覆盖"。实测输出:
`exception 9 / happy_path 6 / boundary 6 / performance 3 / permission 3 /
compatibility 3` —— **并发幂等与数据一致性两维从未独立生成过**,
`compatibility` 被当成了兜底筐(缓存一致性、白名单特殊处理都塞在里面)。

### 决策
改为程序判断维度选择:
- 每条事实必有 `happy_path` + `exception`(基本盘)
- 含禁止/必须约束 → 必须有 `forbid`
- 有表证据(SQL/TABLE) → 必须有 `data_consistency` + `idempotent`
- 需求含可度量信号(长度/数量/时间/上限) → 必须有 `boundary`
- 涉及状态/权限/角色 → 追加 `permission`

### 理由
- ✅ 维度覆盖从"模型自觉"变成"代码保证", 可测可断言
- ✅ 数据一致性与幂等依赖表证据, 没表证据硬生成只会产出填不出值的空壳用例
- ❌ 程序化模板的表述多样性不如 LLM 自由生成

### 后果
- 正面影响: 七维全部生成, 且每条维度的预期结果含可判定锚点
- 负面影响: 步骤文案偏模板化, 需人工润色

### 何时回滚
若 LLM 生成质量的提升速度明显快于模板可接受度, 可改为"程序定维度 + LLM 填内容"
的混合模式(当前 LLM 角色已被压到只润色表述)。

---

## ADR-017: 需求文档按内容哈希去重, 不按文件名

### 状态
✅ Accepted (2026-10-09)

### 背景
`requirements/` 下有 46 个 json。文件名完全不可靠 ——
真正的需求也可能叫 `debug-test.json`, 而调试残留反而叫 `REQ-CS-001.json`。
早期只按"是否含 3 个连续反斜杠"过滤转义损坏副本, 结果 47 → 36 份,
但按内容分组后这 36 份里**只有 8 组**, 其中一组 26 个文件承载的是同一份需求,
差别只在 `req_id` 与 `title`。

不���重的后果不是"文件多了点", 而是**统计口径失真**:
同一批规则被抽 26 遍, 覆盖报告的分母被灌水, 「覆盖 100%」变成自欺欺人。

### 决策
按规范化后的内容 SHA-256 分组, 规范化包含三步:
1. 排除身份字段(`req_id` / `title` / `domain` / 时间戳 / `actors` ...)
2. 循环还原多层反斜杠转义至收敛
3. 剥掉全部空白

组内代表取**原始文本最短**的那份(= 转义层数最少, 最干净)。

### 理由
- ✅ 排除 `req_id`/`title` 是关键 —— `ce001.json` 与 `1.json` 的
  background 完全一致, 只差这两个字段; 算进哈希则 26 份一份都合并不掉
- ✅ 组内选最干净而非最"丰富": 规范化后组内 payload 恒等,
  "信息量最大"这个指标根本没有区分力, 而转义层数多的副本才是真风险
- ❌ 规范化规则可能过激 —— 若两份需求只有 title 不同而业务规则相同, 会被合并

### 后果
- 正面影响: 需求从 36 份降到 8 份, 事实与覆盖统计口径恢复真实
- 负面影响: 若规范化误判, 两份不同需求可能被合并(需靠 dup_map 日志核对)

### 何时回滚
出现"两份业务不同的需求被合并"的实际案例时, 收紧规范化规则。

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