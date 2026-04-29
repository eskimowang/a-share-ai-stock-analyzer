"""Google Gemini 客户端（google-genai SDK）。"""
import os
from google import genai
from google.genai import types
from .base import AIClient


class GeminiClient(AIClient):
    def __init__(self, api_key: str, model: str = "gemini-2.5-pro",
                 proxy: str | None = None):
        self.name = "Gemini"
        self.model = model
        # Gemini SDK 通过环境变量配代理
        if proxy:
            os.environ["HTTPS_PROXY"] = proxy
            os.environ["HTTP_PROXY"] = proxy
        self.client = genai.Client(api_key=api_key)

    def complete(self, system: str, user: str, max_tokens: int = 2048) -> str:
        import time as _time
        # 主模型 + 2 个 fallback（Gemini 常 503 过载）
        models_to_try = [
            self.model,
            "gemini-2.0-flash",
            "gemini-1.5-flash",
        ]
        seen = set()
        last_err = None
        for m in models_to_try:
            if m in seen:
                continue
            seen.add(m)
            for attempt in range(2):
                try:
                    resp = self.client.models.generate_content(
                        model=m,
                        contents=user,
                        config=types.GenerateContentConfig(
                            system_instruction=system,
                            max_output_tokens=max_tokens,
                            temperature=0.3,
                            thinking_config=types.ThinkingConfig(thinking_budget=0),
                        ),
                    )
                    return resp.text
                except Exception as e:
                    last_err = e
                    s = str(e)
                    if "503" in s or "UNAVAILABLE" in s or "RESOURCE_EXHAUSTED" in s:
                        _time.sleep(3 * (attempt + 1))
                        continue
                    # 非过载错误直接跳到下个模型
                    break
        raise last_err if last_err else RuntimeError("Gemini all models failed")
