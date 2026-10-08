"""
LLM 客户端封装 - 严格遵守 Token 安全规范

【安全铁律】
1. API key 优先从 .env 文件读取 (项目级配置), 其次环境变量
2. 日志输出永远只用 len() + 前 8 位, 绝不全量 echo
3. 任何异常信息自动脱敏
4. 不缓存 key 到内存超过必要时间

【.env 加载优先级】
系统环境变量 > .env 文件
(已存在的环境变量不会被 .env 覆盖, 方便 CI/调试时临时注入)
"""

import os
import re
import time
import logging
import json
import requests
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ============================================================
# 安全工具函数
# ============================================================

# 常见 token 模式 (用于异常信息脱敏)
TOKEN_PATTERNS = [
    re.compile(r'sk-[a-zA-Z0-9_\-]{20,}'),           # MiniMax sk-cp-...
    re.compile(r'sk-ant-[a-zA-Z0-9_\-]{20,}'),       # Anthropic sk-ant-...
    re.compile(r'Bearer\s+[a-zA-Z0-9_\-\.]{20,}'),   # Bearer xxx
    re.compile(r'api[_-]?key["\']?\s*[:=]\s*["\']?([a-zA-Z0-9_\-]{16,})', re.IGNORECASE),
]


def mask_token(value: str) -> str:
    """脱敏 token, 只返回长度 + 前 8 位"""
    if not value:
        return "<empty>"
    return f"len={len(value)}, prefix={value[:8]}..."


def redact_text(text: str) -> str:
    """从文本中清除任何可能的 token"""
    if not text:
        return text
    for pattern in TOKEN_PATTERNS:
        text = pattern.sub(lambda m: f"<REDACTED:{mask_token(m.group(0))}>", text)
    return text


def safe_repr(obj: Any, max_len: int = 200) -> str:
    """生成对象的安全字符串表示"""
    try:
        s = repr(obj)
        if len(s) > max_len:
            s = s[:max_len] + "...<truncated>"
        return redact_text(s)
    except Exception:
        return f"<unrepr-able:{type(obj).__name__}>"


# ============================================================
# 配置类
# ============================================================

@dataclass
class LLMConfig:
    """LLM 配置 - 不含任何敏感信息"""
    # 默认走 pateway.ai 的 Anthropic 兼容端点。可用环境变量覆盖, 无需改代码:
    #   LLM_BASE_URL    端点
    #   LLM_MODEL       模型名
    #   LLM_API_STYLE   "anthropic" | "openai_responses"
    #   LLM_API_KEY_ENV key 所在的环境变量名
    base_url: str = "https://api.pateway.ai/v1/messages"
    api_key_env: str = "PAGETWAY_API_KEY"
    model: str = "deepseek-v4.1-flash"
    api_style: str = "anthropic"   # "anthropic" | "openai_responses"
    disable_thinking: bool = True  # 关掉 thinking: 省 token 且响应文本更干净
    fallback_models: List[str] = field(default_factory=list)
    max_tokens: int = 4096
    temperature: float = 1.0
    thinking: str = "disabled"  # Responses API 暂不发送该参数
    timeout: int = 180
    max_retries: int = 3
    backoff_factor: float = 2.0


# ============================================================
# LLM 客户端
# ============================================================

