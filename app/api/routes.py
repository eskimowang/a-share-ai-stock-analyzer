"""API 路由统一注册。"""
import logging
from fastapi import APIRouter, Header, HTTPException, Path
from fastapi.responses import HTMLResponse, Response, JSONResponse
from pydantic import BaseModel, Field
from typing import Optional

from ..config import CONFIG
from ..services.analyze_service import analyze_stock, _collect_stock_data
from ..services.chat_service import (
    send_message, confirm_tool, list_sessions, get_messages,
)


router = APIRouter()
log = logging.getLogger(__name__)


# ========== 单股辅助数据（供 report V2 图表使用） ==========
@router.get("/api/stock/{code}/moneyflow")
def stock_moneyflow(code: str, days: int = 30):
    from ..db import query_all
    rows = query_all(
        "SELECT trade_date, net_mf_amount, buy_lg_amount, sell_lg_amount, net_d5_amount "
        "FROM moneyflow_cache WHERE stock_code=? ORDER BY trade_date DESC LIMIT ?",
        (code, days),
    )
    rows.reverse()
    return {"count": len(rows), "items": rows}


@router.get("/api/stock/{code}/holder")
def stock_holder(code: str):
    from ..db import query_all
    rows = query_all(
        "SELECT end_date, holder_num FROM holder_number_cache "
        "WHERE stock_code=? ORDER BY end_date LIMIT 12",
        (code,),
    )
    return {"count": len(rows), "items": rows}


@router.get("/api/stock/{code}/reports")
def stock_reports(code: str, limit: int = 30):
    from ..db import query_all
    rows = query_all(
        "SELECT report_date, broker, rating, title FROM reports_cache "
        "WHERE stock_code=? ORDER BY report_date DESC LIMIT ?",
        (code, limit),
    )
    return {"count": len(rows), "items": rows}


# ========== 单股分析 ==========
class AnalyzeRequest(BaseModel):
    force_refresh: bool = False
    with_adversary: bool = True
    differentiated: bool = True   # 默认开：4 家 AI 看不同侧面
    max_tokens: int = 2000


@router.post("/api/analyze/{code}")
def analyze(code: str = Path(..., pattern="^[0-9]{6}$"),
             body: AnalyzeRequest = None):
    body = body or AnalyzeRequest()
    try:
        result = analyze_stock(
            code,
            with_adversary=body.with_adversary,
            differentiated=body.differentiated,
            force_refresh=body.force_refresh,
            max_tokens=body.max_tokens,
        )
        return result
    except Exception as e:
        log.exception("单股分析失败: %s", code)
        raise HTTPException(500, f"分析失败: {e}")


@router.get("/api/analyze/{code}/data")
def stock_data(code: str):
    """只返回数据包（供调试，不调 AI）"""
    return _collect_stock_data(code)


# ========== 持仓交易策略 ==========
@router.post("/api/strategy/trading")
def strategy_trading():
    """基于当前持仓跑一次交易策略（同步返回，较慢）"""
    from ..db import query_all, query_one
    from ..services.analyze_service import _brains, _mb
    from ..ai.prompts import SYSTEM_PROMPT, SYSTEM_PROMPT_ADVERSARY
    from ..data_sources import UnifiedDataSource
    from ..config import CONFIG
    import concurrent.futures
    import time

    # 1. 拉持仓
    rows = query_all("""
        SELECT p.stock_code, p.stock_name, t.price as cost, t.quantity as qty
        FROM positions p JOIN trades t ON p.id=t.position_id
        WHERE p.status='holding' ORDER BY p.id
    """)
    if not rows:
        raise HTTPException(404, "无持仓")

    # 2. 每只补现价 + 研报
    ds = UnifiedDataSource(tushare_token=CONFIG["data_sources"]["tushare"].get("token"))
    positions = []
    total_mv = total_cost = 0
    for r in rows:
        code = r["stock_code"]
        db_snap = query_one(
            "SELECT close, pe_ttm, pb, total_mv FROM daily_basic "
            "WHERE stock_code=? ORDER BY trade_date DESC LIMIT 1", (code,)
        ) or {}
        cur = db_snap.get("close") or r["cost"]
        mv = cur * r["qty"]
        total_mv += mv
        total_cost += r["cost"] * r["qty"]
        positions.append({
            "code": code, "name": r["stock_name"],
            "cost": r["cost"], "qty": r["qty"],
            "current": cur, "market_value": mv,
            "pl_pct": (cur - r["cost"]) / r["cost"] * 100 if r["cost"] else 0,
            "pe_ttm": db_snap.get("pe_ttm"), "pb": db_snap.get("pb"),
        })

    # 3. 构造 prompt
    detail = ""
    for p in positions:
        w = p["market_value"] / total_mv * 100
        detail += (
            f"\n## {p['code']} {p['name']} (权重 {w:.1f}%)\n"
            f"- 成本 {p['cost']:.2f} 现价 {p['current']:.2f} 浮盈 {p['pl_pct']:+.2f}%\n"
            f"- 数量 {p['qty']} 市值 {p['market_value']:,.0f}\n"
            f"- PE {p['pe_ttm']} PB {p['pb']}\n"
        )

    prompt = f"""基于以下 {len(positions)} 只持仓，给每只**今日操作建议**。

组合市值 {total_mv:,.0f}，成本 {total_cost:,.0f}，浮盈 {(total_mv-total_cost)/total_cost*100:+.2f}%。

持仓明细:
{detail}

输出：
1. 一句话组合判断
2. 每只操作表格（代码/名称/操作/仓位变动/理由）
3. 最大风险
4. 今日必做的 1 件事
"""

    # 4. 4 家 AI 并行
    def _one(c):
        sys = SYSTEM_PROMPT_ADVERSARY if c.name == "Claude" else SYSTEM_PROMPT
        try:
            return c.name + (" [反方]" if c.name == "Claude" else ""), c.complete(sys, prompt, max_tokens=2500)
        except Exception as e:
            return c.name, f"[失败] {e}"

    t0 = time.time()
    opinions = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(_brains)) as pool:
        for f in concurrent.futures.as_completed([pool.submit(_one, b) for b in _brains]):
            n, t = f.result()
            opinions[n] = t

    # 5. 仲裁
    deepseek = next((b for b in _brains if b.name == "DeepSeek"), _brains[0])
    joined = "\n\n".join(f"## 【{n}】\n{t}" for n, t in opinions.items())
    final = deepseek.complete(
        "投资策略专家，整合观点给最终可执行决策。",
        f"四家 AI 对持仓的分析:\n\n{joined}\n\n"
        "请整合成最终操作清单，含：\n"
        "- 一句话组合结论\n"
        "- 每只立即操作表格\n"
        "- 反方警示吸收\n"
        "- 今日资金使用建议\n",
        max_tokens=3000,
    )

    return {
        "analyzed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_market_value": total_mv,
        "total_cost": total_cost,
        "portfolio_pl_pct": (total_mv - total_cost) / total_cost * 100 if total_cost else 0,
        "positions": positions,
        "opinions": opinions,
        "consensus": final,
        "duration_seconds": round(time.time() - t0, 1),
    }


