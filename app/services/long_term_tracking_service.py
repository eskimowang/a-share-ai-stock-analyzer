"""长期力量跟踪分析服务 —— 月度运行，从历史痕迹挖出"谁在下棋"。

数据维度:
- 股东户数 8 季度趋势（筹码谁在持续吸/派）
- 研报评级多年轨迹（机构态度演变）
- 财务趋势（业绩支不支撑股价）
- K 线 + 成交量（过去 60 天技术面）
- 资金流 + 龙虎榜 + 融资融券
"""
import concurrent.futures
import json
import logging
import time
from datetime import datetime

from ..config import CONFIG
from ..db import db, execute, query_all, query_one
from ..ai.multi_brain import build_brains_from_config
from ..ai.prompts import SYSTEM_PROMPT, SYSTEM_PROMPT_ADVERSARY

log = logging.getLogger(__name__)


def _ensure_tracking_tables():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS long_term_tracking_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT,
            positions_count INTEGER,
            report_md TEXT,
            final_arbitration TEXT,
            duration_seconds REAL,
            error_msg TEXT
        );
        """)


def _build_long_view(code: str, name: str, cost: float, qty: int) -> str:
    """构造单只股票的历史痕迹数据块"""
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
            total = sum(stats.values())
            report_section += f"  {y}: {total} 份研报 ({', '.join(f'{r}×{c}' for r, c in top)})\n"

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

    # K 线过去 60 天（压成周 K 简化）
    kline = query_all(
        "SELECT trade_date, close, volume, change_pct FROM daily_quotes "
        "WHERE stock_code=? ORDER BY trade_date DESC LIMIT 60",
        (code,),
    )
    kline_section = ""
    if kline:
        kline.reverse()
        weekly = []
        seen_weeks = set()
        for k in kline:
            try:
                dt = datetime.strptime(k["trade_date"], "%Y-%m-%d")
            except Exception:
                continue
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


def run_long_term_tracking() -> dict:
    """月度执行：对所有持仓做长期力量跟踪分析，返回最终报告 markdown。"""
    _ensure_tracking_tables()
    start = time.time()
    run_id = execute(
        "INSERT INTO long_term_tracking_runs(status) VALUES (?)", ("running",)
    )
    log.info(f"[长期跟踪] Run #{run_id} 启动")

    try:
        positions = query_all("""
            SELECT p.stock_code, p.stock_name,
            SUM(CASE WHEN t.trade_type='buy' THEN t.quantity ELSE -t.quantity END) as qty,
            SUM(CASE WHEN t.trade_type='buy' THEN t.price*t.quantity+t.fee ELSE -t.price*t.quantity+t.fee END) as cost
            FROM positions p JOIN trades t ON p.id=t.position_id
            WHERE p.status='holding' GROUP BY p.id
        """)
        if not positions:
            execute(
                "UPDATE long_term_tracking_runs SET status=?, error_msg=?, duration_seconds=? WHERE id=?",
                ("failed", "无持仓", time.time() - start, run_id),
            )
            return {"status": "failed", "error": "无持仓"}

        full_sections = "\n".join(
            _build_long_view(p["stock_code"], p["stock_name"], p["cost"], p["qty"])
            for p in positions
        )

        prompt = f"""【任务】对以下 {len(positions)} 只持仓做**长期力量跟踪分析**。不是日内判断，而是**从历史痕迹里挖出'谁在长期下棋'**。

你需要回答 4 个核心问题：

1. **筹码在谁手里**：从股东户数 4-8 季度趋势，判断
   - 是否有主力长期吸筹（户数持续下降）
   - 是否已完成派发（户数持续上升）
   - 季度节点（季报发布前后）有无异常变动

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
（哪些事件改变了股价节奏）

### 未来 3-6 个月推演
- 乐观剧本（XX 发生 → 股价可能到 Y）
- 悲观剧本（ZZ 发生 → 股价可能到 W）
- 最可能剧本（基于当前痕迹 → XXX）

### 给主人的一句话建议（大白话）

---

最后给 **组合结论**：
- 谁是**最该长期跟踪**的（最有信号的）
- 谁是**该尽早放弃**的（最糟糕的历史痕迹）
- **组合整体力量判断**：顺风还是逆风

