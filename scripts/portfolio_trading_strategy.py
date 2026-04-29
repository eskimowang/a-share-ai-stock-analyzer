"""围绕真实持仓做交易策略分析 —— 组合级别 + 每只动作建议。

4 家 AI 并行（含 Claude 反方）+ 仲裁
"""
import sys, os, yaml, time, concurrent.futures
from datetime import datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.data_sources import UnifiedDataSource
from app.db import query_all
from app.ai.multi_brain import MultiBrain, build_brains_from_config
from app.ai.prompts import SYSTEM_PROMPT, SYSTEM_PROMPT_ADVERSARY

with open("/opt/stock-analyzer/config/config.yaml") as f:
    cfg = yaml.safe_load(f)

ds = UnifiedDataSource(tushare_token=cfg["data_sources"]["tushare"]["token"])
ts = ds.tushare

print(f"{'='*70}\n交易策略分析 - {datetime.now():%Y-%m-%d %H:%M}\n{'='*70}\n")

# ---------- 拉持仓数据 ----------
rows = query_all("""
    SELECT p.stock_code, p.stock_name, t.price as cost, t.quantity as qty
    FROM positions p JOIN trades t ON p.id=t.position_id
    WHERE p.status='holding' ORDER BY p.id
""")

print("[1/3] 拉每只持仓的最新数据…")
t0 = time.time()
positions = []
total_market_value = 0
total_cost = 0

for r in rows:
    code = r["stock_code"]
    name = r["stock_name"]
    cost = r["cost"]
    qty = r["qty"]

    try:
        # 日线（最近 20 天）+ 估值指标
        df, _ = ds.get_daily(code, start="20260301")
        db_df = ts.get_daily_basic(code)
        fi = ts.get_fina_indicator(code)
        # report_rc 频率严重限制（2次/分钟），本次跳过；研报可以稍后单独补
        reports_df = None

        last = df.iloc[-1] if df is not None and not df.empty else {}
        db_row = db_df.iloc[0] if not db_df.empty else {}
        fi_row = fi.iloc[0] if not fi.empty else {}

        current = last.get("close", cost)
        market_value = current * qty
        pl_pct = (current - cost) / cost * 100 if cost else 0
        pl_amount = (current - cost) * qty

        total_market_value += market_value
        total_cost += cost * qty

        # 最近 10 天 K 线简表
        recent_kline = []
        if df is not None and not df.empty:
            for _, d in df.tail(10).iterrows():
                recent_kline.append(
                    f"  {d['trade_date']} 开{d['open']:.2f}/高{d['high']:.2f}/低{d['low']:.2f}/收{d['close']:.2f} ({d.get('change_pct', 0):.2f}%)"
                )

        # 研报共识
        reports_summary = ""
        if reports_df is not None and not reports_df.empty:
            for _, rr in reports_df.head(3).iterrows():
                reports_summary += f"  - {rr.get('report_date','')} {rr.get('org_name','')} 评级: {rr.get('rating','')}\n"

        positions.append({
            "code": code, "name": name, "cost": cost, "qty": qty,
            "current": current, "pl_pct": pl_pct, "pl_amount": pl_amount,
            "market_value": market_value,
            "pe_ttm": db_row.get("pe_ttm"),
            "pb": db_row.get("pb"),
            "total_mv_yi": (db_row.get("total_mv") or 0) / 1e4,
            "roe": fi_row.get("roe"),
            "net_margin": fi_row.get("netprofit_margin"),
            "recent_kline": "\n".join(recent_kline),
            "reports_summary": reports_summary or "  无近期研报",
        })
    except Exception as e:
        print(f"  ! {code} 失败: {e}")

print(f"  {len(positions)} 只 / {time.time()-t0:.1f}s")
print(f"  组合市值: ¥{total_market_value:,.0f}  成本: ¥{total_cost:,.0f}  浮盈: {(total_market_value-total_cost)/total_cost*100:.2f}%")

# ---------- 构造组合级 prompt ----------
positions_detail = ""
for p in positions:
    weight = p["market_value"] / total_market_value * 100
    positions_detail += f"""
## {p['code']} {p['name']} (权重 {weight:.1f}%)
- 成本 {p['cost']:.3f}  现价 {p['current']:.3f}  盈亏 {p['pl_pct']:+.2f}% ({p['pl_amount']:+,.0f})
- 持仓 {p['qty']} 股  市值 ¥{p['market_value']:,.0f}
- PE {p['pe_ttm'] or 'N/A'}  PB {p['pb'] or 'N/A'}  总市值 {p['total_mv_yi']:.0f}亿
- ROE {p['roe'] or 'N/A'}%  净利率 {p['net_margin'] or 'N/A'}%
- 近 10 日 K 线:
{p['recent_kline']}
- 近期研报:
{p['reports_summary']}
"""

