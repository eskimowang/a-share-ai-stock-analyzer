"""聊天服务 —— 多 AI 仲裁 + 工具调用 + 上下文注入。"""
import json
import logging
import re
import uuid
from datetime import datetime
from typing import Optional

from ..config import CONFIG
from ..db import db, query_all, query_one, execute
from ..ai.multi_brain import build_brains_from_config
from ..ai.openai_compat import DEEPSEEK_MODEL_ALIASES, normalize_model_name
from .tool_executor import list_tools_for_ai, execute_tool

log = logging.getLogger(__name__)

_brains = build_brains_from_config(CONFIG)


def _pick_brain(model: Optional[str]):
    if not model:
        return _brains[0]
    for brain in _brains:
        if brain.name == model:
            return brain
    if model in DEEPSEEK_MODEL_ALIASES or model.startswith("deepseek-"):
        for brain in _brains:
            if brain.name == "DeepSeek":
                requested = normalize_model_name("DeepSeek", model, getattr(brain, "model", None))
                if getattr(brain, "model", None) != requested:
                    log.info("Switch DeepSeek model from %s to %s for this request", getattr(brain, "model", None), requested)
                    brain.model = requested
                return brain
    return _brains[0]


def _ensure_chat_tables():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS chat_sessions (
            id TEXT PRIMARY KEY,
            title TEXT,
            ai_mode TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT,
            content TEXT,
            ai_name TEXT,
            tool_call TEXT,
            tool_result TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_chat_session ON chat_messages(session_id, timestamp);
        """)


SYSTEM_TEMPLATE = """你是 A 股投资助手。基于用户真实持仓数据回答问题。

## 今日日期
{today_date}

## 用户当前持仓（含最新一日完整快照）
{positions_summary}

## 用户当前自选
{watchlist_summary}

## 用户交易认知规则
{trading_cognition_summary}

## 系统分析架构
{analysis_architecture_summary}

## GitHub热门工具参考
{open_source_tools_summary}

## 决策行动反馈闭环
{decision_feedback_summary}

## 可用工具（严格输出 <TOOL_CALL>{{"name":"query_stock_snapshot","args":{{"stock_code":"002261"}}}}</TOOL_CALL> 触发）
{tools_desc}

## 严格规则
1. **禁止凭记忆或联网编造任何股价、涨跌幅、成交量、资金流数据** —— 上面持仓表里已经给了你最新的 close/涨跌幅/PE/PB/资金流，直接引用这些数字
2. 需要查询持仓以外信息（K 线历史、研报、一致预期、财务、筹码、融资、市场环境）时，必须优先调工具，不要瞎猜
3. 股票代码参数统一用 `stock_code`
4. 所有数据引用必须带日期（如"04-23 收盘 560.89"），数据库无此数据就说"DB 内无此数据"
5. 研报解读时，中金/中信证券权重最高；其他券商以蒸馏和数量统计为主，并参考作者/团队历史命中率
6. 破坏性操作必须先说明操作并等用户确认
6. 答案用 markdown 格式，控制在 400 字内
"""


def _build_system_prompt() -> str:
    from datetime import datetime as _dt
    positions = query_all(
        "SELECT p.stock_code, p.stock_name, "
        "SUM(CASE WHEN t.trade_type='buy' THEN t.quantity ELSE -t.quantity END) as qty, "
        "SUM(CASE WHEN t.trade_type='buy' THEN t.price*t.quantity+t.fee ELSE -t.price*t.quantity+t.fee END) as cost "
        "FROM positions p JOIN trades t ON p.id=t.position_id "
        "WHERE p.status='holding' GROUP BY p.id"
    )
    lines = []
    for p in positions:
        code = p['stock_code']
        qty = p['qty'] or 0
        avg = (p['cost'] / qty) if qty else 0
        snap = query_one(
            "SELECT trade_date, close, pe_ttm, pb FROM daily_basic WHERE stock_code=? "
            "ORDER BY trade_date DESC LIMIT 1", (code,)
        ) or {}
        kl = query_one(
            "SELECT trade_date, close, change_pct FROM daily_quotes WHERE stock_code=? "
            "ORDER BY trade_date DESC LIMIT 1", (code,)
        ) or {}
        mf = query_one(
            "SELECT net_mf_amount FROM moneyflow_cache WHERE stock_code=? "
            "ORDER BY trade_date DESC LIMIT 1", (code,)
        ) or {}
        cur = kl.get('close') or snap.get('close') or 0
        chg = kl.get('change_pct')
        pl = ((cur - avg) / avg * 100) if avg else 0
        mfv = (mf.get('net_mf_amount') or 0) / 10000 if mf.get('net_mf_amount') else None
        kl_date = str(kl.get('trade_date', ''))[:10]
        parts = [f"{code} {p['stock_name']}", f"{qty}股", f"成本{avg:.2f}",
                 f"现价{cur:.2f}"]
        if chg is not None:
            parts.append(f"今{chg:+.2f}%")
        parts.append(f"浮盈{pl:+.2f}%")
        if snap.get('pe_ttm'):
            parts.append(f"PE{snap['pe_ttm']:.1f}")
        if snap.get('pb'):
            parts.append(f"PB{snap['pb']:.2f}")
        if mfv is not None:
            parts.append(f"资金净流{mfv:+.2f}亿")
        parts.append(f"[数据日{kl_date}]")
        lines.append("- " + " / ".join(parts))
    pos_str = "\n".join(lines) if lines else "（无持仓）"

    watch = query_all("SELECT stock_code, stock_name FROM watchlist LIMIT 20")
    watch_str = ", ".join(f"{w['stock_code']}({w['stock_name']})" for w in watch) or "（无）"

    tools = list_tools_for_ai()
    tools_desc = "\n".join(
        f"- `{t['name']}` [{t['level']}] {t['description']}" for t in tools
    )

    try:
        from .trading_cognition_service import format_trading_cognition_for_prompt
        cognition = format_trading_cognition_for_prompt(context='聊天 持仓 分析 推荐 风控', limit=10)
    except Exception as e:
        log.warning('读取交易认知规则失败: %s', e)
        cognition = '（交易认知规则暂不可用）'

    try:
        from .analysis_architecture_service import format_analysis_architecture_for_prompt
        architecture = format_analysis_architecture_for_prompt(
            context='聊天 持仓 分析 推荐 风控 研报 数据 AI PK', limit=10,
        )
    except Exception as e:
        log.warning('读取分析架构失败: %s', e)
        architecture = '（分析架构暂不可用）'

    try:
        from .open_source_tool_reference_service import format_open_source_tool_references_for_prompt
        open_source_tools = format_open_source_tool_references_for_prompt(
            context='聊天 持仓 分析 推荐 风控 研报 数据 AI PK 回测', limit=8,
        )
    except Exception as e:
        log.warning('读取开源工具参考失败: %s', e)
        open_source_tools = '（开源工具参考暂不可用）'

    try:
        from .decision_feedback_service import format_decision_feedback_for_prompt
        decision_feedback = format_decision_feedback_for_prompt(
            context='聊天 分析 决策 行动 反馈 盈利 风控 复盘', limit=6,
        )
    except Exception as e:
        log.warning('读取决策反馈闭环失败: %s', e)
        decision_feedback = '（决策反馈闭环暂不可用）'

    return SYSTEM_TEMPLATE.format(
        today_date=_dt.now().strftime('%Y-%m-%d %H:%M'),
        positions_summary=pos_str,
        watchlist_summary=watch_str,
        trading_cognition_summary=cognition,
        analysis_architecture_summary=architecture,
        open_source_tools_summary=open_source_tools,
        decision_feedback_summary=decision_feedback,
        tools_desc=tools_desc,
    )


TOOL_CALL_RE = re.compile(r"<TOOL_CALL>\s*(\{.*?\})\s*</TOOL_CALL>", re.DOTALL)


def _normalize_tool_call(raw) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None

    name = raw.get("name") or raw.get("tool_name")
    args = raw.get("args")
    if args is None:
        args = raw.get("arguments")

    function = raw.get("function")
    if isinstance(function, dict):
        name = name or function.get("name")
        if args is None:
            args = function.get("arguments")
    elif isinstance(function, str):
        name = name or function

    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            args = {}
    if not isinstance(args, dict):
        args = {}

    if not name:
        return None
    return {"name": name, "args": args}


def _parse_tool_call(text: str) -> Optional[dict]:
    m = TOOL_CALL_RE.search(text)
    if not m:
        return None
    try:
        return _normalize_tool_call(json.loads(m.group(1)))
    except Exception:
        return None


def _strip_tool_call(text: str) -> str:
    return TOOL_CALL_RE.sub("", text).strip()


def _safe_text(text, ai_name: str = "AI") -> str:
    if text is None:
        return ""
    if not isinstance(text, str):
        try:
            text = json.dumps(text, ensure_ascii=False, default=str)
        except Exception:
            text = str(text)
    return text.strip()


def _looks_like_error(text: str) -> bool:
    s = (text or "").strip()
    return s.startswith("[错误]") or s.startswith("[失败]")


def _call_ai_with_fallback(chosen, system: str, user: str,
                           max_tokens: int = 1500) -> tuple[str, str]:
    """调用一个 AI；失败或空回复时自动换可用后备，避免聊天接口 500。"""
    tried = []
    candidates = [chosen] + [b for b in _brains if b is not chosen]
    for brain in candidates:
        if brain.name in tried:
            continue
        tried.append(brain.name)
        try:
            text = _safe_text(brain.complete(system, user, max_tokens=max_tokens), brain.name)
            if text:
                if brain is not chosen:
                    log.warning("AI fallback succeeded: %s -> %s", chosen.name, brain.name)
                return text, brain.name
            log.warning("%s returned empty chat response", brain.name)
        except Exception as e:
            log.exception("AI chat call failed: %s", brain.name)
    return (
        "AI 调用这次没有成功，但系统没有崩。可以稍后重试，"
        "或换 DeepSeek V4 Pro 再发一次；后台已经记录错误。"
        f"\n\n已尝试: {', '.join(tried)}",
        "系统",
    )


def send_message(session_id: Optional[str], message: str,
                  mode: str = "consensus", model: Optional[str] = None) -> dict:
    """处理一条聊天消息。返回 AI 回复（可能包含工具调用）。"""
    _ensure_chat_tables()

    # 新建会话
    if not session_id:
        session_id = str(uuid.uuid4())
        title = message[:30]
        execute(
            "INSERT INTO chat_sessions(id, title, ai_mode) VALUES (?,?,?)",
            (session_id, title, mode),
        )

    # 保存用户消息
    execute(
        "INSERT INTO chat_messages(session_id, role, content) VALUES (?,?,?)",
        (session_id, "user", message),
    )

    # 互动即入池：聊天里提到过的股票自动加入独立跟踪池
    try:
        from .interaction_tracking_service import record_interaction_stocks
        tracked = record_interaction_stocks(message, session_id=session_id)
        if tracked:
            log.info("记录互动股票: %s", tracked)
    except Exception as e:
        log.warning("记录互动股票失败: %s", e)

    # 构造 system prompt（含持仓上下文）
    system = _build_system_prompt()

    # 读取会话最近 10 条历史
    history = query_all(
        "SELECT role, content, ai_name FROM chat_messages "
        "WHERE session_id=? ORDER BY timestamp DESC LIMIT 10",
        (session_id,),
    )
    history.reverse()
    history_text = "\n\n".join(
        f"{m['role'].upper()}: {m['content']}" for m in history[:-1]  # 排除当前
    )
    full_user = f"{history_text}\n\n当前问题: {message}" if history_text else message

    # 根据 mode 调用 AI
    if mode == "consensus":
        # 4 家一起答，Claude 做仲裁
        import concurrent.futures
        def _one(c):
            try:
                return c.name, c.complete(system, full_user, max_tokens=1500)
            except Exception as e:
                return c.name, f"[错误] {e}"
        opinions = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(_brains)) as pool:
            for f in concurrent.futures.as_completed([pool.submit(_one, b) for b in _brains]):
                n, t = f.result()
                opinions[n] = t

        claude = next((b for b in _brains if b.name == "Claude"), _brains[0])
        joined = "\n\n".join(f"## 【{n}】\n{t}" for n, t in opinions.items())
        non_error = [t for t in opinions.values() if t and not _looks_like_error(t)]
        try:
            combined = claude.complete(
                "你整合多家 AI 观点给用户简洁回复。",
                f"用户问: {message}\n\n四家 AI 回答:\n{joined}\n\n"
                "请给一个简洁、实用、不超过 300 字的综合回复。如果有工具调用需求，"
                "以 <TOOL_CALL>{...}</TOOL_CALL> 格式输出。",
                max_tokens=800,
            )
            response_text = _safe_text(combined, "仲裁")
            ai_name = "仲裁"
        except Exception as e:
            log.exception("仲裁整合失败")
            if non_error:
                response_text = non_error[0]
                ai_name = "仲裁降级"
            else:
                response_text = f"多模型仲裁暂时失败：{e}"
                ai_name = "系统"
    else:
        # 单 AI
        chosen = _pick_brain(model)
        response_text, ai_name = _call_ai_with_fallback(
            chosen, system, full_user, max_tokens=1500
        )

    response_text = _safe_text(response_text, ai_name)
    if not response_text:
        response_text = "AI 这次返回为空，已降级为系统提示。请稍后重试。"
        ai_name = "系统"

    # 解析工具调用
    tool_call = _parse_tool_call(response_text)
    clean_text = _strip_tool_call(response_text) or response_text
    tool_result = None
    require_confirm = None

    if tool_call:
        # 执行工具（只读自动执行，写入/破坏要确认）
        try:
            exec_result = execute_tool(
                tool_call.get("name"),
                tool_call.get("args", {}) or {},
                session_id=session_id,
                user_confirmed=False,  # 默认未确认
            )
        except Exception as e:
            log.exception("工具调用解析/执行失败")
            exec_result = {"success": False, "error": str(e), "tool_call": tool_call}

        if exec_result.get("require_confirm"):
            require_confirm = exec_result
        else:
            tool_result = exec_result
            # 工具结果喂回 AI 生成最终回答
            claude = next((b for b in _brains if b.name == "Claude"), _brains[0])
            try:
                final = claude.complete(
                    system,
                    f"用户问: {message}\n\n工具 {tool_call.get('name')} 的执行结果:\n"
                    f"{json.dumps(tool_result, ensure_ascii=False, default=str)}\n\n"
                    "请基于工具结果给用户一个简洁的最终回答，不超过 300 字。",
                    max_tokens=800,
                )
                final_text = _safe_text(final, claude.name)
                clean_text = (_strip_tool_call(final_text) or final_text) or clean_text
            except Exception:
                log.exception("工具结果二次总结失败")
                if not clean_text:
                    clean_text = (
                        "工具已执行，但总结模型暂时失败。原始结果：\n"
                        f"{json.dumps(tool_result, ensure_ascii=False, default=str)}"
                    )

    # 保存 AI 回复
    execute(
        "INSERT INTO chat_messages(session_id, role, content, ai_name, tool_call, tool_result) "
        "VALUES (?,?,?,?,?,?)",
        (session_id, "assistant", clean_text, ai_name,
         json.dumps(tool_call) if tool_call else None,
         json.dumps(tool_result, ensure_ascii=False, default=str) if tool_result else None),
    )

    # 更新会话 last_active
    execute("UPDATE chat_sessions SET last_active=CURRENT_TIMESTAMP WHERE id=?", (session_id,))

    return {
        "session_id": session_id,
        "messages": [
            {"role": "user", "content": message},
            {"role": "assistant", "ai_name": ai_name, "content": clean_text,
             "tool_call": tool_call, "tool_result": tool_result},
        ],
        "require_confirm": require_confirm,
    }


def confirm_tool(session_id: str, tool_name: str, args: dict) -> dict:
    """用户确认后执行工具。"""
    return execute_tool(tool_name, args, session_id=session_id, user_confirmed=True)


def list_sessions() -> list[dict]:
    return query_all(
        "SELECT id, title, ai_mode, created_at, last_active FROM chat_sessions "
        "ORDER BY last_active DESC LIMIT 50"
    )


def get_messages(session_id: str) -> list[dict]:
    return query_all(
        "SELECT role, content, ai_name, tool_call, tool_result, timestamp "
        "FROM chat_messages WHERE session_id=? ORDER BY timestamp",
        (session_id,),
    )
