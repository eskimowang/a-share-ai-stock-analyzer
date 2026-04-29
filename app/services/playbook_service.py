"""Playbook 复盘服务 —— 14 类手法扫描 + 真实收益匹配 + 案例聚合。

使用:
  scan_all_tracked_stocks() 每周末跑一次，扫所有持仓+自选的历史命中
  compute_outcomes() 计算每个历史命中 7d/30d 后的真实收益
  get_pattern_cases(pattern) 拿某类手法的所有历史案例（带胜率）
  get_stock_case_chart(code, date, pattern) 取某次命中的 K 线数据（前推 30 日 + 后推 30 日）
"""
import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from ..db import db, execute, query_all, query_one
from .pattern_detector import detect_all_patterns, scan_stock_history, PATTERNS

log = logging.getLogger(__name__)


def _ensure_playbook_tables():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS playbook_detections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stock_code TEXT,
            stock_name TEXT,
            trade_date DATE,
            pattern TEXT,
            confidence REAL,
            signals TEXT,   -- JSON
            narrative TEXT,
            outcome_7d REAL,
            outcome_30d REAL,
            computed_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(stock_code, trade_date, pattern)
        );
        CREATE INDEX IF NOT EXISTS idx_pb_pattern ON playbook_detections(pattern, trade_date DESC);
        CREATE INDEX IF NOT EXISTS idx_pb_stock ON playbook_detections(stock_code, trade_date DESC);
        """)


def _ensure_market_run_table():
    _ensure_playbook_tables()
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS playbook_market_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            scope TEXT DEFAULT 'market',
            since_days INTEGER,
            start_date DATE,
            end_date DATE,
            stocks_scanned INTEGER DEFAULT 0,
            total_detections INTEGER DEFAULT 0,
            high_confidence_count INTEGER DEFAULT 0,
            status TEXT,
            error_msg TEXT,
            duration_seconds REAL,
            summary_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_pb_market_runs_at ON playbook_market_runs(run_at DESC);
        """)


def _to_symbol(ts_code: str) -> str:
    return str(ts_code or "").split(".")[0]


def _normalize_trade_date(d: str) -> str:
    s = str(d or "")
    if "-" in s:
        return s[:10]
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 else s


def collect_recent_market_data(days: int = 10) -> dict:
    """按交易日批量补全市场最近 N 天日线/估值/资金流，供周复盘使用。"""
    from datetime import datetime, timedelta
    from ..config import CONFIG
    from ..data_sources.tushare_client import TushareClient

    token = CONFIG.get("data_sources", {}).get("tushare", {}).get("token")
    if not token:
        return {"status": "skipped", "reason": "Tushare token missing"}

    _ensure_market_run_table()
    ts = TushareClient(token)
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=max(days + 7, 15))).strftime("%Y%m%d")

    try:
        cal = ts.pro.trade_cal(exchange="", start_date=start, end_date=end, is_open="1")
        # Tushare may return trade_cal in descending order; always select the latest N dates.
        all_dates = sorted({str(x) for x in cal["cal_date"].tolist()})
        trade_dates = all_dates[-days:]
    except Exception:
        trade_dates = []

    daily_rows = basic_rows = moneyflow_rows = 0
    errors = []

    for trade_date in trade_dates:
        try:
            df = ts.pro.daily(trade_date=trade_date)
            if df is not None and not df.empty:
                with db() as c:
                    for _, r in df.iterrows():
                        c.execute(
                            "INSERT OR REPLACE INTO daily_quotes"
                            "(stock_code, trade_date, open, high, low, close, volume, amount, change_pct, data_source) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (
                                _to_symbol(r.get("ts_code")),
                                _normalize_trade_date(r.get("trade_date")),
                                r.get("open"), r.get("high"), r.get("low"), r.get("close"),
                                int(r.get("vol") or 0), r.get("amount"), r.get("pct_chg"),
                                "tushare_market",
                            ),
                        )
                daily_rows += len(df)
        except Exception as e:
            errors.append(f"daily {trade_date}: {str(e)[:100]}")

        try:
            df = ts.pro.daily_basic(
                trade_date=trade_date,
                fields="ts_code,trade_date,close,turnover_rate,pe_ttm,pb,ps_ttm,total_mv,circ_mv",
            )
            if df is not None and not df.empty:
                with db() as c:
                    c.execute("""
                    CREATE TABLE IF NOT EXISTS daily_basic (
                        stock_code TEXT NOT NULL,
                        trade_date DATE NOT NULL,
                        close REAL, pe_ttm REAL, pb REAL, ps_ttm REAL,
                        turnover_rate REAL, total_mv REAL, circ_mv REAL,
                        fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY(stock_code, trade_date)
                    );
                    """)
                    for _, r in df.iterrows():
                        c.execute(
                            "INSERT OR REPLACE INTO daily_basic"
                            "(stock_code, trade_date, close, pe_ttm, pb, ps_ttm, turnover_rate, total_mv, circ_mv) "
                            "VALUES (?,?,?,?,?,?,?,?,?)",
                            (
                                _to_symbol(r.get("ts_code")),
                                _normalize_trade_date(r.get("trade_date")),
                                r.get("close"), r.get("pe_ttm"), r.get("pb"), r.get("ps_ttm"),
                                r.get("turnover_rate"), r.get("total_mv"), r.get("circ_mv"),
                            ),
                        )
                basic_rows += len(df)
        except Exception as e:
            errors.append(f"daily_basic {trade_date}: {str(e)[:100]}")

        try:
            df = ts.pro.moneyflow(trade_date=trade_date)
            if df is not None and not df.empty:
                with db() as c:
                    c.execute("""
                    CREATE TABLE IF NOT EXISTS moneyflow_cache (
                        stock_code TEXT NOT NULL,
                        trade_date DATE NOT NULL,
                        net_mf_vol REAL, net_mf_amount REAL,
                        buy_lg_amount REAL, sell_lg_amount REAL,
                        net_d5_amount REAL,
                        fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY(stock_code, trade_date)
                    );
                    """)
                    for _, r in df.iterrows():
                        c.execute(
                            "INSERT OR REPLACE INTO moneyflow_cache"
                            "(stock_code, trade_date, net_mf_vol, net_mf_amount, buy_lg_amount, sell_lg_amount, net_d5_amount) "
                            "VALUES (?,?,?,?,?,?,?)",
                            (
                                _to_symbol(r.get("ts_code")),
                                _normalize_trade_date(r.get("trade_date")),
                                r.get("net_mf_vol"), r.get("net_mf_amount"),
                                r.get("buy_lg_amount"), r.get("sell_lg_amount"),
                                r.get("net_d5_amount"),
                            ),
                        )
                moneyflow_rows += len(df)
        except Exception as e:
            errors.append(f"moneyflow {trade_date}: {str(e)[:100]}")

    return {
        "status": "success",
        "trade_dates": trade_dates,
        "daily_rows": daily_rows,
        "daily_basic_rows": basic_rows,
        "moneyflow_rows": moneyflow_rows,
        "errors": errors[:20],
    }


