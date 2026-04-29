"""定时任务调度器 —— 集成到 FastAPI lifespan。

任务表:
  每日工作日:
    08:50 盘前策略推送       → 🟡 微信
    11:30 午盘小结           → 🔵 站内信
    14:45 尾盘决策           → 🟡 微信

  半月度:
    每月 1/15 日 08:30 荐股矩阵  → 🟡 微信

  实时:
    盘中每 10 分钟异动扫描     → 🔴 微信（仅触发时）
"""
import logging
from datetime import datetime
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import CONFIG
from .db import db, query_all, query_one, execute
from .notifier import WeChatNotifier
from .scheduler_jobs.registry import summarize_job_groups
from .scheduler_jobs.ai_pk import (
    job_ai_pk_intraday as _job_ai_pk_intraday,
    job_ai_pk_daily as _job_ai_pk_daily,
)
from .scheduler_jobs.research_learning import (
    job_tushare_report_rules as _job_tushare_report_rules,
    job_research_report_backtest as _job_research_report_backtest,
    job_recommendation_memory_review as _job_recommendation_memory_review,
)
from .scheduler_jobs.market_intraday import (
    configure_push as _configure_market_intraday_push,
    job_premarket as _job_premarket,
    job_midday as _job_midday,
    job_closing as _job_closing,
    job_alert_scan as _job_alert_scan,
    job_stop_loss_scan as _job_stop_loss_scan,
)
from .scheduler_jobs.review_feedback import (
    configure_push as _configure_review_feedback_push,
    job_review_engine as _job_review_engine,
    job_paper_close as _job_paper_close,
    job_blacklist_refresh as _job_blacklist_refresh,
    job_interaction_tracking as _job_interaction_tracking,
    job_weekly_market_playbook_scan as _job_weekly_market_playbook_scan,
    job_weekly_playbook_scan as _job_weekly_playbook_scan,
    job_daily_playbook_outcome as _job_daily_playbook_outcome,
    job_weekly_backtest as _job_weekly_backtest,
)
from .scheduler_jobs.data_learning import (
    configure_push as _configure_data_learning_push,
    job_data_enrichment as _job_data_enrichment,
    job_premium_reports_weekly as _job_premium_reports_weekly,
    job_premium_reports_monthly_ivd as _job_premium_reports_monthly_ivd,
    job_premium_reports_hot_stocks as _job_premium_reports_hot_stocks,
    job_broker_study as _job_broker_study,
)
from .scheduler_jobs.long_horizon import (
    configure_push as _configure_long_horizon_push,
    job_discovery_matrix as _job_discovery_matrix,
    job_long_term_tracking as _job_long_term_tracking,
)
from .services.game_memory import (
    save_analysis, recall_history, format_history_for_prompt,
    format_track_record_for_prompt, review_pending,
)

log = logging.getLogger("scheduler")

# 全局单例
_scheduler: Optional[AsyncIOScheduler] = None


def _get_wechat() -> Optional[WeChatNotifier]:
    key = CONFIG.get("notification", {}).get("serverchan", {}).get("send_key")
    if not key:
        return None
    return WeChatNotifier(key)


def _save_notification(level: str, title: str, content: str, status: str):
    try:
        execute(
            "INSERT INTO notifications(channel, title, content, status) VALUES (?,?,?,?)",
            ("wechat" if level in ("🔴", "🟡") else "inapp", title, content[:4000], status),
        )
    except Exception:
        pass


def _push(level: str, title: str, markdown: str, short: str = ""):
    """根据级别决定推送通道（微信 + Web Push 并发）。"""
    final_title = f"{level} {title}"
    # Web Push（🔴🟡 推，🔵 不推）
    if level in ("🔴", "🟡"):
        try:
            from .services.push_service import push_to_all
            push_to_all(final_title, short or markdown[:120], url="/chat")
        except Exception as e:
            log.warning(f"Web push 失败: {e}")
        wx = _get_wechat()
        if wx:
            result = wx.send(final_title, markdown, short=short or None)
            _save_notification(
                level,
                final_title,
                markdown,
                "sent" if result.get("code") == 0 else "failed",
            )
            return
    _save_notification(level, final_title, markdown, "inapp")


