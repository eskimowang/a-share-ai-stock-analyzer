"""推荐来源记忆与反测服务。

目标：
1. 把热股月采、荐股矩阵、用户手动输入的推荐沉淀成轻量记忆。
2. 只保存蒸馏后的推荐条目和表现指标，不保存大段原文，节省硬盘。
3. 反测来源/批次/个股在 5/20/60 日后的收益，供后续荐股和单股分析加权。
"""
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Optional

from ..db import db, query_all, query_one

log = logging.getLogger(__name__)

STOCK_CODE_RE = re.compile(r"(?<!\d)([0-9]{6})(?!\d)")
DEFAULT_HORIZONS = (5, 20, 60)


def _ensure_tables():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS recommendation_sources (
            source_key TEXT PRIMARY KEY,
            source_name TEXT,
            source_type TEXT,
            priority REAL DEFAULT 1.0,
            first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            total_recommended INTEGER DEFAULT 0,
            hit_rate_5d REAL,
            avg_return_5d REAL,
            hit_rate_20d REAL,
            avg_return_20d REAL,
            hit_rate_60d REAL,
            avg_return_60d REAL,
            score REAL DEFAULT 50,
            notes TEXT
        );
        CREATE TABLE IF NOT EXISTS recommendation_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_key TEXT NOT NULL,
            source_name TEXT,
            batch_id TEXT,
            recommendation_date TEXT,
            stock_code TEXT NOT NULL,
            stock_name TEXT,
            industry TEXT,
            rank INTEGER,
            reason TEXT,
            conviction REAL,
            horizon_days INTEGER DEFAULT 60,
            status TEXT DEFAULT 'tracking',
            base_trade_date TEXT,
            base_close REAL,
            raw_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source_key, batch_id, stock_code)
        );
        CREATE TABLE IF NOT EXISTS recommendation_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL,
            horizon_days INTEGER NOT NULL,
            reviewed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            base_trade_date TEXT,
            base_close REAL,
            check_trade_date TEXT,
            check_close REAL,
            return_pct REAL,
            max_gain_pct REAL,
            max_drawdown_pct REAL,
            outcome TEXT,
            UNIQUE(item_id, horizon_days)
        );
        CREATE INDEX IF NOT EXISTS idx_recommendation_items_code
          ON recommendation_items(stock_code, recommendation_date DESC);
        CREATE INDEX IF NOT EXISTS idx_recommendation_items_source
          ON recommendation_items(source_key, recommendation_date DESC);
        CREATE INDEX IF NOT EXISTS idx_recommendation_reviews_item
          ON recommendation_reviews(item_id, horizon_days);
        """)


def _to_code(value) -> str:
    m = STOCK_CODE_RE.search(str(value or ""))
    return m.group(1) if m else ""


def _json(data) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)[:4000]


def _norm_date(value=None) -> str:
    if not value:
        return datetime.now().strftime("%Y-%m-%d")
    s = str(value).strip()
    if len(s) >= 10 and "-" in s:
        return s[:10]
    if len(s) >= 8 and s[:8].isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return datetime.now().strftime("%Y-%m-%d")


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
        suffix = ".SH" if code.startswith(("6", "9")) else ".SZ"
        row = query_one(
            "SELECT name FROM stock_universe WHERE symbol=? OR ts_code=? LIMIT 1",
            (code, f"{code}.{suffix}"),
        )
        if row and row.get("name"):
            return row["name"]
    except Exception:
        pass
    return ""


def _quote_on_or_after(code: str, date_str: str) -> Optional[dict]:
    return query_one(
        "SELECT trade_date, close FROM daily_quotes "
        "WHERE stock_code=? AND trade_date>=? AND close IS NOT NULL "
        "ORDER BY trade_date ASC LIMIT 1",
        (code, date_str),
    )


def _latest_quote_before_or_on(code: str, date_str: str) -> Optional[dict]:
    return query_one(
        "SELECT trade_date, close FROM daily_quotes "
        "WHERE stock_code=? AND trade_date<=? AND close IS NOT NULL "
        "ORDER BY trade_date DESC LIMIT 1",
        (code, date_str),
    )


def _latest_quote(code: str) -> Optional[dict]:
    return query_one(
        "SELECT trade_date, close FROM daily_quotes "
        "WHERE stock_code=? AND close IS NOT NULL ORDER BY trade_date DESC LIMIT 1",
        (code,),
    )


def _window_stats(code: str, start_date: str, end_date: str, base_close: float) -> dict:
    rows = query_all(
        "SELECT trade_date, close FROM daily_quotes "
        "WHERE stock_code=? AND trade_date>=? AND trade_date<=? AND close IS NOT NULL "
        "ORDER BY trade_date ASC",
        (code, start_date, end_date),
    )
    if not rows or not base_close:
        return {"max_gain_pct": None, "max_drawdown_pct": None}
    closes = [float(r["close"]) for r in rows if r.get("close") is not None]
    if not closes:
        return {"max_gain_pct": None, "max_drawdown_pct": None}
    max_gain = (max(closes) - base_close) / base_close * 100
    max_drawdown = (min(closes) - base_close) / base_close * 100
    return {"max_gain_pct": max_gain, "max_drawdown_pct": max_drawdown}


def _source_key(value: str) -> str:
    value = (value or "manual").strip().lower()
    value = re.sub(r"[^a-z0-9_\-]+", "_", value)
    return value.strip("_") or "manual"


def record_recommendation_batch(source_key: str,
                                source_name: str,
                                items: list[dict],
                                source_type: str = "manual",
                                batch_id: str | None = None,
                                recommendation_date: str | None = None,
                                default_horizon_days: int = 60,
                                context: Optional[dict] = None) -> dict:
    """记录一批推荐条目，自动去重。"""
    _ensure_tables()
    source_key = _source_key(source_key)
    source_name = source_name or source_key
    recommendation_date = _norm_date(recommendation_date)
    batch_id = batch_id or f"{source_key}:{recommendation_date}"
    context = context or {}
    saved = updated = skipped = 0

    with db() as c:
        c.execute(
            """
            INSERT INTO recommendation_sources(source_key, source_name, source_type, notes)
            VALUES (?,?,?,?)
            ON CONFLICT(source_key) DO UPDATE SET
              source_name=excluded.source_name,
              source_type=excluded.source_type,
              last_seen_at=CURRENT_TIMESTAMP,
              notes=COALESCE(excluded.notes, recommendation_sources.notes)
            """,
            (source_key, source_name, source_type, _json(context) if context else None),
        )

        for idx, item in enumerate(items or [], start=1):
            code = _to_code(item.get("stock_code") or item.get("code") or item.get("ts_code"))
            if not code:
                skipped += 1
                continue
            name = (item.get("stock_name") or item.get("name") or _lookup_name(code) or "")[:80]
            reason = (
                item.get("reason") or item.get("logic") or item.get("core_logic")
                or item.get("why") or ""
            )
            reason = str(reason)[:600]
            industry = str(item.get("industry") or "")[:80]
            rank = item.get("rank") or idx
            try:
                rank = int(rank)
            except Exception:
                rank = idx
            try:
                conviction = float(item.get("conviction")) if item.get("conviction") is not None else None
            except Exception:
                conviction = None

            base = _quote_on_or_after(code, recommendation_date) or _latest_quote(code) or {}
            base_close = base.get("close")
            base_date = base.get("trade_date")

            cur = c.execute(
                """
                INSERT INTO recommendation_items
                (source_key, source_name, batch_id, recommendation_date, stock_code, stock_name,
                 industry, rank, reason, conviction, horizon_days, base_trade_date, base_close, raw_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source_key, batch_id, stock_code) DO UPDATE SET
                  stock_name=COALESCE(NULLIF(excluded.stock_name, ''), recommendation_items.stock_name),
                  industry=COALESCE(NULLIF(excluded.industry, ''), recommendation_items.industry),
                  rank=excluded.rank,
                  reason=COALESCE(NULLIF(excluded.reason, ''), recommendation_items.reason),
                  conviction=COALESCE(excluded.conviction, recommendation_items.conviction),
                  base_trade_date=COALESCE(excluded.base_trade_date, recommendation_items.base_trade_date),
                  base_close=COALESCE(excluded.base_close, recommendation_items.base_close),
                  raw_json=excluded.raw_json
                """,
                (
                    source_key, source_name, batch_id, recommendation_date, code, name,
                    industry, rank, reason, conviction, default_horizon_days,
                    base_date, base_close, _json(item),
                ),
            )
            if cur.rowcount == 1:
                saved += 1
            else:
                updated += 1

    with db() as c:
        total = c.execute(
            "SELECT COUNT(*) AS n FROM recommendation_items WHERE source_key=?",
            (source_key,),
        ).fetchone()
        c.execute(
            "UPDATE recommendation_sources SET total_recommended=?, last_seen_at=CURRENT_TIMESTAMP WHERE source_key=?",
            ((total["n"] if total else 0), source_key),
        )
    _refresh_source_scores()
    return {
        "status": "success",
        "source_key": source_key,
        "source_name": source_name,
        "batch_id": batch_id,
        "recommendation_date": recommendation_date,
        "saved_or_touched": saved,
        "updated": updated,
        "skipped": skipped,
        "items": len(items or []),
    }


def review_recommendation_memory(horizons: tuple[int, ...] = DEFAULT_HORIZONS,
                                 limit: int = 1000) -> dict:
    """按 5/20/60 日窗口反测推荐表现。"""
    _ensure_tables()
    today = datetime.now().date()
    rows = query_all(
        "SELECT * FROM recommendation_items "
        "WHERE status!='ignored' ORDER BY recommendation_date DESC, id DESC LIMIT ?",
        (limit,),
    )
    checked = updated = skipped_future = skipped_no_price = 0
    with db() as c:
        for item in rows:
            code = item["stock_code"]
            rec_date = _norm_date(item.get("recommendation_date"))
            base_date = item.get("base_trade_date")
            base_close = item.get("base_close")
            if not base_date or not base_close:
                base = _quote_on_or_after(code, rec_date) or _latest_quote(code)
                if not base or not base.get("close"):
                    skipped_no_price += 1
                    continue
                base_date = base.get("trade_date")
                base_close = float(base.get("close"))
                c.execute(
                    "UPDATE recommendation_items SET base_trade_date=?, base_close=? WHERE id=?",
                    (base_date, base_close, item["id"]),
                )
            else:
                base_close = float(base_close)

            for h in horizons:
                target = (datetime.strptime(rec_date, "%Y-%m-%d").date() + timedelta(days=h))
                if today < target:
                    skipped_future += 1
                    continue
                target_s = target.strftime("%Y-%m-%d")
                check = _latest_quote_before_or_on(code, target_s) or _latest_quote(code)
                if not check or not check.get("close"):
                    skipped_no_price += 1
                    continue
                check_close = float(check["close"])
                return_pct = (check_close - base_close) / base_close * 100 if base_close else None
                stats = _window_stats(code, base_date, check["trade_date"], base_close)
                if return_pct is None:
                    outcome = "unknown"
                elif return_pct >= 5:
                    outcome = "hit"
                elif return_pct <= -5:
                    outcome = "miss"
                else:
                    outcome = "neutral"

                c.execute(
                    """
                    INSERT INTO recommendation_reviews
                    (item_id, horizon_days, base_trade_date, base_close, check_trade_date,
                     check_close, return_pct, max_gain_pct, max_drawdown_pct, outcome)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(item_id, horizon_days) DO UPDATE SET
                      reviewed_at=CURRENT_TIMESTAMP,
                      base_trade_date=excluded.base_trade_date,
                      base_close=excluded.base_close,
                      check_trade_date=excluded.check_trade_date,
                      check_close=excluded.check_close,
                      return_pct=excluded.return_pct,
                      max_gain_pct=excluded.max_gain_pct,
                      max_drawdown_pct=excluded.max_drawdown_pct,
                      outcome=excluded.outcome
                    """,
                    (
                        item["id"], h, base_date, base_close, check["trade_date"],
                        check_close, return_pct, stats.get("max_gain_pct"),
                        stats.get("max_drawdown_pct"), outcome,
                    ),
                )
                checked += 1
                updated += 1
    sources = _refresh_source_scores()
    return {
        "status": "success",
        "items_scanned": len(rows),
        "reviews_checked": checked,
        "reviews_updated": updated,
        "skipped_future": skipped_future,
        "skipped_no_price": skipped_no_price,
        "sources_updated": sources,
    }


def _refresh_source_scores() -> int:
    _ensure_tables()
    stats = query_all(
        """
        SELECT i.source_key, r.horizon_days,
               COUNT(*) AS n,
               AVG(CASE WHEN r.return_pct > 0 THEN 1.0 ELSE 0.0 END) AS hit_rate,
               AVG(r.return_pct) AS avg_return
        FROM recommendation_reviews r
        JOIN recommendation_items i ON i.id=r.item_id
        GROUP BY i.source_key, r.horizon_days
        """
    )
    by_source: dict[str, dict] = {}
    for row in stats:
        s = by_source.setdefault(row["source_key"], {})
        s[int(row["horizon_days"])] = row

    updated = 0
    with db() as c:
        for source_key, item in by_source.items():
            h5 = item.get(5) or {}
            h20 = item.get(20) or {}
            h60 = item.get(60) or {}
            hit20 = h20.get("hit_rate")
            ret20 = h20.get("avg_return")
            hit60 = h60.get("hit_rate")
            ret60 = h60.get("avg_return")
            score = 50.0
            if hit20 is not None:
                score += (float(hit20) - 0.5) * 35
            if ret20 is not None:
                score += float(ret20) * 1.2
            if hit60 is not None:
                score += (float(hit60) - 0.5) * 25
            if ret60 is not None:
                score += float(ret60) * 0.8
            score = max(0.0, min(100.0, score))
            total = query_one(
                "SELECT COUNT(*) AS n FROM recommendation_items WHERE source_key=?",
                (source_key,),
            ) or {}
            c.execute(
                """
                UPDATE recommendation_sources SET
                  total_recommended=?,
                  hit_rate_5d=?, avg_return_5d=?,
                  hit_rate_20d=?, avg_return_20d=?,
                  hit_rate_60d=?, avg_return_60d=?,
                  score=?,
                  last_seen_at=CURRENT_TIMESTAMP
                WHERE source_key=?
                """,
                (
                    total.get("n", 0),
                    h5.get("hit_rate"), h5.get("avg_return"),
                    hit20, ret20, hit60, ret60,
                    score, source_key,
                ),
            )
            updated += 1
    return updated


def list_source_performance(limit: int = 30) -> dict:
    _ensure_tables()
    rows = query_all(
        "SELECT * FROM recommendation_sources ORDER BY score DESC, total_recommended DESC LIMIT ?",
        (limit,),
    )
    return {"count": len(rows), "items": rows}


def list_recent_recommendations(source_key: str | None = None, limit: int = 80) -> dict:
    _ensure_tables()
    if source_key:
        rows = query_all(
            "SELECT * FROM recommendation_items WHERE source_key=? "
            "ORDER BY recommendation_date DESC, rank ASC LIMIT ?",
            (_source_key(source_key), limit),
        )
    else:
        rows = query_all(
            "SELECT * FROM recommendation_items ORDER BY recommendation_date DESC, rank ASC LIMIT ?",
            (limit,),
        )
    return {"count": len(rows), "items": rows}


def get_recommendation_memory_for_stock(stock_code: str, limit: int = 20) -> dict:
    _ensure_tables()
    code = _to_code(stock_code)
    if not code:
        return {"stock_code": "", "items": [], "summary": "无有效股票代码"}
    items = query_all(
        """
        SELECT i.*, s.score AS source_score, s.hit_rate_20d, s.avg_return_20d,
               s.hit_rate_60d, s.avg_return_60d
        FROM recommendation_items i
        LEFT JOIN recommendation_sources s ON s.source_key=i.source_key
        WHERE i.stock_code=?
        ORDER BY i.recommendation_date DESC, i.id DESC LIMIT ?
        """,
        (code, limit),
    )
    for item in items:
        item["reviews"] = query_all(
            "SELECT horizon_days, check_trade_date, return_pct, max_gain_pct, max_drawdown_pct, outcome "
            "FROM recommendation_reviews WHERE item_id=? ORDER BY horizon_days",
            (item["id"],),
        )
    summary = _format_stock_summary(code, items)
    return {"stock_code": code, "stock_name": _lookup_name(code), "summary": summary, "items": items}


def _format_stock_summary(code: str, items: list[dict]) -> str:
    if not items:
        return f"{code} 暂无推荐记忆。"
    parts = []
    for item in items[:5]:
        score = item.get("source_score")
        score_s = f"{float(score):.0f}" if score is not None else "NA"
        review_s = ""
        reviews = item.get("reviews") or []
        if reviews:
            last = reviews[-1]
            ret = last.get("return_pct")
            review_s = f"，{last.get('horizon_days')}日收益 {ret:+.1f}%" if ret is not None else ""
        parts.append(
            f"{item.get('recommendation_date')} {item.get('source_name')} "
            f"(源评分 {score_s}) 推荐{review_s}：{item.get('reason') or '无摘要'}"
        )
    return "\n".join(parts)


def format_recommendation_memory_for_prompt(stock_code: str | None = None, limit: int = 10) -> str:
    _ensure_tables()
    if stock_code:
        data = get_recommendation_memory_for_stock(stock_code, limit=limit)
        return "## 推荐来源记忆\n" + data.get("summary", "")

    sources = list_source_performance(limit=8).get("items") or []
    recent = list_recent_recommendations(limit=limit).get("items") or []
    lines = ["## 推荐来源记忆（用于降权/加权，不替代当前数据）"]
    if sources:
        lines.append("### 来源评分")
        for s in sources:
            lines.append(
                f"- {s.get('source_name')}：评分 {float(s.get('score') or 50):.0f}，"
                f"20日胜率 {(float(s.get('hit_rate_20d') or 0)*100):.0f}% / "
                f"20日均收益 {float(s.get('avg_return_20d') or 0):+.1f}%"
            )
    if recent:
        lines.append("### 近期推荐仍需跟踪")
        for r in recent[:limit]:
            lines.append(
                f"- {r.get('recommendation_date')} {r.get('source_name')} "
                f"{r.get('stock_code')} {r.get('stock_name') or ''}: {r.get('reason') or ''}"
            )
    if len(lines) == 1:
        lines.append("暂无历史推荐记忆。")
    return "\n".join(lines)


def backfill_latest_hot_stocks_from_reports(run_id: int | None = None,
                                            max_items: int = 300) -> dict:
    """用最近一次热股月采落库研报，回填今天/最近一批推荐记忆。

    旧版热股月采未保存完整入选列表，只能用成功落库研报的个股做蒸馏回填。
    """
    _ensure_tables()
    if run_id:
        run = query_one("SELECT * FROM premium_report_runs WHERE id=? AND scope='hot_stocks'", (run_id,))
    else:
        run = query_one(
            "SELECT * FROM premium_report_runs WHERE scope='hot_stocks' AND status='success' "
            "ORDER BY id DESC LIMIT 1"
        )
    if not run:
        return {"status": "failed", "error": "没有找到成功的热股月采记录"}

    rows = query_all(
        """
        SELECT stock_code, COUNT(*) AS report_count,
               MAX(report_date) AS latest_report_date,
               GROUP_CONCAT(DISTINCT broker) AS brokers
        FROM reports_cache
        WHERE data_source='codex_premium'
          AND fetched_at >= datetime(?, '-10 minutes')
          AND fetched_at <= datetime(?, '+' || ? || ' seconds')
        GROUP BY stock_code
        ORDER BY report_count DESC, latest_report_date DESC
        LIMIT ?
        """,
        (
            run["run_at"],
            run["run_at"],
            int((run.get("duration_seconds") or 0) + 1800),
            max_items,
        ),
    )
    if not rows:
        rows = query_all(
            """
            SELECT stock_code, COUNT(*) AS report_count,
                   MAX(report_date) AS latest_report_date,
                   GROUP_CONCAT(DISTINCT broker) AS brokers
            FROM reports_cache
            WHERE data_source='codex_premium'
            GROUP BY stock_code
            ORDER BY MAX(fetched_at) DESC, report_count DESC
            LIMIT ?
            """,
            (max_items,),
        )
    items = []
    for idx, r in enumerate(rows, start=1):
        code = _to_code(r.get("stock_code"))
        if not code:
            continue
        items.append({
            "code": code,
            "name": _lookup_name(code),
            "rank": idx,
            "reason": (
                f"热股月采后被 {r.get('brokers') or '中信/中金'} 覆盖，"
                f"落库研报 {r.get('report_count')} 份，最近研报 {r.get('latest_report_date') or '未知'}"
            ),
            "report_count": r.get("report_count"),
            "brokers": r.get("brokers"),
        })
    return record_recommendation_batch(
        source_key="hot_stocks_monthly",
        source_name="热股月采",
        source_type="monthly_hot_stocks",
        items=items,
        batch_id=f"premium_report_runs:{run['id']}:backfill",
        recommendation_date=run["run_at"][:10],
        default_horizon_days=60,
        context={"run_id": run["id"], "backfill": "reports_cache_distilled"},
    )
