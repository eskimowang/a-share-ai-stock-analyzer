"""Scheduler job group registry.

The scheduler is being split in small, verifiable steps.  This registry keeps
the dashboard/log grouping stable while individual job implementations move
from app/scheduler.py into app/scheduler_jobs/*.py.
"""

JOB_GROUPS = {
    "market_intraday": [
        "premarket", "midday", "closing", "alert_scan", "stop_loss_scan",
    ],
    "data_learning": [
        "data_enrichment", "tushare_report_rules", "research_report_backtest",
        "broker_study", "premium_reports_weekly", "premium_reports_monthly",
        "premium_reports_hot_stocks",
    ],
    "review_feedback": [
        "review_engine", "paper_close", "weekly_backtest", "playbook_scan",
        "playbook_market_scan", "playbook_outcome", "interaction_tracking",
        "recommendation_memory_review", "blacklist_refresh",
    ],
    "ai_pk": [
        "ai_pk_intraday", "ai_pk_intraday_closing", "ai_pk_daily",
    ],
    "long_horizon": [
        "discovery", "long_term_tracking",
    ],
}

SPLIT_STATUS = {
    "ai_pk": "split",
    "data_learning": "split",
    "long_horizon": "split",
    "market_intraday": "split",
    "review_feedback": "split",
}


def group_for_job(job_id: str) -> str:
    for group, ids in JOB_GROUPS.items():
        if job_id in ids:
            return group
    return "ungrouped"


def summarize_job_groups() -> dict:
    return {group: len(ids) for group, ids in JOB_GROUPS.items()}