def save_detections(detections: list[dict], code: str, name: str = "") -> int:
    _ensure_playbook_tables()
    saved = 0
    for d in detections:
        try:
            execute(
                "INSERT OR IGNORE INTO playbook_detections"
                "(stock_code, stock_name, trade_date, pattern, confidence, signals, narrative) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    code, name,
                    d.get("trade_date"),
                    d.get("pattern"),
                    d.get("confidence"),
                    json.dumps(d.get("signals") or {}, ensure_ascii=False, default=str),
                    d.get("narrative"),
                ),
            )
            saved += 1
        except Exception as e:
            log.warning(f"落库失败 {code} {d.get('pattern')}: {e}")
    return saved


def _price_at_or_after(code: str, date: str) -> Optional[float]:
    r = query_one(
        "SELECT close FROM daily_quotes WHERE stock_code=? AND trade_date >= ? "
        "ORDER BY trade_date LIMIT 1", (code, date),
    )
    return r.get("close") if r else None


def compute_outcomes(limit: int = 500) -> dict:
    """给未验证的 detection 计算 7d/30d 后的真实价格变动。"""
    _ensure_playbook_tables()
    pending = query_all(
        "SELECT id, stock_code, trade_date FROM playbook_detections "
        "WHERE computed_at IS NULL LIMIT ?", (limit,),
    )
    updated = 0
    for p in pending:
        code = p["stock_code"]
        td = p["trade_date"]
        entry = _price_at_or_after(code, td)
        if not entry:
            continue
        d7 = (datetime.strptime(td, "%Y-%m-%d") + timedelta(days=10)).strftime("%Y-%m-%d")  # 近似 7 交易日
        d30 = (datetime.strptime(td, "%Y-%m-%d") + timedelta(days=42)).strftime("%Y-%m-%d")
        p7 = _price_at_or_after(code, d7)
        p30 = _price_at_or_after(code, d30)
        if not p7:
            continue  # 还没到 7 天
        o7 = (p7 - entry) / entry * 100 if entry else None
        o30 = (p30 - entry) / entry * 100 if (p30 and entry) else None
        execute(
            "UPDATE playbook_detections SET outcome_7d=?, outcome_30d=?, "
            "computed_at=CURRENT_TIMESTAMP WHERE id=?",
            (o7, o30, p["id"]),
        )
        updated += 1
    return {"updated": updated, "pending": len(pending)}