class LLMClient:
    """
    LLM 客户端 - 调用 rightapi 的 OpenAI Responses 兼容接口

    使用示例:
        client = LLMClient()
        response = client.call("你好")
        print(response)
    """

    def __init__(self, config: Optional[LLMConfig] = None):
        self.config = config or LLMConfig()
        self._session = requests.Session()
        self._api_key_loaded = False

        # 加载并验证 API key
        self._load_api_key()

    def _load_api_key(self):
        """加载 API key, 优先级: .env 文件 > 环境变量

        流程:
        1. 在 PROJECT_ROOT 找 .env 文件, 用 dotenv 加载 (override=False, 已有 env 优先)
        2. 从 os.environ.get(api_key_env) 读 key
        3. key 不存在则报错并提示两种设置方式
        """
        # 1) 加载 .env (如果存在)
        self._load_dotenv_file()

        # 允许通过环境变量覆盖 endpoint / 模型 / 协议风格 / key 环境变量名
        self.config.base_url = (
            os.environ.get("LLM_BASE_URL")
            or os.environ.get("RIGHTAPI_BASE_URL")
            or self.config.base_url
        )
        self.config.model = os.environ.get("LLM_MODEL", self.config.model)
        self.config.api_style = os.environ.get("LLM_API_STYLE", self.config.api_style)
        self.config.api_key_env = os.environ.get(
            "LLM_API_KEY_ENV", self.config.api_key_env
        )

        # 2) 读 key
        api_key = os.environ.get(self.config.api_key_env)

        if not api_key:
            raise EnvironmentError(
                f"未找到 {self.config.api_key_env}. "
                f"两种设置方式:\n"
                f"  1. 在项目根 .env 文件加: {self.config.api_key_env}=你的key\n"
                f"  2. 或设置系统环境变量: set {self.config.api_key_env}=你的key\n"
                f"参考 .env.example 模板."
            )

        # 【关键】只记录长度 + 前 8 位, 绝不全量输出
        logger.info(f"API key loaded: {mask_token(api_key)}")

        self._api_key = api_key
        self._api_key_loaded = True

    @staticmethod
    def _load_dotenv_file():
        """从项目根目录加载 .env 文件 (如存在).

        dotenv override=False: 已有环境变量不会被 .env 覆盖.
        这样:
          - 平时 .env 提供 key
          - CI/调试时可以临时 `ANTHROPIC_API_KEY=xxx python ...` 覆盖
        """
        try:
            from dotenv import load_dotenv
            from pathlib import Path
            # 项目根 = llm_client.py 上两级 (src/llm_client.py -> src -> root)
            project_root = Path(__file__).resolve().parent.parent
            env_file = project_root / ".env"
            if env_file.exists():
                loaded = load_dotenv(env_file, override=False)
                if loaded:
                    logger.debug(f".env 已加载: {env_file}")
                else:
                    logger.debug(f".env 文件存在但未加载到新变量: {env_file}")
            # 没 .env 文件就静默 (走环境变量 fallback)
        except ImportError:
            logger.warning("python-dotenv 未安装, 跳过 .env 加载 (仅读环境变量)")

    def call(
        self,
        prompt: str,
        system: str = "",
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        thinking: Optional[str] = None,
        model: Optional[str] = None,
    ) -> str:
        """
        调用 LLM, 返回文本响应

        Args:
            prompt: 用户 prompt
            system: 系统 prompt
            max_tokens: 最大输出 token
            temperature: 温度参数
            thinking: thinking 控制 (None=用默认, "disabled", "adaptive")
            model: 覆盖默认模型

        Returns:
            模型返回的文本内容
        """
        if not self._api_key_loaded:
            raise RuntimeError("LLM client not initialized")

        payload = self._build_payload(
            prompt=prompt,
            system=system,
            max_tokens=max_tokens,
            temperature=temperature,
            model=model,
        )

        # 重试 + 降级
        models_to_try = [payload["model"]] + [
            m for m in self.config.fallback_models if m != payload["model"]
        ]

        last_error = None
        for attempt, current_model in enumerate(models_to_try):
            payload["model"] = current_model
            for retry in range(self.config.max_retries):
                try:
                    logger.debug(
                        f"LLM call: model={current_model}, "
                        f"attempt={attempt+1}, retry={retry+1}, "
                        f"prompt_len={len(prompt)}"
                    )

                    start = time.time()
                    response = self._session.post(
                        self.config.base_url,
                        headers={
                            "Content-Type": "application/json",
                            # 同时带两种鉴权头: Anthropic 兼容端点读 x-api-key,
                            # OpenAI 兼容端点读 Authorization
                            "x-api-key": self._api_key,
                            "Authorization": f"Bearer {self._api_key}",
                        },
                        json=payload,
                        stream=True,
                        timeout=self.config.timeout,
                    )
                    response.raise_for_status()
                    elapsed = time.time() - start

                    text = self._extract_stream_text(
                        response.iter_lines(decode_unicode=False)
                    )

                    # 记录成功 (脱敏)
                    logger.info(
                        f"LLM call OK: model={current_model}, "
                        f"elapsed={elapsed:.2f}s, "
                        f"response_status={response.status_code}"
                    )

                    return text

                except Exception as e:
                    last_error = e
                    error_msg = safe_repr(e)

                    # 判断是否可重试
                    retryable = self._is_retryable(e)

                    if retry < self.config.max_retries - 1 and retryable:
                        wait = self.config.backoff_factor ** retry
                        logger.warning(
                            f"LLM call failed (retryable): {error_msg[:200]}, "
                            f"wait {wait}s..."
                        )
                        time.sleep(wait)
                        continue
                    else:
                        logger.error(
                            f"LLM call failed (non-retryable or max retries): "
                            f"{error_msg[:200]}"
                        )
                        break

            # 当前模型失败, 尝试降级模型
            if attempt < len(models_to_try) - 1:
                logger.warning(
                    f"Falling back to model: {models_to_try[attempt + 1]}"
                )

        # 所有尝试都失败
        raise RuntimeError(
            f"LLM call failed after all retries and fallbacks. "
            f"Last error: {safe_repr(last_error)}"
        )

    def complete(
        self,
        prompt: str,
        system: str = "",
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        thinking: Optional[str] = None,
        model: Optional[str] = None,
    ) -> str:
        """兼容旧调用方: complete(...) -> call(...)."""
        return self.call(
            prompt=prompt,
            system=system,
            max_tokens=max_tokens,
            temperature=temperature,
            thinking=thinking,
            model=model,
        )

    def _build_payload(
        self,
        prompt: str,
        system: str = "",
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        model: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        构造请求体。按 config.api_style 分两种协议:

        anthropic        → Anthropic Messages API
                          {model, max_tokens, system, messages, temperature,
                           stream, thinking}
        openai_responses → OpenAI Responses API
                          {model, input, max_output_tokens, temperature, stream}
        """
        if self.config.api_style == "anthropic":
            payload: Dict[str, Any] = {
                "model": model or self.config.model,
                "max_tokens": max_tokens or self.config.max_tokens,
                "messages": [{"role": "user", "content": prompt}],
                "stream": True,
            }
            if system:
                # Anthropic 的 system 是顶层字段, 不是 message
                payload["system"] = system
            if temperature is not None:
                payload["temperature"] = temperature
            elif self.config.temperature is not None:
                payload["temperature"] = self.config.temperature
            if self.config.disable_thinking:
                # 不关掉的话 thinking 会吃掉大量 max_tokens, 且会混进响应
                payload["thinking"] = {"type": "disabled"}
            return payload

        messages = []
        if system:
            messages.append({
                "type": "message",
                "role": "system",
                "content": [{"type": "input_text", "text": system}],
            })
        messages.append({
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": prompt}],
        })
        return {
            "model": model or self.config.model,
            "input": messages,
            "max_output_tokens": max_tokens or self.config.max_tokens,
            "temperature": (
                temperature if temperature is not None else self.config.temperature
            ),
            "stream": True,
        }

    @staticmethod
    def _extract_stream_text(lines: Any) -> str:
        """
        从 SSE data 行中拼接增量文本。同时兼容两种协议:

        Anthropic : content_block_delta 且 delta.type=="text_delta" → delta.text
                    (thinking_delta / input_json_delta 等一律忽略)
        OpenAI    : response.output_text.delta → delta
        """
        parts = []
        for line in lines:
            if isinstance(line, bytes):
                line = line.decode("utf-8", errors="replace")
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                continue
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                logger.debug("忽略无法解析的 SSE 事件: %s", safe_repr(data))
                continue

            # --- Anthropic Messages API ---
            if event.get("type") == "content_block_delta":
                delta = event.get("delta") or {}
                if delta.get("type") == "text_delta":
                    parts.append(delta.get("text", ""))
                # thinking_delta 故意不收: 思考过程不是用例正文

            # --- OpenAI Responses API ---
            elif event.get("type") == "response.output_text.delta":
                parts.append(event.get("delta", ""))
            elif event.get("type") == "response.output_text.done" and not parts:
                parts.append(event.get("text", ""))

        return "".join(parts)

    def _is_retryable(self, exception: Exception) -> bool:
        """判断异常是否可重试"""
        # 网络错误、超时、5xx 可重试
        # 4xx (除 429) 不可重试
        if isinstance(exception, requests.RequestException):
            response = getattr(exception, "response", None)
            return response is None or response.status_code == 429 or response.status_code >= 500

        # 默认不可重试 (避免无效重试浪费 token)
        return False

    def count_tokens(self, text: str) -> int:
        """
        估算 token 数 (粗略, 不调 API)
        中英文混合: 中文 1 字符 ≈ 1.5 token, 英文 1 字符 ≈ 0.25 token
        """
        chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
        other_chars = len(text) - chinese_chars
        return int(chinese_chars * 1.5 + other_chars * 0.25)


# ============================================================
# 便捷函数
# ============================================================

_default_client: Optional[LLMClient] = None


def get_default_client() -> LLMClient:
    """获取全局默认客户端 (单例)"""
    global _default_client
    if _default_client is None:
        _default_client = LLMClient()
    return _default_client


def call_llm(prompt: str, system: str = "", **kwargs) -> str:
    """便捷调用接口"""
    return get_default_client().call(prompt, system=system, **kwargs)


# ============================================================
# 命令行测试入口
# ============================================================

if __name__ == "__main__":
    # 测试: 调用一次, 确认连通性
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )

    print("=" * 60)
    print("LLM 客户端连通性测试")
    print("=" * 60)

    try:
        client = LLMClient()
        print(f"✅ 客户端初始化成功")
        print(f"   模型: {client.config.model}")
        print(f"   Base URL: {client.config.base_url}")
        print()

        # 简单测试
        print("发送测试 prompt: '回复 OK 即可'")
        response = client.call(
            prompt="回复 OK 即可",
            system="你是一个简洁的助手",
            max_tokens=50,
        )
        print(f"✅ 调用成功")
        print(f"   响应: {response}")
        print()

        # Token 计数测试
        text = "你好, world! This is a test."
        tokens = client.count_tokens(text)
        print(f"✅ Token 估算: '{text}' ≈ {tokens} tokens")

    except EnvironmentError as e:
        print(f"❌ 环境变量未设置: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ 调用失败: {safe_repr(e)}")
        sys.exit(1)