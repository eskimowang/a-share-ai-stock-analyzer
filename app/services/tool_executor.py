"""Function Calling 工具执行器 —— 三级权限，白名单，审计日志。

安全原则:
- 🟢 只读: 直接执行
- 🟡 写入: 需要用户 PWA 弹窗确认
- 🔴 破坏: 需要确认 + 10 秒倒计时
- 所有调用进 tool_audit 日志表
"""
import inspect
import json
import logging
import re
from datetime import datetime
from typing import Any, Optional

from ..db import db, query_all, query_one, execute

log = logging.getLogger(__name__)


# ---------- 工具级别定义 ----------
READ_ONLY = "read"
WRITE = "write"
DESTRUCTIVE = "destructive"


def _tool(level: str, description: str):
    def decorator(fn):
        fn.__tool_level__ = level
        fn.__tool_desc__ = description
        return fn
    return decorator


STOCK_CODE_RE = re.compile(r"(?<!\d)([0-9]{6})(?!\d)")


def _normalize_stock_code(value) -> str:
    m = STOCK_CODE_RE.search(str(value or ""))
    return m.group(1) if m else ""


def _lookup_stock_name(stock_code: str) -> str:
    for table in ("positions", "watchlist"):
        try:
            row = query_one(
                f"SELECT stock_name FROM {table} WHERE stock_code=? AND stock_name IS NOT NULL LIMIT 1",
                (stock_code,),
            )
            if row and row.get("stock_name"):
                return row["stock_name"]
        except Exception:
            pass
    try:
        suffix = ".SH" if stock_code.startswith(("6", "9")) else ".SZ"
        row = query_one(
            "SELECT name FROM stock_universe WHERE symbol=? OR ts_code=? LIMIT 1",
            (stock_code, f"{stock_code}{suffix}"),
        )
        if row and row.get("name"):
            return row["name"]
    except Exception:
        pass
    return ""


# ---------- 🟢 只读工具 ----------
@_tool(READ_ONLY, "查询所有持仓及浮盈浮亏")
def query_positions() -> list[dict]:
    return query_all(
        "SELECT p.id, p.stock_code, p.stock_name, "
        "SUM(CASE WHEN t.trade_type='buy' THEN t.quantity ELSE -t.quantity END) as qty, "
        "SUM(CASE WHEN t.trade_type='buy' THEN t.price*t.quantity+t.fee ELSE -t.price*t.quantity+t.fee END) as cost "
        "FROM positions p JOIN trades t ON p.id=t.position_id "
        "WHERE p.status='holding' GROUP BY p.id"
    )


@_tool(READ_ONLY, "查询最近手动提交的持仓变动")
def query_holding_changes(stock_code: str = "", limit: int = 30) -> dict:
    from .holding_change_service import list_holding_changes, current_holdings_summary
    stock_code = _normalize_stock_code(stock_code) if stock_code else ""
    return {
        "recent_changes": list_holding_changes(limit=limit, stock_code=stock_code or None),
        "current_holdings": current_holdings_summary() if not stock_code else None,
    }


@_tool(WRITE, "提交一条持仓变动（买入/卖出，写入真实持仓和交易记录）")
def submit_holding_change(stock_code: str, trade_type: str, price: float,
                          quantity: int, trade_date: str = "", stock_name: str = "",
                          fee: float = 0, note: str = "") -> dict:
    from .holding_change_service import submit_holding_change as _submit
    return _submit({
        "stock_code": stock_code,
        "stock_name": stock_name,
        "trade_type": trade_type,
        "trade_date": trade_date,
        "price": price,
        "quantity": quantity,
        "fee": fee,
        "note": note,
        "source": "ai_tool",
    })


