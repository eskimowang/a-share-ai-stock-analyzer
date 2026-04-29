"""单股深度分析服务 —— 拉数据 + 4 家 AI 并行 + 仲裁 + 缓存。"""
import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from ..config import CONFIG
from ..db import db, query_all, query_one, execute
from ..data_sources import UnifiedDataSource
from ..ai.multi_brain import MultiBrain, build_brains_from_config
from ..ai.prompts import SYSTEM_PROMPT, SYSTEM_PROMPT_ADVERSARY, build_analysis_prompt

log = logging.getLogger(__name__)

_ds = UnifiedDataSource(tushare_token=CONFIG["data_sources"]["tushare"].get("token"))
_brains = build_brains_from_config(CONFIG)
_mb = MultiBrain(_brains)


def get_cached_analysis(code: str, max_age_hours: int = 6) -> Optional[dict]:
    """命中最近 N 小时内的分析则返回。"""
    cutoff = (datetime.now() - timedelta(hours=max_age_hours)).strftime("%Y-%m-%d %H:%M:%S")
    row = query_one(
        "SELECT * FROM daily_analysis WHERE stock_code=? AND created_at >= ? "
        "ORDER BY created_at DESC LIMIT 1",
        (code, cutoff),
    )
    if row and row.get("ai_summary"):
        try:
            data = json.loads(row["ai_summary"])
            data["_from_cache"] = True
            data["_cached_at"] = row["created_at"]
            return data
        except Exception:
            pass
    return None


def _collect_stock_data(code: str) -> dict:
    """从 DB（优先）+ 实时接口（补齐）组装完整股票数据包。"""
    # 1. 基本信息
    basics = {}
    try:
        if _ds.tushare:
            basics = _ds.tushare.get_basics(code)
    except Exception as e:
        log.warning(f"tushare basics fail: {e}")

    # 2. 日线（从 DB 读过去 60 天）
    daily = query_all(
        "SELECT trade_date, open, high, low, close, volume, amount, change_pct "
        "FROM daily_quotes WHERE stock_code=? ORDER BY trade_date DESC LIMIT 60",
        (code,),
    )
    daily.reverse()  # 时间升序

    # 3. 估值快照
    db_snapshot = query_one(
        "SELECT trade_date, close, pe_ttm, pb, ps_ttm, turnover_rate, total_mv "
        "FROM daily_basic WHERE stock_code=? ORDER BY trade_date DESC LIMIT 1",
        (code,),
    ) or {}

    # 4. 财务指标（最新一期）
    fin = query_one(
        "SELECT * FROM financials WHERE stock_code=? ORDER BY report_period DESC LIMIT 1",
        (code,),
    ) or {}

    # 5. 研报
    reports = query_all(
        "SELECT report_date, broker, author, rating, title "
        "FROM reports_cache WHERE stock_code=? "
        "ORDER BY report_date DESC LIMIT 10",
        (code,),
    )
    reports_fmt = [
        {"report_date": r["report_date"], "org_name": r["broker"],
         "author_name": r["author"], "rating": r["rating"]}
        for r in reports
    ]

    # 6. 持仓（如果是用户持仓）
    position = query_one(
        "SELECT p.id, SUM(CASE WHEN t.trade_type='buy' THEN t.quantity ELSE -t.quantity END) as holding_qty, "
        "SUM(CASE WHEN t.trade_type='buy' THEN t.price*t.quantity+t.fee ELSE -t.price*t.quantity+t.fee END) as net_cost "
        "FROM positions p JOIN trades t ON p.id=t.position_id "
        "WHERE p.stock_code=? AND p.status='holding' GROUP BY p.id",
        (code,),
    )

    # 7. 财务趋势 (8 期)
    financials_trend = query_all(
        "SELECT report_period, roe, gross_margin, net_margin "
        "FROM financials WHERE stock_code=? ORDER BY report_period DESC LIMIT 8",
        (code,),
    )
    financials_trend.reverse()  # 升序

    # 8. 资金流 (近 5 日)
    moneyflow = query_all(
        "SELECT trade_date, net_mf_amount, buy_lg_amount, sell_lg_amount, net_d5_amount "
        "FROM moneyflow_cache WHERE stock_code=? ORDER BY trade_date DESC LIMIT 5",
        (code,),
    )

    # 9. 龙虎榜
    top_list = query_all(
        "SELECT trade_date, reason, net_buy_amount, total_buy "
        "FROM top_list_cache WHERE stock_code=? ORDER BY trade_date DESC LIMIT 5",
        (code,),
    )

    # 10. 股东户数趋势
    holder_trend = query_all(
        "SELECT end_date, holder_num FROM holder_number_cache "
        "WHERE stock_code=? ORDER BY end_date LIMIT 8",
        (code,),
    )

    # 11. 融资融券趋势
    margin_trend = query_all(
        "SELECT trade_date, rzye, rqye FROM margin_detail_cache "
        "WHERE stock_code=? ORDER BY trade_date DESC LIMIT 5",
        (code,),
    )

    # 12. Codex 信息包 + 推荐来源记忆
    codex_info = ""
    recommendation_memory = {}
    try:
        from .recommendation_memory_service import get_recommendation_memory_for_stock
        recommendation_memory = get_recommendation_memory_for_stock(code, limit=8)
        if recommendation_memory.get("items"):
            codex_info = (codex_info + "\n\n" if codex_info else "") + recommendation_memory.get("summary", "")
    except Exception as e:
        log.warning("读取推荐记忆失败 %s: %s", code, e)

    try:
        from .trading_cognition_service import format_trading_cognition_for_prompt
        cognition = format_trading_cognition_for_prompt(
            context=f"单股分析 {code} {basics.get('name','')} 持仓 买入 卖出 止损 仓位",
            limit=12,
        )
        codex_info = (codex_info + "\n\n" if codex_info else "") + cognition
    except Exception as e:
        log.warning("读取交易认知规则失败 %s: %s", code, e)


    try:
        from .analysis_architecture_service import format_analysis_architecture_for_prompt
        architecture = format_analysis_architecture_for_prompt(
            context=f"单股分析 {code} {basics.get('name','')} 研报 数据 风控 仓位 AI PK",
            limit=10,
        )
        codex_info = (codex_info + "\n\n" if codex_info else "") + architecture
    except Exception as e:
        log.warning("读取分析架构失败 %s: %s", code, e)


    try:
        from .open_source_tool_reference_service import format_open_source_tool_references_for_prompt
        tools_ref = format_open_source_tool_references_for_prompt(
            context=f"单股分析 {code} {basics.get('name','')} 研报 数据 回测 风控 技术指标",
            limit=6,
        )
        codex_info = (codex_info + "\n\n" if codex_info else "") + tools_ref
    except Exception as e:
        log.warning("读取开源工具参考失败 %s: %s", code, e)


    try:
        from .decision_feedback_service import format_decision_feedback_for_prompt
        feedback = format_decision_feedback_for_prompt(
            context=f"单股分析 {code} {basics.get('name','')} 决策 行动 反馈 盈利 复盘",
            limit=6,
        )
        codex_info = (codex_info + "\n\n" if codex_info else "") + feedback
    except Exception as e:
        log.warning("读取决策反馈闭环失败 %s: %s", code, e)

    return {
        "code": code,
        "name": basics.get("name", ""),
        "industry": basics.get("industry", "N/A"),
        "latest_date": db_snapshot.get("trade_date") or (daily[-1]["trade_date"] if daily else ""),
        "close": db_snapshot.get("close") or (daily[-1]["close"] if daily else None),
        "change_pct": (daily[-1]["change_pct"] if daily else None),
        "pe_ttm": db_snapshot.get("pe_ttm"),
        "pb": db_snapshot.get("pb"),
        "total_mv": db_snapshot.get("total_mv"),
        "report_period": fin.get("report_period"),
        "revenue": None,
        "net_profit": None,
        "roe": fin.get("roe"),
        "gross_margin": fin.get("gross_margin"),
        "net_margin": fin.get("net_margin"),
        "debt_ratio": None,
        "daily": daily,
        "reports": reports_fmt,
        "position": position,
        "codex_info": codex_info,
        "recommendation_memory": recommendation_memory,
        # 差异化 prompt 用的额外字段
        "daily_basic": db_snapshot,
        "financials": financials_trend,
        "moneyflow": moneyflow,
        "top_list": top_list,
        "holder_trend": holder_trend,
        "margin_trend": margin_trend,
    }


