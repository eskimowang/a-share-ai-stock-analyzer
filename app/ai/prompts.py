"""A 股分析 Prompt 模板（2026-04 升级版，基于 AI 议会结论）。

变动：
- 理论体系：删 DCF/朱格拉/美林时钟，加 筹码解禁/政策脉冲/事件驱动/业绩预期差/财报质量/产业链
- 荐股矩阵：驱动类型 × 赔率空间（时间为辅助标签）
- 新增：对抗性仲裁（反方系统提示）
"""

# ========================================
# 1. 正方分析师（默认）
# ========================================
SYSTEM_PROMPT = """你是严谨的 A 股投资分析师，擅长基本面 + 技术面 + 资金面三位一体分析。

核心理论工具箱（AI 议会修订版）：
- 估值: PE / PEG / PB 分位（DCF 在 A 股失效，不用）
- 戴维斯双击/双杀（利润与估值共振）
- 行业生命周期 + 景气度投资
- 基钦周期（库存 3-4 年，对中短期有效）
- 筹码与解禁：股东户数环比、大宗交易、减持预披露 ⭐
- 政策脉冲量化：国务院/证监会关键词热度、产业政策分级 ⭐
- 事件驱动：业绩预告、并购重组、ST摘帽、定增破发、回购注销 ⭐
- 财报质量：应收/存货异常、现金流与净利润背离、商誉预警 ⭐
- 资金验证：北向（剔量化）、龙虎榜席位持续性、两融、ETF申赎
- 技术面：道氏（趋势）、均线 + MACD（节奏）

核心纪律：
1. 数据驱动，不编造
2. 量化观点：具体买卖信号、目标价、止损位、赔率
3. 风险提示明确
4. 结构化输出，便于仲裁对比"""


# ========================================
# 2. 反方分析师（对抗性仲裁用）
# ========================================
SYSTEM_PROMPT_ADVERSARY = """你是做空/反方分析师，专门识别**看多一致预期的盲点**和潜在下跌触发。

这不是情绪化看空，是严格的**对冲思维**：即使数据面偏乐观，你也必须穷尽可以证伪的角度。

你的工作内容：
1. 找出这只股票**最可能的做空逻辑**（至少 3 个）
2. 识别**看多一致预期的脆弱点**（市场共识哪里可能错）
3. 估值泡沫评估：明确指出高估程度
4. 基本面隐患：应收/商誉/关联交易/股东减持
5. 估值锚（戴维斯双杀）：业绩下修 + 估值收缩的合理路径
6. 技术破位点：跌破哪里确认趋势反转
7. 最坏情况下跌空间和触发条件

**不允许中立、不允许平衡视角**。你的价值就是跳出群体思维，挖掘风险。

铁律：不看多、不中立、基于真实数据、量化表达（而不是情绪化)。"""


# ========================================
# 3. 单股分析 Prompt 模板（交易策略 + 基础分析共用）
# ========================================
ANALYSIS_PROMPT = """请分析以下 A 股标的：

## 股票信息
代码: {code}   名称: {name}   行业: {industry}

## 当前行情（{latest_date}）
- 收盘 {close}   涨跌 {change_pct}%
- PE-TTM {pe_ttm}   PB {pb}   总市值 {total_mv_yi} 亿

## 最近 15 天 K 线
{kline_table}

## 最近一期财务（{report_period}）
- 营收 {revenue_yi} 亿   净利润 {net_profit_yi} 亿
- ROE {roe}%   毛利率 {gross_margin}%   净利率 {net_margin}%
- 资产负债率 {debt_ratio}%

## 用户持仓
{position_info}

## 近期券商研报
{reports_summary}

## Codex 补充信息（公告/政策/事件/舆情）
{codex_info}

---

**请严格按以下 Markdown 结构输出，不要引言和结语：**

### 一句话结论
（买/卖/持有 + 核心理由，不超过 30 字）

### 评分（1-10 分）
- 基本面: X / 10 — 简要理由
- 技术面: X / 10 — 简要理由
- 资金面: X / 10 — 简要理由

### 驱动类型判断
（政策/业绩/资金/主题 之一，并说明为什么）

### 赔率评估
- 上行空间: X%（到 Y 元）
- 下行风险: X%（到 Z 元）
- 赔率: 1:X（上/下）

### 关键价位
- 支撑: X 元   压力: Y 元
- 建议买入区间: A-B 元
- 止损位: C 元   目标价（6 月）: D 元

### 3 个买入逻辑
1. ... 2. ... 3. ...

### 3 个风险点
1. ... 2. ... 3. ...

### 最终建议
- 操作: 【买入 / 加仓 / 持有 / 减仓 / 卖出】
- 仓位建议: X%   持有周期: 短/中/长
"""


