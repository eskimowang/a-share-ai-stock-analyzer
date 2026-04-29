"""AI PK scheduler jobs.

This module owns only the scheduling wrapper behavior for the AI PK contest.
The trading and referee logic remains in services.ai_pk_service.
"""
import logging
from typing import Callable, Optional

log = logging.getLogger("scheduler.ai_pk")

PushFn = Callable[[str, str, str, str], None]


def job_ai_pk_intraday(push: Optional[PushFn] = None):
    """Trading-day intraday AI PK refresh."""
    log.info("[AI PK intraday] start")
    try:
        from ..services.ai_pk_service import run_ai_pk_intraday

        result = run_ai_pk_intraday(force=False, source="scheduler")
        dash = result.get("dashboard") or {}
        leaders = dash.get("contestants") or []
        leader = leaders[0] if leaders else {}
        log.info(
            "[AI PK intraday] done status=%s phase=%s trades=%s leader=%s return=%.2f%%",
            result.get("status"),
            result.get("market_phase"),
            result.get("trades_count"),
            leader.get("contestant"),
            float(leader.get("total_return") or 0) * 100,
        )
        return result
    except Exception as exc:
        log.exception("AI PK intraday failed: %s", exc)
        if push:
            push("🔴", "AI PK盘中异常", f"任务异常: {exc}", "AI PK盘中异常")
        return {"status": "failed", "error": str(exc)}


def job_ai_pk_daily(push: Optional[PushFn] = None):
    """Post-close AI PK refresh."""
    log.info("[AI PK daily] start")
    try:
        from ..services.ai_pk_service import run_ai_pk_daily

        result = run_ai_pk_daily(force=False)
        dash = result.get("dashboard") or {}
        leaders = dash.get("contestants") or []
        leader = leaders[0] if leaders else {}
        log.info(
            "[AI PK daily] done status=%s trades=%s leader=%s return=%.2f%%",
            result.get("status"),
            result.get("trades_count"),
            leader.get("contestant"),
            float(leader.get("total_return") or 0) * 100,
        )
        return result
    except Exception as exc:
        log.exception("AI PK daily failed: %s", exc)
        if push:
            push("🔴", "AI PK异常", f"任务异常: {exc}", "AI PK异常")
        return {"status": "failed", "error": str(exc)}