def analyze_stock(code: str, with_adversary: bool = True,
                    force_refresh: bool = False,
                    max_tokens: int = 2000,
                    differentiated: bool = True) -> dict:
    """对一只股票做完整分析。

    differentiated=True: 每家 AI 看不同数据片（打破同源污染），默认开启
    differentiated=False: 所有 AI 看相同全数据
    """
    if not force_refresh:
        cached = get_cached_analysis(code)
        if cached:
            log.info(f"{code} 命中缓存")
            return cached

    # 拉数据
    data = _collect_stock_data(code)

    # 4 家 AI 并行
    if differentiated:
        opinions = _mb.analyze_differentiated(data, max_tokens=max_tokens)
    elif with_adversary:
        opinions = _mb.analyze_with_adversary(data, adversary_name="Claude", max_tokens=max_tokens)
    else:
        opinions = _mb.analyze(data, max_tokens=max_tokens)

    # 仲裁（反方时用 DeepSeek，否则默认 Claude）
    arbiter_name = "DeepSeek" if with_adversary else "Claude"
    arbiter = next((b for b in _brains if b.name == arbiter_name), _brains[0])
    consensus = _mb.consensus(data, opinions, arbiter=arbiter, max_tokens=1500)

    result = {
        "code": code,
        "name": data.get("name"),
        "analyzed_at": datetime.now().isoformat(),
        "data_snapshot": {
            "close": data.get("close"),
            "pe_ttm": data.get("pe_ttm"),
            "pb": data.get("pb"),
            "roe": data.get("roe"),
            "reports_count": len(data.get("reports", [])),
        },
        "opinions": opinions,
        "consensus": consensus,
        "with_adversary": with_adversary,
        "_from_cache": False,
    }

    try:
        from .decision_feedback_service import record_decision_feedback_event
        record_decision_feedback_event({
            "stage": "analysis",
            "source": "single_stock_analysis",
            "stock_code": code,
            "stock_name": data.get("name") or "",
            "title": f"{code} {data.get('name') or ''} 单股分析",
            "summary": str(consensus)[:1000],
            "decision": "等待用户或订单系统形成行动决策",
            "expected_outcome": "后续用5/20/60日收益、回撤和纪律执行反馈检验",
            "confidence": None,
            "status": "open",
        })
    except Exception as e:
        log.warning("记录决策反馈事件失败 %s: %s", code, e)

    # 存 DB
    try:
        execute(
            "INSERT INTO daily_analysis(stock_code, analysis_date, close_price, ai_summary, signal) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(stock_code, analysis_date) DO UPDATE SET "
            "close_price=excluded.close_price, ai_summary=excluded.ai_summary, "
            "signal=excluded.signal, created_at=CURRENT_TIMESTAMP",
            (code, datetime.now().strftime("%Y-%m-%d"),
             data.get("close"), json.dumps(result, ensure_ascii=False), "unknown"),
        )
    except Exception as e:
        log.warning(f"保存分析失败: {e}")

    return result
