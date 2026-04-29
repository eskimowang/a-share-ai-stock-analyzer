"""Tushare Pro 数据源（付费主源，¥200 基础 + ¥500 研报）。"""
import threading
import time
from datetime import datetime

import tushare as ts
import pandas as pd


def _ts_code(code: str) -> str:
    """600519 → 600519.SH, 300244 → 300244.SZ"""
    if "." in code:
        return code.upper()
    suffix = "SH" if code.startswith(("6", "9")) else "SZ"
    return f"{code}.{suffix}"


class TushareClient:
    _rt_lock = threading.Lock()
    _rt_calls = []
    _rt_cache = {}
    _rt_limit = 10
    _rt_window_seconds = 3600
    _rt_max_wait_seconds = 90
    _rt_cache_ttl_seconds = 20
    _rt_block_until = 0

    def __init__(self, token: str):
        if not token:
            raise ValueError("Tushare token required")
        ts.set_token(token)
        self.pro = ts.pro_api()

    # ========== 行情 ==========
    def get_daily(self, code: str, start: str = "20200101", end: str = None) -> pd.DataFrame:
        ts_code = _ts_code(code)
        df = self.pro.daily(ts_code=ts_code, start_date=start, end_date=end or "")
        if df.empty:
            return df
        df = df.rename(columns={"pct_chg": "change_pct", "vol": "volume"})
        df["stock_code"] = ts_code.split(".")[0]
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
        df = df.sort_values("trade_date")
        return df[["stock_code", "trade_date", "open", "high", "low", "close",
                   "volume", "amount", "change_pct"]]

    def get_daily_basic(self, code: str, end: str = None) -> pd.DataFrame:
        """PE/PB/PS/换手率等每日估值指标。"""
        return self.pro.daily_basic(
            ts_code=_ts_code(code), end_date=end or "",
            fields="ts_code,trade_date,close,turnover_rate,pe_ttm,pb,ps_ttm,dv_ratio,total_mv,circ_mv",
        )

    def get_realtime(self, code: str, wait_for_rate_limit: bool = False) -> dict:
        """Tushare 分钟线实时快照。

        当前账户 rt_min 频率较低（实测 10 次/小时）。默认超过额度直接返回空数据；
        对午盘/异动这类小批量持仓任务，可短暂等待窗口释放，但不会长时间卡住调度器。
        """
        ts_code = _ts_code(code)
        cache_key = ts_code
        now = time.time()
        cached = self._rt_cache.get(cache_key)
        if cached and now - cached[0] <= self._rt_cache_ttl_seconds:
            return dict(cached[1])

        self._reserve_rt_slot(wait_for_rate_limit=wait_for_rate_limit)
        try:
            df = self.pro.rt_min(ts_code=ts_code, freq="1MIN")
        except Exception as e:
            if "频率超限" in str(e):
                self._block_rt_budget()
            raise
        if df is None or df.empty:
            return {}

        r = df.iloc[0].to_dict()
        ts = _normalize_rt_time(r.get("time"))
        result = {
            "code": ts_code.split(".")[0],
            "ts_code": ts_code,
            "name": None,
            "price": _to_float(r.get("close")),
            "open": _to_float(r.get("open")),
            "high": _to_float(r.get("high")),
            "low": _to_float(r.get("low")),
            "volume": _to_float(r.get("vol")),
            "amount": _to_float(r.get("amount")),
            "timestamp": ts,
            "_source": "tushare_rt_min",
        }
        self._rt_cache[cache_key] = (time.time(), dict(result))
        return result

    def _reserve_rt_slot(self, wait_for_rate_limit: bool) -> None:
        while True:
            cls = type(self)
            with cls._rt_lock:
                now = time.time()
                if now < cls._rt_block_until:
                    wait_seconds = cls._rt_block_until - now
                    if not wait_for_rate_limit or wait_seconds > cls._rt_max_wait_seconds:
                        raise RuntimeError("Tushare rt_min hourly budget exhausted")
                    sleep_seconds = max(1.0, wait_seconds)
                else:
                    sleep_seconds = 0
                if sleep_seconds:
                    pass
                else:
                    cls._rt_calls = [
                        t for t in cls._rt_calls if now - t < cls._rt_window_seconds
                    ]
                    if len(cls._rt_calls) < cls._rt_limit:
                        cls._rt_calls.append(now)
                        return
                    wait_seconds = cls._rt_window_seconds - (now - cls._rt_calls[0]) + 0.5
                    if not wait_for_rate_limit:
                        raise RuntimeError("Tushare rt_min rate budget exhausted")
                    if wait_seconds > cls._rt_max_wait_seconds:
                        raise RuntimeError("Tushare rt_min hourly budget exhausted")
                    sleep_seconds = max(1.0, wait_seconds)

            time.sleep(sleep_seconds)

    def _block_rt_budget(self) -> None:
        cls = type(self)
        with cls._rt_lock:
            now = time.time()
            cls._rt_block_until = max(cls._rt_block_until, now + cls._rt_window_seconds)
            cls._rt_calls = [now] * cls._rt_limit

    # ========== 基本信息 ==========
    def get_basics(self, code: str) -> dict:
        ts_code = _ts_code(code)
        df = self.pro.stock_basic(ts_code=ts_code)
        if df.empty:
            return {}
        r = df.iloc[0].to_dict()
        return {
            "ts_code": r["ts_code"], "name": r["name"],
            "industry": r.get("industry"), "market": r.get("market"),
            "list_date": r.get("list_date"),
        }

    # ========== 财务三大表 ==========
    def get_income(self, code: str, period: str = None) -> pd.DataFrame:
        """利润表。period 如 20241231 / 20250331。"""
        return self.pro.income(ts_code=_ts_code(code), period=period or "")

    def get_balance(self, code: str, period: str = None) -> pd.DataFrame:
        return self.pro.balancesheet(ts_code=_ts_code(code), period=period or "")

    def get_cashflow(self, code: str, period: str = None) -> pd.DataFrame:
        return self.pro.cashflow(ts_code=_ts_code(code), period=period or "")

    def get_fina_indicator(self, code: str, period: str = None) -> pd.DataFrame:
        """ROE/净利率/毛利率 等财务指标。"""
        return self.pro.fina_indicator(ts_code=_ts_code(code), period=period or "")

    # ========== 研报（¥500/年 独立权限）==========
    def get_reports(self, code: str, start: str = None, end: str = None, limit: int = 50) -> pd.DataFrame:
        """券商研报摘要。字段含 评级/目标价/盈利预测。"""
        return self.pro.report_rc(
            ts_code=_ts_code(code), start_date=start or "", end_date=end or "",
            limit=limit,
        )

    def get_consensus(self, code: str) -> pd.DataFrame:
        """券商一致预期（多家券商预测的平均）。"""
        return self.pro.report_rc(ts_code=_ts_code(code))

    # ========== 龙虎榜 ==========
    def get_top_list(self, trade_date: str, code: str = None) -> pd.DataFrame:
        kwargs = {"trade_date": trade_date}
        if code:
            kwargs["ts_code"] = _ts_code(code)
        return self.pro.top_list(**kwargs)

    # ========== 资金流 ==========
    def get_moneyflow(self, code: str, start: str = None, end: str = None) -> pd.DataFrame:
        return self.pro.moneyflow(ts_code=_ts_code(code),
                                   start_date=start or "", end_date=end or "")

    # ========== S 级: 股东户数（议会 S 级筹码真信号）==========
    def get_holder_number(self, code: str, start: str = None, end: str = None) -> pd.DataFrame:
        """股东户数（季度数据，造假成本高，A 股核心筹码信号）"""
        return self.pro.stk_holdernumber(
            ts_code=_ts_code(code),
            start_date=start or "", end_date=end or "",
        )

    # ========== S 级: 解禁股份（减持压力预警）==========
    def get_share_float(self, code: str = None, start: str = None, end: str = None) -> pd.DataFrame:
        """限售股解禁时间表（必披露，刚性约束）"""
        kwargs = {"start_date": start or "", "end_date": end or ""}
        if code:
            kwargs["ts_code"] = _ts_code(code)
        return self.pro.share_float(**kwargs)

    # ========== A 级: 融资融券 ==========
    def get_margin_detail(self, code: str, start: str = None, end: str = None) -> pd.DataFrame:
        """个股融资融券余额（杠杆情绪）"""
        return self.pro.margin_detail(
            ts_code=_ts_code(code),
            start_date=start or "", end_date=end or "",
        )

    # ========== ETF / 基金 ==========
    def get_fund_daily(self, code: str, start: str = None, end: str = None) -> pd.DataFrame:
        """ETF/基金日线行情（和 stock_daily 不同接口）"""
        # 上交所 ETF: 5xxxxx (如 510300 / 588000)
        # 深交所 ETF/LOF: 159xxx / 16xxxx / 162xxx / 184xxx
        if "." in code:
            ts_code = code
        elif code.startswith("5"):
            ts_code = f"{code}.SH"
        else:
            ts_code = f"{code}.SZ"  # 159xxx, 16xxxx 都是深交所
        df = self.pro.fund_daily(ts_code=ts_code,
                                   start_date=start or "", end_date=end or "")
        if df.empty:
            return df
        df = df.rename(columns={"pct_chg": "change_pct", "vol": "volume"})
        df["stock_code"] = ts_code.split(".")[0]
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
        return df.sort_values("trade_date")[
            ["stock_code", "trade_date", "open", "high", "low", "close",
             "volume", "amount", "change_pct"]
        ]

    def is_fund(self, code: str) -> bool:
        """判断是不是 ETF/LOF（通常 5/15/16/501/502/506/508 开头）"""
        return code.startswith(("5", "15", "16"))


def _to_float(value):
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _normalize_rt_time(value) -> str:
    if not value:
        return ""
    try:
        return pd.to_datetime(value).strftime("%Y%m%d%H%M%S")
    except Exception:
        return datetime.now().strftime("%Y%m%d%H%M%S")
