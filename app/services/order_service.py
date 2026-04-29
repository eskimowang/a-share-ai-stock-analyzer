"""下单指令生成服务 —— 从 AI 分析抽出可执行的订单指令。

当前模式: 生成指令 → 微信推送 → 用户手工下单（中银证券 app）
未来: broker 适配器接入 easytrader / 券商 API 自动执行
"""
import json
import logging
import re
from datetime import datetime
from typing import Optional

from ..db import db, execute, query_all, query_one

log = logging.getLogger(__name__)


# ========== Broker 抽象接口（为将来 API 接入留出） ==========
class BrokerAdapter:
    """券商适配器接口。"""
    name = "manual"

    def place_order(self, stock_code: str, direction: str,
                     quantity: int, price: Optional[float] = None,
                     order_type: str = "limit") -> dict:
        """下单。manual 模式只记录，不真下。"""
        raise NotImplementedError


class ManualBroker(BrokerAdapter):
    """手工模式 —— 只记录指令，等用户在中银证券 app 里操作。"""
    name = "manual"

    def place_order(self, stock_code, direction, quantity, price=None, order_type="limit"):
        return {
            "broker": "manual",
            "stock_code": stock_code,
            "direction": direction,
            "quantity": quantity,
            "price": price,
            "order_type": order_type,
            "status": "pending_manual",
            "message": "请在中银证券 app 中手工执行",
        }


_BROKER = ManualBroker()


def get_broker() -> BrokerAdapter:
    return _BROKER


