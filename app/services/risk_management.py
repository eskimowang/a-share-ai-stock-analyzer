"""风险管理服务 —— 头寸分配 / 止损规则 / 延迟滑点模型。

V4 Max 方案:
  - 波动率倒数加权分配仓位（单股上限 20%）
  - 硬止损 -8% / 回撤止损 -12%
  - 成交价用下一根分钟 K 开盘 + 0.15% 滑点
"""
import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from ..db import db, execute, query_all, query_one

log = logging.getLogger(__name__)

# ========== 风控参数 ==========
HARD_STOP_LOSS_PCT = -8.0   # 浮亏 -8% 硬止损
SOFT_COGNITION_WARNING_PCT = -5.0  # 用户交易认知：约 -5% 先复核/降风险
TRAILING_STOP_PCT = -12.0   # 从最高点回撤 -12% 跟踪止损
SLIPPAGE_PCT = 0.15         # 滑点 0.15%
MAX_SINGLE_POSITION = 0.20  # 单股上限 20%
VOL_LOOKBACK_DAYS = 20
STOP_LOSS_EPSILON = 1e-6


def _ensure_risk_tables():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS stop_loss_triggers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            triggered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            stock_code TEXT,
            stock_name TEXT,
            rule TEXT,           -- hard_stop_8pct / trailing_12pct / verdict_reversal
            entry_price REAL,
            current_price REAL,
            peak_price REAL,
            drawdown_pct REAL,
            suggested_action TEXT,
            executed INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS position_allocations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            source TEXT,         -- discovery / manual / closing
            source_run_id INTEGER,
            candidates TEXT,     -- JSON: [{code, name, vol_20d, weight_pct, shares, amount}]
            total_capital REAL,
            note TEXT
        );
        """)


# ========== 1. 头寸分配器 ==========
def _calc_20d_volatility(code: str) -> Optional[float]:
    """用近 20 日日收益率标准差 * sqrt(252) 作为年化波动率代理。"""
    rows = query_all(
        "SELECT change_pct FROM daily_quotes WHERE stock_code=? "
        "ORDER BY trade_date DESC LIMIT ?", (code, VOL_LOOKBACK_DAYS + 1),
    )
    if len(rows) < 10:
        return None
    pcts = [(r["change_pct"] or 0) / 100 for r in rows]
    n = len(pcts)
    mean = sum(pcts) / n
    var = sum((p - mean) ** 2 for p in pcts) / max(1, n - 1)
    return var ** 0.5


def allocate_by_inverse_volatility(candidates: list[dict], total_capital: float,
                                      max_single: float = MAX_SINGLE_POSITION) -> dict:
    """V4 推荐：波动率倒数加权 w_i = 1 / (vol_i + ε)

    candidates: [{code, name}]
    total_capital: 可用资金（元）
    max_single: 单股上限 %（0-1）

    返回: [{code, name, vol_20d, weight_pct, amount, shares(估算)}]
    """
    _ensure_risk_tables()
    enriched = []
    for c in candidates:
        vol = _calc_20d_volatility(c["code"])
        if vol is None:
            continue
        enriched.append({"code": c["code"], "name": c.get("name", ""), "vol_20d": vol})

    if not enriched:
        return {"error": "候选股 20 日数据不足", "allocations": []}

    # 倒数加权
    inv_vols = [1.0 / (e["vol_20d"] + STOP_LOSS_EPSILON) for e in enriched]
    total_inv = sum(inv_vols)
    raw_weights = [iv / total_inv for iv in inv_vols]

    # 应用单股上限 + 重分配超出部分
    weights = [min(w, max_single) for w in raw_weights]
    slack = 1.0 - sum(weights)
    if slack > 0:
        # 把溢出空间等比分回未触顶的标的
        non_capped = [i for i, w in enumerate(weights) if w < max_single]
        if non_capped:
            bonus = slack / len(non_capped)
            for i in non_capped:
                weights[i] = min(max_single, weights[i] + bonus)

    allocations = []
    for e, w in zip(enriched, weights):
        amt = total_capital * w
        # 尝试获取最新价估算股数
        snap = query_one(
            "SELECT close FROM daily_quotes WHERE stock_code=? "
            "ORDER BY trade_date DESC LIMIT 1", (e["code"],)
        )
        close = (snap or {}).get("close") or 0
        shares = int(amt / close / 100) * 100 if close else 0  # A 股 100 股起
        allocations.append({
            "code": e["code"], "name": e["name"],
            "vol_20d": round(e["vol_20d"], 4),
            "vol_20d_annualized": round(e["vol_20d"] * (252 ** 0.5), 4),
            "weight_pct": round(w * 100, 2),
            "amount": round(amt, 2),
            "shares": shares,
            "est_close": close,
        })

    return {
        "total_capital": total_capital,
        "max_single_pct": max_single * 100,
        "allocations": allocations,
        "total_weight_pct": round(sum(w for w in weights) * 100, 2),
    }


# ========== 2. 止损规则引擎 ==========
def check_stop_loss_for_all_positions() -> list[dict]:
    """扫描所有持仓，触发止损规则。返回触发的列表。"""
    _ensure_risk_tables()
    positions = query_all("""
        SELECT p.id, p.stock_code, p.stock_name,
        SUM(CASE WHEN t.trade_type='buy' THEN t.quantity ELSE -t.quantity END) as qty,
        SUM(CASE WHEN t.trade_type='buy' THEN t.price*t.quantity+t.fee ELSE -t.price*t.quantity+t.fee END) as cost,
        MAX(CASE WHEN t.trade_type='buy' THEN t.trade_date END) as last_buy_date
        FROM positions p JOIN trades t ON p.id=t.position_id
        WHERE p.status='holding' GROUP BY p.id
    """)
    triggers = []
    for p in positions:
        code = p["stock_code"]
        qty = p["qty"] or 0
        if qty <= 0:
            continue
        cost = p["cost"]
        avg_price = cost / qty if qty else 0

        # 最新现价 - 优先实时
        current = None
        try:
            from ..data_sources import UnifiedDataSource
            from ..config import CONFIG
            ds = UnifiedDataSource(tushare_token=CONFIG["data_sources"]["tushare"].get("token"))
            rt = ds.get_realtime(code)
            current = (rt or {}).get("price")
        except Exception:
            pass
        if not current:
            snap = query_one(
                "SELECT close FROM daily_quotes WHERE stock_code=? "
                "ORDER BY trade_date DESC LIMIT 1", (code,)
            )
            current = (snap or {}).get("close") or 0
        if not current or not avg_price:
            continue

        drawdown = (current - avg_price) / avg_price * 100

        # 峰值价（从最后一次买入到现在）
        start_date = p.get("last_buy_date") or "1970-01-01"
        peak_row = query_one(
            "SELECT MAX(high) as peak FROM daily_quotes "
            "WHERE stock_code=? AND trade_date >= ?", (code, start_date)
        )
        peak = (peak_row or {}).get("peak") or avg_price
        peak_drawdown = (current - peak) / peak * 100 if peak else 0

        rule = None
        action = None
        if drawdown <= HARD_STOP_LOSS_PCT:
            rule = "hard_stop_8pct"
            action = f"次日开盘清仓 {qty} 股（浮亏 {drawdown:+.2f}%，跌破 -8% 硬止损）"
        elif peak_drawdown <= TRAILING_STOP_PCT:
            rule = "trailing_12pct"
            action = f"次日开盘卖 {qty // 2} 股（从峰值回撤 {peak_drawdown:+.2f}%，触发跟踪止损）"
        elif drawdown <= SOFT_COGNITION_WARNING_PCT:
            rule = "soft_cognition_5pct"
            action = f"触发交易认知软警戒：复核逻辑并考虑降低风险暴露（浮亏 {drawdown:+.2f}%）"

        if rule:
            # 去重：同一只股 + 同规则 1 天内不重复
            existing = query_one(
                "SELECT id FROM stop_loss_triggers WHERE stock_code=? AND rule=? "
                "AND date(triggered_at) = date('now')", (code, rule)
            )
            if existing:
                continue
            tid = execute(
                "INSERT INTO stop_loss_triggers"
                "(stock_code, stock_name, rule, entry_price, current_price, peak_price, "
                " drawdown_pct, suggested_action) VALUES (?,?,?,?,?,?,?,?)",
                (code, p["stock_name"], rule, avg_price, current, peak,
                 drawdown, action),
            )
            triggers.append({
                "id": tid, "stock_code": code, "stock_name": p["stock_name"],
                "rule": rule, "entry_price": round(avg_price, 2),
                "current_price": round(current, 2), "peak_price": round(peak, 2),
                "drawdown_pct": round(drawdown, 2),
                "peak_drawdown_pct": round(peak_drawdown, 2),
                "action": action,
            })
    return triggers


def get_recent_triggers(limit: int = 20) -> list[dict]:
    _ensure_risk_tables()
    return query_all(
        "SELECT * FROM stop_loss_triggers ORDER BY id DESC LIMIT ?", (limit,),
    )


# ========== 3. 延迟 + 滑点模型 ==========
def apply_slippage(signal_price: float, direction: str,
                    slippage_pct: float = SLIPPAGE_PCT) -> float:
    """给信号价加滑点。direction='buy' 向上加，'sell' 向下减。"""
    if direction == "buy":
        return signal_price * (1 + slippage_pct / 100)
    if direction == "sell":
        return signal_price * (1 - slippage_pct / 100)
    return signal_price


def get_delayed_fill_price(code: str, signal_datetime: str,
                              direction: str = "buy",
                              delay_minutes: int = 1) -> Optional[float]:
    """信号时间 + delay_minutes 后的下一根分钟 K 开盘价 + 滑点。

    目前 DB 里只有日线。若没有分钟数据:
    - 若信号在盘中 → 返回当日收盘价 + 滑点（近似）
    - 若信号在收盘后 → 返回次日开盘价 + 滑点
    """
    dt = datetime.strptime(signal_datetime[:19], "%Y-%m-%d %H:%M:%S") if len(signal_datetime) >= 19 else datetime.strptime(signal_datetime[:10], "%Y-%m-%d")
    # 简化: 用信号日次日开盘价
    next_day = (dt + timedelta(days=1)).strftime("%Y-%m-%d")
    row = query_one(
        "SELECT open FROM daily_quotes WHERE stock_code=? AND trade_date >= ? "
        "ORDER BY trade_date LIMIT 1", (code, next_day)
    )
    if row and row.get("open"):
        return apply_slippage(row["open"], direction)
    return None
