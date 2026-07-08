# Stage 1 — PRD 提炼

**目标**: 从原始需求 (background / actors / main_flow) 提炼结构化输入, 给 Stage 2 关键词抽取用。

## 输入

```
{{ requirement_json }}
```

## 输出 (YAML)

```yaml
# core_scenes: 核心业务场景 (从 main_flow 提炼, 每条要足够具体可测)
core_scenes:
  - name: 场景名 (简短动词短语)
    actor: 谁参与
    preconditions: 前置条件 (逗号分隔)
    summary: 一句话描述
    
# edge_cases: 边界/异常 (从 main_flow 推断或补充)
edge_cases:
  - name: 边界条件
    trigger: 触发场景
    
# key_concepts: 关键业务概念 (给 Stage 2 关键词抽取用, 优先搜索这些词)
key_concepts:
  - 名词 1
  - 名词 2
  - ...

# acceptance_criteria: 验收标准 (从 background 提炼)
acceptance_criteria:
  - 标准 1
  - 标准 2
```

## 规则

1. **必须基于输入**: 不要编造 main_flow 没提到的场景
2. **保持简洁**: core_scenes ≤ 5, edge_cases ≤ 5, key_concepts ≤ 10
3. **业务术语**: key_concepts 用需求里的真实业务词 (例: "获客助手", "长连接", "客服状态"), 不要技术术语
4. **actor 复用**: 必须从 requirement.actors 里选