@_tool(READ_ONLY, "查询某股最新价和估值")
def query_stock_snapshot(stock_code: str) -> dict:
    stock_code = _normalize_stock_code(stock_code)
    if not stock_code:
        return {"success": False, "error": "缺少有效股票代码"}

    basic = query_one(
        "SELECT stock_code, trade_date, close, pe_ttm, pb, ps_ttm, turnover_rate, total_mv, circ_mv "
        "FROM daily_basic WHERE stock_code=? ORDER BY trade_date DESC LIMIT 1",
        (stock_code,),
    ) or {}
    quote = query_one(
        "SELECT trade_date, open, high, low, close, volume, amount, change_pct "
        "FROM daily_quotes WHERE stock_code=? ORDER BY trade_date DESC LIMIT 1",
        (stock_code,),
    ) or {}
    name = _lookup_stock_name(stock_code)

    if not basic and not quote:
        return {
            "success": False,
            "stock_code": stock_code,
            "stock_name": name,
            "error": f"本地暂无 {stock_code} {name or ''} 的行情快照",
        }

    return {
        "success": True,
        "stock_code": stock_code,
        "stock_name": name,
        "trade_date": quote.get("trade_date") or basic.get("trade_date"),
        "close": quote.get("close") if quote.get("close") is not None else basic.get("close"),
        "change_pct": quote.get("change_pct"),
        "open": quote.get("open"),
        "high": quote.get("high"),
        "low": quote.get("low"),
        "volume": quote.get("volume"),
        "amount": quote.get("amount"),
        "pe_ttm": basic.get("pe_ttm"),
        "pb": basic.get("pb"),
        "ps_ttm": basic.get("ps_ttm"),
        "turnover_rate": basic.get("turnover_rate"),
        "total_mv": basic.get("total_mv"),
        "circ_mv": basic.get("circ_mv"),
        "data_note": "来自本地行情缓存；非持仓股票也可查询。",
    }


@_tool(READ_ONLY, "查询某股最近研报")
def query_reports(stock_code: str, limit: int = 10) -> list[dict]:
    stock_code = _normalize_stock_code(stock_code)
    return query_all(
        "SELECT report_date, broker, rating, title FROM reports_cache "
        "WHERE stock_code=? ORDER BY report_date DESC LIMIT ?",
        (stock_code, limit),
    )


@_tool(READ_ONLY, "查询某股最近分析")
def query_analysis(stock_code: str) -> dict:
    stock_code = _normalize_stock_code(stock_code)
    row = query_one(
        "SELECT analysis_date, close_price, ai_summary, signal "
        "FROM daily_analysis WHERE stock_code=? ORDER BY created_at DESC LIMIT 1",
        (stock_code,),
    )
    if row and row.get("ai_summary"):
        try:
            row["ai_summary"] = json.loads(row["ai_summary"])
        except Exception:
            pass
    return row or {}


@_tool(READ_ONLY, "查询自选股")
def query_watchlist() -> list[dict]:
    return query_all("SELECT * FROM watchlist ORDER BY priority DESC, added_at DESC")


@_tool(READ_ONLY, "查询某股近 N 天 K 线")
def query_kline(stock_code: str, days: int = 30) -> list[dict]:
    stock_code = _normalize_stock_code(stock_code)
    return query_all(
        "SELECT trade_date, open, high, low, close, volume, change_pct "
        "FROM daily_quotes WHERE stock_code=? ORDER BY trade_date DESC LIMIT ?",
        (stock_code, days),
    )


@_tool(READ_ONLY, "查询未来 180 天解禁事件")
def query_upcoming_floats(stock_code: Optional[str] = None) -> list[dict]:
    stock_code = _normalize_stock_code(stock_code) if stock_code else None
    if stock_code:
        return query_all(
            "SELECT stock_code, float_date, float_share, float_ratio, holder_name, share_type "
            "FROM share_float_cache WHERE stock_code=? AND float_date >= date('now') "
            "ORDER BY float_date LIMIT 20",
            (stock_code,),
        )
    return query_all(
        "SELECT stock_code, float_date, float_share, float_ratio "
        "FROM share_float_cache WHERE float_date >= date('now') "
        "ORDER BY float_date LIMIT 50",
    )

@_tool(READ_ONLY, "查询某股多源深度画像（财务/筹码/融资/资金/研报/事件风险）")
def query_stock_deep_profile(stock_code: str) -> dict:
    stock_code = _normalize_stock_code(stock_code)
    from .data_enrichment_service import get_stock_deep_profile
    return get_stock_deep_profile(stock_code)


@_tool(READ_ONLY, "查询最新市场环境（指数/涨跌停/龙虎榜/融资/停复牌/ETF）")
def query_market_context() -> dict:
    from .data_enrichment_service import get_latest_market_context
    return get_latest_market_context()


@_tool(READ_ONLY, "查询外部数据源接口可用性状态")
def query_source_api_status() -> dict:
    from .data_enrichment_service import list_source_api_status
    return list_source_api_status()


@_tool(READ_ONLY, "查询某股 Tushare 研报加工信号（一致预期/评级/盈利预测/目标价）")
def query_research_report_signals(stock_code: str, limit: int = 20) -> dict:
    stock_code = _normalize_stock_code(stock_code)
    from .tushare_report_service import get_research_report_signals
    return get_research_report_signals(stock_code, limit=limit)


