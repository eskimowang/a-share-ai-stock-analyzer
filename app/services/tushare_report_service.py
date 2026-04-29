"""Tushare 研报规则引擎。

Tushare report_rc 是付费研报/一致预期接口，但当前账号存在低频限制
（实测可能为 10 次/小时）。因此这里不做全市场暴力采集，而是：

1. 重点池优先：持仓 > 自选 > 互动股票。
2. 慢速采集：默认每次最多 2 只，单次 report_rc 调用至少间隔 370 秒。
3. 缓存复用：优先加工 stock_deep_profiles 里已有的 report_rc 数据。
4. 去重加工：落 reports_cache，同时生成 research_report_signals，供 AI 直接查询。
5. 批量加工：把 reports_cache 里的全市场缓存研报转成可反测信号。
6. 质量分级：按券商层级、样本数、命中率、超额收益给团队/作者打分。
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
POSITIVE_RATINGS = ("买入", "增持", "推荐", "强烈推荐", "跑赢", "优于", "outperform", "buy")
NEGATIVE_RATINGS = ("卖出", "减持", "回避", "低于", "underperform", "sell")
NEUTRAL_RATINGS = ("中性", "持有", "neutral", "hold")

KEY_BROKER_KEYWORDS = ("中金", "中国国际金融", "CICC", "中信证券")
MAJOR_BROKER_KEYWORDS = (
    "中信建投", "华泰", "国泰君安", "申万宏源", "海通", "广发",
    "招商证券", "国盛", "浙商", "兴业", "东方证券", "中邮",
)



def _ensure_tables():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS tushare_report_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            scope TEXT,
            status TEXT,
            target_count INTEGER DEFAULT 0,
            live_calls INTEGER DEFAULT 0,
            cached_reports_processed INTEGER DEFAULT 0,
            reports_saved INTEGER DEFAULT 0,
            signals_saved INTEGER DEFAULT 0,
            duration_seconds REAL,
            summary_json TEXT,
            error_msg TEXT
        );
        CREATE TABLE IF NOT EXISTS tushare_report_api_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            api TEXT DEFAULT 'report_rc',
            stock_code TEXT,
            called_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            ok INTEGER,
            row_count INTEGER DEFAULT 0,
            error TEXT
        );
        CREATE TABLE IF NOT EXISTS research_report_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stock_code TEXT NOT NULL,
            stock_name TEXT,
            report_date TEXT,
            org_name TEXT,
            author_name TEXT,
            report_title TEXT,
            report_type TEXT,
            classify TEXT,
            quarter TEXT,
            rating TEXT,
            target_price REAL,
            eps REAL,
            pe REAL,
            net_profit REAL,
            roe REAL,
            op_revenue REAL,
            op_profit REAL,
            signal_score REAL,
            stance TEXT,
            key_points TEXT,
            raw_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(stock_code, report_date, org_name, report_title)
        );
        CREATE TABLE IF NOT EXISTS research_author_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            org_name TEXT,
            author_name TEXT,
            broker_tier TEXT,
            horizon_days INTEGER,
            total_reports INTEGER DEFAULT 0,
            hit_count INTEGER DEFAULT 0,
            hit_rate REAL,
            avg_return REAL,
            avg_excess_return REAL,
            avg_weighted_score REAL,
            quality_score REAL,
            grade TEXT,
            sample_level TEXT,
            last_report_date TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(org_name, author_name, horizon_days)
        );
        CREATE INDEX IF NOT EXISTS idx_research_signals_code_date
          ON research_report_signals(stock_code, report_date DESC);
        CREATE INDEX IF NOT EXISTS idx_research_signals_author
          ON research_report_signals(org_name, author_name, report_date DESC);
        CREATE INDEX IF NOT EXISTS idx_report_api_calls_time
          ON tushare_report_api_calls(api, called_at DESC);
        CREATE INDEX IF NOT EXISTS idx_author_stats_rank
          ON research_author_stats(horizon_days, hit_rate DESC, total_reports DESC);
        """)

    with db() as c:
        for table, col, ddl in [
            ("research_report_signals", "broker_tier", "TEXT"),
            ("research_report_signals", "broker_weight", "REAL"),
            ("research_report_signals", "weighted_signal_score", "REAL"),
            ("research_report_signals", "entry_trade_date", "TEXT"),
            ("research_report_signals", "entry_close", "REAL"),
            ("research_report_signals", "return_20d", "REAL"),
            ("research_report_signals", "return_60d", "REAL"),
            ("research_report_signals", "peer_excess_20d", "REAL"),
            ("research_report_signals", "peer_excess_60d", "REAL"),
            ("research_report_signals", "hit_20d", "INTEGER"),
            ("research_report_signals", "hit_60d", "INTEGER"),
            ("research_report_signals", "evaluated_at", "TIMESTAMP"),
            ("research_author_stats", "quality_score", "REAL"),
            ("research_author_stats", "grade", "TEXT"),
            ("research_author_stats", "sample_level", "TEXT"),
        ]:
            try:
                cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
                if col not in cols:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
            except Exception:
                pass


