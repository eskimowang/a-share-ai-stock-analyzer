"""AI PK simulated portfolio service.

Four AI contestants each start with 1,000,000 CNY and trade A-shares under a
basic realistic paper-trading rule set: cash only, 100-share lots, T+1 sell
availability, commission, stamp duty on sells, and no leverage.
"""
import json
import logging
import math
from datetime import datetime, timedelta

from ..db import db, query_all, query_one

log = logging.getLogger(__name__)

INITIAL_CASH = 1_000_000.0
COMMISSION_RATE = 0.0003
MIN_COMMISSION = 5.0
STAMP_DUTY_RATE = 0.0005
TRANSFER_RATE = 0.00001
LOT_SIZE = 100
LIMIT_BUY_BLOCK_PCT = 9.2
LIMIT_SELL_BLOCK_PCT = -9.2
IMPACT_COST_COEFF = 0.10
OPEN_CLOSE_IMPACT_MULTIPLIER = 2.0
MAX_IMPACT_COST = 0.035
INDEX_FUND_BENCHMARKS = [
    {"code": "510300", "name": "沪深300ETF", "weight": 0.35},
    {"code": "510050", "name": "上证50ETF", "weight": 0.15},
    {"code": "510500", "name": "中证500ETF", "weight": 0.18},
    {"code": "512100", "name": "中证1000ETF", "weight": 0.14},
    {"code": "159915", "name": "创业板ETF", "weight": 0.12},
    {"code": "588000", "name": "科创50ETF", "weight": 0.06},
]
INDEX_BENCHMARK_CODES = [x["code"] for x in INDEX_FUND_BENCHMARKS]

REFEREE_NAME = "RuleKeeper"
REFEREE_DISPLAY_NAME = "裁判"


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


CONTESTANTS = [
    {
        "name": "DeepSeek",
        "display_name": "DeepSeek",
        "strategy_profile": "基本面、估值、研报质量优先；偏向盈利确定性和中金/中信等高质量研报共振。",
        "risk_profile": "均衡",
        "max_positions": 5,
        "max_weight": 0.22,
        "cash_reserve": 0.06,
    },
    {
        "name": "Gemini",
        "display_name": "Gemini",
        "strategy_profile": "成长趋势、资金热度、成交活跃度优先；偏向强势行业里的高弹性品种。",
        "risk_profile": "进攻",
        "max_positions": 6,
        "max_weight": 0.18,
        "cash_reserve": 0.04,
    },
    {
        "name": "Claude",
        "display_name": "Claude",
        "strategy_profile": "风险控制、波动回撤、估值纪律优先；宁可少赚，也要避免大幅回撤。",
        "risk_profile": "防守",
        "max_positions": 4,
        "max_weight": 0.16,
        "cash_reserve": 0.30,
    },
    {
        "name": "Codex",
        "display_name": "Codex",
        "strategy_profile": "政策事件、14类操作手法、多源数据共振优先；偏向有结构化证据的交易机会。",
        "risk_profile": "系统化",
        "max_positions": 7,
        "max_weight": 0.16,
        "cash_reserve": 0.08,
    },
    {
        "name": "IndexETF",
        "display_name": "Index ETF",
        "strategy_profile": "被动指数基金基准；固定跟踪沪深300、上证50、中证500、中证1000、创业板、科创50 ETF组合。",
        "risk_profile": "基准",
        "max_positions": 12,
        "max_weight": 0.12,
        "cash_reserve": 0.02,
    },
    {
        "name": "Contrarian",
        "display_name": "反共识者",
        "strategy_profile": "反共识风险基准；当市场追涨一致时偏向低拥挤、低估值、低波动，专门检验群体性错误。",
        "risk_profile": "反共识",
        "max_positions": 5,
        "max_weight": 0.14,
        "cash_reserve": 0.20,
    },
]


def _json(data) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def _to_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


def _to_code(value) -> str:
    s = str(value or "").strip()
    digits = "".join(ch for ch in s if ch.isdigit())
    return digits[:6] if len(digits) >= 6 else ""


