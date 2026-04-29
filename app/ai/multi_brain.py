"""多模型并行调用 + 综合整合。"""
import concurrent.futures
import logging
from typing import Dict, List, Optional

from .base import AIClient
from .prompts import SYSTEM_PROMPT, SYSTEM_PROMPT_ADVERSARY, build_analysis_prompt

log = logging.getLogger(__name__)


class MultiBrain:
    def __init__(self, clients: List[AIClient]):
        if not clients:
            raise ValueError("至少需要一个 AI 客户端")
        self.clients = clients

    def analyze(self, stock_data: dict, max_tokens: int = 2048) -> Dict[str, str]:
        """并行调所有模型，返回 {模型名: 分析文本}。"""
        prompt = build_analysis_prompt(stock_data)
        results: Dict[str, str] = {}

        def _one(client: AIClient) -> tuple[str, str]:
            try:
                text = client.complete(SYSTEM_PROMPT, prompt, max_tokens=max_tokens)
                log.info(f"{client.name} 完成 ({len(text)} 字符)")
                return client.name, text
            except Exception as e:
                log.error(f"{client.name} 失败: {e}")
                return client.name, f"[错误] {type(e).__name__}: {e}"

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(self.clients)) as pool:
            futures = [pool.submit(_one, c) for c in self.clients]
            for f in concurrent.futures.as_completed(futures):
                name, text = f.result()
                results[name] = text

        return results

    def analyze_differentiated(self, stock_data: dict,
                                max_tokens: int = 2000) -> Dict[str, str]:
        """差异化信息源: 每家 AI 看不同侧面的数据，破"同源污染"。

        DeepSeek → 财务面 / Gemini → 研报面 / Claude → 技术+博弈 / Codex → 政策事件
        没匹配的 AI 走默认全数据 prompt。
        """
        from .differentiated_prompts import get_prompt_for
        from .prompts import SYSTEM_PROMPT, SYSTEM_PROMPT_ADVERSARY
        results: Dict[str, str] = {}

        def _one(client: AIClient) -> tuple[str, str]:
            custom_prompt = get_prompt_for(client.name, stock_data)
            is_adv = client.name == "Claude"
            sys = SYSTEM_PROMPT_ADVERSARY if is_adv else SYSTEM_PROMPT
            tag = " [差异化·反方]" if is_adv else " [差异化]"
            if custom_prompt is None:
                # 没匹配，用通用全数据 prompt
                custom_prompt = build_analysis_prompt(stock_data)
                tag = " [通用]"
            try:
                text = client.complete(sys, custom_prompt, max_tokens=max_tokens)
                log.info(f"{client.name}{tag} 完成 ({len(text)} 字符)")
                return f"{client.name}{tag}", text
            except Exception as e:
                log.error(f"{client.name}{tag} 失败: {e}")
                return f"{client.name}{tag}", f"[错误] {type(e).__name__}: {e}"

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(self.clients)) as pool:
            futures = [pool.submit(_one, c) for c in self.clients]
            for f in concurrent.futures.as_completed(futures):
                name, text = f.result()
                results[name] = text
        return results

    def analyze_with_adversary(
        self, stock_data: dict,
        adversary_name: str = "Claude",
        max_tokens: int = 2000,
    ) -> Dict[str, str]:
        """对抗性仲裁：指定一家 AI 扮演反方（做空逻辑），其他常规分析。

        突破"同源信息污染"盲点。
        """
        prompt = build_analysis_prompt(stock_data)
        results: Dict[str, str] = {}

        def _one(client: AIClient) -> tuple[str, str]:
            is_adversary = client.name == adversary_name
            sys = SYSTEM_PROMPT_ADVERSARY if is_adversary else SYSTEM_PROMPT
            tag = " [反方]" if is_adversary else ""
            try:
                text = client.complete(sys, prompt, max_tokens=max_tokens)
                display = f"{client.name}{tag}"
                log.info(f"{display} 完成 ({len(text)} 字符)")
                return display, text
            except Exception as e:
                log.error(f"{client.name} 失败: {e}")
                return f"{client.name}{tag}", f"[错误] {type(e).__name__}: {e}"

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(self.clients)) as pool:
            futures = [pool.submit(_one, c) for c in self.clients]
            for f in concurrent.futures.as_completed(futures):
                name, text = f.result()
                results[name] = text
        return results

    def consensus(self, stock_data: dict, opinions: Dict[str, str],
                   arbiter: Optional[AIClient] = None, max_tokens: int = 1500) -> str:
        """让一个仲裁模型整合多家观点（按历史胜率加权）。"""
        if not arbiter:
            arbiter = self.clients[0]
        joined = "\n\n".join(f"## 【{name}】\n{text}" for name, text in opinions.items())
        has_adversary = any("反方" in name for name in opinions.keys())
        adversary_note = (
            "特别注意：其中有一位是反方（做空视角），**不能直接忽略它**，"
            "要认真评估它的证伪逻辑，纳入最终风险评估。"
            if has_adversary else ""
        )
        # 按历史胜率加权
        try:
            from ..services.game_memory import format_track_record_for_prompt
            track_record = format_track_record_for_prompt()
        except Exception:
            track_record = ""
        weighting_rule = (
            "\n**仲裁加权规则**: 按上方胜率数据加权 —— "
            "胜率 ≥70% 的 AI 在其擅长判断上作为主要依据；"
            "胜率 <40% 的 AI 在该类判断上降权为反方参考；"
            "胜率库为空则等权。\n"
            if track_record and "为空" not in track_record else ""
        )
        sys = ("你是专业投资顾问，整合多方观点给出最终可执行建议。"
               "按历史胜率加权采纳各家意见。")
        user = (
            f"{track_record}{weighting_rule}\n"
            f"{len(opinions)} 位 AI 分析师对股票 {stock_data.get('code')} "
            f"({stock_data.get('name')}) 的独立分析：\n\n"
            f"{joined}\n\n{adversary_note}\n\n"
            "---\n请按以下格式输出（简洁，可直接执行）：\n"
            "### 🎯 综合结论（一句话，30字内）\n"
            "### 🤝 一致观点（正反方都认同的共识点）\n"
            "### ⚔️ 核心分歧（正反方主要分歧 + 分歧解决条件）\n"
            "### ⭐ 最终建议\n"
            "- 操作: 买入/加仓/持有/减仓/卖出\n"
            "- 仓位: X%\n"
            "- 驱动类型: 政策/业绩/资金/主题\n"
            "- 赔率: 1:X（上/下）\n"
            "- 止损位: X 元\n"
            "- 关键风险: ...\n"
            "- 建议时间窗: 短/中/长\n"
        )
        kwargs = {"reasoning_effort": "high"} if arbiter.name == "DeepSeek" else {}
        return arbiter.complete(sys, user, max_tokens=max_tokens, **kwargs)


