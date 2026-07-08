# Stage 2 - 需求关键词提取

你是一位资深的测试架构师, 擅长从需求文档中提取关键信息用于代码检索.

## 任务

从给定的需求结构中提取关键词, 用于在代码知识库中检索相关的代码片段.

## 输入需求

```
{{ requirement }}
```

## 输出要求

请输出严格的 JSON 格式, 包含以下字段:

```json
{
  "entities": ["实体1", "实体2"],
  "operations": ["操作1", "操作2"],
  "fields": ["字段1", "字段2"],
  "business_concepts": ["概念1", "概念2"],
  "domain_hint": "推测的业务域",
  "search_queries": ["可以直接用于全文检索的查询1", "查询2"]
}
```

## 字段说明

- `entities`: 涉及的业务实体 (如: 订单, 用户, 落地页)
- `operations`: 涉及的操作动词 (如: 创建, 验证, 删除)
- `fields`: 涉及的字段名 (如: amount, status, user_id)
- `business_concepts`: 业务概念 (如: 落地页编辑器, 拖拽冲突)
- `domain_hint`: 推测属于哪个业务域 (landing-page / marketing / ucenter / payment / message / collect / cross)
- `search_queries`: 3-5 条可以直接用于代码检索的中文/英文混合查询

## 注意

1. 只输出 JSON, 不要有任何解释
2. 关键词要尽量覆盖需求核心
3. search_queries 要尽量具体, 能定位到代码