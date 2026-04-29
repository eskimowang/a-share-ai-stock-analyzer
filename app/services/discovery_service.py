"""荐股发现服务 —— 从无到有的全自动分析。

流程（AI 议会决议版）:
  Stage 1: Codex 联网收集宏观大势
  Stage 2: 多理论研判，AI 自主选出 5-8 个行业
  Stage 3: 每个行业按市值/基本面筛 2-3 只龙头
  Stage 4: 4 家 AI 并行分析每只候选（对抗性仲裁）
  Stage 5: 输出"驱动 × 赔率" 3×3 矩阵
  Stage 6: 保存 + 推送微信

运行:
  - 每月 1/15 日 08:30 自动
  - 手动触发: curl -X POST /api/discovery/run
"""
import json
import logging
import time
import concurrent.futures
from datetime import datetime

from ..db import db, query_all, query_one, execute
from ..data_sources import UnifiedDataSource
from ..ai.local_cli import LocalCLIClient
from ..ai.info_collector import CodexInfoCollector
from ..ai.multi_brain import build_brains_from_config
from ..ai.prompts import SYSTEM_PROMPT, SYSTEM_PROMPT_ADVERSARY
from ..config import CONFIG
from .stock_universe import filter_tradable

log = logging.getLogger(__name__)


def _ensure_discovery_tables():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS discovery_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT,  -- running / success / failed
            macro_analysis TEXT,
            industries_picked TEXT,
            candidates TEXT,
            matrix TEXT,
            duration_seconds REAL,
            error_msg TEXT
        );
        CREATE TABLE IF NOT EXISTS discovery_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER,
            stock_code TEXT,
            stock_name TEXT,
            industry TEXT,
            theory_support TEXT,
            driver_type TEXT,     -- 政策/业绩/资金/主题
            payoff_level TEXT,    -- 低/中/高 赔率
            time_horizon TEXT,    -- 短/中/长
            confidence TEXT,      -- 高/中/低
            core_logic TEXT,
            key_risk TEXT,
            analysis_detail TEXT,
            FOREIGN KEY (run_id) REFERENCES discovery_runs(id)
        );
        """)


def _get_tools():
    ds = UnifiedDataSource(tushare_token=CONFIG["data_sources"]["tushare"].get("token"))
    codex = LocalCLIClient(
        name="Codex", agent="codex",
        endpoint=CONFIG["ai"]["local_cli"]["endpoint"], timeout=300,
    )
    info = CodexInfoCollector(codex)
    brains = build_brains_from_config(CONFIG)
    return ds, info, brains


def _stage_macro(info: CodexInfoCollector) -> dict:
    """Stage 1: Codex 采集宏观大势"""
    log.info("[荐股 1/6] Codex 采集宏观大势...")
    macro = info.collect_macro_info()
    return macro


def _stage_industries(info: CodexInfoCollector, macro: dict) -> dict:
    """Stage 2: 多理论研判行业 (8 个候选)"""
    log.info("[荐股 2/6] Codex 多理论研判 8 个行业...")
    return info.judge_industries_multi_theory(top_n=8)


def _stage_candidates(ds: UnifiedDataSource, industries_data: dict) -> list[dict]:
    """Stage 3: 每个入选行业按市值 + 基本面筛 2 只龙头"""
    log.info("[荐股 3/6] Tushare 按行业筛龙头...")
    picks = industries_data.get("synthesized_picks", []) if isinstance(industries_data, dict) else []
    candidates = []

    # 如果 Codex 已给出 leading_stocks，直接用（快）
    for p in picks[:8]:
        industry = p.get("industry", "")
        leaders = p.get("leading_stocks", [])[:2]  # 每行业取 2 只
        for code in leaders:
            code = str(code).strip().replace(".SH", "").replace(".SZ", "")
            if not code or not code.isdigit():
                continue
            candidates.append({
                "code": code,
                "industry": industry,
                "driver_type": p.get("driver_type", ""),
                "payoff_ratio": p.get("payoff_ratio", ""),
                "time_horizon": p.get("time_horizon", ""),
                "theory_support": p.get("theories_supporting", []),
                "core_logic": p.get("core_logic", ""),
            })

    log.info(f"  候选: {len(candidates)} 只")
    # 过滤不可交易股（ST/退市/停牌）
    tradable_codes = set(filter_tradable([c["code"] for c in candidates]))
    candidates = [c for c in candidates if c["code"] in tradable_codes]
    log.info(f"  过滤后可交易: {len(candidates)} 只")
    return candidates[:16]


def _enrich_candidate_data(ds: UnifiedDataSource, code: str) -> dict:
    """拉一只候选股票的精简数据包"""
    try:
        # 基本信息
        basics = ds.tushare.get_basics(code) if ds.tushare else {}
        # 当前估值
        db_basic = ds.tushare.get_daily_basic(code) if ds.tushare else None
        snap = db_basic.iloc[0].to_dict() if db_basic is not None and not db_basic.empty else {}
        # 财务指标
        fi = ds.tushare.get_fina_indicator(code) if ds.tushare else None
        fi_row = fi.iloc[0].to_dict() if fi is not None and not fi.empty else {}
        # 从 DB 读日线（今晚会全市场采集，但可能这只还没入库，兜底）
        daily = query_all(
            "SELECT trade_date, open, high, low, close, volume, change_pct "
            "FROM daily_quotes WHERE stock_code=? ORDER BY trade_date DESC LIMIT 30",
            (code,),
        )
        daily.reverse()
        if not daily:
            # 实时拉
            df, _ = ds.get_daily(code, start="20260301")
            if df is not None and not df.empty:
                daily = df.tail(30).to_dict(orient="records")

        return {
            "code": code,
            "name": basics.get("name", ""),
            "industry": basics.get("industry", ""),
            "close": snap.get("close"),
            "pe_ttm": snap.get("pe_ttm"),
            "pb": snap.get("pb"),
            "total_mv_yi": (snap.get("total_mv") or 0) / 1e4,
            "roe": fi_row.get("roe"),
            "gross_margin": fi_row.get("grossprofit_margin"),
            "net_margin": fi_row.get("netprofit_margin"),
            "daily": daily,
        }
    except Exception as e:
        log.warning(f"  {code} 数据拉取失败: {e}")
        return {"code": code, "_error": str(e)}


def _stage_analyze(brains, candidates_enriched: list[dict]) -> dict:
    """Stage 4: 让 4 家 AI 对候选池整体打分（用一次调用批量分析，省时间）"""
    log.info(f"[荐股 4/6] 4 家 AI 并行对候选池打分...")

    # 构造候选简表
    lines = []
    for c in candidates_enriched:
        if c.get("_error"):
            continue
        kline_summary = ""
        if c.get("daily"):
            recent = c["daily"][-5:] if len(c["daily"]) >= 5 else c["daily"]
            kline_summary = " → ".join(f"{r['close']:.2f}" for r in recent)
        lines.append(
            f"- {c.get('code')} {c.get('name')} ({c.get('industry')}): "
            f"收盘 {c.get('close')} / PE {c.get('pe_ttm')} / PB {c.get('pb')} / "
            f"ROE {c.get('roe')}% / 近 5 日走势 {kline_summary}"
        )
    candidates_md = "\n".join(lines)

    prompt = f"""【荐股矩阵任务】从候选池中筛选并按"驱动类型 × 赔率空间"分类。