def _to_code(value) -> str:
    m = STOCK_CODE_RE.search(str(value or ""))
    return m.group(1) if m else ""


def _ts_code(code: str) -> str:
    code = _to_code(code)
    if not code:
        return ""
    suffix = "SH" if code.startswith(("6", "9")) else "SZ"
    return f"{code}.{suffix}"


def _date_yyyymmdd(days_back: int = 0) -> str:
    return (datetime.now() - timedelta(days=days_back)).strftime("%Y%m%d")


def _norm_date(value) -> str:
    s = str(value or "").strip()
    if len(s) >= 10 and "-" in s:
        return s[:10]
    if len(s) >= 8 and s[:8].isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s[:10]


def _clean(value):
    if value is None:
        return None
    try:
        if isinstance(value, float) and math.isnan(value):
            return None
    except Exception:
        pass
    try:
        if hasattr(value, "item"):
            return _clean(value.item())
    except Exception:
        pass
    return value


def _num(value):
    value = _clean(value)
    if value in (None, "", "nan", "None"):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _json(data) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def _broker_tier(org_name: str) -> tuple[str, float]:
    org = (org_name or "").strip()
    if any(k.lower() in org.lower() for k in KEY_BROKER_KEYWORDS):
        return "S", 1.0
    if any(k.lower() in org.lower() for k in MAJOR_BROKER_KEYWORDS):
        return "A", 0.65
    if org:
        return "B", 0.35
    return "B", 0.25


def _first_trade_on_or_after(code: str, date: str) -> dict | None:
    return query_one(
        "SELECT trade_date, close FROM daily_quotes WHERE stock_code=? AND trade_date>=? "
        "ORDER BY trade_date ASC LIMIT 1",
        (_to_code(code), date),
    )


def _trade_after_n(code: str, start_trade_date: str, n: int) -> dict | None:
    rows = query_all(
        "SELECT trade_date, close FROM daily_quotes WHERE stock_code=? AND trade_date>=? "
        "ORDER BY trade_date ASC LIMIT ?",
        (_to_code(code), start_trade_date, max(1, n + 1)),
    )
    if len(rows) <= n:
        return None
    return rows[n]


def _industry_for_code(code: str) -> str:
    try:
        row = query_one(
            "SELECT industry FROM stock_universe WHERE symbol=? OR ts_code=? LIMIT 1",
            (_to_code(code), _ts_code(code)),
        )
        return (row or {}).get("industry") or ""
    except Exception:
        return ""


def _peer_return(code: str, start_date: str, end_date: str, max_peers: int = 80) -> float | None:
    industry = _industry_for_code(code)
    if not industry:
        return None
    peers = query_all(
        "SELECT symbol FROM stock_universe WHERE industry=? AND symbol IS NOT NULL LIMIT ?",
        (industry, max_peers),
    )
    vals = []
    for p in peers:
        peer = _to_code(p.get("symbol"))
        if not peer or peer == _to_code(code):
            continue
        s = query_one(
            "SELECT close FROM daily_quotes WHERE stock_code=? AND trade_date>=? ORDER BY trade_date ASC LIMIT 1",
            (peer, start_date),
        )
        e = query_one(
            "SELECT close FROM daily_quotes WHERE stock_code=? AND trade_date<=? ORDER BY trade_date DESC LIMIT 1",
            (peer, end_date),
        )
        try:
            if s and e and s.get("close"):
                vals.append((float(e["close"]) - float(s["close"])) / float(s["close"]) * 100)
        except Exception:
            pass
    if len(vals) < 5:
        return None
    vals.sort()
    mid = len(vals) // 2
    if len(vals) % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2


def _is_hit(stance: str, ret: float | None, excess: float | None = None) -> int | None:
    if ret is None:
        return None
    metric = excess if excess is not None else ret
    stance = stance or "neutral"
    if stance == "positive":
        return 1 if metric > 0 else 0
    if stance == "negative":
        return 1 if metric < 0 else 0
    return 1 if abs(metric) <= 5 else 0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _quality_grade(total_reports: int,
                   hit_rate: float | None,
                   avg_return: float | None,
                   avg_excess_return: float | None,
                   avg_weighted_score: float | None,
                   broker_tier: str) -> tuple[float, str, str]:
    """把历史命中率、样本数和券商权重合成团队/作者质量等级。"""
    total = max(0, int(total_reports or 0))
    hr = _clamp(float(hit_rate or 0), 0, 1)
    sample_level = "low"
    if total >= 10:
        sample_level = "high"
    elif total >= 5:
        sample_level = "medium"

    sample_score = min(20.0, math.log1p(total) * 7.0)
    performance = avg_excess_return if avg_excess_return is not None else avg_return
    perf_score = _clamp(float(performance or 0), -20, 20) * 0.6
    conviction_score = _clamp(float(avg_weighted_score or 0), -2.5, 3.5) * 3.0
    tier_bonus = {"S": 8.0, "A": 4.0}.get((broker_tier or "B").upper(), 0.0)

    score = _clamp(hr * 55.0 + sample_score + perf_score + conviction_score + tier_bonus, 0.0, 100.0)
    if score >= 85:
        grade = "S"
    elif score >= 75:
        grade = "A"
    elif score >= 60:
        grade = "B"
    elif score >= 45:
        grade = "C"
    else:
        grade = "D"

    # 样本太少时不让等级虚高；保留分数，等级体现置信度折扣。
    if sample_level == "low" and grade in ("S", "A"):
        grade = "B"
    elif sample_level == "medium" and grade == "S":
        grade = "A"
    return round(score, 2), grade, sample_level


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


