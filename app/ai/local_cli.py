"""通过 Tailscale HTTP 调用 mao 本地 Claude/Codex CLI。"""
import httpx
from .base import AIClient


class LocalCLIClient(AIClient):
    def __init__(self, name: str, agent: str, endpoint: str, timeout: float = 300.0):
        """
        name:     展示名（Claude / Codex）
        agent:    路径（claude / codex）
        endpoint: http://100.124.76.93:18888
        """
        self.name = name
        self.model = f"local-{agent}"
        self._url = f"{endpoint.rstrip('/')}/{agent}"
        self._client = httpx.Client(timeout=timeout)

    def complete(self, system: str, user: str, max_tokens: int = 2048) -> str:
        # Claude / Codex CLI 在非交互模式下会把普通 prompt 当"对话开场"，
        # 所以必须用强命令式语气，明确告诉它"立即执行、不要反问"
        prompt = (
            "# 任务\n"
            "【立即执行】以下股票分析任务。严格按指定格式输出完整分析结果。\n"
            "**禁止**：询问额外信息、解释你的角色定位、列出分析框架、要求我补充数据。\n"
            "**收到即完整输出**分析报告，不要有任何前置对话或确认。\n\n"
            f"# 分析师角色设定\n{system}\n\n"
            f"# 具体分析任务（数据完整，请直接基于以下内容输出）\n{user}\n\n"
            "---\n现在开始输出完整的分析报告（严格按模板中的 Markdown 章节结构）："
        )
        r = self._client.post(self._url, json={"prompt": prompt})
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"{self.name}: {data['error']}")
        return data["output"]