def _ensure_tables():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS ai_pk_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contestant TEXT UNIQUE,
            display_name TEXT,
            strategy_profile TEXT,
            risk_profile TEXT,
            initial_cash REAL DEFAULT 1000000,
            cash REAL DEFAULT 1000000,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS ai_pk_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contestant TEXT,
            stock_code TEXT,
            stock_name TEXT,
            quantity INTEGER DEFAULT 0,
            available_qty INTEGER DEFAULT 0,
            avg_cost REAL DEFAULT 0,
            last_price REAL DEFAULT 0,
            market_value REAL DEFAULT 0,
            unrealized_pnl REAL DEFAULT 0,
            unrealized_pnl_pct REAL DEFAULT 0,
            opened_at TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(contestant, stock_code)
        );
        CREATE TABLE IF NOT EXISTS ai_pk_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contestant TEXT,
            trade_date TEXT,
            stock_code TEXT,
            stock_name TEXT,
            side TEXT,
            price REAL,
            quantity INTEGER,
            gross_amount REAL,
            fee REAL,
            cash_after REAL,
            reason TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS ai_pk_daily_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contestant TEXT,
            trade_date TEXT,
            cash REAL,
            market_value REAL,
            total_equity REAL,
            daily_return REAL,
            total_return REAL,
            rank_no INTEGER,
            positions_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(contestant, trade_date)
        );
        CREATE TABLE IF NOT EXISTS ai_pk_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contestant TEXT,
            trade_date TEXT,
            strategy_text TEXT,
            review_text TEXT,
            target_json TEXT,
            evidence_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(contestant, trade_date)
        );
        CREATE TABLE IF NOT EXISTS ai_pk_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            trade_date TEXT,
            status TEXT,
            contestants INTEGER DEFAULT 0,
            trades_count INTEGER DEFAULT 0,
            summary_json TEXT,
            error_msg TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_ai_pk_snapshots_date
          ON ai_pk_daily_snapshots(trade_date DESC, rank_no);
        CREATE INDEX IF NOT EXISTS idx_ai_pk_trades_date
          ON ai_pk_trades(trade_date DESC, contestant);

        CREATE TABLE IF NOT EXISTS index_fund_quote_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fund_code TEXT,
            fund_name TEXT,
            trade_date TEXT,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL,
            amount REAL,
            change_pct REAL,
            source TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(fund_code, trade_date)
        );
        CREATE INDEX IF NOT EXISTS idx_index_fund_quote_cache_code_date
          ON index_fund_quote_cache(fund_code, trade_date DESC);
        """)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(ai_pk_runs)").fetchall()}
        if "run_type" not in cols:
            c.execute("ALTER TABLE ai_pk_runs ADD COLUMN run_type TEXT DEFAULT 'daily'")
        if "run_key" not in cols:
            c.execute("ALTER TABLE ai_pk_runs ADD COLUMN run_key TEXT")
        if "market_phase" not in cols:
            c.execute("ALTER TABLE ai_pk_runs ADD COLUMN market_phase TEXT")
        trade_cols = {r["name"] for r in c.execute("PRAGMA table_info(ai_pk_trades)").fetchall()}
        if "executed_at" not in trade_cols:
            c.execute("ALTER TABLE ai_pk_trades ADD COLUMN executed_at TEXT")
        if "run_type" not in trade_cols:
            c.execute("ALTER TABLE ai_pk_trades ADD COLUMN run_type TEXT DEFAULT 'daily'")
        if "run_key" not in trade_cols:
            c.execute("ALTER TABLE ai_pk_trades ADD COLUMN run_key TEXT")
        if "market_phase" not in trade_cols:
            c.execute("ALTER TABLE ai_pk_trades ADD COLUMN market_phase TEXT")
        if "referee_status" not in trade_cols:
            c.execute("ALTER TABLE ai_pk_trades ADD COLUMN referee_status TEXT DEFAULT 'pass'")
        if "referee_notes" not in trade_cols:
            c.execute("ALTER TABLE ai_pk_trades ADD COLUMN referee_notes TEXT")
        c.execute("CREATE INDEX IF NOT EXISTS idx_ai_pk_runs_key ON ai_pk_runs(run_key)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_ai_pk_trades_executed ON ai_pk_trades(executed_at DESC)")


def _init_accounts():
    _ensure_tables()
    with db() as c:
        for p in CONTESTANTS:
            c.execute(
                """
                INSERT INTO ai_pk_accounts
                (contestant, display_name, strategy_profile, risk_profile, initial_cash, cash)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(contestant) DO UPDATE SET
                  display_name=excluded.display_name,
                  strategy_profile=excluded.strategy_profile,
                  risk_profile=excluded.risk_profile,
                  updated_at=CURRENT_TIMESTAMP
                """,
                (
                    p["name"], p["display_name"], p["strategy_profile"],
                    p["risk_profile"], INITIAL_CASH, INITIAL_CASH,
                ),
            )


def _latest_trade_date() -> str:
    row = query_one("SELECT MAX(trade_date) AS d FROM daily_quotes")
    return (row or {}).get("d") or datetime.now().strftime("%Y-%m-%d")


def _is_index_fund_code(code: str) -> bool:
    c = _to_code(code)
    return any(x["code"] == c for x in INDEX_FUND_BENCHMARKS) or c.startswith(("510", "512", "588", "159"))


def _index_fund_name(code: str) -> str:
    c = _to_code(code)
    for item in INDEX_FUND_BENCHMARKS:
        if item["code"] == c:
            return item["name"]
    return ""


def _stock_name(code: str) -> str:
    fund_name = _index_fund_name(code)
    if fund_name:
        return fund_name
    for sql, params in [
        ("SELECT name FROM stock_universe WHERE symbol=? OR ts_code LIKE ? LIMIT 1", (code, f"{code}.%")),
        ("SELECT stock_name AS name FROM watchlist WHERE stock_code=? LIMIT 1", (code,)),
        ("SELECT stock_name AS name FROM positions WHERE stock_code=? LIMIT 1", (code,)),
    ]:
        try:
            row = query_one(sql, params)
            if row and row.get("name"):
                return row["name"]
        except Exception:
            pass
    return ""


def _user_holding_codes() -> set[str]:
    """Real portfolio holdings are excluded from the PK candidate pool."""
    try:
        rows = query_all(
            "SELECT DISTINCT stock_code FROM positions WHERE status='holding' AND stock_code IS NOT NULL"
        )
        return {_to_code(r.get("stock_code")) for r in rows if _to_code(r.get("stock_code"))}
    except Exception:
        return set()


def _fund_ts_code(code: str) -> str:
    c = _to_code(code)
    return f"{c}.SH" if c.startswith("5") else f"{c}.SZ"


def _cache_index_fund_quotes(code: str, trade_date: str) -> None:
    c = _to_code(code)
    if not c:
        return
    try:
        from datetime import timedelta
        from ..config import CONFIG
        from ..data_sources.tushare_client import TushareClient

        token = CONFIG.get("data_sources", {}).get("tushare", {}).get("token")
        if not token:
            return
        end = trade_date.replace("-", "")
        start_dt = datetime.strptime(trade_date[:10], "%Y-%m-%d") - timedelta(days=45)
        start = start_dt.strftime("%Y%m%d")
        df = TushareClient(token).get_fund_daily(c, start=start, end=end)
        if df is None or df.empty:
            return
        name = _index_fund_name(c)
        with db() as conn:
            for _, r in df.iterrows():
                conn.execute(
                    """
                    INSERT INTO index_fund_quote_cache
                    (fund_code, fund_name, trade_date, open, high, low, close,
                     volume, amount, change_pct, source)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(fund_code, trade_date) DO UPDATE SET
                      fund_name=excluded.fund_name,
                      open=excluded.open,
                      high=excluded.high,
                      low=excluded.low,
                      close=excluded.close,
                      volume=excluded.volume,
                      amount=excluded.amount,
                      change_pct=excluded.change_pct,
                      source=excluded.source
                    """,
                    (
                        c, name, str(r.get("trade_date")),
                        _to_float(r.get("open")), _to_float(r.get("high")),
                        _to_float(r.get("low")), _to_float(r.get("close")),
                        _to_float(r.get("volume")), _to_float(r.get("amount")),
                        _to_float(r.get("change_pct")), "tushare_fund_daily",
                    ),
                )
    except Exception as e:
        log.warning("Index fund quote fetch failed %s: %s", code, e)


def _index_fund_quote_on_or_before(code: str, trade_date: str) -> dict | None:
    c = _to_code(code)
    if not c:
        return None
    row = query_one(
        """
        SELECT fund_code AS stock_code, fund_name AS stock_name, trade_date,
               open, high, low, close, volume, amount, change_pct, source
        FROM index_fund_quote_cache
        WHERE fund_code=? AND trade_date<=?
        ORDER BY trade_date DESC
        LIMIT 1
        """,
        (c, trade_date),
    )
    if row:
        return row
    _cache_index_fund_quotes(c, trade_date)
    return query_one(
        """
        SELECT fund_code AS stock_code, fund_name AS stock_name, trade_date,
               open, high, low, close, volume, amount, change_pct, source
        FROM index_fund_quote_cache
        WHERE fund_code=? AND trade_date<=?
        ORDER BY trade_date DESC
        LIMIT 1
        """,
        (c, trade_date),
    )


def _price_on_or_before(code: str, trade_date: str) -> dict | None:
    return query_one(
        """
        SELECT trade_date, close, change_pct
        FROM daily_quotes
        WHERE stock_code=? AND trade_date<=?
        ORDER BY trade_date DESC
        LIMIT 1
        """,
        (code, trade_date),
    )


_REALTIME_DS = None


def _get_realtime_ds():
    global _REALTIME_DS
    if _REALTIME_DS is None:
        from ..config import CONFIG
        from ..data_sources import UnifiedDataSource
        _REALTIME_DS = UnifiedDataSource(
            tushare_token=CONFIG.get("data_sources", {}).get("tushare", {}).get("token")
        )
    return _REALTIME_DS


def _realtime_quote(code: str) -> dict:
    try:
        return _get_realtime_ds().get_realtime(code) or {}
    except Exception as e:
        log.debug("AI PK realtime quote fail %s: %s", code, e)
        return {}

def _referee_trade_check(contestant: str, side: str, stock_code: str, price: float,
                         quantity: int, cash_after: float, run_type: str,
                         market_phase: str) -> tuple[str, str]:
    """Rule-keeper audit for every executed PK trade."""
    notes = []
    status = "pass"
    if side not in ("buy", "sell"):
        status = "violation"
        notes.append("方向非法")
    if quantity <= 0 or quantity % LOT_SIZE != 0:
        status = "violation"
        notes.append("数量不是100股整数")
    if price <= 0:
        status = "violation"
        notes.append("成交价无效")
    if side == "buy" and cash_after < -0.01:
        status = "violation"
        notes.append("买入后现金为负")
    if side == "buy" and _to_code(stock_code) in _user_holding_codes():
        status = "review" if status == "pass" else status
        notes.append("买入标的与用户真实持仓重合，需复核独立性")
    if run_type == "intraday" and market_phase not in ("morning", "afternoon"):
        status = "review" if status == "pass" else status
        notes.append("非连续竞价阶段触发")
    if not notes:
        notes.append("通过：100股整数、现金约束、T+1/可用股、独立候选池规则")
    return status, "；".join(notes)


def _referee_dashboard(trades: list[dict]) -> dict:
    counts = {}
    for t in trades or []:
        status = t.get("referee_status") or "pass"
        counts[status] = counts.get(status, 0) + 1
    latest = next((t for t in trades if t.get("operation_datetime") or t.get("executed_at")), None)
    return {
        "name": REFEREE_NAME,
        "display_name": REFEREE_DISPLAY_NAME,
        "role": "只负责规则公平，不参与选股收益排名",
        "status": "active",
        "latest_audit_time": (latest or {}).get("operation_datetime") or "",
        "audit_counts": counts,
        "rules": [
            "每个AI初始100万，现金交易，无杠杆",
            "买卖数量必须为100股整数",
            "遵守T+1和可用股约束",
            "买卖计入佣金，卖出计入印花税",
            "候选池排除用户真实持仓，防止复制用户组合",
            "每笔交易显示本地执行时间、运行类型和裁判结果",
            "涨跌停限制：涨停买不到、跌停卖不出",
            "流动性冲击：成交价按订单金额/日成交额加入冲击成本",
        ],
    }



def _quote_for_rules(code: str, trade_date: str) -> dict:
    if _is_index_fund_code(code):
        fq = _index_fund_quote_on_or_before(code, trade_date)
        if fq:
            return fq
    return query_one(
        """
        SELECT trade_date, open, high, low, close, volume, amount, change_pct
        FROM daily_quotes
        WHERE stock_code=? AND trade_date<=?
        ORDER BY trade_date DESC
        LIMIT 1
        """,
        (code, trade_date),
    ) or {}


def _is_limit_blocked(side: str, code: str, trade_date: str) -> tuple[bool, str]:
    q = _quote_for_rules(code, trade_date)
    chg = _to_float(q.get("change_pct"), 0.0)
    if side == "buy" and chg >= LIMIT_BUY_BLOCK_PCT:
        return True, f"涨停附近({chg:+.2f}%)，模拟规则：买不到"
    if side == "sell" and chg <= LIMIT_SELL_BLOCK_PCT:
        return True, f"跌停附近({chg:+.2f}%)，模拟规则：卖不出"
    return False, ""


def _day_amount_yuan(code: str, trade_date: str) -> float:
    q = _quote_for_rules(code, trade_date)
    amount = _to_float(q.get("amount"))
    if amount <= 0:
        return 0.0
    # Tushare/AkShare 常见日成交额口径为千元；保守转为元。
    return amount * 1000.0


def _impact_adjusted_price(side: str, code: str, base_price: float, quantity: int,
                           trade_date: str, market_phase: str = "") -> tuple[float, float]:
    if base_price <= 0 or quantity <= 0:
        return base_price, 0.0
    day_amount = _day_amount_yuan(code, trade_date)
    gross = base_price * quantity
    if day_amount <= 0:
        impact = 0.001
    else:
        coeff = IMPACT_COST_COEFF
        if market_phase in ("morning", "preopen", "close") or market_phase == "":
            coeff *= OPEN_CLOSE_IMPACT_MULTIPLIER
        impact = min(MAX_IMPACT_COST, max(0.0002, gross / day_amount * coeff))
    price = base_price * (1 + impact if side == "buy" else 1 - impact)
    return round(price, 3), impact


def _benchmark_targets_from_pool(contestant_cfg: dict, pool: list[dict], trade_date: str) -> tuple[list[dict], str, dict]:
    reserve = float(contestant_cfg.get("cash_reserve") or 0.02)
    investable = max(0.0, 1.0 - reserve)
    available = []
    for item in INDEX_FUND_BENCHMARKS:
        code = item["code"]
        q = _index_fund_quote_on_or_before(code, trade_date)
        if q and _to_float(q.get("close")) > 0:
            available.append((item, q))
    total_weight = sum(_to_float(item.get("weight")) for item, _ in available) or 1.0
    targets = []
    for item, q in available:
        code = item["code"]
        weight = investable * _to_float(item.get("weight")) / total_weight
        m20, vol20 = _returns_and_volatility(code, trade_date, 20)
        # For funds, _returns_and_volatility may have no stock daily cache; compute from fund cache if needed.
        if m20 == 0 and vol20 == 0:
            rows = query_all(
                """
                SELECT close FROM index_fund_quote_cache
                WHERE fund_code=? AND trade_date<=?
                ORDER BY trade_date DESC LIMIT 21
                """,
                (code, trade_date),
            )
            closes = [_to_float(r.get("close")) for r in reversed(rows) if _to_float(r.get("close")) > 0]
            if len(closes) >= 2:
                m20 = (closes[-1] - closes[0]) / closes[0] * 100
        targets.append({
            "stock_code": code,
            "stock_name": item["name"],
            "industry": "index_fund",
            "price": _to_float(q.get("close")),
            "score": 1.0,
            "momentum_20d": round(m20, 3),
            "volatility_20d": round(vol20, 3),
            "pe_ttm": None,
            "pb": None,
            "turnover_rate": 0,
            "reason": "固定指数基金组合，作为AI是否有alpha的基准",
            "target_weight": round(weight, 4),
        })
    strategy_text = (
        f"{contestant_cfg['display_name']} 为固定指数基金基准，不参与AI选股。"
        "跟踪沪深300、上证50、中证500、中证1000、创业板、科创50 ETF组合，检验AI是否跑赢被动指数基金。"
    )
    evidence = {
        "fixed_index_funds": INDEX_FUND_BENCHMARKS,
        "available_funds": [x["code"] for x, _ in available],
        "target_count": len(targets),
        "rule": "固定指数基金组合；使用Tushare fund_daily缓存，缺单只基金时按可用基金权重归一",
    }
    return targets, strategy_text, evidence
def _price_for_execution(code: str, trade_date: str, fallback: float = 0.0,
                         realtime: bool = False) -> float:
    if _is_index_fund_code(code):
        fq = _index_fund_quote_on_or_before(code, trade_date)
        if fq and _to_float(fq.get("close")) > 0:
            return _to_float(fq.get("close"))
    if realtime:
        rt = _realtime_quote(code)
        price = _to_float(rt.get("price"))
        if price > 0:
            return price
    px = _price_on_or_before(code, trade_date)
    return _to_float((px or {}).get("close"), fallback)


def _apply_realtime_to_pool(pool: list[dict], max_codes: int = 50) -> list[dict]:
    """Update the most liquid candidates with realtime prices; keep daily fallback."""
    out = []
    for idx, row in enumerate(pool or []):
        item = dict(row)
        if idx < max_codes:
            rt = _realtime_quote(item.get("stock_code"))
            price = _to_float(rt.get("price"))
            if price > 0:
                item["close"] = price
                if rt.get("change_pct") is not None:
                    item["change_pct"] = rt.get("change_pct")
                if rt.get("volume") is not None:
                    item["volume"] = rt.get("volume")
                item["trade_date"] = datetime.now().strftime("%Y-%m-%d")
                item["_realtime_source"] = rt.get("_source") or "realtime"
        out.append(item)
    return out


def _market_phase(now: datetime | None = None) -> str:
    now = now or datetime.now()
    minutes = now.hour * 60 + now.minute
    if 9 * 60 + 30 <= minutes <= 11 * 60 + 30:
        return "morning"
    if 13 * 60 <= minutes <= 14 * 60 + 50:
        return "afternoon"
    if 14 * 60 + 50 < minutes <= 15 * 60:
        return "closing"
    return "closed"


def _is_market_time(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    return _market_phase(now) != "closed"


def _fee(side: str, gross: float) -> float:
    commission = max(MIN_COMMISSION, gross * COMMISSION_RATE)
    transfer = gross * TRANSFER_RATE
    stamp = gross * STAMP_DUTY_RATE if side == "sell" else 0.0
    return round(commission + transfer + stamp, 2)


def _unlock_t1(trade_date: str):
    with db() as c:
        c.execute(
            """
            UPDATE ai_pk_positions
            SET available_qty=quantity
            WHERE quantity>0 AND (opened_at IS NULL OR opened_at < ?)
            """,
            (trade_date,),
        )


def _refresh_position_marks(trade_date: str, realtime: bool = False):
    positions = query_all("SELECT * FROM ai_pk_positions WHERE quantity>0")
    with db() as c:
        for p in positions:
            price = _price_for_execution(
                p["stock_code"], trade_date, _to_float(p.get("last_price")), realtime=realtime
            )
            qty = int(p.get("quantity") or 0)
            avg = _to_float(p.get("avg_cost"))
            mv = qty * price
            pnl = (price - avg) * qty
            pnl_pct = ((price - avg) / avg * 100) if avg else 0.0
            c.execute(
                """
                UPDATE ai_pk_positions
                SET last_price=?, market_value=?, unrealized_pnl=?,
                    unrealized_pnl_pct=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (price, mv, pnl, pnl_pct, p["id"]),
            )