## 候选池（已通过多理论初筛）
{candidates_md}

## 你的任务

1. **不要全部入选**。候选有 {len(candidates_enriched)} 只，最终应落到 6-12 只（每个格子 1-3 只）。
2. 按**驱动类型 × 赔率**的 3×3 矩阵分类:

|  | 低赔率(<1:2) | 中赔率(1:2~1:4) | 高赔率(>1:4) |
|---|---|---|---|
| **政策驱动** | 跟风观望 | 主线跟随 | 主题龙头 |
| **业绩驱动** | 价值持有 | 预期差买入 | 困境反转 |
| **资金驱动** | 回避 | 短线跟随 | 龙头接力 |

3. 每个入选标的**必须有至少 2 个理论框架支持**（估值分位 / 行业生命周期 / 景气拐点 / 政策脉冲 / 筹码 / 戴维斯双击 / capex 周期 / 事件驱动）。

## 输出 JSON

```json
{{
  "market_stance": "牛市初期/震荡上行/震荡下行/熊市 的短评（50字）",
  "matrix": {{
    "政策驱动": {{
      "低赔率": [{{"code":"","name":"","logic":"","time_tag":"短/中/长","crowding":"低/中/高","key_risk":""}}],
      "中赔率": [...],
      "高赔率": [...]
    }},
    "业绩驱动": {{...}},
    "资金驱动": {{...}}
  }},
  "top_3_picks": ["最强的 3 只（不分格子，综合最佳）"],
  "warnings": ["需警示的拥挤度 / 政策风险 / 过热信号"],
  "summary": "3 句话研判综述"
}}
```

