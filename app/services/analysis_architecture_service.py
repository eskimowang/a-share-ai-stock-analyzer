"""分析系统架构规则库。

把用户前面确认的系统建设思路做成可查询、可注入 prompt 的结构化架构：
多源数据 -> 蒸馏存储 -> 互动/持仓/荐股记忆 -> 多 AI 分工 -> 风控/PK/复盘 -> 反馈再分析。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from ..db import db, query_all, query_one


SOURCE_BATCH = "analysis_architecture_20260428"


DEFAULT_COMPONENTS = [
    {
        "component_key": "profit_decision_feedback_loop",
        "layer": "盈利闭环层",
        "title": "分析-决策-行动-反馈-再分析闭环",
        "design_goal": "把系统从观点输出升级为以盈利为目标的闭环机器：每个判断都要能行动、反馈、修正。",
        "operating_rule": "任何建议必须经过五步：分析证据、形成决策、记录行动、跟踪反馈、再分析修正；长期以收益、回撤、胜率、盈亏比和纪律执行度评估。",
        "data_inputs": "多源数据, AI观点, 持仓, 订单, 成交, 反馈事件, 回测结果, AI PK结果",
        "outputs": "decision_feedback_snapshot, action_review, profit_loss_learning, next_iteration_rules",
        "cadence": "每次分析和交易后记录；每日/每周复盘；月度总结规则有效性。",
        "storage_policy": "只保存关键判断、动作、结果、收益回撤和复盘结论，不保存冗余推理。",
        "owner_ai": "全部 AI + 裁判",
        "applies_to": "chat,analysis,decision,action,feedback,review,pk,risk",
        "priority": 110,
    },
    {
        "component_key": "full_source_data_layer",
        "layer": "数据层",
        "title": "全量多源数据优先",
        "design_goal": "让 AI 判断建立在 Tushare、AkShare、Baostock、本地缓存、研报、持仓和互动记忆的交叉验证上。",
        "operating_rule": "任何股价、资金、财务、研报、龙虎榜、融资融券、股东户数等结论，都优先读本地缓存和工具；缺数据就标记缺口，不编造。",
        "data_inputs": "Tushare, AkShare, Baostock, 本地行情缓存, 持仓, 自选, 聊天互动, 外部荐股",
        "outputs": "stock_deep_profile, market_context, source_api_status, analysis_data_package",
        "cadence": "行情交易日更新；深度画像按重点池/个股触发；接口状态随采集记录。",
        "storage_policy": "保留结构化缓存，原始大文件低频保存或不保存，优先保存蒸馏结果。",
        "owner_ai": "全部 AI",
        "applies_to": "chat,analysis,pk,review,data",
        "priority": 100,
    },
    {
        "component_key": "distilled_storage_layer",
        "layer": "存储层",
        "title": "硬盘有限，保存蒸馏后的判断资产",
        "design_goal": "避免把图片、长研报、网页原文无限堆积，把它们压缩为可复用信号。",
        "operating_rule": "大图、长文本、重复行情只做短期缓存；长期保存摘要、因子、证据链、评分、命中率和来源索引。",
        "data_inputs": "截图, 研报原文, 行情明细, 复盘结果",
        "outputs": "规则库, 研报信号, 作者评分, 推荐来源表现, 周期标签",
        "cadence": "采集时即时蒸馏；月度清理低价值原始材料。",
        "storage_policy": "raw 少存，distilled 长存，评分和证据可追溯。",
        "owner_ai": "Codex",
        "applies_to": "data,report,recommendation,review",
        "priority": 98,
    },
    {
        "component_key": "premium_report_pipeline",
        "layer": "研报层",
        "title": "Tushare 付费研报进入加工流水线",
        "design_goal": "把研报从“读观点”升级为“提取预期、评级、目标价、作者/团队历史质量”。",
        "operating_rule": "中金和中信证券权重最高；其他券商以蒸馏、覆盖数量、分歧度和行业热度统计为主。",
        "data_inputs": "Tushare 研报, reports_cache, broker_study, premium_broker_reports",
        "outputs": "研报摘要, 一致预期, 评级变化, 目标价分布, 重点券商信号",
        "cadence": "重点池每周/月度；单股分析时可按需刷新。",
        "storage_policy": "优先保存标题、日期、券商、作者、评级、预测、摘要和信号，不长期堆大段原文。",
        "owner_ai": "Gemini",
        "applies_to": "report,analysis,chat,pk",
        "priority": 97,
    },
    {
        "component_key": "research_author_backtest",
        "layer": "学习层",
        "title": "券商、作者、团队命中率反测",
        "design_goal": "形成研报质量分级，不让低质量观点污染决策。",
        "operating_rule": "对历史研报按 5/20/60 交易日收益、行业相对收益、最大回撤和方向一致性反测；作者/团队持续打分。",
        "data_inputs": "历史研报, 个股行情, 行业走势, 作者/团队字段",
        "outputs": "broker_score, author_score, team_score, horizon_performance",
        "cadence": "月度刷新；研报批量加工后补刷新。",
        "storage_policy": "保存评分、样本数、胜率、平均超额收益；低样本只作参考。",
        "owner_ai": "Gemini",
        "applies_to": "report,analysis,recommendation",
        "priority": 96,
    },
    {
        "component_key": "interaction_stock_memory",
        "layer": "记忆层",
        "title": "互动过的股票进入持续跟踪池",
        "design_goal": "凡是你和系统讨论过的股票，都成为可复盘、可追踪、可提醒的对象。",
        "operating_rule": "聊天中识别股票代码/名称后进入互动股票池，定期刷新行情、研报、资金、风险和上次结论偏差。",
        "data_inputs": "chat_messages, stock_universe, daily_quotes, reports_cache",
        "outputs": "interaction_stock_pool, latest_interaction_analysis, deviation_from_last_view",
        "cadence": "日度/手动触发；重点股票可提高频率。",
        "storage_policy": "保存互动摘要、最后观点、后续走势和复盘结论。",
        "owner_ai": "DeepSeek",
        "applies_to": "chat,tracking,analysis,review",
        "priority": 95,
    },
    {
        "component_key": "holding_change_upload",
        "layer": "持仓层",
        "title": "持仓变动支持图片上传和人工校验",
        "design_goal": "让你用截图或文字把真实持仓变动传给系统，系统只在确认后写入真实持仓。",
        "operating_rule": "截图不带代码时，先用股票名称匹配代码，再让用户确认；涉及买卖写入必须走确认和审计。",
        "data_inputs": "持仓截图, 手工录入, positions, trades",
        "outputs": "holding_changes, positions, orders, risk_alerts",
        "cadence": "按上传/交易后即时处理。",
        "storage_policy": "图片可短期留存，长期保存结构化交易记录和识别置信度。",
        "owner_ai": "Codex",
        "applies_to": "holding,chat,risk,analysis",
        "priority": 94,
    },
    {
        "component_key": "recommendation_memory_learning",
        "layer": "荐股学习层",
        "title": "热股月采和外部荐股进入学习机制",
        "design_goal": "外部推荐不是直接买，而是形成来源记忆、后验表现和可复用经验。",
        "operating_rule": "记录来源、推荐理由、时间、价格区间、后续 5/20/60 日收益和最大回撤；以后分析时自动引用历史表现。",
        "data_inputs": "热股月采, 荐股矩阵, 外部推荐, daily_quotes",
        "outputs": "recommendation_memory, source_performance, stock_recommendation_history",
        "cadence": "收到推荐即录入；每周/月度反测更新。",
        "storage_policy": "保存来源、推荐摘要和表现，不保存重复大段材料。",
        "owner_ai": "DeepSeek",
        "applies_to": "recommendation,chat,analysis",
        "priority": 93,
    },
    {
        "component_key": "biweekly_playbook_review",
        "layer": "复盘层",
        "title": "14 类操作手法隔周全市场复盘",
        "design_goal": "把 14 类手法从个股案例扩展到全市场扫描，沉淀市场风格和主力行为样本。",
        "operating_rule": "每隔一周扫描市场股票，识别诱多出货、假突破、洗盘、吸筹、龙虎榜接力等手法，并统计后验收益。",
        "data_inputs": "daily_quotes, moneyflow_cache, top_list_cache, stock_universe",
        "outputs": "market_weekly_playbook_run, detected_patterns, outcome_stats",
        "cadence": "隔周复盘；也保留手动触发。",
        "storage_policy": "保存命中标签、证据链和后验表现，避免保存全量无用中间图。",
        "owner_ai": "Claude",
        "applies_to": "playbook,review,analysis,pk",
        "priority": 92,
    },
    {
        "component_key": "four_ai_differentiated_jury",
        "layer": "AI 分工层",
        "title": "四个 AI 分工，不读同一份答案",
        "design_goal": "降低同源污染，让不同模型从不同侧面提出独立判断。",
        "operating_rule": "DeepSeek 看基本面/估值/仲裁，Gemini 看研报/机构，Claude 看技术/博弈/反方，Codex 看政策/事件/系统结构。",
        "data_inputs": "financials, reports, daily_quotes, moneyflow, top_list, policy/event context",
        "outputs": "independent_opinions, adversary_warning, consensus",
        "cadence": "单股分析、持仓策略、PK 调仓时触发。",
        "storage_policy": "保存各 AI 观点和仲裁结果，便于以后反测谁更准。",
        "owner_ai": "全部 AI",
        "applies_to": "analysis,chat,pk",
        "priority": 91,
    },
    {
        "component_key": "realistic_simulation_friction",
        "layer": "模拟真实性层",
        "title": "涨跌停、流动性冲击和基准对照",
        "design_goal": "让AI PK更接近真实交易，而不是理想化纸面成交。",
        "operating_rule": "AI PK必须包含固定指数基金基准、反共识者；交易执行加入涨停买不到、跌停卖不出、大单冲击成本。",
        "data_inputs": "daily_quotes, amount, volume, ai_pk_trades, ai_pk_positions",
        "outputs": "impact_adjusted_trade, limit_blocked_order, index_fund_baseline_return, contrarian_return",
        "cadence": "每次AI PK调仓和盘中模拟交易。",
        "storage_policy": "保存执行价格、冲击成本说明、裁判审核结果和基准表现。",
        "owner_ai": "裁判 + AI PK",
        "applies_to": "pk,risk,feedback,review",
        "priority": 109,
    },
    {
        "component_key": "scheduler_job_split_plan",
        "layer": "调度治理层",
        "title": "调度任务分组，逐步拆出 scheduler/jobs",
        "design_goal": "降低scheduler.py集中度，让数据更新、AI PK、推送、长期跟踪、券商报告等任务独立维护。",
        "operating_rule": "先引入任务分组注册表，再按职责逐个迁移job实现；任何一次迁移都必须保持任务ID和触发时间不变。",
        "data_inputs": "APScheduler jobs, scheduler_jobs.registry",
        "outputs": "job_group_map, safer_scheduler_refactor",
        "cadence": "系统维护时逐步拆分。",
        "storage_policy": "只保存注册表和任务元数据，不增加运行时负担。",
        "owner_ai": "Codex",
        "applies_to": "scheduler,ops,maintenance",
        "priority": 85,
    },
    {
        "component_key": "ai_pk_realistic_simulation",
        "layer": "PK 层",
        "title": "4 个 AI 各 100 万模拟盘独立交易",
        "design_goal": "用真实 A 股约束检验 AI 选股、仓位和风控，不允许复制你的持仓。",
        "operating_rule": "每个 AI 独立候选池、独立策略、独立持仓；执行 100 股整数、T+1、现金约束、交易费用、涨跌停和无杠杆。",
        "data_inputs": "stock_universe, daily_quotes, realtime quote, ai_pk_accounts",
        "outputs": "ai_pk_dashboard, trades, positions, daily_snapshots, strategy_review",
        "cadence": "交易日每日运行，盘中可实时模拟调仓。",
        "storage_policy": "保存交易流水、每日净值、仓位、策略文字和证据。",
        "owner_ai": "全部 AI",
        "applies_to": "pk,dashboard,review",
        "priority": 90,
    },
    {
        "component_key": "decision_pipeline",
        "layer": "决策层",
        "title": "候选、证据、风险、仓位、执行五步走",
        "design_goal": "所有建议最后都落到可执行但可审计的清单。",
        "operating_rule": "先入候选池，再补证据链，再过风险/仓位，再形成买卖/等待建议；真实交易写入必须确认。",
        "data_inputs": "候选池, 深度画像, 研报信号, 交易认知规则, 风控状态",
        "outputs": "action_table, position_plan, stop_loss_condition, order_suggestion",
        "cadence": "每次聊天、分析、复盘、调仓。",
        "storage_policy": "保存结论和关键证据，不保存无意义推理草稿。",
        "owner_ai": "DeepSeek",
        "applies_to": "chat,analysis,risk,pk",
        "priority": 89,
    },
    {
        "component_key": "risk_position_center",
        "layer": "风控层",
        "title": "风控和仓位是最终闸门",
        "design_goal": "防止因为看好、研报乐观或 AI 共识而跳过本金安全。",
        "operating_rule": "仓位上限、现金储备、-5%软警戒、硬止损、主题集中度和单股风险必须先于买入冲动。",
        "data_inputs": "positions, trades, current quote, cognition rules, ai_pk positions",
        "outputs": "risk_alerts, allocation_plan, stop_loss_review",
        "cadence": "盘前、盘中、收盘、持仓变动后。",
        "storage_policy": "保存风险事件、触发条件和处理结果。",
        "owner_ai": "Claude",
        "applies_to": "risk,holding,pk,chat",
        "priority": 88,
    },
    {
        "component_key": "audit_and_permissions",
        "layer": "安全层",
        "title": "只读、写入、破坏操作分级审计",
        "design_goal": "AI 可以查数据，但不能悄悄改真实持仓或删除记录。",
        "operating_rule": "只读工具直接执行；写入工具需要确认；破坏性操作需要更强确认；所有工具调用进审计表。",
        "data_inputs": "tool_calls, user_confirmations",
        "outputs": "tool_audit, confirmation_required",
        "cadence": "每次工具调用。",
        "storage_policy": "审计日志长期保存。",
        "owner_ai": "Codex",
        "applies_to": "chat,tool,holding,risk",
        "priority": 87,
    },
    {
        "component_key": "ai_hedge_fund_reference",
        "layer": "外部架构借鉴",
        "title": "吸收 ai-hedge-fund 的多代理团队思想",
        "design_goal": "参考开源 ai-hedge-fund 的 analyst/risk manager/portfolio manager 分层，把观点、风险和组合决策拆开。",
        "operating_rule": "不照搬名人投资人代理；保留“基本面、情绪/机构、技术、估值、风险经理、组合经理”的岗位分离，用 A 股数据和你的交易认知重写。",
        "data_inputs": "开源多代理架构思想, 本系统四 AI 观点, 风控和组合规则",
        "outputs": "role_separation, risk_gate, portfolio_decision",
        "cadence": "架构层长期约束。",
        "storage_policy": "只保存可执行规则，不保存外部项目代码。",
        "owner_ai": "Codex",
        "applies_to": "architecture,pk,analysis,chat",
        "priority": 86,
    },
]


def _ensure_tables() -> None:
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS analysis_architecture_components (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            component_key TEXT UNIQUE NOT NULL,
            layer TEXT,
            title TEXT NOT NULL,
            design_goal TEXT,
            operating_rule TEXT,
            data_inputs TEXT,
            outputs TEXT,
            cadence TEXT,
            storage_policy TEXT,
            owner_ai TEXT,
            applies_to TEXT,
            priority INTEGER DEFAULT 50,
            enabled INTEGER DEFAULT 1,
            source_batch TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_analysis_architecture_layer
          ON analysis_architecture_components(layer, priority DESC);
        CREATE INDEX IF NOT EXISTS idx_analysis_architecture_enabled
          ON analysis_architecture_components(enabled, priority DESC);
        """)


