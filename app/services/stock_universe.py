"""A 股全生命周期股池 —— 含退市/ST/停牌状态。

用途:
  - discovery: 选股时过滤不可交易（已退市 / 停牌 / ST）
  - backtest: 历史回测时按日动态可交易性（防止幸存者偏差）
  - broker_study: 热股筛选避免选到 ST
"""
import logging
from datetime import datetime
from typing import Optional

from ..config import CONFIG
from ..db import db, execute, query_all, query_one
from ..data_sources.tushare_client import TushareClient

log = logging.getLogger(__name__)

_ts = None


def _get_ts():
    global _ts
    if _ts is None:
        _ts = TushareClient(CONFIG["data_sources"]["tushare"]["token"])
    return _ts


def _ensure_universe_table():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS stock_universe (
            ts_code TEXT PRIMARY KEY,
            symbol TEXT,
            name TEXT,
            area TEXT,
            industry TEXT,
            market TEXT,
            list_date TEXT,
            delist_date TEXT,
            list_status TEXT,  -- L=上市 D=退市 P=暂停
            is_st INTEGER DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_universe_status ON stock_universe(list_status, is_st);
        """)


def sync_universe_from_tushare() -> dict:
    """拉 Tushare stock_basic 三种状态写入 stock_universe 表。"""
    _ensure_universe_table()
    ts = _get_ts()
    total = 0
    details = {}
    for status in ("L", "D", "P"):
        try:
            df = ts.pro.stock_basic(
                exchange="", list_status=status,
                fields="ts_code,symbol,name,area,industry,market,list_date,delist_date"
            )
            if df is None or df.empty:
                continue
            with db() as c:
                for _, r in df.iterrows():
                    name = r.get("name") or ""
                    # 判断 ST（名称含 ST/* / *ST）
                    is_st = 1 if ("ST" in name.upper() or name.startswith("*")) else 0
                    c.execute(
                        "INSERT OR REPLACE INTO stock_universe"
                        "(ts_code, symbol, name, area, industry, market, list_date, "
                        " delist_date, list_status, is_st) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (r.get("ts_code"), r.get("symbol"), name,
                         r.get("area"), r.get("industry"), r.get("market"),
                         r.get("list_date"), r.get("delist_date"), status, is_st),
                    )
                    total += 1
            details[status] = len(df)
            log.info(f"stock_basic list_status={status}: {len(df)} 只")
        except Exception as e:
            log.warning(f"stock_basic {status} fail: {e}")
    return {"total": total, "by_status": details}


def filter_tradable(codes: list[str], as_of_date: Optional[str] = None) -> list[str]:
    """过滤不可交易的股（退市 / 停牌 / ST）。

    as_of_date: 回测用 — 该日期时的可交易性（dele_date > as_of 的退市股此时尚可交易）。
    """
    _ensure_universe_table()
    if not codes:
        return []
    as_of_date = as_of_date or datetime.now().strftime("%Y%m%d")

    # 把 6 位代码转为 ts_code (如 600150 → 600150.SH)
    def _ts_code(c):
        return f"{c}.{'SH' if c.startswith(('6','9')) else 'SZ'}" if "." not in c else c

    ts_codes = [_ts_code(c) for c in codes]
    placeholders = ",".join("?" * len(ts_codes))
    rows = query_all(
        f"SELECT ts_code, list_status, delist_date, is_st FROM stock_universe "
        f"WHERE ts_code IN ({placeholders})",
        tuple(ts_codes),
    )
    lookup = {r["ts_code"]: r for r in rows}

    good = []
    for code, tsc in zip(codes, ts_codes):
        r = lookup.get(tsc)
        if not r:
            # 库里没有 → 保守策略：认为不可交易
            continue
        if r["is_st"]:
            continue
        if r["list_status"] == "P":  # 暂停上市
            continue
        if r["list_status"] == "D":
            # 已退市 — 如果是回测 as_of 在退市前，仍可交易
            if r.get("delist_date") and r["delist_date"] > as_of_date:
                good.append(code)
            continue
        good.append(code)
    return good


def get_universe_stats() -> dict:
    _ensure_universe_table()
    rows = query_all(
        "SELECT list_status, is_st, COUNT(*) as n FROM stock_universe GROUP BY list_status, is_st"
    )
    stats = {"total": 0}
    for r in rows:
        stats[f"{r['list_status']}_st{r['is_st']}"] = r["n"]
        stats["total"] += r["n"]
    return stats


def get_stock_info(code: str) -> Optional[dict]:
    _ensure_universe_table()
    tsc = f"{code}.{'SH' if code.startswith(('6','9')) else 'SZ'}" if "." not in code else code
    return query_one(
        "SELECT * FROM stock_universe WHERE ts_code=?", (tsc,),
    )
