"""交易认知规则库。

从用户提供的炒股认知截图中蒸馏出可执行规则，供聊天、单股分析、
持仓复盘、AI PK 和风险管理读取。只保存文字规则，不保存大图，节省硬盘。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from ..db import db, query_all, query_one


SOURCE_BATCH = "desktop_new_folder_20260401"
SOURCE_PATH = r"C:\Users\mao\Desktop\新建文件夹"


DEFAULT_RULES = [
    {
        "rule_key": "market_nature_resource_allocation",
        "category": "底层认知",
        "title": "股市首先是融资和资源配置场所",
        "rule_text": "股票代表公司部分所有权，股市承担融资、资源配置、价格发现和价值交换功能，不是天然让散户致富的场所。",
        "operational_check": "分析任何机会前，先问它背后的融资、产业、资金和估值交换逻辑是什么。",
        "risk_guard": "不要把短期交易当业余爱好，更不要用愿望代替商业逻辑。",
        "applies_to": "chat,analysis,pk,review",
        "priority": 100,
    },
    {
        "rule_key": "four_systems",
        "category": "系统框架",
        "title": "交易体系四模块",
        "rule_text": "完整交易体系包括选股体系、交易体系、仓位体系、风控体系。选股有边界，买卖有纪律，仓位有分配，风控有底线。",
        "operational_check": "每次建议必须同时回答：为何选、何时买卖、买多少、错了怎么办。",
        "risk_guard": "缺少任一模块时，只能观察或轻仓验证。",
        "applies_to": "chat,analysis,pk,risk",
        "priority": 98,
    },
    {
        "rule_key": "analysis_three_dimensions",
        "category": "分析框架",
        "title": "宏观、行业、公司三维分析",
        "rule_text": "分析股票不先问能不能买，而是先判断宏观周期、行业赛道和公司质地。周期决定顺逆风，行业决定赔率池，公司决定能否持续赚钱。",
        "operational_check": "输出结论前必须标注：宏观周期、行业阶段、公司护城河/商业模式/估值是否匹配。",
        "risk_guard": "行业阶段和公司质地不清楚时，不给重仓建议。",
        "applies_to": "analysis,chat,pk",
        "priority": 95,
    },
    {
        "rule_key": "stock_factors_four_faces",
        "category": "分析框架",
        "title": "影响股价的四类因素",
        "rule_text": "股价受基本面、技术面、政策面、资金面共同影响。任何行情的动力都要回到资金推动和预期变化。",
        "operational_check": "单股分析至少给出基本面、技术面、政策面、资金面各一条证据。",
        "risk_guard": "只有单一消息或单一指标支撑时，降低置信度。",
        "applies_to": "analysis,chat",
        "priority": 92,
    },
    {
        "rule_key": "trade_is_response_not_prediction",
        "category": "交易纪律",
        "title": "交易不是预测，而是应对",
        "rule_text": "买入要有理由，卖出要有条件，不凭感觉。交易计划要预设确认、失效和应对。",
        "operational_check": "所有买入建议都必须给出触发条件、失效条件、止损位和复盘时间。",
        "risk_guard": "无法定义失效条件的机会，不进入买入清单。",
        "applies_to": "chat,analysis,pk,risk",
        "priority": 96,
    },
    {
        "rule_key": "position_management_over_stock_picking",
        "category": "仓位体系",
        "title": "仓位管理大于选股能力",
        "rule_text": "方向对但仓位轻，可能少赚；方向错且仓位重，会一次出局。首次开仓不满仓，永远保留预备队。",
        "operational_check": "默认参考 334 仓位法：30% 底仓、30% 机动、40% 现金，除非证据强且风控允许。",
        "risk_guard": "单股、单行业、单主题都要有上限；不允许因为看好而忽略现金储备。",
        "applies_to": "risk,pk,chat,analysis",
        "priority": 99,
    },
    {
        "rule_key": "stop_loss_discipline",
        "category": "风控体系",
        "title": "止损纪律大于分析预测",
        "rule_text": "止损要快，止盈要慢。买入后若逻辑破坏或价格触发纪律线，先处理风险，不与亏损争辩。",
        "operational_check": "软警戒：买入价下方约 5% 或跌破关键均线复核；硬止损按系统规则执行。",
        "risk_guard": "跌破 60 日线、趋势失效、主线退潮时，不允许用补仓掩盖错误。",
        "applies_to": "risk,chat,analysis,pk",
        "priority": 99,
    },
    {
        "rule_key": "logic_verification_three_questions",
        "category": "验证机制",
        "title": "听消息买，必须眼见实",
        "rule_text": "消息不能直接买。买入前三问：为什么涨，谁在买，还能涨吗。对应逻辑、资金、空间。",
        "operational_check": "对荐股、研报、热股月采、聊天推荐都必须补齐逻辑、资金、空间三问。",
        "risk_guard": "只听到消息但看不到资金和空间时，记录为待观察，不转买入。",
        "applies_to": "recommendation,report,chat,analysis",
        "priority": 97,
    },
    {
        "rule_key": "wait_over_action",
        "category": "交易纪律",
        "title": "等待大于操作",
        "rule_text": "等待是一种美德。大部分收益来自少数时间，机会大于能力；看不懂时看戏。",
        "operational_check": "当市场、主线或个股证据不足时，输出等待条件，而不是硬给操作。",
        "risk_guard": "为了参与感而交易，视为违规。",
        "applies_to": "chat,analysis,pk",
        "priority": 93,
    },
    {
        "rule_key": "profit_three_principles",
        "category": "总原则",
        "title": "盈利三原则",
        "rule_text": "炒股盈利顺序是保障资本、稳健盈利、追求卓越。先活下来，再追求复利。",
        "operational_check": "组合建议先检查最大回撤和现金安全垫，再讨论收益率。",
        "risk_guard": "任何高收益设想都不能越过本金安全和止损纪律。",
        "applies_to": "risk,pk,chat",
        "priority": 94,
    },
    {
        "rule_key": "avoid_common_mistakes",
        "category": "反面清单",
        "title": "散户常见致命错误",
        "rule_text": "避免三分钟审查一只票、只买生不买熟、买前过度自信买后过度恐慌、追求确定性和精准买点、靠意念炒股。",
        "operational_check": "分析报告里对高风险机会必须列出“我可能错在哪里”。",
        "risk_guard": "研究不足、证据不足、心理状态不稳时，不给买入动作。",
        "applies_to": "chat,analysis,review",
        "priority": 90,
    },
    {
        "rule_key": "market_participant_hierarchy",
        "category": "市场结构",
        "title": "认识市场利益结构",
        "rule_text": "市场里不同参与者的信息、资金、成本和规则优势不同。中小散户信息最晚、情绪最重、成本最高。",
        "operational_check": "判断行情时要区分机构、大资金、游资、散户情绪和监管/交易所规则影响。",
        "risk_guard": "不要假设自己比资金优势方更早知道消息。",
        "applies_to": "chat,analysis,pk",
        "priority": 86,
    },
    {
        "rule_key": "mainline_hot_dragon",
        "category": "主线打法",
        "title": "一板定热点，二板定龙头，找主线研究",
        "rule_text": "热点交易要看板块结构、龙头确认和主升浪阶段。目标是理解主力意图，只做主升浪。",
        "operational_check": "热点股必须判断：是否主线、是否龙头、是否仍在主升、是否已经一致拥挤。",
        "risk_guard": "非龙头、后排补涨、主线退潮时不追高。",
        "applies_to": "playbook,pk,chat,analysis",
        "priority": 91,
    },
    {
        "rule_key": "seasonal_market_cycle",
        "category": "市场节奏",
        "title": "春播、夏长、秋收、冬藏",
        "rule_text": "市场不同阶段对应不同动作：春播观察布局，夏长重仓介入，秋收逐步减仓，冬藏空仓休息。",
        "operational_check": "每周复盘需要给市场阶段标签和对应策略。",
        "risk_guard": "冬藏期不因个别反弹轻易重仓。",
        "applies_to": "review,pk,chat",
        "priority": 84,
    },
    {
        "rule_key": "a_share_basic_rules",
        "category": "A股约束",
        "title": "A股交易约束必须进入决策",
        "rule_text": "A股有 T+1、100 股一手、涨跌停、ST/*ST 5% 限制、交易时间等约束。",
        "operational_check": "所有模拟交易和建议都必须符合交易单位、可卖数量、涨跌停和时间约束。",
        "risk_guard": "不允许给出当日买入后又卖出的普通 A 股策略。",
        "applies_to": "pk,risk,chat",
        "priority": 88,
    },
    {
        "rule_key": "kline_volume_price_basis",
        "category": "技术基础",
        "title": "K线和成交量是价格行为证据",
        "rule_text": "K线记录开高低收，成交量衡量交易强度。技术判断不能离开量价配合。",
        "operational_check": "突破、回踩、洗盘、派发判断必须同时看价格位置和成交量变化。",
        "risk_guard": "无量上涨、放量滞涨、跳空低开后弱反抽都要降级处理。",
        "applies_to": "playbook,analysis,chat",
        "priority": 82,
    },
]


def _ensure_tables() -> None:
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS trading_cognition_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_key TEXT UNIQUE NOT NULL,
            category TEXT,
            title TEXT NOT NULL,
            rule_text TEXT NOT NULL,
            operational_check TEXT,
            risk_guard TEXT,
            applies_to TEXT,
            priority INTEGER DEFAULT 50,
            enabled INTEGER DEFAULT 1,
            source_batch TEXT,
            source_path TEXT,
            evidence_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_trading_cognition_category
          ON trading_cognition_rules(category, priority DESC);
        CREATE INDEX IF NOT EXISTS idx_trading_cognition_enabled
          ON trading_cognition_rules(enabled, priority DESC);
        """)