@_tool(WRITE, "刷新 Tushare 研报并加工成投研信号（低频付费接口，写入缓存）")
def refresh_tushare_reports(stock_code: str = "", max_stocks: int = 2,
                            fetch_live: bool = True) -> dict:
    stock_code = _normalize_stock_code(stock_code) if stock_code else ""
    from .tushare_report_service import collect_and_process_tushare_reports
    return collect_and_process_tushare_reports(
        stock_code=stock_code or None,
        max_stocks=max_stocks,
        fetch_live=fetch_live,
        process_cached=True,
    )


@_tool(READ_ONLY, "查询研报作者/团队历史命中率（按20/60交易日反测）")
def query_research_author_performance(horizon_days: int = 60,
                                      min_reports: int = 1,
                                      limit: int = 30) -> dict:
    from .tushare_report_service import get_research_author_performance
    return get_research_author_performance(
        horizon_days=horizon_days,
        min_reports=min_reports,
        limit=limit,
    )


@_tool(WRITE, "批量加工缓存研报并刷新研报质量分级")
def batch_process_cached_reports(stock_code: str = "", limit: int = 5000) -> dict:
    stock_code = _normalize_stock_code(stock_code) if stock_code else ""
    from .tushare_report_service import process_reports_cache_batch
    return process_reports_cache_batch(limit=limit, stock_code=stock_code or None)


@_tool(WRITE, "批量加工缓存研报、反测命中率并刷新作者/团队质量等级")
def refresh_research_report_backtest(limit: int = 5000, stock_code: str = "") -> dict:
    stock_code = _normalize_stock_code(stock_code) if stock_code else ""
    from .tushare_report_service import refresh_research_report_quality_scores
    return refresh_research_report_quality_scores(limit=limit, stock_code=stock_code or None)


@_tool(READ_ONLY, "查询推荐来源记忆（热股月采/荐股矩阵/外部荐股的历史表现）")
def query_recommendation_memory(stock_code: str = "", source_key: str = "", limit: int = 20) -> dict:
    from .recommendation_memory_service import (
        get_recommendation_memory_for_stock,
        list_recent_recommendations,
        list_source_performance,
    )
    stock_code = _normalize_stock_code(stock_code) if stock_code else ""
    if stock_code:
        return get_recommendation_memory_for_stock(stock_code, limit=limit)
    if source_key:
        return list_recent_recommendations(source_key=source_key, limit=limit)
    return {
        "sources": list_source_performance(limit=limit),
        "recent": list_recent_recommendations(limit=limit),
    }


@_tool(READ_ONLY, "查询用户交易认知规则库（仓位/止损/等待/主线/逻辑验证）")
def query_trading_cognition_rules(category: str = "", applies_to: str = "", limit: int = 50) -> dict:
    from .trading_cognition_service import list_trading_cognition_rules, core_cognition_checklist
    data = list_trading_cognition_rules(
        category=category or None,
        applies_to=applies_to or None,
        limit=limit,
    )
    data["checklist"] = core_cognition_checklist()
    return data


@_tool(READ_ONLY, "查询系统分析架构（多源数据/研报学习/互动跟踪/AI分工/PK/风控/蒸馏存储）")
def query_analysis_architecture(layer: str = "", applies_to: str = "", limit: int = 50) -> dict:
    from .analysis_architecture_service import list_analysis_architecture, architecture_checklist
    data = list_analysis_architecture(
        layer=layer or None,
        applies_to=applies_to or None,
        limit=limit,
    )
    data["checklist"] = architecture_checklist()
    return data


@_tool(READ_ONLY, "查询GitHub热门股票/量化投资工具参考（只作架构借鉴）")
def query_open_source_tool_references(category: str = "", limit: int = 30) -> dict:
    from .open_source_tool_reference_service import list_open_source_tool_references
    return list_open_source_tool_references(category=category or None, limit=limit)


@_tool(READ_ONLY, "查询分析-决策-行动-反馈闭环账本和盈利复盘状态")
def query_decision_feedback_loop(stock_code: str = "", stage: str = "", limit: int = 50) -> dict:
    from .decision_feedback_service import get_decision_feedback_snapshot, list_decision_feedback_events
    snapshot = get_decision_feedback_snapshot()
    events = list_decision_feedback_events(stock_code=stock_code or None, stage=stage or None, limit=limit)
    snapshot["events"] = events
    return snapshot


