"""多源数据增强服务。

目标：
- 全市场：每日补齐行情/估值/资金流/涨跌停/龙虎榜/指数环境等。
- 重点股：对持仓、自选、互动过的股票做深度画像，汇总财务、筹码、事件、研报等。

说明：不让 AI 直接乱打外部接口，而是先把数据沉淀为稳定缓存，再开放只读工具。
"""
import json
import logging
import math
import re
import time
from datetime import datetime, timedelta
from typing import Optional

from ..config import CONFIG
from ..db import db, query_all, query_one

log = logging.getLogger(__name__)

STOCK_CODE_RE = re.compile(r"(?<!\d)([0-9]{6})(?!\d)")


def _ensure_tables():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS data_enrichment_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            scope TEXT,
            status TEXT,
            target_count INTEGER DEFAULT 0,
            refreshed_count INTEGER DEFAULT 0,
            duration_seconds REAL,
            summary_json TEXT,
            error_msg TEXT
        );
        CREATE TABLE IF NOT EXISTS stock_deep_profiles (
            stock_code TEXT PRIMARY KEY,
            stock_name TEXT,
            refreshed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            latest_trade_date TEXT,
            latest_close REAL,
            data_sources TEXT,
            profile_json TEXT,
            risk_flags_json TEXT
        );
        CREATE TABLE IF NOT EXISTS market_context_cache (
            scope_key TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            refreshed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            payload_json TEXT,
            PRIMARY KEY(scope_key, trade_date)
        );
        CREATE TABLE IF NOT EXISTS source_api_status (
            api TEXT PRIMARY KEY,
            label TEXT,
            last_checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            ok INTEGER,
            row_count INTEGER,
            error TEXT,
            sample_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_stock_deep_profiles_refreshed
          ON stock_deep_profiles(refreshed_at DESC);
        CREATE INDEX IF NOT EXISTS idx_market_context_refreshed
          ON market_context_cache(refreshed_at DESC);
        """)


def _to_code(value) -> str:
    m = STOCK_CODE_RE.search(str(value or ""))
    return m.group(1) if m else ""


def _ts_code(code: str) -> str:
    code = _to_code(code)
    if not code:
        return ""
    suffix = "SH" if code.startswith(("6", "9")) else "SZ"
    return f"{code}.{suffix}"


def _normalize_trade_date(value) -> str:
    s = str(value or "")
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s[:10]


def _date_yyyymmdd(days_back: int = 0) -> str:
    return (datetime.now() - timedelta(days=days_back)).strftime("%Y%m%d")


def _clean(value):
    if value is None:
        return None
    try:
        if isinstance(value, float) and math.isnan(value):
            return None
    except Exception:
        pass
    try:
        # pandas/numpy scalar
        if hasattr(value, "item"):
            return _clean(value.item())
    except Exception:
        pass
    try:
        # pandas NaT / Timestamp
        if hasattr(value, "isoformat"):
            return value.isoformat()
    except Exception:
        pass
    return value


def _records(df, limit: int = 50) -> list[dict]:
    if df is None or getattr(df, "empty", True):
        return []
    rows = []
    for _, r in df.head(limit).iterrows():
        rows.append({str(k): _clean(v) for k, v in r.to_dict().items()})
    return rows


def _latest(records: list[dict], date_key: str = "trade_date") -> dict:
    if not records:
        return {}
    return sorted(records, key=lambda x: str(x.get(date_key) or ""), reverse=True)[0]


def _pick(row: dict, fields: list[str]) -> dict:
    if not isinstance(row, dict):
        return {}
    return {k: row.get(k) for k in fields if row.get(k) is not None}


def _compact_rows(rows, fields: list[str], limit: int = 20) -> list[dict]:
    if not isinstance(rows, list):
        return []
    return [_pick(r, fields) for r in rows[:max(0, limit)] if isinstance(r, dict)]


def _distill_profile(profile: dict) -> dict:
    """把宽表原始数据蒸馏成 AI 判断所需的关键摘要，避免 profile_json 持续膨胀。"""
    market = profile.get("market") or {}
    valuation = profile.get("valuation") or {}
    moneyflow = profile.get("moneyflow") or {}
    margin = profile.get("margin_detail") or {}
    financials = profile.get("financials") or {}
    holders = profile.get("holders") or {}
    events = profile.get("events") or {}
    pledge = profile.get("pledge") or {}
    research = profile.get("research") or {}

    price_fields = ["trade_date", "open", "high", "low", "close", "pre_close", "change", "pct_chg", "change_pct", "vol", "volume", "amount"]
    valuation_fields = ["trade_date", "close", "turnover_rate", "pe_ttm", "pb", "ps_ttm", "total_mv", "circ_mv"]
    money_fields = ["trade_date", "buy_lg_amount", "sell_lg_amount", "buy_elg_amount", "sell_elg_amount", "net_mf_vol", "net_mf_amount", "buy_md_amount", "sell_md_amount"]
    margin_fields = ["trade_date", "rzye", "rqye", "rzmre", "rqyl", "rzche", "rzrqye"]
    report_fields = ["ts_code", "name", "report_date", "report_title", "report_type", "classify", "org_name", "author_name", "quarter", "rd", "eps", "pe", "np", "tp", "roe", "op_rt", "op_pr"]

    out = {
        "_distilled": True,
        "stock_code": profile.get("stock_code"),
        "stock_name": profile.get("stock_name"),
        "refreshed_at": profile.get("refreshed_at"),
        "data_sources": profile.get("data_sources") or [],
        "snapshot": profile.get("snapshot") or {},
        "risk_flags": (profile.get("risk_flags") or [])[:12],
        "errors": (profile.get("errors") or [])[:8],
        "market": {
            "realtime": _pick(market.get("realtime") or {}, ["code", "name", "price", "prev_close", "open", "high", "low", "change_pct", "volume", "amount", "timestamp", "_source"]),
            "daily": _compact_rows(market.get("daily"), price_fields, 30),
            "weekly": _compact_rows(market.get("weekly"), price_fields, 26),
            "monthly": _compact_rows(market.get("monthly"), price_fields, 24),
            "limit_price": _compact_rows(market.get("limit_price"), ["trade_date", "ts_code", "up_limit", "down_limit"], 5),
        },
        "valuation": {
            "daily_basic": _compact_rows(valuation.get("daily_basic"), valuation_fields, 30),
        },
        "moneyflow": {
            "rows": _compact_rows(moneyflow.get("rows"), money_fields, 30),
        },
        "margin_detail": {
            "rows": _compact_rows(margin.get("rows"), margin_fields, 30),
        },
        "financials": {
            "income": _compact_rows(financials.get("income"), ["ann_date", "end_date", "basic_eps", "diluted_eps", "total_revenue", "revenue", "operate_profit", "total_profit", "n_income", "n_income_attr_p"], 8),
            "balance": _compact_rows(financials.get("balance"), ["ann_date", "end_date", "total_assets", "total_liab", "total_hldr_eqy_exc_min_int", "money_cap", "accounts_receiv", "inventories"], 8),
            "cashflow": _compact_rows(financials.get("cashflow"), ["ann_date", "end_date", "net_profit", "c_fr_sale_sg", "n_cashflow_act", "free_cashflow", "c_pay_acq_const_fiolta"], 8),
            "indicators": _compact_rows(financials.get("indicators"), ["ann_date", "end_date", "eps", "dt_eps", "gross_margin", "roe", "roe_dt", "netprofit_margin", "debt_to_assets", "current_ratio", "quick_ratio"], 8),
        },
        "holders": {
            "top10": _compact_rows(holders.get("top10"), ["ann_date", "end_date", "holder_name", "hold_amount", "hold_ratio", "hold_float_ratio", "hold_change", "holder_type"], 15),
            "top10_float": _compact_rows(holders.get("top10_float"), ["ann_date", "end_date", "holder_name", "hold_amount", "hold_ratio", "hold_float_ratio", "hold_change", "holder_type"], 15),
            "holder_number": _compact_rows(holders.get("holder_number"), ["ann_date", "end_date", "holder_num"], 12),
        },
        "events": {
            "forecast": _compact_rows(events.get("forecast"), ["ann_date", "end_date", "type", "p_change_min", "p_change_max", "net_profit_min", "net_profit_max", "summary", "change_reason"], 8),
            "express": _compact_rows(events.get("express"), ["ann_date", "end_date", "revenue", "operate_profit", "total_profit", "n_income", "diluted_eps", "diluted_roe", "yoy_net_profit"], 8),
            "dividend": _compact_rows(events.get("dividend"), ["ann_date", "end_date", "div_proc", "cash_div", "record_date", "ex_date", "pay_date"], 8),
            "share_float": _compact_rows(events.get("share_float"), ["ann_date", "float_date", "float_share", "float_ratio", "holder_name", "share_type"], 12),
            "repurchase": _compact_rows(events.get("repurchase"), ["ann_date", "end_date", "proc", "vol", "amount", "high_limit", "low_limit"], 8),
            "block_trade": _compact_rows(events.get("block_trade"), ["trade_date", "price", "vol", "amount", "buyer", "seller"], 12),
        },
        "pledge": {
            "stat": _compact_rows(pledge.get("stat"), ["end_date", "pledge_count", "unrest_pledge", "rest_pledge", "total_share", "pledge_ratio"], 12),
            "detail": _compact_rows(pledge.get("detail"), ["ann_date", "holder_name", "pledge_amount", "start_date", "end_date", "is_release", "release_date", "pledgor", "p_total_ratio", "h_total_ratio"], 12),
        },
        "research": {
            "reports": _compact_rows(research.get("reports"), report_fields, 10),
            "skipped": research.get("skipped"),
        },
    }

    # 保留各类 count，避免摘要中丢失覆盖度信息。
    for section_name, section in (("market", market), ("valuation", valuation), ("moneyflow", moneyflow),
                                  ("margin_detail", margin), ("financials", financials), ("holders", holders),
                                  ("events", events), ("pledge", pledge), ("research", research)):
        if not isinstance(section, dict):
            continue
        counts = {k: v for k, v in section.items() if str(k).endswith("_count")}
        if counts:
            out.setdefault(section_name, {}).update(counts)
    return out


def _distill_market_context(payload: dict) -> dict:
    """市场环境保留排行榜和关键字段，避免每天存一大坨宽表 JSON。"""
    sections = (payload.get("sections") or {}) if isinstance(payload, dict) else {}

    def rows(api, fields, limit):
        sec = sections.get(api) or {}
        return {
            "label": sec.get("label"),
            "rows": sec.get("rows"),
            "data": _compact_rows(sec.get("data"), fields, limit),
        }

    return {
        "_distilled": True,
        "status": payload.get("status"),
        "trade_date": payload.get("trade_date"),
        "errors": (payload.get("errors") or [])[:8],
        "refresh": payload.get("refresh"),
        "sections": {
            "limit_list_d": rows("limit_list_d", ["trade_date", "ts_code", "name", "industry", "close", "pct_chg", "amount", "first_time", "last_time", "open_times", "limit"], 50),
            "top_list": rows("top_list", ["trade_date", "ts_code", "name", "close", "pct_change", "turnover_rate", "amount", "net_amount", "reason"], 50),
            "top_inst": rows("top_inst", ["trade_date", "ts_code", "exalter", "buy", "sell", "net_buy", "side", "reason"], 80),
            "margin": rows("margin", ["trade_date", "exchange_id", "rzye", "rzmre", "rzche", "rqye", "rzrqye", "rqyl"], 10),
            "suspend_d": rows("suspend_d", ["ts_code", "trade_date", "suspend_timing", "suspend_type"], 30),
            "index_daily": rows("index_daily", ["ts_code", "trade_date", "close", "open", "high", "low", "pct_chg", "amount"], 15),
            "index_dailybasic": rows("index_dailybasic", ["ts_code", "trade_date", "total_mv", "float_mv", "turnover_rate", "pe", "pe_ttm", "pb"], 15),
            "index_weight": rows("index_weight", ["index_code", "con_code", "trade_date", "weight"], 100),
            "fund_daily": rows("fund_daily", ["ts_code", "trade_date", "open", "high", "low", "close", "pct_chg", "amount"], 15),
        },
    }


def _json(data) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def _save_api_status(api: str, label: str, ok: bool, row_count: int = 0,
                     error: str = "", sample=None):
    try:
        with db() as c:
            c.execute(
                """
                INSERT INTO source_api_status(api, label, ok, row_count, error, sample_json, last_checked_at)
                VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(api) DO UPDATE SET
                  label=excluded.label,
                  ok=excluded.ok,
                  row_count=excluded.row_count,
                  error=excluded.error,
                  sample_json=excluded.sample_json,
                  last_checked_at=CURRENT_TIMESTAMP
                """,
                (api, label, 1 if ok else 0, row_count, error[:500], _json(sample or {})),
            )
    except Exception:
        pass


def _call_df(pro, api: str, label: str, **params):
    try:
        fn = getattr(pro, api, None)
        df = fn(**params) if fn else pro.query(api, **params)
        rows = 0 if df is None else len(df)
        sample = _records(df, limit=1)
        _save_api_status(api, label, True, rows, "", sample[0] if sample else {})
        return df, None
    except Exception as e:
        err = str(e).replace("\n", " ")[:500]
        _save_api_status(api, label, False, 0, err, {})
        return None, err


def _get_tushare():
    token = CONFIG.get("data_sources", {}).get("tushare", {}).get("token")
    if not token:
        return None
    from ..data_sources.tushare_client import TushareClient
    return TushareClient(token)


def _lookup_name(code: str) -> str:
    for table in ("positions", "watchlist", "interaction_stocks"):
        try:
            row = query_one(
                f"SELECT stock_name FROM {table} WHERE stock_code=? AND stock_name IS NOT NULL LIMIT 1",
                (code,),
            )
            if row and row.get("stock_name"):
                return row["stock_name"]
        except Exception:
            pass
    try:
        row = query_one(
            "SELECT name FROM stock_universe WHERE symbol=? OR ts_code=? LIMIT 1",
            (code, _ts_code(code)),
        )
        if row and row.get("name"):
            return row["name"]
    except Exception:
        pass
    return ""


def get_target_stock_pool(limit: int = 50) -> list[dict]:
    """持仓 + 自选 + 互动股，按实盘相关度排序。"""
    _ensure_tables()
    targets: dict[str, dict] = {}

    def add(code, name="", source="unknown", score=0, mention_count=0):
        code = _to_code(code)
        if not code:
            return
        item = targets.setdefault(code, {
            "stock_code": code,
            "stock_name": name or _lookup_name(code),
            "sources": [],
            "score": 0,
            "mention_count": 0,
        })
        if name and not item.get("stock_name"):
            item["stock_name"] = name
        if source not in item["sources"]:
            item["sources"].append(source)
        item["score"] += score
        item["mention_count"] = max(item.get("mention_count") or 0, mention_count or 0)

    try:
        rows = query_all("""
            SELECT p.stock_code, p.stock_name
            FROM positions p
            WHERE p.status='holding'
        """)
        for r in rows:
            add(r.get("stock_code"), r.get("stock_name"), "position", 100)
    except Exception:
        pass

    try:
        for r in query_all("SELECT stock_code, stock_name, priority FROM watchlist"):
            add(r.get("stock_code"), r.get("stock_name"), "watchlist", 60 + int(r.get("priority") or 0) * 10)
    except Exception:
        pass

    try:
        for r in query_all(
            "SELECT stock_code, stock_name, mention_count FROM interaction_stocks "
            "WHERE active=1 ORDER BY mention_count DESC, last_seen_at DESC"
        ):
            mc = int(r.get("mention_count") or 0)
            add(r.get("stock_code"), r.get("stock_name"), "interaction", 30 + mc, mc)
    except Exception:
        pass

    items = list(targets.values())
    items.sort(key=lambda x: (x.get("score") or 0, x.get("mention_count") or 0), reverse=True)
    return items[:max(1, limit)]


def _risk_flags(profile: dict) -> list[dict]:
    flags = []
    snap = profile.get("snapshot") or {}
    valuation = profile.get("valuation") or {}
    moneyflow = profile.get("moneyflow") or {}
    margin = profile.get("margin_detail") or {}
    holders = profile.get("holders") or {}
    pledge = profile.get("pledge") or {}
    events = profile.get("events") or {}

    pb = snap.get("pb")
    ps = snap.get("ps_ttm")
    pe = snap.get("pe_ttm")
    turnover = snap.get("turnover_rate")
    if pb is not None and pb >= 8:
        flags.append({"level": "watch", "type": "valuation", "text": f"PB 偏高: {pb:.2f}"})
    if ps is not None and ps >= 10:
        flags.append({"level": "watch", "type": "valuation", "text": f"PS-TTM 偏高: {ps:.2f}"})
    if pe is None and (pb is not None or ps is not None):
        flags.append({"level": "watch", "type": "profitability", "text": "PE-TTM 缺失，可能盈利较弱或异常"})
    if turnover is not None and turnover >= 15:
        flags.append({"level": "watch", "type": "trading", "text": f"换手率高: {turnover:.2f}%"})

    mf_rows = moneyflow.get("rows") or []
    if mf_rows:
        latest = _latest(mf_rows)
        net = latest.get("net_mf_amount")
        if net is not None:
            flags.append({"level": "info", "type": "moneyflow", "text": f"最新净流: {net}"})

    md_rows = margin.get("rows") or []
    if len(md_rows) >= 2:
        rows = sorted(md_rows, key=lambda x: str(x.get("trade_date") or ""), reverse=True)
        latest, prev = rows[0], rows[1]
        try:
            delta = float(latest.get("rzye") or 0) - float(prev.get("rzye") or 0)
            flags.append({"level": "info", "type": "margin", "text": f"融资余额日变动: {delta/100000000:+.2f} 亿"})
        except Exception:
            pass

    hn = holders.get("holder_number") or []
    if len(hn) >= 2:
        rows = sorted(hn, key=lambda x: str(x.get("end_date") or ""), reverse=True)
        try:
            cur = float(rows[0].get("holder_num") or 0)
            prev = float(rows[1].get("holder_num") or 0)
            if prev:
                pct = (cur - prev) / prev * 100
                direction = "筹码趋散" if pct > 0 else "筹码趋集中"
                flags.append({"level": "info", "type": "chips", "text": f"股东户数环比 {pct:+.2f}%（{direction}）"})
        except Exception:
            pass

    ps_rows = pledge.get("stat") or []
    if ps_rows:
        latest = _latest(ps_rows, "end_date")
        try:
            ratio = float(latest.get("pledge_ratio") or 0)
            if ratio >= 20:
                flags.append({"level": "risk", "type": "pledge", "text": f"质押比例较高: {ratio:.2f}%"})
        except Exception:
            pass

    if events.get("share_float"):
        flags.append({"level": "watch", "type": "unlock", "text": f"未来解禁事件 {len(events.get('share_float') or [])} 条"})
    if events.get("forecast"):
        f = _latest(events.get("forecast") or [], "ann_date")
        typ = f.get("type") or ""
        if typ:
            flags.append({"level": "info", "type": "forecast", "text": f"最新业绩预告: {typ}"})

    valuation_rows = valuation.get("daily_basic") or []
    if valuation_rows:
        latest = _latest(valuation_rows)
        snap["pb"] = snap.get("pb", latest.get("pb"))
        snap["ps_ttm"] = snap.get("ps_ttm", latest.get("ps_ttm"))

    return flags


def collect_market_context(days: int = 7) -> dict:
    """补全最新市场环境：全市场行情、涨跌停、龙虎榜、融资、指数。"""
    _ensure_tables()
    summary = {"status": "success", "refresh": None, "trade_date": None, "sections": {}, "errors": []}
    try:
        from .playbook_service import collect_recent_market_data
        summary["refresh"] = collect_recent_market_data(days=max(1, min(days, 10)))
    except Exception as e:
        summary["errors"].append(f"market_data: {e}")

    latest = query_one("SELECT MAX(trade_date) as d FROM daily_quotes") or {}
    trade_date = str(latest.get("d") or datetime.now().strftime("%Y-%m-%d"))[:10]
    trade_yyyymmdd = trade_date.replace("-", "")
    summary["trade_date"] = trade_date

    ts = _get_tushare()
    if not ts:
        summary["status"] = "partial"
        summary["errors"].append("Tushare token missing")
    else:
        pro = ts.pro
        market_cases = [
            ("limit_list_d", "涨跌停股票池", {"trade_date": trade_yyyymmdd}, 120),
            ("top_list", "龙虎榜", {"trade_date": trade_yyyymmdd}, 120),
            ("top_inst", "龙虎榜机构席位", {"trade_date": trade_yyyymmdd}, 200),
            ("margin", "融资融券汇总", {"trade_date": trade_yyyymmdd}, 20),
            ("suspend_d", "停复牌", {"trade_date": trade_yyyymmdd}, 80),
            ("index_daily", "指数日线", {"ts_code": "000001.SH", "start_date": _date_yyyymmdd(30), "end_date": trade_yyyymmdd}, 30),
            ("index_dailybasic", "指数估值", {"ts_code": "000001.SH", "start_date": _date_yyyymmdd(30), "end_date": trade_yyyymmdd}, 30),
            ("index_weight", "沪深300成分权重", {"index_code": "000300.SH", "start_date": _date_yyyymmdd(60), "end_date": trade_yyyymmdd}, 350),
            ("fund_daily", "ETF日线", {"ts_code": "510300.SH", "start_date": _date_yyyymmdd(30), "end_date": trade_yyyymmdd}, 30),
        ]
        for api, label, params, limit in market_cases:
            df, err = _call_df(pro, api, label, **params)
            if err:
                summary["errors"].append(f"{api}: {err[:120]}")
            summary["sections"][api] = {
                "label": label,
                "rows": 0 if df is None else len(df),
                "data": _records(df, limit=limit),
            }

    payload_to_store = _distill_market_context(summary)
    with db() as c:
        c.execute(
            """
            INSERT OR REPLACE INTO market_context_cache(scope_key, trade_date, payload_json, refreshed_at)
            VALUES (?,?,?,CURRENT_TIMESTAMP)
            """,
            ("latest", trade_date, _json(payload_to_store)),
        )
    return payload_to_store


def enrich_stock_profile(stock_code: str, days: int = 90, include_research: bool = True) -> dict:
    """为单只股票构建多源深度画像。"""
    _ensure_tables()
    code = _to_code(stock_code)
    if not code:
        return {"status": "failed", "error": "缺少有效股票代码", "stock_code": stock_code}
    name = _lookup_name(code)
    ts_code = _ts_code(code)
    end = _date_yyyymmdd(0)
    start = _date_yyyymmdd(days)
    long_start = _date_yyyymmdd(900)

    profile = {
        "stock_code": code,
        "stock_name": name,
        "refreshed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data_sources": ["local_cache"],
        "snapshot": {},
        "market": {},
        "valuation": {},
        "moneyflow": {},
        "margin_detail": {},
        "financials": {},
        "holders": {},
        "events": {},
        "pledge": {},
        "research": {},
        "errors": [],
    }

    # 本地缓存快照作为底盘
    quote = query_one(
        "SELECT trade_date, open, high, low, close, volume, amount, change_pct "
        "FROM daily_quotes WHERE stock_code=? ORDER BY trade_date DESC LIMIT 1",
        (code,),
    ) or {}
    basic = query_one(
        "SELECT trade_date, close, turnover_rate, pe_ttm, pb, ps_ttm, total_mv, circ_mv "
        "FROM daily_basic WHERE stock_code=? ORDER BY trade_date DESC LIMIT 1",
        (code,),
    ) or {}
    profile["snapshot"] = {
        "trade_date": quote.get("trade_date") or basic.get("trade_date"),
        "open": quote.get("open"),
        "high": quote.get("high"),
        "low": quote.get("low"),
        "close": quote.get("close") if quote.get("close") is not None else basic.get("close"),
        "volume": quote.get("volume"),
        "amount": quote.get("amount"),
        "change_pct": quote.get("change_pct"),
        "turnover_rate": basic.get("turnover_rate"),
        "pe_ttm": basic.get("pe_ttm"),
        "pb": basic.get("pb"),
        "ps_ttm": basic.get("ps_ttm"),
        "total_mv": basic.get("total_mv"),
        "circ_mv": basic.get("circ_mv"),
    }

    try:
        from ..data_sources.unified import UnifiedDataSource
        ds = UnifiedDataSource(tushare_token=CONFIG.get("data_sources", {}).get("tushare", {}).get("token"))
        rt = ds.get_realtime(code) or {}
        if rt:
            profile["market"]["realtime"] = rt
            profile["data_sources"].append(rt.get("_source") or "realtime")
    except Exception as e:
        profile["errors"].append(f"realtime: {str(e)[:120]}")

    ts = _get_tushare()
    if not ts:
        profile["errors"].append("Tushare token missing")
    else:
        pro = ts.pro
        profile["data_sources"].append("tushare")
        cases = [
            ("daily", "日线行情", {"ts_code": ts_code, "start_date": start, "end_date": end}, "market", "daily", 80),
            ("weekly", "周线行情", {"ts_code": ts_code, "start_date": _date_yyyymmdd(520), "end_date": end}, "market", "weekly", 80),
            ("monthly", "月线行情", {"ts_code": ts_code, "start_date": _date_yyyymmdd(1600), "end_date": end}, "market", "monthly", 80),
            ("adj_factor", "复权因子", {"ts_code": ts_code, "start_date": start, "end_date": end}, "market", "adj_factor", 80),
            ("stk_limit", "每日涨跌停价格", {"ts_code": ts_code, "trade_date": end}, "market", "limit_price", 10),
            ("daily_basic", "每日估值/换手/市值", {"ts_code": ts_code, "start_date": start, "end_date": end,
                                                    "fields": "ts_code,trade_date,close,turnover_rate,pe_ttm,pb,ps_ttm,total_mv,circ_mv"}, "valuation", "daily_basic", 80),
            ("moneyflow", "个股资金流", {"ts_code": ts_code, "start_date": start, "end_date": end}, "moneyflow", "rows", 80),
            ("margin_detail", "个股融资融券", {"ts_code": ts_code, "start_date": start, "end_date": end}, "margin_detail", "rows", 80),
            ("income", "利润表", {"ts_code": ts_code, "start_date": long_start, "end_date": end}, "financials", "income", 16),
            ("balancesheet", "资产负债表", {"ts_code": ts_code, "start_date": long_start, "end_date": end}, "financials", "balance", 16),
            ("cashflow", "现金流量表", {"ts_code": ts_code, "start_date": long_start, "end_date": end}, "financials", "cashflow", 16),
            ("fina_indicator", "财务指标", {"ts_code": ts_code, "start_date": long_start, "end_date": end}, "financials", "indicators", 16),
            ("forecast", "业绩预告", {"ts_code": ts_code, "start_date": long_start, "end_date": end}, "events", "forecast", 20),
            ("express", "业绩快报", {"ts_code": ts_code, "start_date": long_start, "end_date": end}, "events", "express", 20),
            ("dividend", "分红送股", {"ts_code": ts_code}, "events", "dividend", 20),
            ("disclosure_date", "财报披露计划", {"ts_code": ts_code, "start_date": _date_yyyymmdd(365), "end_date": _date_yyyymmdd(-180)}, "events", "disclosure", 20),
            ("top10_holders", "前十大股东", {"ts_code": ts_code, "start_date": long_start, "end_date": end}, "holders", "top10", 30),
            ("top10_floatholders", "前十大流通股东", {"ts_code": ts_code, "start_date": long_start, "end_date": end}, "holders", "top10_float", 30),
            ("stk_holdernumber", "股东户数", {"ts_code": ts_code, "start_date": long_start, "end_date": end}, "holders", "holder_number", 40),
            ("share_float", "限售解禁", {"ts_code": ts_code, "start_date": end, "end_date": _date_yyyymmdd(-365)}, "events", "share_float", 30),
            ("pledge_stat", "股权质押统计", {"ts_code": ts_code}, "pledge", "stat", 30),
            ("pledge_detail", "股权质押明细", {"ts_code": ts_code}, "pledge", "detail", 30),
            ("block_trade", "大宗交易", {"ts_code": ts_code, "start_date": start, "end_date": end}, "events", "block_trade", 30),
        ]
        if include_research:
            cases.append(("report_rc", "券商研报/一致预期", {"ts_code": ts_code, "start_date": long_start, "end_date": end}, "research", "reports", 30))
        else:
            profile["research"]["reports"] = []
            profile["research"]["reports_count"] = 0
            profile["research"]["skipped"] = "批量日更跳过 report_rc，避免触发 Tushare 2次/分钟限频；单股刷新或周研报任务会补。"
        for api, label, params, section, key, limit in cases:
            df, err = _call_df(pro, api, label, **params)
            if err:
                profile["errors"].append(f"{api}: {err[:160]}")
            profile.setdefault(section, {})[key] = _records(df, limit=limit)
            profile.setdefault(section, {})[f"{key}_count"] = 0 if df is None else len(df)

        # 回购接口有些账户不支持按 ts_code，失败时用日期拉全市场后过滤。
        df, err = _call_df(pro, "repurchase", "股份回购", ts_code=ts_code)
        if err:
            df, err2 = _call_df(pro, "repurchase", "股份回购", start_date=long_start, end_date=end)
            if df is not None and not getattr(df, "empty", True) and "ts_code" in df.columns:
                df = df[df["ts_code"] == ts_code]
            if err2:
                profile["errors"].append(f"repurchase: {err2[:160]}")
        profile["events"]["repurchase"] = _records(df, limit=30)
        profile["events"]["repurchase_count"] = 0 if df is None else len(df)

    flags = _risk_flags(profile)
    profile["risk_flags"] = flags
    latest_trade_date = profile.get("snapshot", {}).get("trade_date")
    latest_close = profile.get("snapshot", {}).get("close")
    profile_to_store = _distill_profile(profile)
    with db() as c:
        c.execute(
            """
            INSERT INTO stock_deep_profiles
            (stock_code, stock_name, latest_trade_date, latest_close, data_sources,
             profile_json, risk_flags_json, refreshed_at)
            VALUES (?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(stock_code) DO UPDATE SET
              stock_name=excluded.stock_name,
              latest_trade_date=excluded.latest_trade_date,
              latest_close=excluded.latest_close,
              data_sources=excluded.data_sources,
              profile_json=excluded.profile_json,
              risk_flags_json=excluded.risk_flags_json,
              refreshed_at=CURRENT_TIMESTAMP
            """,
            (code, name, latest_trade_date, latest_close,
             ",".join(sorted(set(profile.get("data_sources") or []))),
             _json(profile_to_store), _json(flags)),
        )
    return {"status": "success", "stock_code": code, "stock_name": name,
            "latest_trade_date": latest_trade_date, "latest_close": latest_close,
            "risk_flags": flags, "errors": profile.get("errors") or [],
            "profile": profile_to_store}


def run_data_enrichment(stock_code: str | None = None, max_stocks: int = 30,
                        refresh_market: bool = True, refresh_days: int = 7) -> dict:
    _ensure_tables()
    start = time.time()
    scope = "single" if stock_code else "target_pool"
    summary = {"status": "success", "scope": scope, "market": None, "stocks": [], "errors": []}
    try:
        if refresh_market:
            summary["market"] = collect_market_context(days=refresh_days)

        if stock_code:
            targets = [{"stock_code": _to_code(stock_code), "stock_name": _lookup_name(_to_code(stock_code))}]
        else:
            targets = get_target_stock_pool(limit=max_stocks)

        for idx, t in enumerate(targets):
            code = _to_code(t.get("stock_code"))
            if not code:
                continue
            try:
                include_research = bool(stock_code) or idx < 2
                r = enrich_stock_profile(code, include_research=include_research)
                summary["stocks"].append({
                    "stock_code": code,
                    "stock_name": r.get("stock_name"),
                    "latest_trade_date": r.get("latest_trade_date"),
                    "latest_close": r.get("latest_close"),
                    "risk_flags": r.get("risk_flags"),
                    "errors": r.get("errors")[:5],
                })
            except Exception as e:
                log.exception("多源画像失败: %s", code)
                summary["errors"].append(f"{code}: {str(e)[:200]}")

        duration = time.time() - start
        status = "success" if not summary["errors"] else "partial"
        with db() as c:
            rid = c.execute(
                """
                INSERT INTO data_enrichment_runs(scope, status, target_count, refreshed_count,
                                                 duration_seconds, summary_json, error_msg)
                VALUES (?,?,?,?,?,?,?)
                """,
                (scope, status, len(targets), len(summary["stocks"]), duration,
                 _json(summary), "; ".join(summary["errors"])[:1000]),
            ).lastrowid
        summary.update({
            "status": status,
            "run_id": rid,
            "target_count": len(targets),
            "refreshed_count": len(summary["stocks"]),
            "duration_seconds": duration,
        })
        return summary
    except Exception as e:
        duration = time.time() - start
        with db() as c:
            rid = c.execute(
                "INSERT INTO data_enrichment_runs(scope, status, target_count, refreshed_count, duration_seconds, summary_json, error_msg) VALUES (?,?,?,?,?,?,?)",
                (scope, "failed", 0, 0, duration, _json(summary), str(e)[:1000]),
            ).lastrowid
        log.exception("多源数据增强失败")
        return {"status": "failed", "run_id": rid, "error": str(e), "duration_seconds": duration}


def get_stock_deep_profile(stock_code: str) -> dict:
    _ensure_tables()
    code = _to_code(stock_code)
    if not code:
        return {"success": False, "error": "缺少有效股票代码"}
    row = query_one(
        "SELECT * FROM stock_deep_profiles WHERE stock_code=?",
        (code,),
    )
    if not row:
        return {"success": False, "stock_code": code, "error": "尚无多源深度画像，请先运行数据增强"}
    try:
        profile = json.loads(row.get("profile_json") or "{}")
    except Exception:
        profile = {}
    return {
        "success": True,
        "stock_code": code,
        "stock_name": row.get("stock_name"),
        "refreshed_at": row.get("refreshed_at"),
        "latest_trade_date": row.get("latest_trade_date"),
        "latest_close": row.get("latest_close"),
        "data_sources": row.get("data_sources"),
        "risk_flags": json.loads(row.get("risk_flags_json") or "[]"),
        "profile": profile,
    }


def get_latest_market_context() -> dict:
    _ensure_tables()
    row = query_one(
        "SELECT * FROM market_context_cache WHERE scope_key='latest' ORDER BY refreshed_at DESC LIMIT 1"
    )
    if not row:
        return {"success": False, "error": "尚无市场环境缓存，请先运行数据增强"}
    try:
        payload = json.loads(row.get("payload_json") or "{}")
    except Exception:
        payload = {}
    return {"success": True, "trade_date": row.get("trade_date"),
            "refreshed_at": row.get("refreshed_at"), "payload": payload}


def list_source_api_status() -> dict:
    _ensure_tables()
    rows = query_all("SELECT * FROM source_api_status ORDER BY ok DESC, api")
    return {"success": True, "count": len(rows), "items": rows}