def seed_analysis_architecture(overwrite: bool = True) -> dict:
    _ensure_tables()
    inserted = 0
    updated = 0
    with db() as c:
        for item in DEFAULT_COMPONENTS:
            exists = c.execute(
                "SELECT id FROM analysis_architecture_components WHERE component_key=?",
                (item["component_key"],),
            ).fetchone()
            if exists and not overwrite:
                continue
            c.execute(
                """
                INSERT INTO analysis_architecture_components
                (component_key, layer, title, design_goal, operating_rule, data_inputs,
                 outputs, cadence, storage_policy, owner_ai, applies_to, priority,
                 enabled, source_batch)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(component_key) DO UPDATE SET
                  layer=excluded.layer,
                  title=excluded.title,
                  design_goal=excluded.design_goal,
                  operating_rule=excluded.operating_rule,
                  data_inputs=excluded.data_inputs,
                  outputs=excluded.outputs,
                  cadence=excluded.cadence,
                  storage_policy=excluded.storage_policy,
                  owner_ai=excluded.owner_ai,
                  applies_to=excluded.applies_to,
                  priority=excluded.priority,
                  enabled=excluded.enabled,
                  source_batch=excluded.source_batch,
                  updated_at=CURRENT_TIMESTAMP
                """,
                (
                    item["component_key"], item["layer"], item["title"],
                    item["design_goal"], item["operating_rule"], item["data_inputs"],
                    item["outputs"], item["cadence"], item["storage_policy"],
                    item["owner_ai"], item["applies_to"], item["priority"], 1,
                    SOURCE_BATCH,
                ),
            )
            if exists:
                updated += 1
            else:
                inserted += 1
    return {
        "ok": True,
        "inserted": inserted,
        "updated": updated,
        "total_default_components": len(DEFAULT_COMPONENTS),
        "source_batch": SOURCE_BATCH,
    }


