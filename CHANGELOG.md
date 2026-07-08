# 变更日志 - CHANGELOG

> **记录所有代码/配置变更, 包括修改原因 + 影响评估**
> **任何修改必须在 commit 前先更新本文件**
> **遵循 [Keep a Changelog](https://keepachangelog.com/) 规范**

最后更新: 2026-06-27

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

### 进行中

暂无.

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