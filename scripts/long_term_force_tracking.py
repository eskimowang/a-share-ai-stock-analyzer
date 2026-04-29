"""【长期力量跟踪分析】挖掘 8 只持仓背后的历史痕迹。

基于数据库已有数据：
- 股东户数 8 季度趋势（是谁在持续吸筹/出货？）
- 研报评级多年轨迹（机构态度如何演变）
- 财务趋势（业绩支不支撑股价）
- K 线 + 成交量（过去 60 天的技术面）
- 资金流 + 龙虎榜 + 融资融券近期

让 4 家 AI 从这些历史痕迹里看出"谁在下棋"。
"""
import sys, os, yaml, time, concurrent.futures
from datetime import datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import query_all, query_one
from app.ai.multi_brain import build_brains_from_config
from app.ai.prompts import SYSTEM_PROMPT, SYSTEM_PROMPT_ADVERSARY

with open("/opt/stock-analyzer/config/config.yaml") as f:
    cfg = yaml.safe_load(f)

print(f"{'='*70}\n长期力量跟踪分析 - {datetime.now():%Y-%m-%d %H:%M}\n{'='*70}\n")

# ---------- 拉每只持仓的长期数据 ----------
positions = query_all("""
    SELECT p.stock_code, p.stock_name,
    SUM(CASE WHEN t.trade_type='buy' THEN t.quantity ELSE -t.quantity END) as qty,
    SUM(CASE WHEN t.trade_type='buy' THEN t.price*t.quantity+t.fee ELSE -t.price*t.quantity+t.fee END) as cost
    FROM positions p JOIN trades t ON p.id=t.position_id
    WHERE p.status='holding' GROUP BY p.id
""")

def build_long_view(code: str, name: str, cost: float, qty: int) -> str:
    """为一只股票构造"历史痕迹"数据块"""
    # 股东户数 8 季度
    holders = query_all(
        "SELECT end_date, holder_num FROM holder_number_cache "
        "WHERE stock_code=? ORDER BY end_date", (code,)
    )
    holder_section = ""
    if holders:
        lines = []
        prev = None
        for h in holders:
            n = h["holder_num"] or 0
            chg = ""
            if prev and prev > 0:
                pct = (n - prev) / prev * 100
                chg = f" ({pct:+.1f}%)"
            lines.append(f"  {h['end_date']}: {n:,} 户{chg}")
            prev = n
        holder_section = "\n".join(lines)

    # 研报多年轨迹
    reports = query_all(
        "SELECT report_date, broker, rating FROM reports_cache "
        "WHERE stock_code=? ORDER BY report_date DESC LIMIT 30",
        (code,),
    )
    report_section = ""
    if reports:
        # 按年度聚合
        year_stats = {}
        for r in reports:
            date = r.get("report_date") or ""
            year = date[:4] if date else "未知"
            rating = r.get("rating") or "-"
            year_stats.setdefault(year, {}).setdefault(rating, 0)
            year_stats[year][rating] += 1
        for y in sorted(year_stats.keys(), reverse=True)[:4]:
            stats = year_stats[y]
            top = sorted(stats.items(), key=lambda x: -x[1])[:3]
            report_section += f"  {y}: {len(sum([[r]*c for r, c in stats.items()], []))} 份研报 ({', '.join(f'{r}×{c}' for r, c in top)})\n"

    # 财务趋势
    fins = query_all(
        "SELECT report_period, roe, gross_margin, net_margin FROM financials "
        "WHERE stock_code=? ORDER BY report_period DESC LIMIT 8",
        (code,),
    )
    fin_section = ""
    if fins:
        fins.reverse()
        fin_section = "| 期末 | ROE% | 毛利率% | 净利率% |\n|---|---|---|---|\n"
        for f in fins:
            fin_section += f"| {f['report_period']} | {f.get('roe') or '-'} | {f.get('gross_margin') or '-'} | {f.get('net_margin') or '-'} |\n"

    # K 线过去 60 天
    kline = query_all(
        "SELECT trade_date, close, volume, change_pct FROM daily_quotes "
        "WHERE stock_code=? ORDER BY trade_date DESC LIMIT 60",
        (code,),
    )
    kline_section = ""
    if kline:
        kline.reverse()
        # 取每周最后一天简化
        weekly = []
        seen_weeks = set()
        for k in kline:
            dt = datetime.strptime(k["trade_date"], "%Y-%m-%d")
            week = f"{dt.isocalendar().year}-W{dt.isocalendar().week}"
            if week not in seen_weeks:
                seen_weeks.add(week)
                weekly.append(k)
        kline_section = "近 60 日关键周K线:\n"
        for k in weekly[-12:]:
            kline_section += f"  {k['trade_date']}  收 {k['close']:.2f}  量 {int(k.get('volume') or 0):>10,}\n"

    # 当前估值
    snap = query_one(
        "SELECT close, pe_ttm, pb, total_mv FROM daily_basic "
        "WHERE stock_code=? ORDER BY trade_date DESC LIMIT 1",
        (code,),
    ) or {}
    current = snap.get("close") or 0
    avg = cost / qty if qty else 0

    return f"""
## {code} {name}
- 当前: {current:.2f}元  成本: {avg:.2f}元  浮盈: {(current-avg)/avg*100:+.2f}%  PE: {snap.get('pe_ttm')}  PB: {snap.get('pb')}

### 股东户数 · 筹码是谁在持续进/出（关键！）
{holder_section or '（无历史）'}

### 券商态度演变
{report_section or '（无历史研报）'}

### 财务业绩趋势
{fin_section or '（无历史）'}

### 60 日价量轨迹
{kline_section or '（无）'}
"""


