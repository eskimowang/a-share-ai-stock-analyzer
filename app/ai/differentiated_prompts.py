"""差异化信息源 prompt —— 每家 AI 读不同侧面的数据，打破"同源污染"。

分工:
  DeepSeek → 财务视角（利润表 + 资产负债 + 现金流 + ROE/PE 变化）
  Gemini   → 机构视角（研报评级 + 目标价 + 覆盖度变化 + 一致预期）
  Claude   → 技术/博弈视角（K线 + 资金流 + 龙虎榜 + 筹码 + 融资盘）对抗性
  Codex    → 政策/事件视角（政策脉冲 + 事件驱动 + 产业链）

为何不把全部数据给所有人: 现状是 4 家读同样 prompt，观点天然相关，仲裁价值折损。
通过偏置数据片，迫使每家从自己擅长的角度给出独立判断。

仲裁阶段: DeepSeek 读 4 份不同侧面的独立分析后做最终综合。
"""
from typing import Optional


BASE_RULES = """
核心纪律：
1. 数据驱动，不编造
2. 你只看到你那部分数据，其他维度的判断留给其他 AI（你不必强行跨界）
3. 给出结构化结论：核心判断 + 关键证据（日期+数字） + 赔率 + 对仲裁有价值的信号
4. 如果你这份数据不足以下结论，明确说"数据不足"，不要臆测
"""


def build_financial_prompt(data: dict) -> str:
    """DeepSeek 看财务面"""
    code = data.get("code", "")
    name = data.get("name", "")
    fins = data.get("financials", [])  # 期望后端塞 ROE/毛利率/净利率历史
    snap = data.get("daily_basic", {}) or {}

    fin_section = ""
    if fins:
        fin_section = "| 期末 | ROE% | 毛利率% | 净利率% | 营收同比 | 净利同比 |\n|---|---|---|---|---|---|\n"
        for f in fins[-8:]:
            fin_section += (f"| {f.get('report_period','')} | {f.get('roe','-')} | "
                            f"{f.get('gross_margin','-')} | {f.get('net_margin','-')} | "
                            f"{f.get('revenue_yoy','-')} | {f.get('profit_yoy','-')} |\n")
    else:
        fin_section = "（无财务历史）"

    return f"""【你负责 · 财务基本面】{name}({code})

{BASE_RULES}

## 估值快照
- 现价 {snap.get('close','?')}
- PE(TTM) {snap.get('pe_ttm','?')}
- PB {snap.get('pb','?')}
- 总市值 {snap.get('total_mv','?')}（单位: 万元）
- 换手率 {snap.get('turnover_rate','?')}%

## 财务趋势（近 8 期）
{fin_section}

---

## 你要回答

1. **业绩趋势定性**: 向上/向下/拐点/持续弱
2. **估值水平**: 高/中/低（结合 PE × ROE × 行业）
3. **戴维斯双击/双杀条件**: 是否满足
4. **财报质量隐忧**: 应收/存货/现金流背离 等（从有限数据能看出的）
5. **一句话判断 + 赔率**（基于财务面）
"""


def build_institutional_prompt(data: dict) -> str:
    """Gemini 看机构研报"""
    code = data.get("code", "")
    name = data.get("name", "")
    reports = data.get("reports", [])

    recent = ""
    if reports:
        recent = "| 日期 | 券商 | 评级 | 目标价 |\n|---|---|---|---|\n"
        for r in reports[:20]:
            recent += (f"| {r.get('report_date','')} | {r.get('broker','')} | "
                       f"{r.get('rating','-')} | {r.get('target_price','-')} |\n")
    else:
        recent = "（近期无研报）"

    # 粗聚合 评级数
    rating_count = {}
    for r in reports:
        rt = r.get("rating") or "-"
        rating_count[rt] = rating_count.get(rt, 0) + 1

    return f"""【你负责 · 机构视角 / 研报面】{name}({code})

{BASE_RULES}

## 近 20 份研报
{recent}

## 评级分布
{rating_count}

---

## 你要回答

1. **机构覆盖度**: 上升/下降/稳定
2. **评级趋势**: 从买入→中性 or 反向？哪家券商转向？
3. **目标价共识**: 均值是否显著高于现价？
4. **一致预期风险**: 预期是否已过热（估值已反映）
5. **一句话判断 + 赔率**（基于研报面）
"""