def _get_targets(stock_code: str | None = None, max_stocks: int = 2) -> list[dict]:
    if stock_code:
        code = _to_code(stock_code)
        return [{"stock_code": code, "stock_name": _lookup_name(code), "score": 999}] if code else []
    try:
        from .data_enrichment_service import get_target_stock_pool
        return get_target_stock_pool(limit=max(1, max_stocks))
    except Exception:
        rows = query_all(
            "SELECT DISTINCT stock_code, stock_name FROM positions WHERE status='holding' LIMIT ?",
            (max_stocks,),
        )
        return rows


def _get_tushare():
    token = CONFIG.get("data_sources", {}).get("tushare", {}).get("token")
    if not token:
        return None
    from ..data_sources.tushare_client import TushareClient
    return TushareClient(token)


def _latest_report_call() -> Optional[dict]:
    _ensure_tables()
    return query_one(
        "SELECT * FROM tushare_report_api_calls WHERE api='report_rc' ORDER BY called_at DESC LIMIT 1"
    )


def _seconds_since(ts: str | None) -> float:
    if not ts:
        return 10**9
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return (datetime.now() - datetime.strptime(str(ts)[:19], fmt)).total_seconds()
        except Exception:
            pass
    return 10**9


def _record_api_call(stock_code: str, ok: bool, rows: int = 0, error: str = ""):
    with db() as c:
        c.execute(
            "INSERT INTO tushare_report_api_calls(api, stock_code, ok, row_count, error) VALUES (?,?,?,?,?)",
            ("report_rc", stock_code, 1 if ok else 0, rows, error[:500]),
        )


def _records(df) -> list[dict]:
    if df is None or getattr(df, "empty", True):
        return []
    out = []
    for _, r in df.iterrows():
        out.append({str(k): _clean(v) for k, v in r.to_dict().items()})
    return out


def _fetch_report_rc_live(code: str, months: int = 24) -> tuple[list[dict], str]:
    ts = _get_tushare()
    if not ts:
        return [], "Tushare token missing"
    end = _date_yyyymmdd(0)
    start = (datetime.now() - timedelta(days=max(30, months * 31))).strftime("%Y%m%d")
    try:
        df = ts.pro.report_rc(ts_code=_ts_code(code), start_date=start, end_date=end)
        rows = _records(df)
        _record_api_call(code, True, len(rows), "")
        return rows, ""
    except Exception as e:
        err = str(e).replace("\n", " ")[:500]
        _record_api_call(code, False, 0, err)
        return [], err


def _cached_report_rows(code: str) -> list[dict]:
    """复用多源深度画像里已经抓到的 report_rc 行。"""
    row = query_one(
        "SELECT profile_json FROM stock_deep_profiles WHERE stock_code=?",
        (_to_code(code),),
    )
    if not row:
        return []
    try:
        profile = json.loads(row.get("profile_json") or "{}")
    except Exception:
        return []
    reports = ((profile.get("research") or {}).get("reports") or [])
    return [r for r in reports if isinstance(r, dict)]


def _rating_from_row(row: dict) -> str:
    for key in ("rd", "rating", "rate", "report_rating"):
        val = row.get(key)
        if isinstance(val, str) and val.strip() and val.strip().lower() not in ("nan", "none"):
            return val.strip()[:30]
    return ""