@_tool(WRITE, "记录一条分析/决策/行动/反馈/再分析事件，写入闭环账本")
def record_decision_feedback_event(stage: str, source: str = "ai_tool",
                                   stock_code: str = "", stock_name: str = "",
                                   title: str = "", summary: str = "",
                                   decision: str = "", action: str = "",
                                   expected_outcome: str = "", actual_outcome: str = "",
                                   pnl_pct: float = None, max_drawdown_pct: float = None,
                                   confidence: float = None, next_review_at: str = "",
                                   status: str = "open") -> dict:
    from .decision_feedback_service import record_decision_feedback_event as _record
    return _record({
        "stage": stage,
        "source": source,
        "stock_code": stock_code,
        "stock_name": stock_name,
        "title": title,
        "summary": summary,
        "decision": decision,
        "action": action,
        "expected_outcome": expected_outcome,
        "actual_outcome": actual_outcome,
        "pnl_pct": pnl_pct,
        "max_drawdown_pct": max_drawdown_pct,
        "confidence": confidence,
        "next_review_at": next_review_at,
        "status": status,
    })


@_tool(WRITE, "刷新推荐来源命中率反测（5/20/60日收益，写入记忆库）")
def refresh_recommendation_memory(limit: int = 1000) -> dict:
    from .recommendation_memory_service import review_recommendation_memory
    return review_recommendation_memory(limit=limit)


@_tool(WRITE, "刷新多源数据画像（写入缓存；默认刷新重点池）")
def refresh_multi_source_data(stock_code: str = "", max_stocks: int = 30) -> dict:
    from .data_enrichment_service import run_data_enrichment
    stock_code = _normalize_stock_code(stock_code) if stock_code else ""
    return run_data_enrichment(stock_code=stock_code or None, max_stocks=max_stocks,
                               refresh_market=True, refresh_days=7)


@_tool(READ_ONLY, "查询4个AI模拟账户PK看板（净值/持仓/收益/策略复盘）")
def query_ai_pk_dashboard() -> dict:
    from .ai_pk_service import get_ai_pk_dashboard
    return get_ai_pk_dashboard()


@_tool(WRITE, "运行4个AI模拟账户PK日度调仓")
def run_ai_pk_daily(force: bool = False) -> dict:
    from .ai_pk_service import run_ai_pk_daily as _run
    return _run(force=force)


@_tool(WRITE, "运行4个AI模拟账户PK盘中实时调仓")
def run_ai_pk_intraday(force: bool = False) -> dict:
    from .ai_pk_service import run_ai_pk_intraday as _run
    return _run(force=force, source="tool")



# ---------- 🟡 写入工具 ----------
@_tool(WRITE, "录入一笔交易")
def add_trade(stock_code: str, trade_type: str, price: float,
              quantity: int, fee: float = 0, notes: str = "") -> dict:
    if trade_type not in ("buy", "sell"):
        raise ValueError("trade_type must be buy/sell")
    if price <= 0 or quantity <= 0:
        raise ValueError("price and quantity must be positive")

    pos = query_one(
        "SELECT id FROM positions WHERE stock_code=? AND status='holding'",
        (stock_code,),
    )
    if not pos:
        # 新建持仓
        pid = execute(
            "INSERT INTO positions(stock_code, opened_at, notes) VALUES (?,?,?)",
            (stock_code, datetime.now().strftime("%Y-%m-%d"), notes[:200]),
        )
    else:
        pid = pos["id"]

    tid = execute(
        "INSERT INTO trades(position_id, trade_date, trade_type, price, quantity, fee, notes) "
        "VALUES (?,?,?,?,?,?,?)",
        (pid, datetime.now().strftime("%Y-%m-%d"),
         trade_type, price, quantity, fee, notes[:200]),
    )
    return {"position_id": pid, "trade_id": tid, "stock_code": stock_code,
            "action": trade_type, "price": price, "quantity": quantity}


@_tool(WRITE, "加入自选股")
def add_watchlist(stock_code: str, stock_name: str = "",
                   reason: str = "", priority: int = 0) -> dict:
    rid = execute(
        "INSERT OR IGNORE INTO watchlist(stock_code, stock_name, reason, priority) "
        "VALUES (?,?,?,?)",
        (stock_code, stock_name, reason[:200], priority),
    )
    return {"watchlist_id": rid, "stock_code": stock_code}