# ========== 聊天 ==========
class ChatRequest(BaseModel):
    session_id: Optional[str] = None
    message: str = Field(..., min_length=1, max_length=5000)
    mode: str = Field("consensus", pattern="^(consensus|single)$")
    model: Optional[str] = None


@router.post("/api/chat/send")
def chat_send(body: ChatRequest):
    try:
        return send_message(body.session_id, body.message, body.mode, body.model)
    except Exception as e:
        log.exception("聊天接口失败")
        raise HTTPException(500, f"聊天失败: {e}")


class ConfirmRequest(BaseModel):
    session_id: str
    tool_name: str
    args: dict = {}


@router.post("/api/chat/confirm")
def chat_confirm(body: ConfirmRequest):
    try:
        return confirm_tool(body.session_id, body.tool_name, body.args)
    except Exception as e:
        raise HTTPException(500, f"确认失败: {e}")


def _check_admin_token(x_admin_token: Optional[str] = Header(default=None)):
    token = CONFIG.get("security", {}).get("admin_token")
    if not token:
        raise HTTPException(403, "聊天历史接口未开放")
    if x_admin_token != token:
        raise HTTPException(403, "需要管理员令牌")


@router.get("/api/chat/sessions")
def chat_sessions(x_admin_token: Optional[str] = Header(default=None)):
    _check_admin_token(x_admin_token)
    return list_sessions()


@router.get("/api/chat/sessions/{session_id}")
def chat_messages(session_id: str, x_admin_token: Optional[str] = Header(default=None)):
    _check_admin_token(x_admin_token)
    return get_messages(session_id)


# ========== 荐股矩阵 ==========
@router.post("/api/discovery/run")
def discovery_run():
    """触发一次完整荐股流程（同步，5-15 分钟），返回矩阵 + md 报告。"""
    from ..services.discovery_service import run_discovery_full
    try:
        return run_discovery_full()
    except Exception as e:
        raise HTTPException(500, f"荐股失败: {e}")


@router.get("/api/discovery/latest")
def discovery_latest():
    """返回最近一次成功的荐股矩阵。"""
    from ..services.discovery_service import get_latest_matrix
    result = get_latest_matrix()
    if not result:
        raise HTTPException(404, "尚无成功的荐股记录")
    return result


@router.get("/api/discovery/runs")
def discovery_runs(limit: int = 10):
    """列出最近 N 次荐股运行记录。"""
    from ..db import query_all
    from ..services.discovery_service import _ensure_discovery_tables
    _ensure_discovery_tables()
    rows = query_all(
        "SELECT id, run_at, status, duration_seconds, error_msg "
        "FROM discovery_runs ORDER BY id DESC LIMIT ?", (limit,),
    )
    return {"count": len(rows), "items": rows}


# ========== 长期力量跟踪 ==========
@router.post("/api/tracking/run")
def tracking_run():
    """触发一次长期力量跟踪分析（同步，3-6 分钟）。"""
    from ..services.long_term_tracking_service import run_long_term_tracking
    try:
        return run_long_term_tracking()
    except Exception as e:
        raise HTTPException(500, f"长期跟踪失败: {e}")


@router.get("/api/tracking/latest")
def tracking_latest():
    """返回最近一次成功的长期跟踪报告。"""
    from ..services.long_term_tracking_service import get_latest_tracking
    r = get_latest_tracking()
    if not r:
        raise HTTPException(404, "尚无成功的长期跟踪记录")
    return r


# ========== 互动股票跟踪 ==========
@router.get("/api/tracking/interaction/stocks")
def interaction_tracking_stocks(active_only: bool = True, limit: int = 200):
    """返回你在聊天里互动过的股票跟踪池。"""
    from ..services.interaction_tracking_service import list_interaction_stocks
    if limit <= 0:
        return {"count": 0, "items": []}
    items = list_interaction_stocks(active_only=active_only, limit=limit)
    return {"count": len(items), "items": items}


@router.post("/api/tracking/interaction/run")
def interaction_tracking_run(since_days: int = 30, max_stocks: Optional[int] = None,
                             refresh_market_data: bool = False, refresh_days: int = 3):
    """手动触发一次互动股票池复盘。"""
    from ..services.interaction_tracking_service import run_interaction_tracking
    return run_interaction_tracking(
        since_days=since_days,
        max_stocks=max_stocks,
        refresh_market_data=refresh_market_data,
        refresh_days=refresh_days,
    )


@router.get("/api/tracking/interaction/latest")
def interaction_tracking_latest():
    from ..services.interaction_tracking_service import get_latest_interaction_tracking_run
    r = get_latest_interaction_tracking_run()
    if not r:
        raise HTTPException(404, "尚无互动股票跟踪记录")
    return r


@router.get("/api/tracking/interaction/analysis")
def interaction_tracking_analysis(stock_code: Optional[str] = None, limit: int = 50):
    """返回我们交流过的股票的持续分析记录。"""
    from ..services.interaction_tracking_service import list_interaction_analysis
    items = list_interaction_analysis(stock_code=stock_code, limit=limit)
    return {"count": len(items), "items": items}



# ========== 多源数据增强 ==========
class DataEnrichmentRunReq(BaseModel):
    stock_code: Optional[str] = None
    max_stocks: int = 30
    refresh_market: bool = True
    refresh_days: int = 7


@router.post("/api/data-enrichment/run")
def data_enrichment_run(body: DataEnrichmentRunReq):
    from ..services.data_enrichment_service import run_data_enrichment
    return run_data_enrichment(
        stock_code=body.stock_code,
        max_stocks=body.max_stocks,
        refresh_market=body.refresh_market,
        refresh_days=body.refresh_days,
    )


@router.get("/api/data-enrichment/profile/{stock_code}")
def data_enrichment_profile(stock_code: str):
    from ..services.data_enrichment_service import get_stock_deep_profile
    return get_stock_deep_profile(stock_code)


@router.get("/api/data-enrichment/market/latest")
def data_enrichment_market_latest():
    from ..services.data_enrichment_service import get_latest_market_context
    return get_latest_market_context()