def scan_all_tracked_stocks(since_days: int = 180) -> dict:
    """扫持仓+自选的 since_days 天命中。"""
    _ensure_playbook_tables()
    targets = query_all(
        "SELECT DISTINCT stock_code as code, stock_name as name FROM positions WHERE status='holding' "
        "UNION SELECT stock_code as code, stock_name as name FROM watchlist"
    )
    start = (datetime.now() - timedelta(days=since_days)).strftime("%Y-%m-%d")
    end = datetime.now().strftime("%Y-%m-%d")
    total_saved = 0
    by_stock = []
    for t in targets:
        try:
            hits = scan_stock_history(t["code"], start, end)
            saved = save_detections(hits, t["code"], t.get("name") or "")
            total_saved += saved
            by_stock.append({"code": t["code"], "name": t["name"], "hits": len(hits), "saved": saved})
        except Exception as e:
            log.warning(f"扫描 {t['code']} 失败: {e}")
    compute_outcomes(limit=2000)
    return {
        "status": "success",
        "stocks_scanned": len(targets),
        "total_detections": total_saved,
        "by_stock": by_stock,
    }


def get_pattern_cases(pattern: str, limit: int = 50,
                       min_confidence: float = 0.6) -> dict:
    """拿某类手法的所有历史案例 + 聚合胜率（按出货/吸筹不同极性）。"""
    _ensure_playbook_tables()
    rows = query_all(
        "SELECT * FROM playbook_detections WHERE pattern=? AND confidence >= ? "
        "ORDER BY trade_date DESC LIMIT ?",
        (pattern, min_confidence, limit),
    )
    meta = PATTERNS.get(pattern, {})

    # 看是否是看空手法（icon 🔴 => 识别到后预期跌）
    is_bearish = meta.get("icon") == "🔴"
    is_bullish = meta.get("icon") == "🟢"

    stats = {"total": 0, "verified": 0, "aligned_7d": 0, "aligned_30d": 0,
             "avg_7d": 0, "avg_30d": 0}
    verified_rows = [r for r in rows if r.get("computed_at") and r.get("outcome_7d") is not None]
    stats["total"] = len(rows)
    stats["verified"] = len(verified_rows)
    if verified_rows:
        o7s = [r["outcome_7d"] for r in verified_rows if r.get("outcome_7d") is not None]
        o30s = [r["outcome_30d"] for r in verified_rows if r.get("outcome_30d") is not None]
        stats["avg_7d"] = sum(o7s) / len(o7s) if o7s else 0
        stats["avg_30d"] = sum(o30s) / len(o30s) if o30s else 0
        if is_bearish:
            stats["aligned_7d"] = sum(1 for v in o7s if v < 0)
            stats["aligned_30d"] = sum(1 for v in o30s if v < 0)
        elif is_bullish:
            stats["aligned_7d"] = sum(1 for v in o7s if v > 0)
            stats["aligned_30d"] = sum(1 for v in o30s if v > 0)
        else:
            stats["aligned_7d"] = sum(1 for v in o7s if abs(v) < 3)
            stats["aligned_30d"] = sum(1 for v in o30s if abs(v) < 3)
        stats["win_rate_7d"] = stats["aligned_7d"] / len(o7s) if o7s else 0
        stats["win_rate_30d"] = stats["aligned_30d"] / len(o30s) if o30s else 0

    # 解析 signals JSON
    for r in rows:
        if r.get("signals"):
            try:
                r["signals"] = json.loads(r["signals"])
            except Exception:
                pass

    return {
        "pattern": pattern,
        "meta": meta,
        "stats": stats,
        "cases": rows,
    }


def get_stock_case_chart(code: str, date: str, pattern: str) -> dict:
    """拿某次命中的 K 线窗口（前 30 日 + 后 30 日）用于 ECharts。"""
    start = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=45)).strftime("%Y-%m-%d")
    end = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=45)).strftime("%Y-%m-%d")
    kline = query_all(
        "SELECT trade_date, open, high, low, close, volume, change_pct "
        "FROM daily_quotes WHERE stock_code=? AND trade_date >= ? AND trade_date <= ? "
        "ORDER BY trade_date",
        (code, start, end),
    )
    # 资金流同窗口
    mf = query_all(
        "SELECT trade_date, net_mf_amount FROM moneyflow_cache "
        "WHERE stock_code=? AND trade_date >= ? AND trade_date <= ? "
        "ORDER BY trade_date",
        (code, start, end),
    )
    detection = query_one(
        "SELECT * FROM playbook_detections WHERE stock_code=? AND trade_date=? AND pattern=?",
        (code, date, pattern),
    )
    if detection and detection.get("signals"):
        try:
            detection["signals"] = json.loads(detection["signals"])
        except Exception:
            pass
    return {"kline": kline, "moneyflow": mf, "detection": detection, "center_date": date}


