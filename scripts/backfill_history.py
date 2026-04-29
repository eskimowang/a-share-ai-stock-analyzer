"""历史数据回填 —— 为 14 手法 playbook 补齐日线 + 资金流 + 融资融券 180-365 天。

只对"持仓 + 自选"个股跑，避免拉爆 Tushare 配额。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
import logging
import yaml
from datetime import datetime, timedelta

from app.data_sources.tushare_client import TushareClient
from app.db import db, execute, query_all

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill")

with open("/opt/stock-analyzer/config/config.yaml") as f:
    cfg = yaml.safe_load(f)
ts = TushareClient(cfg["data_sources"]["tushare"]["token"])


def get_target_codes() -> list[str]:
    rows = query_all(
        "SELECT DISTINCT stock_code as code FROM positions WHERE status='holding' "
        "UNION SELECT stock_code as code FROM watchlist"
    )
    # 过滤 ETF（只要个股）
    return [r["code"] for r in rows if not ts.is_fund(r["code"])]


def backfill_daily(codes: list[str], days: int = 365):
    log.info(f"→ 回填日线（{len(codes)} 只，{days} 天）")
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    ok = fail = 0
    total_rows = 0
    for code in codes:
        try:
            df = ts.get_daily(code, start=start, end=end)
            if df is None or df.empty:
                fail += 1; continue
            with db() as c:
                for _, r in df.iterrows():
                    c.execute(
                        "INSERT OR REPLACE INTO daily_quotes"
                        "(stock_code, trade_date, open, high, low, close, volume, amount, change_pct, data_source) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (code, r["trade_date"], r["open"], r["high"], r["low"], r["close"],
                         int(r["volume"] or 0), r["amount"], r["change_pct"], "tushare_backfill"),
                    )
                    total_rows += 1
            ok += 1
        except Exception as e:
            fail += 1
            log.warning(f"  {code} 失败: {e}")
    log.info(f"  日线回填: ✅{ok} ❌{fail} 共 {total_rows} 条")


def backfill_moneyflow(codes: list[str], days: int = 120):
    """资金流 tushare 限制近 2 年，每股拉近 120 天"""
    log.info(f"→ 回填资金流（{len(codes)} 只，{days} 天）")
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    ok = fail = 0
    for code in codes:
        try:
            df = ts.get_moneyflow(code, start=start, end=end)
            if df is None or df.empty:
                fail += 1; continue
            with db() as c:
                for _, r in df.iterrows():
                    c.execute(
                        "INSERT OR REPLACE INTO moneyflow_cache"
                        "(stock_code, trade_date, net_mf_vol, net_mf_amount, buy_lg_amount, sell_lg_amount, net_d5_amount) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (code, r.get("trade_date"), r.get("net_mf_vol"),
                         r.get("net_mf_amount"), r.get("buy_lg_amount"),
                         r.get("sell_lg_amount"), r.get("net_d5_amount")),
                    )
            ok += 1
        except Exception as e:
            fail += 1
            log.warning(f"  {code} 失败: {e}")
    log.info(f"  资金流回填: ✅{ok} ❌{fail}")


def backfill_margin(codes: list[str], days: int = 60):
    log.info(f"→ 回填融资融券（{len(codes)} 只，{days} 天）")
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    ok = fail = 0
    for code in codes:
        try:
            df = ts.pro.margin_detail(ts_code=f"{code}.{('SH' if code.startswith(('6','9')) else 'SZ')}",
                                       start_date=start, end_date=end)
            if df is None or df.empty:
                fail += 1; continue
            with db() as c:
                for _, r in df.iterrows():
                    c.execute(
                        "INSERT OR REPLACE INTO margin_detail_cache"
                        "(stock_code, trade_date, rzye, rqye) VALUES (?,?,?,?)",
                        (code, r.get("trade_date"), r.get("rzye"), r.get("rqye")),
                    )
            ok += 1
        except Exception as e:
            fail += 1
    log.info(f"  融资融券回填: ✅{ok} ❌{fail}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--skip-daily", action="store_true")
    ap.add_argument("--skip-mf", action="store_true")
    ap.add_argument("--skip-margin", action="store_true")
    args = ap.parse_args()

    codes = get_target_codes()
    log.info(f"目标: {len(codes)} 只个股")

    if not args.skip_daily:
        backfill_daily(codes, days=args.days)
    if not args.skip_mf:
        backfill_moneyflow(codes, days=min(120, args.days))
    if not args.skip_margin:
        backfill_margin(codes, days=min(60, args.days))

    # 统计最终覆盖
    for code in codes[:3]:
        rows = query_all(
            "SELECT MIN(trade_date) as mn, MAX(trade_date) as mx, COUNT(*) as n "
            "FROM daily_quotes WHERE stock_code=?", (code,),
        )
        if rows:
            r = rows[0]
            log.info(f"  {code}: {r['mn']} ~ {r['mx']}, {r['n']} 条")


if __name__ == "__main__":
    main()