# 构造完整 prompt
full_sections = "\n".join(
    build_long_view(p["stock_code"], p["stock_name"], p["cost"], p["qty"])
    for p in positions
)

prompt = f"""【任务】对以下 8 只持仓做**长期力量跟踪分析**。不是日内判断，而是**从历史痕迹里挖出'谁在长期下棋'**。

你需要回答 4 个核心问题：

1. **筹码在谁手里**：从股东户数 4-8 季度趋势，判断
   - 是否有主力长期吸筹（户数持续下降）
   - 是否已完成派发（户数持续上升）
   - 季度节点（季报季报发布前后）有无异常变动

2. **机构态度是否在变化**：从研报评级轨迹，判断
   - 覆盖券商数是在增加还是减少
   - 评级从"买入"→"中性"→"减持"？还是反向？
   - 哪些券商从看多转空（信号非常强）

3. **业绩支不支撑股价**：从财务趋势，判断
   - ROE、毛利率、净利率的方向
   - 业绩是否出现拐点（很重要）
   - 当前估值是否被透支（PE×增长 vs 基本面）

4. **价量配合讲了什么故事**：从 60 日 K 线 + 成交量，判断
   - 是否有"筹码分布型态"（W底/M顶/箱体/上升通道）
   - 关键位置的成交量变化

---

{full_sections}

---

# 输出要求

对每只持仓给一份 **"长期跟踪档案"**：

## {{股票}} {{代码}}
### 筹码方向定性（3 选 1）
🟢 主力长期吸筹 / 🔴 主力已派发完毕 / 🟡 震荡不明

### 机构态度
（从研报轨迹看出什么）

### 业绩拐点
（是上行拐点/下行拐点/持续疲弱/持续强劲）

### 历史关键事件复盘
（哪些事件改变了股价节奏，比如财报、政策、股东变动）

### 未来 3-6 个月推演
基于历史痕迹，如果我继续持有：
- 乐观剧本（XX 发生 → 股价可能到 Y）
- 悲观剧本（ZZ 发生 → 股价可能到 W）
- 最可能剧本（基于当前痕迹 → XXX）

### 给主人的一句话建议（大白话）

---

最后给 **组合结论**：
- 8 只里谁是你**最该长期跟踪**的（最有信号的）
- 谁是**该尽早放弃**的（最糟糕的历史痕迹）
- **组合整体力量判断**：是顺风组合还是逆风组合

**揭露式写作，不要模糊。数据证据要引用具体日期和数字。**
"""

# 4 家并行
brains = build_brains_from_config(cfg)
def _one(c):
    sys = SYSTEM_PROMPT_ADVERSARY if c.name == "Claude" else SYSTEM_PROMPT
    t = time.time()
    try:
        return c.name, c.complete(sys, prompt, max_tokens=4000), time.time()-t
    except Exception as e:
        return c.name, f"[失败] {e}", 0

print("4 家 AI 并行分析（预计 1-3 分钟）...")
t0 = time.time()
results = {}
with concurrent.futures.ThreadPoolExecutor(max_workers=len(brains)) as pool:
    for f in concurrent.futures.as_completed([pool.submit(_one, b) for b in brains]):
        n, t, d = f.result()
        results[n] = (t, d)
print(f"总耗时 {time.time()-t0:.1f}s\n")

# 输出
output = f"/opt/stock-analyzer/data-cache/long_term_tracking_{datetime.now():%Y%m%d_%H%M}.md"
with open(output, "w", encoding="utf-8") as f:
    f.write(f"# 长期力量跟踪分析 - {datetime.now():%Y-%m-%d %H:%M}\n\n")
    for n, (t, d) in results.items():
        f.write(f"## 【{n}】({d:.1f}s)\n\n{t}\n\n---\n\n")

print(f"✅ 完成，报告：{output}\n")
# 仲裁
try:
    ds = next((b for b in brains if b.name == "DeepSeek"), brains[0])
    joined = "\n\n".join(f"# 【{n}】\n{t}" for n, (t, _) in results.items())
    final = ds.complete(
        "资深 A 股研究员，整合 4 家长期跟踪分析。",
        f"4 家 AI 对 8 只持仓的长期力量跟踪分析:\n\n{joined}\n\n"
        "请整合输出一份 **长期跟踪总纲**（markdown，2500 字内）：\n\n"
        "## 一、筹码在谁手里（8 只分类 + 证据）\n"
        "🟢 长期吸筹（谁在买，推断是哪类资金）\n"
        "🔴 已派发完毕（谁在卖，推断手法）\n"
        "🟡 震荡不明（为什么看不清）\n\n"
        "## 二、机构态度变化最大的 3 只\n\n"
        "## 三、业绩真相（8 只谁业绩支撑、谁透支）\n\n"
        "## 四、建议持续跟踪的 3 个信号（具体到数据指标）\n\n"
        "## 五、最有价值的 1 个历史发现\n\n"
        "数据证据要具体。",
        max_tokens=4000,
    )
    with open(output, "a", encoding="utf-8") as f:
        f.write("\n\n# 🎯 长期跟踪总纲（DeepSeek 仲裁）\n\n" + final)
    print(final)
except Exception as e:
    print(f"仲裁失败: {e}")
