"""统一数据源 - 自动多源 fallback（Tushare → AKShare → Baostock）。"""
import logging
from .akshare_client import AKShareClient
from .baostock_client import BaostockClient

log = logging.getLogger(__name__)


def _fmt_start(start: str, dashed: bool) -> str:
    """20200101 ↔ 2020-01-01 互转。"""
    if not start:
        return start
    if dashed and "-" not in start:
        return f"{start[:4]}-{start[4:6]}-{start[6:8]}"
    if not dashed and "-" in start:
        return start.replace("-", "")
    return start


class UnifiedDataSource:
    def __init__(self, tushare_token: str = None):
        self.tushare = None
        if tushare_token:
            try:
                from .tushare_client import TushareClient
                self.tushare = TushareClient(tushare_token)
                log.info("Tushare Pro 已启用")
            except Exception as e:
                log.warning(f"Tushare 初始化失败: {e}")
        self.akshare = AKShareClient()
        self.baostock = BaostockClient()

    def get_daily(self, code: str, start: str = "20200101", end: str = None):
        """按优先级试多个源。返回 (DataFrame, source_name) 或 (None, None)。"""
        # 1. Tushare（付费主源）
        if self.tushare:
            try:
                df = self.tushare.get_daily(code, _fmt_start(start, False),
                                             _fmt_start(end, False))
                if df is not None and not df.empty:
                    return df, "tushare"
            except Exception as e:
                log.warning(f"Tushare fail {code}: {e}")

        # 2. AKShare
        try:
            df = self.akshare.get_daily(code, _fmt_start(start, False),
                                         _fmt_start(end, False))
            if df is not None and not df.empty:
                return df, "akshare"
        except Exception as e:
            log.warning(f"AKShare fail {code}: {e}")

        # 3. Baostock
        try:
            df = self.baostock.get_daily(code, _fmt_start(start, True),
                                          _fmt_start(end, True))
            if df is not None and not df.empty:
                return df, "baostock"
        except Exception as e:
            log.warning(f"Baostock fail {code}: {e}")

        return None, None

    def get_realtime(self, code: str, wait_for_rate_limit: bool = False) -> dict:
        """实时行情。优先 Tushare rt_min；失败再试 AKShare，不再调用腾讯接口。"""
        if self.tushare:
            try:
                rt = self.tushare.get_realtime(
                    code, wait_for_rate_limit=wait_for_rate_limit
                )
                if rt:
                    return _enrich_realtime_from_daily(code, rt)
            except Exception as e:
                log.warning(f"Tushare realtime fail {code}: {e}")

        try:
            rt = self.akshare.get_realtime(code)
            if rt:
                return _enrich_realtime_from_daily(code, rt)
        except Exception as e:
            log.warning(f"Realtime fail {code}: {e}")
        return {}


def _enrich_realtime_from_daily(code: str, rt: dict) -> dict:
    """Use local daily quote cache to fill prev_close/change_pct."""
    out = dict(rt or {})
    try:
        from ..db import query_one

        snap = query_one(
            "SELECT close FROM daily_quotes WHERE stock_code=? "
            "ORDER BY trade_date DESC LIMIT 1",
            (code,),
        ) or {}
        prev_close = _to_float(out.get("prev_close")) or _to_float(snap.get("close"))
        price = _to_float(out.get("price"))
        if prev_close:
            out["prev_close"] = prev_close
        if price is not None and prev_close and out.get("change_pct") is None:
            out["change_pct"] = (price - prev_close) / prev_close * 100
    except Exception as e:
        log.debug("realtime enrich fail %s: %s", code, e)
    return out


def _to_float(value):
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None
