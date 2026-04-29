"""GitHub 开源投资工具参考层。

只保存对本系统有启发的架构/能力，不安装外部项目、不复制代码。
来源为 2026-04-28 在 GitHub 上按热度和相关性筛选的股票/量化/投资工具。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from ..db import db, query_all, query_one


SOURCE_BATCH = "github_popular_stock_tools_20260428"


DEFAULT_TOOLS = [
    {
        "repo_key": "openbb",
        "name": "OpenBB",
        "repo": "OpenBB-finance/OpenBB",
        "repo_url": "https://github.com/OpenBB-finance/OpenBB",
        "stars_text": "66.6k",
        "category": "数据平台/研究终端",
        "useful_parts": "connect once, consume everywhere；多数据源整合；REST/API/桌面/AI Agent 多入口。",
        "adoption_rule": "强化本系统的数据接入层和 source_status：所有数据源统一注册、统一健康检查、统一给 AI 使用。",
        "caution": "不直接接入其许可证和外部数据包；只吸收数据平台架构思想。",
        "priority": 100,
    },
    {
        "repo_key": "qlib",
        "name": "Qlib",
        "repo": "microsoft/qlib",
        "repo_url": "https://github.com/microsoft/qlib",
        "stars_text": "41.3k",
        "category": "AI量化研究平台",
        "useful_parts": "数据处理、模型训练、回测、风险建模、组合优化、订单执行的完整 ML 流水线。",
        "adoption_rule": "把我们的分析链固定为：数据 -> 因子/信号 -> 模型/AI意见 -> 回测 -> 风险 -> 组合 -> 执行。",
        "caution": "暂不引入重型 ML 训练；优先保存蒸馏因子和可回测信号，节省硬盘。",
        "priority": 99,
    },
    {
        "repo_key": "vnpy",
        "name": "vn.py / VeighNa",
        "repo": "vnpy/vnpy",
        "repo_url": "https://github.com/vnpy/vnpy",
        "stars_text": "39.9k",
        "category": "A股/国内量化交易框架",
        "useful_parts": "事件驱动、交易接口、风控模块、组合管理、仿真交易、Web/数据服务，且国内市场适配强。",
        "adoption_rule": "AI PK 和真实持仓建议必须显式分离：仿真撮合、风控规则、组合子账户、交易审计各自成层。",
        "caution": "不接券商实盘自动交易；真实交易仍要求用户确认和手工执行。",
        "priority": 98,
    },
    {
        "repo_key": "yfinance",
        "name": "yfinance",
        "repo": "ranaroussi/yfinance",
        "repo_url": "https://github.com/ranaroussi/yfinance",
        "stars_text": "23.2k",
        "category": "市场数据接口",
        "useful_parts": "Ticker/Tickers/download/Market/WebSocket/Search/Screener 等清晰数据接口设计。",
        "adoption_rule": "把 Tushare/AkShare/Baostock 的能力也拆成标准接口：单标的、多标的、市场、搜索、筛选器、实时流。",
        "caution": "Yahoo 数据仅作海外/补充参考，A股主源仍用 Tushare/AkShare/本地缓存。",
        "priority": 96,
    },
    {
        "repo_key": "backtrader",
        "name": "backtrader",
        "repo": "mementum/backtrader",
        "repo_url": "https://github.com/mementum/backtrader",
        "stars_text": "21.3k",
        "category": "回测/交易模拟",
        "useful_parts": "多数据源、多策略、多周期、佣金、订单类型、仓位 sizing、分析器和交易日历。",
        "adoption_rule": "增强我们的回测层：手续费、滑点、涨跌停、T+1、仓位 sizing、交易日历必须进入每个策略验证。",
        "caution": "GPL 代码不复制进系统；只吸收回测设计和指标口径。",
        "priority": 95,
    },
    {
        "repo_key": "zipline",
        "name": "Zipline",
        "repo": "quantopian/zipline",
        "repo_url": "https://github.com/quantopian/zipline",
        "stars_text": "19.7k",
        "category": "事件驱动回测",
        "useful_parts": "事件驱动 handle_data、order_target、record；研究输出和交易执行分离。",
        "adoption_rule": "把每个 AI PK 调仓和策略复盘都记录为事件：信号、目标仓位、订单、成交、绩效指标。",
        "caution": "项目偏旧，不作为依赖；只吸收事件流水和 record 思想。",
        "priority": 94,
    },
    {
        "repo_key": "qbot",
        "name": "Qbot",
        "repo": "UFund-Me/Qbot",
        "repo_url": "https://github.com/UFund-Me/Qbot",
        "stars_text": "17.1k",
        "category": "AI自动量化平台",
        "useful_parts": "数据层、策略层、交易引擎抽象；AI策略、自动化因子挖掘、在线回测、模拟交易、消息提醒。",
        "adoption_rule": "把本系统的荐股、AI PK、持仓提醒、因子挖掘、消息推送合并进同一闭环。",
        "caution": "自动交易能力只用于模拟；真实买卖仍走用户确认。",
        "priority": 93,
    },
    {
        "repo_key": "myhhub_stock",
        "name": "InStock / myhhub stock",
        "repo": "myhhub/stock",
        "repo_url": "https://github.com/myhhub/stock",
        "stars_text": "12.4k",
        "category": "A股选股/形态/筹码",
        "useful_parts": "A股每日数据、技术指标、筹码分布、K线形态、综合选股、策略验证回测、移动端显示。",
        "adoption_rule": "强化 A股专属维度：筹码分布、61类K线形态、资金流、沪深股通、人气指标、策略筛选矩阵。",
        "caution": "不照搬策略；所有形态信号要经过我们自己的后验收益统计。",
        "priority": 92,
    },
    {
        "repo_key": "ta_lib_python",
        "name": "TA-Lib Python",
        "repo": "TA-Lib/ta-lib-python",
        "repo_url": "https://github.com/TA-Lib/ta-lib-python",
        "stars_text": "11.9k",
        "category": "技术指标/形态识别",
        "useful_parts": "150+ 技术指标、K线形态识别，支持 Pandas/Polars/Numpy。",
        "adoption_rule": "统一技术指标口径，给 14类操作手法、单股分析和复盘提供标准化指标层。",
        "caution": "服务器若安装成本高，优先用已有 pandas 指标和轻量实现；不为指标库牺牲稳定性。",
        "priority": 91,
    },
    {
        "repo_key": "vectorbt",
        "name": "vectorbt",
        "repo": "polakowo/vectorbt",
        "repo_url": "https://github.com/polakowo/vectorbt",
        "stars_text": "7.3k",
        "category": "批量回测/参数扫描",
        "useful_parts": "快速批量测试大量交易想法，适合参数网格和策略族比较。",
        "adoption_rule": "给 14类手法和AI PK策略增加批量参数扫描：不是看一个案例，而是看同类样本分布。",
        "caution": "不追求重型依赖；先做轻量批量回测和汇总分布。",
        "priority": 90,
    },
    {
        "repo_key": "quantstats",
        "name": "QuantStats",
        "repo": "ranaroussi/quantstats",
        "repo_url": "https://github.com/ranaroussi/quantstats",
        "stars_text": "7.0k",
        "category": "组合绩效分析",
        "useful_parts": "组合画像、风险指标、回撤、收益分解和绩效报告。",
        "adoption_rule": "AI PK 和真实组合复盘必须补齐：最大回撤、胜率、盈亏比、夏普/波动、收益来源分解。",
        "caution": "绩效指标不等于买卖建议；只作为后验评估和风控材料。",
        "priority": 89,
    },
]


def _ensure_tables() -> None:
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS open_source_tool_references (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_key TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            repo TEXT,
            repo_url TEXT,
            stars_text TEXT,
            category TEXT,
            useful_parts TEXT,
            adoption_rule TEXT,
            caution TEXT,
            priority INTEGER DEFAULT 50,
            enabled INTEGER DEFAULT 1,
            source_batch TEXT,
            checked_at TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_open_source_tool_refs_category
          ON open_source_tool_references(category, priority DESC);
        CREATE INDEX IF NOT EXISTS idx_open_source_tool_refs_enabled
          ON open_source_tool_references(enabled, priority DESC);
        """)


