"""14 类 A 股操作手法量化探测器。

每个 pattern 独立打分 (0-1 置信度), 基于数据量化规则, 不依赖 AI 主观判断。

数据源:
  daily_quotes (OHLCV)
  moneyflow_cache (net_mf_amount, buy_lg_amount, sell_lg_amount)
  top_list_cache (龙虎榜)
  holder_number_cache (股东户数)
  margin_detail_cache (融资融券)

返回结构:
  {
    "pattern": "诱多出货",
    "confidence": 0.78,
    "trade_date": "2026-04-22",
    "signals": {
      "close_at_high_position": true,  # 价位偏高
      "volume_surge": 1.85,              # 量能倍率
      "fund_net_outflow": -2.3e8,        # 资金净流出 2.3 亿
      "... ": ...
    },
    "narrative": "4月22日放量3.5%涨, 但大单净流出2.3亿, 尾盘收阴"
  }
"""
from datetime import datetime, timedelta
from typing import Optional

from ..db import query_all, query_one

# 未来函数防护: 所有实时性强的数据源（龙虎榜/资金流/融资融券/研报）
# 在查询时只取 end_date 当日及之前，但要注意 end_date 应该是 T-1 以避免盘中决策时偷看当日 EOD 数据
# 日线 daily_quotes 不需要这种保护（它本身就是 EOD 数据）
def _enforce_eod_cutoff(end_date: str, context: str = 'intraday') -> str:
    """盘中场景把 end_date 回推到 T-1，防止偷看今天 EOD 数据。"""
    from datetime import datetime as _dt, timedelta as _td, time as _time
    try:
        dt = _dt.strptime(end_date[:10], '%Y-%m-%d')
    except Exception:
        return end_date
    now = _dt.now()
    # 如果请求日期是今天，且当前时间在 16:30 之前（EOD 数据未发布），回推一天
    if dt.date() == now.date() and now.time() < _time(16, 30) and context == 'intraday':
        return (dt - _td(days=1)).strftime('%Y-%m-%d')
    return end_date



