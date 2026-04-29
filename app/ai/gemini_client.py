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
        import logging as _log
        import time as _time
        log = _log.getLogger(__name__)
        # 主模型 + fallback（2026-04 Google 已淘汰 1.5-flash，改用 2.5/2.0 系列）
        models_to_try = [
            self.model,
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
            "gemini-2.0-flash",
            "gemini-2.0-flash-lite",
        ]
        seen = set()
        last_err = None
        for m in models_to_try:
            if m in seen:
                continue
            seen.add(m)
            for attempt in range(3):  # 每模型 3 次尝试
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
                    # 空响应也当失败，重试
                    text = resp.text if resp else None
                    if not text or not text.strip():
                        last_err = RuntimeError(f"{m} 返回空内容")
                        _time.sleep(2)
                        continue
                    if m != self.model:
                        log.info(f"Gemini 主模型失败，fallback 成功: {m}")
                    return text
                except Exception as e:
                    last_err = e
                    s = str(e)
                    # 过载/限流 → 指数退避重试（更长等待）
                    if "503" in s or "UNAVAILABLE" in s or "RESOURCE_EXHAUSTED" in s or "429" in s:
                        wait = 2 + attempt * 3  # 2s, 5s, 8s
                        log.warning(f"Gemini {m} 过载 (第 {attempt+1} 次): {s[:120]}，等 {wait}s")
                        _time.sleep(wait)
                        continue
                    # 网络错误/超时 → 短等待重试
                    if "timeout" in s.lower() or "connection" in s.lower() or "network" in s.lower():
                        _time.sleep(1 + attempt)
                        continue
                    # 其他错误（比如 400 请求格式错）直接跳到下个模型
                    log.warning(f"Gemini {m} 非过载错误，切换 fallback: {s[:120]}")
                    break
        log.error(f"Gemini 全部 {len(seen)} 个模型失败，最后错误: {last_err}")
        raise last_err if last_err else RuntimeError("Gemini all models failed")