def _derive_signal(row: dict, stock_code: str) -> dict:
    rating = _rating_from_row(row)
    title = str(row.get("report_title") or row.get("title") or "")
    report_type = str(row.get("report_type") or "")
    classify = str(row.get("classify") or "")
    org = str(row.get("org_name") or row.get("broker") or "")
    score = 0.0
    points = []

    lower_rating = rating.lower()
    if any(x.lower() in lower_rating for x in POSITIVE_RATINGS):
        score += 2.0
        points.append(f"评级/观点偏正面: {rating}")
    elif any(x.lower() in lower_rating for x in NEGATIVE_RATINGS):
        score -= 2.0
        points.append(f"评级/观点偏负面: {rating}")
    elif any(x.lower() in lower_rating for x in NEUTRAL_RATINGS):
        points.append(f"评级/观点中性: {rating}")

    if "个股" in report_type or "公司" in classify or row.get("name"):
        score += 0.5
        points.append("个股相关研报")
    elif report_type and "非个股" in report_type:
        score -= 0.2
        points.append("非个股/行业报告，需弱化权重")

    eps = _num(row.get("eps"))
    pe = _num(row.get("pe"))
    np = _num(row.get("np"))
    roe = _num(row.get("roe"))
    # report_rc 的 tp 常见含义更接近 total profit/利润总额预测，
    # 不是股票目标价；目标价只认显式 target_price/tgt_price 字段。
    total_profit = _num(row.get("tp"))
    target_price = _num(row.get("target_price") or row.get("tgt_price"))
    op_rt = _num(row.get("op_rt"))
    op_pr = _num(row.get("op_pr"))

    if eps is not None:
        points.append(f"EPS预测 {eps:g}")
        if eps > 0:
            score += 0.3
    if np is not None:
        points.append(f"净利润预测 {np:g}")
        if np > 0:
            score += 0.3
    if roe is not None:
        points.append(f"ROE预测 {roe:g}")
        if roe >= 10:
            score += 0.5
        elif roe <= 3:
            score -= 0.3
    if pe is not None:
        points.append(f"PE预测 {pe:g}")
        if pe > 80:
            score -= 0.4
        elif 0 < pe <= 30:
            score += 0.3
    if target_price is not None:
        points.append(f"目标价 {target_price:g}")
    if total_profit is not None:
        points.append(f"利润总额预测 {total_profit:g}")
    if op_rt is not None:
        points.append(f"营收预测 {op_rt:g}")
    if op_pr is not None:
        points.append(f"营业利润预测 {op_pr:g}")
    tier, weight = _broker_tier(org)
    if tier == "S":
        score += 0.8
        points.append(f"重点券商: {org}（高权重）")
    elif tier == "A":
        score += 0.2
        points.append(f"主流券商: {org}")
    elif org:
        points.append(f"普通券商: {org}（低权重蒸馏/统计）")
    if title:
        points.append(f"标题: {title[:80]}")

    if score >= 1.5:
        stance = "positive"
    elif score <= -1:
        stance = "negative"
    else:
        stance = "neutral"

    return {
        "rating": rating,
        "target_price": target_price,
        "total_profit": total_profit,
        "eps": eps,
        "pe": pe,
        "net_profit": np,
        "roe": roe,
        "op_revenue": op_rt,
        "op_profit": op_pr,
        "signal_score": round(score, 2),
        "broker_tier": tier,
        "broker_weight": weight,
        "weighted_signal_score": round(score * weight, 2),
        "stance": stance,
        "key_points": points[:10],
    }


