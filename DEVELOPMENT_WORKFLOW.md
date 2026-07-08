# 开发工作流 - DEVELOPMENT_WORKFLOW

> **PyCharm 上编辑代码时, 必须遵守的流程**
> **本文档是日常开发的 SOP, 优先级仅次于 PROJECT_CHARTER.md**

最后更新: 2026-06-27

---

## 一、修改代码前的 3 个必读

每次打开 PyCharm 准备改代码, 先读这 3 个文档:

```
1. PROJECT_CHARTER.md  ← 项目宪法 (范围/原则/约束)
2. DECISIONS.md        ← 已有的决策 (为什么这么做)
3. CHANGELOG.md        ← 最近改了什么 (不要重复/冲突)
```

**如果你要做的修改违反上述任何文档, 停下, 先更新文档, 再改代码.**

---

## 二、修改代码的标准流程

### 流程图

```
需求/想法
    │
    ▼
读 PROJECT_CHARTER + DECISIONS + 最近的 CHANGELOG
    │
    ▼
判断: 是否需要新 ADR?
    │
    ├── 是 → 先写 ADR, 评审
    │
    └── 否 ↓
    │
    ▼
判断: 是否违反项目宪法?
    │
    ├── 是 → 修改 PROJECT_CHARTER (需明确理由)
    │
    └── 否 ↓
    │
    ▼
在 CHANGELOG.md [Unreleased] 加新条目
    │
    ▼
改代码 (PyCharm)
    │
    ▼
跑 AD-HOC 验证 (参照之前模板)
    │
    ▼
提交 (commit message 引用 CHANGELOG 条目)
```

### 每个环节详细说明

#### 环节 1: 读 3 个文档 (5 分钟)

打开 PROJECT_CHARTER.md, 重点看:
- 项目范围 (In Scope / Out of Scope)
- 设计原则 (6 条)
- 技术约束 (不可违反 / 可以调整)
- 边界清单

打开 DECISIONS.md, 重点看:
- 相关模块的 ADR
- 是否有冲突的旧决策

打开 CHANGELOG.md, 重点看:
- 最近改了什么
- 有没有 in-progress 的工作

#### 环节 2: 判断 ADR 必要性

**什么时候必须写新 ADR**:
- 引入新技术/新依赖
- 修改核心架构
- 改变已记录的决策
- 偏离 Out of Scope

**什么时候不需要**:
- Bug fix (除非根本原因变化)
- 重构 (内部实现调整)
- 配置项调整 (在已有 ADR 范围内)

#### 环节 3: 判断是否违反项目宪法

**如果违反**, 必须:
1. 修改 PROJECT_CHARTER.md (In Scope / 原则 / 约束)
2. 写新 ADR 解释变更
3. CHANGELOG 标为 "Deviation"

#### 环节 4: 写 CHANGELOG 条目

每条修改的最小记录:

```markdown
### [2026-MM-DD] 简短标题

- **类型**: [Added/Changed/Fixed/Removed/Refactor/Deviation/Security]
- **文件**: path/to/file
- **ADR 引用**: ADR-XXX 或 "无"
- **原因**: 一句话
- **影响**: 一句话
- **回归检查**:
  - [ ] 项目宪法
  - [ ] 决策一致性
  - [ ] Token 安全
  - [ ] 功能不破坏
```

#### 环节 5: 改代码

PyCharm 实践:
- 用 Git 管理 (每个改动一个分支)
- 跑通 AD-HOC 验证后再合并
- 改完跑一遍相关模块

#### 环节 6: AD-HOC 验证

参照之前的 hermes-verify-* 模式:
- 创建临时验证脚本
- 跑通后清理
- 在 commit message 写 "Verified: hermes-verify-XXX"

#### 环节 7: 提交

commit message 格式:

```
<type>: <subject>

<body>

Refs: CHANGELOG.md#2026-MM-DD-标题
Refs: ADR-XXX (如有)
Verified: hermes-verify-XXX (如有)
```

示例:
```
fix: 删除 tree-sitter-vue 幻觉包

PyPI 上不存在 tree-sitter-vue, 导致 pip install 失败.
升级 tree-sitter 系列到 0.21.0 兼容 Python 3.12.

Refs: CHANGELOG.md#2026-06-27-删除幻觉包-tree-sitter-vue
Refs: ADR-006
Verified: hermes-verify-req-v2-155707 (22/22 通过)
```

---

## 三、PyCharm 工作流配置

### 3.1 推荐布局

```
┌──────────────────────────────────────────┐
│  PyCharm Project 视图 (Alt+1)             │
│  ┌──────────┬──────────────────────────┐ │
│  │ Project  │ Editor                   │ │
│  │          │                          │ │
│  │ 📁 yiye-agent                        │ │
│  │  ├─ 📁 .config                       │ │
│  │  ├─ 📁 src                           │ │
│  │  ├─ 📁 prompts                       │ │
│  │  ├─ 📁 bin                           │ │
│  │  ├─ 📄 PROJECT_CHARTER.md            │ │
│  │  ├─ 📄 DECISIONS.md                  │ │
│  │  ├─ 📄 CHANGELOG.md                  │ │
│  │  └─ 📄 README.md                     │ │
│  │          │                          │ │
│  └──────────┴──────────────────────────┘ │
│  ┌──────────────────────────────────────┐│
│  │  Terminal / Run / Debug              ││
│  └──────────────────────────────────────┘│
└──────────────────────────────────────────┘
```