@router.get("/api/data-enrichment/source-status")
def data_enrichment_source_status():
    from ..services.data_enrichment_service import list_source_api_status
    return list_source_api_status()

# ========== 通知/推送 ==========
@router.get("/api/notifications/recent")
def notifications_recent(limit: int = 10):
    from ..db import query_all
    items = query_all(
        "SELECT sent_at, channel, title, content, status FROM notifications "
        "ORDER BY id DESC LIMIT ?", (limit,),
    )
    return {"count": len(items), "items": items}


# ========== 订单指令 ==========
@router.get("/api/orders")
def list_orders(status: str = "pending", limit: int = 50):
    from ..services.order_service import list_pending_orders, list_all_orders
    items = list_pending_orders(limit) if status == "pending" else list_all_orders(limit)
    return {"count": len(items), "items": items}


class OrderExecuteReq(BaseModel):
    broker: str = "manual"


@router.post("/api/orders/{oid}/execute")
def mark_order_done(oid: int, body: OrderExecuteReq = None):
    from ..services.order_service import mark_order_executed
    body = body or OrderExecuteReq()
    mark_order_executed(oid, body.broker)
    return {"ok": True, "order_id": oid}


class OrderCreateReq(BaseModel):
    stock_code: str
    direction: str  # buy/sell/hold
    quantity: Optional[int] = None
    price_hint: Optional[float] = None
    reason: Optional[str] = None
    priority: str = "normal"


@router.post("/api/orders")
def create_order(body: OrderCreateReq):
    from ..services.order_service import save_orders
    ids = save_orders([{
        "stock_code": body.stock_code,
        "direction": body.direction,
        "quantity": body.quantity,
        "price_hint": body.price_hint,
        "reason": body.reason,
        "priority": body.priority,
        "source": "manual",
    }])
    return {"ids": ids}


# ========== 纸交易 ==========
@router.get("/api/paper/stats")
def paper_stats():
    from ..services.order_service import get_paper_stats
    return get_paper_stats()


@router.get("/api/paper/trades")
def list_paper_trades(limit: int = 50, closed: Optional[int] = None):
    from ..db import query_all
    if closed is None:
        rows = query_all("SELECT * FROM paper_trades ORDER BY id DESC LIMIT ?", (limit,))
    else:
        rows = query_all("SELECT * FROM paper_trades WHERE closed=? ORDER BY id DESC LIMIT ?",
                          (closed, limit))
    return {"count": len(rows), "items": rows}


# ========== 回测 ==========
@router.post("/api/backtest/run")
def backtest_run(hold_days: int = 7, since_days: int = 180):
    from ..services.backtest_service import run_backtest
    return run_backtest(hold_days=hold_days, since_days=since_days)


@router.get("/api/backtest/latest")
def backtest_latest():
    from ..services.backtest_service import get_latest_backtest
    r = get_latest_backtest()
    if not r:
        raise HTTPException(404, "尚无回测记录")
    return r


# ========== 审计流水 ==========
@router.get("/api/audit")
def audit_list(limit: int = 100, level: Optional[str] = None):
    from ..db import query_all
    if level:
        rows = query_all(
            "SELECT * FROM tool_audit WHERE level=? ORDER BY id DESC LIMIT ?",
            (level, limit),
        )
    else:
        rows = query_all(
            "SELECT * FROM tool_audit ORDER BY id DESC LIMIT ?", (limit,)
        )
    return {"count": len(rows), "items": rows}


# ========== Web Push ==========
@router.get("/api/push/key")
def push_key():
    from ..services.push_service import get_public_key
    k = get_public_key()
    if not k:
        raise HTTPException(503, "VAPID 未配置")
    return {"public_key": k}


class PushSubscribeReq(BaseModel):
    subscription: dict
    user_agent: Optional[str] = ""


@router.post("/api/push/subscribe")
def push_subscribe(body: PushSubscribeReq):
    from ..services.push_service import register_subscription
    try:
        sid = register_subscription(body.subscription, body.user_agent or "")
        return {"ok": True, "id": sid}
    except Exception as e:
        raise HTTPException(400, f"订阅失败: {e}")


class PushTestReq(BaseModel):
    title: str = "测试推送"
    body: str = "这是一条 Web Push 测试"
    url: str = "/chat"


@router.post("/api/push/test")
def push_test(body: PushTestReq):
    from ..services.push_service import push_to_all
    return push_to_all(body.title, body.body, body.url)


# ========== 券商风格学习 ==========
class BrokerStudyReq(BaseModel):
    brokers: Optional[list[str]] = None  # None → 默认 中信 + 中金
    industries: Optional[list[str]] = None


@router.post("/api/broker_study/run")
def broker_study_run(body: BrokerStudyReq = None):
    from ..services.broker_study_service import run_broker_study, BROKERS_TO_STUDY
    body = body or BrokerStudyReq()
    brokers = [{"name": b, "tier": "自定义"} for b in body.brokers] if body.brokers else BROKERS_TO_STUDY
    return run_broker_study(brokers=brokers, industries=body.industries)


@router.get("/api/broker_study/profile")
def broker_profile(broker: str, industry: Optional[str] = None):
    from ..services.broker_study_service import get_broker_profile
    return {"profiles": get_broker_profile(broker, industry)}


@router.get("/api/broker_study/matrix")
def broker_matrix():
    from ..services.broker_study_service import get_broker_matrix
    return get_broker_matrix()


# ========== 中信中金研报采集 ==========
class PremiumReportReq(BaseModel):
    scope: str = "positions_watchlist"  # positions / watchlist / ivd / positions_watchlist / custom
    brokers: Optional[list[str]] = None
    months: int = 6
    codes: Optional[list[str]] = None  # 若 scope=custom 则用这个


@router.post("/api/premium_reports/run")
def premium_reports_run(body: PremiumReportReq = None):
    from ..services.premium_broker_reports import (
        run_premium_report_collection, _ensure_tables,
    )
    from ..services import premium_broker_reports as _pbr
    body = body or PremiumReportReq()

    if body.scope == "custom" and body.codes:
        # 临时 scope: 用提供的 codes 列表
        def _custom_stocks(scope):
            if scope == "custom":
                from ..db import query_one as qo
                out = []
                for c in body.codes:
                    row = qo("SELECT stock_name FROM positions WHERE stock_code=? LIMIT 1", (c,))
                    out.append({"code": c, "name": (row or {}).get("stock_name", "")})
                return out
            return _pbr._get_target_stocks(scope)

        _orig = _pbr._get_target_stocks
        _pbr._get_target_stocks = _custom_stocks
        try:
            return run_premium_report_collection(scope="custom", brokers=body.brokers,
                                                    months=body.months)
        finally:
            _pbr._get_target_stocks = _orig

    return run_premium_report_collection(scope=body.scope, brokers=body.brokers,
                                           months=body.months)


