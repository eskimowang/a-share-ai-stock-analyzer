"""分析-决策-行动-反馈-再分析闭环。

目标不是堆观点，而是把每个判断变成可追踪资产：
分析有证据，决策有仓位，行动有记录，反馈有收益/回撤，最后再修正模型。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from ..db import db, query_all, query_one


STAGES = [
    {"key": "analysis", "name": "分析", "rule": "先查多源数据、研报、资金、技术、持仓和交易认知，不编造。"},
    {"key": "decision", "name": "决策", "rule": "把观点落为买/卖/持/等待、仓位、价格区间、失效条件和复盘时间。"},
    {"key": "action", "name": "行动", "rule": "真实交易必须用户确认；AI PK和纸交易必须记录订单、成交、费用和时间。"},
    {"key": "feedback", "name": "反馈", "rule": "按5/20/60日收益、最大回撤、执行纪律、机会成本来评价。"},
    {"key": "reanalysis", "name": "再分析", "rule": "用反馈修正规则、数据权重、AI角色、券商作者评分和仓位纪律。"},
]


def _ensure_tables() -> None:
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS decision_feedback_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            stage TEXT NOT NULL,
            source TEXT DEFAULT 'manual',
            stock_code TEXT,
            stock_name TEXT,
            title TEXT,
            summary TEXT,
            decision TEXT,
            action TEXT,
            expected_outcome TEXT,
            actual_outcome TEXT,
            pnl_pct REAL,
            max_drawdown_pct REAL,
            confidence REAL,
            evidence_json TEXT,
            next_review_at TEXT,
            status TEXT DEFAULT 'open',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_decision_feedback_stage_time
          ON decision_feedback_events(stage, event_time DESC);
        CREATE INDEX IF NOT EXISTS idx_decision_feedback_code_time
          ON decision_feedback_events(stock_code, event_time DESC);
        """)


def _count(sql: str, params: tuple = ()) -> int:
    try:
        row = query_one(sql, params)
        if not row:
            return 0
        return int(next(iter(row.values())) or 0)
    except Exception:
        return 0


def record_decision_feedback_event(event: dict) -> dict:
    _ensure_tables()
    stage = str(event.get("stage") or "analysis").strip()
    valid = {s["key"] for s in STAGES}
    if stage not in valid:
        raise ValueError(f"stage 必须是 {sorted(valid)}")
    evidence = event.get("evidence") or event.get("evidence_json") or {}
    if isinstance(evidence, str):
        evidence_text = evidence[:4000]
    else:
        evidence_text = json.dumps(evidence, ensure_ascii=False, default=str)[:4000]
    with db() as c:
        cur = c.execute(
            """
            INSERT INTO decision_feedback_events
            (stage, source, stock_code, stock_name, title, summary, decision, action,
             expected_outcome, actual_outcome, pnl_pct, max_drawdown_pct, confidence,
             evidence_json, next_review_at, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                stage,
                str(event.get("source") or "manual")[:80],
                str(event.get("stock_code") or event.get("code") or "")[:20],
                str(event.get("stock_name") or event.get("name") or "")[:80],
                str(event.get("title") or "")[:200],
                str(event.get("summary") or "")[:1200],
                str(event.get("decision") or "")[:500],
                str(event.get("action") or "")[:500],
                str(event.get("expected_outcome") or "")[:500],
                str(event.get("actual_outcome") or "")[:500],
                event.get("pnl_pct"),
                event.get("max_drawdown_pct"),
                event.get("confidence"),
                evidence_text,
                str(event.get("next_review_at") or "")[:40],
                str(event.get("status") or "open")[:40],
            ),
        )
        event_id = cur.lastrowid
    return {"ok": True, "id": event_id, "stage": stage}


def list_decision_feedback_events(stock_code: Optional[str] = None,
                                  stage: Optional[str] = None,
                                  limit: int = 100) -> dict:
    _ensure_tables()
    limit = max(1, min(int(limit or 100), 300))
    where = []
    params: list = []
    if stock_code:
        where.append("stock_code=?")
        params.append(stock_code)
    if stage:
        where.append("stage=?")
        params.append(stage)
    sql = "SELECT * FROM decision_feedback_events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY event_time DESC, id DESC LIMIT ?"
    params.append(limit)
    rows = query_all(sql, tuple(params))
    return {"count": len(rows), "items": rows, "stages": STAGES}


def get_decision_feedback_snapshot() -> dict:
    _ensure_tables()
    events_by_stage = query_all(
        "SELECT stage, COUNT(*) AS count FROM decision_feedback_events GROUP BY stage"
    )
    return {
        "success": True,
        "objective": "以盈利为目标，但用风控、仓位、反馈和复盘约束路径，追求长期正期望。",
        "loop": STAGES,
        "counts": {
            "feedback_events": _count("SELECT COUNT(*) AS n FROM decision_feedback_events"),
            "analysis_records": _count("SELECT COUNT(*) AS n FROM daily_analysis"),
            "pending_orders": _count("SELECT COUNT(*) AS n FROM order_instructions WHERE status='pending'"),
            "executed_orders": _count("SELECT COUNT(*) AS n FROM order_instructions WHERE status='executed'"),
            "holding_changes": _count("SELECT COUNT(*) AS n FROM holding_change_submissions"),
            "ai_pk_trades": _count("SELECT COUNT(*) AS n FROM ai_pk_trades"),
            "ai_pk_snapshots": _count("SELECT COUNT(*) AS n FROM ai_pk_daily_snapshots"),
            "paper_trades": _count("SELECT COUNT(*) AS n FROM paper_trades"),
        },
        "events_by_stage": events_by_stage,
        "recent_events": list_decision_feedback_events(limit=12)["items"],
        "rules": {
            "profit": "盈利来自可重复正期望，不来自一次预测正确。",
            "loss_control": "亏损和回撤是反馈信号，不能用补仓掩盖逻辑失效。",
            "execution": "建议必须能落成动作；动作必须能被复盘；复盘必须改变下一次判断。",
            "memory": "所有互动股票、持仓变动、荐股来源、研报作者、AI PK策略都要进入后验学习。",
        },
    }


def format_decision_feedback_for_prompt(context: str = "", limit: int = 8) -> str:
    snap = get_decision_feedback_snapshot()
    lines = [
        "## 分析-决策-行动-反馈-再分析闭环",
        "系统目标是构建长期盈利能力：每次建议必须能被记录、执行、反馈和修正。",
        "闭环顺序：" + " -> ".join(s["name"] for s in STAGES),
        "硬规则：分析不等于决策；决策不等于行动；行动必须有反馈；反馈必须进入下一轮分析。",
    ]
    counts = snap.get("counts", {})
    lines.append(
        "当前闭环账本：分析记录{analysis_records}，待执行订单{pending_orders}，已执行订单{executed_orders}，"
        "持仓变动{holding_changes}，AI PK成交{ai_pk_trades}，反馈事件{feedback_events}。".format(**counts)
    )
    recent = snap.get("recent_events", [])[: max(0, min(int(limit or 8), 20))]
    if recent:
        lines.append("最近反馈事件：")
        for e in recent:
            lines.append(
                f"- {e.get('event_time')} [{e.get('stage')}] {e.get('stock_code') or ''} "
                f"{e.get('title') or e.get('summary') or ''} 决策:{e.get('decision') or '-'} 结果:{e.get('actual_outcome') or '-'}"
            )
    return "\n".join(lines)