def _account_equity(contestant: str) -> dict:
    account = query_one("SELECT * FROM ai_pk_accounts WHERE contestant=?", (contestant,)) or {}
    rows = query_all("SELECT * FROM ai_pk_positions WHERE contestant=? AND quantity>0", (contestant,))
    market_value = sum(_to_float(r.get("market_value")) for r in rows)
    cash = _to_float(account.get("cash"), INITIAL_CASH)
    return {"cash": cash, "market_value": market_value, "total_equity": cash + market_value}


def _candidate_pool(limit: int = 450) -> list[dict]:
    trade_date = _latest_trade_date()
    excluded = _user_holding_codes()
    try:
        rows = query_all(
            """
            SELECT q.stock_code, COALESCE(u.name, '') AS stock_name,
                   COALESCE(u.industry, '') AS industry,
                   q.trade_date, q.close, q.change_pct, q.volume, q.amount,
                   b.pe_ttm, b.pb, b.turnover_rate, b.total_mv
            FROM daily_quotes q
            LEFT JOIN daily_basic b
              ON b.stock_code=q.stock_code AND b.trade_date=q.trade_date
            LEFT JOIN stock_universe u
              ON u.symbol=q.stock_code OR u.ts_code LIKE q.stock_code || '.%'
            WHERE q.trade_date=?
              AND q.close BETWEEN 1 AND 1500
              AND COALESCE(q.amount, 0) > 1000000
              AND COALESCE(q.change_pct, 0) < 9.2
              AND COALESCE(u.name, '') NOT LIKE '%ST%'
            ORDER BY COALESCE(q.amount, 0) DESC
            LIMIT ?
            """,
            (trade_date, limit),
        )
    except Exception:
        rows = query_all(
            """
            SELECT stock_code, '' AS stock_name, '' AS industry,
                   trade_date, close, change_pct, volume, amount
            FROM daily_quotes
            WHERE trade_date=? AND close BETWEEN 2 AND 300
            ORDER BY COALESCE(amount, 0) DESC
            LIMIT ?
            """,
            (trade_date, limit),
        )
    if excluded:
        rows = [r for r in rows if _to_code(r.get("stock_code")) not in excluded]
    for r in rows:
        if not r.get("stock_name"):
            r["stock_name"] = _stock_name(r["stock_code"])
    return rows