# ========================================
# 4. 荐股矩阵专用 Prompt（驱动 × 赔率 + 时间标签）
# ========================================
DISCOVERY_MATRIX_PROMPT = """请从候选池中筛选出适合入选「驱动×赔率矩阵」的标的。

## 候选池
{candidates_summary}

## 当前大势
{market_view}

## 热点行业（多理论研判）
{industries_summary}

---

**请按此矩阵分类输出，严格 JSON 格式：**

```json
{{
  "matrix": {{
    "政策驱动": {{
      "低赔率": [{{"code":"","name":"","logic":"","time_tag":"短/中/长","crowding":"低/中/高"}}],
      "中赔率": [...],
      "高赔率": [...]
    }},
    "业绩驱动": {{...}},
    "资金驱动": {{...}}
  }},
  "warnings": ["拥挤度警示", "政策风险"],
  "summary": "3 句话综述"
}}
```

**纪律**:
- 每个标的**必须有至少 2 个理论支持**才能入选
- 拥挤度（crowding）高的标红，避免追高
- 时间标签（time_tag）：短<2周 / 中 2周-3月 / 长>3月
- 不强求每个格子都有候选，质量优先
"""


# ========================================
# 5. 交易策略 Prompt（多方博弈 + 日常操作）
# ========================================
TRADING_STRATEGY_PROMPT = """【每日交易策略】分析用户持仓当前多方博弈格局。

## 用户持仓
{positions_list}

## 今日市场环境
- 大盘: {market_today}
- 热门板块: {hot_sectors}
- 资金面: 北向 {northbound}，两融变动 {margin_change}

## 每只持仓的多方数据
{each_position_data}

## Codex 今日新闻/公告摘要
{codex_news}

---

**请对每只持仓输出（JSON）：**

```json
{{
  "positions": [
    {{
      "code": "",
      "name": "",
      "current_price": 0,
      "position_cost": 0,
      "position_pl_pct": 0,
      "game_analysis": {{
        "主力动向": "",
        "游资动向": "",
        "机构动向": "",
        "散户情绪": ""
      }},
      "action": "买入/加仓/持有/减仓/卖出",
      "action_reason": "",
      "confidence": "高/中/低",
      "urgency": "立即/今日/本周",
      "alert_level": "🔴紧急/🟡重要/🔵信息"
    }}
  ],
  "overall_portfolio_view": "",
  "biggest_risk_today": "",
  "top_action_today": ""
}}
```
"""


# ========================================
# 工具函数
# ========================================
def build_analysis_prompt(data: dict) -> str:
    """把股票数据字典填充到分析 prompt。"""
    def fmt(val, default="N/A", multiplier=1, digits=2):
        if val is None or val == "":
            return default
        try:
            return f"{float(val) * multiplier:.{digits}f}"
        except (TypeError, ValueError):
            return str(val)

    # K 线表格（最近 15 天）
    kline_rows = []
    for row in data.get("daily", [])[-15:]:
        kline_rows.append(
            f"| {row['trade_date']} | {row['open']:.2f} | {row['high']:.2f} | "
            f"{row['low']:.2f} | {row['close']:.2f} | {row.get('change_pct', 0):.2f}% |"
        )
    kline_table = "| 日期 | 开 | 高 | 低 | 收 | 涨跌 |\n| --- | --- | --- | --- | --- | --- |\n" + "\n".join(kline_rows)

    # 持仓信息
    pos = data.get("position")
    if pos and pos.get("holding_qty", 0) > 0:
        avg_cost = pos["net_cost"] / pos["holding_qty"] if pos["holding_qty"] else 0
        close_p = data.get("close") or 0
        position_info = (
            f"- 持仓数量: {pos['holding_qty']} 股\n"
            f"- 平均成本: {avg_cost:.2f} 元\n"
            f"- 当前浮盈: {((close_p - avg_cost) / avg_cost * 100):.2f}%"
            if avg_cost > 0 and close_p else "无持仓"
        )
    else:
        position_info = "用户未持仓，处于观察阶段"

    # 研报摘要
    reports = data.get("reports", [])
    if reports:
        lines = [
            f"- {r.get('report_date', '')} {r.get('org_name', '')} 评级: {r.get('rating', '')}"
            for r in reports[:5]
        ]
        reports_summary = "\n".join(lines)
    else:
        reports_summary = "近期无研报数据"

    # Codex 补充信息
    codex_info = data.get("codex_info", "")
    if not codex_info:
        codex_info = "（未接入 Codex 信息包，基于 Tushare 数据分析）"

    return ANALYSIS_PROMPT.format(
        code=data.get("code", ""),
        name=data.get("name", ""),
        industry=data.get("industry", "N/A"),
        latest_date=data.get("latest_date", ""),
        close=fmt(data.get("close")),
        change_pct=fmt(data.get("change_pct")),
        pe_ttm=fmt(data.get("pe_ttm")),
        pb=fmt(data.get("pb")),
        total_mv_yi=fmt(data.get("total_mv"), multiplier=1/1e4),
        report_period=data.get("report_period", "N/A"),
        revenue_yi=fmt(data.get("revenue"), multiplier=1/1e8),
        net_profit_yi=fmt(data.get("net_profit"), multiplier=1/1e8),
        roe=fmt(data.get("roe")),
        gross_margin=fmt(data.get("gross_margin")),
        net_margin=fmt(data.get("net_margin")),
        debt_ratio=fmt(data.get("debt_ratio")),
        kline_table=kline_table,
        position_info=position_info,
        reports_summary=reports_summary,
        codex_info=codex_info,
    )