# ========== 14 类手法元数据 ==========
PATTERNS = {
    "诱多出货": {
        "icon": "🔴",
        "definition": "高位放量大阳诱散户追高，主力同步大单出货",
        "key_signals": ["价位偏高(>MA20*1.12)", "放量(>5日均量1.5x)", "大单净流出 or 尾盘收阴"],
        "victim": "追高散户",
        "data_needed": ["daily_quotes", "moneyflow_cache"],
    },
    "假突破": {
        "icon": "🔴",
        "definition": "向上突破关键位但 3 日内回落破突破价",
        "key_signals": ["突破60日高点", "3天内回落 <突破价"],
        "victim": "突破买入者",
        "data_needed": ["daily_quotes"],
    },
    "拉升派发": {
        "icon": "🔴",
        "definition": "连续上涨期边涨边派，股东户数上升",
        "key_signals": ["5日均涨幅>1%", "成交放大", "资金净流出 or 股东户数升"],
        "victim": "趋势跟随者",
        "data_needed": ["daily_quotes", "moneyflow_cache", "holder_number_cache"],
    },
    "尾盘偷袭": {
        "icon": "🟡",
        "definition": "最后时段异常拉高或砸盘制造误导（需分时数据，当前降级近似）",
        "key_signals": ["close 远离 (open+low+high)/3", "收盘价靠近极值"],
        "victim": "尾盘追单者",
        "data_needed": ["daily_quotes"],
        "degraded": True,
    },
    "借利好出货": {
        "icon": "🔴",
        "definition": "买入评级研报/重大公告后 3 天反而下跌",
        "key_signals": ["近 5 日有买入级研报", "后续 3 日跌 >2%"],
        "victim": "听消息买入者",
        "data_needed": ["daily_quotes", "reports_cache"],
    },
    "借利空吸筹": {
        "icon": "🟢",
        "definition": "利空消息后不跌反涨（主力利用恐慌吸筹）",
        "key_signals": ["研报下调/评级转中性", "后续 3 日涨 >2%", "资金净流入"],
        "victim": "—",
        "data_needed": ["daily_quotes", "reports_cache", "moneyflow_cache"],
    },
    "洗盘": {
        "icon": "🟡",
        "definition": "震荡下跌但筹码集中度未变（主力甩轿）",
        "key_signals": ["20日最大回撤 >5%", "股东户数环比不升 or 微降", "缩量"],
        "victim": "不坚定散户",
        "data_needed": ["daily_quotes", "holder_number_cache"],
    },
    "吸筹": {
        "icon": "🟢",
        "definition": "缩量下跌 + 股东户数减少 + 大单承接",
        "key_signals": ["10日均涨幅<0", "缩量<20日均量0.85x", "户数环比降>3%", "大单净买>0"],
        "victim": "—",
        "data_needed": ["daily_quotes", "moneyflow_cache", "holder_number_cache"],
    },
    "对倒": {
        "icon": "🟡",
        "definition": "成交活跃但价格不动（主力自买自卖制造假象）",
        "key_signals": ["5日均量 >20日均量1.3x", "5日累计涨幅 <3% 且 >-3%"],
        "victim": "技术派",
        "data_needed": ["daily_quotes"],
    },
    "龙虎榜接力": {
        "icon": "🟡",
        "definition": "游资连续多日上榜接力维持人气",
        "key_signals": ["近 5 日上榜 >=3 次", "净买入方向一致"],
        "victim": "跟风游资",
        "data_needed": ["top_list_cache"],
    },
    "机构抱团": {
        "icon": "🟢",
        "definition": "股东户数持续下降（机构集中持仓）",
        "key_signals": ["近 3 季度户数环比均 <0", "累计降幅 >15%"],
        "victim": "—",
        "data_needed": ["holder_number_cache"],
    },
    "北向骗线": {
        "icon": "🟡",
        "definition": "北向异常买入但价走弱（疑似量化伪装，需北向持仓数据，当前不可用）",
        "key_signals": [],
        "victim": "北向跟随者",
        "data_needed": ["northbound_holding (not collected)"],
        "unavailable": True,
    },
    "大宗派发": {
        "icon": "🔴",
        "definition": "大股东折价大宗出货（需大宗交易数据，当前不可用）",
        "key_signals": [],
        "victim": "公开市场买入者",
        "data_needed": ["block_trade (not collected)"],
        "unavailable": True,
    },
    "融资爆仓": {
        "icon": "🔴",
        "definition": "融资余额骤降伴随放量下跌（强平踩踏）",
        "key_signals": ["融资余额 5 日降幅 >5%", "近 3 日跌幅累计 >3%", "放量"],
        "victim": "杠杆多头",
        "data_needed": ["margin_detail_cache", "daily_quotes"],
    },
}


# ========== 探测器辅助函数 ==========
def _fetch_window(code: str, end_date: str, days: int = 30) -> list[dict]:
    """取某股截至 end_date 的近 N 日 K 线（升序）。"""
    rows = query_all(
        "SELECT trade_date, open, high, low, close, volume, change_pct "
        "FROM daily_quotes WHERE stock_code=? AND trade_date <= ? "
        "ORDER BY trade_date DESC LIMIT ?",
        (code, end_date, days),
    )
    rows.reverse()
    return rows


def _fetch_moneyflow(code: str, end_date: str, days: int = 10) -> list[dict]:
    rows = query_all(
        "SELECT trade_date, net_mf_amount, buy_lg_amount, sell_lg_amount, net_d5_amount "
        "FROM moneyflow_cache WHERE stock_code=? AND trade_date <= ? "
        "ORDER BY trade_date DESC LIMIT ?",
        (code, end_date, days),
    )
    rows.reverse()
    return rows