def _returns_and_volatility(code: str, trade_date: str, days: int = 20) -> tuple[float, float]:
    rows = query_all(
        """
        SELECT close
        FROM daily_quotes
        WHERE stock_code=? AND trade_date<=?
        ORDER BY trade_date DESC
        LIMIT ?
        """,
        (code, trade_date, max(2, days + 1)),
    )
    if len(rows) < 2:
        return 0.0, 0.0
    closes = [_to_float(r.get("close")) for r in reversed(rows) if _to_float(r.get("close")) > 0]
    if len(closes) < 2:
        return 0.0, 0.0
    momentum = (closes[-1] - closes[0]) / closes[0] * 100
    rets = []
    for i in range(1, len(closes)):
        if closes[i - 1]:
            rets.append((closes[i] - closes[i - 1]) / closes[i - 1] * 100)
    if not rets:
        return momentum, 0.0
    mean = sum(rets) / len(rets)
    vol = math.sqrt(sum((x - mean) ** 2 for x in rets) / len(rets))
    return momentum, vol


def _research_score(code: str, trade_date: str) -> tuple[float, str]:
    rows = query_all(
        """
        SELECT r.org_name, r.author_name, r.stance, r.weighted_signal_score,
               s.quality_score, s.grade
        FROM research_report_signals r
        LEFT JOIN research_author_stats s
          ON s.org_name=r.org_name AND s.author_name=COALESCE(r.author_name, '')
         AND s.horizon_days=60
        WHERE r.stock_code=? AND r.report_date<=?
        ORDER BY r.report_date DESC
        LIMIT 8
        """,
        (code, trade_date),
    )
    score = 0.0
    notes = []
    for r in rows:
        w = _to_float(r.get("weighted_signal_score"))
        q = _to_float(r.get("quality_score")) / 100.0
        stance = r.get("stance") or "neutral"
        sign = 1.0 if stance == "positive" else (-1.0 if stance == "negative" else 0.2)
        grade_bonus = {"S": 1.0, "A": 0.7, "B": 0.4, "C": 0.1}.get(r.get("grade"), 0.0)
        score += sign * (w + q + grade_bonus)
        if len(notes) < 2 and r.get("org_name"):
            notes.append(f"{r.get('org_name')} {stance} {r.get('grade') or ''}".strip())
    return score, "；".join(notes)


def _playbook_score(code: str, trade_date: str) -> tuple[float, str]:
    cutoff = (datetime.strptime(trade_date[:10], "%Y-%m-%d") - timedelta(days=21)).strftime("%Y-%m-%d")
    rows = query_all(
        """
        SELECT pattern, confidence, narrative
        FROM playbook_detections
        WHERE stock_code=? AND trade_date>=? AND trade_date<=?
        ORDER BY trade_date DESC, confidence DESC
        LIMIT 5
        """,
        (code, cutoff, trade_date),
    )
    score = sum(_to_float(r.get("confidence")) for r in rows) * 2.0
    note = "；".join(f"{r.get('pattern')} {int(_to_float(r.get('confidence'))*100)}%" for r in rows[:2])
    return score, note


def _latest_financial_quality(code: str) -> float:
    row = query_one(
        """
        SELECT roe, gross_margin, net_margin
        FROM financials
        WHERE stock_code=?
        ORDER BY report_period DESC
        LIMIT 1
        """,
        (code,),
    ) or {}
    roe = _to_float(row.get("roe"))
    gross = _to_float(row.get("gross_margin"))
    net = _to_float(row.get("net_margin"))
    score = 0.0
    if roe > 15:
        score += 4
    elif roe > 8:
        score += 2
    elif roe < 3:
        score -= 2
    if gross > 35:
        score += 1.5
    if net > 10:
        score += 1.5
    return score


