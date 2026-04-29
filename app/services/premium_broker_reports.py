"""中信证券 + 中金公司 研报收集服务 —— 填补 AKShare 东财源的空白。

规模策略（按优先级）:
  ⭐⭐⭐ 持仓 (8) + 自选 —— 每周日
  ⭐⭐   IVD 上市公司 (50 只) —— 每月 15 号
  ⭐    Top 20 热门 × 10 行业 —— 手工触发

数据落 reports_cache 表（同 AKShare 走一起），broker 字段区分来源。
"""
import json
import logging
import time
import concurrent.futures
from datetime import datetime
from typing import Optional

from ..config import CONFIG
from ..db import db, execute, query_all, query_one
from ..ai.local_cli import LocalCLIClient
from ..ai.info_collector import CodexInfoCollector
from .stock_universe import filter_tradable

log = logging.getLogger(__name__)


PREMIUM_BROKERS = ["中信证券", "中金公司"]

TOP_INDUSTRIES_FOR_HEAT = [
    "半导体", "新能源汽车", "医药生物", "银行", "食品饮料",
    "机械设备", "电力设备", "国防军工", "房地产", "计算机",
]


def _ensure_tables():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS premium_report_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            scope TEXT,            -- positions / watchlist / ivd / custom
            broker_filter TEXT,
            stock_count INTEGER,
            reports_collected INTEGER,
            duration_seconds REAL,
            status TEXT,
            error_msg TEXT
        );
        -- reports_cache 已存在，不改 schema
        """)


def _collect_one_stock_broker(info: CodexInfoCollector, code: str, name: str,
                                broker: str, months: int = 6) -> list[dict]:
    """Codex 联网找某家券商对某只股的研报。"""
    prompt = f"""【任务】联网搜索 **{broker}** 研究部近 {months} 个月发布的关于 **{name}({code})** 的所有研报。

找到后逐条列出。即使只找到 1 份也要列出。找不到就 reports 留空。

【输出严格 JSON】
{{
  "broker": "{broker}",
  "stock_code": "{code}",
  "stock_name": "{name}",
  "collected_date": "YYYY-MM-DD",
  "reports": [
    {{
      "report_date": "YYYY-MM-DD",
      "title": "研报完整标题",
      "analyst": "首席/团队名字",
      "rating": "买入/增持/持有/中性/减持/卖出（如无请留空）",
      "target_price": 数字或 null,
      "core_thesis": "核心观点一句话",
      "summary": "1-2 句话摘要",
      "source": "慧博/券商官网/wind/研报码"
    }}
  ],
  "coverage_level": "A/B/C/D (A=覆盖深度/频繁；D=未覆盖)",
  "note": "如果该券商停止覆盖或未覆盖这只股，直接说明"
}}

