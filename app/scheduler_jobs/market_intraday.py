"""Intraday market scheduler jobs.

This module contains the scheduler-facing wrappers for the user's live market
workflow: premarket briefing, midday summary, closing decision, alert scan, and
stop-loss scan.  The underlying analysis and risk logic remains in services.
"""
import logging
from datetime import datetime, timedelta
from typing import Callable, Optional

from ..config import CONFIG
from ..db import query_all, query_one
from ..services.game_memory import (
    save_analysis,
    format_history_for_prompt,
    format_track_record_for_prompt,
)

log = logging.getLogger("scheduler.market_intraday")

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


def _rt_trade_date(rt: dict) -> str:
    ts = str((rt or {}).get("timestamp") or "")
    if len(ts) >= 8 and ts[:8].isdigit():
        return f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"
    return ""


def _rt_time_label(rt: dict) -> str:
    ts = str((rt or {}).get("timestamp") or "")
    if len(ts) >= 14 and ts[:14].isdigit():
        return f"{ts[8:10]}:{ts[10:12]}"
    return "实时"


def _is_realtime_snapshot_fresh(rt: dict, today: str | None = None) -> bool:
    if not rt or rt.get("price") is None or rt.get("change_pct") is None:
        return False
    today = today or datetime.now().strftime("%Y-%m-%d")
    rt_date = _rt_trade_date(rt)
    # Any realtime source with a timestamp must match today's trading date.
    if rt_date and rt_date != today:
        return False
    return True