def seed_trading_cognition_rules(overwrite: bool = True) -> dict:
    """写入/刷新默认交易认知规则。"""
    _ensure_tables()
    inserted = 0
    updated = 0
    with db() as c:
        for r in DEFAULT_RULES:
            exists = c.execute(
                "SELECT id FROM trading_cognition_rules WHERE rule_key=?",
                (r["rule_key"],),
            ).fetchone()
            if exists and not overwrite:
                continue
            c.execute(
                """
                INSERT INTO trading_cognition_rules
                (rule_key, category, title, rule_text, operational_check, risk_guard,
                 applies_to, priority, enabled, source_batch, source_path, evidence_count)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(rule_key) DO UPDATE SET
                  category=excluded.category,
                  title=excluded.title,
                  rule_text=excluded.rule_text,
                  operational_check=excluded.operational_check,
                  risk_guard=excluded.risk_guard,
                  applies_to=excluded.applies_to,
                  priority=excluded.priority,
                  enabled=excluded.enabled,
                  source_batch=excluded.source_batch,
                  source_path=excluded.source_path,
                  evidence_count=excluded.evidence_count,
                  updated_at=CURRENT_TIMESTAMP
                """,
                (
                    r["rule_key"], r["category"], r["title"], r["rule_text"],
                    r["operational_check"], r["risk_guard"], r["applies_to"],
                    r["priority"], 1, SOURCE_BATCH, SOURCE_PATH, 55,
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
        "total_default_rules": len(DEFAULT_RULES),
        "source_batch": SOURCE_BATCH,
        "evidence_count": 55,
    }


def _ensure_seeded() -> None:
    _ensure_tables()
    row = query_one("SELECT COUNT(*) AS n FROM trading_cognition_rules")
    if not row or int(row.get("n") or 0) == 0:
        seed_trading_cognition_rules(overwrite=True)


def list_trading_cognition_rules(
    category: Optional[str] = None,
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
    if category:
        where.append("category=?")
        params.append(category)
    if applies_to:
        where.append("applies_to LIKE ?")
        params.append(f"%{applies_to}%")
    sql = "SELECT * FROM trading_cognition_rules"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY priority DESC, id ASC LIMIT ?"
    params.append(limit)
    rows = query_all(sql, tuple(params))
    cats = query_all(
        "SELECT category, COUNT(*) AS count FROM trading_cognition_rules WHERE enabled=1 GROUP BY category ORDER BY MAX(priority) DESC"
    )
    return {
        "count": len(rows),
        "categories": cats,
        "items": rows,
        "source": {
            "source_batch": SOURCE_BATCH,
            "source_path": SOURCE_PATH,
            "evidence_count": 55,
            "distilled_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
    }


def _context_bonus(row: dict, context: str) -> int:
    ctx = (context or "").lower()
    text = " ".join(str(row.get(k) or "") for k in ("category", "title", "rule_text", "operational_check", "applies_to"))
    bonus = 0
    if any(k in ctx for k in ["仓位", "持仓", "加仓", "买入", "组合", "pk"]):
        if any(k in text for k in ["仓位", "334", "资本", "现金"]):
            bonus += 20
    if any(k in ctx for k in ["止损", "亏损", "风险", "破位", "减仓", "卖出"]):
        if any(k in text for k in ["止损", "风控", "资本", "失效"]):
            bonus += 20
    if any(k in ctx for k in ["热点", "主线", "龙头", "涨停", "二板", "题材"]):
        if any(k in text for k in ["主线", "龙头", "热点", "量价"]):
            bonus += 18
    if any(k in ctx for k in ["研报", "推荐", "消息", "热股月采"]):
        if any(k in text for k in ["消息", "荐股", "验证", "三问"]):
            bonus += 18
    if any(k in ctx for k in ["分析", "单股", "行业", "公司", "宏观"]):
        if any(k in text for k in ["宏观", "行业", "公司", "四类因素"]):
            bonus += 12
    return bonus


def format_trading_cognition_for_prompt(context: str = "", limit: int = 10) -> str:
    """给 AI prompt 注入的精简版规则。"""
    _ensure_seeded()
    rows = query_all("SELECT * FROM trading_cognition_rules WHERE enabled=1")
    ranked = sorted(
        rows,
        key=lambda r: int(r.get("priority") or 0) + _context_bonus(r, context),
        reverse=True,
    )
    selected = ranked[: max(1, min(int(limit or 10), 30))]
    lines = [
        "## 用户交易认知规则（从桌面“新建文件夹”55张截图蒸馏）",
        "这些是用户认可的交易原则。分析和建议必须接受它们约束，尤其是仓位、止损、等待和逻辑验证。",
    ]
    for r in selected:
        lines.append(
            f"- [{r.get('category')}] {r.get('title')}: {r.get('rule_text')} "
            f"执行检查: {r.get('operational_check')} 风险底线: {r.get('risk_guard')}"
        )
    return "\n".join(lines)


def core_cognition_checklist() -> dict:
    _ensure_seeded()
    return {
        "must_answer": [
            "为什么涨/跌，逻辑是什么？",
            "谁在买/卖，资金证据是什么？",
            "还能涨/跌到哪里，赔率空间是什么？",
            "买多少，是否保留现金预备队？",
            "错了怎么办，止损/失效条件是什么？",
        ],
        "position_reference": "334 仓位法：30%底仓、30%机动、40%现金，具体按风险等级调整。",
        "risk_reference": "软警戒约 -5% 或跌破关键均线；硬止损按系统风控执行。",
        "style_reference": "一板定热点、二板定龙头、找主线研究；看不懂时等待。",
    }