如果数据不足，reports 可以为空。严格 JSON，不要前言。
"""
    return info._query(prompt, max_tokens=2500)


def _save_reports(code: str, broker: str, reports: list[dict]) -> int:
    """落库到 reports_cache。UNIQUE 约束假定 (stock_code, report_date, broker, title)。"""
    saved = 0
    for r in reports:
        try:
            rating = r.get("rating") or ""
            execute(
                "INSERT OR IGNORE INTO reports_cache"
                "(stock_code, report_date, broker, author, rating, title, data_source) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    code,
                    (r.get("report_date") or "")[:10],
                    broker,
                    (r.get("analyst") or "")[:100],
                    rating[:20],
                    (r.get("title") or "")[:400],
                    "codex_premium",
                ),
            )
            saved += 1
        except Exception as e:
            log.warning(f"落库失败 {code} {broker}: {e}")
    return saved


def _get_target_stocks(scope: str) -> list[dict]:
    """按 scope 返回目标股列表 [{code, name}]。"""
    if scope == "positions":
        rows = query_all(
            "SELECT DISTINCT stock_code as code, stock_name as name "
            "FROM positions WHERE status='holding'"
        )
    elif scope == "watchlist":
        rows = query_all(
            "SELECT stock_code as code, stock_name as name FROM watchlist"
        )
    elif scope == "positions_watchlist":
        rows = query_all(
            "SELECT DISTINCT stock_code as code, stock_name as name FROM positions WHERE status='holding' "
            "UNION SELECT stock_code as code, stock_name as name FROM watchlist"
        )
    elif scope == "ivd":
        rows = query_all(
            "SELECT DISTINCT code, name FROM ivd_companies "
            "WHERE exchange IN ('SH','SZ') AND is_active=1"
        )
    else:
        rows = []
    return [{"code": r["code"], "name": r["name"] or ""} for r in rows]


def run_premium_report_collection(scope: str = "positions_watchlist",
                                     brokers: Optional[list[str]] = None,
                                     months: int = 6,
                                     max_concurrent: int = 4) -> dict:
    """为每只股 × 每家 premium broker 采集研报。"""
    _ensure_tables()
    brokers = brokers or PREMIUM_BROKERS
    stocks = _get_target_stocks(scope)
    if not stocks:
        return {"status": "failed", "error": f"scope='{scope}' 返回空"}

    run_id = execute(
        "INSERT INTO premium_report_runs(scope, broker_filter, stock_count, status) "
        "VALUES (?,?,?,?)",
        (scope, json.dumps(brokers, ensure_ascii=False), len(stocks), "running"),
    )
    log.info(f"[中信中金研报] Run #{run_id} 启动, {len(stocks)} 股 × {len(brokers)} 券商 = {len(stocks)*len(brokers)} 格")
    start = time.time()

    codex = LocalCLIClient(
        name="Codex", agent="codex",
        endpoint=CONFIG["ai"]["local_cli"]["endpoint"], timeout=300,
    )
    info = CodexInfoCollector(codex)

    tasks = [(s, b) for s in stocks for b in brokers]

    def _one(task):
        stock, broker = task
        code, name = stock["code"], stock["name"]
        try:
            data = _collect_one_stock_broker(info, code, name, broker, months)
            reports = data.get("reports", []) if isinstance(data, dict) else []
            saved = _save_reports(code, broker, reports)
            return {
                "code": code, "broker": broker,
                "collected": len(reports), "saved": saved,
                "coverage": data.get("coverage_level") if isinstance(data, dict) else "D",
            }
        except Exception as e:
            return {"code": code, "broker": broker, "collected": 0, "saved": 0, "error": str(e)[:200]}

    total_collected = 0
    total_saved = 0
    detail = []
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrent) as pool:
            for fut in concurrent.futures.as_completed([pool.submit(_one, t) for t in tasks]):
                r = fut.result()
                detail.append(r)
                total_collected += r.get("collected", 0)
                total_saved += r.get("saved", 0)

        duration = time.time() - start
        execute(
            "UPDATE premium_report_runs SET reports_collected=?, duration_seconds=?, status=? WHERE id=?",
            (total_saved, duration, "success", run_id),
        )
        log.info(f"[中信中金研报] Run #{run_id} 完成: 落库 {total_saved}/{total_collected} 份，{duration:.0f}s")

        return {
            "status": "success",
            "run_id": run_id,
            "stocks": len(stocks),
            "brokers": brokers,
            "reports_collected": total_collected,
            "reports_saved": total_saved,
            "duration_seconds": duration,
            "detail": detail[:20],  # 只返前 20 条摘要
        }

    except Exception as e:
        log.exception(f"[中信中金研报] Run #{run_id} 异常")
        execute(
            "UPDATE premium_report_runs SET status=?, error_msg=?, duration_seconds=? WHERE id=?",
            ("failed", str(e), time.time() - start, run_id),
        )
        return {"status": "failed", "error": str(e)}


def get_premium_reports_for_stock(code: str, limit: int = 20) -> list[dict]:
    """拉某股的中信+中金研报（和东财源分开看）。"""
    _ensure_tables()
    return query_all(
        "SELECT * FROM reports_cache WHERE stock_code=? AND broker IN ('中信证券','中金公司') "
        "ORDER BY report_date DESC LIMIT ?",
        (code, limit),
    )


def _pick_hot_stocks_by_industry(info: CodexInfoCollector, industry: str,
                                   top_n: int = 20) -> list[dict]:
    """Codex 联网选某行业热度/流动性/市值 top-N 的 A 股。"""
    prompt = f"""【任务】列出 A 股 **{industry}** 行业近 1 个月**最热门 / 流动性最好 / 市值最高**的 top {top_n} 只股票。

标准: 综合考虑市值、换手率、机构关注度、近期涨跌幅热度。

【输出 JSON】
{{
  "industry": "{industry}",
  "picked_date": "YYYY-MM-DD",
  "top_stocks": [
    {{"rank": 1, "code": "6位代码", "name": "公司名", "reason": "为什么上榜（市值/热度/景气）"}}
  ]
}}