### 3.2 必须安装的插件

| 插件 | 用途 |
|---|---|
| **.ignore** | .gitignore 语法高亮 |
| **Markdown** | 编辑 Markdown 文档 |
| **YAML** | 编辑 YAML 配置 |
| **SQLite** | 查看 _index.db |
| **GitToolBox** | Git 增强 |

### 3.3 推荐快捷键

| 操作 | 快捷键 |
|---|---|
| 打开文件 | Ctrl+Shift+N |
| 最近修改 | Alt+Shift+C (GitToolBox) |
| 跳转到定义 | Ctrl+Click |
| 查找用法 | Alt+F7 |
| 重命名重构 | Shift+F6 |
| 格式化代码 | Ctrl+Alt+L |
| 运行 | Shift+F10 |
| 调试 | Shift+F9 |

### 3.4 运行配置 (Run/Debug Configurations)

为常用命令创建配置:

| 配置名 | Script | Parameters | Working dir |
|---|---|---|---|
| testcase-init | bin/testcase.py | init | 项目根 |
| testcase-kb-build | bin/testcase.py | kb build landing-page --max-files 30 | 项目根 |
| testcase-kb-stats | bin/testcase.py | kb stats landing-page | 项目根 |
| testcase-generate | bin/testcase.py | generate REQ-001 --domain landing-page | 项目根 |
| testcase-ui | bin/testcase.py | ui | 项目根 |
| llm-client-test | src/llm_client.py | (空) | 项目根 |

---

## 四、Git 工作流建议

### 4.1 分支策略

```
main (稳定)
  │
  ├── feature/xxx       (新功能)
  ├── fix/xxx           (修复)
  ├── refactor/xxx      (重构)
  └── deviation/xxx     (偏离项目宪法的修改)
```

### 4.2 Commit 规范

**类型** (type):
- `feat` 新功能
- `fix` 修复
- `refactor` 重构
- `docs` 文档
- `test` 测试
- `chore` 杂项 (依赖/配置)
- `deviation` 偏离 (需 ADR 引用)

**示例**:

```
feat(generate): 添加用例生成 Stage 5

实现了 Stage 5 测试用例生成, 支持 YAML/JSON 输出.

Refs: CHANGELOG.md#2026-06-27-项目骨架初始化
Refs: ADR-009, ADR-010
Verified: hermes-verify-14files-153705 (100/100)
```

### 4.3 提交前 Checklist

- [ ] CHANGELOG.md 已更新
- [ ] 引用了相关 ADR (如有)
- [ ] 代码跑过 AD-HOC 验证
- [ ] 没引入新的幻觉包
- [ ] Token 没硬编码
- [ ] 工作区没有遗留文件

---

## 五、PR/Code Review 检查清单

每次合并前必须检查:

- [ ] **项目宪法**: 不违反 PROJECT_CHARTER.md
- [ ] **决策一致**: 不与 DECISIONS.md 冲突
- [ ] **变更记录**: CHANGELOG.md 已更新
- [ ] **偏离标注**: 偏离 Out of Scope 的修改有 explicit 标注
- [ ] **Token 安全**: 没硬编码任何 key/token
- [ ] **验证证据**: 有 AD-HOC 验证报告
- [ ] **依赖变更**: requirements.txt 变更需说明
- [ ] **配置变更**: .config/*.yaml 变更需说明

---

## 六、日常工作模板

### 6.1 每天开工

1. 打开 PyCharm
2. 看 `git log --oneline -10`
3. 看最近 CHANGELOG
4. 确认要做的修改符合项目宪法

### 6.2 改完代码

1. 跑相关测试
2. 写 AD-HOC 验证 (如必要)
3. 更新 CHANGELOG.md
4. Commit

### 6.3 改完配置

1. 跑 AD-HOC 验证配置正确性
2. 更新 CHANGELOG.md
3. Commit

### 6.4 改完文档

1. 检查内容是否仍准确
2. 更新"最后更新"日期
3. Commit

---

## 七、特殊情况处理

### 情况 1: 紧急修复 (Hotfix)

```
跳过 ADR → 直接改 → CHANGELOG 标 [Hotfix] → 事后补 ADR
```

### 情况 2: 偏离项目宪法

```
1. 必须写新 ADR 解释为什么偏离
2. 修改 PROJECT_CHARTER (如必要)
3. CHANGELOG 标 Deviation
4. 在 commit message 强调
```

### 情况 3: 不确定要不要改

```
问自己 3 个问题:
1. 这个改动是为了解决真实问题吗?
2. 有没有更简单的方案?
3. 未来 6 个月还会需要这个改动吗?

3 个都是 Yes → 改
其他 → 先讨论
```

---

## 八、每月 Review

每月最后一天做一次 Review:

1. **CHANGELOG 完整性**: 所有修改都有记录
2. **ADR 时效性**: 是否有 ADR 已过时
3. **PROJECT_CHARTER**: 项目范围是否变化
4. **代码偏离**: 累积的 Deviation 是否需要回滚
5. **依赖更新**: 是否有可升级的依赖

---

**记住: PROJECT_CHARTER 是宪法, DECISIONS 是判例, CHANGELOG 是案卷.**
**任何修改都要经得起这 3 个文件的检验.**