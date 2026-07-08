# Stage 4 - 业务场景推导

你是一位资深的测试架构师, 擅长从需求和代码中推导完整的测试场景.

## 任务

基于需求描述和关联代码, 推导出该需求需要测试的所有业务场景.

## 需求描述

```
{{ requirement }}
```

## 关联代码

以下是与该需求相关的代码片段:

{{ linked_chunks }}

## 输出要求

请输出严格的 JSON 数组, 每个元素是一个测试场景:

```json
[
  {
    "scenario_id": "SC-001",
    "name": "场景名称",
    "description": "场景详细描述",
    "type": "happy_path | boundary | exception | permission | compatibility | performance",
    "priority": "P0 | P1 | P2",
    "actors": ["涉及的角色"],
    "preconditions": ["前置条件"],
    "trigger": "触发条件",
    "main_flow": ["步骤1", "步骤2"],
    "expected_result": "预期结果",
    "related_chunks": ["关联代码 ID"]
  }
]
```

## 场景类型说明

- `happy_path`: 正常流程
- `boundary`: 边界值 (数值/字符串/集合/时间边界)
- `exception`: 异常流程 (网络错误/数据错误/系统错误)
- `permission`: 权限相关 (无权限/越权/角色切换)
- `compatibility`: 兼容性 (浏览器/设备/版本)
- `performance`: 性能相关 (并发/响应时间/资源)

## 优先级

- `P0`: 核心功能, 必须测试
- `P1`: 重要功能, 应该测试
- `P2`: 边缘情况, 时间允许测试

## 推导原则

1. **覆盖主流程**: 必须有 happy_path
2. **边界值**: 每个参数都要考虑边界
3. **异常覆盖**: 至少 30% 的场景是 exception
4. **权限**: 涉及多角色的需求必须有 permission 场景
5. **数量**: 至少生成 20 个场景, 不超过 40 个

## 注意

只输出 JSON 数组, 不要有任何解释. 场景 ID 从 SC-001 开始递增.