def _fetch_toplist(code: str, end_date: str, days: int = 10) -> list[dict]:
    cutoff = (datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=days)).strftime("%Y-%m-%d")
    return query_all(
        "SELECT trade_date, reason, net_buy_amount "
        "FROM top_list_cache WHERE stock_code=? AND trade_date >= ? AND trade_date <= ? "
        "ORDER BY trade_date DESC",
        (code, cutoff, end_date),
    )


def _fetch_holder_trend(code: str, end_date: str) -> list[dict]:
    return query_all(
        "SELECT end_date, holder_num FROM holder_number_cache "
        "WHERE stock_code=? AND end_date <= ? ORDER BY end_date DESC LIMIT 6",
        (code, end_date),
    )


def _fetch_margin(code: str, end_date: str, days: int = 10) -> list[dict]:
    rows = query_all(
        "SELECT trade_date, rzye, rqye FROM margin_detail_cache "
        "WHERE stock_code=? AND trade_date <= ? "
        "ORDER BY trade_date DESC LIMIT ?",
        (code, end_date, days),
    )
    rows.reverse()
    return rows


def _fetch_reports(code: str, end_date: str, days: int = 10) -> list[dict]:
    cutoff = (datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=days)).strftime("%Y-%m-%d")
    return query_all(
        "SELECT report_date, rating, title FROM reports_cache "
        "WHERE stock_code=? AND report_date >= ? AND report_date <= ? "
        "ORDER BY report_date DESC",
        (code, cutoff, end_date),
    )


def _avg(nums, fallback=0):
    ns = [n for n in nums if n is not None]
    return sum(ns) / len(ns) if ns else fallback


def _sum(nums):
    return sum(n or 0 for n in nums)


# ========== 14 个探测器 ==========
def detect_youduo_chuhuo(code: str, date: str) -> Optional[dict]:
    """诱多出货"""
    kline = _fetch_window(code, date, 30)
    if len(kline) < 20:
        return None
    today = kline[-1]
    ma20 = _avg([k["close"] for k in kline[-20:]])
    vol_avg5 = _avg([k["volume"] for k in kline[-6:-1]])
    signals = {}

    # 价位偏高
    high_pos = today["close"] >= ma20 * 1.12
    signals["price_vs_ma20"] = (today["close"] / ma20 - 1) if ma20 else 0
    # 放量
    vol_ratio = (today["volume"] or 0) / vol_avg5 if vol_avg5 else 0
    signals["volume_ratio_5d"] = vol_ratio
    vol_surge = vol_ratio > 1.5
    # 资金净流出
    mf = _fetch_moneyflow(code, date, 3)
    net_mf = _sum([m["net_mf_amount"] for m in mf])
    signals["net_mf_3d"] = net_mf
    outflow = net_mf < 0
    # 尾盘近极值 (收阴)
    bearish_close = today["close"] < today["open"]
    signals["bearish_candle"] = bearish_close

    score = 0
    if high_pos: score += 0.3
    if vol_surge: score += 0.3
    if outflow: score += 0.25
    if bearish_close: score += 0.15
    if score < 0.5:
        return None
    return {
        "pattern": "诱多出货", "trade_date": date, "confidence": round(score, 2),
        "signals": signals,
        "narrative": f"{date} 收盘 {today['close']:.2f}（偏MA20 {signals['price_vs_ma20']*100:+.1f}%），量比 {vol_ratio:.2f}x，近 3 日资金净流 {net_mf/1e8:+.2f}亿",
    }