def _save_one_report_signal(code: str, row: dict, save_report_cache: bool = True) -> dict:
    code = _to_code(row.get("ts_code") or row.get("stock_code") or code)
    if not code:
        return {"saved": False, "reason": "missing_code"}
    stock_name = row.get("name") or _lookup_name(code)
    report_date = _norm_date(row.get("report_date") or row.get("ann_date"))
    title = (row.get("report_title") or row.get("title") or "").strip()[:500]
    org = (row.get("org_name") or row.get("broker") or "").strip()[:100]
    author = (row.get("author_name") or row.get("author") or "").strip()[:100]
    signal = _derive_signal(row, code)
    rating = signal.get("rating") or ""
    core_logic = "；".join(signal.get("key_points") or [])[:1000]

    if not report_date or not (title or org):
        return {"saved": False, "reason": "missing_report_identity"}

    reports_saved = 0
    existing = None
    if save_report_cache:
        existing = query_one(
            "SELECT id FROM reports_cache WHERE stock_code=? AND report_date=? AND broker=? AND title=? LIMIT 1",
            (code, report_date, org, title),
        )
    if save_report_cache and not existing:
        with db() as c:
            c.execute(
                """
                INSERT INTO reports_cache
                (stock_code, report_date, broker, author, rating, target_price,
                 title, core_logic, data_source)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    code, report_date, org, author, rating[:20], signal.get("target_price"),
                    title, core_logic, "tushare_report_rc",
                ),
            )
        reports_saved = 1

    with db() as c:
        c.execute(
            """
            INSERT INTO research_report_signals
            (stock_code, stock_name, report_date, org_name, author_name, report_title,
             report_type, classify, quarter, rating, target_price, eps, pe, net_profit,
             roe, op_revenue, op_profit, signal_score, broker_tier, broker_weight,
             weighted_signal_score, stance, key_points, raw_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(stock_code, report_date, org_name, report_title) DO UPDATE SET
              stock_name=excluded.stock_name,
              author_name=excluded.author_name,
              report_type=excluded.report_type,
              classify=excluded.classify,
              quarter=excluded.quarter,
              rating=excluded.rating,
              target_price=excluded.target_price,
              eps=excluded.eps,
              pe=excluded.pe,
              net_profit=excluded.net_profit,
              roe=excluded.roe,
              op_revenue=excluded.op_revenue,
              op_profit=excluded.op_profit,
              signal_score=excluded.signal_score,
              broker_tier=excluded.broker_tier,
              broker_weight=excluded.broker_weight,
              weighted_signal_score=excluded.weighted_signal_score,
              stance=excluded.stance,
              key_points=excluded.key_points,
              raw_json=excluded.raw_json,
              created_at=CURRENT_TIMESTAMP
            """,
            (
                code, stock_name, report_date, org, author, title,
                row.get("report_type"), row.get("classify"), row.get("quarter"),
                rating, signal.get("target_price"), signal.get("eps"), signal.get("pe"),
                signal.get("net_profit"), signal.get("roe"), signal.get("op_revenue"),
                signal.get("op_profit"), signal.get("signal_score"), signal.get("broker_tier"),
                signal.get("broker_weight"), signal.get("weighted_signal_score"),
                signal.get("stance"), _json(signal.get("key_points") or []), _json(row),
            ),
        )
    return {"saved": True, "reports_saved": reports_saved, "signals_saved": 1, "stance": signal.get("stance")}


def _report_cache_to_signal_row(row: dict) -> dict:
    code = _to_code(row.get("stock_code") or row.get("ts_code") or row.get("code"))
    report_date = _norm_date(row.get("report_date") or row.get("ann_date") or row.get("date"))
    org = str(row.get("broker") or row.get("org_name") or "").strip()
    rating = str(row.get("rating") or row.get("rd") or "").strip()
    author = str(row.get("author") or row.get("author_name") or "").strip()
    if not author:
        author = "未标注团队"
    title = str(row.get("title") or row.get("report_title") or "").strip()
    if not title:
        title_parts = [p for p in (org, code, report_date, rating or "研报") if p]
        title = " ".join(title_parts)[:500]
    core_logic = str(row.get("core_logic") or row.get("summary") or "").strip()
    return {
        "source_cache_id": row.get("id"),
        "stock_code": code,
        "name": row.get("stock_name") or _lookup_name(code),
        "report_date": report_date,
        "broker": org,
        "org_name": org,
        "author": author,
        "author_name": author,
        "rating": rating,
        "rd": rating,
        "target_price": _num(row.get("target_price")),
        "title": title,
        "report_title": title,
        "report_type": row.get("report_type") or "缓存研报",
        "classify": row.get("classify") or "公司研报",
        "core_logic": core_logic[:800],
        "data_source": row.get("data_source") or row.get("source") or "reports_cache",
    }


def process_reports_cache_batch(limit: int = 5000, stock_code: str | None = None) -> dict:
    """批量把 reports_cache 里的研报加工成可回测信号。"""
    _ensure_tables()
    code = _to_code(stock_code) if stock_code else ""
    where = ["stock_code IS NOT NULL", "stock_code!=''", "report_date IS NOT NULL", "report_date!=''"]
    params: list = []
    if code:
        where.append("stock_code=?")
        params.append(code)
    params.append(max(1, min(int(limit or 5000), 20000)))
    rows = query_all(
        f"""
        SELECT *
        FROM reports_cache
        WHERE {' AND '.join(where)}
        ORDER BY report_date DESC, id DESC
        LIMIT ?
        """,
        tuple(params),
    )
    summary = {
        "status": "success",
        "scope": "single" if code else "batch_cache",
        "stock_code": code or None,
        "rows_checked": len(rows),
        "signals_processed": 0,
        "signals_skipped": 0,
        "skip_reasons": {},
        "by_source": {},
        "broker_tiers": {},
    }
    for r in rows:
        source = str(r.get("data_source") or "reports_cache")
        summary["by_source"][source] = summary["by_source"].get(source, 0) + 1
        signal_row = _report_cache_to_signal_row(r)
        tier, _ = _broker_tier(signal_row.get("org_name") or "")
        summary["broker_tiers"][tier] = summary["broker_tiers"].get(tier, 0) + 1
        saved = _save_one_report_signal(signal_row.get("stock_code"), signal_row, save_report_cache=False)
        if saved.get("saved"):
            summary["signals_processed"] += saved.get("signals_saved") or 1
        else:
            summary["signals_skipped"] += 1
            reason = saved.get("reason") or "unknown"
            summary["skip_reasons"][reason] = summary["skip_reasons"].get(reason, 0) + 1
    return summary


def refresh_research_report_quality_scores(limit: int = 5000, stock_code: str | None = None) -> dict:
    """批量加工缓存研报、反测 20/60 日表现，并刷新作者/团队等级。"""
    batch = process_reports_cache_batch(limit=limit, stock_code=stock_code)
    backtest = evaluate_research_report_outcomes(limit=limit)
    return {
        "status": "success",
        "batch": batch,
        "backtest": backtest,
        "note": "已按券商层级、样本数、命中率、收益/超额收益刷新团队/作者质量等级",
    }


def collect_and_process_tushare_reports(stock_code: str | None = None,
                                        max_stocks: int = 2,
                                        months: int = 24,
                                        fetch_live: bool = True,
                                        process_cached: bool = True,
                                        min_interval_seconds: int = 370) -> dict:
    """采集并加工 Tushare report_rc。

    fetch_live=True 时会尊重 min_interval_seconds，避免触发付费接口限频。
    process_cached=True 时会先加工已有深度画像里的 report_rc 数据。
    """
    _ensure_tables()
    start_ts = time.time()
    targets = _get_targets(stock_code, max_stocks=max_stocks)
    summary = {
        "status": "success",
        "scope": "single" if stock_code else "target_pool",
        "targets": [],
        "live_calls": 0,
        "cached_reports_processed": 0,
        "reports_saved": 0,
        "signals_saved": 0,
        "errors": [],
        "rules": {
            "priority": "positions > watchlist > interaction",
            "max_stocks": max_stocks,
            "months": months,
            "fetch_live": fetch_live,
            "process_cached": process_cached,
            "min_interval_seconds": min_interval_seconds,
        },
    }

    for idx, target in enumerate(targets):
        code = _to_code(target.get("stock_code") or target.get("code"))
        if not code:
            continue
        item = {"stock_code": code, "stock_name": target.get("stock_name") or target.get("name") or _lookup_name(code),
                "cached": 0, "live": 0, "reports_saved": 0, "signals_saved": 0, "errors": []}

        rows = []
        if process_cached:
            cached = _cached_report_rows(code)
            rows.extend(cached)
            item["cached"] = len(cached)
            summary["cached_reports_processed"] += len(cached)

        if fetch_live:
            last = _latest_report_call()
            wait = min_interval_seconds - _seconds_since((last or {}).get("called_at"))
            if wait > 0:
                # 手动单股时不睡太久，直接提示冷却；调度任务会自然在下一轮继续。
                item["errors"].append(f"report_rc 冷却中，还需约 {int(wait)} 秒")
            else:
                live_rows, err = _fetch_report_rc_live(code, months=months)
                summary["live_calls"] += 1
                item["live"] = len(live_rows)
                rows.extend(live_rows)
                if err:
                    item["errors"].append(err)
                    summary["errors"].append(f"{code}: {err}")

        seen = set()
        for r in rows:
            key = (str(r.get("ts_code") or code), str(r.get("report_date") or r.get("ann_date")),
                   str(r.get("org_name") or r.get("broker")), str(r.get("report_title") or r.get("title")))
            if key in seen:
                continue
            seen.add(key)
            saved = _save_one_report_signal(code, r)
            if saved.get("saved"):
                item["reports_saved"] += saved.get("reports_saved") or 0
                item["signals_saved"] += saved.get("signals_saved") or 0

        summary["reports_saved"] += item["reports_saved"]
        summary["signals_saved"] += item["signals_saved"]
        summary["targets"].append(item)

    duration = time.time() - start_ts
    status = "success" if not summary["errors"] else "partial"
    if not targets:
        status = "failed"
        summary["errors"].append("无目标股票")
    with db() as c:
        rid = c.execute(
            """
            INSERT INTO tushare_report_runs(scope, status, target_count, live_calls,
                                            cached_reports_processed, reports_saved,
                                            signals_saved, duration_seconds, summary_json, error_msg)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                summary["scope"], status, len(targets), summary["live_calls"],
                summary["cached_reports_processed"], summary["reports_saved"],
                summary["signals_saved"], duration, _json(summary),
                "; ".join(summary["errors"])[:1000],
            ),
        ).lastrowid
    summary.update({"status": status, "run_id": rid, "target_count": len(targets),
                    "duration_seconds": duration})
    return summary