class HotStocksReq(BaseModel):
    top_per_industry: int = 20
    industries: Optional[list[str]] = None
    brokers: Optional[list[str]] = None
    months: int = 6


@router.post("/api/premium_reports/hot_stocks")
def premium_reports_hot(body: HotStocksReq = None):
    """Top N × 10 行业 × 中信中金 的月度大扫。"""
    from ..services.premium_broker_reports import run_hot_stocks_premium_reports
    body = body or HotStocksReq()
    return run_hot_stocks_premium_reports(
        top_per_industry=body.top_per_industry,
        industries=body.industries, brokers=body.brokers, months=body.months,
    )


@router.get("/api/premium_reports/{code}")
def premium_reports_for_stock(code: str, limit: int = 20):
    from ..services.premium_broker_reports import get_premium_reports_for_stock
    return {"code": code, "items": get_premium_reports_for_stock(code, limit)}


@router.get("/api/premium_reports/runs/latest")
def premium_reports_latest_run():
    from ..services.premium_broker_reports import get_latest_run_summary
    r = get_latest_run_summary()
    if not r:
        raise HTTPException(404, "尚无采集记录")
    return r



# ========== Tushare 付费研报规则 ==========
class AiPkRunReq(BaseModel):
    force: bool = False


@router.get("/api/ai-pk/dashboard")
def ai_pk_dashboard():
    from ..services.ai_pk_service import get_ai_pk_dashboard
    return get_ai_pk_dashboard()


@router.post("/api/ai-pk/run")
def ai_pk_run(body: AiPkRunReq = None):
    from ..services.ai_pk_service import run_ai_pk_daily
    body = body or AiPkRunReq()
    return run_ai_pk_daily(force=body.force)


@router.post("/api/ai-pk/run-intraday")
def ai_pk_run_intraday(body: AiPkRunReq = None):
    from ..services.ai_pk_service import run_ai_pk_intraday
    body = body or AiPkRunReq()
    return run_ai_pk_intraday(force=body.force, source="api")


class TushareReportRunReq(BaseModel):
    stock_code: Optional[str] = None
    max_stocks: int = 2
    months: int = 24
    fetch_live: bool = True
    process_cached: bool = True
    min_interval_seconds: int = 370


class TushareReportBatchReq(BaseModel):
    stock_code: Optional[str] = None
    limit: int = 5000


@router.post("/api/tushare-reports/run")
def tushare_reports_run(body: TushareReportRunReq = None):
    from ..services.tushare_report_service import collect_and_process_tushare_reports
    body = body or TushareReportRunReq()
    return collect_and_process_tushare_reports(
        stock_code=body.stock_code,
        max_stocks=body.max_stocks,
        months=body.months,
        fetch_live=body.fetch_live,
        process_cached=body.process_cached,
        min_interval_seconds=body.min_interval_seconds,
    )


@router.get("/api/tushare-reports/{stock_code}")
def tushare_reports_for_stock(stock_code: str, limit: int = 20):
    from ..services.tushare_report_service import get_research_report_signals
    return get_research_report_signals(stock_code, limit=limit)


@router.get("/api/tushare-reports/runs/latest")
def tushare_reports_latest_run():
    from ..services.tushare_report_service import get_latest_tushare_report_run
    r = get_latest_tushare_report_run()
    if not r:
        raise HTTPException(404, "尚无 Tushare 研报加工记录")
    return r


@router.post("/api/tushare-reports/process-cache")
def tushare_reports_process_cache(body: TushareReportBatchReq = None):
    from ..services.tushare_report_service import process_reports_cache_batch
    body = body or TushareReportBatchReq()
    return process_reports_cache_batch(limit=body.limit, stock_code=body.stock_code)


@router.post("/api/tushare-reports/backtest")
def tushare_reports_backtest(limit: int = 5000,
                             stock_code: Optional[str] = None,
                             body: TushareReportBatchReq = None):
    from ..services.tushare_report_service import refresh_research_report_quality_scores
    if body:
        limit = body.limit
        stock_code = body.stock_code
    return refresh_research_report_quality_scores(limit=limit, stock_code=stock_code)


@router.get("/api/tushare-reports/authors/performance")
def tushare_report_author_performance(horizon_days: int = 60,
                                      min_reports: int = 1,
                                      limit: int = 30):
    from ..services.tushare_report_service import get_research_author_performance
    return get_research_author_performance(
        horizon_days=horizon_days,
        min_reports=min_reports,
        limit=limit,
    )


# ========== 推荐来源记忆 ==========
class RecommendationMemoryIngestReq(BaseModel):
    source_key: str = "manual"
    source_name: str = "手动推荐"
    source_type: str = "manual"
    batch_id: Optional[str] = None
    recommendation_date: Optional[str] = None
    horizon_days: int = 60
    items: list[dict] = Field(default_factory=list)
    context: dict = Field(default_factory=dict)


@router.post("/api/recommendation-memory/ingest")
def recommendation_memory_ingest(body: RecommendationMemoryIngestReq):
    from ..services.recommendation_memory_service import record_recommendation_batch
    return record_recommendation_batch(
        source_key=body.source_key,
        source_name=body.source_name,
        source_type=body.source_type,
        items=body.items,
        batch_id=body.batch_id,
        recommendation_date=body.recommendation_date,
        default_horizon_days=body.horizon_days,
        context=body.context,
    )


@router.post("/api/recommendation-memory/backfill-hot-stocks")
def recommendation_memory_backfill_hot_stocks(run_id: Optional[int] = None):
    from ..services.recommendation_memory_service import backfill_latest_hot_stocks_from_reports
    return backfill_latest_hot_stocks_from_reports(run_id=run_id)


@router.post("/api/recommendation-memory/review")
def recommendation_memory_review(limit: int = 1000):
    from ..services.recommendation_memory_service import review_recommendation_memory
    return review_recommendation_memory(limit=limit)


@router.get("/api/recommendation-memory/sources")
def recommendation_memory_sources(limit: int = 30):
    from ..services.recommendation_memory_service import list_source_performance
    return list_source_performance(limit=limit)


@router.get("/api/recommendation-memory/recent")
def recommendation_memory_recent(source_key: Optional[str] = None, limit: int = 80):
    from ..services.recommendation_memory_service import list_recent_recommendations
    return list_recent_recommendations(source_key=source_key, limit=limit)


