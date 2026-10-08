# 变更日志 - CHANGELOG

> **记录所有代码/配置变更, 包括修改原因 + 影响评估**
> **任何修改必须在 commit 前先更新本文件**
> **遵循 [Keep a Changelog](https://keepachangelog.com/) 规范**

最后更新: 2026-10-09

---

## 格式说明

每条变更记录格式:

```markdown
### [YYYY-MM-DD] 简短标题

**类型**: Added / Changed / Fixed / Removed / Refactor / Deviation
**文件**: 涉及的文件
**ADR 引用**: ADR-XXX (相关决策记录)
**原因**: 为什么改
**影响**: 改了什么
**回归检查**:
- [ ] 没违反 PROJECT_CHARTER.md
- [ ] 没改变已记录的 ADR
- [ ] Token 安全检查通过
- [ ] 不破坏现有功能
```

---

## [Unreleased]

### [2026-10-09] 需求内容哈希去重

**类型**: Added
**文件**: `src/req_dedup.py` (新), `src/kb_build.py`, `tests/test_req_dedup.py`
**ADR 引用**: ADR-017
**原因**: `requirements/` 下 46 个 json 实测按内容分组只有 **8 组**,
其中一组 26 个文件(`1.json`/`ce001.json`/`test002.json`/`测试这个.json` ...)
承载同一份「客服获客助手异常自动下线」需求, 差别只在 `req_id` 与 `title`。
不去重则同一批规则被抽 26 遍, 覆盖报告分母被灌水。
**影响**:
- 新增 `src/req_dedup.py`: 规范化(排身份字段 + 循环还原多层转义 + 剥空白) → SHA-256 分组
- 组内代表取**原始文本最短**者(= 转义层数最少, 最干净)
- `_req_files` 改为调用新模块, 36 份 → 8 份

**回归检查**:
- [x] 新增 20 条测试 (含"组内选代表不能退化成按文件名取第一个"的判别性断言)
- [x] 没改变已记录的 ADR
- [x] Token 安全检查通过 (未涉及)
- [x] 不破坏现有功能 (224 → 264 passed)

**踩坑**: 最初用"payload 最大者"做代表选取, 但该指标衡量的是**规范化后**的长度,
而组内哈希相同就意味着规范化结果逐字节相同 —— 该值在组内**恒等**, 完全没有区分力。
测试直接把这个事实钉死(`test_payload_size_is_constant_within_group`)。

---

### [2026-10-09] 新增 cases 表, 用例正文落库

**类型**: Added
**文件**: `src/index_builder.py`, `src/fact_case_generator.py`, `src/business_qa.py`, `tests/test_cases_store.py`
**ADR 引用**: ADR-014
**原因**: `case_fact_map` 只存 case_id → fact_id 映射, 用例正文在内存里生成完就丢。
后果: 业务问答里「这条规则有没有测过」「具体怎么测的」两问只能给一个计数,
给不出用例标题与预期结果 —— 而业务方要的就是后者。
**影响**:
- `FACT_SCHEMA_SQL` 新增 `cases` 表 (标题/维度/前置/步骤/预期/断言/表引用/可判定性)
- `IndexBuilder` 新增 `add_cases()` / `get_cases()` / `search_cases()`
- `add_cases()` **自动回填** `case_fact_map` —— 只写正文不写映射会让覆盖报告全线报未覆盖且不报错
- `FactCaseGenerator.generate()` 改为按 `fact_id` 取 refs, 并支持从 Fact 证据自动推导
- `_build_facts()` 接入用例生成, 构建结束即产出用例与覆盖率
- `BusinessQA` 覆盖度路直接输出用例标题/维度/预期/涉及表, 不再只给计数

**回归检查**:
- [x] 新增 20 条测试
- [x] 没改变已记录的 ADR
- [x] Token 安全检查通过 (未涉及)
- [x] 不破坏现有功能

**顺带修掉的隐患**: `GeneratedCase.to_dict()` 里 `generated_by` 是硬编码字面量,
而 `add_cases` 走 `getattr` 取属性 —— 序列化与入库两个来源不一致。
已提升为正式字段, 单一真源。

---

### [2026-10-09] Fact 层回填工具 + 陈旧索引检测

**类型**: Added
**文件**: `tools/backfill_facts.py` (新), `tests/test_backfill_facts.py`, `bin/testcase.py`
**ADR 引用**: ADR-014
**原因**: 符号索引(解析 9000+ 文件)一次要几十分钟, 但改了 Fact 抽取逻辑后
想验证效果**不需要重建符号索引** —— chunks 与 links 都能从已有索引库复原。
另外 CLI 的 `kb build` 压根没传 `requirements_dir`, Fact 层从未被触发。
**影响**:
- `tools/backfill_facts.py`: 从 `chunks.chunk_data` 复原 CodeChunk,
  从 `sql_index` 复原 SqlStatement, 重缝数据链路后重跑 Fact + 用例, 几十秒完成
- **陈旧索引检测**: 索引里的 `called_methods` 全是裸方法名(不含接收者)时判定为陈旧并拒绝回填
- CLI `kb build` 接入 `requirements_dir`, 新增 `--facts/--no-facts` 与 `--llm/--no-llm`
- CLI 输出补上 `事实/证据不足/用例/规则覆盖` 四个数字

**回归检查**:
- [x] 新增 9 条测试
- [x] 没改变已记录的 ADR
- [x] Token 安全检查通过 (未涉及)
- [x] 不破坏现有功能

**实测发现**: 8 个域的索引**全部陈旧** —— `called_methods` 1249 条全是裸方法名
(`updateComboCount`), 而当前解析器产出的是 `baseMapper.updateComboCount`。
库是 `_walk_calls` 修好之前建的。接收者缺失会让 DataLink 的 exact 分支
(唯一可信的那条)永远走不到, 全域降级为 guess, 而**不报任何错**。
已加显式告警 + 陈旧检测拦停。

---

### [2026-10-09] 修 clear_domain 漏清 Fact 层导致一条用例挂两个 fact_id

**类型**: Fixed
**文件**: `src/index_builder.py`, `tools/backfill_facts.py`, `tests/test_cases_store.py`
**ADR 引用**: ADR-014
**原因**: `case_id` 是 `FACT-0001` 这种序号, 每轮重新生成都会复用同一批编号;
而 `fact_id` 是内容哈希, 需求文件一变就变。`case_fact_map` 主键是
`(case_id, fact_id)`, `INSERT OR REPLACE` **覆盖不掉另一条**。
`clear_domain` 只清符号索引, 不清 facts/cases/case_fact_map。
**影响**:
- 实测 customer-service 重跑后 138 条用例里 **98 条挂着 2 个 fact_id**,
  覆盖报告把一条用例算到两个业务规则头上。**症状只是数字虚高, 全程不报错。**
- 新增 `clear_fact_layer()` 作为单一清理入口; `clear_domain()` 调用它
- `backfill_facts.py` 里内联复制的清库逻辑改为调用同一入口(之前两处各清一半)
- `add_cases()` 写入前先删该 case_id 的旧映射(第二道防线)

**回归检查**:
- [x] 新增 4 条测试, 含全库校验"不存在一条用例挂多个 fact_id"
- [x] 没改变已记录的 ADR
- [x] Token 安全检查通过 (未涉及)
- [x] 不破坏现有功能

**教训**: 同一份清理逻辑写在两处时, 一定会分叉。凡是"多处都要做同一件事",
必须收敛到单一入口。

---

### [2026-10-09] 修 table_refs 装成了 Mapper 方法名而非表名

**类型**: Fixed
**文件**: `src/fact_case_generator.py`, `src/business_qa.py`, `tests/test_cases_store.py`
**ADR 引用**: ADR-014
**原因**: `EV_TABLE` 的 `ref_uri` 存的是 SQL id
(`ai.yiye.dao.XxxMapper.getTotalReportByAgentId`, 为溯源),
真实表名(CSV, 如 `agent_conf,boss_order`)在 `snippet` 里。
`_table_refs_from_evidence()` 取了 `ref_uri`。
**影响**:
- `cases.table_refs` 装满 qualified_stmt, `data_consistency` 维度的"涉及表"
  变成一串 Mapper 方法名, 表级断言立不住 —— 但看起来"有引用", 极易漏检
- 8 个域回填后校验: 含 `Mapper` 的 table_refs 从 98 → **0**
- 顺带补 `classify()` 的问句覆盖: 「手动上线怎么验证的」原本判为 UNKNOWN
  (明明有 18 条用例却答不出来), 现已并入 COVERAGE 路

**回归检查**:
- [x] 新增 2 条测试, 含"table_refs 里混进 Mapper 名"的显式断言
- [x] 没改变已记录的 ADR
- [x] Token 安全检查通过 (未涉及)
- [x] 不破坏现有功能

---

### [2026-10-09] 补齐 kb list / kb stats 两个从未实现的函数

**类型**: Fixed
**文件**: `src/kb_build.py`, `tests/test_cli_commands.py` (新)
**原因**: `bin/testcase.py` 的 `kb list` 与 `kb stats` 引用
`list_domains()` / `show_stats()`, 但这两个函数**在 src/kb_build.py 里从未存在** ——
一敲就 `ImportError`。
**影响**:
- 实现 `list_domains()`: 从 kb-config.yaml 读, 返回配置里定义的域
  (而非仅已建索引的域 —— 用户问的是"这项目该有哪些域")
- 实现 `show_stats()`: **直接查库而非读 index_meta**。Fact 层可以在不重建
  符号索引的情况下反复回填, meta 早已过期, 读它会让用户看到"事实=0"
  而库里明明有 13 条
- `show_stats` 输出的覆盖度行强制附带"只反映有无用例回填, 不代表断言正确"
- 新增 `tests/test_cli_commands.py` 13 条测试

**回归检查**:
- [x] 新增 13 条测试
- [x] 没改变已记录的 ADR
- [x] Token 安全检查通过 (未涉及)
- [x] 不破坏现有功能

**教训(本项目已栽三次)**:
同一类问题 —— 「有测试」≠「覆盖了用户会敲的命令」:
1. `kb search` 调 `searcher.search()` —— 该方法不存在
2. `build_all_domains` 返回 list, CLI 按 dict.items() 迭代
3. `kb list` / `kb stats` 引用从未实现的函数

三次都是用户实际会敲的命令, 而 293 条测试一条都没覆盖。
补测试时要专门盯**命令入口**本身(注册了哪些子命令、参数是否齐全、
被引用的符号是否存在), 而不只是核心算法。

---

### [2026-10-09] kb build 接入 Fact 层并新增 --no-llm

**类型**: Changed
**文件**: `bin/testcase.py`
**原因**: `kb build` 从未传 `requirements_dir`, 所以 Fact 层**从未被 CLI 触发过** ——
只有手工调 `build_all_domains` 才会跑。另一个问题: 默认 `enrich_with_llm=True`,
全量 8 域上万个 chunk 会跑数万次 LLM 调用。
**影响**:
- `kb build` 默认接入 Fact 抽取与用例生成; `--no-facts` 可关闭
- 新增 `--no-llm`: 实测 payment 域 266 chunks 从 **91.3s → 0.3s**(303 倍)
- 构建结果输出补上 `事实/证据不足/用例/规则覆盖` 四个数字

**回归检查**:
- [x] 测试覆盖新参数已注册
- [x] 没改变已记录的 ADR
- [x] Token 安全检查通过 (未涉及)
- [x] 不破坏现有功能

**全量实测(8 域, 5059+2615+1192+468+380+230+91+52 = 10087 文件)**:

| 域 | 文件 | 符号 | SQL | 数据链路 | 事实 | 用例 | 规则覆盖 |
|---|---|---|---|---|---|---|---|
| landing-page | 2615 | 8867 | 575 | 969 | 13 | 168 | 13/13 |
| marketing | 1192 | 3799 | 432 | 534 | 13 | 156 | 13/13 |
| cross | 5059 | 9977 | 205 | 298 | 13 | 123 | 13/13 |
| customer-service | 468 | 1455 | 159 | 246 | 13 | 128 | 13/13 |
| collect | 380 | 1241 | 80 | 102 | 13 | 142 | 13/13 |
| message | 230 | 635 | 16 | 18 | 13 | 130 | 13/13 |
| payment | 91 | 266 | 22 | 24 | 13 | 146 | 13/13 |
| ucenter | 52 | 158 | 16 | 19 | 12 | 92 | 12/12 |

合计: 26398 符号 / 1539 条 SQL / 103 条事实 / 2228 条证据 / 1073 条用例。
**一致性校验全通过**: 多 fact_id 关联 0、孤儿映射 0、孤儿证据 0、
用例数与映射数逐域相等。

---

## [0.1.0] - 2026-06-27

### Added (新增)

#### 项目骨架初始化

- **类型**: Added
- **文件**: 项目根目录所有文件
- **ADR 引用**: ADR-001 ~ ADR-013
- **原因**: 从零开始搭建 AI 测试用例生成系统
- **影响**:
  - 创建 20 个核心文件 (Python + YAML + Markdown)
  - 总代码量: 144.6 KB
  - 实现 6 业务域 + cross 知识库配置
  - 实现 Stage 2-5 完整流程
  - 实现 FastAPI Web UI + SSE 实时推送
- **回归检查**:
  - [x] 没违反 PROJECT_CHARTER.md
  - [x] Token 安全规范严格执行
  - [x] 100/100 AD-HOC 验证通过

#### 核心模块

| 模块 | 文件 | 行数 | 状态 |
|---|---|---|---|
| LLM 客户端 | `src/llm_client.py` | ~330 | ✅ |
| 日志 + 审计 | `src/logger.py` | ~190 | ✅ |
| Prompt 加载 | `src/prompt_loader.py` | ~95 | ✅ |
| 工作区 | `src/workspace.py` | ~330 | ✅ |
| 代码解析 | `src/code_parser.py` | ~380 | ✅ |
| 索引构建 | `src/index_builder.py` | ~410 | ✅ |
| 知识库构建 | `src/kb_build.py` | ~340 | ✅ |
| 用例生成 | `src/generate.py` | ~470 | ✅ |
| Web UI | `src/ui_server.py` | ~300 | ✅ |
| CLI 入口 | `bin/testcase.py` | ~330 | ✅ |

#### Prompt 模板

| 模板 | 用途 | 字数 |
|---|---|---|
| `stage2_extract_keywords.md` | 关键词提取 | 797 |
| `stage3_link.md` | 需求-代码关联 | 801 |
| `stage4_scenarios.md` | 场景推导 | 1115 |
| `stage5_case.md` | 用例生成 | 1794 |

#### 配置文件

| 文件 | 配置项数 | 状态 |
|---|---|---|
| `.config/config.yaml` | 14 配置段 | ✅ |
| `.config/kb-config.yaml` | 6 业务域 + cross | ✅ |
| `.gitignore` | 7 大类敏感排除 | ✅ |
| `requirements.txt` | 14 依赖包 | ✅ |

### Fixed (修复)

#### 删除幻觉包 tree-sitter-vue

- **类型**: Fixed
- **文件**: `requirements.txt`
- **ADR 引用**: ADR-006
- **原因**: PyPI 上不存在 `tree-sitter-vue` 包, pip install 报错
- **影响**:
  - 删除 `tree-sitter-vue>=0.20.0`
  - 删除 `tree-sitter-typescript>=0.20.0` (项目不需要)
  - 升级 `tree-sitter` 系列到 0.21.0+ (兼容 Python 3.12)
  - 注释说明 Vue SFC 用 javascript grammar 处理
- **回归检查**:
  - [x] 没违反 PROJECT_CHARTER.md (技术约束)
  - [x] Tree-sitter 仍为主选方案
  - [x] Python 3.12 兼容性确认

#### 补充安装 Python 依赖

- **类型**: Fixed
- **文件**: 用户 CMD 环境
- **原因**: 用户 CMD 默认 `python` 指向 Python 3.12, 但 anthropic SDK 未装在该 Python 下
- **影响**:
  - 在 Python 3.12 下 `pip install anthropic` 成功
  - LLM 连通性验证: HTTP 200, 1.39 秒
- **回归检查**:
  - [x] Token 安全: key 从环境变量读
  - [x] MiniMax M3 调用成功

### Deviation (偏离)

#### UI 形态偏离: Vue 3 → 内嵌 HTML

- **类型**: Deviation
- **文件**: `src/ui_server.py`
- **ADR 引用**: ADR-005, ADR-013
- **原因**:
  - MVP 阶段优先保证可运行
  - 团队不熟悉 Vue 3 + TypeScript 构建链
  - 内嵌 HTML 已能完成核心 UI 功能
- **影响**:
  - ✅ 功能等价 (任务面板/历史列表/实时推送/域统计)
  - ❌ 前端工程化缺失 (无组件复用, 无独立部署)
  - ❌ 无法做 PWA / 移动端
- **何时回滚**:
  - 团队要多人协作维护 UI
  - 需要做复杂交互 (图表/拖拽)
  - 要部署到生产环境给非技术用户
- **回归检查**:
  - [x] 偏离已在 DECISIONS.md 中标记
  - [x] 不影响核心架构

### Security (安全)

#### Token 凭据泄露事故

- **类型**: Security Incident
- **时间**: 2026-06-27
- **事件**: 用户在对话中发出完整 MiniMax API key (`sk-cp-...`)
- **处置**:
  - 立即警告, 要求撤销
  - 用户在 MiniMax 控制台撤销并生成新 key
  - 新 key 通过 `Win+R` → `sysdm.cpl` 永久环境变量设置
- **教训**:
  - 即使是 AI 对话, 也绝对不要发 token
  - 必须用 `len() + [:8]` 脱敏规范
- **回归检查**:
  - [x] 事故记录到 memory
  - [x] 强化 DECISIONS-011 (Token 安全规范)
  - [x] 后续所有 token 操作零事故

---

## 变更记录模板

每次修改前, 复制以下模板到 `## [Unreleased]` 下面:

```markdown
### [YYYY-MM-DD] 修改标题

- **类型**: [Added/Changed/Fixed/Removed/Refactor/Deviation/Security]
- **文件**: `path/to/file.py`
- **ADR 引用**: ADR-XXX 或 "无"
- **原因**: 为什么改
- **影响**: 改了什么, 可能影响哪些功能
- **回归检查**:
  - [ ] 没违反 PROJECT_CHARTER.md (项目宪法)
  - [ ] 没改变已记录的 ADR (决策记录)
  - [ ] Token 安全检查通过 (如有 token 相关改动)
  - [ ] 不破坏现有功能 (如有功能改动)
  - [ ] 有对应 AD-HOC 验证证据
```

---

## 与其他文档的协作

### 修改前必读

```
PROJECT_CHARTER.md  ← 看项目原则和范围
    ↓
DECISIONS.md        ← 看相关决策
    ↓
本文档 (CHANGELOG.md)  ← 记录本次变更
```

### 修改流程

1. **修改前**:
   - 读 PROJECT_CHARTER.md, 确认不违反
   - 读 DECISIONS.md, 确认不改变既定决策
   - 在 CHANGELOG.md `[Unreleased]` 下加新条目

2. **修改中**:
   - 改代码/配置
   - 保持改动最小化 (避免 scope creep)

3. **修改后**:
   - 跑 AD-HOC 验证
   - 把 `[Unreleased]` 移到具体版本号
   - 在 commit message 引用 CHANGELOG 条目

---

## 重要提醒

⚠️ **CHANGELOG 不是流水账**, 是决策的证据链
⚠️ **重大变更必须有 ADR 引用**
⚠️ **偏离项目宪法的修改必须显式标注**
⚠️ **token 相关的任何修改必须在 Security 段落记录**