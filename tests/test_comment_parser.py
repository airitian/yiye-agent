# -*- coding: utf-8 -*-
"""
中文注释与注解抽取测试

为什么这些用例重要:
  业务词只能从中文注释里召回。如果抽取逻辑漏抽或误抽,
  「客服异常自动下线」这类业务语义在检索层就彻底消失。
"""

from src.comment_parser import extract_chinese_comments, extract_terms_from_comments


JAVA_SAMPLE = '''/**
 * 客服配置服务
 */
public class AgentConfService extends BaseService {

    // 更新客服状态为下线
    @ApiOperation("客服异常自动下线")
    public void updateStatusByAgentId(Long agentId) {
        // 详见 https://example.com/doc  (纯URL 行, 不该被抽)
        cache.put(agentId, "在线");
    }

    /**
     * 查询客服在线状态
     * @param agentId 客服ID
     */
    public String queryAgentStatus(Long agentId) {
        return "ACTIVE";
    }
}
'''


# ============================================================
# 基本抽取
# ============================================================

def test_extracts_line_block_and_annotation():
    text = extract_chinese_comments(JAVA_SAMPLE)
    assert "客服配置服务" in text          # 块注释(javadoc)
    assert "更新客服状态为下线" in text    # 行注释
    assert "查询客服在线状态" in text      # javadoc 第二段
    assert "客服ID" in text               # @param
    assert "客服异常自动下线" in text      # 注解值


def test_annotation_keeps_annotation_name():
    text = extract_chinese_comments(JAVA_SAMPLE)
    assert "ApiOperation" in text


def test_pure_english_code_yields_empty():
    """纯英文代码不该产出垃圾"""
    assert extract_chinese_comments("public void foo(int a) { return a + 1; }") == ""


def test_empty_input_safe():
    assert extract_chinese_comments("") == ""
    assert extract_chinese_comments(None) == ""


# ============================================================
# 噪声过滤
# ============================================================

def test_url_line_not_extracted():
    """纯 URL 行即使含中文也不是业务语义"""
    text = extract_chinese_comments(
        '// 参考文档 https://example.com\npublic class A {}')
    assert "https://example.com" not in text


def test_javadoc_decorators_stripped():
    text = extract_chinese_comments("/**\n * 客服服务\n */\nclass A {}")
    assert "*" not in text
    assert "/" not in text
    assert "客服服务" in text


def test_deduplicates_preserving_order():
    """同一句注释常在块注释里重复出现, 应去重"""
    code = "// 客服状态\n/* 客服状态 */\nclass A {}"
    text = extract_chinese_comments(code)
    assert text.count("客服状态") == 1


# ============================================================
# 术语粗提
# ============================================================

def test_extracts_chinese_terms():
    terms = extract_terms_from_comments("客服配置服务 更新客服状态为下线 获客助手")
    assert "客服配置服务" in terms
    assert "获客助手" in terms


def test_terms_ignore_non_chinese():
    assert extract_terms_from_comments("update agent status online") == []


def test_terms_are_candidates_only_not_guaranteed():
    """
    实测: 粗提会把「客服异常自动」这种半截词也提出来。
    这是刻意接受的 —— 它只是候选, 入库前需人工确认。
    本测试把这个已知缺陷固定下来, 防止有人误以为它是精确的。
    """
    terms = extract_terms_from_comments("客服异常自动下线")
    assert len(terms) >= 1
    # 所有词长度都应在 2-6 字之间, 不该出现超长片段
    assert all(2 <= len(t) <= 6 for t in terms)
