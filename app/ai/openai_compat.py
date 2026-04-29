"""OpenAI 兼容协议的统一客户端。
DeepSeek / OpenAI / OpenRouter 都用这个（格式一致）。

DeepSeek V4 新增: thinking mode (reasoning_effort 参数)
- 非思考模式: reasoning_effort=None（默认）
- 思考强度: 'high' / 'max'（复杂 Agent 场景用 max）
"""
import logging

import httpx
from openai import OpenAI
from .base import AIClient

log = logging.getLogger(__name__)

DEEPSEEK_V4_PRO = "deepseek-v4-pro"
DEEPSEEK_V4_FLASH = "deepseek-v4-flash"
DEEPSEEK_MODEL_ALIASES = {
    "DeepSeek": DEEPSEEK_V4_PRO,
    "deepseek": DEEPSEEK_V4_PRO,
    "deepseek-v4": DEEPSEEK_V4_PRO,
    "deepseek-v4-pro": DEEPSEEK_V4_PRO,
    "deepseek-v4-flash": DEEPSEEK_V4_FLASH,
    "deepseek-chat": DEEPSEEK_V4_FLASH,
    "deepseek-reasoner": DEEPSEEK_V4_FLASH,
}


def normalize_model_name(provider_name: str, model: str | None, fallback: str | None = None) -> str:
    candidate = model or fallback
    if provider_name == "DeepSeek":
        if candidate in DEEPSEEK_MODEL_ALIASES:
            return DEEPSEEK_MODEL_ALIASES[candidate]
        if candidate and candidate.startswith("deepseek-"):
            return candidate
        return fallback or DEEPSEEK_V4_PRO
    return candidate or fallback or ""


class OpenAICompatClient(AIClient):
    def __init__(self, name: str, model: str, api_key: str,
                 base_url: str = 'https://api.openai.com/v1',
                 proxy: str | None = None, timeout: float = 300.0,
                 reasoning_effort: str | None = None):
        self.name = name
        self.model = normalize_model_name(name, model)
        self.reasoning_effort = reasoning_effort  # None / 'high' / 'max'
        http_client = httpx.Client(proxy=proxy, timeout=timeout) if proxy else httpx.Client(timeout=timeout)
        self.client = OpenAI(api_key=api_key, base_url=base_url, http_client=http_client)

    def complete(self, system: str, user: str, max_tokens: int = 2048,
                 reasoning_effort: str | None = None) -> str:
        # 允许调用时覆盖默认强度
        effort = reasoning_effort if reasoning_effort is not None else self.reasoning_effort
        extra_body = {}
        if effort in ('high', 'max'):
            extra_body['thinking'] = {'type': 'enabled', 'reasoning_effort': effort}

        for attempt in range(2):
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {'role': 'system', 'content': system},
                    {'role': 'user', 'content': user},
                ],
                max_tokens=max_tokens,
                temperature=0.3,
                extra_body=extra_body or None,
            )
            text = (resp.choices[0].message.content or '').strip()
            if text:
                return text
            log.warning("%s returned empty content on attempt %s", self.name, attempt + 1)
            user = user + "\n\n请重新输出完整分析，不能返回空内容。"
        raise RuntimeError(f"{self.name} returned empty content")
