"""Scheduler jobs for data_learning."""
import logging
from typing import Callable, Optional

log = logging.getLogger("scheduler.data_learning")

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


def job_data_enrichment():
    """每工作日 16:40：多源数据增强，先补全市场环境，再刷新重点池深度画像。"""
    log.info("[多源数据增强] 开始")
    try:
        from ..services.data_enrichment_service import run_data_enrichment
        result = run_data_enrichment(max_stocks=30, refresh_market=True, refresh_days=7)
        log.info(
            "[多源数据增强] 完成 status=%s refreshed=%s/%s duration=%.1fs",
            result.get("status"),
            result.get("refreshed_count"),
            result.get("target_count"),
            result.get("duration_seconds", 0),
        )
        if result.get("status") == "failed":
            _push("🔴", "多源数据增强失败", f"任务失败: {result.get('error')}", short="多源数据失败")
    except Exception as e:
        log.exception("多源数据增强异常: %s", e)
        _push("🔴", "多源数据增强异常", f"任务异常: {e}", short="多源数据异常")


def job_premium_reports_weekly():
    """每周日 20:00 采集中信+中金对"持仓+自选"的研报。"""
    log.info("[中信中金研报·周] 开始")
    try:
        from ..services.premium_broker_reports import run_premium_report_collection
        result = run_premium_report_collection(scope="positions_watchlist")
        if result.get("status") == "success":
            md = (
                f"## 中信+中金研报 · 周采集\n\n"
                f"- 范围: 持仓+自选 ({result.get('stocks')} 只)\n"
                f"- 落库: **{result.get('reports_saved')}** 份\n"
                f"- 用时: {result.get('duration_seconds', 0):.0f}s\n\n"
                f"前 20 格覆盖度:\n"
            )
            for d in (result.get("detail") or [])[:20]:
                cov = d.get("coverage") or "—"
                md += f"- {d.get('code')} · {d.get('broker')}: {d.get('saved')} 份，覆盖度 {cov}\n"
            _push("🔵", "中信+中金研报周采集", md, short=f"落库 {result.get('reports_saved')} 份")
    except Exception as e:
        log.exception(f"中信中金周采集失败: {e}")


def job_premium_reports_monthly_ivd():
    """每月 20 号：为 IVD 50 只补中信+中金研报。"""
    log.info("[中信中金研报·月 IVD] 开始")
    try:
        from ..services.premium_broker_reports import run_premium_report_collection
        result = run_premium_report_collection(scope="ivd")
        if result.get("status") == "success":
            log.info(f"[中信中金研报·月 IVD] 落库 {result.get('reports_saved')} 份")
    except Exception as e:
        log.exception(f"IVD 月采集失败: {e}")


def job_premium_reports_hot_stocks():
    """每月 25 号 20:00: Top 20 × 10 行业 × 中信中金 研报全扫。"""
    log.info("[中信中金·热股月采] 开始")
    try:
        from ..services.premium_broker_reports import run_hot_stocks_premium_reports
        result = run_hot_stocks_premium_reports(top_per_industry=20)
        if result.get("status") == "success":
            md = (
                f"## 中信+中金研报 · 热股月采\n\n"
                f"- 行业: {result.get('industries')}\n"
                f"- 去重后个股: {result.get('unique_stocks')}\n"
                f"- 采集格: {result.get('total_grids')}\n"
                f"- **落库: {result.get('reports_saved')} 份**\n"
                f"- 用时: {result.get('duration_seconds', 0)/60:.1f} 分钟\n"
            )
            _push("🟡", "热股月采完成", md,
                   short=f"落库 {result.get('reports_saved')} 份研报")
    except Exception as e:
        log.exception(f"热股月采异常: {e}")


def job_broker_study():
    """月度：Codex 联网研究中信/中金在 10 大行业的研报风格。"""
    log.info("[券商风格学习] 开始")
    try:
        from ..services.broker_study_service import run_broker_study
        result = run_broker_study()
        if result.get("status") == "success":
            md = result.get("summary_md") or "（空）"
            _push("🟡", "月度券商风格学习",
                   "## 中信 / 中金 研究风格画像\n\n" + md,
                   short=f"券商风格学习完成 ({result.get('profiles_count')} 格)")
        else:
            log.warning(f"券商风格学习失败: {result.get('error')}")
    except Exception as e:
        log.exception(f"券商风格学习异常: {e}")
