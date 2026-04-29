"""博弈记忆服务 —— 核心：每次分析都存证据 + 历史记忆反馈给 AI。

这是"不断强化分析能力"的底层机制。

用户核心洞察：
  "永远是人之间的战争。哪怕用量化也是人或者AI定的策略"
  所以个股几方力量博弈分析是核心分析。
"""
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Optional

from ..db import db, query_all, query_one, execute

log = logging.getLogger(__name__)


VERDICT_KEYWORDS = {
    "吸筹": ["吸筹", "底部吸筹", "建仓"],
    "出货": ["出货", "派发", "高位出货"],
    "诱多": ["诱多", "假突破", "拉高诱多"],
    "诱空": ["诱空", "恐吓"],
    "洗盘": ["洗盘", "震荡洗盘"],
    "假突破": ["假突破"],
    "主升浪": ["主升浪", "加速上涨"],
    "正常震荡": ["震荡", "横盘", "整理"],
}


def detect_verdict(text: str) -> Optional[str]:
    """从 AI 输出文本里检测割韭菜行为定性"""
    if not text:
        return None
    for verdict, keywords in VERDICT_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                return verdict
    return None


def save_analysis(
    stock_code: str, stock_name: str,
    ai_source: str, raw_analysis: str,
    price_at_analysis: Optional[float] = None,
    predicted_direction: Optional[str] = None,
    predicted_target: Optional[float] = None,
    predicted_timeframe: int = 7,
    evidence: Optional[dict] = None,
) -> int:
    """保存一次分析到记忆。"""
    verdict = detect_verdict(raw_analysis)
    ev = evidence or {}
    return execute(
        "INSERT INTO game_analysis_log"
        "(stock_code, stock_name, ai_source, verdict, price_at_analysis, "
        " predicted_direction, predicted_target, predicted_timeframe, "
        " evidence_kline, evidence_moneyflow, evidence_toplist, evidence_holder, raw_analysis) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            stock_code, stock_name, ai_source, verdict,
            price_at_analysis,
            predicted_direction, predicted_target, predicted_timeframe,
            json.dumps(ev.get("kline"), ensure_ascii=False, default=str) if ev.get("kline") else None,
            json.dumps(ev.get("moneyflow"), ensure_ascii=False, default=str) if ev.get("moneyflow") else None,
            json.dumps(ev.get("toplist"), ensure_ascii=False, default=str) if ev.get("toplist") else None,
            json.dumps(ev.get("holder"), ensure_ascii=False, default=str) if ev.get("holder") else None,
            raw_analysis,
        ),
    )


def recall_history(stock_code: str, days: int = 30, limit: int = 10) -> list[dict]:
    """读取最近 N 天的历史分析 —— 作为"记忆"喂给下次 AI 分析。"""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    return query_all(
        "SELECT analysis_date, ai_source, verdict, predicted_direction, "
        "predicted_target, hit, hit_score, actual_price, raw_analysis "
        "FROM game_analysis_log "
        "WHERE stock_code=? AND analysis_date >= ? "
        "ORDER BY analysis_date DESC LIMIT ?",
        (stock_code, cutoff, limit),
    )


def format_history_for_prompt(stock_code: str, days: int = 30) -> str:
    """把历史记忆格式化成 Markdown，插入到下次 prompt 里"""
    history = recall_history(stock_code, days=days, limit=8)
    if not history:
        return "（无历史博弈记录，本次为首次分析）"

    lines = []
    for h in history:
        date = (h.get("analysis_date") or "")[:10]
        src = h.get("ai_source", "")
        verdict = h.get("verdict", "-")
        direction = h.get("predicted_direction") or "-"
        hit = h.get("hit")
        hit_mark = "✅对" if hit else ("❌错" if hit is False else "⏳待验证")
        lines.append(
            f"- **{date}** {src}: 判断为 **{verdict}**，预测 {direction} → {hit_mark}"
        )
    return "## 历史博弈记录（过去 {} 天）\n\n".format(days) + "\n".join(lines)


