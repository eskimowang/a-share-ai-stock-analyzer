"""回测引擎 —— 把历史 AI 判断喂价格数据，算每家 AI 真实胜率。

来源: game_analysis_log（每次 job_closing 存入的判断）
用法: 相比 review_pending 的"7 天后对比当前价"，这个能回测
     任意 hold_days (1/3/7/14/30)，且对比历史真实价格（不是 now）。
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

from ..db import db, execute, query_all, query_one

log = logging.getLogger(__name__)


def _ensure_backtest_tables():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS backtest_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            hold_days INTEGER,
            start_date TEXT,
            end_date TEXT,
            total_analyses INTEGER,
            hit_count INTEGER,
            win_rate REAL,
            avg_return REAL,
            by_ai TEXT,      -- JSON: {"DeepSeek": {wins, n, wr, avg}, ...}
            by_verdict TEXT  -- JSON: {"吸筹": {...}, ...}
        );
        """)


def _price_at(code: str, date: str) -> Optional[float]:
    """拿某个日期的收盘价（找不到则找最近的后一个交易日）。"""
    r = query_one(
        "SELECT close FROM daily_quotes WHERE stock_code=? AND trade_date >= ? "
        "ORDER BY trade_date LIMIT 1", (code, date),
    )
    return r.get("close") if r else None


def _benchmark_price_at(index_code: str, date: str) -> Optional[float]:
    """拿 benchmark 某日收盘（找不到则找后一个交易日）。"""
    r = query_one(
        "SELECT close FROM benchmark_quotes WHERE index_code=? AND trade_date >= ? "
        "ORDER BY trade_date LIMIT 1", (index_code, date),
    )
    return r.get("close") if r else None


def _benchmark_return(index_code: str, start_date: str, hold_days: int) -> Optional[float]:
    """给 benchmark 某起始日持 N 天的涨跌率。"""
    end_date = (datetime.strptime(start_date, "%Y-%m-%d") +
                timedelta(days=hold_days + 3)).strftime("%Y-%m-%d")
    p0 = _benchmark_price_at(index_code, start_date)
    p1 = _benchmark_price_at(index_code, end_date)
    if not p0 or not p1:
        return None
    return (p1 - p0) / p0


