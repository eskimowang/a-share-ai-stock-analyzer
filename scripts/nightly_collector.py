"""夜间数据采集守护 —— 每晚 02:00 由 systemd timer 触发。

采集目标（按优先级）:
1. 持仓 + 自选
2. IVD 全量 A 股
3. 用户关注的其他股票（未来荐股候选池）

采集内容:
- 日线行情（过去 5 天，用于复查修正）
- 每日估值（daily_basic）
- 财务指标（fina_indicator，季度更新）
- 研报（report_rc，限流接口）
- 公告（anns_d，未来扩展）

限流保护:
- REPORT_RATE_LIMIT_S 环境变量控制研报接口间隔
- 2000 积分: 32 秒/次
- 5000 积分: 6 秒/次（需用户升级后改）
"""
import sys
import os
import time
import logging
from datetime import datetime, timedelta

sys.path.insert(0, "/opt/stock-analyzer")

import yaml
import akshare as ak
from app.data_sources.tushare_client import TushareClient
from app.db import db, execute, query_all


# ---------- 日志 ----------
LOG_FILE = "/opt/stock-analyzer/logs/nightly.log"
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("nightly")

# ---------- 配置 ----------
with open("/opt/stock-analyzer/config/config.yaml") as f:
    cfg = yaml.safe_load(f)

ts = TushareClient(cfg["data_sources"]["tushare"]["token"])


def _filter_stocks(codes: list[str]) -> list[str]:
    """过滤掉 ETF/LOF（资金流/龙虎榜/股东户数/研报/融资融券/解禁 都只对个股有意义）"""
    return [c for c in codes if not ts.is_fund(c)]

# 研报限流（可通过环境变量调整）
# 2000 积分 → 2/分钟 → 32 秒
# 5000 积分 → 10+/分钟 → 6-8 秒
# 8000 积分 → 20+/分钟 → 3-5 秒
REPORT_RATE_LIMIT_S = float(os.environ.get("REPORT_RATE_LIMIT_S", "32"))


# ---------- 目标清单 ----------
def target_codes_daily() -> list[str]:
    """日常采集：持仓 + 自选（每晚跑）"""
    codes = set()
    for r in query_all("SELECT DISTINCT stock_code FROM positions WHERE status='holding'"):
        codes.add(r["stock_code"])
    for r in query_all("SELECT DISTINCT stock_code FROM watchlist"):
        codes.add(r["stock_code"])
    return sorted(codes)


def target_codes_monthly() -> list[str]:
    """月度采集：IVD 全量 A 股（每月 1 号跑）"""
    codes = set()
    # IVD 全量（仅 A 股）
    for r in query_all(
        "SELECT DISTINCT code FROM ivd_companies WHERE exchange IN ('SH', 'SZ') AND is_active=1"
    ):
        codes.add(r["code"])
    # 并入日常清单，保证月度一次跑全
    codes.update(target_codes_daily())
    return sorted(codes)


# ---------- 任务日志 ----------
def start_job(name: str, targets: int) -> int:
    with db() as c:
        cur = c.execute(
            "INSERT INTO nightly_job_log(job_name, targets_count, status) VALUES (?,?,?)",
            (name, targets, "running"),
        )
        return cur.lastrowid


def finish_job(job_id: int, success: int, fail: int, errors: str = ""):
    status = "success" if fail == 0 else ("partial" if success > 0 else "failed")
    with db() as c:
        c.execute(
            "UPDATE nightly_job_log SET end_time=CURRENT_TIMESTAMP, status=?, "
            "success_count=?, fail_count=?, error_summary=? WHERE id=?",
            (status, success, fail, errors[:2000], job_id),
        )


