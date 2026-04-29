"""Codex 信息收集器 — 独立于 4 家意见，作为信息源提供结构化数据。"""
import json
import logging
import re
from typing import Optional

log = logging.getLogger(__name__)


def _extract_json(text: str) -> Optional[dict]:
    """Codex 输出可能包含说明文字，抽第一个 JSON 对象。"""
    # 尝试直接解析
    try:
        return json.loads(text)
    except Exception:
        pass
    # 找 ```json 代码块
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # 找第一个 {...}
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return None


class CodexInfoCollector:
    def __init__(self, codex_client):
        """codex_client 必须是 LocalCLIClient(agent='codex')。"""
        self.codex = codex_client

    def _query(self, prompt: str, max_tokens: int = 3000) -> dict:
        """调用 codex，强制 JSON 输出。"""
        sys = "你是金融信息研究员，严格按指定 JSON 结构输出。只输出 JSON，不要前言和解释。"
        raw = self.codex.complete(sys, prompt, max_tokens=max_tokens)
        data = _extract_json(raw)
        if data is None:
            log.warning(f"Codex JSON 解析失败: {raw[:200]}")
            return {"_raw": raw, "_parse_error": True}
        return data

    # ========== 个股信息 ==========
    def collect_stock_info(self, code: str, name: str, days: int = 30) -> dict:
        prompt = f"""【任务】用联网搜索收集 A 股 **{name}({code})** 近 {days} 天关键信息。

【输出格式】严格按以下 JSON，不要添加其他文字：
{{
  "code": "{code}",
  "name": "{name}",
  "collect_date": "YYYY-MM-DD",
  "recent_announcements": [
    {{"date": "YYYY-MM-DD", "title": "公告标题", "summary": "一句话摘要", "impact": "正面/负面/中性", "source": "URL"}}
  ],
  "policy_impact": [
    {{"policy": "政策名称", "date": "YYYY-MM-DD", "direction": "利好/利空/中性", "summary": "对该公司的影响", "source": "URL"}}
  ],
  "analyst_reports": [
    {{"broker": "券商", "date": "YYYY-MM-DD", "rating": "买入/增持/中性/减持", "target_price": 数字或null, "logic": "核心逻辑一句话"}}
  ],
  "sentiment_events": [
    {{"date": "YYYY-MM-DD", "event": "事件", "impact": "正面/负面"}}
  ],
  "summary": "3-5 句话综合信息面",
  "info_grade": "A/B/C/D"
}}

【要求】
- 必须引用权威源（巨潮资讯 / 券商官网 / 新华社 / 财联社等）
- 无信息时相应数组返回 []
- info_grade: A=信息充分/B=一般/C=稀疏/D=几乎无信息
"""
        return self._query(prompt)

    # ========== 行业信息 ==========
    def collect_industry_info(self, industry: str) -> dict:
        prompt = f"""【任务】用联网搜索收集 A 股 **{industry}** 行业近 60 天景气度和关键动态。

【输出格式】严格 JSON：
{{
  "industry": "{industry}",
  "collect_date": "YYYY-MM-DD",
  "cycle_stage": "复苏/繁荣/衰退/萧条",
  "boom_score": 1-10 数字,
  "boom_reason": "景气度判断理由",
  "key_policies": [
    {{"policy": "政策", "date": "YYYY-MM-DD", "direction": "利好/利空", "summary": "摘要", "source": "URL"}}
  ],
  "top_players": [
    {{"code": "股票代码", "name": "公司", "market_share": "市占率%或描述", "edge": "核心优势"}}
  ],
  "inflection_points": ["可能的拐点信号 1", "拐点 2"],
  "risks": ["风险 1", "风险 2"],
  "summary": "5 句话行业综述"
}}
"""
        return self._query(prompt)

    # ========== 宏观信息 ==========
    def collect_macro_info(self) -> dict:
        prompt = """【任务】用联网搜索收集当前中国 A 股宏观环境。

【输出格式】严格 JSON：
{
  "collect_date": "YYYY-MM-DD",
  "market_phase": "牛市初期/牛市中后/震荡上行/震荡下行/熊市",
  "major_indices": {
    "上证指数": {"latest": 点位, "change_1w": "%", "change_1m": "%", "key_level": "关键点位描述"},
    "深证成指": {"latest": 点位, "change_1w": "%", "change_1m": "%", "key_level": "..."},
    "创业板指": {"latest": 点位, "change_1w": "%", "change_1m": "%", "key_level": "..."}
  },
  "macro_events": [
    {"date": "YYYY-MM-DD", "event": "事件 (如降准/LPR调整/社融)", "impact": "利好/利空", "magnitude": "大/中/小"}
  ],
  "capital_flow": {
    "northbound_1w": "净流入/流出金额",
    "margin_balance_trend": "上升/下降/持平",
    "etf_net_flow": "描述"
  },
  "hot_sectors": ["主线 1", "主线 2", "主线 3"],
  "risk_signals": ["风险 1", "风险 2"],
  "summary": "5 句话宏观综述"
}
"""
        return self._query(prompt, max_tokens=4000)

    # ========== 多理论框架研判行业（AI 议会修订版） ==========
    def judge_industries_multi_theory(self, top_n: int = 8) -> dict:
        """多理论综合研判（2026-04 修订）。

        已删除（议会决议在 A 股失效）:
        - DCF 估值（贴现率敏感 + 现金流造假）
        - 朱格拉周期（10 年，对中短期无意义）
        - 美林时钟（多次失效）

        采纳的理论:
        - 行业生命周期（导入/成长/成熟/衰退）
        - 估值分位（PE/PB 历史分位 → 均值回归）
        - 基钦周期（3-4 年库存周期，对中期有效）
        - 相对强弱 RS（行业指数 vs 大盘）
        - 政策脉冲量化（产业政策分级 + 关键词热度）
        - 景气度拐点 + 戴维斯双击
        - 筹码解禁（减持预披露 + 股东户数环比）
        - 事件驱动（业绩预告 + 并购 + ST 摘帽）
        """
        prompt = f"""【任务】用多个投资理论框架综合研判 A 股行业配置机会。**不是找热门，是找有逻辑支撑的**。

【输出格式】严格 JSON：
{{
  "collect_date": "YYYY-MM-DD",
  "theory_views": {{
    "industry_lifecycle": {{
      "emerging": [{{"industry": "...", "why": "处于导入/早期成长"}}],
      "growth": [{{"industry": "...", "why": "加速成长期"}}],
      "mature": [{{"industry": "...", "why": "成熟稳定现金流"}}],
      "declining": [{{"industry": "...", "why": "衰退应回避"}}]
    }},
    "valuation_percentile": {{
      "deep_undervalued": [{{"industry": "...", "pe_pct": "历史 PE 分位", "reason": "估值/基本面背离"}}],
      "overheated": [{{"industry": "...", "pe_pct": "历史分位", "reason": "估值风险"}}]
    }},
    "kitchin_inventory_cycle": {{
      "current_phase": "被动去库/主动补库/被动补库/主动去库",
      "benefiting_industries": [{{"industry": "...", "why": "周期位置判断"}}]
    }},
    "capex_cycle": {{
      "_comment": "原朱格拉周期，限定用于资本密集型行业 (钢铁/水泥/煤炭/工程机械/半导体设备/光伏风电设备/船舶)",
      "target_industries": ["钢铁", "半导体设备", "工程机械", "..."],
      "current_phase": "资本开支扩张/见顶/收缩/出清",
      "indicator_evidence": {{
        "fixed_asset_investment_yoy": "固定资产投资增速",
        "capex_over_depreciation": "资本开支/折旧比",
        "construction_in_progress_ratio": "在建工程占比",
        "capacity_utilization": "产能利用率"
      }},
      "beneficiaries": [{{"industry": "...", "why": "周期位置 + 盈利扩张路径"}}]
    }},
    "relative_strength": {{
      "strong_vs_market": [{{"industry": "...", "rs_signal": "近期 RS 表现"}}],
      "weak_vs_market": [{{"industry": "...", "rs_signal": "..."}}]
    }},
    "policy_pulse": [
      {{"policy": "具体政策", "level": "国家级/部委/地方", "date": "YYYY-MM-DD", "direction": "利好/利空", "beneficiaries": ["行业"], "source": "URL"}}
    ],
    "prosperity_inflection_davis": [
      {{"industry": "...", "inflection_type": "业绩/订单/价格 拐点", "evidence": "数据", "timeframe": "短/中/长", "davis_double": "双击条件是否满足"}}
    ],
    "event_driven_opportunities": [
      {{"industry": "...", "event_type": "业绩预告/并购/ST摘帽/定增破发", "specific_event": "描述", "timeline": "何时落地"}}
    ]
  }},
  "synthesized_picks": [
    {{
      "rank": 1,
      "industry": "行业名",
      "申万二级": "...",
      "theories_supporting": ["理论1", "理论2"],
      "driver_type": "政策/业绩/资金/主题",
      "payoff_ratio": "1:X（上/下赔率）",
      "confidence": "高/中/低",
      "time_horizon": "短/中/长线",
      "crowding": "低/中/高 (融资占比/换手率)",
      "core_logic": "一句话逻辑",
      "key_catalyst": ["催化"],
      "key_risk": ["风险"],
      "leading_stocks": ["代码1", "代码2", "代码3"]
    }}
  ],
  "summary": "5 句话研判结论 + 说明哪些理论当前最有效"
}}

【硬性要求】
- `synthesized_picks` 返回 {top_n} 个候选
- 每个**必须 ≥2 个理论框架支持**才能入选（AI 议会决议）
- 标注 driver_type（驱动类型）和 payoff_ratio（赔率）
- 标注 crowding（拥挤度，拥挤度高的要警示）
"""
        return self._query(prompt, max_tokens=5000)

    # ========== 新增：筹码与解禁事件 ==========
    def collect_chip_lockup_info(self, code: str, name: str) -> dict:
        prompt = f"""【任务】收集 A 股 **{name}({code})** 的筹码与解禁事件数据（最重要的 A 股变量之一）。

【输出 JSON】
{{
  "code": "{code}",
  "shareholder_concentration": {{
    "latest_date": "YYYY-MM-DD",
    "top10_holding_pct": "十大股东持股比例",
    "holder_count_trend": "股东户数近 4 季变化",
    "interpretation": "集中/分散/趋势判断"
  }},
  "upcoming_lockup_release": [
    {{"release_date": "YYYY-MM-DD", "shares": 数量, "pct_of_float": "占流通股%", "cost_basis": "原始成本", "impact": "压力大/中/小"}}
  ],
  "recent_reductions": [
    {{"date": "YYYY-MM-DD", "holder": "股东", "shares": 数量, "price_range": "减持价区间", "remaining": "剩余持股%"}}
  ],
  "block_trades": [
    {{"date": "YYYY-MM-DD", "shares": 数量, "price": "成交价", "premium_discount": "溢价/折价%", "counter_party_type": "机构/券商/游资"}}
  ],
  "buyback_repurchase": [
    {{"date": "YYYY-MM-DD", "shares": 数量, "purpose": "注销/股权激励", "progress": "已完成%"}}
  ],
  "summary": "一句话解读 —— 筹码是变紧还是变松，减持/解禁压力大小"
}}
"""
        return self._query(prompt)

    # ========== 新增：政策脉冲（个股 or 行业） ==========
    def collect_policy_pulse(self, target: str, is_industry: bool = False) -> dict:
        scope = "行业" if is_industry else "个股"
        prompt = f"""【任务】量化近 60 天**{target}**（{scope}）的政策脉冲。

【输出 JSON】
{{
  "target": "{target}",
  "scope": "{scope}",
  "collect_date": "YYYY-MM-DD",
  "policies": [
    {{
      "policy_name": "文件/规划名",
      "level": "国家级/部委/地方",
      "date": "YYYY-MM-DD",
      "issuer": "发布机构",
      "direction": "利好/利空/中性",
      "magnitude": "重大/一般/轻微",
      "key_points": ["要点 1", "要点 2"],
      "source": "URL"
    }}
  ],
  "keyword_heat": {{
    "positive_keywords": {{"稳市场": 频次, "科技自立": 频次}},
    "negative_keywords": {{"反内卷": 频次}},
    "heat_trend": "升温/降温/持平",
    "observation_period": "近 30 天"
  }},
  "summary": "3 句话政策面研判"
}}
"""
        return self._query(prompt)

    # ========== 新增：事件驱动 ==========
    def collect_event_driven(self, code: str, name: str) -> dict:
        prompt = f"""【任务】收集 A 股 **{name}({code})** 的事件驱动机会。

五类事件：业绩预告 / 并购重组 / ST 摘帽 / 定增破发 / 回购注销。

【输出 JSON】
{{
  "code": "{code}",
  "active_events": [
    {{
      "event_type": "业绩预告/并购重组/ST摘帽/定增破发/回购注销",
      "date": "YYYY-MM-DD",
      "description": "事件描述",
      "impact": "利好/利空 + 幅度估计",
      "expected_price_reaction": "股价反应路径",
      "source": "URL"
    }}
  ],
  "upcoming_events": [
    {{"event_type": "...", "expected_date": "YYYY-MM-DD", "description": "..."}}
  ],
  "has_davis_double_setup": "是否存在戴维斯双击条件",
  "summary": "一句话事件面综述"
}}
"""
        return self._query(prompt)

    # ========== 新增：财报质量识别 ==========
    def collect_earnings_quality(self, code: str, name: str) -> dict:
        prompt = f"""【任务】识别 **{name}({code})** 财报质量风险。

【输出 JSON】
{{
  "code": "{code}",
  "latest_report": "YYYY-MM-DD",
  "receivables_anomaly": {{
    "ratio_change": "应收账款/营收 的同比变化",
    "risk_level": "高/中/低",
    "concern": "具体风险"
  }},
  "inventory_anomaly": {{
    "ratio_change": "存货/营收 变化",
    "risk_level": "高/中/低"
  }},
  "cashflow_profit_divergence": {{
    "cfo_over_net_profit": "经营现金流/净利润 比值",
    "risk_level": "高/中/低",
    "interpretation": "正/负向背离"
  }},
  "goodwill_risk": {{
    "goodwill_pct_of_equity": "商誉/净资产 %",
    "risk_level": "高/中/低",
    "impairment_history": "近 3 年减值情况"
  }},
  "related_party_transactions": {{
    "scale": "关联交易规模",
    "risk_level": "高/中/低"
  }},
  "overall_quality_grade": "A/B/C/D",
  "summary": "一句话财报质量结论"
}}
"""
        return self._query(prompt)

    # ========== 新增：业绩预期差 ==========
    def collect_earnings_surprise(self, code: str, name: str) -> dict:
        prompt = f"""【任务】统计 **{name}({code})** 的业绩预期差。

【输出 JSON】
{{
  "code": "{code}",
  "consensus_estimates": {{
    "current_year_eps": "一致预期 EPS",
    "current_year_revenue": "一致预期营收（亿）",
    "growth_forecast": "增速预期",
    "estimate_count": "覆盖分析师数"
  }},
  "latest_actual_vs_estimate": {{
    "period": "YYYY-MM-DD",
    "actual_net_profit": 数字,
    "estimated_net_profit": 数字,
    "surprise_pct": "超/低于预期 %",
    "direction": "超预期/低于预期/符合"
  }},
  "estimate_revision_trend": {{
    "last_30d_upgrades": 次数,
    "last_30d_downgrades": 次数,
    "net_revision": "上调净数",
    "trend": "上修/下修/稳定"
  }},
  "summary": "一句话预期差判断"
}}
"""
        return self._query(prompt)

    # ========== 新增：产业链映射 ==========
    def collect_supply_chain_map(self, code: str, name: str) -> dict:
        prompt = f"""【任务】构建 **{name}({code})** 的产业链地图。

【输出 JSON】
{{
  "code": "{code}",
  "industry_chain_position": "上游/中游/下游",
  "upstream": {{
    "key_inputs": ["原材料 1"],
    "main_suppliers": [{{"name": "...", "code": "股票代码", "dependency": "供应依赖度"}}],
    "cost_pressure": "原料成本变化趋势"
  }},
  "downstream": {{
    "main_customers": [{{"name": "...", "code": "可能的股票代码", "revenue_pct": "占营收%"}}],
    "demand_trend": "需求趋势",
    "bargaining_power": "议价能力高/中/低"
  }},
  "competitors": [
    {{"name": "...", "code": "...", "market_share": "%", "差异化": "产品/成本/渠道"}}
  ],
  "chain_prosperity": "上中下游景气传导判断",
  "beneficiary_tree": "如果该公司景气上行，上游哪些受益，下游哪些受压",
  "summary": "一句话产业链位置综述"
}}
"""
        return self._query(prompt)