def _score_candidate(contestant: str, row: dict, trade_date: str) -> dict:
    code = row["stock_code"]
    close = _to_float(row.get("close"))
    chg = _to_float(row.get("change_pct"))
    amount = _to_float(row.get("amount"))
    turnover = _to_float(row.get("turnover_rate"))
    pe = _to_float(row.get("pe_ttm"), 999.0)
    pb = _to_float(row.get("pb"), 99.0)
    m20, vol20 = _returns_and_volatility(code, trade_date, 20)
    research, research_note = _research_score(code, trade_date)
    playbook, playbook_note = _playbook_score(code, trade_date)
    fin = _latest_financial_quality(code)
    liquidity = min(8.0, math.log10(max(amount, 1)) - 6.5)
    valuation = 0.0
    if 0 < pe < 35:
        valuation += (35 - pe) / 10
    if 0 < pb < 4:
        valuation += (4 - pb) / 1.5
    if pe <= 0 or pe > 120:
        valuation -= 2

    if contestant == "DeepSeek":
        score = valuation * 2.2 + fin * 1.6 + research * 1.7 + m20 * 0.08 + liquidity
        style = "基本面/研报质量"
    elif contestant == "Gemini":
        score = m20 * 0.42 + chg * 0.25 + turnover * 0.18 + liquidity * 2.0 + research * 0.5
        style = "成长趋势/资金热度"
    elif contestant == "Claude":
        score = valuation * 1.4 + fin * 1.0 - vol20 * 1.8 + max(0, m20) * 0.1 + research * 0.5
        if chg > 6:
            score -= 3
        style = "风险控制/低波动"
    elif contestant == "Contrarian":
        crowding_penalty = max(0.0, m20) * 0.28 + max(0.0, chg) * 0.7 + max(0.0, turnover - 4) * 0.6
        score = valuation * 1.7 + fin * 0.8 + liquidity * 0.8 - vol20 * 0.8 - crowding_penalty - research * 0.15
        if chg < -2 and vol20 < 4:
            score += 1.5
        style = "反共识/低拥挤"
    else:
        score = playbook * 2.2 + research * 0.9 + m20 * 0.16 + liquidity + turnover * 0.08
        style = "政策事件/14手法"

    reason_parts = [
        f"{style}得分 {score:.1f}",
        f"20日动量 {m20:.1f}%",
        f"波动 {vol20:.1f}",
    ]
    if research_note:
        reason_parts.append(f"研报 {research_note}")
    if playbook_note:
        reason_parts.append(f"手法 {playbook_note}")
    return {
        "stock_code": code,
        "stock_name": row.get("stock_name") or _stock_name(code),
        "industry": row.get("industry") or "",
        "price": close,
        "score": round(score, 3),
        "momentum_20d": round(m20, 3),
        "volatility_20d": round(vol20, 3),
        "pe_ttm": pe if pe != 999.0 else None,
        "pb": pb if pb != 99.0 else None,
        "turnover_rate": turnover,
        "reason": "；".join(reason_parts),
    }


def _build_targets(contestant_cfg: dict, pool: list[dict], trade_date: str) -> tuple[list[dict], str, dict]:
    name = contestant_cfg["name"]
    if name == "IndexETF":
        return _benchmark_targets_from_pool(contestant_cfg, pool, trade_date)
    scored = [_score_candidate(name, r, trade_date) for r in pool if _to_float(r.get("close")) > 0]
    scored = [s for s in scored if s["score"] > -5]
    scored.sort(key=lambda x: x["score"], reverse=True)
    max_positions = int(contestant_cfg["max_positions"])
    reserve = float(contestant_cfg["cash_reserve"])
    max_weight = float(contestant_cfg["max_weight"])
    top = scored[:max_positions]
    total_score = sum(max(1.0, s["score"]) for s in top) or 1.0
    investable = max(0.0, 1.0 - reserve)
    targets = []
    for s in top:
        raw = investable * max(1.0, s["score"]) / total_score
        weight = min(max_weight, max(0.06, raw))
        t = dict(s)
        t["target_weight"] = round(weight, 4)
        targets.append(t)
    used = sum(t["target_weight"] for t in targets)
    if used > investable and used > 0:
        scale = investable / used
        for t in targets:
            t["target_weight"] = round(t["target_weight"] * scale, 4)
    strategy_text = (
        f"{contestant_cfg['display_name']} 今日采用“{contestant_cfg['strategy_profile']}” "
        f"策略，目标持仓 {len(targets)} 只，现金保留约 {reserve*100:.0f}%。"
        "交易认知叠加：首次不满仓、保留预备队、证据不足时等待。"
        "候选池已排除你的真实持仓，避免复制用户组合。"
    )
    evidence = {
        "candidate_count": len(pool),
        "scored_count": len(scored),
        "top_candidates": scored[:10],
        "excluded_user_holdings": sorted(_user_holding_codes()),
        "rule": "A股100股整数、现金约束、T+1、无杠杆、卖出收印花税",
        "trading_cognition_overlay": "仓位管理大于选股；买入前三问：为什么涨、谁在买、还能涨吗；止损快、止盈慢；看不懂等待。",
        "analysis_architecture_overlay": "多源数据交叉验证；研报先看质量；四AI独立分工；组合经理/风控闸门收束；不复制用户持仓。",
    }
    return targets, strategy_text, evidence