def build_technical_prompt(data: dict) -> str:
    """Claude 看技术/博弈（对抗性）"""
    code = data.get("code", "")
    name = data.get("name", "")
    daily = data.get("daily", [])
    moneyflow = data.get("moneyflow", [])
    top_list = data.get("top_list", [])
    holder = data.get("holder_trend", [])
    margin = data.get("margin_trend", [])

    kline_rows = ""
    if daily:
        for k in daily[-20:]:
            kline_rows += (f"  {k.get('trade_date','')}  "
                           f"开{k.get('open',0):.2f}/高{k.get('high',0):.2f}/"
                           f"低{k.get('low',0):.2f}/收{k.get('close',0):.2f}  "
                           f"量{int(k.get('volume',0) or 0):>10,}  ({k.get('change_pct',0):+.2f}%)\n")
    mf_rows = ""
    if moneyflow:
        for m in moneyflow[:5]:
            mf_rows += f"  {m.get('trade_date','')}  净流入{m.get('net_mf_amount',0)}  大买{m.get('buy_lg_amount',0)}/大卖{m.get('sell_lg_amount',0)}\n"
    tl_rows = ""
    if top_list:
        for t in top_list[:5]:
            tl_rows += f"  {t.get('trade_date','')}  {t.get('reason','')}  净买入{t.get('net_buy_amount',0)}\n"

    return f"""【你负责 · 技术面 + 博弈揭露（反方视角）】{name}({code})

{BASE_RULES}

**你是反方**。专门识别看多盲点、诱多套路、主力派发痕迹。

## 近 20 日 K 线
```
{kline_rows or '（无）'}
```

## 近 5 日资金流向
```
{mf_rows or '（无）'}
```

## 龙虎榜（近 5 日）
```
{tl_rows or '（未上榜）'}
```

## 股东户数近期
{holder[-4:] if holder else '（无）'}

## 融资余额近期
{margin[-5:] if margin else '（无）'}

---

## 14 类操作手法对照（从 K 线 + 资金流 + 龙虎榜 + 筹码识别）

诱多出货 / 假突破 / 拉升派发 / 尾盘偷袭 / 借利好出货 / 借利空吸筹 / 洗盘 / 吸筹 / 对倒 / 龙虎榜接力 / 机构抱团 / 北向骗线 / 大宗派发 / 融资爆仓

## 你要回答

1. **今日局面定性（对照上面 14 类）**: 唯一标签
2. **具体证据链**: 从 K 线/资金流/筹码里找出 3-5 条支撑
3. **主力行为推断**: 吸筹/派发/震荡/诱多
4. **对抗性警告**: 即使看多共识，下跌触发点在哪
5. **一句话判断 + 赔率**（看空或谨慎视角）
"""


def build_policy_prompt(data: dict) -> str:
    """Codex 看政策/事件/产业链（带联网搜索能力）"""
    code = data.get("code", "")
    name = data.get("name", "")
    industry = data.get("industry", "") or "—"

    return f"""【你负责 · 政策/事件/产业链】{name}({code}) · 行业: {industry}

{BASE_RULES}

**利用你的联网搜索能力**，收集近 60 天内：

## 需要回答的 4 个问题

1. **政策脉冲**：国务院 / 部委 / 地方 是否有影响本行业 or 本公司的新政？
   - 利好还是利空？重大/一般？
   - 关键词热度变化（如"反内卷""国产替代""稳市场"等）

2. **事件驱动**：业绩预告、并购重组、ST 摘帽、定增破发、回购注销 —— 近期有没有？
   - 是否存在"戴维斯双击"事件条件？

3. **产业链传导**：
   - 上游原材料成本变化（影响毛利）
   - 下游需求变化（影响营收）
   - 竞争对手动向

4. **宏观背景**：
   - 本行业当前在经济周期的哪个位置（导入/成长/成熟/衰退）
   - 对本公司的影响是顺风还是逆风

## 输出结构

- 政策 + 事件逐条列出（日期/来源）
- 产业链传导的**量化判断**（不是定性）
- **一句话判断 + 赔率**（基于政策 + 事件面）
- 最后说明：哪些数据**没查到**，供其他 AI 补齐
"""


def get_prompt_for(ai_name: str, data: dict) -> str:
    """按 AI 名字派发差异化 prompt。无匹配则返回 None。"""
    mapping = {
        "DeepSeek": build_financial_prompt,
        "Gemini": build_institutional_prompt,
        "Claude": build_technical_prompt,
        "Codex": build_policy_prompt,
    }
    fn = mapping.get(ai_name)
    if not fn:
        return None
    prompt = fn(data)
    try:
        from ..services.trading_cognition_service import format_trading_cognition_for_prompt
        prompt += "\n\n" + format_trading_cognition_for_prompt(
            context=f"{ai_name} {data.get('code','')} {data.get('name','')} 单股分析 仓位 止损 主线",
            limit=8,
        )
    except Exception:
        pass
    try:
        from ..services.analysis_architecture_service import format_analysis_architecture_for_prompt
        prompt += "\n\n" + format_analysis_architecture_for_prompt(
            context=f"{ai_name} {data.get('code','')} {data.get('name','')} 单股分析 研报 数据 风控 PK",
            limit=7,
        )
    except Exception:
        pass
    try:
        from ..services.open_source_tool_reference_service import format_open_source_tool_references_for_prompt
        prompt += "\n\n" + format_open_source_tool_references_for_prompt(
            context=f"{ai_name} {data.get('code','')} {data.get('name','')} 单股分析 回测 技术指标 数据 组合",
            limit=5,
        )
    except Exception:
        pass
    try:
        from ..services.decision_feedback_service import format_decision_feedback_for_prompt
        prompt += "\n\n" + format_decision_feedback_for_prompt(
            context=f"{ai_name} {data.get('code','')} {data.get('name','')} 分析 决策 行动 反馈 盈利",
            limit=4,
        )
    except Exception:
        pass
    return prompt