def _ensure_seeded() -> None:
    _ensure_tables()
    row = query_one("SELECT COUNT(*) AS n FROM analysis_architecture_components")
    if not row or int(row.get("n") or 0) == 0:
        seed_analysis_architecture(overwrite=True)


def list_analysis_architecture(
    layer: Optional[str] = None,
    applies_to: Optional[str] = None,
    limit: int = 100,
    enabled_only: bool = True,
) -> dict:
    _ensure_seeded()
    limit = max(1, min(int(limit or 100), 300))
    where = []
    params: list = []
    if enabled_only:
        where.append("enabled=1")
    if layer:
        where.append("layer=?")
        params.append(layer)
    if applies_to:
        where.append("applies_to LIKE ?")
        params.append(f"%{applies_to}%")
    sql = "SELECT * FROM analysis_architecture_components"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY priority DESC, id ASC LIMIT ?"
    params.append(limit)
    rows = query_all(sql, tuple(params))
    layers = query_all(
        "SELECT layer, COUNT(*) AS count FROM analysis_architecture_components WHERE enabled=1 GROUP BY layer ORDER BY MAX(priority) DESC"
    )
    return {
        "count": len(rows),
        "layers": layers,
        "items": rows,
        "source": {
            "source_batch": SOURCE_BATCH,
            "distilled_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "note": "系统架构规则层：多源数据、研报学习、互动跟踪、AI分工、PK、风控和审计。",
        },
    }


