"""AI 客户端抽象基类。所有厂商统一接口。"""
from abc import ABC, abstractmethod


class AIClient(ABC):
    name: str = "base"          # 模型名称（Claude / GPT / Gemini / DeepSeek / Qwen）
    model: str = ""             # 具体模型 ID

    @abstractmethod
    def complete(self, system: str, user: str, max_tokens: int = 2048) -> str:
        """同步单轮对话。"""
        ...

    def __repr__(self):
        return f"<{self.name} ({self.model})>"