def detect_jiatupo(code: str, date: str) -> Optional[dict]:
    """假突破"""
    kline = _fetch_window(code, date, 75)
    if len(kline) < 63:
        return None
    today_idx = len(kline) - 1
    # 找前 60 日最高
    past60_high = max(k["high"] for k in kline[today_idx - 60:today_idx])
    signals = {"past60_high": past60_high, "today_close": kline[today_idx]["close"]}
    # 过去 3 天内是否突破
    broke_day_idx = None
    breakout_price = None
    for i in range(today_idx - 3, today_idx):
        if kline[i]["close"] > past60_high:
            broke_day_idx = i
            breakout_price = kline[i]["close"]
            break
    if broke_day_idx is None:
        return None
    signals["breakout_date"] = kline[broke_day_idx]["trade_date"]
    signals["breakout_price"] = breakout_price
    # 今日是否回落破突破价
    today_close = kline[today_idx]["close"]
    if today_close >= breakout_price:
        return None
    drop_pct = (today_close - breakout_price) / breakout_price
    score = 0.6 + min(0.3, abs(drop_pct) * 10)
    return {
        "pattern": "假突破", "trade_date": date, "confidence": round(score, 2),
        "signals": signals,
        "narrative": f"{kline[broke_day_idx]['trade_date']} 突破 60 日高点 {past60_high:.2f} 到 {breakout_price:.2f}，{date} 回落至 {today_close:.2f}（跌破 {drop_pct*100:.1f}%）",
    }


def detect_lashen_paifa(code: str, date: str) -> Optional[dict]:
    """拉升派发"""
    kline = _fetch_window(code, date, 20)
    if len(kline) < 10:
        return None
    past5 = kline[-5:]
    avg_chg_5 = _avg([k["change_pct"] for k in past5])
    vol_avg20 = _avg([k["volume"] for k in kline[-20:]])
    vol_avg5 = _avg([k["volume"] for k in past5])
    signals = {"avg_change_5d": avg_chg_5, "volume_5d_vs_20d": vol_avg5/vol_avg20 if vol_avg20 else 0}

    if avg_chg_5 < 1.0 or (vol_avg5 / vol_avg20 if vol_avg20 else 0) < 1.2:
        return None

    mf = _fetch_moneyflow(code, date, 5)
    net_mf_sum = _sum([m["net_mf_amount"] for m in mf])
    signals["net_mf_5d"] = net_mf_sum

    holders = _fetch_holder_trend(code, date)
    holder_qoq = 0
    if len(holders) >= 2 and holders[1]["holder_num"]:
        holder_qoq = (holders[0]["holder_num"] - holders[1]["holder_num"]) / holders[1]["holder_num"]
    signals["holder_qoq"] = holder_qoq

    score = 0.25  # base for rising with volume
    if net_mf_sum < 0:
        score += 0.4
    if holder_qoq > 0.03:
        score += 0.35
    if score < 0.5:
        return None
    return {
        "pattern": "拉升派发", "trade_date": date, "confidence": round(score, 2),
        "signals": signals,
        "narrative": f"近 5 日均涨 {avg_chg_5:+.2f}%，量比 {signals['volume_5d_vs_20d']:.2f}x，资金净流 {net_mf_sum/1e8:+.2f}亿，户数环比 {holder_qoq*100:+.1f}%",
    }


def detect_weipan_toushiki(code: str, date: str) -> Optional[dict]:
    """尾盘偷袭 (降级版：用收盘价在当日区间位置)"""
    kline = _fetch_window(code, date, 2)
    if not kline:
        return None
    today = kline[-1]
    rng = (today["high"] or 0) - (today["low"] or 0)
    if rng < 0.01:
        return None
    # 收盘靠近最高 or 最低
    close_pos = (today["close"] - today["low"]) / rng
    signals = {"close_position_in_range": close_pos, "change_pct": today.get("change_pct")}
    if close_pos > 0.92 and (today.get("change_pct") or 0) > 2:
        return {
            "pattern": "尾盘偷袭", "trade_date": date, "confidence": 0.55,
            "signals": signals,
            "narrative": f"{date} 涨 {today['change_pct']:+.2f}%，收盘位于日内高位 {close_pos*100:.0f}%（尾盘拉升嫌疑）",
        }
    if close_pos < 0.08 and (today.get("change_pct") or 0) < -2:
        return {
            "pattern": "尾盘偷袭", "trade_date": date, "confidence": 0.55,
            "signals": signals,
            "narrative": f"{date} 跌 {today['change_pct']:+.2f}%，收盘位于日内低位 {close_pos*100:.0f}%（尾盘砸盘嫌疑）",
        }
    return None