def _context_bonus(row: dict, context: str) -> int:
    ctx = (context or "").lower()
    text = " ".join(str(row.get(k) or "") for k in (
        "layer", "title", "design_goal", "operating_rule", "applies_to", "owner_ai"
    ))
    bonus = 0
    groups = [
        (["研报", "report", "中信", "中金", "作者"], ["研报", "作者", "团队", "broker"]),
        (["持仓", "holding", "交易", "截图"], ["持仓", "截图", "确认", "positions"]),
        (["pk", "模拟", "组合"], ["PK", "100 万", "组合", "T+1"]),
        (["风险", "止损", "仓位", "risk"], ["风控", "仓位", "止损", "现金"]),
        (["聊天", "互动", "跟踪"], ["互动", "聊天", "持续跟踪"]),
        (["数据", "tushare", "akshare"], ["Tushare", "AkShare", "多源"]),
        (["复盘", "14", "手法"], ["14 类", "复盘", "手法"]),
    ]
    for keys, hits in groups:
        if any(k in ctx for k in keys) and any(h.lower() in text.lower() for h in hits):
            bonus += 18
    return bonus


def format_analysis_architecture_for_prompt(context: str = "", limit: int = 8) -> str:
    _ensure_seeded()
    rows = query_all("SELECT * FROM analysis_architecture_components WHERE enabled=1")
    ranked = sorted(
        rows,
        key=lambda r: int(r.get("priority") or 0) + _context_bonus(r, context),
        reverse=True,
    )
    selected = ranked[: max(1, min(int(limit or 8), 30))]
    lines = [
        "## 系统分析架构（用户已确认）",
        "分析必须按“多源数据 -> 蒸馏 -> 记忆/跟踪 -> 多AI分工 -> 风控/仓位 -> 行动 -> 反馈 -> 再分析”的盈利闭环运行。",
    ]
    for r in selected:
        lines.append(
            f"- [{r.get('layer')}] {r.get('title')}: {r.get('operating_rule')} "
            f"输入: {r.get('data_inputs')} 输出: {r.get('outputs')} 节奏: {r.get('cadence')}"
        )
    return "\n".join(lines)


def architecture_checklist() -> dict:
    _ensure_seeded()
    return {
        "decision_flow": [
            "分析、决策、行动、反馈、再分析必须形成闭环。",
            "先查多源数据，不凭记忆补数字。",
            "保留蒸馏后的信号、评分和证据链，控制硬盘占用。",
            "研报先分券商/作者/团队质量，再进入结论。",
            "互动股票、持仓变动、外部荐股都要留下记忆并后验复盘。",
            "四个 AI 分工独立，最后由风控和组合决策闸门收束。",
        ],
        "ai_roles": {
            "DeepSeek": "基本面、估值、综合仲裁",
            "Gemini": "研报、机构、一致预期、作者质量",
            "Claude": "技术、资金、博弈、反方风险",
            "Codex": "政策、事件、数据管线、系统结构",
        },
        "storage_principle": "少存原始大文件，多存蒸馏信号、来源索引、评分和后验表现。",
        "portfolio_principle": "真实持仓需要用户确认；AI PK 独立模拟，不复制用户持仓。",
        "profit_objective": "以盈利为目的，但盈利必须来自可复盘、可重复、可风控的正期望流程。",
    }