def get_research_report_signals(stock_code: str, limit: int = 20) -> dict:
    _ensure_tables()
    code = _to_code(stock_code)
    if not code:
        return {"success": False, "error": "缺少有效股票代码"}
    rows = query_all(
        """
        SELECT stock_code, stock_name, report_date, org_name, author_name, report_title,
               report_type, classify, quarter, rating, target_price, eps, pe,
               net_profit, roe, op_revenue, op_profit, signal_score, broker_tier,
               broker_weight, weighted_signal_score, stance, entry_trade_date, entry_close,
               return_20d, return_60d, peer_excess_20d, peer_excess_60d,
               hit_20d, hit_60d, key_points, created_at
        FROM research_report_signals
        WHERE stock_code=?
        ORDER BY report_date DESC, created_at DESC
        LIMIT ?
        """,
        (code, max(1, min(limit, 100))),
    )
    for r in rows:
        try:
            r["key_points"] = json.loads(r.get("key_points") or "[]")
        except Exception:
            r["key_points"] = []

    stance_counts = {}
    brokers = {}
    latest_date = None
    for r in rows:
        stance_counts[r.get("stance") or "unknown"] = stance_counts.get(r.get("stance") or "unknown", 0) + 1
        broker = r.get("org_name") or "unknown"
        brokers[broker] = brokers.get(broker, 0) + 1
        latest_date = latest_date or r.get("report_date")
    avg_score = None
    scores = [r.get("signal_score") for r in rows if r.get("signal_score") is not None]
    if scores:
        avg_score = sum(scores) / len(scores)

    author_performance = []
    seen_authors = set()
    for r in rows:
        key = (r.get("org_name") or "", r.get("author_name") or "")
        if not key[0] or key in seen_authors:
            continue
        seen_authors.add(key)
        stats = query_all(
            """
            SELECT org_name, author_name, broker_tier, horizon_days, total_reports,
                   hit_count, hit_rate, avg_return, avg_excess_return,
                   avg_weighted_score, quality_score, grade, sample_level,
                   last_report_date
            FROM research_author_stats
            WHERE org_name=? AND author_name=?
            ORDER BY horizon_days
            """,
            key,
        )
        if stats:
            author_performance.append({"org_name": key[0], "author_name": key[1], "stats": stats})

    return {
        "success": True,
        "stock_code": code,
        "stock_name": (rows[0].get("stock_name") if rows else _lookup_name(code)),
        "count": len(rows),
        "latest_report_date": latest_date,
        "stance_counts": stance_counts,
        "broker_counts": brokers,
        "avg_signal_score": avg_score,
        "author_performance": author_performance[:10],
        "items": rows,
    }