@router.get("/api/recommendation-memory/stock/{stock_code}")
def recommendation_memory_stock(stock_code: str, limit: int = 20):
    from ..services.recommendation_memory_service import get_recommendation_memory_for_stock
    return get_recommendation_memory_for_stock(stock_code, limit=limit)


# ========== 持仓变动入口 ==========
class HoldingChangeReq(BaseModel):
    stock_code: Optional[str] = None
    stock_name: Optional[str] = None
    trade_type: str = Field(..., pattern="^(buy|sell|买入|卖出|加仓|减仓|清仓)$")
    trade_date: Optional[str] = None
    price: float
    quantity: int
    fee: float = 0
    note: Optional[str] = None
    source: str = "manual"


class HoldingChangeBatchReq(BaseModel):
    changes: list[dict] = Field(default_factory=list)

class HoldingImageReq(BaseModel):
    filename: Optional[str] = None
    mime_type: str = "image/jpeg"
    image_base64: str
    width: Optional[int] = None
    height: Optional[int] = None
    note: Optional[str] = None
    extracted_text: Optional[str] = None
    apply_change: bool = False
    change: dict = Field(default_factory=dict)


@router.post("/api/holding-changes/submit")
def holding_change_submit(body: HoldingChangeReq):
    from ..services.holding_change_service import submit_holding_change
    try:
        payload = body.model_dump() if hasattr(body, "model_dump") else body.dict()
        return submit_holding_change(payload)
    except Exception as e:
        raise HTTPException(400, str(e))


@router.post("/api/holding-changes/batch")
def holding_change_batch(body: HoldingChangeBatchReq):
    from ..services.holding_change_service import submit_holding_changes
    return submit_holding_changes(body.changes)


@router.get("/api/holding-changes/recent")
def holding_change_recent(limit: int = 80, stock_code: Optional[str] = None):
    from ..services.holding_change_service import list_holding_changes
    return list_holding_changes(limit=limit, stock_code=stock_code)


@router.get("/api/holding-changes/holdings")
def holding_change_holdings():
    from ..services.holding_change_service import current_holdings_summary
    return current_holdings_summary()


@router.post("/api/holding-changes/image")
def holding_change_image(body: HoldingImageReq):
    from ..services.holding_change_service import save_holding_change_image
    try:
        payload = body.model_dump() if hasattr(body, "model_dump") else body.dict()
        return save_holding_change_image(payload)
    except Exception as e:
        raise HTTPException(400, str(e))


@router.get("/api/holding-changes/images")
def holding_change_images(limit: int = 50, stock_code: Optional[str] = None):
    from ..services.holding_change_service import list_holding_change_images
    return list_holding_change_images(limit=limit, stock_code=stock_code)


@router.get("/api/holding-changes/images/{image_id}/file")
def holding_change_image_file(image_id: int):
    from ..services.holding_change_service import get_holding_change_image_file
    try:
        item = get_holding_change_image_file(image_id)
        headers = {"Cache-Control": "private, max-age=3600"}
        return Response(content=item["content"], media_type=item["mime_type"], headers=headers)
    except Exception as e:
        raise HTTPException(404, str(e))


# ========== 交易认知规则库 ==========
@router.get("/api/trading-cognition/rules")
def trading_cognition_rules(category: Optional[str] = None,
                            applies_to: Optional[str] = None,
                            limit: int = 100):
    from ..services.trading_cognition_service import list_trading_cognition_rules, core_cognition_checklist
    data = list_trading_cognition_rules(category=category, applies_to=applies_to, limit=limit)
    data["checklist"] = core_cognition_checklist()
    return data


@router.post("/api/trading-cognition/seed")
def trading_cognition_seed(overwrite: bool = True):
    from ..services.trading_cognition_service import seed_trading_cognition_rules
    return seed_trading_cognition_rules(overwrite=overwrite)


# ========== 分析系统架构 ==========
@router.get("/api/analysis-architecture/components")
def analysis_architecture_components(layer: Optional[str] = None,
                                     applies_to: Optional[str] = None,
                                     limit: int = 100):
    from ..services.analysis_architecture_service import list_analysis_architecture, architecture_checklist
    data = list_analysis_architecture(layer=layer, applies_to=applies_to, limit=limit)
    data["checklist"] = architecture_checklist()
    return data


@router.post("/api/analysis-architecture/seed")
def analysis_architecture_seed(overwrite: bool = True):
    from ..services.analysis_architecture_service import seed_analysis_architecture
    return seed_analysis_architecture(overwrite=overwrite)


@router.get("/api/analysis-architecture/open-source-tools")
def open_source_tool_references(category: Optional[str] = None, limit: int = 50):
    from ..services.open_source_tool_reference_service import list_open_source_tool_references
    return list_open_source_tool_references(category=category, limit=limit)


@router.post("/api/analysis-architecture/open-source-tools/seed")
def open_source_tool_references_seed(overwrite: bool = True):
    from ..services.open_source_tool_reference_service import seed_open_source_tool_references
    return seed_open_source_tool_references(overwrite=overwrite)

# ========== 分析-决策-行动-反馈闭环 ==========
class DecisionFeedbackEventReq(BaseModel):
    stage: str = Field("analysis", pattern="^(analysis|decision|action|feedback|reanalysis)$")
    source: str = "manual"
    stock_code: Optional[str] = None
    stock_name: Optional[str] = None
    title: Optional[str] = None
    summary: Optional[str] = None
    decision: Optional[str] = None
    action: Optional[str] = None
    expected_outcome: Optional[str] = None
    actual_outcome: Optional[str] = None
    pnl_pct: Optional[float] = None
    max_drawdown_pct: Optional[float] = None
    confidence: Optional[float] = None
    evidence: dict = Field(default_factory=dict)
    next_review_at: Optional[str] = None
    status: str = "open"


@router.get("/api/decision-feedback/snapshot")
def decision_feedback_snapshot():
    from ..services.decision_feedback_service import get_decision_feedback_snapshot
    return get_decision_feedback_snapshot()


@router.get("/api/decision-feedback/events")
def decision_feedback_events(stock_code: Optional[str] = None,
                             stage: Optional[str] = None,
                             limit: int = 100):
    from ..services.decision_feedback_service import list_decision_feedback_events
    return list_decision_feedback_events(stock_code=stock_code, stage=stage, limit=limit)


@router.post("/api/decision-feedback/events")
def decision_feedback_record(body: DecisionFeedbackEventReq):
    from ..services.decision_feedback_service import record_decision_feedback_event
    return record_decision_feedback_event(body.model_dump())


