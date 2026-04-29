"""互动股票跟踪服务。

把用户在聊天里提到过的股票自动沉淀为一个独立跟踪池，
并在收盘后用价格、14 类手法命中和风险等级做轻量复盘。
"""
import json
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Optional

from ..db import db, query_all, query_one

log = logging.getLogger(__name__)

STOCK_CODE_RE = re.compile(r"(?<!\d)([0-9]{6})(?!\d)")


def _safe_query_all(sql: str, params=()) -> list[dict]:
    try:
        return query_all(sql, params)
    except Exception:
        return []


def _safe_query_one(sql: str, params=()) -> Optional[dict]:
    try:
        return query_one(sql, params)
    except Exception:
        return None


def _table_exists(name: str) -> bool:
    row = _safe_query_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    )
    return bool(row)


def _columns(table: str) -> set[str]:
    try:
        with db() as c:
            return {r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def _ensure_column(table: str, column: str, ddl: str):
    if column in _columns(table):
        return
    with db() as c:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _ensure_interaction_tables():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS interaction_stocks (
            stock_code TEXT PRIMARY KEY,
            stock_name TEXT,
            source TEXT DEFAULT 'chat',
            first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            mention_count INTEGER DEFAULT 1,
            last_session_id TEXT,
            last_message TEXT,
            active INTEGER DEFAULT 1,
            tracking_level TEXT DEFAULT 'auto',
            last_scan_at TIMESTAMP,
            last_trade_date TEXT,
            last_close REAL,
            last_change_pct REAL,
            last_pattern TEXT,
            last_confidence REAL,
            last_risk_level TEXT
        );
        CREATE TABLE IF NOT EXISTS interaction_stock_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stock_code TEXT,
            stock_name TEXT,
            session_id TEXT,
            event_type TEXT DEFAULT 'mention',
            message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS interaction_tracking_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT,
            stocks_scanned INTEGER DEFAULT 0,
            alerts_count INTEGER DEFAULT 0,
            duration_seconds REAL,
            summary_json TEXT,
            error_msg TEXT
        );
        CREATE TABLE IF NOT EXISTS interaction_stock_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stock_code TEXT,
            stock_name TEXT,
            run_id INTEGER,
            trade_date TEXT,
            close REAL,
            change_pct REAL,
            risk_level TEXT,
            top_pattern TEXT,
            top_confidence REAL,
            analysis_text TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(stock_code, trade_date)
        );
        CREATE INDEX IF NOT EXISTS idx_interaction_stocks_active
          ON interaction_stocks(active, last_seen_at DESC);
        CREATE INDEX IF NOT EXISTS idx_interaction_events_code
          ON interaction_stock_events(stock_code, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_interaction_snapshots_code
          ON interaction_stock_snapshots(stock_code, trade_date DESC);
        CREATE INDEX IF NOT EXISTS idx_interaction_snapshots_created
          ON interaction_stock_snapshots(created_at DESC);
        """)

    for col, ddl in {
        "tracking_level": "TEXT DEFAULT 'auto'",
        "last_scan_at": "TIMESTAMP",
        "last_trade_date": "TEXT",
        "last_close": "REAL",
        "last_change_pct": "REAL",
        "last_pattern": "TEXT",
        "last_confidence": "REAL",
        "last_risk_level": "TEXT",
    }.items():
        _ensure_column("interaction_stocks", col, ddl)

    for col, ddl in {
        "run_id": "INTEGER",
        "analysis_text": "TEXT",
    }.items():
        _ensure_column("interaction_stock_snapshots", col, ddl)


def _to_code(value: str | None) -> str:
    if not value:
        return ""
    m = STOCK_CODE_RE.search(str(value))
    return m.group(1) if m else ""


def _lookup_code_name(code: str) -> str:
    for table in ("positions", "watchlist"):
        if _table_exists(table):
            row = _safe_query_one(
                f"SELECT stock_name FROM {table} WHERE stock_code=? AND stock_name IS NOT NULL LIMIT 1",
                (code,),
            )
            if row and row.get("stock_name"):
                return row["stock_name"]

    if _table_exists("stock_universe"):
        cols = _columns("stock_universe")
        if "name" in cols:
            clauses = []
            params = []
            if "symbol" in cols:
                clauses.append("symbol=?")
                params.append(code)
            if "ts_code" in cols:
                suffix = ".SH" if code.startswith(("6", "9")) else ".SZ"
                clauses.append("ts_code=?")
                params.append(f"{code}{suffix}")
            if clauses:
                row = _safe_query_one(
                    f"SELECT name FROM stock_universe WHERE {' OR '.join(clauses)} LIMIT 1",
                    tuple(params),
                )
                if row and row.get("name"):
                    return row["name"]
    return ""


def _known_name_targets() -> list[dict]:
    targets: dict[str, dict] = {}

    for table in ("positions", "watchlist"):
        if _table_exists(table):
            for row in _safe_query_all(
                f"SELECT stock_code as code, stock_name as name FROM {table} "
                "WHERE stock_code IS NOT NULL AND stock_name IS NOT NULL"
            ):
                code = _to_code(row.get("code"))
                name = (row.get("name") or "").strip()
                if code and len(name) >= 2:
                    targets[code] = {"code": code, "name": name}

    if _table_exists("stock_universe"):
        cols = _columns("stock_universe")
        if "name" in cols:
            code_expr = None
            if "symbol" in cols:
                code_expr = "symbol"
            elif "ts_code" in cols:
                code_expr = "substr(ts_code, 1, 6)"
            if code_expr:
                for row in _safe_query_all(
                    f"SELECT {code_expr} as code, name FROM stock_universe "
                    "WHERE name IS NOT NULL LIMIT 8000"
                ):
                    code = _to_code(row.get("code"))
                    name = (row.get("name") or "").strip()
                    if code and len(name) >= 2:
                        targets.setdefault(code, {"code": code, "name": name})

    return list(targets.values())


def extract_stock_mentions(text: str) -> list[dict]:
    """从一句聊天文本里提取 A 股代码或股票简称。"""
    text = text or ""
    found: dict[str, dict] = {}

    for code in STOCK_CODE_RE.findall(text):
        found[code] = {"code": code, "name": _lookup_code_name(code)}

    for item in _known_name_targets():
        name = item.get("name") or ""
        code = item.get("code") or ""
        if code and name and name in text:
            found[code] = {"code": code, "name": name}

    return sorted(found.values(), key=lambda x: x["code"])


def record_interaction_stocks(message: str, session_id: str | None = None,
                              source: str = "chat") -> list[dict]:
    """记录本次聊天提到的股票；无命中时返回空列表。"""
    _ensure_interaction_tables()
    mentions = extract_stock_mentions(message)
    if not mentions:
        return []

    short_message = (message or "")[:500]
    with db() as c:
        for m in mentions:
            code = m["code"]
            name = m.get("name") or _lookup_code_name(code)
            c.execute(
                """
                INSERT INTO interaction_stocks
                (stock_code, stock_name, source, last_session_id, last_message,
                 mention_count, active)
                VALUES (?,?,?,?,?,1,1)
                ON CONFLICT(stock_code) DO UPDATE SET
                  stock_name=COALESCE(NULLIF(excluded.stock_name, ''), interaction_stocks.stock_name),
                  source=excluded.source,
                  last_session_id=excluded.last_session_id,
                  last_message=excluded.last_message,
                  mention_count=interaction_stocks.mention_count + 1,
                  last_seen_at=CURRENT_TIMESTAMP,
                  active=1
                """,
                (code, name, source, session_id, short_message),
            )
            c.execute(
                """
                INSERT INTO interaction_stock_events
                (stock_code, stock_name, session_id, event_type, message)
                VALUES (?,?,?,?,?)
                """,
                (code, name, session_id, "mention", short_message),
            )
    return mentions


def _latest_quote(code: str) -> dict | None:
    return _safe_query_one(
        """
        SELECT trade_date, close, change_pct, volume, amount
        FROM daily_quotes
        WHERE stock_code=?
        ORDER BY trade_date DESC
        LIMIT 1
        """,
        (code,),
    )


def _latest_patterns(code: str, since_days: int = 30) -> list[dict]:
    cutoff = (datetime.now() - timedelta(days=since_days)).strftime("%Y-%m-%d")
    rows = _safe_query_all(
        """
        SELECT trade_date, pattern, confidence, narrative
        FROM playbook_detections
        WHERE stock_code=? AND trade_date >= ?
        ORDER BY trade_date DESC, confidence DESC
        LIMIT 8
        """,
        (code, cutoff),
    )
    return rows


def _latest_snapshot(code: str) -> dict | None:
    _ensure_interaction_tables()
    return _safe_query_one(
        """
        SELECT *
        FROM interaction_stock_snapshots
        WHERE stock_code=?
        ORDER BY trade_date DESC, created_at DESC
        LIMIT 1
        """,
        (code,),
    )


def _analysis_text(item: dict) -> str:
    code = item.get("code") or ""
    name = item.get("name") or ""
    date = item.get("trade_date") or "未知日期"
    close = item.get("close")
    chg = item.get("change_pct")
    risk = item.get("risk") or "normal"
    pattern = item.get("top_pattern")
    conf = item.get("top_confidence")
    narrative = item.get("top_narrative") or ""
    close_s = f"{close:.2f}" if isinstance(close, (int, float)) else "-"
    chg_s = f"{chg:+.2f}%" if isinstance(chg, (int, float)) else "-"

    head = f"{date} {code} {name} 收 {close_s}，涨跌 {chg_s}。"
    if risk == "high":
        conf_s = f"{conf * 100:.0f}%" if isinstance(conf, (int, float)) else "-"
        return f"{head}持续分析判断：高风险，命中 {pattern}（{conf_s}）。{narrative} 后续重点看量能是否继续放大、资金是否转净流出，以及是否跌破近期关键支撑。"
    if risk == "watch":
        conf_s = f"{conf * 100:.0f}%" if isinstance(conf, (int, float)) else "-"
        return f"{head}持续分析判断：观察，出现 {pattern}（{conf_s}）迹象。{narrative} 暂不直接下结论，继续跟踪明后两个交易日的价量和资金方向。"
    if pattern:
        conf_s = f"{conf * 100:.0f}%" if isinstance(conf, (int, float)) else "-"
        return f"{head}持续分析判断：普通跟踪，虽有 {pattern}（{conf_s}）但强度不高。{narrative} 先保留观察。"
    return f"{head}持续分析判断：正常，当前未命中高置信 14 类手法。继续跟踪后续价格、量能、资金流和新一轮交流重点。"


def _save_snapshots(run_id: int, items: list[dict]) -> None:
    if not items:
        return
    with db() as c:
        for item in items:
            analysis = item.get("analysis_text") or _analysis_text(item)
            c.execute(
                """
                INSERT INTO interaction_stock_snapshots
                (stock_code, stock_name, run_id, trade_date, close, change_pct,
                 risk_level, top_pattern, top_confidence, analysis_text)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(stock_code, trade_date) DO UPDATE SET
                  stock_name=excluded.stock_name,
                  run_id=excluded.run_id,
                  close=excluded.close,
                  change_pct=excluded.change_pct,
                  risk_level=excluded.risk_level,
                  top_pattern=excluded.top_pattern,
                  top_confidence=excluded.top_confidence,
                  analysis_text=excluded.analysis_text,
                  created_at=CURRENT_TIMESTAMP
                """,
                (
                    item.get("code"),
                    item.get("name"),
                    run_id,
                    item.get("trade_date"),
                    item.get("close"),
                    item.get("change_pct"),
                    item.get("risk"),
                    item.get("top_pattern"),
                    item.get("top_confidence"),
                    analysis,
                ),
            )


def list_interaction_stocks(active_only: bool = True, limit: int = 200) -> list[dict]:
    _ensure_interaction_tables()
    where = "WHERE active=1" if active_only else ""
    rows = query_all(
        f"""
        SELECT *
        FROM interaction_stocks
        {where}
        ORDER BY last_seen_at DESC, mention_count DESC
        LIMIT ?
        """,
        (limit,),
    )
    for row in rows:
        quote = _latest_quote(row["stock_code"]) or {}
        if quote:
            row["latest_quote"] = quote
        row["recent_patterns"] = _latest_patterns(row["stock_code"])
        row["latest_analysis"] = _latest_snapshot(row["stock_code"])
    return rows


def _risk_from_hits(hits: list[dict]) -> tuple[str, Optional[dict]]:
    if not hits:
        return "normal", None
    from .pattern_detector import PATTERNS

    top = hits[0]
    meta = PATTERNS.get(top.get("pattern"), {})
    icon = meta.get("icon", "")
    conf = float(top.get("confidence") or 0)
    if icon == "🔴" and conf >= 0.75:
        return "high", top
    if conf >= 0.60:
        return "watch", top
    return "normal", top


def run_interaction_tracking(since_days: int = 30, max_stocks: int | None = None,
                             refresh_market_data: bool = False,
                             refresh_days: int = 3) -> dict:
    """对互动股票池做一次轻量复盘。"""
    _ensure_interaction_tables()
    start = time.time()
    summary = {
        "scanned": [],
        "alerts": [],
        "skipped": [],
        "data_refresh": None,
    }

    try:
        if refresh_market_data:
            from .playbook_service import collect_recent_market_data
            summary["data_refresh"] = collect_recent_market_data(days=max(1, min(refresh_days, 10)))

        targets = list_interaction_stocks(active_only=True, limit=max_stocks or 500)
        if not targets:
            run_id = _insert_run("success", 0, 0, time.time() - start, summary)
            return {
                "status": "success",
                "run_id": run_id,
                "stocks_scanned": 0,
                "alerts_count": 0,
                "summary": summary,
                "duration_seconds": time.time() - start,
            }

        from .pattern_detector import detect_all_patterns
        from .playbook_service import save_detections

        for t in targets:
            code = t["stock_code"]
            name = t.get("stock_name") or _lookup_code_name(code)
            quote = _latest_quote(code)
            if not quote or not quote.get("trade_date"):
                summary["skipped"].append({"code": code, "name": name, "reason": "no_quote"})
                continue

            trade_date = quote["trade_date"]
            hits = detect_all_patterns(code, trade_date)
            saved = save_detections(hits, code, name or "")
            risk, top = _risk_from_hits(hits)
            top_pattern = top.get("pattern") if top else None
            top_conf = float(top.get("confidence") or 0) if top else None

            with db() as c:
                c.execute(
                    """
                    UPDATE interaction_stocks
                    SET stock_name=COALESCE(NULLIF(?, ''), stock_name),
                        last_scan_at=CURRENT_TIMESTAMP,
                        last_trade_date=?,
                        last_close=?,
                        last_change_pct=?,
                        last_pattern=?,
                        last_confidence=?,
                        last_risk_level=?
                    WHERE stock_code=?
                    """,
                    (
                        name,
                        trade_date,
                        quote.get("close"),
                        quote.get("change_pct"),
                        top_pattern,
                        top_conf,
                        risk,
                        code,
                    ),
                )

            item = {
                "code": code,
                "name": name,
                "trade_date": trade_date,
                "close": quote.get("close"),
                "change_pct": quote.get("change_pct"),
                "hits": len(hits),
                "saved": saved,
                "risk": risk,
                "top_pattern": top_pattern,
                "top_confidence": top_conf,
                "top_narrative": top.get("narrative") if top else None,
            }
            item["analysis_text"] = _analysis_text(item)
            summary["scanned"].append(item)
            if risk in ("high", "watch"):
                summary["alerts"].append(item)

        summary["alerts"].sort(
            key=lambda x: (x.get("risk") == "high", x.get("top_confidence") or 0),
            reverse=True,
        )
        duration = time.time() - start
        run_id = _insert_run("success", len(summary["scanned"]), len(summary["alerts"]), duration, summary)
        _save_snapshots(run_id, summary["scanned"])
        return {
            "status": "success",
            "run_id": run_id,
            "stocks_scanned": len(summary["scanned"]),
            "alerts_count": len(summary["alerts"]),
            "summary": summary,
            "duration_seconds": duration,
        }
    except Exception as e:
        duration = time.time() - start
        run_id = _insert_run("failed", 0, 0, duration, summary, str(e))
        log.exception("互动股票跟踪失败: %s", e)
        return {"status": "failed", "run_id": run_id, "error": str(e), "duration_seconds": duration}


def _insert_run(status: str, stocks_scanned: int, alerts_count: int,
                duration: float, summary: dict, error_msg: str | None = None) -> int:
    with db() as c:
        cur = c.execute(
            """
            INSERT INTO interaction_tracking_runs
            (status, stocks_scanned, alerts_count, duration_seconds, summary_json, error_msg)
            VALUES (?,?,?,?,?,?)
            """,
            (
                status,
                stocks_scanned,
                alerts_count,
                duration,
                json.dumps(summary, ensure_ascii=False, default=str),
                error_msg,
            ),
        )
        return cur.lastrowid


def get_latest_interaction_tracking_run() -> dict | None:
    _ensure_interaction_tables()
    row = query_one(
        """
        SELECT *
        FROM interaction_tracking_runs
        ORDER BY run_at DESC
        LIMIT 1
        """
    )
    if not row:
        return None
    if row.get("summary_json"):
        try:
            row["summary"] = json.loads(row["summary_json"])
        except Exception:
            row["summary"] = None
    return row

def list_interaction_analysis(stock_code: str | None = None, limit: int = 50) -> list[dict]:
    """返回“我们交流过的股票”的持续分析时间线。"""
    _ensure_interaction_tables()
    limit = max(1, min(int(limit or 50), 300))
    if stock_code:
        return query_all(
            """
            SELECT *
            FROM interaction_stock_snapshots
            WHERE stock_code=?
            ORDER BY trade_date DESC, created_at DESC
            LIMIT ?
            """,
            (stock_code, limit),
        )
    return query_all(
        """
        SELECT *
        FROM interaction_stock_snapshots
        ORDER BY trade_date DESC, created_at DESC
        LIMIT ?
        """,
        (limit,),
    )