portfolio_prompt = f"""【交易策略任务】对下面 8 只持仓做**当日 / 本周操作建议**，**组合级 + 逐只级**双层分析。

## 组合概况
- 总市值: ¥{total_market_value:,.0f}
- 总成本: ¥{total_cost:,.0f}
- 组合浮盈: {(total_market_value-total_cost)/total_cost*100:+.2f}%
- 可用资金: ¥85,375.57

## 持仓明细
{positions_detail}

## 输出要求（Markdown）

### 🎯 组合级判断（100 字以内）
- 主线、集中度风险、板块暴露

### 🏆 逐只操作建议（每只）
| 代码 | 名称 | 操作 | 仓位变动 | 关键理由 | 触发条件 |
|---|---|---|---|---|---|

操作取值：坚守持有 / 小幅减仓 / 大幅减仓 / 全仓清仓 / 小幅加仓 / 大幅加仓 / 观察
仓位变动例：卖 30%、加 20%、保持
触发条件：跌破 X 元 / 突破 Y 元 / 放量滞涨 / 缩量反弹 ...

### 📊 多方博弈分析（挑 3 只最关键的逐一分析）
每只：主力动向、游资态度、机构评级、散户情绪

### ⚠️ 集中度风险评估
科大国盾量子占 43%，你怎么看？

### 💰 可用资金（¥8.5 万）使用建议
加仓哪只？还是留存？理由？

### 📅 本周关键事件日历
哪天可能有什么事件（财报、解禁、催化）需要盯

### 🚨 立即止损线
哪只已触及止损？跌到 X 元必须走？
"""

# ---------- 4 家 AI 分析（含对抗性）----------
print("\n[2/3] 4 家 AI 并行（Claude 扮演反方）…")
brains = build_brains_from_config(cfg)

def _one(client):
    is_adversary = client.name == "Claude"
    sys_role = SYSTEM_PROMPT_ADVERSARY if is_adversary else SYSTEM_PROMPT
    tag = " [反方]" if is_adversary else ""
    t = time.time()
    try:
        text = client.complete(sys_role, portfolio_prompt, max_tokens=3500)
        return f"{client.name}{tag}", text, time.time() - t
    except Exception as e:
        return f"{client.name}{tag}", f"[失败] {e}", 0

t0 = time.time()
results = {}
with concurrent.futures.ThreadPoolExecutor(max_workers=len(brains)) as pool:
    for f in concurrent.futures.as_completed([pool.submit(_one, b) for b in brains]):
        n, text, dur = f.result()
        results[n] = (text, dur)
print(f"  4 家并行 {time.time()-t0:.1f}s")

# ---------- 仲裁 ----------
print("\n[3/3] 仲裁整合…")
joined = "\n\n".join(f"## 【{n}】\n{t}" for n, (t, _) in results.items())

# 仲裁用 DeepSeek（Claude 已扮演反方，避免用同一个）
arb = [b for b in brains if b.name == "DeepSeek"][0]
final = arb.complete(
    "投资策略专家，负责整合多方观点给出最终可执行决策。",
    f"【持仓组合】总市值 ¥{total_market_value:,.0f}，8 只股票，可用资金 ¥85,375\n\n"
    f"以下是 4 家 AI 的分析（其中 Claude 是反方）：\n\n{joined}\n\n"
    "请整合成**最终可执行操作清单**：\n\n"
    "### 🎯 一句话组合结论\n"
    "### 📋 立即操作（今日执行）\n"
    "| 代码 | 名称 | 动作 | 股数 | 预期价 | 理由 |\n"
    "### 📋 本周观察（跟踪触发后操作）\n"
    "### 🔴 反方观点吸收\n"
    "哪些反方警示必须重视？怎么对冲？\n"
    "### ⭐ 资金使用建议\n"
    "¥85,375 可用资金：加仓哪只 / 买新票 / 留存？\n"
    "### 📅 未来 5 个交易日关键节点\n"
    "2500 字内，简洁可执行，不要客套。",
    max_tokens=4000,
)

# ---------- 保存 ----------
output_file = f"/opt/stock-analyzer/data-cache/portfolio_strategy_{datetime.now():%Y%m%d_%H%M}.md"
with open(output_file, "w", encoding="utf-8") as f:
    f.write(f"# 持仓交易策略 - {datetime.now():%Y-%m-%d %H:%M}\n\n")
    f.write(f"## 组合概况\n- 总市值: ¥{total_market_value:,.0f}\n")
    f.write(f"- 总成本: ¥{total_cost:,.0f}\n- 浮盈: {(total_market_value-total_cost)/total_cost*100:+.2f}%\n")
    f.write(f"- 可用资金: ¥85,375.57\n\n")
    f.write(f"## 持仓明细\n{positions_detail}\n\n---\n\n")
    for n, (t, d) in results.items():
        f.write(f"## 【{n}】 ({d:.1f}s)\n\n{t}\n\n---\n\n")
    f.write("# 🎯 最终决策（DeepSeek 仲裁）\n\n" + final)

print(f"\n{'='*70}\n✅ 完成\n报告：{output_file}\n{'='*70}\n")
print("\n【最终决策清单】\n")
print(final)