# ========== 订单指令表 ==========
def _ensure_order_tables():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS order_instructions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            source TEXT,            -- closing / discovery / manual
            source_run_id INTEGER,  -- 关联到 discovery_runs 或其他
            stock_code TEXT,
            stock_name TEXT,
            direction TEXT,         -- buy / sell / hold
            quantity INTEGER,
            price_hint REAL,
            reason TEXT,
            priority TEXT,          -- urgent / normal / optional
            status TEXT DEFAULT 'pending',  -- pending / executed / cancelled / expired
            executed_at TIMESTAMP,
            broker TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_orders_status ON order_instructions(status, created_at);

        CREATE TABLE IF NOT EXISTS paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            source TEXT,
            source_run_id INTEGER,
            stock_code TEXT,
            stock_name TEXT,
            direction TEXT,
            entry_price REAL,
            entry_date DATE,
            quantity INTEGER,
            exit_price REAL,
            exit_date DATE,
            pnl_pct REAL,
            hold_days INTEGER,
            closed INTEGER DEFAULT 0
        );
        """)


# ========== 从 AI 文本里抽订单 ==========
# 匹配 "002371 买入 100 股 @ 45.20" 之类
_ORDER_PATTERNS = [
    # 严格: 代码 方向 数量 @ 价格
    re.compile(
        r"(?P<code>\d{6})\s*(?:[·\-·]?\s*[一-龥A-Za-z]+)?\s*"
        r"(?P<dir>买入|卖出|加仓|减仓|清仓|持有|继续持有|不动)"
        r"(?:.{0,60}?(?P<qty>\d+)\s*股)?"
        r"(?:.{0,80}?(?:@|价格|限价|约)\s*(?P<price>\d+(?:\.\d+)?))?"
    ),
]


def extract_orders_from_text(text: str, source: str = "manual",
                              source_run_id: Optional[int] = None) -> list[dict]:
    """从一段 AI 分析文本里抽出结构化订单。失败返回 []。"""
    if not text:
        return []
    orders = []
    seen_codes = set()
    for pat in _ORDER_PATTERNS:
        for m in pat.finditer(text):
            code = m.group("code")
            if code in seen_codes:
                continue
            seen_codes.add(code)
            dir_cn = m.group("dir")
            direction_map = {
                "买入": "buy", "加仓": "buy",
                "卖出": "sell", "减仓": "sell", "清仓": "sell",
                "持有": "hold", "继续持有": "hold", "不动": "hold",
            }
            direction = direction_map.get(dir_cn, "hold")
            qty = int(m.group("qty")) if m.group("qty") else None
            price = float(m.group("price")) if m.group("price") else None
            orders.append({
                "stock_code": code,
                "direction": direction,
                "quantity": qty,
                "price_hint": price,
                "reason": dir_cn,
                "source": source,
                "source_run_id": source_run_id,
            })
    return orders


def save_orders(orders: list[dict]) -> list[int]:
    """保存订单指令到 DB，返回 id 列表。"""
    _ensure_order_tables()
    ids = []
    for o in orders:
        name = ""
        snap = query_one(
            "SELECT stock_name FROM positions WHERE stock_code=? LIMIT 1",
            (o.get("stock_code"),),
        )
        if snap:
            name = snap.get("stock_name") or ""
        rid = execute(
            "INSERT INTO order_instructions"
            "(source, source_run_id, stock_code, stock_name, direction, "
            " quantity, price_hint, reason, priority, broker) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                o.get("source"), o.get("source_run_id"),
                o.get("stock_code"), name,
                o.get("direction"),
                o.get("quantity"), o.get("price_hint"),
                (o.get("reason") or "")[:300],
                o.get("priority", "normal"),
                "manual",
            ),
        )
        ids.append(rid)
        try:
            from .decision_feedback_service import record_decision_feedback_event
            record_decision_feedback_event({
                "stage": "decision",
                "source": o.get("source") or source,
                "stock_code": o.get("stock_code"),
                "stock_name": name,
                "title": "订单指令生成",
                "summary": (o.get("reason") or "")[:500],
                "decision": o.get("direction"),
                "action": f"{o.get('direction')} {o.get('quantity') or ''} @ {o.get('price_hint') or 'market'}",
                "expected_outcome": "等待用户手工执行并进入反馈复盘",
                "status": "open",
            })
        except Exception as e:
            log.warning("记录订单闭环事件失败: %s", e)
    return ids


# ========== 纸交易：自动模拟下单 ==========
def simulate_paper_trades(orders: list[dict]):
    """把每条 buy/sell 订单登记为 paper_trade，买入价用当前收盘。"""
    _ensure_order_tables()
    from ..db import query_one as qo
    today = datetime.now().strftime("%Y-%m-%d")
    n = 0
    for o in orders:
        if o.get("direction") not in ("buy", "sell"):
            continue
        code = o.get("stock_code")
        # 拿当天收盘
        snap = qo(
            "SELECT close, trade_date FROM daily_quotes WHERE stock_code=? "
            "ORDER BY trade_date DESC LIMIT 1", (code,)
        )
        if not snap:
            continue
        entry_price = o.get("price_hint") or snap["close"]
        qty = o.get("quantity") or 100
        execute(
            "INSERT INTO paper_trades(source, source_run_id, stock_code, "
            "direction, entry_price, entry_date, quantity, closed) "
            "VALUES (?,?,?,?,?,?,?,0)",
            (o.get("source"), o.get("source_run_id"), code,
             o.get("direction"), entry_price, today, qty),
        )
        n += 1
    log.info(f"[纸交易] 登记 {n} 笔")
    return n


def close_paper_trades_by_time(days: int = 7):
    """把 N 天前开的单用当前价平仓，计算 pnl。"""
    _ensure_order_tables()
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    pending = query_all(
        "SELECT id, stock_code, entry_price, entry_date, direction, quantity "
        "FROM paper_trades WHERE closed=0 AND entry_date <= ?",
        (cutoff,),
    )
    closed_count = 0
    for p in pending:
        snap = query_one(
            "SELECT close, trade_date FROM daily_quotes WHERE stock_code=? "
            "ORDER BY trade_date DESC LIMIT 1", (p["stock_code"],)
        )
        if not snap:
            continue
        exit_price = snap["close"]
        entry = p["entry_price"]
        pnl_pct = (exit_price - entry) / entry * 100
        if p["direction"] == "sell":
            pnl_pct = -pnl_pct  # 做空视角（对于"建议卖出"的股，跌才是对）
        hold = (datetime.now().date() - datetime.strptime(p["entry_date"], "%Y-%m-%d").date()).days
        execute(
            "UPDATE paper_trades SET closed=1, exit_price=?, exit_date=?, "
            "pnl_pct=?, hold_days=? WHERE id=?",
            (exit_price, snap["trade_date"], pnl_pct, hold, p["id"]),
        )
        closed_count += 1
    log.info(f"[纸交易] 平仓 {closed_count} 笔")
    return closed_count


def get_paper_stats() -> dict:
    _ensure_order_tables()
    rows = query_all(
        "SELECT source, COUNT(*) as n, AVG(pnl_pct) as avg_pnl, "
        "SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) as wins "
        "FROM paper_trades WHERE closed=1 GROUP BY source"
    )
    all_closed = query_all(
        "SELECT pnl_pct FROM paper_trades WHERE closed=1"
    )
    total_n = len(all_closed)
    if total_n == 0:
        return {"total": 0, "by_source": rows, "message": "纸交易尚未平仓"}
    total_wins = sum(1 for r in all_closed if (r["pnl_pct"] or 0) > 0)
    avg = sum(r["pnl_pct"] or 0 for r in all_closed) / total_n
    return {
        "total": total_n, "wins": total_wins,
        "win_rate": total_wins / total_n if total_n else 0,
        "avg_pnl_pct": avg,
        "by_source": rows,
    }


def list_pending_orders(limit: int = 50) -> list[dict]:
    _ensure_order_tables()
    return query_all(
        "SELECT * FROM order_instructions WHERE status='pending' "
        "ORDER BY id DESC LIMIT ?", (limit,),
    )


def list_all_orders(limit: int = 100) -> list[dict]:
    _ensure_order_tables()
    return query_all(
        "SELECT * FROM order_instructions ORDER BY id DESC LIMIT ?", (limit,),
    )


def mark_order_executed(order_id: int, broker: str = "manual") -> bool:
    execute(
        "UPDATE order_instructions SET status='executed', "
        "executed_at=CURRENT_TIMESTAMP, broker=? WHERE id=?",
        (broker, order_id),
    )
    try:
        row = query_one("SELECT * FROM order_instructions WHERE id=?", (order_id,)) or {}
        from .decision_feedback_service import record_decision_feedback_event
        record_decision_feedback_event({
            "stage": "action",
            "source": "order_execution",
            "stock_code": row.get("stock_code") or "",
            "stock_name": row.get("stock_name") or "",
            "title": "订单已执行",
            "summary": row.get("reason") or "",
            "decision": row.get("direction") or "",
            "action": f"{row.get('direction')} {row.get('quantity') or ''} @ {row.get('price_hint') or 'manual'}",
            "expected_outcome": "后续按收益、回撤和纪律执行进入反馈",
            "status": "open",
        })
    except Exception as e:
        log.warning("记录订单执行闭环事件失败: %s", e)
    return True


def format_orders_for_wechat(orders: list[dict]) -> str:
    """订单指令排成微信 markdown 表。"""
    if not orders:
        return ""
    md = "## 🎯 今日下单指令（请在中银证券 app 手工执行）\n\n"
    md += "| 代码 | 名称 | 方向 | 数量 | 参考价 | 理由 |\n|---|---|---|---|---|---|\n"
    for o in orders:
        dir_cn = {"buy": "🟢 买", "sell": "🔴 卖", "hold": "⚪ 持"}.get(o.get("direction"), o.get("direction"))
        qty = o.get("quantity") or "—"
        price = f"{o.get('price_hint'):.2f}" if o.get("price_hint") else "市价"
        md += f"| {o.get('stock_code')} | {o.get('stock_name') or '—'} | {dir_cn} | {qty} | {price} | {(o.get('reason') or '')[:30]} |\n"
    return md