@_tool(WRITE, "触发对某股的新分析")
def trigger_analysis(stock_code: str) -> dict:
    from .analyze_service import analyze_stock
    result = analyze_stock(stock_code, force_refresh=True)
    return {"stock_code": stock_code, "analyzed_at": result.get("analyzed_at"),
            "close": result.get("data_snapshot", {}).get("close")}


# ---------- 🔴 破坏工具 ----------
@_tool(DESTRUCTIVE, "移出自选")
def remove_watchlist(stock_code: str) -> dict:
    n = execute("DELETE FROM watchlist WHERE stock_code=?", (stock_code,))
    return {"stock_code": stock_code, "removed": n}


@_tool(DESTRUCTIVE, "关闭（归档）某个持仓")
def close_position(position_id: int) -> dict:
    n = execute(
        "UPDATE positions SET status='closed', closed_at=? WHERE id=?",
        (datetime.now().strftime("%Y-%m-%d"), position_id),
    )
    return {"position_id": position_id, "closed": n > 0}


# ---------- 工具注册表 ----------
TOOLS: dict = {}
for name in list(globals()):
    fn = globals()[name]
    if callable(fn) and hasattr(fn, "__tool_level__"):
        TOOLS[name] = fn


def list_tools_for_ai() -> list[dict]:
    """返回 AI 可见的工具描述（JSON Schema 风格）。"""
    import inspect
    result = []
    for name, fn in TOOLS.items():
        sig = inspect.signature(fn)
        params = {}
        for pname, p in sig.parameters.items():
            ann = p.annotation
            if ann in (int,): t = "integer"
            elif ann in (float,): t = "number"
            elif ann in (bool,): t = "boolean"
            else: t = "string"
            params[pname] = {
                "type": t,
                "required": p.default == inspect.Parameter.empty,
                "default": None if p.default == inspect.Parameter.empty else p.default,
            }
        result.append({
            "name": name,
            "level": fn.__tool_level__,
            "description": fn.__tool_desc__,
            "params": params,
        })
    return result


def _ensure_audit_table():
    with db() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS tool_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                tool_name TEXT,
                level TEXT,
                args TEXT,
                user_confirmed INTEGER,
                executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                result TEXT,
                error TEXT
            )
        """)


# 参数别名：AI 常在这几对参数名之间混，统一归一化
_ARG_ALIASES = {
    "code": "stock_code",
    "symbol": "stock_code",
    "ticker": "stock_code",
    "name": "stock_name",
    "limit_count": "limit",
    "max_count": "limit",
    "num_days": "days",
    "day_count": "days",
}


def _normalize_args(fn, args: dict) -> dict:
    """把常见错参数名映射到真实参数名，并归一化股票代码。"""
    sig = inspect.signature(fn)
    valid = set(sig.parameters.keys())
    out = {}
    for k, v in (args or {}).items():
        target = None
        if k in valid:
            target = k
        elif k in _ARG_ALIASES and _ARG_ALIASES[k] in valid:
            target = _ARG_ALIASES[k]
        if not target:
            continue
        if target in ("stock_code", "code"):
            v = _normalize_stock_code(v) or v
        out[target] = v
    return out


def execute_tool(tool_name: str, args: dict,
                 session_id: str = "",
                 user_confirmed: bool = False) -> dict:
    """执行一个工具调用。

    - 只读级别: 直接执行
    - 写入/破坏级别: 必须 user_confirmed=True
    """
    _ensure_audit_table()
    fn = TOOLS.get(tool_name)
    if not fn:
        raise ValueError(f"未知工具: {tool_name}")
    args = _normalize_args(fn, args)

    level = fn.__tool_level__

    # 权限检查
    if level != READ_ONLY and not user_confirmed:
        # 不执行，返回需要确认
        return {
            "require_confirm": True,
            "tool_name": tool_name,
            "level": level,
            "description": fn.__tool_desc__,
            "args": args,
        }

    # 审计 + 执行
    result, error = None, None
    try:
        result = fn(**args)
    except Exception as e:
        error = str(e)
        log.exception(f"tool {tool_name} fail")

    # 记录审计
    with db() as c:
        c.execute(
            "INSERT INTO tool_audit(session_id, tool_name, level, args, user_confirmed, result, error) "
            "VALUES (?,?,?,?,?,?,?)",
            (session_id, tool_name, level,
             json.dumps(args, ensure_ascii=False, default=str),
             1 if user_confirmed else 0,
             json.dumps(result, ensure_ascii=False, default=str) if result is not None else None,
             error),
        )

    if error:
        return {"success": False, "error": error}
    return {"success": True, "result": result}