# ========== 风险管理 ==========
class AllocateReq(BaseModel):
    candidates: list[dict]   # [{code, name}]
    total_capital: float
    max_single: float = 0.20


@router.post("/api/risk/allocate")
def risk_allocate(body: AllocateReq):
    from ..services.risk_management import allocate_by_inverse_volatility
    return allocate_by_inverse_volatility(
        body.candidates, body.total_capital, body.max_single,
    )


@router.post("/api/risk/stop_loss/scan")
def risk_stop_loss_scan():
    from ..services.risk_management import check_stop_loss_for_all_positions
    triggers = check_stop_loss_for_all_positions()
    return {"triggers": triggers, "count": len(triggers)}


@router.get("/api/risk/stop_loss/recent")
def risk_stop_loss_recent(limit: int = 20):
    from ..services.risk_management import get_recent_triggers
    return {"items": get_recent_triggers(limit)}


# ========== Playbook 14 手法复盘 ==========
@router.get("/api/playbook/patterns")
def playbook_patterns():
    """返回 14 pattern 的元数据。"""
    from ..services.playbook_service import get_patterns_meta
    return get_patterns_meta()


@router.get("/api/playbook/pattern/{name}")
def playbook_pattern_detail(name: str, limit: int = 50, min_confidence: float = 0.6):
    """某类手法的所有历史案例 + 胜率统计。"""
    from ..services.playbook_service import get_pattern_cases
    return get_pattern_cases(name, limit=limit, min_confidence=min_confidence)


@router.get("/api/playbook/stock/{code}")
def playbook_stock_all(code: str, since_days: int = 180):
    """某股近 N 天所有探测命中。"""
    from ..services.playbook_service import get_stock_all_detections
    rows = get_stock_all_detections(code, since_days)
    return {"count": len(rows), "items": rows}


@router.get("/api/playbook/case")
def playbook_case_chart(code: str, date: str, pattern: str):
    """某次命中的 K 线 + 资金流数据（图示用）。"""
    from ..services.playbook_service import get_stock_case_chart
    return get_stock_case_chart(code, date, pattern)


class PlaybookScanReq(BaseModel):
    since_days: int = 180


@router.post("/api/playbook/scan")
def playbook_scan(body: PlaybookScanReq = None):
    """扫持仓+自选全部历史，落库 + 算收益。"""
    from ..services.playbook_service import scan_all_tracked_stocks
    body = body or PlaybookScanReq()
    return scan_all_tracked_stocks(since_days=body.since_days)


@router.post("/api/playbook/compute_outcomes")
def playbook_compute_outcomes(limit: int = 2000):
    from ..services.playbook_service import compute_outcomes
    return compute_outcomes(limit=limit)



@router.post("/api/playbook/market_weekly/run")
def playbook_market_weekly_run(since_days: int = 10, max_stocks: Optional[int] = None,
                               refresh_market_data: bool = True):
    """手动触发全市场 14 类手法周复盘。"""
    from ..services.playbook_service import scan_market_weekly
    return scan_market_weekly(
        since_days=since_days,
        max_stocks=max_stocks,
        refresh_market_data=refresh_market_data,
    )


@router.get("/api/playbook/market_weekly/latest")
def playbook_market_weekly_latest():
    from ..services.playbook_service import get_latest_market_weekly_run
    r = get_latest_market_weekly_run()
    if not r:
        raise HTTPException(404, "尚无全市场周复盘记录")
    return r


