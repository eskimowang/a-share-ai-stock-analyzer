"""Research-report and recommendation-memory scheduler jobs."""
import logging
from typing import Callable, Optional

log = logging.getLogger("scheduler.research_learning")

PushFn = Callable[[str, str, str, str], None]


def job_tushare_report_rules(push: Optional[PushFn] = None):
    """Low-frequency Tushare premium report collection and distillation."""
    log.info("[Tushare report rules] start")
    try:
        from ..services.tushare_report_service import collect_and_process_tushare_reports

        result = collect_and_process_tushare_reports(
            max_stocks=2,
            months=24,
            fetch_live=True,
            process_cached=True,
            min_interval_seconds=370,
        )
        log.info(
            "[Tushare report rules] done status=%s targets=%s live=%s signals=%s",
            result.get("status"),
            result.get("target_count"),
            result.get("live_calls"),
            result.get("signals_saved"),
        )
        return result
    except Exception as exc:
        log.exception("Tushare report rules failed: %s", exc)
        if push:
            push("🔴", "Tushare研报规则异常", f"任务异常: {exc}", "研报规则异常")
        return {"status": "failed", "error": str(exc)}


def job_research_report_backtest(push: Optional[PushFn] = None):
    """Backtest cached report opinions and refresh broker/author/team scores."""
    log.info("[Research report backtest] start")
    try:
        from ..services.tushare_report_service import refresh_research_report_quality_scores

        result = refresh_research_report_quality_scores(limit=5000)
        batch = result.get("batch") or {}
        backtest = result.get("backtest") or {}
        log.info(
            "[Research report backtest] done batch=%s checked=%s updated=%s authors=%s",
            batch.get("signals_processed"),
            backtest.get("signals_checked"),
            backtest.get("signals_updated"),
            backtest.get("author_stats_updated"),
        )
        return result
    except Exception as exc:
        log.exception("Research report backtest failed: %s", exc)
        if push:
            push("🔴", "研报作者反测异常", f"任务异常: {exc}", "研报反测异常")
        return {"status": "failed", "error": str(exc)}


def job_recommendation_memory_review(push: Optional[PushFn] = None):
    """Review recommendation-source memory, including hot-stock picks."""
    log.info("[Recommendation memory review] start")
    try:
        from ..services.recommendation_memory_service import review_recommendation_memory

        result = review_recommendation_memory(limit=2000)
        log.info(
            "[Recommendation memory review] done reviews=%s sources=%s",
            result.get("reviews_updated"),
            result.get("sources_updated"),
        )
        return result
    except Exception as exc:
        log.exception("Recommendation memory review failed: %s", exc)
        if push:
            push("🔴", "推荐来源记忆反测异常", f"任务异常: {exc}", "推荐记忆异常")
        return {"status": "failed", "error": str(exc)}