**揭露式写作，数据证据要引用具体日期和数字。**
"""

        brains = build_brains_from_config(CONFIG)

        def _one(c):
            sys = SYSTEM_PROMPT_ADVERSARY if c.name == "Claude" else SYSTEM_PROMPT
            try:
                return c.name, c.complete(sys, prompt, max_tokens=4000)
            except Exception as e:
                return c.name, f"[失败] {e}"

        log.info(f"[长期跟踪] 4 家 AI 并行分析 {len(positions)} 只持仓...")
        opinions = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(brains)) as pool:
            for f in concurrent.futures.as_completed([pool.submit(_one, b) for b in brains]):
                n, t = f.result()
                opinions[n] = t

        # 仲裁 —— 整合 4 家意见
        log.info("[长期跟踪] DeepSeek 仲裁整合...")
        from .game_memory import format_track_record_for_prompt
        track_record = format_track_record_for_prompt()
        ds = next((b for b in brains if b.name == "DeepSeek"), brains[0])
        joined = "\n\n".join(f"# 【{n}】\n{t}" for n, t in opinions.items())
        final = ds.complete(
            "资深 A 股研究员，整合 4 家长期跟踪分析。按历史胜率加权采纳。",
            f"{track_record}\n\n"
            f"4 家 AI 对 {len(positions)} 只持仓的长期力量跟踪分析:\n\n{joined}\n\n"
            "请整合输出一份 **长期跟踪总纲**（markdown，2500 字内）：\n\n"
            "## 一、筹码在谁手里（分类 + 证据）\n"
            "🟢 长期吸筹（谁在买，推断哪类资金）\n"
            "🔴 已派发完毕（谁在卖，推断手法）\n"
            "🟡 震荡不明（为什么看不清）\n\n"
            "## 二、机构态度变化最大的 3 只\n\n"
            "## 三、业绩真相（谁业绩支撑、谁透支）\n\n"
            "## 四、建议持续跟踪的 3 个信号（具体到数据指标）\n\n"
            "## 五、最有价值的 1 个历史发现\n\n"
            "数据证据要具体（引用日期 + 数字）。",
            max_tokens=4000, reasoning_effort="high",
        )

        # 组装完整报告 markdown
        report_md = (
            f"# 长期力量跟踪分析 - {datetime.now():%Y-%m-%d %H:%M}\n\n"
            f"持仓数: {len(positions)} 只\n\n"
        )
        for n, t in opinions.items():
            report_md += f"## 【{n}】\n\n{t}\n\n---\n\n"
        report_md += f"\n\n# 🎯 长期跟踪总纲（DeepSeek 仲裁）\n\n{final}\n"

        duration = time.time() - start
        execute(
            "UPDATE long_term_tracking_runs SET status=?, positions_count=?, "
            "report_md=?, final_arbitration=?, duration_seconds=? WHERE id=?",
            ("success", len(positions), report_md, final, duration, run_id),
        )
        log.info(f"[长期跟踪] Run #{run_id} 完成，耗时 {duration:.0f}s")

        return {
            "status": "success",
            "run_id": run_id,
            "positions_count": len(positions),
            "duration_seconds": duration,
            "report_md": report_md,
            "arbitration": final,
        }

    except Exception as e:
        log.exception(f"[长期跟踪] Run #{run_id} 失败")
        execute(
            "UPDATE long_term_tracking_runs SET status=?, error_msg=?, duration_seconds=? WHERE id=?",
            ("failed", str(e), time.time() - start, run_id),
        )
        return {"status": "failed", "error": str(e)}


def get_latest_tracking() -> dict | None:
    _ensure_tracking_tables()
    row = query_one(
        "SELECT * FROM long_term_tracking_runs WHERE status='success' "
        "ORDER BY run_at DESC LIMIT 1"
    )
    if not row:
        return None
    return {
        "run_at": row["run_at"],
        "positions_count": row["positions_count"],
        "duration_seconds": row["duration_seconds"],
        "report_md": row["report_md"],
        "arbitration": row["final_arbitration"],
    }
