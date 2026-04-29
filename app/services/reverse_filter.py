"""反向过滤器 —— 诱多/拉升派发等低胜率 🔴 手法命中时把股票打入短期黑名单。

V4 建议 5: 低胜率手法不应作为卖出信号（可能反向），而应作为"别追涨"的过滤器。
"""
import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from ..db import db, execute, query_all, query_one

log = logging.getLogger(__name__)

# 这些手法胜率 <40% → 反向过滤（命中就代表"别追涨")
BEARISH_PATTERNS_FOR_BLACKLIST = {
    "诱多出货": {"days": 3, "note": "短期诱多，勿追涨"},
    "拉升派发": {"days": 5, "note": "派发中，高位减仓"},
    "借利好出货": {"days": 3, "note": "利好反跌，主力在卖"},
    "假突破": {"days": 2, "note": "假突破，勿追突破价"},
    "对倒": {"days": 3, "note": "成交假繁荣，不要信任此票"},
    "尾盘偷袭": {"days": 1, "note": "尾盘拉升，次日大概率高开低走"},
}


def _ensure_blacklist_table():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS suggestion_blacklist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stock_code TEXT,
            pattern TEXT,
            confidence REAL,
            reason TEXT,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP,
            active INTEGER DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_bl_active ON suggestion_blacklist(stock_code, active, expires_at);
        """)


def update_blacklist_from_detections():
    """扫最近命中的 🔴 手法，加入短期黑名单。"""
    _ensure_blacklist_table()
    # 读最近 3 天的探测结果
    cutoff = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
    detections = query_all(
        "SELECT stock_code, pattern, confidence, trade_date FROM playbook_detections "
        "WHERE trade_date >= ? AND pattern IN (" +
        ",".join("?" * len(BEARISH_PATTERNS_FOR_BLACKLIST)) + ") "
        "AND confidence >= 0.6 ORDER BY trade_date DESC",
        (cutoff, *BEARISH_PATTERNS_FOR_BLACKLIST.keys()),
    )
    added = 0
    for d in detections:
        rule = BEARISH_PATTERNS_FOR_BLACKLIST.get(d["pattern"])
        if not rule:
            continue
        expires = (datetime.now() + timedelta(days=rule["days"])).strftime("%Y-%m-%d %H:%M:%S")
        # 去重：同股同手法 24 小时内不重复
        existing = query_one(
            "SELECT id FROM suggestion_blacklist WHERE stock_code=? AND pattern=? "
            "AND active=1 AND added_at > datetime('now', '-1 day')",
            (d["stock_code"], d["pattern"]),
        )
        if existing:
            continue
        execute(
            "INSERT INTO suggestion_blacklist"
            "(stock_code, pattern, confidence, reason, expires_at) VALUES (?,?,?,?,?)",
            (d["stock_code"], d["pattern"], d["confidence"],
             rule["note"], expires),
        )
        added += 1

    # 清理过期
    execute(
        "UPDATE suggestion_blacklist SET active=0 "
        "WHERE active=1 AND expires_at < datetime('now')"
    )
    return {"added": added}


def is_blacklisted(code: str) -> dict:
    """查某股当前黑名单状态。返回 {blacklisted:bool, reasons:[{pattern, note}]}"""
    _ensure_blacklist_table()
    rows = query_all(
        "SELECT pattern, reason, confidence, expires_at FROM suggestion_blacklist "
        "WHERE stock_code=? AND active=1 AND expires_at > datetime('now') "
        "ORDER BY added_at DESC",
        (code,),
    )
    return {
        "blacklisted": len(rows) > 0,
        "count": len(rows),
        "reasons": rows,
    }


def annotate_advice_with_blacklist(code: str, advice: str) -> str:
    """在建议文本前加黑名单警告。"""
    bl = is_blacklisted(code)
    if not bl["blacklisted"]:
        return advice
    warn = "⛔ 黑名单警告:"
    for r in bl["reasons"][:2]:
        warn += f" [{r['pattern']}·{int(r['confidence']*100)}%→{r['reason']}]"
    return f"{warn}\n{advice}"


def get_all_blacklist() -> list[dict]:
    _ensure_blacklist_table()
    return query_all(
        "SELECT * FROM suggestion_blacklist WHERE active=1 AND expires_at > datetime('now') "
        "ORDER BY added_at DESC"
    )