def build_brains_from_config(config: dict) -> List[AIClient]:
    """根据 config.yaml 自动构建已配置的 AI 客户端列表。"""
    from .openai_compat import OpenAICompatClient, normalize_model_name, DEEPSEEK_V4_PRO
    from .gemini_client import GeminiClient
    from .local_cli import LocalCLIClient

    ai_cfg = config.get("ai", {})
    brains: List[AIClient] = []

    # DeepSeek（国内直通）
    ds = ai_cfg.get("deepseek", {})
    if ds.get("api_key"):
        brains.append(OpenAICompatClient(
            name="DeepSeek",
            model=normalize_model_name("DeepSeek", ds.get("model"), DEEPSEEK_V4_PRO),
            api_key=ds["api_key"],
            base_url=ds.get("base_url", "https://api.deepseek.com/v1"),
        ))

    # Gemini（走 mao Clash 代理）
    gem = ai_cfg.get("gemini", {})
    if gem.get("api_key"):
        brains.append(GeminiClient(
            api_key=gem["api_key"],
            model=gem.get("model", "gemini-2.5-flash"),
            proxy=gem.get("proxy"),
        ))

    # Claude / Codex（通过 mao 上的本地 CLI 代理）
    local = ai_cfg.get("local_cli", {})
    if local.get("endpoint") and local.get("enabled", True):
        for agent_cfg in local.get("agents", []):
            brains.append(LocalCLIClient(
                name=agent_cfg["name"],
                agent=agent_cfg["agent"],
                endpoint=local["endpoint"],
                timeout=agent_cfg.get("timeout", 180),
            ))

    # OpenRouter（可选，未来接入更多模型）
    orr = ai_cfg.get("openrouter", {})
    if orr.get("api_key"):
        for m in orr.get("models", []):
            brains.append(OpenAICompatClient(
                name=m.get("name", m["model"]),
                model=m["model"],
                api_key=orr["api_key"],
                base_url="https://openrouter.ai/api/v1",
                proxy=orr.get("proxy"),
            ))

    return brains