def _backfill_broker_metadata() -> int:
    """给历史信号补齐券商层级和加权分，尤其是中金/中信的高权重。"""
    _ensure_tables()
    rows = query_all(
        "SELECT id, org_name, signal_score FROM research_report_signals "
        "WHERE broker_tier IS NULL OR broker_weight IS NULL OR weighted_signal_score IS NULL "
        "   OR (org_name LIKE '%中金%' AND broker_tier!='S') "
        "   OR (org_name LIKE '%中信证券%' AND broker_tier!='S')"
    )
    with db() as c:
        for r in rows:
            tier, weight = _broker_tier(r.get("org_name") or "")
            score = r.get("signal_score")
            weighted = (float(score) * weight) if score is not None else None
            c.execute(
                "UPDATE research_report_signals SET broker_tier=?, broker_weight=?, weighted_signal_score=? WHERE id=?",
                (tier, weight, weighted, r.get("id")),
            )
    return len(rows)


def evaluate_research_report_outcomes(limit: int = 500) -> dict:
    """用研报发布日期后的 20/60 个交易日表现反测券商/作者命中率。"""
    _ensure_tables()
    metadata_backfilled = _backfill_broker_metadata()
    rows = query_all(
        """
        SELECT * FROM research_report_signals
        WHERE report_date IS NOT NULL
        ORDER BY report_date DESC
        LIMIT ?
        """,
        (max(1, min(limit, 5000)),),
    )
    updated = 0
    horizons = (20, 60)
    for r in rows:
        code = _to_code(r.get("stock_code"))
        entry = _first_trade_on_or_after(code, r.get("report_date"))
        if not entry or not entry.get("close"):
            continue
        updates = {
            "entry_trade_date": entry.get("trade_date"),
            "entry_close": entry.get("close"),
        }
        for h in horizons:
            end = _trade_after_n(code, entry.get("trade_date"), h)
            if not end or not end.get("close"):
                continue
            try:
                ret = (float(end["close"]) - float(entry["close"])) / float(entry["close"]) * 100
            except Exception:
                continue
            peer = _peer_return(code, entry.get("trade_date"), end.get("trade_date"))
            excess = ret - peer if peer is not None else None
            hit = _is_hit(r.get("stance"), ret, excess)
            updates[f"return_{h}d"] = ret
            updates[f"peer_excess_{h}d"] = excess
            updates[f"hit_{h}d"] = hit
        if len(updates) <= 2:
            continue
        with db() as c:
            c.execute(
                """
                UPDATE research_report_signals
                SET entry_trade_date=?,
                    entry_close=?,
                    return_20d=COALESCE(?, return_20d),
                    return_60d=COALESCE(?, return_60d),
                    peer_excess_20d=COALESCE(?, peer_excess_20d),
                    peer_excess_60d=COALESCE(?, peer_excess_60d),
                    hit_20d=COALESCE(?, hit_20d),
                    hit_60d=COALESCE(?, hit_60d),
                    evaluated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (
                    updates.get("entry_trade_date"), updates.get("entry_close"),
                    updates.get("return_20d"), updates.get("return_60d"),
                    updates.get("peer_excess_20d"), updates.get("peer_excess_60d"),
                    updates.get("hit_20d"), updates.get("hit_60d"), r.get("id"),
                ),
            )
        updated += 1

    stats_updated = _rebuild_author_stats()
    return {"status": "success", "signals_checked": len(rows), "signals_updated": updated,
            "metadata_backfilled": metadata_backfilled,
            "author_stats_updated": stats_updated}


def _rebuild_author_stats() -> int:
    _ensure_tables()
    total = 0
    for horizon in (20, 60):
        hit_col = f"hit_{horizon}d"
        ret_col = f"return_{horizon}d"
        excess_col = f"peer_excess_{horizon}d"
        rows = query_all(
            f"""
            SELECT org_name, author_name,
                   COALESCE(broker_tier, 'B') as broker_tier,
                   COUNT(*) as total_reports,
                   SUM(CASE WHEN {hit_col}=1 THEN 1 ELSE 0 END) as hit_count,
                   AVG({ret_col}) as avg_return,
                   AVG({excess_col}) as avg_excess_return,
                   AVG(weighted_signal_score) as avg_weighted_score,
                   MAX(report_date) as last_report_date
            FROM research_report_signals
            WHERE {hit_col} IS NOT NULL
              AND org_name IS NOT NULL AND org_name!=''
            GROUP BY org_name, author_name
            """
        )
        with db() as c:
            for r in rows:
                total_reports = int(r.get("total_reports") or 0)
                hits = int(r.get("hit_count") or 0)
                hit_rate = hits / total_reports if total_reports else None
                tier, _ = _broker_tier(r.get("org_name") or "")
                quality_score, grade, sample_level = _quality_grade(
                    total_reports=total_reports,
                    hit_rate=hit_rate,
                    avg_return=r.get("avg_return"),
                    avg_excess_return=r.get("avg_excess_return"),
                    avg_weighted_score=r.get("avg_weighted_score"),
                    broker_tier=tier,
                )
                c.execute(
                    """
                    INSERT INTO research_author_stats
                    (org_name, author_name, broker_tier, horizon_days, total_reports,
                     hit_count, hit_rate, avg_return, avg_excess_return,
                     avg_weighted_score, quality_score, grade, sample_level,
                     last_report_date, updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                    ON CONFLICT(org_name, author_name, horizon_days) DO UPDATE SET
                      broker_tier=excluded.broker_tier,
                      total_reports=excluded.total_reports,
                      hit_count=excluded.hit_count,
                      hit_rate=excluded.hit_rate,
                      avg_return=excluded.avg_return,
                      avg_excess_return=excluded.avg_excess_return,
                      avg_weighted_score=excluded.avg_weighted_score,
                      quality_score=excluded.quality_score,
                      grade=excluded.grade,
                      sample_level=excluded.sample_level,
                      last_report_date=excluded.last_report_date,
                      updated_at=CURRENT_TIMESTAMP
                    """,
                    (
                        r.get("org_name"), r.get("author_name") or "",
                        tier, horizon, total_reports, hits,
                        hit_rate, r.get("avg_return"), r.get("avg_excess_return"),
                        r.get("avg_weighted_score"), quality_score, grade, sample_level,
                        r.get("last_report_date"),
                    ),
                )
                total += 1
    return total


def get_research_author_performance(horizon_days: int = 60, min_reports: int = 1,
                                    limit: int = 30) -> dict:
    _ensure_tables()
    horizon = 60 if int(horizon_days or 60) >= 60 else 20
    rows = query_all(
        """
        SELECT org_name, author_name, broker_tier, horizon_days, total_reports,
               hit_count, hit_rate, avg_return, avg_excess_return,
               avg_weighted_score, quality_score, grade, sample_level,
               last_report_date, updated_at
        FROM research_author_stats
        WHERE horizon_days=? AND total_reports>=?
        ORDER BY
          CASE grade WHEN 'S' THEN 0 WHEN 'A' THEN 1 WHEN 'B' THEN 2 WHEN 'C' THEN 3 ELSE 4 END,
          quality_score DESC,
          CASE broker_tier WHEN 'S' THEN 0 WHEN 'A' THEN 1 ELSE 2 END,
          hit_rate DESC,
          total_reports DESC,
          avg_excess_return DESC
        LIMIT ?
        """,
        (horizon, max(1, int(min_reports or 1)), max(1, min(limit, 100))),
    )
    return {"success": True, "horizon_days": horizon, "min_reports": min_reports,
            "count": len(rows), "items": rows}


def get_latest_tushare_report_run() -> Optional[dict]:
    _ensure_tables()
    row = query_one("SELECT * FROM tushare_report_runs ORDER BY id DESC LIMIT 1")
    if row and row.get("summary_json"):
        try:
            row["summary"] = json.loads(row["summary_json"])
        except Exception:
            pass
    return row