def _sell(contestant: str, pos: dict, price: float, quantity: int, trade_date: str, reason: str,
          run_type: str = 'daily', run_key: str = '', market_phase: str = '') -> int:
    quantity = max(0, int(quantity // LOT_SIZE * LOT_SIZE))
    quantity = min(quantity, int(pos.get("available_qty") or 0), int(pos.get("quantity") or 0))
    if quantity < LOT_SIZE or price <= 0:
        return 0
    blocked, block_reason = _is_limit_blocked("sell", pos["stock_code"], trade_date)
    if blocked:
        log.info("AI PK sell blocked by limit rule: %s %s %s", contestant, pos["stock_code"], block_reason)
        return 0
    price, impact = _impact_adjusted_price("sell", pos["stock_code"], price, quantity, trade_date, market_phase)
    if impact:
        reason = f"{reason}；流动性冲击成本{impact*100:.2f}%"
    gross = price * quantity
    fee = _fee("sell", gross)
    cash_delta = gross - fee
    new_qty = int(pos.get("quantity") or 0) - quantity
    account = query_one("SELECT cash FROM ai_pk_accounts WHERE contestant=?", (contestant,)) or {}
    cash_after = _to_float(account.get("cash")) + cash_delta
    with db() as c:
        c.execute(
            "UPDATE ai_pk_accounts SET cash=?, updated_at=CURRENT_TIMESTAMP WHERE contestant=?",
            (cash_after, contestant),
        )
        if new_qty <= 0:
            c.execute("DELETE FROM ai_pk_positions WHERE id=?", (pos["id"],))
        else:
            c.execute(
                """
                UPDATE ai_pk_positions
                SET quantity=?, available_qty=?, last_price=?, market_value=?,
                    unrealized_pnl=?, unrealized_pnl_pct=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (
                    new_qty,
                    max(0, int(pos.get("available_qty") or 0) - quantity),
                    price,
                    price * new_qty,
                    (price - _to_float(pos.get("avg_cost"))) * new_qty,
                    ((price - _to_float(pos.get("avg_cost"))) / _to_float(pos.get("avg_cost")) * 100)
                    if _to_float(pos.get("avg_cost")) else 0,
                    pos["id"],
                ),
            )
        c.execute(
            """
            INSERT INTO ai_pk_trades
            (contestant, trade_date, stock_code, stock_name, side, price, quantity,
             gross_amount, fee, cash_after, reason, executed_at, run_type, run_key,
             market_phase, referee_status, referee_notes)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                contestant, trade_date, pos["stock_code"], pos.get("stock_name") or "",
                "sell", price, quantity, gross, fee, cash_after, reason[:500],
                _now_text(), run_type, run_key, market_phase,
                *_referee_trade_check(contestant, "sell", pos["stock_code"], price, quantity,
                                      cash_after, run_type, market_phase),
            ),
        )
    return 1


def _buy(contestant: str, target: dict, amount: float, trade_date: str, reason: str,
         run_type: str = 'daily', run_key: str = '', market_phase: str = '') -> int:
    if target["stock_code"] in _user_holding_codes():
        log.info("AI PK referee blocked user-holding copy: %s %s", contestant, target["stock_code"])
        return 0
    price = _to_float(target.get("price"))
    if price <= 0 or amount <= price * LOT_SIZE:
        return 0
    blocked, block_reason = _is_limit_blocked("buy", target["stock_code"], trade_date)
    if blocked:
        log.info("AI PK buy blocked by limit rule: %s %s %s", contestant, target["stock_code"], block_reason)
        return 0
    account = query_one("SELECT cash FROM ai_pk_accounts WHERE contestant=?", (contestant,)) or {}
    cash = _to_float(account.get("cash"))
    spend = min(amount, cash)
    base_price = price
    qty = int((spend / (base_price * (1 + COMMISSION_RATE))) // LOT_SIZE * LOT_SIZE)
    if qty < LOT_SIZE:
        return 0
    price, impact = _impact_adjusted_price("buy", target["stock_code"], base_price, qty, trade_date, market_phase)
    if impact:
        reason = f"{reason}；流动性冲击成本{impact*100:.2f}%"
    gross = price * qty
    fee = _fee("buy", gross)
    total_cost = gross + fee
    while qty >= LOT_SIZE and total_cost > cash:
        qty -= LOT_SIZE
        price, impact = _impact_adjusted_price("buy", target["stock_code"], base_price, qty, trade_date, market_phase)
        gross = price * qty
        fee = _fee("buy", gross)
        total_cost = gross + fee
    if qty < LOT_SIZE:
        return 0

    pos = query_one(
        "SELECT * FROM ai_pk_positions WHERE contestant=? AND stock_code=?",
        (contestant, target["stock_code"]),
    )
    cash_after = cash - total_cost
    with db() as c:
        c.execute(
            "UPDATE ai_pk_accounts SET cash=?, updated_at=CURRENT_TIMESTAMP WHERE contestant=?",
            (cash_after, contestant),
        )
        if pos:
            old_qty = int(pos.get("quantity") or 0)
            old_cost = _to_float(pos.get("avg_cost")) * old_qty
            new_qty = old_qty + qty
            avg = (old_cost + total_cost) / new_qty if new_qty else price
            c.execute(
                """
                UPDATE ai_pk_positions
                SET stock_name=?, quantity=?, avg_cost=?, last_price=?, market_value=?,
                    unrealized_pnl=?, unrealized_pnl_pct=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (
                    target.get("stock_name") or "",
                    new_qty, avg, price, new_qty * price,
                    (price - avg) * new_qty,
                    ((price - avg) / avg * 100) if avg else 0,
                    pos["id"],
                ),
            )
        else:
            c.execute(
                """
                INSERT INTO ai_pk_positions
                (contestant, stock_code, stock_name, quantity, available_qty, avg_cost,
                 last_price, market_value, unrealized_pnl, unrealized_pnl_pct, opened_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    contestant, target["stock_code"], target.get("stock_name") or "",
                    qty, 0, total_cost / qty, price, qty * price,
                    price * qty - total_cost,
                    ((price * qty - total_cost) / total_cost * 100) if total_cost else 0,
                    trade_date,
                ),
            )
        c.execute(
            """
            INSERT INTO ai_pk_trades
            (contestant, trade_date, stock_code, stock_name, side, price, quantity,
             gross_amount, fee, cash_after, reason, executed_at, run_type, run_key,
             market_phase, referee_status, referee_notes)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                contestant, trade_date, target["stock_code"], target.get("stock_name") or "",
                "buy", price, qty, gross, fee, cash_after, reason[:500],
                _now_text(), run_type, run_key, market_phase,
                *_referee_trade_check(contestant, "buy", target["stock_code"], price, qty,
                                      cash_after, run_type, market_phase),
            ),
        )
    return 1


def _rebalance_one(contestant_cfg: dict, pool: list[dict], trade_date: str,
                   realtime: bool = False, run_type: str = 'daily',
                   run_key: str = '', market_phase: str = '') -> dict:
    contestant = contestant_cfg["name"]
    targets, strategy_text, evidence = _build_targets(contestant_cfg, pool, trade_date)
    target_by_code = {t["stock_code"]: t for t in targets}
    positions = query_all("SELECT * FROM ai_pk_positions WHERE contestant=? AND quantity>0", (contestant,))
    equity_before = _account_equity(contestant)["total_equity"]
    trades = 0
    actions = []

    # Sell first to free cash and obey T+1 availability.
    for p in positions:
        price = _price_for_execution(
            p["stock_code"], trade_date, _to_float(p.get("last_price")), realtime=realtime
        )
        cur_value = price * int(p.get("quantity") or 0)
        target = target_by_code.get(p["stock_code"])
        desired = equity_before * _to_float((target or {}).get("target_weight"))
        stop_loss = _to_float(p.get("unrealized_pnl_pct")) <= -8.0
        if not target:
            sell_value = cur_value
            reason = "不在今日目标池，退出持仓"
        elif stop_loss:
            sell_value = cur_value * 0.5
            reason = "触发模拟止损，降低风险暴露"
        elif cur_value > desired * 1.12:
            sell_value = cur_value - desired
            reason = "超过目标仓位，卖出再平衡"
        else:
            continue
        qty = int((sell_value / price) // LOT_SIZE * LOT_SIZE)
        done = _sell(contestant, p, price, qty, trade_date, reason, run_type, run_key, market_phase)
        trades += done
        if done:
            actions.append({"side": "sell", "stock_code": p["stock_code"], "stock_name": p.get("stock_name"), "reason": reason})

    _refresh_position_marks(trade_date)
    equity_mid = _account_equity(contestant)
    positions_now = {
        p["stock_code"]: p for p in query_all(
            "SELECT * FROM ai_pk_positions WHERE contestant=? AND quantity>0",
            (contestant,),
        )
    }
    reserve_cash = equity_mid["total_equity"] * _to_float(contestant_cfg.get("cash_reserve"))

    for t in targets:
        pos = positions_now.get(t["stock_code"])
        current_value = _to_float((pos or {}).get("market_value"))
        desired = equity_mid["total_equity"] * _to_float(t["target_weight"])
        buy_value = desired - current_value
        cash_now = _account_equity(contestant)["cash"]
        if buy_value <= max(10000, desired * 0.08) or cash_now <= reserve_cash + 15000:
            continue
        amount = min(buy_value, cash_now - reserve_cash)
        done = _buy(contestant, t, amount, trade_date, t["reason"], run_type, run_key, market_phase)
        trades += done
        if done:
            actions.append({"side": "buy", "stock_code": t["stock_code"], "stock_name": t.get("stock_name"), "reason": t["reason"]})

    review = _make_review(contestant_cfg, targets, actions)
    with db() as c:
        c.execute(
            """
            INSERT INTO ai_pk_decisions
            (contestant, trade_date, strategy_text, review_text, target_json, evidence_json)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(contestant, trade_date) DO UPDATE SET
              strategy_text=excluded.strategy_text,
              review_text=excluded.review_text,
              target_json=excluded.target_json,
              evidence_json=excluded.evidence_json,
              created_at=CURRENT_TIMESTAMP
            """,
            (contestant, trade_date, strategy_text, review, _json(targets), _json(evidence)),
        )
    return {"contestant": contestant, "targets": len(targets), "trades": trades, "actions": actions}


def _make_review(contestant_cfg: dict, targets: list[dict], actions: list[dict]) -> str:
    top = targets[:3]
    top_txt = "；".join(f"{t['stock_code']} {t.get('stock_name') or ''} {t['target_weight']*100:.0f}%" for t in top)
    action_count = len(actions)
    if action_count:
        action_txt = "；".join(f"{a['side']} {a['stock_code']} {a.get('stock_name') or ''}" for a in actions[:5])
    else:
        action_txt = "无成交，维持观察"
    return (
        f"{contestant_cfg['display_name']} 复盘：今日按{contestant_cfg['risk_profile']}风格筛选，"
        f"目标组合为 {top_txt or '空仓观察'}。实际动作：{action_txt}。"
        f"后续重点观察目标股是否继续满足策略证据，若跌破纪律线则降低仓位。"
    )


def _snapshot_positions(contestant: str) -> list[dict]:
    rows = query_all(
        """
        SELECT stock_code, stock_name, quantity, available_qty, avg_cost, last_price,
               market_value, unrealized_pnl, unrealized_pnl_pct
        FROM ai_pk_positions
        WHERE contestant=? AND quantity>0
        ORDER BY market_value DESC
        """,
        (contestant,),
    )
    return rows


def _save_snapshots(trade_date: str):
    rows = []
    for p in CONTESTANTS:
        name = p["name"]
        equity = _account_equity(name)
        prev = query_one(
            """
            SELECT total_equity
            FROM ai_pk_daily_snapshots
            WHERE contestant=? AND trade_date<?
            ORDER BY trade_date DESC
            LIMIT 1
            """,
            (name, trade_date),
        )
        prev_equity = _to_float((prev or {}).get("total_equity"), INITIAL_CASH)
        total_equity = equity["total_equity"]
        daily_return = (total_equity - prev_equity) / prev_equity if prev_equity else 0.0
        total_return = (total_equity - INITIAL_CASH) / INITIAL_CASH
        rows.append({
            "contestant": name,
            "cash": equity["cash"],
            "market_value": equity["market_value"],
            "total_equity": total_equity,
            "daily_return": daily_return,
            "total_return": total_return,
            "positions": _snapshot_positions(name),
        })
    rows.sort(key=lambda x: x["total_equity"], reverse=True)
    with db() as c:
        for idx, r in enumerate(rows, start=1):
            c.execute(
                """
                INSERT INTO ai_pk_daily_snapshots
                (contestant, trade_date, cash, market_value, total_equity,
                 daily_return, total_return, rank_no, positions_json)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(contestant, trade_date) DO UPDATE SET
                  cash=excluded.cash,
                  market_value=excluded.market_value,
                  total_equity=excluded.total_equity,
                  daily_return=excluded.daily_return,
                  total_return=excluded.total_return,
                  rank_no=excluded.rank_no,
                  positions_json=excluded.positions_json,
                  created_at=CURRENT_TIMESTAMP
                """,
                (
                    r["contestant"], trade_date, r["cash"], r["market_value"],
                    r["total_equity"], r["daily_return"], r["total_return"],
                    idx, _json(r["positions"]),
                ),
            )


def run_ai_pk_daily(force: bool = False) -> dict:
    """Run one daily PK rebalance if today's snapshot does not exist."""
    _init_accounts()
    trade_date = _latest_trade_date()
    existing = query_one(
        "SELECT COUNT(*) AS n FROM ai_pk_daily_snapshots WHERE trade_date=?",
        (trade_date,),
    )
    if (existing or {}).get("n") and not force:
        _unlock_t1(trade_date)
        _refresh_position_marks(trade_date)
        _save_snapshots(trade_date)
        return {"status": "skipped", "trade_date": trade_date, "reason": "今日已生成PK结果", "dashboard": get_ai_pk_dashboard()}

    status = "success"
    error = ""
    result = {"trade_date": trade_date, "contestants": [], "trades_count": 0}
    try:
        _unlock_t1(trade_date)
        _refresh_position_marks(trade_date)
        pool = _candidate_pool()
        for cfg in CONTESTANTS:
            one = _rebalance_one(cfg, pool, trade_date, run_type='daily', run_key=f'daily:{trade_date}', market_phase='close')
            result["contestants"].append(one)
            result["trades_count"] += one["trades"]
        _refresh_position_marks(trade_date)
        _save_snapshots(trade_date)
    except Exception as e:
        status = "failed"
        error = str(e)
        log.exception("AI PK daily run failed: %s", e)
    with db() as c:
        c.execute(
            """
            INSERT INTO ai_pk_runs(status, trade_date, contestants, trades_count, summary_json, error_msg)
            VALUES (?,?,?,?,?,?)
            """,
            (status, trade_date, len(CONTESTANTS), result["trades_count"], _json(result), error[:1000]),
        )
    result["status"] = status
    if error:
        result["error"] = error
    result["dashboard"] = get_ai_pk_dashboard()
    return result


def run_ai_pk_intraday(force: bool = False, source: str = "scheduler") -> dict:
    """Run a realtime intraday paper-trading round during A-share sessions."""
    _init_accounts()
    now = datetime.now()
    phase = _market_phase(now)
    trade_date = now.strftime("%Y-%m-%d")
    run_bucket = now.strftime("%H%M")
    run_key = f"intraday:{trade_date}:{phase}:{run_bucket}"

    if not force and not _is_market_time(now):
        market_date = _latest_trade_date()
        _unlock_t1(market_date)
        _refresh_position_marks(market_date, realtime=True)
        _save_snapshots(market_date)
        return {
            "status": "skipped",
            "trade_date": market_date,
            "run_type": "intraday",
            "market_phase": phase,
            "reason": "非A股连续竞价时段，未执行盘中交易",
            "dashboard": get_ai_pk_dashboard(),
        }

    existing = query_one(
        "SELECT id FROM ai_pk_runs WHERE run_key=? AND status IN ('success','running') LIMIT 1",
        (run_key,),
    )
    if existing and not force:
        _refresh_position_marks(trade_date, realtime=True)
        _save_snapshots(trade_date)
        return {
            "status": "skipped",
            "trade_date": trade_date,
            "run_type": "intraday",
            "market_phase": phase,
            "reason": "本盘中时段已运行",
            "dashboard": get_ai_pk_dashboard(),
        }

    status = "success"
    error = ""
    result = {
        "trade_date": trade_date,
        "run_type": "intraday",
        "market_phase": phase,
        "source": source,
        "contestants": [],
        "trades_count": 0,
    }
    try:
        _unlock_t1(trade_date)
        _refresh_position_marks(trade_date, realtime=True)
        pool = _apply_realtime_to_pool(_candidate_pool(limit=140), max_codes=10)
        realtime_pool = [item for item in pool if item.get("_realtime_source")]
        result["realtime_coverage"] = {
            "checked": min(10, len(pool)),
            "usable": len(realtime_pool),
            "required_min": 5,
            "rule": "盘中PK只允许使用当日实时价；覆盖不足则跳过，不能用日线价冒充盘中成交。",
        }
        if len(realtime_pool) < 5:
            status = "skipped"
            result["reason"] = "实时行情覆盖不足，已跳过盘中调仓，避免用日线价模拟成交"
        else:
            for cfg in CONTESTANTS:
                one = _rebalance_one(cfg, realtime_pool, trade_date, realtime=True, run_type='intraday', run_key=run_key, market_phase=phase)
                result["contestants"].append(one)
                result["trades_count"] += one["trades"]
        _refresh_position_marks(trade_date, realtime=True)
        _save_snapshots(trade_date)
    except Exception as e:
        status = "failed"
        error = str(e)
        log.exception("AI PK intraday run failed: %s", e)
    with db() as c:
        c.execute(
            """
            INSERT INTO ai_pk_runs
            (run_type, run_key, market_phase, status, trade_date, contestants,
             trades_count, summary_json, error_msg)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                "intraday", run_key, phase, status, trade_date, len(CONTESTANTS),
                result["trades_count"], _json(result), error[:1000],
            ),
        )
    result["status"] = status
    if error:
        result["error"] = error
    result["dashboard"] = get_ai_pk_dashboard()
    return result


def _normalize_trade_for_dashboard(row: dict) -> dict:
    item = dict(row)
    dt = item.get("executed_at") or item.get("created_at") or ""
    item["operation_datetime"] = dt
    item["operation_time"] = str(dt)[11:19] if len(str(dt)) >= 19 else str(dt)
    item["run_type"] = item.get("run_type") or "daily"
    phase = item.get("market_phase") or ""
    item["market_phase_display"] = {
        "morning": "早盘",
        "afternoon": "午后",
        "close": "收盘",
        "closed": "非交易",
        "preopen": "开盘前",
        "lunch": "午休",
    }.get(phase, phase or "日度")
    item["referee_status"] = item.get("referee_status") or "pass"
    item["referee_notes"] = item.get("referee_notes") or "历史成交按旧记录补显示时间；新成交将逐笔裁判审核"
    item["referee_status_display"] = {
        "pass": "通过",
        "review": "复核",
        "violation": "违规",
    }.get(item["referee_status"], item["referee_status"])
    return item


def get_ai_pk_dashboard() -> dict:
    _init_accounts()
    trade_date = _latest_trade_date()
    _refresh_position_marks(trade_date)
    latest_date_row = query_one("SELECT MAX(trade_date) AS d FROM ai_pk_daily_snapshots")
    latest_snapshot_date = (latest_date_row or {}).get("d")
    if not latest_snapshot_date:
        _save_snapshots(trade_date)
        latest_snapshot_date = trade_date
    else:
        existing_names = {
            r.get("contestant") for r in query_all(
                "SELECT DISTINCT contestant FROM ai_pk_daily_snapshots WHERE trade_date=?",
                (latest_snapshot_date,),
            )
        }
        missing_contestants = {p["name"] for p in CONTESTANTS} - existing_names
        if missing_contestants:
            _save_snapshots(latest_snapshot_date)
    snapshots = query_all(
        """
        SELECT *
        FROM ai_pk_daily_snapshots
        WHERE trade_date=?
        ORDER BY rank_no ASC
        """,
        (latest_snapshot_date,),
    )
    accounts = {a["contestant"]: a for a in query_all("SELECT * FROM ai_pk_accounts")}
    decisions = {
        d["contestant"]: d for d in query_all(
            "SELECT * FROM ai_pk_decisions WHERE trade_date=?",
            (latest_snapshot_date,),
        )
    }
    contestants = []
    for s in snapshots:
        name = s["contestant"]
        positions = []
        try:
            positions = json.loads(s.get("positions_json") or "[]")
        except Exception:
            positions = []
        d = decisions.get(name) or {}
        targets = []
        try:
            targets = json.loads(d.get("target_json") or "[]")
        except Exception:
            targets = []
        contestants.append({
            "contestant": name,
            "display_name": (accounts.get(name) or {}).get("display_name") or name,
            "strategy_profile": (accounts.get(name) or {}).get("strategy_profile") or "",
            "risk_profile": (accounts.get(name) or {}).get("risk_profile") or "",
            "rank_no": s.get("rank_no"),
            "cash": s.get("cash"),
            "market_value": s.get("market_value"),
            "total_equity": s.get("total_equity"),
            "daily_return": s.get("daily_return"),
            "total_return": s.get("total_return"),
            "positions": positions,
            "decision": {
                "strategy_text": d.get("strategy_text") or "",
                "review_text": d.get("review_text") or "",
                "targets": targets[:10],
            },
        })
    history = {}
    for p in CONTESTANTS:
        rows = query_all(
            """
            SELECT trade_date, total_equity, daily_return, total_return, rank_no
            FROM ai_pk_daily_snapshots
            WHERE contestant=?
            ORDER BY trade_date ASC
            LIMIT 240
            """,
            (p["name"],),
        )
        history[p["name"]] = rows
    trades = [
        _normalize_trade_for_dashboard(t) for t in query_all(
            """
            SELECT *
            FROM ai_pk_trades
            ORDER BY COALESCE(executed_at, created_at) DESC, id DESC
            LIMIT 120
            """
        )
    ]
    latest_run = query_one("SELECT * FROM ai_pk_runs ORDER BY id DESC LIMIT 1")
    return {
        "success": True,
        "trade_date": latest_snapshot_date,
        "latest_market_date": trade_date,
        "initial_cash": INITIAL_CASH,
        "rules": {
            "capital_per_ai": INITIAL_CASH,
            "lot_size": LOT_SIZE,
            "commission_rate": COMMISSION_RATE,
            "min_commission": MIN_COMMISSION,
            "stamp_duty_sell": STAMP_DUTY_RATE,
            "t_plus_one": True,
            "exclude_user_holdings": True,
            "leverage": "none",
            "intraday_enabled": True,
            "intraday_schedule": "交易日 09:35-14:55 约每30分钟模拟调仓",
            "index_baseline": "Index ETF 固定指数基金基准：沪深300/50/500/1000/创业板/科创50 ETF组合",
            "contrarian_baseline": "反共识者用于检验AI同源风险",
            "limit_rules": "涨停附近禁止模拟买入，跌停附近禁止模拟卖出",
            "impact_cost_model": "成交价 = 基准价 ± min(3.5%, 订单金额/日成交额×系数)",
            "trading_cognition_overlay": {
                "position": "仓位管理大于选股；首次不开满，保留现金预备队",
                "logic": "买入前三问：为什么涨、谁在买、还能涨吗",
                "risk": "止损快、止盈慢；逻辑失效或破位先处理风险",
                "patience": "机会大于能力，看不懂时等待"
            },
            "analysis_architecture": {
                "data": "多源行情、财务、资金、研报、互动记忆交叉验证",
                "reports": "中金/中信权重更高，作者和团队按历史命中率分级",
                "agents": "DeepSeek/Gemini/Claude/Codex 分工独立，降低同源污染",
                "portfolio_gate": "风险经理和组合约束最终收束交易动作",
                "storage": "保存蒸馏信号和后验表现，减少大文件堆积"
            },
            "referee": {
                "name": REFEREE_NAME,
                "display_name": REFEREE_DISPLAY_NAME,
                "scope": "校验交易时间、100股整数、T+1、现金约束、费用、是否复制用户持仓",
            },
            "open_source_tool_refs": {
                "backtest": "backtrader / Zipline / vectorbt: 事件流水、批量回测、手续费滑点和交易日历",
                "performance": "QuantStats: AI PK需展示收益、回撤、胜率、波动和收益来源",
                "data": "OpenBB / yfinance: 统一数据接口和健康检查思想",
                "a_share": "vn.py / InStock: 国内市场交易约束、风控和A股形态/筹码维度"
            },
        },
        "contestants": contestants,
        "history": history,
        "referee": _referee_dashboard(trades),
        "recent_trades": trades,
        "latest_run": latest_run,
    }