# ---------- 采集：日线 ----------
def collect_daily_quotes(codes: list[str]):
    log.info(f"→ 采集日线（{len(codes)} 只）")
    job_id = start_job("daily_quotes", len(codes))
    success = fail = 0
    errs = []

    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=5)).strftime("%Y%m%d")

    for code in codes:
        try:
            # ETF/基金走 fund_daily，股票走 daily
            if ts.is_fund(code):
                df = ts.get_fund_daily(code, start=start, end=end)
                source = "tushare_fund"
            else:
                df = ts.get_daily(code, start=start, end=end)
                source = "tushare"
            if df is None or df.empty:
                fail += 1
                continue
            with db() as c:
                for _, r in df.iterrows():
                    c.execute(
                        "INSERT OR REPLACE INTO daily_quotes"
                        "(stock_code, trade_date, open, high, low, close, volume, amount, change_pct, data_source) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (
                            code, r["trade_date"],
                            r["open"], r["high"], r["low"], r["close"],
                            int(r["volume"] or 0), r["amount"], r["change_pct"],
                            source,
                        ),
                    )
            success += 1
        except Exception as e:
            fail += 1
            errs.append(f"{code}: {str(e)[:80]}")

    finish_job(job_id, success, fail, " | ".join(errs[:20]))
    log.info(f"  日线完成: ✅ {success}  ❌ {fail}")


# ---------- 采集：财务 ----------
def collect_financials(codes: list[str]):
    codes = _filter_stocks(codes)
    log.info(f"→ 采集财务（{len(codes)} 只个股，ETF 已跳过）")
    job_id = start_job("financials", len(codes))
    success = fail = 0
    errs = []

    for code in codes:
        try:
            fi = ts.get_fina_indicator(code)
            if fi is None or fi.empty:
                fail += 1
                continue
            with db() as c:
                for _, r in fi.iterrows():
                    c.execute(
                        "INSERT OR REPLACE INTO financials"
                        "(stock_code, report_period, roe, gross_margin, net_margin, data_source) "
                        "VALUES (?,?,?,?,?,?)",
                        (
                            code, r.get("end_date"),
                            r.get("roe"),
                            r.get("grossprofit_margin"),
                            r.get("netprofit_margin"),
                            "tushare",
                        ),
                    )
            success += 1
        except Exception as e:
            fail += 1
            errs.append(f"{code}: {str(e)[:80]}")

    finish_job(job_id, success, fail, " | ".join(errs[:20]))
    log.info(f"  财务完成: ✅ {success}  ❌ {fail}")


# ---------- 采集：研报（改用 AKShare 东方财富，免费无限）----------
def collect_reports(codes: list[str]):
    codes = _filter_stocks(codes)
    log.info(f"→ 采集研报（{len(codes)} 只个股，AKShare 东财源，ETF 已跳过）")
    job_id = start_job("reports", len(codes))
    success = fail = 0
    errs = []
    total_reports = 0

    # AKShare 返回字段名的多种可能（东财接口字段曾变化）
    field_map = {
        "date": ["日期", "发布日期", "report_date"],
        "broker": ["机构", "机构名称", "org_name"],
        "author": ["研究员", "分析师", "author"],
        "rating": ["东财评级", "评级", "rating"],
        "title": ["标题", "研报标题", "title"],
        "target_price": ["目标价", "目标价(元)"],
    }

    def pick(row, keys, default=None):
        for k in keys:
            v = row.get(k)
            if v is not None and str(v).strip() and str(v) != "nan":
                return v
        return default

    for i, code in enumerate(codes):
        try:
            df = ak.stock_research_report_em(symbol=code)
            if df is None or df.empty:
                fail += 1
                continue
            with db() as c:
                for _, r in df.iterrows():
                    try:
                        c.execute(
                            "INSERT OR IGNORE INTO reports_cache"
                            "(stock_code, report_date, broker, author, rating, target_price, title, data_source) "
                            "VALUES (?,?,?,?,?,?,?,?)",
                            (
                                code,
                                str(pick(r, field_map["date"], ""))[:20],
                                str(pick(r, field_map["broker"], ""))[:100],
                                str(pick(r, field_map["author"], ""))[:200],
                                str(pick(r, field_map["rating"], ""))[:20],
                                None,  # AKShare 目标价解析复杂，先跳过
                                str(pick(r, field_map["title"], ""))[:200],
                                "akshare_em",
                            ),
                        )
                        total_reports += 1
                    except Exception:
                        pass
            success += 1

            if (i + 1) % 20 == 0:
                log.info(f"  进度 {i+1}/{len(codes)}（共收集 {total_reports} 份）")
            time.sleep(0.5)  # 轻限流，对 AKShare 礼貌
        except Exception as e:
            fail += 1
            errs.append(f"{code}: {str(e)[:80]}")

    finish_job(job_id, success, fail, " | ".join(errs[:20]))
    log.info(f"  研报完成: ✅ {success}  ❌ {fail}  收集 {total_reports} 份")