def _is_continuous_auction_now(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    minutes = now.hour * 60 + now.minute
    return (9 * 60 + 30 <= minutes <= 11 * 60 + 30) or (13 * 60 <= minutes <= 15 * 60)


def _fmt_price(value) -> str:
    try:
        return f"{float(value):.2f}"
    except Exception:
        return "-"


# ========== 国际/政治消息采集 ==========
def _collect_overnight_world_news(positions: list[dict]) -> str:
    """Codex 联网采集昨夜国际 + 政治面大事，并对每只持仓给相关度打分。"""
    from ..config import CONFIG
    from ..ai.local_cli import LocalCLIClient
    from ..ai.info_collector import CodexInfoCollector

    position_summary = ", ".join(
        f"{p['stock_code']} {p['stock_name']}" for p in positions
    )

    try:
        codex = LocalCLIClient(
            name="Codex", agent="codex",
            endpoint=CONFIG["ai"]["local_cli"]["endpoint"], timeout=180,
        )
        info = CodexInfoCollector(codex)
        prompt = f"""【任务】联网搜索最近 12-24 小时内**国际市场 + 政治/政策**大事件，
并**对我的 A 股持仓**评估影响相关度。

## 我的持仓
{position_summary}

## 必查维度
1. **美股昨夜收盘**：三大指数 (道指/标普/纳指) 涨跌幅 + 关键板块 (半导体/军工/医药等)
2. **汇率 + 大宗**：美元/人民币、黄金、原油 WTI/布伦特 关键变化
3. **地缘政治**：中美关系（出口管制/加征关税/对话）、台海、俄乌、中东
4. **国内重磅政策**：昨夜至今早 重要会议、文件、发改委/央行动作
5. **行业特定消息**：比如 半导体国产替代、造船业订单、量子科技政策、存储芯片价格 等（跟持仓相关的）

## 输出 JSON

```json
{{
  "collect_time": "YYYY-MM-DD HH:MM",
  "overnight_events": [
    {{
      "event": "事件一句话",
      "category": "美股/汇率/地缘/政策/行业",
      "impact_direction": "利好/利空/中性",
      "magnitude": "重大/一般/轻微",
      "impact_on_my_positions": [
        {{"code": "股票代码", "name": "中文名", "correlation": "高/中/低", "reason": "为什么相关、影响方向"}}
      ],
      "source": "URL 或来源"
    }}
  ],
  "net_mood_for_a_shares": "今开情绪整体判断（看多/看空/中性）",
  "highest_correlation_event": "对你持仓影响最大的那条事件"
}}
```

要求：
- overnight_events 返回 3-6 条最有信号的
- 不要 overnight_events 之外的填空数据（垃圾不放）
- impact_on_my_positions 只列**真正相关**的持仓代码（不是每条都列全）
- 相关度判断要有理由
"""
        data = info._query(prompt, max_tokens=3500)
        if not isinstance(data, dict):
            return ""

        # 组装 markdown
        md = ""
        mood = data.get("net_mood_for_a_shares", "")
        if mood:
            md += f"**今开情绪**: {mood}\n\n"
        highlight = data.get("highest_correlation_event", "")
        if highlight:
            md += f"**最需关注**: {highlight}\n\n"
        events = data.get("overnight_events", [])
        if events:
            md += "**昨夜重点事件**:\n"
            icon_map = {"利好": "🟢", "利空": "🔴", "中性": "⚪"}
            for e in events[:6]:
                icon = icon_map.get(e.get("impact_direction", ""), "⚪")
                cat = e.get("category", "")
                mag = e.get("magnitude", "")
                md += f"- {icon} [{cat}·{mag}] {e.get('event', '')}\n"
                for imp in (e.get("impact_on_my_positions") or [])[:3]:
                    corr = imp.get("correlation", "低")
                    corr_icon = "⭐⭐⭐" if corr == "高" else ("⭐⭐" if corr == "中" else "⭐")
                    nm = imp.get('name', '') or ''
                    md += f"  - {corr_icon} **{imp.get('code', '')} {nm}**: {imp.get('reason', '')[:70]}\n"
        return md
    except Exception as e:
        log.warning(f"国际消息采集失败: {e}")
        return ""


# ========== 任务 1：开盘前瞻 (09:00) —— 14 手法探测驱动 ==========
def job_premarket():
    """每个工作日 09:00 开盘前推送 —— 基于昨日收盘 + 14 类手法量化探测 + 国际面消息生成操作建议。"""
    log.info("[开盘前瞻] 开始")
    try:
        from ..services.pattern_detector import detect_all_patterns, PATTERNS
        from ..services.analyze_service import _brains

        positions = query_all("""
            SELECT p.stock_code, p.stock_name,
            SUM(CASE WHEN t.trade_type='buy' THEN t.quantity ELSE -t.quantity END) as qty,
            SUM(CASE WHEN t.trade_type='buy' THEN t.price*t.quantity+t.fee ELSE -t.price*t.quantity+t.fee END) as cost
            FROM positions p JOIN trades t ON p.id=t.position_id
            WHERE p.status='holding' GROUP BY p.id
        """)
        if not positions:
            log.info("[开盘前瞻] 无持仓，跳过")
            return

        # 找"最新有数据的日期"作为探测基准（通常是上个交易日）
        latest_row = query_one(
            "SELECT MAX(trade_date) as d FROM daily_quotes"
        )
        latest_date = latest_row["d"] if latest_row else datetime.now().strftime("%Y-%m-%d")

        # 每只持仓跑探测 + 整理数据块
        blocks = []
        alert_count = 0
        for p in positions:
            code = p["stock_code"]
            name = p["stock_name"]
            qty = p["qty"] or 0
            avg = p["cost"] / qty if qty else 0

            snap = query_one(
                "SELECT trade_date, close, change_pct, volume FROM daily_quotes "
                "WHERE stock_code=? ORDER BY trade_date DESC LIMIT 1", (code,)
            ) or {}
            mf = query_one(
                "SELECT net_mf_amount FROM moneyflow_cache "
                "WHERE stock_code=? ORDER BY trade_date DESC LIMIT 1", (code,)
            ) or {}
            cur = snap.get("close") or 0
            chg = snap.get("change_pct") or 0
            mfv = (mf.get("net_mf_amount") or 0) / 10000  # 万元 → 亿
            pl = (cur - avg) / avg * 100 if avg else 0

            # 14 手法探测
            hits = detect_all_patterns(code, latest_date)
            hits_top = hits[:3] if hits else []
            if hits_top:
                alert_count += 1

            block = (
                f"## {code} {name}  昨收 {cur:.2f} {chg:+.2f}%  浮盈 {pl:+.2f}%\n"
                f"资金净流 {mfv:+.2f}亿 / {qty}股 / 成本 {avg:.2f}\n"
            )
            if hits_top:
                block += "\n**探测器识别**：\n"
                for h in hits_top:
                    meta = PATTERNS.get(h['pattern'], {})
                    icon = meta.get('icon', '⚪')
                    block += f"- {icon} **{h['pattern']}** 置信 {int(h['confidence']*100)}% — {h['narrative']}\n"
            else:
                block += "\n（无异常手法识别）\n"
            blocks.append(block)

        # 国际 + 政治面消息（并行采）
        log.info("[开盘前瞻] 采集国际 + 政治面...")
        world_news_md = _collect_overnight_world_news(positions)

        # 让 DeepSeek 基于探测结果 + 国际消息给具体操作建议
        data_section = "\n\n".join(blocks)
        world_block = f"\n\n# 🌍 国际/政治面（昨夜至今晨）\n\n{world_news_md}\n" if world_news_md else ""
        prompt = f"""【09:00 开盘前瞻】基于昨日 ({latest_date}) 收盘数据 + 14 类 A 股手法量化探测器 + 昨夜国际/政治面，
对每只持仓给**今日开盘具体操作建议**。

{data_section}
{world_block}

---

# 输出格式（严格）

## 📊 今日大势定调
一句话（<60 字，综合技术信号 + 昨夜国际面情绪）

## 🌍 国际/政治面潜在驱动
列出 2-3 条今日最需关注的国际/政治事件 + 对哪只持仓相关度最高（⭐⭐⭐/⭐⭐/⭐）
引用持仓时写"代码 + 中文名"，如 "688981 中芯国际"（不要只写代码）

## 🎯 每只持仓操作建议

对每只输出（简洁表格）:

| 代码 名称 | 昨日信号 | 外因相关度 | 今日策略 |
|---|---|---|---|
| {{code}} {{stock_name}} | {{关键手法 + 置信}} | {{相关度 + 外因方向}} | 具体动作：冲到 XX 元卖 1/3 / 跌到 XX 不追 / 持有不动 |

**重要**: 第一列必须同时写代码和中文名，例如 "600150 中国船舶"（不要只写代码）。

## ⚠️ 今日最大风险
2-3 句话（技术 + 外因叠加看，哪个陷阱要避开）

## ✅ 必做 1 件事
如果没有就写"今天没急事"

**要求**：
- 数字必须具体（XX 元、XX 股、XX 亿）
- 空方手法 (🔴) 叠加资金流出 → 明确建议减仓
- 多方手法 (🟢) 叠加资金流入 → 可持有或加仓
- 诱多出货 / 拉升派发 命中 → 不论涨跌，建议兑现部分盈利
- 字数 400 内，markdown 格式
"""
        ds = next((b for b in _brains if b.name == "DeepSeek"), _brains[0])
        brief = ds.complete(
            "资深 A 股操盘手，擅长用量化信号给具体可执行的开盘操作建议，用大白话。",
            prompt, max_tokens=1500, reasoning_effort='high',
        )

        title = f"09:00 开盘前瞻 · {alert_count} 只命中手法"
        _push("🟡", title, brief, short=f"{alert_count} 只异动信号 · {brief[:60]}")
        log.info(f"[开盘前瞻] 推送完成，{alert_count}/{len(positions)} 只命中")
    except Exception as e:
        log.exception(f"开盘前瞻失败: {e}")


# ========== 任务 2：午盘小结 (11:30) ==========
def job_midday():
    log.info("[午盘小结] 开始")
    try:
        from ..data_sources import UnifiedDataSource
        from ..config import CONFIG

        today = datetime.now().strftime("%Y-%m-%d")
        ds = UnifiedDataSource(tushare_token=CONFIG["data_sources"]["tushare"].get("token"))
        positions = query_all("""
            SELECT p.stock_code, p.stock_name,
            SUM(CASE WHEN t.trade_type='buy' THEN t.quantity ELSE -t.quantity END) as qty
            FROM positions p JOIN trades t ON p.id=t.position_id
            WHERE p.status='holding' GROUP BY p.id
        """)
        lines = []
        has_alert = False
        realtime_count = 0
        stale_count = 0

        for p in positions:
            code = p["stock_code"]
            name = p["stock_name"]
            rt = {}
            try:
                rt = ds.get_realtime(code, wait_for_rate_limit=True) or {}
            except Exception as e:
                log.warning("[午盘小结] realtime fail %s: %s", code, e)

            if _is_realtime_snapshot_fresh(rt, today=today):
                realtime_count += 1
                pct = float(rt.get("change_pct") or 0)
                price = rt.get("price")
                icon = "🟢" if pct > 1 else ("🔴" if pct < -1 else "⚪")
                time_label = _rt_time_label(rt)
                lines.append(
                    f"{icon} {code} {name}: {pct:+.2f}%"
                    f"（现价 {_fmt_price(price)}，{time_label}）"
                )
                if abs(pct) > 3:
                    has_alert = True
                continue

            stale_count += 1
            snap = query_one(
                "SELECT trade_date, close, change_pct FROM daily_quotes WHERE stock_code=? "
                "ORDER BY trade_date DESC LIMIT 1", (code,)
            ) or {}
            pct = snap.get("change_pct") or 0
            td = str(snap.get("trade_date") or "")[:10]
            lines.append(
                f"⚪ {code} {name}: 实时失败"
                f"（昨收 {_fmt_price(snap.get('close'))}，{td} {float(pct or 0):+.2f}%）"
            )

        content = (
            "## 午盘小结 11:30（实时快照）\n\n"
            + "\n".join(lines)
            + "\n\n"
            + f"数据说明：实时 {realtime_count} 只；实时失败/过期 {stale_count} 只。"
            + "\n只有当**当日实时涨跌幅**超过 ±3% 时才升级推送。"
        )

        _push("🟡" if has_alert else "🔵", "午盘小结", content, short="午盘实时持仓状态")
        log.info("[午盘小结] 完成 realtime=%s stale=%s", realtime_count, stale_count)
    except Exception as e:
        log.exception(f"午盘小结失败: {e}")


# ========== 任务 3：尾盘决策 (14:45) ==========
def job_closing():
    log.info("[尾盘决策] 开始")
    from ..services.analyze_service import _brains
    from ..ai.prompts import SYSTEM_PROMPT, SYSTEM_PROMPT_ADVERSARY
    try:
        import concurrent.futures
        positions = query_all("""
            SELECT p.stock_code, p.stock_name,
            SUM(CASE WHEN t.trade_type='buy' THEN t.quantity ELSE -t.quantity END) as qty,
            SUM(CASE WHEN t.trade_type='buy' THEN t.price*t.quantity+t.fee ELSE -t.price*t.quantity+t.fee END) as cost
            FROM positions p JOIN trades t ON p.id=t.position_id
            WHERE p.status='holding' GROUP BY p.id
        """)
        if not positions:
            return

        # 为每只持仓准备深度数据包（K线 + 成交量 + 资金流 + 龙虎榜）
        detail_blocks = []
        for p in positions:
            code = p["stock_code"]
            avg = p["cost"] / p["qty"] if p["qty"] else 0

            # 近 10 日 K 线 + 成交量
            kline = query_all(
                "SELECT trade_date, open, high, low, close, volume, change_pct "
                "FROM daily_quotes WHERE stock_code=? ORDER BY trade_date DESC LIMIT 10",
                (code,),
            )
            kline.reverse()
            kline_rows = "\n".join(
                f"  {k['trade_date']}  开{k['open']:.2f}/高{k['high']:.2f}/低{k['low']:.2f}/收{k['close']:.2f}  量{int(k['volume'] or 0):>10,}  ({k.get('change_pct', 0):+.2f}%)"
                for k in kline
            )

            # 近 5 日资金流向（大单/中单/小单净）
            mf = query_all(
                "SELECT trade_date, net_mf_amount, buy_lg_amount, sell_lg_amount, net_d5_amount "
                "FROM moneyflow_cache WHERE stock_code=? ORDER BY trade_date DESC LIMIT 5",
                (code,),
            )
            mf_rows = "\n".join(
                f"  {m['trade_date']}  净流入 {m['net_mf_amount']}  大单买 {m['buy_lg_amount']} / 卖 {m['sell_lg_amount']}  5日净 {m['net_d5_amount']}"
                for m in mf
            ) or "  （无资金流数据）"

            # 龙虎榜（近 5 日）
            tl = query_all(
                "SELECT trade_date, reason, net_buy_amount FROM top_list_cache "
                "WHERE stock_code=? ORDER BY trade_date DESC LIMIT 5",
                (code,),
            )
            tl_rows = "\n".join(
                f"  {t['trade_date']}  {t['reason']}  净买入 {t['net_buy_amount']}"
                for t in tl
            ) or "  （近期未上龙虎榜）"

            # 融资融券变化
            margin = query_all(
                "SELECT trade_date, rzye, rqye FROM margin_detail_cache "
                "WHERE stock_code=? ORDER BY trade_date DESC LIMIT 5",
                (code,),
            )
            margin_rows = ""
            if margin and len(margin) >= 2:
                latest = margin[0]
                old = margin[-1]
                rzye_change = ((latest.get("rzye") or 0) - (old.get("rzye") or 0)) / 1e8
                margin_rows = f"  融资余额近 5 日变化: {rzye_change:+.2f} 亿（升=散户加杠杆，降=去杠杆）"
            else:
                margin_rows = "  （无融资融券数据）"

            # 股东户数
            holder = query_all(
                "SELECT end_date, holder_num FROM holder_number_cache "
                "WHERE stock_code=? ORDER BY end_date DESC LIMIT 4",
                (code,),
            )
            holder_rows = ""
            if holder and len(holder) >= 2:
                latest_h = holder[0]["holder_num"] or 0
                old_h = holder[-1]["holder_num"] or 0
                if old_h:
                    chg = (latest_h - old_h) / old_h * 100
                    holder_rows = f"  近 3 季度股东户数从 {old_h:,} → {latest_h:,} ({chg:+.1f}%)。降=筹码集中（主力吸筹），升=筹码分散"

            current_close = kline[-1]["close"] if kline else 0
            detail_blocks.append(
                f"\n## {code} {p['stock_name']}\n"
                f"- 持仓 {p['qty']} 股，成本 {avg:.2f}，现价 {current_close:.2f}，浮盈 {(current_close-avg)/avg*100:+.2f}%\n"
                f"\n### 近 10 日 K 线（A股红涨绿跌）\n```\n{kline_rows}\n```\n"
                f"\n### 近 5 日资金流向\n```\n{mf_rows}\n```\n"
                f"\n### 龙虎榜（近 5 日）\n```\n{tl_rows}\n```\n"
                f"\n### 融资融券\n{margin_rows}\n"
                f"\n### 股东户数\n{holder_rows}\n"
            )

        # 【量化探测器】对每只持仓跑 14 类手法量化检测，结果喂给 AI 作为客观对照
        detector_blocks = []
        try:
            from ..services.pattern_detector import detect_all_patterns
            from ..services.playbook_service import save_detections
            today = datetime.now().strftime("%Y-%m-%d")
            for p in positions:
                code = p["stock_code"]
                hits = detect_all_patterns(code, today)
                if hits:
                    save_detections(hits, code, p["stock_name"])
                    lines = [f"- **{h['pattern']}** (置信 {h['confidence']*100:.0f}%): {h['narrative']}"
                             for h in hits[:3]]
                    detector_blocks.append(f"### {code} {p['stock_name']}\n" + "\n".join(lines))
        except Exception as e:
            log.warning(f"量化探测器异常: {e}")
        detector_section = (
            "\n\n# 🔬 量化探测器识别（今日独立于主观判断）\n\n"
            + "\n\n".join(detector_blocks)
            + "\n\n**重要**: 上面是规则量化识别的结果，请对照你的主观判断。"
              "两者吻合 → 信号加强；不一致 → 给出你的解释。\n\n"
        ) if detector_blocks else ""

        # 历史博弈记忆（供 AI 参考）
        history_blocks = []
        for p in positions:
            h = format_history_for_prompt(p["stock_code"], days=14)
            if "无历史博弈记录" not in h:
                history_blocks.append(f"### {p['stock_code']} {p['stock_name']}\n{h}")
        history_section = (
            "\n\n# 📚 历史博弈记忆（你过去判断对错）\n\n"
            + "\n\n".join(history_blocks)
            + "\n\n" + format_track_record_for_prompt()
            + "\n\n**请参考上面历史判断，避免重复之前的错误。**\n\n"
            if history_blocks else ""
        )

        # 【天】宏观背景（从最近指数行情拉 + 让 AI 补充政策/事件）
        macro_snippet = ""
        try:
            indices = ["000001.SH", "399001.SZ", "399006.SZ"]
            # 简单拉指数近 5 日（Tushare 应该有 index_daily，这里用简易版）
            idx_rows = []
            from ..data_sources import UnifiedDataSource
            ds2 = UnifiedDataSource(tushare_token=CONFIG["data_sources"]["tushare"].get("token"))
            for ix in indices:
                try:
                    idx_df = ds2.tushare.pro.index_daily(
                        ts_code=ix, start_date=(datetime.now() - timedelta(days=8)).strftime("%Y%m%d"),
                    )
                    if idx_df is not None and not idx_df.empty:
                        latest = idx_df.iloc[0]
                        idx_rows.append(
                            f"- {ix}: 最新 {latest.get('close',0):.2f}，"
                            f"今日 {latest.get('pct_chg',0):+.2f}%"
                        )
                except Exception:
                    pass
            if idx_rows:
                macro_snippet = "\n".join(idx_rows)
        except Exception as e:
            log.warning(f"拉宏观失败: {e}")

        prompt = (
            "# 【尾盘 14:45 深度揭露式博弈分析】\n\n"
            "距 A 股收盘 15 分钟，T+1 下今日最后操作窗口。\n\n"
            "## 你的使命\n\n"
            "不是给普通的'看多看空'，而是**揭露**：\n"
            "1. **天**：大势在哪个位置，风格在谁手里，政策背景\n"
            "2. **地**：具体利益集团（主力/游资/机构/北向/私募/公募/散户）**具体在干什么**，用了哪些**操作手法**\n"
            "3. 让用户看懂套路，不再被割\n\n"
            "## 【天】今日大势数据\n"
            f"{macro_snippet or '（指数数据未拉到）'}\n\n"
            "## 【地】A 股常见操作手法清单（你必须对照识别）\n\n"
            "| 操作手法 | 特征 | 受害者 |\n"
            "| --- | --- | --- |\n"
            "| 诱多出货 | 高位放量拉升吸引散户接盘，主力同步减仓 | 追高散户 |\n"
            "| 假突破 | 突破关键位但不持续 | 突破买入者 |\n"
            "| 拉升派发 | 边涨边派，成交量放大但涨幅有限 | 趋势跟随者 |\n"
            "| 尾盘偷袭 | 最后 5 分钟异常拉高/砸盘 | 尾盘追单者 |\n"
            "| 借利好出货 | 业绩/政策利好发布后反而下跌 | 听消息买入者 |\n"
            "| 借利空吸筹 | 利空消息后不跌反涨 | 恐慌卖出者 |\n"
            "| 洗盘 | 震荡下跌但筹码集中度未减（股东户数降）| 不坚定散户 |\n"
            "| 吸筹 | 缩量下跌 + 股东户数减少 + 大单承接 | —— |\n"
            "| 对倒 | 同一主力自买自卖制造活跃 | 技术派 |\n"
            "| 龙虎榜接力 | 游资连续上榜维持人气 | 跟风游资 |\n"
            "| 机构抱团 | 多家公募季度末配置相同股票 | —— |\n"
            "| 北向骗线 | 通道假装外资买入，实为量化/内资 | 北向跟随者 |\n"
            "| 大宗派发 | 大股东折价大宗出货给关联方 | 公开市场买入者 |\n"
            "| 融资爆仓 | 融资盘临界，下跌触发强平踩踏 | 杠杆多头 |\n\n"
            "**每只持仓必须对照清单，明确说出：'今天是 XX 手法，具体从 K线/资金流/龙虎榜/筹码上怎么看出来'。**\n\n"
            + detector_section
            + history_section
            + "\n---\n\n## 【数据包】每只持仓的详细数据\n"
            + "".join(detail_blocks) +
            "\n\n---\n\n"
            "# 输出要求（揭露式深度报告）\n\n"
            "## 开头 · 【天】大势定调（200 字内）\n\n"
            "- 指数位置（偏高/偏低/关键位）+ 今日风格在谁手里（大盘/中小盘/成长/价值）\n"
            "- 流动性/政策/情绪的三个关键背景变量\n"
            "- 这个大势对**我们这组持仓**的影响是顺风还是逆风\n\n"
            "## 每只持仓 · 【地】利益集团操作揭露\n\n"
            "对每只按下面结构（给我揭露感，不要模糊）：\n\n"
            "### {股票名} {代码} · 基本面速写（1-2 句）\n"
            "公司做什么 + 当前驱动逻辑 + 最近的政策/催化/利空事件\n\n"
            "### 各方到底在干什么（具体操作揭露）\n\n"
            "**主力**：今天他在做 [吸筹/拉升派发/诱多/尾盘偷袭/对倒/借利好出货 其中之一]。\n"
            "- 证据 1：YYYY-MM-DD 放了 X 亿成交，但股价只涨了 Y%（=拉升派发的典型特征）\n"
            "- 证据 2：大单净流入 -X 亿，说明他表面在拉，实际在卖\n\n"
            "**游资**：今天 [追涨接力/刚上榜就撤/封板偷袭/未出现]。\n"
            "- 证据：龙虎榜上的 XX 营业部，近 5 日净买入 X 亿\n\n"
            "**机构 / 北向**：[季末抱团/趁震荡吸筹/悄悄减持/暂时观望]。\n"
            "- 证据：...\n\n"
            "**散户**：[融资加杠杆追涨/恐慌被洗/不明方向]。\n"
            "- 证据：融资余额变化 + 股东户数变化\n\n"
            "### 今日定性（对照操作手法清单，给唯一标签）\n"
            "🔴 诱多出货 / 🔴 假突破 / 🟡 洗盘 / 🟡 拉高派发 / 🟢 吸筹 / 🟢 主升浪 / 正常震荡\n\n"
            "### 大白话告诉用户怎么做\n"
            "- 立即: 卖多少股 / 买多少 / 不动 + 具体价位\n"
            "- 明早开盘盯什么（上面提到的几方里的哪一股力量最关键）\n\n"
            "---\n\n"
            "## 尾部 · 组合全景\n"
            "- 今日谁在你持仓里扮演了最关键的角色\n"
            "- 明日 1 个核心不确定点\n"
            "- 一句话总结（大白话）\n\n"
            "**要揭露感、要细节、要数据证据。字数不限。**"
        )

        # 用 4 家并行（含反方）
        def _one(c):
            sys = SYSTEM_PROMPT_ADVERSARY if c.name == "Claude" else SYSTEM_PROMPT
            try:
                return c.name, c.complete(sys, prompt, max_tokens=1000)
            except Exception as e:
                return c.name, f"[失败] {e}"

        opinions = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(_brains)) as pool:
            for f in concurrent.futures.as_completed([pool.submit(_one, b) for b in _brains]):
                n, t = f.result()
                opinions[n] = t

        # 仲裁（按胜率加权 + 保留深度 + 突出博弈 + 割韭菜 + 大白话操作建议）
        ds = next((b for b in _brains if b.name == "DeepSeek"), _brains[0])
        joined = "\n\n".join(f"### {n}\n{t}" for n, t in opinions.items())
        track_record = format_track_record_for_prompt()
        final = ds.complete(
            "A 股博弈专家，整合 4 家观点输出深度报告，保留所有数据证据和博弈分析。"
            "按历史胜率加权：胜率高的 AI 在其擅长判断类型上权重更大，"
            "胜率 <40% 的 AI 在该类判断上降权为反方参考。",
            f"{track_record}\n\n"
            f"今日尾盘 4 家 AI 的深度分析：\n\n{joined}\n\n"
            "# 整合输出（深度 Markdown 报告）\n\n"
            "对每只持仓输出：\n\n"
            "## {股票} {代码}\n\n"
            "**一句话定性**: 属于哪种局面（吸筹/出货/诱多/洗盘/假突破/正常震荡）\n\n"
            "**多方博弈共识**（4 家都认同的）: ...\n\n"
            "**分歧点**: 哪家看法最独特，为什么\n\n"
            "**关键证据**（从提供的 K 线/资金流/龙虎榜/筹码数据里挑出 3-5 条）\n"
            "- YYYY-MM-DD 的 K 线上 X\n"
            "- 资金流：大单净流入 Y 亿\n"
            "- 股东户数从 A 变 B（...）\n\n"
            "**大白话操作**（避开术语）: 立即动作 + 具体股数 + 价格\n\n"
            "**今晚过夜风险**: 日常语言说清楚\n\n"
            "---\n\n"
            "最后写：\n## 🎯 组合总结\n- 今日大主线\n- 明日三个关注点\n\n"
            "保持深度、数据证据、博弈分析，不要简化成短摘要。",
            max_tokens=4000, reasoning_effort='high',
        )

        # 存博弈记忆（每家 AI + 仲裁结果）
        for p in positions:
            code = p["stock_code"]
            name = p["stock_name"]
            try:
                # 每家 AI 的意见单独存（便于后续按 AI 统计胜率）
                for ai_tag, text in opinions.items():
                    # 提取这家 AI 对这只股票的部分（粗略：搜索股票代码所在段）
                    if code in text:
                        # 找到 code 附近 2000 字
                        idx = text.find(code)
                        segment = text[max(0, idx-200):idx+2000]
                    else:
                        segment = text[:3000]
                    snap = next((k for k in query_all(
                        "SELECT close FROM daily_quotes WHERE stock_code=? ORDER BY trade_date DESC LIMIT 1",
                        (code,)
                    )), {})
                    save_analysis(
                        stock_code=code, stock_name=name,
                        ai_source=ai_tag.replace(" [反方]", ""),
                        raw_analysis=segment,
                        price_at_analysis=snap.get("close"),
                        predicted_timeframe=7,
                    )
                # 仲裁结果另存
                save_analysis(
                    stock_code=code, stock_name=name,
                    ai_source="仲裁(DeepSeek)",
                    raw_analysis=final,
                    predicted_timeframe=7,
                )
            except Exception as e:
                log.warning(f"存博弈记忆 {code} 失败: {e}")

        # 从仲裁文本抽订单指令 + 纸交易登记
        try:
            from ..services.order_service import (
                extract_orders_from_text, save_orders,
                simulate_paper_trades, format_orders_for_wechat,
            )
            orders = extract_orders_from_text(final, source="closing")
            if orders:
                save_orders(orders)
                simulate_paper_trades(orders)
                order_md = format_orders_for_wechat(orders)
                final = final + "\n\n" + order_md
                log.info(f"[尾盘决策] 抽出 {len(orders)} 条订单指令")
        except Exception as e:
            log.warning(f"订单抽取失败: {e}")

        _push("🟡", "尾盘决策", final, short="尾盘 15 分钟决策窗口")
        log.info("[尾盘决策] 推送完成 + 博弈记忆已写入")
    except Exception as e:
        log.exception(f"尾盘决策失败: {e}")


