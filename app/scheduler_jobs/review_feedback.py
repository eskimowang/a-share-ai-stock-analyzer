"""Review, feedback, and playbook scheduler jobs."""
import logging
from typing import Callable, Optional

from ..db import query_all
from ..services.game_memory import review_pending

log = logging.getLogger("scheduler.review_feedback")

PushFn = Callable[[str, str, str, str], None]
_push_callback: Optional[PushFn] = None


def configure_push(push: Optional[PushFn]) -> None:
    global _push_callback
    _push_callback = push


def _push(level: str, title: str, markdown: str, short: str = ""):
    if _push_callback:
        return _push_callback(level, title, markdown, short)
    log.info("push skipped because callback is not configured: %s %s", level, title)
    return None


def job_review_engine():
    """每日收盘后跑 —— 把 7 天前的博弈判断对照当前价格打分，更新胜率统计。"""
    log.info("[复盘引擎] 开始")
    try:
        r7 = review_pending(days_ago=7)
        r30 = review_pending(days_ago=30)
        summary = f"7日复盘: {r7['reviewed']}/{r7['pending_before']}，30日复盘: {r30['reviewed']}/{r30['pending_before']}"
        log.info(f"[复盘引擎] {summary}")
        # 只有显著变化才推送
        if r7["reviewed"] > 0 or r30["reviewed"] > 0:
            # 取最新胜率 Top
            rows = query_all(
                "SELECT ai_name, verdict_type, total_predictions, hits, win_rate "
                "FROM ai_track_record WHERE total_predictions >= 3 "
                "ORDER BY win_rate DESC LIMIT 10"
            )
            if rows:
                md = "## 博弈记忆复盘（每日）\n\n"
                md += f"- 本次复盘 7 天前 {r7['reviewed']} 条，30 天前 {r30['reviewed']} 条\n\n"
                md += "## AI × 割韭菜行为类型胜率\n\n"
                for r in rows:
                    md += f"- **{r['ai_name']}** 在 **{r['verdict_type']}** 判断上 {r['hits']}/{r['total_predictions']} = {(r['win_rate'] or 0)*100:.0f}%\n"
                _push("🔵", "博弈复盘", md, short="胜率更新")
    except Exception as e:
        log.exception(f"复盘引擎失败: {e}")


# ========== 任务 5：半月度荐股矩阵 ==========


def job_paper_close():
    """每日 16:10 把 7 天前登记的纸交易平仓。"""
    log.info("[纸交易平仓] 开始")
    try:
        from ..services.order_service import close_paper_trades_by_time
        close_paper_trades_by_time(days=7)
    except Exception as e:
        log.exception(f"纸交易平仓失败: {e}")


def job_blacklist_refresh():
    """每工作日 16:30 根据当日探测结果刷新反向过滤黑名单。"""
    log.info('[反向黑名单] 刷新')
    try:
        from ..services.reverse_filter import update_blacklist_from_detections
        r = update_blacklist_from_detections()
        log.info(f'[反向黑名单] 新增 {r.get("added")} 条')
    except Exception as e:
        log.exception(f'反向黑名单刷新失败: {e}')


def job_interaction_tracking():
    """每工作日 16:25 跟踪用户聊过的股票，命中高风险手法时推送。"""
    log.info("[互动股票跟踪] 开始")
    try:
        from ..services.interaction_tracking_service import run_interaction_tracking
        result = run_interaction_tracking(
            since_days=30,
            refresh_market_data=True,
            refresh_days=3,
        )
        alerts = result.get("summary", {}).get("alerts") or []
        if not alerts:
            log.info(f"[互动股票跟踪] 完成，无预警，扫描 {result.get('stocks_scanned')} 只")
            return

        md = (
            "## 互动股票跟踪\n\n"
            f"- 扫描: **{result.get('stocks_scanned')}** 只\n"
            f"- 预警: **{len(alerts)}** 条\n"
            f"- 用时: {result.get('duration_seconds', 0):.1f} 秒\n\n"
            "### 需要盯紧\n"
        )
        for a in alerts[:10]:
            nm = a.get("name") or ""
            pct = a.get("change_pct")
            pct_s = f"{pct:+.2f}%" if pct is not None else "-"
            md += (
                f"- {a.get('trade_date')} **{a.get('code')} {nm}** "
                f"收 {a.get('close')} / {pct_s} · "
                f"{a.get('top_pattern')} {int((a.get('top_confidence') or 0)*100)}%: "
                f"{a.get('top_narrative') or ''}\n"
            )
        _push("🟡", "互动股票跟踪预警", md, short=f"{len(alerts)} 条互动股票预警")
        log.info(f"[互动股票跟踪] 完成 run_id={result.get('run_id')}")
    except Exception as e:
        log.exception(f"互动股票跟踪异常: {e}")
        _push("🔴", "互动股票跟踪失败", f"任务异常: {e}", short="互动跟踪失败")