# ---------- 主流程 ----------
# ---------- 采集：Tushare 独有能力 ----------
def _ts_code(code: str) -> str:
    if "." in code: return code.upper()
    return f"{code}.{'SH' if code.startswith(('6','9')) else 'SZ'}"


def _ensure_extra_tables():
    """建表（一次性，幂等）存储 Tushare 独有能力"""
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS daily_basic (
            stock_code TEXT NOT NULL,
            trade_date DATE NOT NULL,
            close REAL, pe_ttm REAL, pb REAL, ps_ttm REAL,
            turnover_rate REAL, total_mv REAL, circ_mv REAL,
            fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(stock_code, trade_date)
        );
        CREATE TABLE IF NOT EXISTS moneyflow_cache (
            stock_code TEXT NOT NULL,
            trade_date DATE NOT NULL,
            net_mf_vol REAL, net_mf_amount REAL,
            buy_lg_amount REAL, sell_lg_amount REAL,
            net_d5_amount REAL,
            fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(stock_code, trade_date)
        );
        CREATE TABLE IF NOT EXISTS top_list_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stock_code TEXT, stock_name TEXT,
            trade_date DATE, reason TEXT,
            net_buy_amount REAL, total_buy REAL, total_sell REAL,
            fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(stock_code, trade_date, reason)
        );
        -- S 级：股东户数环比（筹码真信号）
        CREATE TABLE IF NOT EXISTS holder_number_cache (
            stock_code TEXT NOT NULL,
            end_date DATE NOT NULL,
            ann_date DATE,
            holder_num INTEGER,
            fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(stock_code, end_date)
        );
        -- S 级：解禁时间表（减持预警）
        CREATE TABLE IF NOT EXISTS share_float_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stock_code TEXT NOT NULL,
            ann_date DATE,
            float_date DATE,
            float_share REAL,                -- 流通股数量
            float_ratio REAL,                -- 占流通比
            holder_name TEXT,
            share_type TEXT,
            fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(stock_code, float_date, holder_name)
        );
        -- A 级：融资融券余额
        CREATE TABLE IF NOT EXISTS margin_detail_cache (
            stock_code TEXT NOT NULL,
            trade_date DATE NOT NULL,
            rzye REAL,            -- 融资余额
            rqye REAL,            -- 融券余额
            rzmre REAL,           -- 融资买入额
            rqmcl REAL,           -- 融券卖出量
            rzrqye REAL,          -- 两融余额
            fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(stock_code, trade_date)
        );
        """)


def collect_daily_basic(codes: list[str]):
    """每日估值快照（PE/PB/PS/换手率/市值）—— 仅个股，ETF 无估值"""
    codes = _filter_stocks(codes)
    log.info(f"→ 采集每日估值（{len(codes)} 只个股，ETF 已跳过）")
    _ensure_extra_tables()
    job_id = start_job("daily_basic", len(codes))
    success = fail = 0
    for code in codes:
        try:
            df = ts.get_daily_basic(code)
            if df is None or df.empty:
                fail += 1; continue
            with db() as c:
                for _, r in df.iterrows():
                    c.execute(
                        "INSERT OR REPLACE INTO daily_basic"
                        "(stock_code, trade_date, close, pe_ttm, pb, ps_ttm, turnover_rate, total_mv, circ_mv) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (code, r.get("trade_date"), r.get("close"),
                         r.get("pe_ttm"), r.get("pb"), r.get("ps_ttm"),
                         r.get("turnover_rate"), r.get("total_mv"), r.get("circ_mv"))
                    )
            success += 1
        except Exception:
            fail += 1
    finish_job(job_id, success, fail, "")
    log.info(f"  估值完成: ✅ {success}  ❌ {fail}")


def collect_moneyflow(codes: list[str]):
    """资金流向（大单小单，近 10 日）—— 仅个股，ETF 跳过"""
    codes = _filter_stocks(codes)
    log.info(f"→ 采集资金流向（{len(codes)} 只个股，ETF 已跳过）")
    _ensure_extra_tables()
    job_id = start_job("moneyflow", len(codes))
    success = fail = 0
    end = datetime.now().strftime("%Y%m%d")
    start_d = (datetime.now() - timedelta(days=10)).strftime("%Y%m%d")
    for code in codes:
        try:
            df = ts.get_moneyflow(code, start=start_d, end=end)
            if df is not None and not df.empty:
                with db() as c:
                    for _, r in df.iterrows():
                        c.execute(
                            "INSERT OR REPLACE INTO moneyflow_cache"
                            "(stock_code, trade_date, net_mf_vol, net_mf_amount, buy_lg_amount, sell_lg_amount, net_d5_amount) "
                            "VALUES (?,?,?,?,?,?,?)",
                            (code, r.get("trade_date"),
                             r.get("net_mf_vol"), r.get("net_mf_amount"),
                             r.get("buy_lg_amount"), r.get("sell_lg_amount"),
                             r.get("net_d5_amount"))
                        )
                success += 1
            else:
                fail += 1
        except Exception:
            fail += 1
    finish_job(job_id, success, fail, "")
    log.info(f"  资金流完成: ✅ {success}  ❌ {fail}")


def collect_holder_number(codes: list[str]):
    """股东户数（季度，S 级筹码信号）—— 仅个股，ETF 跳过"""
    codes = _filter_stocks(codes)
    log.info(f"→ 采集股东户数（{len(codes)} 只个股，季度数据，ETF 已跳过）")
    _ensure_extra_tables()
    job_id = start_job("holder_number", len(codes))
    success = fail = 0
    # 最近 2 年的股东户数（够看 4-8 个季度趋势）
    end = datetime.now().strftime("%Y%m%d")
    start_d = (datetime.now() - timedelta(days=730)).strftime("%Y%m%d")
    for code in codes:
        try:
            df = ts.get_holder_number(code, start=start_d, end=end)
            if df is None or df.empty:
                fail += 1; continue
            with db() as c:
                for _, r in df.iterrows():
                    c.execute(
                        "INSERT OR REPLACE INTO holder_number_cache"
                        "(stock_code, end_date, ann_date, holder_num) VALUES (?,?,?,?)",
                        (code, r.get("end_date"), r.get("ann_date"),
                         int(r.get("holder_num") or 0))
                    )
            success += 1
        except Exception:
            fail += 1
    finish_job(job_id, success, fail, "")
    log.info(f"  股东户数: ✅ {success}  ❌ {fail}")


def collect_share_float(codes: list[str]):
    """解禁时间表（S 级减持预警，未来 180 天）—— 仅个股"""
    codes = _filter_stocks(codes)
    log.info(f"→ 采集解禁时间表（{len(codes)} 只个股，ETF 已跳过）")
    _ensure_extra_tables()
    job_id = start_job("share_float", len(codes))
    success = fail = 0
    total = 0
    # 未来 6 个月的解禁计划
    start_d = datetime.now().strftime("%Y%m%d")
    end = (datetime.now() + timedelta(days=180)).strftime("%Y%m%d")
    for code in codes:
        try:
            df = ts.get_share_float(code=code, start=start_d, end=end)
            if df is None or df.empty:
                continue  # 没解禁不算失败
            with db() as c:
                for _, r in df.iterrows():
                    c.execute(
                        "INSERT OR IGNORE INTO share_float_cache"
                        "(stock_code, ann_date, float_date, float_share, float_ratio, holder_name, share_type) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (code, r.get("ann_date"), r.get("float_date"),
                         r.get("float_share"), r.get("float_ratio"),
                         r.get("holder_name"), r.get("share_type"))
                    )
                    total += 1
            success += 1
        except Exception:
            fail += 1
    finish_job(job_id, success, fail, "")
    log.info(f"  解禁时间表: ✅ {success}  ❌ {fail}  收集 {total} 条未来 180 天解禁事件")


def collect_margin(codes: list[str]):
    """融资融券余额（A 级杠杆情绪，近 10 日）—— 仅个股"""
    codes = _filter_stocks(codes)
    log.info(f"→ 采集融资融券（{len(codes)} 只个股，ETF 已跳过）")
    _ensure_extra_tables()
    job_id = start_job("margin", len(codes))
    success = fail = 0
    end = datetime.now().strftime("%Y%m%d")
    start_d = (datetime.now() - timedelta(days=15)).strftime("%Y%m%d")
    for code in codes:
        try:
            df = ts.get_margin_detail(code, start=start_d, end=end)
            if df is None or df.empty:
                fail += 1; continue
            with db() as c:
                for _, r in df.iterrows():
                    c.execute(
                        "INSERT OR REPLACE INTO margin_detail_cache"
                        "(stock_code, trade_date, rzye, rqye, rzmre, rqmcl, rzrqye) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (code, r.get("trade_date"),
                         r.get("rzye"), r.get("rqye"),
                         r.get("rzmre"), r.get("rqmcl"), r.get("rzrqye"))
                    )
            success += 1
        except Exception:
            fail += 1
    finish_job(job_id, success, fail, "")
    log.info(f"  融资融券: ✅ {success}  ❌ {fail}")


def collect_top_list():
    """龙虎榜（按日期，一次拿全市场）"""
    log.info("→ 采集龙虎榜（最近 5 日）")
    _ensure_extra_tables()
    job_id = start_job("top_list", 5)
    success = fail = 0
    total = 0
    for days_ago in range(0, 5):
        day = (datetime.now() - timedelta(days=days_ago)).strftime("%Y%m%d")
        try:
            df = ts.get_top_list(day)
            if df is not None and not df.empty:
                with db() as c:
                    for _, r in df.iterrows():
                        c.execute(
                            "INSERT OR IGNORE INTO top_list_cache"
                            "(stock_code, stock_name, trade_date, reason, net_buy_amount, total_buy, total_sell) "
                            "VALUES (?,?,?,?,?,?,?)",
                            (r.get("ts_code","").split(".")[0], r.get("name"),
                             r.get("trade_date"), r.get("reason"),
                             r.get("net_amount"), r.get("amount"), r.get("sell_amount"))
                        )
                        total += 1
                success += 1
                log.info(f"  {day}: {len(df)} 条龙虎榜")
        except Exception:
            fail += 1
    finish_job(job_id, success, fail, "")
    log.info(f"  龙虎榜: ✅ {success} 天  ❌ {fail} 天  收集 {total} 条")


# ---------- IVD 港美股日线（AKShare，月度）----------
def collect_ivd_foreign_daily():
    """采集 IVD 港股 + 美股近 90 日日线（AKShare 免费）。"""
    log.info("→ 采集 IVD 港美股日线（AKShare）")
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS foreign_daily_quotes (
            exchange TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            trade_date DATE NOT NULL,
            open REAL, high REAL, low REAL, close REAL,
            volume REAL, amount REAL,
            PRIMARY KEY (exchange, stock_code, trade_date)
        );
        """)

    hk = query_all(
        "SELECT code, name FROM ivd_companies WHERE exchange='HK' AND is_active=1"
    )
    us = query_all(
        "SELECT code, name, exchange FROM ivd_companies "
        "WHERE exchange IN ('NASDAQ','NYSE') AND is_active=1"
    )

    total_codes = len(hk) + len(us)
    job_id = start_job("ivd_foreign_daily", total_codes)
    success = fail = 0
    total_rows = 0
    errs = []

    # HK: ak.stock_hk_daily(symbol='01548', adjust='qfq')
    for r in hk:
        code = str(r["code"]).zfill(5)
        try:
            df = ak.stock_hk_daily(symbol=code, adjust="qfq")
            if df is None or df.empty:
                fail += 1
                continue
            df = df.tail(90)
            with db() as c:
                for _, row in df.iterrows():
                    c.execute(
                        "INSERT OR REPLACE INTO foreign_daily_quotes"
                        "(exchange, stock_code, trade_date, open, high, low, close, volume, amount) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        ("HK", code, str(row.get("date") or row.get("日期"))[:10],
                         float(row.get("open") or 0), float(row.get("high") or 0),
                         float(row.get("low") or 0), float(row.get("close") or 0),
                         float(row.get("volume") or 0), float(row.get("amount", 0) or 0))
                    )
                    total_rows += 1
            success += 1
        except Exception as e:
            fail += 1
            errs.append(f"HK:{code}:{str(e)[:80]}")

    # US: ak.stock_us_daily(symbol='ABT', adjust='qfq')
    for r in us:
        sym = str(r["code"]).upper()
        ex = r["exchange"]
        try:
            df = ak.stock_us_daily(symbol=sym, adjust="qfq")
            if df is None or df.empty:
                fail += 1
                continue
            df = df.tail(90)
            with db() as c:
                for _, row in df.iterrows():
                    c.execute(
                        "INSERT OR REPLACE INTO foreign_daily_quotes"
                        "(exchange, stock_code, trade_date, open, high, low, close, volume, amount) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (ex, sym, str(row.get("date") or row.get("日期"))[:10],
                         float(row.get("open") or 0), float(row.get("high") or 0),
                         float(row.get("low") or 0), float(row.get("close") or 0),
                         float(row.get("volume") or 0), 0.0)
                    )
                    total_rows += 1
            success += 1
        except Exception as e:
            fail += 1
            errs.append(f"{ex}:{sym}:{str(e)[:80]}")

    finish_job(job_id, success, fail, " | ".join(errs[:10]))
    log.info(f"  IVD港美股: ✅ {success}  ❌ {fail}  收集 {total_rows} 条")


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["daily", "monthly"], default="daily",
                    help="daily=持仓+自选（每晚）/ monthly=+IVD 全量（每月 1 号）")
    args = ap.parse_args()

    log.info("=" * 60)
    log.info(f"数据采集开始 {datetime.now():%Y-%m-%d %H:%M}  mode={args.mode}")
    log.info("=" * 60)
    t0 = time.time()

    codes = target_codes_monthly() if args.mode == "monthly" else target_codes_daily()
    log.info(f"目标股票: {len(codes)} 只（{args.mode} 模式）")

    if not codes:
        log.warning("目标为空，退出")
        return

    # 每日都采集的：日线/财务/估值/研报/资金流/龙虎榜/融资融券
    # S 级低频（股东户数季度、解禁半年）仅月度模式运行，节省调用
    daily_tasks = [
        ("日线", collect_daily_quotes, codes),
        ("财务", collect_financials, codes),
        ("估值", collect_daily_basic, codes),
        ("研报(AK)", collect_reports, codes),
        ("资金流", collect_moneyflow, codes),
        ("融资融券", collect_margin, codes),
        ("龙虎榜", collect_top_list, None),
    ]
    monthly_extra_tasks = [
        ("股东户数", collect_holder_number, codes),
        ("解禁时间表", collect_share_float, codes),
        ("IVD港美股日线", collect_ivd_foreign_daily, None),
    ]
    tasks = daily_tasks + (monthly_extra_tasks if args.mode == "monthly" else [])

    for name, fn, arg in tasks:
        try:
            fn(arg) if arg is not None else fn()
        except Exception as e:
            log.exception(f"{name}任务异常: {e}")

    elapsed = time.time() - t0
    log.info("=" * 60)
    log.info(f"采集完成 ({args.mode})，总耗时 {elapsed:.0f}s ({elapsed/60:.1f} 分钟)")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
