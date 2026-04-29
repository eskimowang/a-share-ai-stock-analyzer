"""Scheduler jobs for long_horizon."""
import logging
from typing import Callable, Optional

log = logging.getLogger("scheduler.long_horizon")

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


def job_discovery_matrix():
    """每月 1/15 日 08:30 跑完整荐股流程（5-15 分钟），结果推微信。"""
    log.info("[荐股矩阵] 开始")
    try:
        from ..services.discovery_service import run_discovery_full
        result = run_discovery_full()
        if result.get("status") == "success":
            md = result.get("report_md") or "（报告生成失败）"
            dur = result.get("duration_seconds", 0)
            short = f"半月度荐股完成（{dur:.0f}s）"
            _push("🟡", "半月度荐股矩阵", md, short=short)
            log.info(f"[荐股矩阵] 完成并推送, run_id={result.get('run_id')}")
        else:
            err = result.get("error", "未知错误")
            _push("🔴", "荐股失败", f"半月度荐股任务失败: {err}", short="荐股失败")
            log.error(f"[荐股矩阵] 失败: {err}")
    except Exception as e:
        log.exception(f"荐股矩阵任务异常: {e}")
        _push("🔴", "荐股异常", f"任务抛异常: {e}", short="荐股异常")


# ========== 任务 6：每日纸交易平仓 + 周度回测 ==========


def job_long_term_tracking():
    """每月 5 日 09:00 跑一次（避开荐股日 1/15），结果推微信。"""
    log.info("[长期跟踪] 开始")
    try:
        from ..services.long_term_tracking_service import run_long_term_tracking
        result = run_long_term_tracking()
        if result.get("status") == "success":
            md = result.get("arbitration") or "（仲裁结果为空）"
            dur = result.get("duration_seconds", 0)
            count = result.get("positions_count", 0)
            short = f"长期跟踪完成（{count} 只/{dur:.0f}s）"
            _push("🟡", "月度长期力量跟踪", md, short=short)
            log.info(f"[长期跟踪] 完成推送, run_id={result.get('run_id')}")
        else:
            err = result.get("error", "未知错误")
            if err == "无持仓":
                log.info("[长期跟踪] 无持仓，跳过")
            else:
                _push("🔴", "长期跟踪失败", f"任务失败: {err}", short="长期跟踪失败")
                log.error(f"[长期跟踪] 失败: {err}")
    except Exception as e:
        log.exception(f"长期跟踪任务异常: {e}")
        _push("🔴", "长期跟踪异常", f"任务抛异常: {e}", short="长期跟踪异常")


# ========== 启停 ==========