# ========== 日内市场任务（实现已拆至 scheduler_jobs.market_intraday）==========
def _run_market_job(fn):
    _configure_market_intraday_push(_push)
    return fn()


def _run_review_job(fn):
    _configure_review_feedback_push(_push)
    return fn()


def _run_data_learning_job(fn):
    _configure_data_learning_push(_push)
    return fn()


def _run_long_horizon_job(fn):
    _configure_long_horizon_push(_push)
    return fn()


def job_premarket():
    """Premarket briefing job; implementation lives in scheduler_jobs.market_intraday."""
    return _run_market_job(_job_premarket)


def job_midday():
    """Midday summary job; implementation lives in scheduler_jobs.market_intraday."""
    return _run_market_job(_job_midday)


def job_closing():
    """Closing decision job; implementation lives in scheduler_jobs.market_intraday."""
    return _run_market_job(_job_closing)


def job_alert_scan():
    """Holding alert scan job; implementation lives in scheduler_jobs.market_intraday."""
    return _run_market_job(_job_alert_scan)


# ========== 任务 X：复盘引擎（每日 16:00 自动跑）==========
def job_review_engine():
    """Review engine job; implementation lives in scheduler_jobs.review_feedback."""
    return _run_review_job(_job_review_engine)
def job_discovery_matrix():
    """Discovery matrix job; implementation lives in scheduler_jobs.long_horizon."""
    return _run_long_horizon_job(_job_discovery_matrix)
def job_paper_close():
    """Paper-trade close job; implementation lives in scheduler_jobs.review_feedback."""
    return _run_review_job(_job_paper_close)
def job_premium_reports_weekly():
    """Premium weekly report job; implementation lives in scheduler_jobs.data_learning."""
    return _run_data_learning_job(_job_premium_reports_weekly)
def job_premium_reports_monthly_ivd():
    """Premium monthly IVD report job; implementation lives in scheduler_jobs.data_learning."""
    return _run_data_learning_job(_job_premium_reports_monthly_ivd)
def job_premium_reports_hot_stocks():
    """Premium hot-stock report job; implementation lives in scheduler_jobs.data_learning."""
    return _run_data_learning_job(_job_premium_reports_hot_stocks)
def job_broker_study():
    """Broker study job; implementation lives in scheduler_jobs.data_learning."""
    return _run_data_learning_job(_job_broker_study)
def job_tushare_report_rules():
    """Tushare report rule job; implementation lives in scheduler_jobs.research_learning."""
    return _job_tushare_report_rules(push=_push)


def job_research_report_backtest():
    """Research report quality backtest job; implementation lives in scheduler_jobs.research_learning."""
    return _job_research_report_backtest(push=_push)


def job_recommendation_memory_review():
    """Recommendation memory review job; implementation lives in scheduler_jobs.research_learning."""
    return _job_recommendation_memory_review(push=_push)


def job_ai_pk_intraday():
    """Trading-day intraday AI PK job; implementation lives in scheduler_jobs.ai_pk."""
    return _job_ai_pk_intraday(push=_push)


def job_ai_pk_daily():
    """Post-close AI PK job; implementation lives in scheduler_jobs.ai_pk."""
    return _job_ai_pk_daily(push=_push)


def job_data_enrichment():
    """Data enrichment job; implementation lives in scheduler_jobs.data_learning."""
    return _run_data_learning_job(_job_data_enrichment)
def job_blacklist_refresh():
    """Reverse blacklist refresh job; implementation lives in scheduler_jobs.review_feedback."""
    return _run_review_job(_job_blacklist_refresh)
def job_stop_loss_scan():
    """Stop-loss scan job; implementation lives in scheduler_jobs.market_intraday."""
    return _run_market_job(_job_stop_loss_scan)


def job_interaction_tracking():
    """Interaction tracking job; implementation lives in scheduler_jobs.review_feedback."""
    return _run_review_job(_job_interaction_tracking)