# ========== 任务 4：异动扫描（每 10 分钟）==========
def _fallback_alert_advice(code: str, name: str, pct: float, rt: dict, position: dict) -> str:
    qty = int(position.get("qty") or 0)
    cost = (position.get("cost", 0) or 0) / qty if qty else 0
    cur = float(rt.get("price") or 0)
    if not cur or not qty:
        return "实时数据不足，暂停操作，收盘后再复核。"
    lots = max(100, ((qty // 3) // 100) * 100)
    lots = min(lots, qty)
    pl = ((cur - cost) / cost * 100) if cost else 0
    if pct >= 5:
        if pl >= 8:
            return f"不追高；若回落跌破{cur*0.98:.2f}，先卖{lots}股锁利，收盘复核。"
        return f"先不加仓；站稳{cur:.2f}再看，跌回{cur*0.97:.2f}先减{lots}股。"
    if pct <= -5:
        return f"不补仓；若跌破{cur*0.98:.2f}，先卖{lots}股控风险，尾盘再评估。"
    return f"未达强异动阈值，围绕{cur:.2f}观察，收盘后再决策。"


def _generate_alert_advice(code: str, name: str, pct: float, rt: dict,
                             position: dict) -> str:
    """为异动股生成 1-2 句操作建议（DeepSeek 快速版）"""
    from ..services.analyze_service import _brains
    from ..services.pattern_detector import detect_all_patterns

    # 昨日手法
    try:
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        hits = detect_all_patterns(code, yesterday)[:2]
    except Exception:
        hits = []
    hits_str = "；".join(f"{h['pattern']}{int(h['confidence']*100)}%" for h in hits) or "无特殊手法"

    qty = position.get("qty", 0) or 0
    cost = (position.get("cost", 0) or 0) / qty if qty else 0
    cur = rt.get("price") or 0
    pl = ((cur - cost) / cost * 100) if cost else 0

    prompt = (
        f"持仓: {code} {name}  成本{cost:.2f}  现价{cur:.2f}  涨跌{pct:+.2f}%  浮盈{pl:+.2f}%\n"
        f"昨日手法探测: {hits_str}\n"
        f"持仓 {qty} 股\n\n"
        "用 1-2 句话给操作建议，必须包含**具体价格/股数**。格式如 "
        "\"立即 XX 元卖 YY 股\" 或 \"持有，跌到 XX 元止损\"。"
        "不超过 60 字。"
    )

    try:
        ds = next((b for b in _brains if b.name == "DeepSeek"), _brains[0])
        return ds.complete(
            "你是 A 股操盘手，用极简大白话给建议，必须带具体数字。",
            prompt, max_tokens=150, reasoning_effort='high',
        ).strip()
    except Exception as e:
        log.warning(f"advice fail for {code}: {e}")
        return _fallback_alert_advice(code, name, pct, rt, position)


def job_alert_scan():
    """扫描持仓异动：仅使用当日连续竞价实时快照，避免盘前/陈旧日线误报。"""
    try:
        from ..data_sources import UnifiedDataSource
        from ..config import CONFIG

        if not _is_continuous_auction_now():
            log.info("[异动扫描] 非连续竞价时段，跳过")
            return

        today = datetime.now().strftime("%Y-%m-%d")
        positions = query_all("""
            SELECT p.stock_code, p.stock_name,
            SUM(CASE WHEN t.trade_type='buy' THEN t.quantity ELSE -t.quantity END) as qty,
            SUM(CASE WHEN t.trade_type='buy' THEN t.price*t.quantity+t.fee ELSE -t.price*t.quantity+t.fee END) as cost
            FROM positions p JOIN trades t ON p.id=t.position_id
            WHERE p.status='holding' GROUP BY p.id
        """)
        if not positions:
            return

        ds = UnifiedDataSource(tushare_token=CONFIG["data_sources"]["tushare"].get("token"))

        alerts = []
        for p in positions:
            code = p["stock_code"]
            name = p["stock_name"]

            already = query_one(
                "SELECT id FROM notifications WHERE title LIKE '%异动%' "
                "AND content LIKE ? AND date(sent_at) = date('now')",
                (f"%{code}%",),
            )
            if already:
                continue

            try:
                rt_data = ds.get_realtime(code, wait_for_rate_limit=True) or {}
            except Exception as e:
                log.warning("[异动扫描] realtime fail %s: %s", code, e)
                continue

            if not _is_realtime_snapshot_fresh(rt_data, today=today):
                log.info("[异动扫描] 跳过非当日实时快照 %s rt=%s", code, rt_data)
                continue

            pct = float(rt_data.get("change_pct") or 0)
            if abs(pct) < 5:
                continue

            advice = _generate_alert_advice(code, name, pct, rt_data, p)
            cur_price = rt_data.get("price", 0)
            alerts.append(
                f"🔴 **{code} {name}** 异动 {pct:+.2f}%"
                f"（现价 {_fmt_price(cur_price)}，{_rt_time_label(rt_data)}）\n"
                f"💡 {advice}"
            )

        if alerts:
            md = "\n\n".join(alerts)
            _push("🔴", "持仓异动预警 · 带建议", md, short=f"{len(alerts)} 只实时异动 · 附操作建议")
    except Exception as e:
        log.exception(f"异动扫描失败: {e}")


def job_stop_loss_scan():
    """每工作日 10:30 + 14:30 扫持仓止损规则，触发即推。"""
    log.info("[止损扫描] 开始")
    try:
        from ..services.risk_management import check_stop_loss_for_all_positions
        triggers = check_stop_loss_for_all_positions()
        if not triggers:
            log.info("[止损扫描] 无触发")
            return
        md_lines = ["## 🚨 止损规则触发\n"]
        for t in triggers:
            rule_cn = {"hard_stop_8pct": "硬止损 -8%",
                       "trailing_12pct": "跟踪止损 -12%"}.get(t["rule"], t["rule"])
            md_lines.append(
                f"### 🔴 {t['stock_code']} {t['stock_name']}  ·  {rule_cn}\n"
                f"- 成本 {t['entry_price']}  现价 {t['current_price']}  "
                f"浮亏 {t['drawdown_pct']:+.2f}%\n"
                f"- 峰值 {t['peak_price']}  从峰回撤 {t['peak_drawdown_pct']:+.2f}%\n"
                f"- **{t['action']}**\n"
            )
        _push("🔴", f"止损触发 · {len(triggers)} 只", "\n".join(md_lines),
              short=f"{len(triggers)} 只触发止损")
    except Exception as e:
        log.exception(f"止损扫描失败: {e}")


