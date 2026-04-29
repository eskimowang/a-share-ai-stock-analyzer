"""券商风格学习服务 —— 研究中信证券、中金公司的研报框架。

目标:
  1. Codex 联网采集: 最近 6 个月每家券商在 10 个行业的代表性报告
  2. DeepSeek 分析: 关注角度 / 分析深度 / 典型话术 / 方法论
  3. 产出: 券商 × 行业矩阵的"风格画像"
  4. 未来可用于识别研报背后的"屁股决定脑袋"
"""
import json
import logging
import time
import concurrent.futures
from datetime import datetime

from ..config import CONFIG
from ..db import db, execute, query_all, query_one
from ..ai.local_cli import LocalCLIClient
from ..ai.info_collector import CodexInfoCollector
from ..ai.multi_brain import build_brains_from_config

log = logging.getLogger(__name__)


TOP_INDUSTRIES = [
    "半导体", "新能源汽车", "医药生物", "银行", "食品饮料",
    "机械设备", "电力设备", "国防军工", "房地产", "计算机",
]

BROKERS_TO_STUDY = [
    {"name": "中信证券", "tier": "头部", "note": "综合大所，研究深度标杆"},
    {"name": "中金公司", "tier": "头部", "note": "外资背景，宏观+策略见长"},
]


def _ensure_broker_tables():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS broker_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            broker_name TEXT,
            industry TEXT,
            profile_data TEXT,  -- JSON: {focus_points, depth_score, methodology, typical_phrasing, blind_spots}
            sample_reports TEXT,  -- JSON: [{date,title,analyst,url}]
            analyzed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(broker_name, industry)
        );
        CREATE TABLE IF NOT EXISTS broker_study_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT,
            brokers TEXT,
            industries TEXT,
            summary TEXT,
            duration_seconds REAL,
            error_msg TEXT
        );
        """)


def _collect_reports_via_codex(info: CodexInfoCollector, broker: str, industry: str) -> dict:
    """让 Codex 联网找某家券商在某行业的近期代表性研报。"""
    prompt = f"""【任务】联网搜索 **{broker}** 研究部在 **{industry}** 行业近 6 个月发布的代表性研报。

要求：
1. 至少找 3 份（最多 6 份），优先权威深度报告（非短评）
2. 每份报告记录：日期、标题、首席/团队、核心结论、1-2 句摘要
3. 尽可能给出报告链接或文号（慧博投研 / 券商官网 / wind 研报码）

【输出严格 JSON】
{{
  "broker": "{broker}",
  "industry": "{industry}",
  "collected_date": "YYYY-MM-DD",
  "reports": [
    {{
      "report_date": "YYYY-MM-DD",
      "title": "完整报告标题",
      "analyst_team": "首席/作者",
      "core_thesis": "核心观点一句话",
      "summary": "1-2 句话摘要",
      "source_url": "URL 或文号"
    }}
  ],
  "coverage_note": "如果这家券商在这个行业覆盖度很低/停覆盖，直接说明",
  "info_grade": "A/B/C/D"
}}

如果信息不足，reports 数组可以少于 3 份，但要在 coverage_note 里说明。
"""
    return info._query(prompt, max_tokens=3000)


def _analyze_broker_style(brains, broker: str, industry: str, reports_data: dict) -> dict:
    """DeepSeek 从 Codex 采集的报告里抽出风格画像。"""
    ds = next((b for b in brains if b.name == "DeepSeek"), brains[0])
    reports = reports_data.get("reports", [])
    if not reports:
        return {
            "broker": broker, "industry": industry,
            "focus_points": [], "depth_score": 0,
            "methodology": "数据不足",
            "typical_phrasing": [],
            "blind_spots": [],
            "note": reports_data.get("coverage_note", "无数据"),
        }

    reports_block = "\n\n".join(
        f"### {i+1}. {r.get('report_date')} 《{r.get('title')}》\n"
        f"- 团队: {r.get('analyst_team','—')}\n"
        f"- 核心结论: {r.get('core_thesis','—')}\n"
        f"- 摘要: {r.get('summary','—')}"
        for i, r in enumerate(reports)
    )

    prompt = f"""【任务】从 **{broker}** 在 **{industry}** 行业的 {len(reports)} 份研报摘要里，
提炼出该家券商的**研究风格画像**。

## 研报摘要
{reports_block}

## 输出 JSON
{{
  "broker": "{broker}",
  "industry": "{industry}",
  "focus_points": ["这家券商关注的 3-5 个核心维度（如：全球产能迁移、单位经济模型、政策博弈）"],
  "depth_score": 1-10 数字,
  "depth_reasoning": "为什么打这个分",
  "methodology": "他们惯用的分析框架（自下而上/自上而下/对比估值/DCF/景气度轮动 等）",
  "typical_phrasing": ["3-5 条典型话术样本（识别特征）"],
  "blind_spots": ["他们容易忽视或偏颇的维度"],
  "vs_market_consensus": "与主流共识 相比通常更保守/激进/一致",
  "signals_to_watch": ["当他们做以下动作时，要警觉：[列 2-3 条]"]
}}