def get_stock_all_detections(code: str, since_days: int = 180) -> list[dict]:
    _ensure_playbook_tables()
    cutoff = (datetime.now() - timedelta(days=since_days)).strftime("%Y-%m-%d")
    rows = query_all(
        "SELECT * FROM playbook_detections WHERE stock_code=? AND trade_date >= ? "
        "ORDER BY trade_date DESC LIMIT 500",
        (code, cutoff),
    )
    for r in rows:
        if r.get("signals"):
            try:
                r["signals"] = json.loads(r["signals"])
            except Exception:
                pass
    return rows


def get_patterns_meta() -> dict:
    """14 pattern 的元数据表，供前端渲染定义+key signals。"""
    return PATTERNS



def _market_targets(start_date: str, end_date: str, max_stocks: int | None = None) -> list[dict]:
    sql = """
        SELECT q.stock_code as code,
               COALESCE(u.name, '') as name,
               MAX(q.trade_date) as latest_date,
               COUNT(*) as rows_count
        FROM daily_quotes q
        LEFT JOIN stock_universe u
          ON u.symbol=q.stock_code OR u.ts_code=q.stock_code || CASE WHEN substr(q.stock_code,1,1) IN ('6','9') THEN '.SH' ELSE '.SZ' END
        WHERE q.trade_date >= ? AND q.trade_date <= ?
          AND (u.list_status IS NULL OR u.list_status='L')
          AND (u.is_st IS NULL OR u.is_st=0)
        GROUP BY q.stock_code
        HAVING rows_count >= 1
        ORDER BY q.stock_code
    """
    params = [start_date, end_date]
    if max_stocks:
        sql += " LIMIT ?"
        params.append(max_stocks)
    return query_all(sql, tuple(params))


def scan_market_weekly(since_days: int = 10, max_stocks: int | None = None,
                       refresh_market_data: bool = True) -> dict:
    """每周全市场 14 类手法复盘：补最近一周市场数据，然后扫描命中并落库。"""
    import time
    from collections import Counter

    _ensure_market_run_table()
    t0 = time.time()
    data_refresh = {}
    if refresh_market_data:
        data_refresh = collect_recent_market_data(days=since_days)

    end_row = query_one("SELECT MAX(trade_date) as d FROM daily_quotes")
    end = end_row.get("d") if end_row else datetime.now().strftime("%Y-%m-%d")
    start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=since_days + 3)).strftime("%Y-%m-%d")
    targets = _market_targets(start, end, max_stocks=max_stocks)

    total_saved = 0
    high_conf = 0
    pattern_counter = Counter()
    top_cases = []
    failures = 0

    for t in targets:
        code = t["code"]
        name = t.get("name") or ""
        try:
            hits = scan_stock_history(code, start, end)
            saved = save_detections(hits, code, name)
            total_saved += saved
            for h in hits:
                pattern_counter[h.get("pattern")] += 1
                if (h.get("confidence") or 0) >= 0.75:
                    high_conf += 1
                    top_cases.append({
                        "code": code,
                        "name": name,
                        "date": h.get("trade_date"),
                        "pattern": h.get("pattern"),
                        "confidence": h.get("confidence"),
                        "narrative": h.get("narrative"),
                    })
        except Exception as e:
            failures += 1
            log.warning(f"市场周扫失败 {code}: {e}")

    compute_outcomes(limit=3000)
    top_cases.sort(key=lambda x: x.get("confidence") or 0, reverse=True)
    summary = {
        "patterns": pattern_counter.most_common(14),
        "top_cases": top_cases[:50],
        "data_refresh": data_refresh,
        "failures": failures,
    }
    duration = round(time.time() - t0, 1)
    run_id = execute(
        "INSERT INTO playbook_market_runs"
        "(scope, since_days, start_date, end_date, stocks_scanned, total_detections, "
        " high_confidence_count, status, error_msg, duration_seconds, summary_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "market",
            since_days,
            start,
            end,
            len(targets),
            total_saved,
            high_conf,
            "success",
            "",
            duration,
            json.dumps(summary, ensure_ascii=False, default=str),
        ),
    )
    return {
        "status": "success",
        "run_id": run_id,
        "start_date": start,
        "end_date": end,
        "stocks_scanned": len(targets),
        "total_detections": total_saved,
        "high_confidence_count": high_conf,
        "duration_seconds": duration,
        "summary": summary,
    }


def get_latest_market_weekly_run() -> dict | None:
    _ensure_market_run_table()
    row = query_one("SELECT * FROM playbook_market_runs ORDER BY id DESC LIMIT 1")
    if not row:
        return None
    if row.get("summary_json"):
        try:
            row["summary"] = json.loads(row["summary_json"])
        except Exception:
            row["summary"] = {}
    return row
