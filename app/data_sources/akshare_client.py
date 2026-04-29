"""AKShare 数据源封装（免费主源）。"""
import akshare as ak
import pandas as pd


class AKShareClient:
    @staticmethod
    def get_daily(code: str, start: str = "20200101", end: str = None) -> pd.DataFrame:
        """日线（前复权）。code 形如 600519 / 300244（纯数字无后缀）。"""
        df = ak.stock_zh_a_hist(
            symbol=code, period="daily",
            start_date=start, end_date=end or "20500101",
            adjust="qfq",
        )
        if df.empty:
            return df
        df = df.rename(columns={
            "日期": "trade_date", "开盘": "open", "最高": "high", "最低": "low",
            "收盘": "close", "成交量": "volume", "成交额": "amount",
            "涨跌幅": "change_pct", "换手率": "turnover_rate",
        })
        df["stock_code"] = code
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
        cols = ["stock_code", "trade_date", "open", "high", "low", "close",
                "volume", "amount", "change_pct", "turnover_rate"]
        return df[[c for c in cols if c in df.columns]]

    @staticmethod
    def get_realtime(code: str) -> dict:
        """实时行情快照。"""
        df = ak.stock_zh_a_spot_em()
        row = df[df["代码"] == code]
        if row.empty:
            return {}
        r = row.iloc[0].to_dict()
        return {
            "code": r["代码"], "name": r["名称"], "price": r["最新价"],
            "change_pct": r["涨跌幅"], "volume": r["成交量"],
            "amount": r["成交额"], "pe_ttm": r.get("市盈率-动态"),
            "pb": r.get("市净率"), "turnover_rate": r.get("换手率"),
        }

    @staticmethod
    def get_financial_abstract(code: str) -> pd.DataFrame:
        """财务摘要（多期指标汇总）。"""
        return ak.stock_financial_abstract(symbol=code)
