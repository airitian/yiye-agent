"""
LLM 客户端封装 - 严格遵守 Token 安全规范

【安全铁律】
1. API key 优先从 .env 文件读取 (项目级配置), 其次环境变量
2. 日志输出永远只用 len() + 前 8 位, 绝不全量 echo
3. 任何异常信息自动脱敏
4. 不缓存 key 到内存超过必要时间

【.env 加载优先级】
.env 文件 > 系统环境变量
(已存在的环境变量不会被 .env 覆盖, 方便 CI/调试时临时注入)
"""

import os
import re
import time
import logging
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field

try:
    import anthropic
except ImportError:
    anthropic = None  # 让导入不报错, 运行时再检查

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
    base_url: str = "https://api.minimaxi.com/anthropic"
    api_key_env: str = "ANTHROPIC_API_KEY"
    model: str = "MiniMax-M3"
    fallback_models: List[str] = field(default_factory=lambda: [
        "MiniMax-M2.7-highspeed",
        "MiniMax-M2"
    ])
    max_tokens: int = 4096
    temperature: float = 1.0
    thinking: str = "disabled"  # "disabled" | "adaptive"
    timeout: int = 180
    max_retries: int = 3
    backoff_factor: float = 2.0


# ============================================================
# LLM 客户端
# ============================================================

class LLMClient:
    """
    LLM 客户端 - 封装 anthropic SDK 调用 MiniMax

    使用示例:
        client = LLMClient()
        response = client.call("你好")
        print(response)
    """

    def __init__(self, config: Optional[LLMConfig] = None):
        if anthropic is None:
            raise ImportError(
                "anthropic SDK 未安装. 请运行: pip install anthropic"
            )

        self.config = config or LLMConfig()
        self._client = None
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

        # 初始化 SDK 客户端 (SDK 内部会处理 key)
        try:
            self._client = anthropic.Anthropic(
                api_key=api_key,
                base_url=self.config.base_url,
                timeout=self.config.timeout,
            )
            self._api_key_loaded = True
        except Exception as e:
            logger.error(f"Failed to init Anthropic client: {safe_repr(e)}")
            raise

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

        kwargs = {
            "model": model or self.config.model,
            "max_tokens": max_tokens or self.config.max_tokens,
            "temperature": temperature if temperature is not None else self.config.temperature,
            "messages": [{"role": "user", "content": prompt}],
        }

        if system:
            kwargs["system"] = system

        # thinking 控制 (仅 M3 支持, M2.x 忽略)
        effective_thinking = thinking or self.config.thinking
        if effective_thinking and "M3" in kwargs["model"]:
            kwargs["thinking"] = {"type": effective_thinking}

        # 重试 + 降级
        models_to_try = [kwargs["model"]] + [
            m for m in self.config.fallback_models if m != kwargs["model"]
        ]

        last_error = None
        for attempt, current_model in enumerate(models_to_try):
            kwargs["model"] = current_model
            for retry in range(self.config.max_retries):
                try:
                    logger.debug(
                        f"LLM call: model={current_model}, "
                        f"attempt={attempt+1}, retry={retry+1}, "
                        f"prompt_len={len(prompt)}"
                    )

                    start = time.time()
                    response = self._client.messages.create(**kwargs)
                    elapsed = time.time() - start

                    # 提取文本
                    text = self._extract_text(response)

                    # 记录成功 (脱敏)
                    logger.info(
                        f"LLM call OK: model={current_model}, "
                        f"elapsed={elapsed:.2f}s, "
                        f"input_tokens={getattr(response.usage, 'input_tokens', '?')}, "
                        f"output_tokens={getattr(response.usage, 'output_tokens', '?')}"
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

    def _extract_text(self, response: Any) -> str:
        """从响应中提取文本内容"""
        if not hasattr(response, 'content'):
            return ""

        parts = []
        for block in response.content:
            if hasattr(block, 'type'):
                if block.type == "text":
                    parts.append(block.text)
                elif block.type == "thinking":
                    # thinking 块不返回给用户, 但可以日志
                    logger.debug(f"Thinking: {getattr(block, 'thinking', '')[:200]}")
        return "\n".join(parts)

    def _is_retryable(self, exception: Exception) -> bool:
        """判断异常是否可重试"""
        # 网络错误、超时、5xx 可重试
        # 4xx (除 429) 不可重试
        if isinstance(exception, anthropic.APIConnectionError):
            return True
        if isinstance(exception, anthropic.APITimeoutError):
            return True
        if isinstance(exception, anthropic.InternalServerError):
            return True
        if isinstance(exception, anthropic.RateLimitError):
            return True

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