def get_ai_track_record() -> list[dict]:
    """返回各 AI 的历史胜率，下次仲裁时按胜率加权"""
    return query_all(
        "SELECT ai_name, verdict_type, total_predictions, hits, win_rate, avg_return "
        "FROM ai_track_record ORDER BY ai_name, verdict_type"
    )


def format_track_record_for_prompt() -> str:
    """历史胜率喂给仲裁 AI，用于加权"""
    records = get_ai_track_record()
    if not records:
        return "（胜率库为空，首次运行）"
    lines = ["## AI 历史胜率（基于已验证判断）\n"]
    for r in records:
        wr = r.get("win_rate")
        if wr is None:
            continue
        lines.append(
            f"- {r['ai_name']} 在 **{r['verdict_type']}** 判断上: {r['hits']}/{r['total_predictions']}, "
            f"胜率 {wr*100:.0f}%"
        )
    return "\n".join(lines)


# ========== 复盘引擎 ==========
def review_pending(days_ago: int = 7) -> dict:
    """复盘 N 天前的预测，对照当前价格打分。"""
    cutoff = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
    pending = query_all(
        "SELECT id, stock_code, price_at_analysis, predicted_direction, predicted_target, "
        "predicted_timeframe, ai_source, verdict FROM game_analysis_log "
        "WHERE verified_at IS NULL AND analysis_date <= ?",
        (cutoff,),
    )

    reviewed = 0
    for p in pending:
        # 拉最新价
        latest = query_one(
            "SELECT close FROM daily_quotes WHERE stock_code=? "
            "ORDER BY trade_date DESC LIMIT 1",
            (p["stock_code"],),
        )
        if not latest or not latest.get("close"):
            continue
        actual_price = latest["close"]
        pa = p.get("price_at_analysis")
        if not pa:
            continue

        actual_change = (actual_price - pa) / pa
        hit = False
        hit_score = 0.0

        direction = p.get("predicted_direction") or ""
        if "涨" in direction:
            hit = actual_change > 0
            hit_score = max(0, min(1, actual_change * 10))  # +10% = 满分
        elif "跌" in direction:
            hit = actual_change < 0
            hit_score = max(0, min(1, -actual_change * 10))
        elif "持平" in direction:
            hit = abs(actual_change) < 0.03
            hit_score = max(0, 1 - abs(actual_change) * 10)

        execute(
            "UPDATE game_analysis_log SET verified_at=CURRENT_TIMESTAMP, "
            "actual_price=?, hit=?, hit_score=? WHERE id=?",
            (actual_price, 1 if hit else 0, hit_score, p["id"]),
        )

        # 更新 AI 胜率统计
        ai = p.get("ai_source", "")
        verdict = p.get("verdict", "")
        if ai and verdict:
            row = query_one(
                "SELECT total_predictions, hits FROM ai_track_record "
                "WHERE ai_name=? AND verdict_type=?",
                (ai, verdict),
            )
            if row:
                new_total = (row["total_predictions"] or 0) + 1
                new_hits = (row["hits"] or 0) + (1 if hit else 0)
                execute(
                    "UPDATE ai_track_record SET total_predictions=?, hits=?, "
                    "win_rate=?, last_updated=CURRENT_TIMESTAMP "
                    "WHERE ai_name=? AND verdict_type=?",
                    (new_total, new_hits, new_hits / new_total, ai, verdict),
                )
            else:
                execute(
                    "INSERT INTO ai_track_record(ai_name, verdict_type, total_predictions, hits, win_rate) "
                    "VALUES (?,?,?,?,?)",
                    (ai, verdict, 1, 1 if hit else 0, 1.0 if hit else 0.0),
                )
        reviewed += 1

    return {"reviewed": reviewed, "pending_before": len(pending)}
