"""Baostock 数据源（免费备份源）。"""
import baostock as bs
import pandas as pd


class BaostockClient:
    @staticmethod
    def _format_code(code: str) -> str:
        """600519 → sh.600519, 300244 → sz.300244"""
        if "." in code:
            return code.lower()
        prefix = "sh" if code.startswith(("6", "9")) else "sz"
        return f"{prefix}.{code}"

    @staticmethod
    def get_daily(code: str, start: str = "2020-01-01", end: str = None) -> pd.DataFrame:
        bs.login()
        try:
            bs_code = BaostockClient._format_code(code)
            rs = bs.query_history_k_data_plus(
                bs_code,
                "date,open,high,low,close,volume,amount,turn,pctChg",
                start_date=start, end_date=end or "",
                frequency="d", adjustflag="2",
            )
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            df = pd.DataFrame(rows, columns=[
                "trade_date", "open", "high", "low", "close",
                "volume", "amount", "turnover_rate", "change_pct",
            ])
            for c in ["open", "high", "low", "close", "amount", "turnover_rate", "change_pct"]:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype(int)
            df["stock_code"] = code.split(".")[-1] if "." in code else code
            return df[["stock_code", "trade_date", "open", "high", "low", "close",
                       "volume", "amount", "change_pct", "turnover_rate"]]
        finally:
            bs.logout()