def detect_lihao_chuhuo(code: str, date: str) -> Optional[dict]:
    """借利好出货"""
    reports = _fetch_reports(code, date, 5)
    buy_reports = [r for r in reports if r.get("rating") in ("买入", "增持", "推荐")]
    if not buy_reports:
        return None
    kline = _fetch_window(code, date, 5)
    if len(kline) < 3:
        return None
    past3_change = _sum([k["change_pct"] for k in kline[-3:]])
    signals = {
        "recent_buy_reports": len(buy_reports),
        "past_3d_total_change": past3_change,
        "reports_sample": [f"{r['report_date']} {r['title'][:30] if r.get('title') else ''}" for r in buy_reports[:2]],
    }
    if past3_change > -2:
        return None
    score = 0.55 + min(0.3, abs(past3_change) / 10)
    return {
        "pattern": "借利好出货", "trade_date": date, "confidence": round(score, 2),
        "signals": signals,
        "narrative": f"近 5 日收到 {len(buy_reports)} 份买入级研报，但近 3 日累计 {past3_change:+.2f}%",
    }


def detect_lihuai_xichou(code: str, date: str) -> Optional[dict]:
    """借利空吸筹"""
    kline = _fetch_window(code, date, 10)
    if len(kline) < 5:
        return None
    past3_change = _sum([k["change_pct"] for k in kline[-3:]])
    if past3_change < 2:
        return None
    mf = _fetch_moneyflow(code, date, 3)
    net_mf = _sum([m["net_mf_amount"] for m in mf])
    signals = {"past_3d_change": past3_change, "net_mf_3d": net_mf}
    if net_mf < 0:
        return None
    score = 0.5
    if net_mf > 5e7:
        score += 0.3
    return {
        "pattern": "借利空吸筹", "trade_date": date, "confidence": round(score, 2),
        "signals": signals,
        "narrative": f"近 3 日涨 {past3_change:+.2f}%，资金净流入 {net_mf/1e8:+.2f} 亿",
    }


def detect_xipan(code: str, date: str) -> Optional[dict]:
    """洗盘"""
    kline = _fetch_window(code, date, 20)
    if len(kline) < 15:
        return None
    highs = [k["high"] for k in kline]
    lows = [k["low"] for k in kline]
    max_h = max(highs)
    min_l = min(lows)
    drawdown = (min_l - max_h) / max_h
    signals = {"max_drawdown_20d": drawdown}
    if drawdown > -0.05:
        return None

    vol_avg20 = _avg([k["volume"] for k in kline])
    vol_avg5 = _avg([k["volume"] for k in kline[-5:]])
    signals["volume_5d_vs_20d"] = vol_avg5 / vol_avg20 if vol_avg20 else 0
    if signals["volume_5d_vs_20d"] > 1.0:
        return None

    holders = _fetch_holder_trend(code, date)
    if len(holders) < 2:
        return None
    holder_qoq = (holders[0]["holder_num"] - holders[1]["holder_num"]) / holders[1]["holder_num"] if holders[1]["holder_num"] else 0
    signals["holder_qoq"] = holder_qoq
    if holder_qoq > 0.02:
        return None
    score = 0.5 + min(0.3, abs(holder_qoq) * 5) + min(0.2, abs(drawdown) * 2)
    return {
        "pattern": "洗盘", "trade_date": date, "confidence": round(score, 2),
        "signals": signals,
        "narrative": f"20 日最大回撤 {drawdown*100:.1f}%，5 日均量仅 20 日的 {signals['volume_5d_vs_20d']*100:.0f}%，户数环比 {holder_qoq*100:+.1f}%",
    }