严格 JSON，top_stocks 数量严格为 {top_n}。
"""
    raw = info._query(prompt, max_tokens=3000)
    if not isinstance(raw, dict):
        return []
    stocks = raw.get("top_stocks", []) or []
    out = []
    seen = set()
    for s in stocks:
        code = str(s.get("code", "")).strip().zfill(6)
        if len(code) != 6 or not code.isdigit() or code in seen:
            continue
        seen.add(code)
        out.append({"code": code, "name": s.get("name", "")})
        if len(out) >= top_n:
            break
    # 过滤不可交易股
    if out:
        tradable = set(filter_tradable([s["code"] for s in out]))
        out = [s for s in out if s["code"] in tradable]
    return out


def run_hot_stocks_premium_reports(top_per_industry: int = 20,
                                      industries: Optional[list[str]] = None,
                                      brokers: Optional[list[str]] = None,
                                      months: int = 6,
                                      max_concurrent: int = 5) -> dict:
    """月度：Top N × 每个行业 × 中信中金 研报采集。"""
    _ensure_tables()
    industries = industries or TOP_INDUSTRIES_FOR_HEAT
    brokers = brokers or PREMIUM_BROKERS

    start = time.time()
    log.info(f"[中信中金·热股月采] {len(industries)} 行业 × top {top_per_industry} × {len(brokers)} 券商")

    codex = LocalCLIClient(
        name="Codex", agent="codex",
        endpoint=CONFIG["ai"]["local_cli"]["endpoint"], timeout=300,
    )
    info = CodexInfoCollector(codex)

    # Stage 1: 每个行业并行挑 top N
    all_stocks = []
    def _pick(ind):
        try:
            return ind, _pick_hot_stocks_by_industry(info, ind, top_per_industry)
        except Exception as e:
            return ind, []

    log.info("[热股月采 1/2] 并行挑选各行业龙头...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(5, len(industries))) as pool:
        for fut in concurrent.futures.as_completed([pool.submit(_pick, ind) for ind in industries]):
            ind, stocks = fut.result()
            log.info(f"  {ind}: {len(stocks)} 只")
            for s in stocks:
                s["industry"] = ind
                all_stocks.append(s)

    # 去重
    seen = set()
    unique = []
    for s in all_stocks:
        if s["code"] not in seen:
            seen.add(s["code"])
            unique.append(s)
    log.info(f"  去重后 {len(unique)} 只（原 {len(all_stocks)}）")

    # Stage 2: 走 premium_report_collection
    run_id = execute(
        "INSERT INTO premium_report_runs(scope, broker_filter, stock_count, status) "
        "VALUES (?,?,?,?)",
        ("hot_stocks", json.dumps(brokers, ensure_ascii=False), len(unique), "running"),
    )

    tasks = [(s, b) for s in unique for b in brokers]
    total_saved = total_collected = 0
    detail = []
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrent) as pool:
            def _one(task):
                stock, broker = task
                try:
                    data = _collect_one_stock_broker(
                        info, stock["code"], stock["name"], broker, months
                    )
                    reports = data.get("reports", []) if isinstance(data, dict) else []
                    saved = _save_reports(stock["code"], broker, reports)
                    return {
                        "code": stock["code"], "name": stock["name"],
                        "industry": stock["industry"], "broker": broker,
                        "collected": len(reports), "saved": saved,
                        "coverage": data.get("coverage_level") if isinstance(data, dict) else "D",
                    }
                except Exception as e:
                    return {"code": stock["code"], "broker": broker, "collected": 0,
                            "saved": 0, "error": str(e)[:150]}

            for fut in concurrent.futures.as_completed([pool.submit(_one, t) for t in tasks]):
                r = fut.result()
                detail.append(r)
                total_collected += r.get("collected", 0)
                total_saved += r.get("saved", 0)

        duration = time.time() - start
        execute(
            "UPDATE premium_report_runs SET reports_collected=?, duration_seconds=?, status=? WHERE id=?",
            (total_saved, duration, "success", run_id),
        )
        try:
            from .recommendation_memory_service import record_recommendation_batch
            recommendation_memory = record_recommendation_batch(
                source_key="hot_stocks_monthly",
                source_name="热股月采",
                source_type="monthly_hot_stocks",
                items=unique,
                batch_id=f"premium_report_runs:{run_id}",
                recommendation_date=datetime.now().strftime("%Y-%m-%d"),
                default_horizon_days=60,
                context={
                    "run_id": run_id,
                    "industries": industries,
                    "brokers": brokers,
                    "top_per_industry": top_per_industry,
                    "reports_saved": total_saved,
                },
            )
        except Exception as e:
            log.warning("热股月采推荐记忆失败: %s", e)
            recommendation_memory = {"status": "failed", "error": str(e)}
        return {
            "status": "success", "run_id": run_id,
            "industries": len(industries),
            "unique_stocks": len(unique),
            "total_grids": len(tasks),
            "reports_saved": total_saved,
            "reports_collected": total_collected,
            "duration_seconds": duration,
            "recommendation_memory": recommendation_memory,
            "hot_stocks": unique[:80],
        }
    except Exception as e:
        log.exception(f"热股月采异常")
        execute(
            "UPDATE premium_report_runs SET status=?, error_msg=?, duration_seconds=? WHERE id=?",
            ("failed", str(e), time.time() - start, run_id),
        )
        return {"status": "failed", "error": str(e)}


def get_latest_run_summary() -> Optional[dict]:
    _ensure_tables()
    row = query_one(
        "SELECT * FROM premium_report_runs ORDER BY id DESC LIMIT 1"
    )
    return row