def job_weekly_market_playbook_scan():
    """Full-market weekly playbook scan job; implementation lives in scheduler_jobs.review_feedback."""
    return _run_review_job(_job_weekly_market_playbook_scan)
def job_weekly_playbook_scan():
    """Weekly playbook scan job; implementation lives in scheduler_jobs.review_feedback."""
    return _run_review_job(_job_weekly_playbook_scan)
def job_daily_playbook_outcome():
    """Daily playbook outcome job; implementation lives in scheduler_jobs.review_feedback."""
    return _run_review_job(_job_daily_playbook_outcome)
def job_weekly_backtest():
    """Weekly backtest job; implementation lives in scheduler_jobs.review_feedback."""
    return _run_review_job(_job_weekly_backtest)
def job_long_term_tracking():
    """Long-term tracking job; implementation lives in scheduler_jobs.long_horizon."""
    return _run_long_horizon_job(_job_long_term_tracking)
def start_scheduler():
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    _scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")

    # 工作日 1-5（周一到周五）
    workday = CronTrigger(day_of_week="mon-fri", timezone="Asia/Shanghai")

    _scheduler.add_job(job_premarket, CronTrigger(hour=9, minute=0, day_of_week="mon-fri"),
                       id="premarket", replace_existing=True)
    _scheduler.add_job(job_midday, CronTrigger(hour=11, minute=30, day_of_week="mon-fri"),
                       id="midday", replace_existing=True)
    _scheduler.add_job(job_closing, CronTrigger(hour=14, minute=45, day_of_week="mon-fri"),
                       id="closing", replace_existing=True)

    # 异动扫描：盘中每 10 分钟（9:30-11:30, 13:00-15:00 工作日）
    _scheduler.add_job(job_alert_scan,
                       CronTrigger(minute="*/10", hour="9,10,11,13,14", day_of_week="mon-fri"),
                       id="alert_scan", replace_existing=True)

    # 复盘引擎：每工作日 16:00（收盘后 1 小时）
    _scheduler.add_job(job_review_engine,
                       CronTrigger(hour=16, minute=0, day_of_week="mon-fri"),
                       id="review_engine", replace_existing=True)

    # 荐股矩阵：每月 1 号、15 号 08:30
    _scheduler.add_job(job_discovery_matrix,
                       CronTrigger(day="1,15", hour=8, minute=30),
                       id="discovery", replace_existing=True)

    # 长期力量跟踪：每月 5 号 09:00（避开荐股日）
    _scheduler.add_job(job_long_term_tracking,
                       CronTrigger(day="5", hour=9, minute=0),
                       id="long_term_tracking", replace_existing=True)

    # 纸交易平仓：每工作日 16:10
    _scheduler.add_job(job_paper_close,
                       CronTrigger(hour=16, minute=10, day_of_week="mon-fri"),
                       id="paper_close", replace_existing=True)

    # 周度回测：每周六 10:00
    _scheduler.add_job(job_weekly_backtest,
                       CronTrigger(day_of_week="sat", hour=10, minute=0),
                       id="weekly_backtest", replace_existing=True)

    # Playbook 周扫：每周六 09:30（回测前先跑）
    _scheduler.add_job(job_weekly_playbook_scan,
                       CronTrigger(day_of_week="sat", hour=9, minute=30),
                       id="playbook_scan", replace_existing=True)

    # Playbook 全市场周复盘：每周日 09:00（补最近一周全市场数据 + 14 手法扫描）
    _scheduler.add_job(job_weekly_market_playbook_scan,
                       CronTrigger(day_of_week="sun", hour=9, minute=0),
                       id="playbook_market_scan", replace_existing=True)

    # Playbook 收益计算：每工作日 16:15
    _scheduler.add_job(job_daily_playbook_outcome,
                       CronTrigger(hour=16, minute=15, day_of_week="mon-fri"),
                       id="playbook_outcome", replace_existing=True)

    # 互动股票跟踪：每工作日 16:25（收盘后，黑名单刷新前）
    _scheduler.add_job(job_interaction_tracking,
                       CronTrigger(hour=16, minute=25, day_of_week="mon-fri"),
                       id="interaction_tracking", replace_existing=True)

    # 反向黑名单刷新：每工作日 16:30
    _scheduler.add_job(job_blacklist_refresh,
                       CronTrigger(hour=16, minute=30, day_of_week="mon-fri"),
                       id="blacklist_refresh", replace_existing=True)

    # 多源数据增强：每工作日 16:40（互动跟踪和黑名单之后，给 AI 建深度资料包）
    _scheduler.add_job(job_data_enrichment,
                       CronTrigger(hour=16, minute=40, day_of_week="mon-fri"),
                       id="data_enrichment", replace_existing=True)

    # Tushare 付费研报规则：每工作日 16:50，低频慢速加工重点池
    _scheduler.add_job(job_tushare_report_rules,
                       CronTrigger(hour=16, minute=50, day_of_week="mon-fri"),
                       id="tushare_report_rules", replace_existing=True)

    # 研报作者/团队反测：每工作日 17:00
    _scheduler.add_job(job_research_report_backtest,
                       CronTrigger(hour=17, minute=0, day_of_week="mon-fri"),
                       id="research_report_backtest", replace_existing=True)

    # 推荐来源记忆：每工作日 17:05（研报反测后，AI PK 前）
    _scheduler.add_job(job_recommendation_memory_review,
                       CronTrigger(hour=17, minute=5, day_of_week="mon-fri"),
                       id="recommendation_memory_review", replace_existing=True)

    # AI模拟账户PK盘中实时交易：交易日每30分钟尝试一次，函数内会避开非连续竞价时段
    _scheduler.add_job(job_ai_pk_intraday,
                       CronTrigger(hour="9,10,11,13,14", minute="5,35", day_of_week="mon-fri"),
                       id="ai_pk_intraday", replace_existing=True)
    _scheduler.add_job(job_ai_pk_intraday,
                       CronTrigger(hour=14, minute=55, day_of_week="mon-fri"),
                       id="ai_pk_intraday_closing", replace_existing=True)

    # AI模拟账户PK：每工作日 17:10（研报反测后）
    _scheduler.add_job(job_ai_pk_daily,
                       CronTrigger(hour=17, minute=10, day_of_week="mon-fri"),
                       id="ai_pk_daily", replace_existing=True)

    # 止损扫描：每工作日 10:30 / 14:30（盘中两次）
    _scheduler.add_job(job_stop_loss_scan,
                       CronTrigger(hour="10,14", minute=30, day_of_week="mon-fri"),
                       id="stop_loss_scan", replace_existing=True)

    # 券商风格学习：每月 10 号 10:00（跟长期跟踪错开）
    _scheduler.add_job(job_broker_study,
                       CronTrigger(day="10", hour=10, minute=0),
                       id="broker_study", replace_existing=True)

    # 中信+中金研报周采集：每周日 20:00
    _scheduler.add_job(job_premium_reports_weekly,
                       CronTrigger(day_of_week="sun", hour=20, minute=0),
                       id="premium_reports_weekly", replace_existing=True)

    # 中信+中金 IVD 月采集：每月 20 号 21:00
    _scheduler.add_job(job_premium_reports_monthly_ivd,
                       CronTrigger(day="20", hour=21, minute=0),
                       id="premium_reports_monthly", replace_existing=True)

    # 中信+中金 热股月采：每月 25 号 20:00（top 20 × 10 行业）
    _scheduler.add_job(job_premium_reports_hot_stocks,
                       CronTrigger(day="25", hour=20, minute=0),
                       id="premium_reports_hot_stocks", replace_existing=True)

    _scheduler.start()
    log.info("调度器启动完成")
    log.info(f"调度任务分组: {summarize_job_groups()}")
    log.info(f"已注册任务: {[j.id for j in _scheduler.get_jobs()]}")
    return _scheduler


def stop_scheduler():
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        log.info("调度器已停止")
        _scheduler = None
