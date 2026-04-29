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

    def get_realtime(self, code: str) -> dict:
        """实时行情。优先腾讯（ECS 能通），失败 fallback AKShare。"""
        try:
            return _fetch_tencent_realtime(code)
        except Exception as e:
            log.debug(f"Tencent realtime fail {code}: {e}")
        try:
            return self.akshare.get_realtime(code)
        except Exception as e:
            log.warning(f"Realtime fail {code}: {e}")
            return {}


def _fetch_tencent_realtime(code: str) -> dict:
    """腾讯财经免费实时接口 http://qt.gtimg.cn/q=sh600150"""
    import httpx
    prefix = "sh" if code.startswith(("6", "9")) else "sz"
    url = f"http://qt.gtimg.cn/q={prefix}{code}"
    with httpx.Client(timeout=6) as client:
        r = client.get(url, headers={"Referer": "https://gu.qq.com/"})
        r.raise_for_status()
        text = r.content.decode("gbk", errors="replace")
    if "=" not in text:
        raise ValueError(f"invalid response: {text[:80]}")
    payload = text.split('"', 2)[1] if '"' in text else ""
    parts = payload.split("~")
    if len(parts) < 33:
        raise ValueError(f"parts too few: {len(parts)}")
    def _f(s):
        try:
            return float(s) if s and s.replace('.','').replace('-','').isdigit() else None
        except Exception:
            return None
    try:
        price = _f(parts[3])
        prev_close = _f(parts[4])
        # 涨跌幅自算，避开索引偏差
        change_pct = None
        if price is not None and prev_close:
            change_pct = (price - prev_close) / prev_close * 100
        return {
            "code": code,
            "name": parts[1],
            "price": price,
            "prev_close": prev_close,
            "open": _f(parts[5]),
            "volume": int(parts[6]) * 100 if parts[6] and parts[6].isdigit() else None,
            "high": _f(parts[33]),
            "low": _f(parts[34]),
            "change_pct": change_pct,
            "timestamp": parts[30] if len(parts) > 30 else None,
            "_source": "tencent",
        }
    except (ValueError, IndexError) as e:
        raise ValueError(f"parse error: {e}")