def detect_xichou(code: str, date: str) -> Optional[dict]:
    """吸筹"""
    kline = _fetch_window(code, date, 20)
    if len(kline) < 10:
        return None
    past10 = kline[-10:]
    avg10 = _avg([k["change_pct"] for k in past10])
    vol_avg20 = _avg([k["volume"] for k in kline])
    vol_avg10 = _avg([k["volume"] for k in past10])
    signals = {
        "avg_change_10d": avg10,
        "volume_10d_vs_20d": vol_avg10 / vol_avg20 if vol_avg20 else 0,
    }
    if avg10 > 0:
        return None
    if vol_avg10 / vol_avg20 > 0.9 if vol_avg20 else True:
        return None

    holders = _fetch_holder_trend(code, date)
    if len(holders) < 2:
        return None
    holder_qoq = (holders[0]["holder_num"] - holders[1]["holder_num"]) / holders[1]["holder_num"] if holders[1]["holder_num"] else 0
    signals["holder_qoq"] = holder_qoq
    if holder_qoq > -0.03:
        return None

    mf = _fetch_moneyflow(code, date, 5)
    buy_minus_sell = _sum([m.get("buy_lg_amount") or 0 for m in mf]) - _sum([m.get("sell_lg_amount") or 0 for m in mf])
    signals["big_order_net_5d"] = buy_minus_sell

    score = 0.4 + min(0.25, abs(holder_qoq) * 5)
    if buy_minus_sell > 0:
        score += 0.3
    return {
        "pattern": "吸筹", "trade_date": date, "confidence": round(min(score, 0.95), 2),
        "signals": signals,
        "narrative": f"近 10 日均涨 {avg10:+.2f}%，量能 {signals['volume_10d_vs_20d']*100:.0f}%（缩量），户数环比 {holder_qoq*100:+.1f}%",
    }


def detect_duidao(code: str, date: str) -> Optional[dict]:
    """对倒"""
    kline = _fetch_window(code, date, 20)
    if len(kline) < 15:
        return None
    past5 = kline[-5:]
    vol_avg20 = _avg([k["volume"] for k in kline])
    vol_avg5 = _avg([k["volume"] for k in past5])
    total_change_5 = _sum([k["change_pct"] for k in past5])
    signals = {
        "volume_5d_vs_20d": vol_avg5 / vol_avg20 if vol_avg20 else 0,
        "total_change_5d": total_change_5,
    }
    vr = signals["volume_5d_vs_20d"]
    if vr < 1.3:
        return None
    if abs(total_change_5) > 3:
        return None
    score = 0.5 + min(0.3, (vr - 1.3) / 2)
    return {
        "pattern": "对倒", "trade_date": date, "confidence": round(score, 2),
        "signals": signals,
        "narrative": f"近 5 日放量 {vr:.2f}x 但累计涨幅 {total_change_5:+.2f}%（量价背离）",
    }


def detect_longhu_jieli(code: str, date: str) -> Optional[dict]:
    """龙虎榜接力"""
    tl = _fetch_toplist(code, date, 7)
    if len(tl) < 3:
        return None
    positive = sum(1 for t in tl if (t.get("net_buy_amount") or 0) > 0)
    negative = len(tl) - positive
    direction_consistent = positive >= 3 or negative >= 3
    signals = {"top_list_count_7d": len(tl), "net_positive": positive, "net_negative": negative}
    if not direction_consistent:
        return None
    score = 0.5 + min(0.3, (len(tl) - 3) * 0.1)
    direction = "接力买" if positive >= 3 else "接力卖"
    return {
        "pattern": "龙虎榜接力", "trade_date": date, "confidence": round(score, 2),
        "signals": signals,
        "narrative": f"近 7 日上榜 {len(tl)} 次，{direction}（{max(positive, negative)} 次同向）",
    }