@router.get("/holdings", response_class=HTMLResponse)
@router.get("/holding-changes", response_class=HTMLResponse)
def holding_changes_page():
    try:
        with open("/opt/stock-analyzer/app/templates/holding_changes.html", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        return HTMLResponse("<h1>holding_changes.html 未生成</h1>")


@router.get("/playbook", response_class=HTMLResponse)
def playbook_index_page():
    try:
        with open("/opt/stock-analyzer/app/templates/playbook.html", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        return HTMLResponse("<h1>playbook.html 未生成</h1>")


@router.get("/playbook/pattern/{name}", response_class=HTMLResponse)
def playbook_pattern_page(name: str):
    try:
        with open("/opt/stock-analyzer/app/templates/playbook_pattern.html", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        return HTMLResponse("<h1>playbook_pattern.html 未生成</h1>")


@router.get("/interaction-tracking", response_class=HTMLResponse)
@router.get("/interacted-stocks", response_class=HTMLResponse)
def interaction_tracking_page():
    try:
        with open("/opt/stock-analyzer/app/templates/interaction_tracking.html", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        return HTMLResponse("<h1>interaction_tracking.html 未生成</h1>")


# ========== Dashboard 页面 ==========
@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page():
    try:
        with open("/opt/stock-analyzer/app/templates/dashboard.html", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        return HTMLResponse("<h1>dashboard.html 未生成</h1>")


@router.get("/ai-pk", response_class=HTMLResponse)
def ai_pk_page():
    try:
        with open("/opt/stock-analyzer/app/templates/ai_pk.html", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        return HTMLResponse("<h1>ai_pk.html 未生成</h1>")




@router.get("/trading-cognition", response_class=HTMLResponse)
def trading_cognition_page():
    try:
        with open("/opt/stock-analyzer/app/templates/trading_cognition.html", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        return HTMLResponse("<h1>trading_cognition.html 未生成</h1>")



@router.get("/analysis-architecture", response_class=HTMLResponse)
@router.get("/architecture", response_class=HTMLResponse)
def analysis_architecture_page():
    try:
        with open("/opt/stock-analyzer/app/templates/analysis_architecture.html", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        return HTMLResponse("<h1>analysis_architecture.html 未生成</h1>")

@router.get("/audit", response_class=HTMLResponse)
def audit_page():
    try:
        with open("/opt/stock-analyzer/app/templates/audit.html", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        return HTMLResponse("<h1>audit.html 未生成</h1>")


# ========== PWA 资源（mobile app shell） ==========
_APP_ICON_SVG = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#FAF7F2"/>
      <stop offset="1" stop-color="#EFE4D2"/>
    </linearGradient>
  </defs>
  <rect width="512" height="512" rx="96" fill="url(#bg)"/>
  <path d="M110 360h300" stroke="#A8734A" stroke-width="18" stroke-linecap="round"/>
  <path d="M130 300l64-70 54 38 102-120 16 14" stroke="#B54848" stroke-width="28" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="370" cy="140" r="28" fill="#D4A76A"/>
  <text x="256" y="460" text-anchor="middle" font-family="Songti SC, SimSun, serif" font-size="60" font-weight="600" fill="#2C2A26">股</text>
</svg>"""


@router.get("/icon.svg")
def app_icon_svg():
    return Response(content=_APP_ICON_SVG, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


@router.get("/manifest.webmanifest")
def app_manifest():
    manifest = {
        "name": "股票分析 · A股博弈",
        "short_name": "股票分析",
        "description": "4 AI 多模型股票分析与博弈揭露",
        "lang": "zh-CN",
        "start_url": "/chat",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#FAF7F2",
        "theme_color": "#FAF7F2",
        "icons": [
            {"src": "/icon.svg", "sizes": "192x192", "type": "image/svg+xml", "purpose": "any maskable"},
            {"src": "/icon.svg", "sizes": "512x512", "type": "image/svg+xml", "purpose": "any maskable"},
        ],
        "shortcuts": [
            {"name": "持仓", "url": "/chat?q=positions", "icons": [{"src": "/icon.svg", "sizes": "96x96"}]},
            {"name": "盘中", "url": "/chat?q=intraday", "icons": [{"src": "/icon.svg", "sizes": "96x96"}]},
        ],
    }
    return JSONResponse(manifest, headers={"Cache-Control": "public, max-age=3600"})


_SW_JS = r"""/* Stock Analyzer PWA SW v3 */
const CACHE = 'sa-shell-v3';
const SHELL = ['/icon.svg', '/manifest.webmanifest'];  // 不缓存 /chat /dashboard /playbook 等页面，避免陈旧

self.addEventListener('install', (e) => {
  self.skipWaiting();
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL).catch(() => null)));
});

self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  // 只对 /icon.svg /manifest.webmanifest 这种真静态资源缓存
  const isStaticAsset = /\.(svg|png|jpg|ico|webmanifest)$/.test(url.pathname);

  if (isStaticAsset) {
    e.respondWith(caches.match(req).then(cached => cached || fetch(req).then(r => {
      if (r.ok) { const clone = r.clone(); caches.open(CACHE).then(c => c.put(req, clone)); }
      return r;
    })));
    return;
  }

  // HTML 页面 + API：网络优先，离线兜底
  e.respondWith(
    fetch(req).catch(() => caches.match(req).then(x => x || new Response(
      '<h2>当前离线</h2><p>请检查网络，或刷新重试。</p>',
      {status: 503, headers: {'Content-Type': 'text/html; charset=utf-8'}}
    )))
  );
});
"""


@router.get("/sw.js")
def service_worker():
    return Response(content=_SW_JS, media_type="application/javascript",
                    headers={"Cache-Control": "no-cache",
                             "Service-Worker-Allowed": "/"})


@router.get("/simple", response_class=HTMLResponse)
def simple_chat():
    """零依赖极简聊天页 —— 排除 CDN/Tailwind/SW/marked 所有嫌疑。"""
    return HTMLResponse("""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>极简聊天</title>
<style>
body{font-family:sans-serif;background:#faf7f2;color:#222;padding:12px;max-width:700px;margin:0 auto;}
#feed{border:1px solid #ccc;min-height:300px;padding:10px;border-radius:6px;background:white;margin-bottom:10px;font-size:14px;white-space:pre-wrap;}
#feed .u{color:#a8734a;font-weight:bold;}
#feed .a{color:#4a8060;}
#feed .err{color:#b54848;}
.row{display:flex;gap:8px;}
textarea{flex:1;padding:8px;border:1px solid #ccc;border-radius:4px;font-size:14px;min-height:60px;font-family:inherit;}
button{padding:10px 20px;background:#a8734a;color:white;border:none;border-radius:4px;cursor:pointer;font-size:14px;}
button:disabled{background:#ccc;}
.info{font-size:12px;color:#666;margin-bottom:10px;}
</style></head><body>
<h2>极简聊天测试</h2>
<p class="info">零外部依赖。如果这个页面能用而 /chat 不能，说明 /chat 的 JS 或 CDN 还有问题。</p>
<div id="feed"></div>
<div class="row">
  <textarea id="msg" placeholder="输入消息">你好</textarea>
  <button id="send">发送</button>
</div>
<script>
var feed = document.getElementById("feed");
var msg = document.getElementById("msg");
var btn = document.getElementById("send");
var sid = localStorage.getItem("simple_chat_sid") || null;

function append(role, text){
  var d = document.createElement("div");
  d.className = role === "user" ? "u" : (role === "error" ? "err" : "a");
  d.textContent = (role==="user"?"你：":role==="error"?"错误：":"AI：") + text;
  feed.appendChild(d);
  feed.scrollTop = feed.scrollHeight;
}

btn.onclick = function(){
  var text = msg.value.trim();
  if (!text) { alert("请输入内容"); return; }
  append("user", text);
  msg.value = "";
  btn.disabled = true;
  btn.textContent = "等待中…";
  fetch("/api/chat/send", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({session_id: sid, message: text, mode: "single", model: "deepseek-v4-pro"})
  }).then(function(r){
    if(!r.ok) throw new Error("HTTP " + r.status);
    return r.json();
  }).then(function(d){
    if(d.session_id){ sid = d.session_id; localStorage.setItem("simple_chat_sid", sid); }
    (d.messages || []).forEach(function(m){
      if(m.role === "assistant") append("ai", m.content);
    });
    btn.disabled = false;
    btn.textContent = "发送";
  }).catch(function(e){
    append("error", e.message);
    btn.disabled = false;
    btn.textContent = "发送";
  });
};
</script>
</body></html>""")


@router.get("/diag", response_class=HTMLResponse)
def diag_page():
    """现场诊断：按顺序测每个外部依赖 + 关键 API 端点。"""
    return HTMLResponse("""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>诊断</title>
<style>
body{font-family:monospace,"Consolas";background:#1a1a1a;color:#0f0;padding:16px;font-size:13px;margin:0;line-height:1.6;}
.ok{color:#0f0;} .fail{color:#f33;} .wait{color:#ff0;}
pre{background:#000;padding:10px;border-radius:4px;overflow-x:auto;max-height:400px;}
h2{color:#ff0;border-bottom:1px solid #333;padding-bottom:4px;}
a{color:#0ff;}
</style></head><body>
<h1>🔍 系统诊断</h1>
<div id="log"></div>
<script>
const log = document.getElementById("log");
function add(html){ log.innerHTML += html; }
function ok(s){ return `<div class="ok">✅ ${s}</div>`; }
function fail(s){ return `<div class="fail">❌ ${s}</div>`; }
function wait(s){ return `<div class="wait">⏳ ${s}</div>`; }
function h2(s){ return `<h2>${s}</h2>`; }

async function testUrl(url, name){
  const t0 = performance.now();
  try{
    const r = await fetch(url, {method:"GET"});
    const dt = (performance.now()-t0).toFixed(0);
    if(r.ok) return ok(`${name}: ${r.status} (${dt}ms)`);
    return fail(`${name}: HTTP ${r.status} (${dt}ms)`);
  }catch(e){
    const dt = (performance.now()-t0).toFixed(0);
    return fail(`${name}: ${e.message} (${dt}ms)`);
  }
}

(async () => {
  add(h2("1. 基础浏览器能力"));
  add(ok(`UserAgent: ${navigator.userAgent.slice(0,80)}`));
  add(ok(`Fetch API: ${typeof fetch !== "undefined"}`));
  add(ok(`LocalStorage: ${typeof localStorage !== "undefined"}`));
  add(ok(`ServiceWorker 支持: ${"serviceWorker" in navigator}`));
  if ("serviceWorker" in navigator) {
    const regs = await navigator.serviceWorker.getRegistrations();
    add(regs.length ? wait(`找到 ${regs.length} 个已注册 SW`) : ok("SW 未注册（干净）"));
  }

  add(h2("2. 应用 API"));
  add(await testUrl("/chat", "/chat"));
  add(await testUrl("/dashboard", "/dashboard"));
  add(await testUrl("/playbook", "/playbook"));
  add(await testUrl("/api/positions?status=holding", "/api/positions"));
  add(await testUrl("/sw.js", "/sw.js"));
  add(await testUrl("/manifest.webmanifest", "/manifest"));

  add(h2("3. 外部 CDN"));
  add(await testUrl("https://cdn.staticfile.org/marked/11.1.1/marked.min.js", "staticfile marked"));
  add(await testUrl("https://cdn.staticfile.org/dompurify/3.1.6/purify.min.js", "staticfile dompurify"));
  add(await testUrl("https://cdn.staticfile.org/echarts/5.4.3/echarts.min.js", "staticfile echarts"));
  add(await testUrl("https://cdn.tailwindcss.com", "tailwindcss"));

  add(h2("4. Chat API"));
  const t0 = performance.now();
  try{
    const r = await fetch("/api/chat/send", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({message:"diag ping", mode:"single", model:"deepseek-v4-pro"})
    });
    const dt = (performance.now()-t0).toFixed(0);
    if(r.ok){
      const d = await r.json();
      add(ok(`POST /api/chat/send: 200 (${dt}ms)`));
      add(`<pre>${JSON.stringify(d, null, 2).slice(0,800)}</pre>`);
    } else {
      add(fail(`POST /api/chat/send: HTTP ${r.status} (${dt}ms)`));
    }
  }catch(e){
    add(fail(`POST /api/chat/send: ${e.message}`));
  }

  add(h2("5. 结论"));
  const fails = log.querySelectorAll(".fail").length;
  if (fails === 0) add(ok("所有测试通过。如果 /chat 仍有问题，按 F12 → Console 查看浏览器报错。"));
  else add(fail(`${fails} 个测试失败，上面红色 ❌ 项即瓶颈。`));
})();
</script>
</body></html>""")


@router.get("/reset", response_class=HTMLResponse)
def reset_pwa():
    """浏览器端清除所有 SW + 缓存，修复卡死状态。"""
    return HTMLResponse("""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/>
<title>清除缓存中…</title>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<style>
body{font-family:"Songti SC",serif;background:#FAF7F2;color:#2C2A26;padding:40px;max-width:500px;margin:0 auto;text-align:center;}
h2{color:#A8734A;}
#log{background:#F1ECE3;padding:12px;border-radius:8px;font-size:13px;text-align:left;margin-top:16px;font-family:monospace;white-space:pre-wrap;}
a{display:inline-block;margin-top:14px;padding:10px 20px;background:#A8734A;color:white;border-radius:6px;text-decoration:none;font-size:14px;}
</style></head><body>
<h2>🧹 清除浏览器缓存</h2>
<p>正在清除旧的 Service Worker 和缓存…</p>
<div id="log"></div>
<div id="done" style="display:none;">
<p>✅ 清理完成，可以打开应用了：</p>
<a href="/chat">进入聊天</a>
<a href="/dashboard" style="background:#6B6558;">进入仪表盘</a>
</div>
<script>
const log = document.getElementById("log");
function L(msg){ log.textContent += msg + "\\n"; }

(async () => {
  try {
    if ("serviceWorker" in navigator) {
      const regs = await navigator.serviceWorker.getRegistrations();
      L("找到 " + regs.length + " 个 Service Worker");
      for (const r of regs) {
        await r.unregister();
        L("  → 已卸载: " + (r.scope || "default"));
      }
    } else L("浏览器不支持 SW");
    if ("caches" in window) {
      const keys = await caches.keys();
      L("找到 " + keys.length + " 个缓存");
      for (const k of keys) {
        await caches.delete(k);
        L("  → 已清除: " + k);
      }
    }
    L("");
    L("✅ 清理完成");
    document.getElementById("done").style.display = "block";
  } catch(e) {
    L("❌ " + e.message);
  }
})();
</script>
</body></html>""")


@router.get("/apple-touch-icon.png")
def apple_touch_icon():
    # iOS 16+ 接受 SVG；低版本会自动 fallback 用默认
    return Response(content=_APP_ICON_SVG, media_type="image/svg+xml")


@router.get("/chat", response_class=HTMLResponse)
def chat_page():
    try:
        with open("/opt/stock-analyzer/app/templates/chat.html", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        return HTMLResponse("<h1>chat.html 还没生成（Codex 正在后台生成中）</h1>")


@router.get("/report/{code}", response_class=HTMLResponse)
def report_page(code: str):
    from fastapi.templating import Jinja2Templates
    from fastapi import Request
    try:
        tmpl = Jinja2Templates(directory="/opt/stock-analyzer/app/templates")
        analysis = analyze_stock(code, force_refresh=False)
        daily = _collect_stock_data(code).get("daily", [])

        # 直接渲染（不用 Request）
        template = tmpl.get_template("report.html")
        html = template.render(
            code=code,
            name=analysis.get("name", ""),
            report_date=analysis.get("analyzed_at", ""),
            stock=analysis.get("data_snapshot", {}),
            position=None,
            daily=daily,
            ai_opinions=analysis.get("opinions", {}),
            consensus=analysis.get("consensus", ""),
            reports=[],
        )
        return HTMLResponse(html)
    except FileNotFoundError:
        return HTMLResponse("<h1>report.html 还没生成（Codex 正在后台生成中）</h1>")
    except Exception as e:
        return HTMLResponse(f"<h1>Error</h1><pre>{e}</pre>")