def seed_open_source_tool_references(overwrite: bool = True) -> dict:
    _ensure_tables()
    inserted = 0
    updated = 0
    checked_at = datetime.now().strftime("%Y-%m-%d")
    with db() as c:
        for item in DEFAULT_TOOLS:
            exists = c.execute(
                "SELECT id FROM open_source_tool_references WHERE repo_key=?",
                (item["repo_key"],),
            ).fetchone()
            if exists and not overwrite:
                continue
            c.execute(
                """
                INSERT INTO open_source_tool_references
                (repo_key, name, repo, repo_url, stars_text, category, useful_parts,
                 adoption_rule, caution, priority, enabled, source_batch, checked_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(repo_key) DO UPDATE SET
                  name=excluded.name,
                  repo=excluded.repo,
                  repo_url=excluded.repo_url,
                  stars_text=excluded.stars_text,
                  category=excluded.category,
                  useful_parts=excluded.useful_parts,
                  adoption_rule=excluded.adoption_rule,
                  caution=excluded.caution,
                  priority=excluded.priority,
                  enabled=excluded.enabled,
                  source_batch=excluded.source_batch,
                  checked_at=excluded.checked_at,
                  updated_at=CURRENT_TIMESTAMP
                """,
                (
                    item["repo_key"], item["name"], item["repo"], item["repo_url"],
                    item["stars_text"], item["category"], item["useful_parts"],
                    item["adoption_rule"], item["caution"], item["priority"],
                    1, SOURCE_BATCH, checked_at,
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
        "total_default_tools": len(DEFAULT_TOOLS),
        "source_batch": SOURCE_BATCH,
        "checked_at": checked_at,
    }


def _ensure_seeded() -> None:
    _ensure_tables()
    row = query_one("SELECT COUNT(*) AS n FROM open_source_tool_references")
    if not row or int(row.get("n") or 0) == 0:
        seed_open_source_tool_references(overwrite=True)


def list_open_source_tool_references(
    category: Optional[str] = None,
    limit: int = 50,
    enabled_only: bool = True,
) -> dict:
    _ensure_seeded()
    limit = max(1, min(int(limit or 50), 100))
    where = []
    params: list = []
    if enabled_only:
        where.append("enabled=1")
    if category:
        where.append("category=?")
        params.append(category)
    sql = "SELECT * FROM open_source_tool_references"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY priority DESC, id ASC LIMIT ?"
    params.append(limit)
    rows = query_all(sql, tuple(params))
    cats = query_all(
        "SELECT category, COUNT(*) AS count FROM open_source_tool_references WHERE enabled=1 GROUP BY category ORDER BY MAX(priority) DESC"
    )
    return {
        "count": len(rows),
        "categories": cats,
        "items": rows,
        "source": {
            "source_batch": SOURCE_BATCH,
            "checked_at": (rows[0].get("checked_at") if rows else datetime.now().strftime("%Y-%m-%d")),
            "note": "GitHub 热门投资/量化/股票工具的架构参考，不代表安装依赖。",
        },
    }


def format_open_source_tool_references_for_prompt(context: str = "", limit: int = 8) -> str:
    _ensure_seeded()
    rows = query_all("SELECT * FROM open_source_tool_references WHERE enabled=1")
    ctx = (context or "").lower()
    def score(r: dict) -> int:
        base = int(r.get("priority") or 0)
        text = " ".join(str(r.get(k) or "") for k in ("name", "category", "useful_parts", "adoption_rule"))
        if any(k in ctx for k in ["回测", "复盘", "pk", "绩效"]) and any(k in text for k in ["回测", "绩效", "策略"]):
            base += 20
        if any(k in ctx for k in ["数据", "tushare", "akshare"]) and any(k in text for k in ["数据", "接口", "多源"]):
            base += 18
        if any(k in ctx for k in ["技术", "形态", "手法"]) and any(k in text for k in ["指标", "形态", "K线"]):
            base += 18
        if any(k in ctx for k in ["研报", "ai", "因子"]) and any(k in text for k in ["AI", "因子", "模型"]):
            base += 12
        return base
    selected = sorted(rows, key=score, reverse=True)[: max(1, min(int(limit or 8), 20))]
    lines = [
        "## GitHub热门投资工具参考（只吸收架构，不复制代码）",
        "系统可借鉴这些开源项目的模块分工：数据平台、AI量化、A股交易框架、回测、指标、绩效报告。",
    ]
    for r in selected:
        lines.append(
            f"- {r.get('name')}({r.get('stars_text')}): {r.get('useful_parts')} "
            f"落地规则: {r.get('adoption_rule')} 注意: {r.get('caution')}"
        )
    return "\n".join(lines)