def detect_jigou_baotuan(code: str, date: str) -> Optional[dict]:
    """机构抱团 (以股东户数连降为特征)"""
    holders = _fetch_holder_trend(code, date)
    if len(holders) < 4:
        return None
    # 检查近 3 季度环比都 <0
    qoqs = []
    for i in range(3):
        a = holders[i]["holder_num"]
        b = holders[i + 1]["holder_num"]
        if not b:
            return None
        qoqs.append((a - b) / b)
    all_down = all(q < 0 for q in qoqs)
    if not all_down:
        return None
    total_drop = (holders[0]["holder_num"] - holders[3]["holder_num"]) / holders[3]["holder_num"] if holders[3]["holder_num"] else 0
    signals = {"qoq_3q": qoqs, "total_drop_3q": total_drop}
    if total_drop > -0.15:
        return None
    score = 0.5 + min(0.3, abs(total_drop) * 2)
    return {
        "pattern": "机构抱团", "trade_date": date, "confidence": round(score, 2),
        "signals": signals,
        "narrative": f"近 3 季度股东户数连降，累计 {total_drop*100:.1f}%（筹码高度集中）",
    }


def detect_beixiang_pianxian(code: str, date: str) -> Optional[dict]:
    """北向骗线 (数据不可用)"""
    return None


def detect_dazong_paifa(code: str, date: str) -> Optional[dict]:
    """大宗派发 (数据不可用)"""
    return None


def detect_rongzi_baocang(code: str, date: str) -> Optional[dict]:
    """融资爆仓"""
    margin = _fetch_margin(code, date, 7)
    if len(margin) < 5:
        return None
    latest_rzye = margin[-1].get("rzye") or 0
    old_rzye = margin[0].get("rzye") or 0
    if not old_rzye:
        return None
    rzye_delta = (latest_rzye - old_rzye) / old_rzye
    signals = {"rzye_5d_change": rzye_delta}
    if rzye_delta > -0.05:
        return None

    kline = _fetch_window(code, date, 5)
    past3_change = _sum([k["change_pct"] for k in kline[-3:]])
    signals["past_3d_change"] = past3_change
    if past3_change > -3:
        return None
    vol_avg20 = _avg([k["volume"] for k in kline[-20:]]) if len(kline) >= 20 else _avg([k["volume"] for k in kline])
    vol_avg3 = _avg([k["volume"] for k in kline[-3:]])
    signals["volume_3d_vs_20d"] = vol_avg3 / vol_avg20 if vol_avg20 else 0

    score = 0.5 + min(0.3, abs(rzye_delta) * 3) + min(0.15, abs(past3_change) / 20)
    return {
        "pattern": "融资爆仓", "trade_date": date, "confidence": round(min(score, 0.95), 2),
        "signals": signals,
        "narrative": f"融资余额 5 日 {rzye_delta*100:+.1f}%（大幅减），近 3 日 {past3_change:+.2f}%，放量 {signals['volume_3d_vs_20d']:.2f}x",
    }


# ========== 总探测器 ==========
_ALL_DETECTORS = [
    detect_youduo_chuhuo, detect_jiatupo, detect_lashen_paifa,
    detect_weipan_toushiki, detect_lihao_chuhuo, detect_lihuai_xichou,
    detect_xipan, detect_xichou, detect_duidao,
    detect_longhu_jieli, detect_jigou_baotuan,
    detect_beixiang_pianxian, detect_dazong_paifa, detect_rongzi_baocang,
]


def detect_all_patterns(code: str, date: str) -> list[dict]:
    """对某只股某一天跑完 14 个探测器。"""
    results = []
    for fn in _ALL_DETECTORS:
        try:
            r = fn(code, date)
            if r and r.get("confidence", 0) >= 0.5:
                results.append(r)
        except Exception:
            pass
    results.sort(key=lambda x: x.get("confidence", 0), reverse=True)
    return results


def scan_stock_history(code: str, start_date: str, end_date: str) -> list[dict]:
    """扫一只股一段时间的所有探测命中（按交易日遍历）。"""
    days = query_all(
        "SELECT DISTINCT trade_date FROM daily_quotes "
        "WHERE stock_code=? AND trade_date >= ? AND trade_date <= ? "
        "ORDER BY trade_date",
        (code, start_date, end_date),
    )
    hits = []
    for d in days:
        dt = d["trade_date"]
        r = detect_all_patterns(code, dt)
        hits.extend(r)
    return hits
