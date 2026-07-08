# 项目宪法 - PROJECT CHARTER

> **本文件定义了项目的核心范围、原则和不可违反的边界**
> **修改本文件需要明确说明原因, 并在 CHANGELOG.md 中记录**

最后更新: 2026-06-27

---

## 1. 项目使命 (Mission)

**为一叶项目 (yiye-agent) 提供 AI 驱动的测试用例自动生成能力.**

我们解决的核心问题:
1. 测试用例产出慢 → AI 自动生成
2. 用例覆盖不全 → 多路召回 + 边界值推导
3. 需求变更响应慢 → 增量更新 + 实时检索
4. 测试资产无沉淀 → 知识库 + 用例库

---

## 2. 项目范围 (Scope)

### ✅ 我们做 (In Scope)

| 领域 | 描述 |
|---|---|
| 知识库构建 | 解析代码, 建立索引 (符号 + 倒排) |
| 需求-代码关联 | 给定需求, 找到相关代码 |
| 边界值推导 | 自动识别输入边界 |
| 测试场景生成 | 推导完整的业务场景 |
| 测试用例生成 | 输出结构化用例 (YAML/JSON/Gherkin/pytest) |
| 人工审核 | QA 介入关键节点 |
| 可观测性 | 实时进度 + 历史追溯 + 错误诊断 |

### ❌ 我们不做 (Out of Scope)

| 领域 | 原因 |
|---|---|
| 自动执行测试 | 由 CI 系统负责 (Jenkins/GitLab CI) |
| 自动修复 Bug | 不在测试用例生成范围内 |
| 自动部署 | 由运维系统负责 |
| 替代测试人员 | AI 是助手, 不是替代 |
| 支持非 Java/Vue 项目 | 第一版只支持 Spring Boot + Vue 2/3 |
| 处理图像/视频需求 | 当前不支持 UI 原型图分析 |
| 多语言支持 (i18n) | 当前仅中文 |

---

## 3. 设计原则 (Principles)

### 原则 1: **可观测性优先 (Observability First)**
> 任何流程都必须可追溯、可中断、可恢复

体现:
- 每个 Stage 独立保存状态
- 每个 LLM 调用有完整日志
- 失败可以重跑 (从失败节点继续)

### 原则 2: **Token 安全 (Token Safety)**
> API key 永不落盘, 永远从环境变量读取

体现:
- 代码里没有任何 token 字面值
- 日志输出只用 `len()` + `[:8]`
- `.gitignore` 排除所有敏感文件

### 原则 3: **配置与代码分离 (Config > Code)**
> 任何可配置项都不能硬编码

体现:
- 6 个业务域在 `kb-config.yaml` 定义
- LLM 参数在 `config.yaml` 定义
- Prompt 模板在 `prompts/*.md` 独立管理

### 原则 4: **提示词与代码解耦 (Prompt as Data)**
> Prompt 是数据, 不是代码

体现:
- Prompt 在 `prompts/` 目录以 Markdown 管理
- 可独立迭代, 不需要改 Python 代码

### 原则 5: **失败友好 (Fail-Friendly)**
> 失败时给用户明确的指引, 不留谜团

体现:
- 错误信息包含上下文
- LLM 调用失败有重试 + 降级
- 知识库可重建, 不丢历史数据

### 原则 6: **MVP 优先 (MVP First)**
> 先跑起来, 再优化

体现:
- Phase 1 只做核心链路 (Stage 2-5)
- Phase 2 再补 Git hook / golden set / 向量检索

---

## 4. 技术约束 (Technical Constraints)

### 不可违反

| 约束 | 原因 |
|---|---|
| LLM 必须用 `MiniMax M-series` | 团队已采购, 不要切其他 |
| Token 必须从环境变量读 | 安全硬约束 |
| Python >= 3.12 | 已验证可用 |
| 不引入 `tree-sitter-vue` 等幻觉包 | PyPI 上不存在 |
| 配置文件必须是 YAML | 人类可读, 注释友好 |
| 代码风格: line length 100, type hints | 团队约定 |

### 可以调整

| 项 | 说明 |
|---|---|
| 单文件模块大小 | 不超过 500 行 |
| 函数复杂度 | 圈复杂度 < 10 |
| 测试覆盖率 | MVP 阶段不强求 |

---

## 5. 业务约束 (Business Constraints)

| 约束 | 说明 |
|---|---|
| 数据不出内网 | 知识库本地存储, 不上传云端 |
| 审计日志必留 | 所有写操作必须留痕 |
| 用例必须人工审核 | AI 生成的用例不能直接执行 |
| 失败兜底 | LLM 调用失败必须有降级方案 |

---

## 6. 边界清单 (Boundary List)

### 边界 1: 不要做 UI 前端工程化 (Phase 1)
- 不引入 npm/yarn/webpack
- 不拆分独立 Vue 项目
- 内嵌 HTML 已经够用

### 边界 2: 不支持真实 PRD 处理 (Phase 1)
- Stage 1 (PRD 提炼) 暂未实现
- 等真实 PRD 接入再做

### 边界 3: 不做跨域实体索引 (Phase 1)
- Phase 1 只做单域检索
- 跨域关联 Phase 2 实施

### 边界 4: 不做隐式调用识别 (Phase 1)
- 反射/AOP/Event 不追踪
- Phase 2 实施

### 边界 5: 不做向量检索 (Phase 1)
- 仅用 AST + FTS5
- MiniMax embedding 待验证

---

## 7. 验收标准 (Acceptance Criteria)

### Phase 1 (MVP) 验收
- [x] 知识库能构建成功 (至少 1 个业务域)
- [x] 检索能返回相关代码 (Top-10 命中率 ≥ 60%)
- [x] 能生成结构化用例 (至少 20 条/需求)
- [x] UI 可访问, 显示任务进度
- [x] Token 安全规范严格执行

### Phase 2 验收
- [ ] 召回率 ≥ 80%
- [ ] Git hook 自动触发增量更新
- [ ] 真实 PRD 处理 (Stage 1)
- [ ] 跨域影响面分析

---

## 8. 修改本文件的规则

### 何时可以修改
- 项目范围发生重大变化
- 团队达成新的共识
- Phase 转换 (Phase 1 → Phase 2)

### 修改流程
1. 在 `CHANGELOG.md` 记录修改原因
2. 修改 `DECISIONS.md` 记录决策过程
3. 修改本文件
4. 在 commit message 中明确引用

### 不可修改
- "我们不做" 清单 (Out of Scope)
- 安全相关约束
- LLM 厂商绑定

---

## 9. 与其他文档的关系

```
PROJECT_CHARTER.md (本文件) ← 最高优先级
    │
    ↓ 定义
    │
DECISIONS.md  ← 决策记录 (为什么)
    │
    ↓ 实施
    │
CHANGELOG.md  ← 变更日志 (改了什么)
    │
    ↓ 沉淀
    │
README.md     ← 使用说明
```

---

## 10. 项目元信息

| 项 | 值 |
|---|---|
| 项目代号 | yiye-agent |
| 当前 Phase | Phase 1 (MVP) |
| 当前版本 | v0.1.0 |
| Python 版本 | 3.12+ |
| LLM | MiniMax M-series |
| 主要模块 | 6 业务域 + cross |
| 最后更新 | 2026-06-27 |

---

**⚠️ 修改本文件前, 请先在 DECISIONS.md 中记录决策过程.**
**📝 任何代码变更, 请同步到 CHANGELOG.md.**