**要求**: 严格 JSON 输出。每只入选都要有 core_logic + key_risk。
"""

    def _one(client):
        is_adv = client.name == "Claude"
        sys = SYSTEM_PROMPT_ADVERSARY if is_adv else SYSTEM_PROMPT
        t = time.time()
        try:
            out = client.complete(sys, prompt, max_tokens=4000)
            return client.name, out, time.time() - t
        except Exception as e:
            return client.name, f"[失败] {e}", 0

    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(brains)) as pool:
        for f in concurrent.futures.as_completed([pool.submit(_one, b) for b in brains]):
            n, t, d = f.result()
            results[n] = {"text": t, "duration": d}
    return results


def _stage_arbitrate(brains, opinions: dict, candidates_enriched: list) -> dict:
    """Stage 5: DeepSeek 仲裁（按胜率加权），输出最终矩阵"""
    log.info("[荐股 5/6] DeepSeek 仲裁（胜率加权）...")
    from .game_memory import format_track_record_for_prompt
    track_record = format_track_record_for_prompt()
    try:
        from .recommendation_memory_service import format_recommendation_memory_for_prompt
        recommendation_memory = format_recommendation_memory_for_prompt(limit=12)
    except Exception as e:
        log.warning("读取推荐来源记忆失败: %s", e)
        recommendation_memory = "## 推荐来源记忆\n暂无可用。"

    ds = next((b for b in brains if b.name == "DeepSeek"), brains[0])
    joined = "\n\n".join(
        f"## 【{n}】\n{v['text']}" for n, v in opinions.items()
    )
    final_prompt = (
        f"{track_record}\n\n"
        f"{recommendation_memory}\n\n"
        "**仲裁规则（重要）**:\n"
        "- 按胜率加权整合：历史胜率高的 AI 在其擅长判断类型上权重更大\n"
        "- 任一 AI 在某类判断上胜率 <40%，请将其意见降权至反方参考\n"
        "- 胜率 ≥70% 的 AI 在其擅长判断上意见应作为主要依据\n"
        "- 胜率库为空时，各 AI 等权处理\n\n"
        f"4 家 AI 对 {len(candidates_enriched)} 只候选的矩阵分类：\n\n"
        f"{joined}\n\n"
        "整合出最终「驱动 × 赔率」3×3 矩阵，JSON 格式：\n"
        "```json\n"
        '{\n'
        '  "market_stance": "...",\n'
        '  "matrix": {"政策驱动":{"低赔率":[],"中赔率":[],"高赔率":[]},\n'
        '             "业绩驱动":{...}, "资金驱动":{...}},\n'
        '  "top_3_picks": [{"code":"","name":"","why":""}],\n'
        '  "warnings": [],\n'
        '  "summary": "..."\n'
        '}\n```\n\n'
        "要求：\n"
        "- 每个格子 0-3 只（可以空）\n"
        "- 每只必须有 code+name+logic+time_tag+key_risk\n"
        "- 避免重复入选同一只到多个格子\n"
        "- 严格 JSON"
    )
    result = ds.complete(
        "资深 A 股研究员，整合多 AI 输出最终矩阵",
        final_prompt,
        max_tokens=4000, reasoning_effort='high',
    )
    # 抽 JSON
    import re
    m = re.search(r"\{[\s\S]*\}", result)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception as e:
            log.warning(f"仲裁 JSON 解析失败: {e}")
    return {"_raw": result, "matrix": {}, "top_3_picks": [], "warnings": [], "summary": result[:500]}


def _stage_save(run_id: int, macro: dict, industries: dict,
                 candidates: list, opinions: dict, final_matrix: dict, duration: float):
    """Stage 6a: 保存到数据库"""
    log.info("[荐股 6/6] 保存 + 生成 markdown...")
    execute(
        "UPDATE discovery_runs SET status=?, macro_analysis=?, industries_picked=?, "
        "candidates=?, matrix=?, duration_seconds=? WHERE id=?",
        (
            "success",
            json.dumps(macro, ensure_ascii=False, default=str)[:10000],
            json.dumps(industries, ensure_ascii=False, default=str)[:10000],
            json.dumps(candidates, ensure_ascii=False, default=str),
            json.dumps(final_matrix, ensure_ascii=False, default=str),
            duration,
            run_id,
        ),
    )
    # 展开到 discovery_candidates 表
    recommendation_items = []
    for driver, buckets in final_matrix.get("matrix", {}).items():
        for payoff_label, items in buckets.items():
            for item in items if isinstance(items, list) else []:
                try:
                    execute(
                        "INSERT INTO discovery_candidates"
                        "(run_id, stock_code, stock_name, driver_type, payoff_level, "
                        " time_horizon, core_logic, key_risk, analysis_detail) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            run_id,
                            str(item.get("code", "")),
                            str(item.get("name", "")),
                            driver, payoff_label,
                            str(item.get("time_tag", "")),
                            str(item.get("logic", ""))[:1000],
                            str(item.get("key_risk", ""))[:1000],
                            json.dumps(item, ensure_ascii=False, default=str),
                        ),
                    )
                    recommendation_items.append({
                        "code": str(item.get("code", "")),
                        "name": str(item.get("name", "")),
                        "industry": driver,
                        "reason": str(item.get("logic", ""))[:600],
                        "rank": len(recommendation_items) + 1,
                        "driver_type": driver,
                        "payoff_level": payoff_label,
                        "time_tag": str(item.get("time_tag", "")),
                    })
                except Exception as e:
                    log.warning(f"保存候选失败: {e}")
    if recommendation_items:
        try:
            from .recommendation_memory_service import record_recommendation_batch
            record_recommendation_batch(
                source_key="discovery_matrix",
                source_name="半月度荐股矩阵",
                source_type="ai_discovery",
                items=recommendation_items,
                batch_id=f"discovery_runs:{run_id}",
                recommendation_date=datetime.now().strftime("%Y-%m-%d"),
                default_horizon_days=60,
                context={
                    "run_id": run_id,
                    "market_stance": final_matrix.get("market_stance", ""),
                    "summary": final_matrix.get("summary", ""),
                },
            )
        except Exception as e:
            log.warning("荐股矩阵推荐记忆失败: %s", e)


def _generate_report_md(final_matrix: dict) -> str:
    """Stage 6b: 生成精美 Markdown"""
    md = "# 🎯 半月度荐股矩阵\n\n"
    md += f"## 大势定调\n\n{final_matrix.get('market_stance', '')}\n\n"
    md += f"## 综述\n\n{final_matrix.get('summary', '')}\n\n"
    md += "## 矩阵（驱动 × 赔率）\n\n"
    matrix = final_matrix.get("matrix", {})
    for driver in ("政策驱动", "业绩驱动", "资金驱动"):
        buckets = matrix.get(driver, {})
        if not any(buckets.get(k) for k in buckets):
            continue
        md += f"### {driver}\n\n"
        for payoff in ("低赔率", "中赔率", "高赔率"):
            items = buckets.get(payoff, [])
            if not items:
                continue
            md += f"**{payoff}**:\n"
            for it in (items if isinstance(items, list) else []):
                md += (
                    f"- **{it.get('code','')} {it.get('name','')}** "
                    f"[{it.get('time_tag','')}]: {it.get('logic','')}\n"
                    f"  ⚠️ 风险: {it.get('key_risk','')}\n"
                )
            md += "\n"
    top3 = final_matrix.get("top_3_picks", [])
    if top3:
        md += "## ⭐ Top 3 精选\n\n"
        for t in top3:
            md += f"- **{t.get('code','')} {t.get('name','')}**: {t.get('why','')}\n"
    warnings = final_matrix.get("warnings", [])
    if warnings:
        md += "\n## ⚠️ 警示\n\n"
        for w in warnings:
            md += f"- {w}\n"
    return md


# ========== 主入口 ==========
def run_discovery_full() -> dict:
    """完整流程，返回最终结果字典"""
    _ensure_discovery_tables()
    start = time.time()
    run_id = execute("INSERT INTO discovery_runs(status) VALUES (?)", ("running",))
    log.info(f"[荐股] Run #{run_id} 启动")

    try:
        ds, info, brains = _get_tools()
        macro = _stage_macro(info)
        industries = _stage_industries(info, macro)
        candidates = _stage_candidates(ds, industries)

        if not candidates:
            execute(
                "UPDATE discovery_runs SET status=?, error_msg=?, duration_seconds=? WHERE id=?",
                ("failed", "候选池为空", time.time() - start, run_id),
            )
            return {"status": "failed", "error": "候选池为空"}

        # 拉数据（并行，每只独立）
        log.info(f"[荐股] 拉 {len(candidates)} 只候选的数据...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            enriched = list(pool.map(lambda c: _enrich_candidate_data(ds, c["code"]), candidates))

        opinions = _stage_analyze(brains, enriched)
        final_matrix = _stage_arbitrate(brains, opinions, enriched)
        duration = time.time() - start

        _stage_save(run_id, macro, industries, candidates, opinions, final_matrix, duration)

        report_md = _generate_report_md(final_matrix)
        log.info(f"[荐股] 完成 Run #{run_id}，耗时 {duration:.0f}s")
        return {
            "status": "success",
            "run_id": run_id,
            "duration_seconds": duration,
            "report_md": report_md,
            "matrix": final_matrix,
        }

    except Exception as e:
        log.exception(f"[荐股] Run #{run_id} 失败")
        execute(
            "UPDATE discovery_runs SET status=?, error_msg=?, duration_seconds=? WHERE id=?",
            ("failed", str(e), time.time() - start, run_id),
        )
        return {"status": "failed", "error": str(e)}


def get_latest_matrix() -> dict | None:
    _ensure_discovery_tables()
    row = query_one(
        "SELECT * FROM discovery_runs WHERE status='success' "
        "ORDER BY run_at DESC LIMIT 1"
    )
    if not row:
        return None
    try:
        return {
            "run_at": row["run_at"],
            "duration_seconds": row["duration_seconds"],
            "matrix": json.loads(row["matrix"]) if row["matrix"] else {},
            "candidates": query_all(
                "SELECT * FROM discovery_candidates WHERE run_id=?", (row["id"],)
            ),
        }
    except Exception as e:
        log.warning(f"读取最新矩阵失败: {e}")
        return None