def run_backtest(hold_days: int = 7, since_days: int = 180,
                 benchmark: str = "000001.SH") -> dict:
    """对 since_days 天内的 game_analysis_log 做完整回测 + β 剥离。

    新增:
    - alpha_return: 剥离 benchmark 后的真实 α 收益
    - alpha_win_rate: 剥 β 后的胜率（"跑赢大盘"才算命中）
    """
    _ensure_backtest_tables()
    start = datetime.now() - timedelta(days=since_days)
    start_str = start.strftime("%Y-%m-%d")

    rows = query_all(
        "SELECT id, stock_code, analysis_date, ai_source, verdict, "
        "predicted_direction, price_at_analysis "
        "FROM game_analysis_log "
        "WHERE analysis_date >= ? AND price_at_analysis IS NOT NULL "
        "ORDER BY analysis_date",
        (start_str,),
    )
    if not rows:
        return {"error": "无历史分析", "total": 0}

    total = len(rows)
    hits = 0
    alpha_hits = 0
    returns = []
    alphas = []
    by_ai = {}
    by_verdict = {}

    for r in rows:
        ad = (r.get("analysis_date") or "")[:10]
        code = r["stock_code"]
        entry = r.get("price_at_analysis")
        if not entry or not ad:
            continue
        exit_date = (datetime.strptime(ad, "%Y-%m-%d") +
                     timedelta(days=hold_days)).strftime("%Y-%m-%d")
        exit_p = _price_at(code, exit_date)
        if not exit_p:
            continue
        ret = (exit_p - entry) / entry

        # β 剥离
        bench_ret = _benchmark_return(benchmark, ad, hold_days)
        alpha = (ret - bench_ret) if bench_ret is not None else None

        direction = r.get("predicted_direction") or ""
        hit = False
        alpha_hit = False
        if "涨" in direction or r.get("verdict") in ("吸筹", "主升浪"):
            hit = ret > 0
            alpha_hit = (alpha is not None and alpha > 0)
        elif "跌" in direction or r.get("verdict") in ("出货", "诱多", "假突破"):
            hit = ret < 0
            alpha_hit = (alpha is not None and alpha < 0)
        elif "持平" in direction or r.get("verdict") in ("震荡", "洗盘", "正常震荡"):
            hit = abs(ret) < 0.03
            alpha_hit = (alpha is not None and abs(alpha) < 0.03)
        if hit:
            hits += 1
        if alpha_hit:
            alpha_hits += 1
        returns.append(ret)
        if alpha is not None:
            alphas.append(alpha)

        ai = r.get("ai_source", "unknown")
        by_ai.setdefault(ai, {"n": 0, "wins": 0, "alpha_wins": 0,
                                "returns": [], "alphas": []})
        by_ai[ai]["n"] += 1
        by_ai[ai]["wins"] += 1 if hit else 0
        by_ai[ai]["alpha_wins"] += 1 if alpha_hit else 0
        by_ai[ai]["returns"].append(ret)
        if alpha is not None:
            by_ai[ai]["alphas"].append(alpha)

        vd = r.get("verdict", "-")
        by_verdict.setdefault(vd, {"n": 0, "wins": 0, "alpha_wins": 0})
        by_verdict[vd]["n"] += 1
        by_verdict[vd]["wins"] += 1 if hit else 0
        by_verdict[vd]["alpha_wins"] += 1 if alpha_hit else 0

    # 聚合
    for ai, d in by_ai.items():
        d["win_rate"] = d["wins"] / d["n"] if d["n"] else 0
        d["avg_return"] = sum(d["returns"]) / len(d["returns"]) if d["returns"] else 0
        d["alpha_win_rate"] = d["alpha_wins"] / len(d["alphas"]) if d["alphas"] else None
        d["avg_alpha"] = sum(d["alphas"]) / len(d["alphas"]) if d["alphas"] else None
        del d["returns"]
        del d["alphas"]
    for vd, d in by_verdict.items():
        d["win_rate"] = d["wins"] / d["n"] if d["n"] else 0
        d["alpha_win_rate"] = d["alpha_wins"] / d["n"] if d["n"] else 0

    import json as _json
    avg_ret = sum(returns) / len(returns) if returns else 0
    win_rate = hits / len(returns) if returns else 0
    avg_alpha = sum(alphas) / len(alphas) if alphas else None
    alpha_win_rate = alpha_hits / len(alphas) if alphas else None

    # benchmark 同期 baseline
    bench_returns = []
    for r in rows:
        ad = (r.get("analysis_date") or "")[:10]
        if not ad: continue
        b = _benchmark_return(benchmark, ad, hold_days)
        if b is not None:
            bench_returns.append(b)
    bench_avg = sum(bench_returns) / len(bench_returns) if bench_returns else None
    bench_positive_rate = (sum(1 for b in bench_returns if b > 0) / len(bench_returns)
                            if bench_returns else None)

    execute(
        "INSERT INTO backtest_runs(hold_days, start_date, end_date, total_analyses, "
        "hit_count, win_rate, avg_return, by_ai, by_verdict) VALUES (?,?,?,?,?,?,?,?,?)",
        (hold_days, start_str, datetime.now().strftime("%Y-%m-%d"),
         total, hits, win_rate, avg_ret,
         _json.dumps(by_ai, ensure_ascii=False),
         _json.dumps(by_verdict, ensure_ascii=False)),
    )

    return {
        "hold_days": hold_days,
        "benchmark": benchmark,
        "total_analyses": total,
        "verified": len(returns),
        "hits": hits,
        "win_rate": win_rate,
        "avg_return": avg_ret,
        "alpha_win_rate": alpha_win_rate,  # 剥 β 后"跑赢大盘"的比率
        "avg_alpha": avg_alpha,              # 平均 α
        "benchmark_avg_return": bench_avg,   # 同期上证平均收益
        "benchmark_positive_rate": bench_positive_rate,  # 上证同期上涨天占比
        "by_ai": by_ai,
        "by_verdict": by_verdict,
    }


def get_latest_backtest() -> Optional[dict]:
    _ensure_backtest_tables()
    r = query_one(
        "SELECT * FROM backtest_runs ORDER BY id DESC LIMIT 1"
    )
    if not r:
        return None
    import json as _json
    try:
        r["by_ai"] = _json.loads(r["by_ai"] or "{}")
    except Exception:
        r["by_ai"] = {}
    try:
        r["by_verdict"] = _json.loads(r["by_verdict"] or "{}")
    except Exception:
        r["by_verdict"] = {}
    return r