def job_weekly_market_playbook_scan():
    """每周日 09:00 全市场 14 手法系统复盘，补最近一周数据并推送摘要。"""
    log.info("[Playbook 全市场周复盘] 开始")
    try:
        from ..services.playbook_service import scan_market_weekly
        result = scan_market_weekly(since_days=10, refresh_market_data=True)
        patterns = result.get("summary", {}).get("patterns") or []
        top_cases = result.get("summary", {}).get("top_cases") or []
        md = (
            f"## 14 类手法全市场周复盘\n\n"
            f"- 区间: {result.get('start_date')} 至 {result.get('end_date')}\n"
            f"- 扫描: **{result.get('stocks_scanned')}** 只\n"
            f"- 命中: **{result.get('total_detections')}** 条\n"
            f"- 高置信: **{result.get('high_confidence_count')}** 条\n"
            f"- 用时: {result.get('duration_seconds', 0)/60:.1f} 分钟\n\n"
        )
        if patterns:
            md += "### 本周高频手法\n"
            for name, n in patterns[:8]:
                md += f"- {name}: {n} 条\n"
        if top_cases:
            md += "\n### 高置信案例 Top 10\n"
            for c in top_cases[:10]:
                nm = c.get("name") or ""
                md += (
                    f"- {c.get('date')} **{c.get('code')} {nm}** "
                    f"{c.get('pattern')} {int((c.get('confidence') or 0)*100)}%: "
                    f"{c.get('narrative')}\n"
                )
        _push("🟡", "14 类手法全市场周复盘", md,
              short=f"扫 {result.get('stocks_scanned')} 只，命中 {result.get('total_detections')} 条")
        log.info(f"[Playbook 全市场周复盘] 完成 run_id={result.get('run_id')}")
    except Exception as e:
        log.exception(f"Playbook 全市场周复盘异常: {e}")
        _push("🔴", "14 类手法全市场周复盘失败", f"任务异常: {e}", short="周复盘失败")


def job_weekly_playbook_scan():
    """每周六 09:30 扫 14 手法命中 + 算收益。"""
    log.info("[Playbook 周扫] 开始")
    try:
        from ..services.playbook_service import scan_all_tracked_stocks
        result = scan_all_tracked_stocks(since_days=180)
        log.info(f"[Playbook 周扫] 扫 {result.get('stocks_scanned')} 只股，命中 {result.get('total_detections')} 条")
        # 不推送，数据内化
    except Exception as e:
        log.exception(f"Playbook 周扫异常: {e}")


def job_daily_playbook_outcome():
    """每工作日 16:15 算 7d/30d 收益匹配。"""
    log.info("[Playbook 收益计算] 开始")
    try:
        from ..services.playbook_service import compute_outcomes
        r = compute_outcomes(limit=500)
        log.info(f"[Playbook 收益计算] 更新 {r.get('updated')} 条")
    except Exception as e:
        log.exception(f"Playbook 收益计算异常: {e}")


def job_weekly_backtest():
    """每周六 10:00 跑一次完整回测，结果只站内。"""
    log.info("[周度回测] 开始")
    try:
        from ..services.backtest_service import run_backtest
        result = run_backtest(hold_days=7, since_days=180)
        md = (
            f"## 周度回测 (hold=7d, 180 天窗口)\n\n"
            f"- 总分析: {result.get('total_analyses')} 条，已验证 {result.get('verified')} 条\n"
            f"- 胜率: {(result.get('win_rate') or 0) * 100:.1f}%\n"
            f"- 平均收益: {(result.get('avg_return') or 0) * 100:+.2f}%\n\n"
            "### 各家 AI 胜率\n"
        )
        for ai, d in (result.get("by_ai") or {}).items():
            md += f"- {ai}: {d.get('wins')}/{d.get('n')} = {d.get('win_rate', 0) * 100:.0f}%, 均收益 {d.get('avg_return', 0) * 100:+.2f}%\n"
        _push("🔵", "周度回测报告", md, short=f"胜率 {(result.get('win_rate') or 0) * 100:.1f}%")
    except Exception as e:
        log.exception(f"周度回测失败: {e}")


# ========== 任务 7：月度长期力量跟踪 ==========