严格 JSON，不要加前言。
"""
    try:
        raw = ds.complete(
            "资深金融分析师，擅长从研报摘要里反推券商的研究方法论。",
            prompt, max_tokens=2000, reasoning_effort='high',
        )
        # 抽 JSON
        import re
        m = re.search(r"\{[\s\S]*\}", raw)
        if m:
            return json.loads(m.group(0))
    except Exception as e:
        log.warning(f"{broker} × {industry} 分析失败: {e}")
    return {"broker": broker, "industry": industry, "_error": "解析失败"}


def run_broker_study(brokers=None, industries=None) -> dict:
    """跑一次完整的券商风格学习。"""
    _ensure_broker_tables()
    brokers = brokers or BROKERS_TO_STUDY
    industries = industries or TOP_INDUSTRIES
    start = time.time()
    run_id = execute(
        "INSERT INTO broker_study_runs(status, brokers, industries) VALUES (?,?,?)",
        ("running",
         json.dumps([b["name"] if isinstance(b, dict) else b for b in brokers], ensure_ascii=False),
         json.dumps(industries, ensure_ascii=False)),
    )
    log.info(f"[券商学习] Run #{run_id} 启动, {len(brokers)} × {len(industries)} = {len(brokers)*len(industries)} 格")

    try:
        codex = LocalCLIClient(
            name="Codex", agent="codex",
            endpoint=CONFIG["ai"]["local_cli"]["endpoint"], timeout=300,
        )
        info = CodexInfoCollector(codex)
        brains = build_brains_from_config(CONFIG)

        # Stage 1: 采集（并行，每个 broker × industry 一个任务）
        log.info(f"[券商学习 1/2] Codex 并行采集 {len(brokers)*len(industries)} 格...")
        tasks = [(b if isinstance(b, dict) else {"name": b}, ind)
                 for b in brokers for ind in industries]
        collected = {}

        def _collect_one(task):
            broker, ind = task
            bname = broker["name"]
            try:
                data = _collect_reports_via_codex(info, bname, ind)
                return (bname, ind, data)
            except Exception as e:
                return (bname, ind, {"_error": str(e), "reports": []})

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            for fut in concurrent.futures.as_completed([pool.submit(_collect_one, t) for t in tasks]):
                bname, ind, data = fut.result()
                collected[(bname, ind)] = data

        # Stage 2: 分析（并行，但 DeepSeek 顺序跑避免限流）
        log.info(f"[券商学习 2/2] DeepSeek 抽 {len(collected)} 份风格画像...")
        profiles = {}
        for (bname, ind), data in collected.items():
            profile = _analyze_broker_style(brains, bname, ind, data)
            profiles[(bname, ind)] = profile

            # 保存到 DB
            execute(
                "INSERT OR REPLACE INTO broker_profiles"
                "(broker_name, industry, profile_data, sample_reports) "
                "VALUES (?,?,?,?)",
                (bname, ind,
                 json.dumps(profile, ensure_ascii=False),
                 json.dumps(data.get("reports", []), ensure_ascii=False)),
            )

        # 聚合 summary
        summary_lines = []
        for b in brokers:
            bname = b["name"] if isinstance(b, dict) else b
            summary_lines.append(f"## {bname}")
            for ind in industries:
                p = profiles.get((bname, ind), {})
                if p.get("_error") or not p.get("focus_points"):
                    summary_lines.append(f"- {ind}: 数据不足")
                    continue
                focus = "、".join(p.get("focus_points", [])[:3])
                depth = p.get("depth_score", 0)
                summary_lines.append(f"- {ind}: 深度 {depth}/10，关注 {focus}")
            summary_lines.append("")
        summary_md = "\n".join(summary_lines)

        duration = time.time() - start
        execute(
            "UPDATE broker_study_runs SET status=?, summary=?, duration_seconds=? WHERE id=?",
            ("success", summary_md, duration, run_id),
        )
        log.info(f"[券商学习] Run #{run_id} 完成，耗时 {duration:.0f}s")
        return {
            "status": "success",
            "run_id": run_id,
            "duration_seconds": duration,
            "summary_md": summary_md,
            "profiles_count": len(profiles),
        }

    except Exception as e:
        log.exception(f"[券商学习] Run #{run_id} 失败")
        execute(
            "UPDATE broker_study_runs SET status=?, error_msg=?, duration_seconds=? WHERE id=?",
            ("failed", str(e), time.time() - start, run_id),
        )
        return {"status": "failed", "error": str(e)}


def get_broker_profile(broker: str, industry: str = None) -> list[dict]:
    _ensure_broker_tables()
    if industry:
        rows = query_all(
            "SELECT * FROM broker_profiles WHERE broker_name=? AND industry=?",
            (broker, industry),
        )
    else:
        rows = query_all(
            "SELECT * FROM broker_profiles WHERE broker_name=? ORDER BY industry",
            (broker,),
        )
    for r in rows:
        try:
            r["profile_data"] = json.loads(r["profile_data"]) if r["profile_data"] else {}
        except Exception:
            pass
        try:
            r["sample_reports"] = json.loads(r["sample_reports"]) if r["sample_reports"] else []
        except Exception:
            pass
    return rows


def get_broker_matrix() -> dict:
    """返回 broker × industry 矩阵视图（dashboard 用）。"""
    _ensure_broker_tables()
    rows = query_all(
        "SELECT broker_name, industry, profile_data FROM broker_profiles"
    )
    matrix = {}
    for r in rows:
        try:
            pd = json.loads(r["profile_data"]) if r["profile_data"] else {}
        except Exception:
            pd = {}
        matrix.setdefault(r["broker_name"], {})[r["industry"]] = {
            "depth": pd.get("depth_score"),
            "focus": pd.get("focus_points", [])[:3],
            "methodology": pd.get("methodology", ""),
        }
    return matrix
