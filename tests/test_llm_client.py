import json

from src.llm_client import LLMClient, LLMConfig


def test_default_config_targets_pateway_anthropic_messages():
    config = LLMConfig()

    assert config.base_url == "https://api.pateway.ai/v1/messages"
    assert config.api_key_env == "PAGETWAY_API_KEY"
    assert config.model == "deepseek-v4.1-flash"
    assert config.api_style == "anthropic"


def test_build_payload_uses_anthropic_messages_format():
    client = object.__new__(LLMClient)
    client.config = LLMConfig()

    payload = client._build_payload(
        prompt="你好",
        system="你是助手",
        max_tokens=128,
        temperature=0.2,
        model=None,
    )

    assert payload["model"] == "deepseek-v4.1-flash"
    assert payload["stream"] is True
    # Anthropic 的 system 是顶层字段, 不是 message
    assert payload["system"] == "你是助手"
    assert payload["messages"] == [{"role": "user", "content": "你好"}]
    assert payload["max_tokens"] == 128
    assert payload["temperature"] == 0.2
    # thinking 必须显式关掉, 否则思考过程会吃掉大量 max_tokens 并混进正文
    assert payload["thinking"] == {"type": "disabled"}


def test_build_payload_keeps_openai_responses_format_available():
    """切回 OpenAI 兼容端点时 (LLM_API_STYLE=openai_responses) payload 格式不能变。"""
    client = object.__new__(LLMClient)
    client.config = LLMConfig(api_style="openai_responses", model="gpt-5.6-luna")

    payload = client._build_payload(
        prompt="你好",
        system="你是助手",
        max_tokens=128,
        temperature=0.2,
        model=None,
    )

    assert payload["model"] == "gpt-5.6-luna"
    assert payload["stream"] is True
    assert payload["input"] == [
        {
            "type": "message",
            "role": "system",
            "content": [{"type": "input_text", "text": "你是助手"}],
        },
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "你好"}],
        },
    ]
    assert payload["max_output_tokens"] == 128
    assert payload["temperature"] == 0.2


def test_extract_stream_text_reads_anthropic_text_delta():
    events = [
        'data: {"type":"message_start","message":{"content":[]}}\n\n',
        'data: {"type":"content_block_start","index":0,'
        '"content_block":{"type":"text","text":""}}\n\n',
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"text_delta","text":"你"}}\n\n',
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"text_delta","text":"好"}}\n\n',
        'data: {"type":"content_block_stop","index":0}\n\n',
        'data: {"type":"message_stop"}\n\n',
    ]

    assert LLMClient._extract_stream_text(events) == "你好"


def test_extract_stream_text_drops_thinking_delta():
    """思考过程不能混进正文, 否则会污染生成的用例 YAML。"""
    events = [
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"thinking_delta","thinking":"先分析需求…"}}\n\n',
        'data: {"type":"content_block_delta","index":1,'
        '"delta":{"type":"text_delta","text":"case_id: CASE-001"}}\n\n',
    ]

    assert LLMClient._extract_stream_text(events) == "case_id: CASE-001"


def test_extract_stream_text_reads_response_output_text_delta():
    events = [
        'data: {"type":"response.output_text.delta","delta":"你"}\n\n',
        'data: {"type":"response.output_text.delta","delta":"好"}\n\n',
        'data: {"type":"response.completed"}\n\n',
        "data: [DONE]\n\n",
    ]

    assert LLMClient._extract_stream_text(events) == "你好"


def test_call_decodes_sse_bytes_as_utf8():
    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def iter_lines(self, decode_unicode):
            assert decode_unicode is False
            return [
                'data: {"type":"response.output_text.delta","delta":"召回"}'.encode("utf-8"),
                b"data: [DONE]",
            ]

    class FakeSession:
        def post(self, *args, **kwargs):
            return FakeResponse()

    client = object.__new__(LLMClient)
    client.config = LLMConfig(max_retries=1)
    client._session = FakeSession()
    client._api_key = "test-key"
    client._api_key_loaded = True

    assert client.call("你好") == "召回"
