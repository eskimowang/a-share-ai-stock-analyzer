"""持仓变动提交服务。

给用户一个稳定入口，把每日买入/卖出/加减仓写入现有 positions + trades，
同时保留一份独立的提交日志，方便 AI 回看“最近仓位怎么变了”。
"""
import base64
import hashlib
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..db import db, query_all, query_one


STOCK_CODE_RE = re.compile(r"(?<!\d)([0-9]{6})(?!\d)")
UPLOAD_ROOT = Path(os.environ.get("STOCK_UPLOAD_ROOT", "/opt/stock-analyzer/data/uploads"))
IMAGE_MAX_BYTES = 2_500_000


def _ensure_tables():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS holding_change_submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            trade_id INTEGER,
            position_id INTEGER,
            stock_code TEXT NOT NULL,
            stock_name TEXT,
            trade_date TEXT NOT NULL,
            trade_type TEXT NOT NULL,
            price REAL NOT NULL,
            quantity INTEGER NOT NULL,
            fee REAL DEFAULT 0,
            source TEXT DEFAULT 'manual',
            note TEXT,
            before_qty INTEGER,
            after_qty INTEGER,
            before_cost REAL,
            after_cost REAL,
            status TEXT DEFAULT 'applied',
            error_msg TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_holding_changes_code_time
          ON holding_change_submissions(stock_code, submitted_at DESC);
        CREATE INDEX IF NOT EXISTS idx_holding_changes_trade_date
          ON holding_change_submissions(trade_date DESC, id DESC);

        CREATE TABLE IF NOT EXISTS holding_change_image_uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            original_filename TEXT,
            mime_type TEXT,
            file_path TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            width INTEGER,
            height INTEGER,
            stock_code TEXT,
            stock_name TEXT,
            trade_date TEXT,
            trade_type TEXT,
            price REAL,
            quantity INTEGER,
            fee REAL DEFAULT 0,
            note TEXT,
            extracted_text TEXT,
            submission_id INTEGER,
            status TEXT DEFAULT 'saved',
            error_msg TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_holding_image_uploads_time
          ON holding_change_image_uploads(uploaded_at DESC);
        CREATE INDEX IF NOT EXISTS idx_holding_image_uploads_code_time
          ON holding_change_image_uploads(stock_code, uploaded_at DESC);
        """)


def _to_code(value) -> str:
    m = STOCK_CODE_RE.search(str(value or ""))
    return m.group(1) if m else ""


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _clean_trade_type(value) -> str:
    text = str(value or "").strip().lower()
    if text in {"buy", "b", "买", "买入", "加仓"}:
        return "buy"
    if text in {"sell", "s", "卖", "卖出", "减仓", "清仓"}:
        return "sell"
    raise ValueError("trade_type 只能是 buy/sell")



def _normalize_name(value) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s+", "", text)
    return text.replace("Ａ", "A").replace("Ｂ", "B").replace("Ｈ", "H")


def _lookup_code_by_name(name: str) -> str:
    """按股票名称反查代码，优先精确匹配。"""
    raw = str(name or "").strip()
    if not raw:
        return ""
    code = _to_code(raw)
    if code:
        return code
    norm = _normalize_name(raw)

    for table in ("positions", "watchlist", "interaction_stocks"):
        try:
            rows = query_all(
                f"SELECT stock_code, stock_name FROM {table} WHERE stock_name IS NOT NULL LIMIT 10000"
            )
            for row in rows:
                if _normalize_name(row.get("stock_name")) == norm and row.get("stock_code"):
                    return _to_code(row.get("stock_code"))
        except Exception:
            pass

    try:
        rows = query_all(
            "SELECT symbol, ts_code, name FROM stock_universe WHERE name=? OR symbol=? OR ts_code=? LIMIT 5",
            (raw, raw, raw),
        )
        for row in rows:
            if row.get("symbol"):
                return _to_code(row.get("symbol"))
            if row.get("ts_code"):
                return _to_code(row.get("ts_code"))
    except Exception:
        pass

    try:
        rows = query_all(
            "SELECT symbol, ts_code, name FROM stock_universe WHERE name LIKE ? LIMIT 20",
            (f"%{raw}%",),
        )
        exact_norm = [r for r in rows if _normalize_name(r.get("name")) == norm]
        candidates = exact_norm or rows
        if len(candidates) == 1:
            row = candidates[0]
            return _to_code(row.get("symbol") or row.get("ts_code"))
    except Exception:
        pass
    return ""


def _lookup_stock_from_text(text: str) -> dict:
    """从用户备注/图片说明里找股票名；没有 OCR 时尤其有用。"""
    source = str(text or "").strip()
    if not source:
        return {"stock_code": "", "stock_name": ""}
    code = _to_code(source)
    if code:
        return {"stock_code": code, "stock_name": _lookup_name(code)}
    try:
        rows = query_all("SELECT symbol, name FROM stock_universe WHERE name IS NOT NULL")
        matches = []
        norm_source = _normalize_name(source)
        for row in rows:
            name = str(row.get("name") or "").strip()
            norm = _normalize_name(name)
            if len(norm) >= 2 and norm in norm_source:
                matches.append((len(norm), row.get("symbol"), name))
        if matches:
            matches.sort(reverse=True)
            _, symbol, name = matches[0]
            return {"stock_code": _to_code(symbol), "stock_name": name}
    except Exception:
        pass
    return {"stock_code": "", "stock_name": ""}

def _lookup_name(code: str, fallback: str = "") -> str:
    if fallback:
        return str(fallback).strip()[:80]
    for table in ("positions", "watchlist", "interaction_stocks"):
        try:
            row = query_one(
                f"SELECT stock_name FROM {table} WHERE stock_code=? AND stock_name IS NOT NULL LIMIT 1",
                (code,),
            )
            if row and row.get("stock_name"):
                return row["stock_name"]
        except Exception:
            pass
    try:
        suffix = "SH" if code.startswith(("6", "9")) else "SZ"
        row = query_one(
            "SELECT name FROM stock_universe WHERE symbol=? OR ts_code=? LIMIT 1",
            (code, f"{code}.{suffix}"),
        )
        if row and row.get("name"):
            return row["name"]
    except Exception:
        pass
    return ""


def _position_totals(c, position_id: int) -> dict:
    row = c.execute(
        """
        SELECT
          COALESCE(SUM(CASE WHEN trade_type='buy' THEN quantity ELSE -quantity END), 0) AS qty,
          COALESCE(SUM(CASE WHEN trade_type='buy' THEN price*quantity+fee ELSE -price*quantity+fee END), 0) AS cost
        FROM trades WHERE position_id=?
        """,
        (position_id,),
    ).fetchone()
    return {"qty": int(row["qty"] or 0), "cost": float(row["cost"] or 0)}


def _active_position(c, code: str) -> Optional[dict]:
    row = c.execute(
        "SELECT * FROM positions WHERE stock_code=? AND status='holding' ORDER BY id DESC LIMIT 1",
        (code,),
    ).fetchone()
    return dict(row) if row else None


def submit_holding_change(change: dict) -> dict:
    """提交一条持仓变动并同步写入 trades。"""
    _ensure_tables()
    name_hint = str(change.get("stock_name") or change.get("name") or "").strip()
    note_hint = str(change.get("note") or change.get("notes") or "").strip()
    code = _to_code(change.get("stock_code") or change.get("code"))
    if not code and name_hint:
        code = _lookup_code_by_name(name_hint)
    if not code and note_hint:
        found = _lookup_stock_from_text(note_hint)
        code = found.get("stock_code") or ""
        name_hint = name_hint or found.get("stock_name") or ""
    if not code:
        raise ValueError("未找到股票代码：可以只填股票名称，但名称需要能在股票库里唯一匹配")

    trade_type = _clean_trade_type(change.get("trade_type") or change.get("action"))
    trade_date = str(change.get("trade_date") or _today())[:10]
    price = float(change.get("price") or 0)
    quantity = int(float(change.get("quantity") or 0))
    fee = float(change.get("fee") or 0)
    if price <= 0:
        raise ValueError("价格必须大于 0")
    if quantity <= 0:
        raise ValueError("数量必须大于 0")

    source = str(change.get("source") or "manual")[:40]
    note = str(change.get("note") or change.get("notes") or "")[:800]
    name = _lookup_name(code, name_hint)

    with db() as c:
        pos = _active_position(c, code)
        if not pos:
            if trade_type == "sell":
                raise ValueError(f"{code} 当前没有 holding 持仓，不能登记卖出")
            cur = c.execute(
                "INSERT INTO positions(stock_code, stock_name, status, opened_at, notes) VALUES (?,?,?,?,?)",
                (code, name, "holding", trade_date, "由持仓变动入口自动创建"),
            )
            position_id = cur.lastrowid
            before = {"qty": 0, "cost": 0.0}
        else:
            position_id = pos["id"]
            before = _position_totals(c, position_id)
            if name and not pos.get("stock_name"):
                c.execute("UPDATE positions SET stock_name=? WHERE id=?", (name, position_id))

        if trade_type == "sell" and quantity > before["qty"]:
            raise ValueError(f"卖出数量 {quantity} 超过当前持仓 {before['qty']}")

        cur = c.execute(
            "INSERT INTO trades(position_id, trade_date, trade_type, price, quantity, fee, notes) "
            "VALUES (?,?,?,?,?,?,?)",
            (position_id, trade_date, trade_type, price, quantity, fee, note),
        )
        trade_id = cur.lastrowid
        after = _position_totals(c, position_id)

        if after["qty"] <= 0:
            c.execute(
                "UPDATE positions SET status='closed', closed_at=?, notes=COALESCE(notes,'') || ? WHERE id=?",
                (trade_date, "\n由持仓变动入口自动清仓", position_id),
            )
        else:
            c.execute("UPDATE positions SET status='holding', closed_at=NULL WHERE id=?", (position_id,))

        log_cur = c.execute(
            """
            INSERT INTO holding_change_submissions
            (trade_id, position_id, stock_code, stock_name, trade_date, trade_type,
             price, quantity, fee, source, note, before_qty, after_qty, before_cost, after_cost, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                trade_id, position_id, code, name, trade_date, trade_type,
                price, quantity, fee, source, note,
                before["qty"], after["qty"], before["cost"], after["cost"], "applied",
            ),
        )
        submission_id = log_cur.lastrowid

    return {
        "ok": True,
        "submission_id": submission_id,
        "trade_id": trade_id,
        "position_id": position_id,
        "stock_code": code,
        "stock_name": name,
        "trade_date": trade_date,
        "trade_type": trade_type,
        "price": price,
        "quantity": quantity,
        "before_qty": before["qty"],
        "after_qty": after["qty"],
        "before_cost": before["cost"],
        "after_cost": after["cost"],
        "position_status": "closed" if after["qty"] <= 0 else "holding",
    }


def submit_holding_changes(changes: list[dict]) -> dict:
    _ensure_tables()
    results = []
    errors = []
    for idx, change in enumerate(changes or [], start=1):
        try:
            results.append(submit_holding_change(change))
        except Exception as e:
            errors.append({"index": idx, "stock_code": change.get("stock_code") or change.get("code"), "error": str(e)})
    return {
        "ok": not errors,
        "applied": len(results),
        "failed": len(errors),
        "results": results,
        "errors": errors,
    }


def list_holding_changes(limit: int = 80, stock_code: str | None = None) -> dict:
    _ensure_tables()
    code = _to_code(stock_code) if stock_code else ""
    if code:
        rows = query_all(
            "SELECT * FROM holding_change_submissions WHERE stock_code=? ORDER BY trade_date DESC, id DESC LIMIT ?",
            (code, limit),
        )
    else:
        rows = query_all(
            "SELECT * FROM holding_change_submissions ORDER BY trade_date DESC, id DESC LIMIT ?",
            (limit,),
        )
    return {"count": len(rows), "items": rows}


def current_holdings_summary() -> dict:
    _ensure_tables()
    rows = query_all(
        """
        SELECT p.id, p.stock_code, p.stock_name, p.opened_at, p.notes,
          COALESCE(SUM(CASE WHEN t.trade_type='buy' THEN t.quantity ELSE -t.quantity END), 0) AS holding_qty,
          COALESCE(SUM(CASE WHEN t.trade_type='buy' THEN t.price*t.quantity+t.fee ELSE -price*quantity+fee END), 0) AS net_cost,
          COUNT(t.id) AS trades_count
        FROM positions p
        LEFT JOIN trades t ON t.position_id=p.id
        WHERE p.status='holding'
        GROUP BY p.id
        HAVING holding_qty > 0
        ORDER BY p.id DESC
        """
    )
    total_cost = 0.0
    for row in rows:
        qty = row.get("holding_qty") or 0
        cost = row.get("net_cost") or 0
        row["avg_cost"] = (cost / qty) if qty else None
        total_cost += float(cost or 0)
        snap = query_one(
            "SELECT trade_date, close, change_pct FROM daily_quotes WHERE stock_code=? ORDER BY trade_date DESC LIMIT 1",
            (row["stock_code"],),
        ) or {}
        row["latest_trade_date"] = snap.get("trade_date")
        row["latest_close"] = snap.get("close")
        row["latest_change_pct"] = snap.get("change_pct")
        if snap.get("close") is not None and qty:
            row["market_value"] = float(snap["close"]) * qty
            row["floating_return_pct"] = ((row["market_value"] - cost) / cost * 100) if cost else None
        else:
            row["market_value"] = None
            row["floating_return_pct"] = None
    account_snapshot = None
    try:
        account_snapshot = query_one(
            """
            SELECT id, snapshot_at, trade_date, total_market_value, withdrawable_cash,
                   available_cash, source, note
            FROM account_snapshots
            ORDER BY snapshot_at DESC, id DESC LIMIT 1
            """
        )
    except Exception:
        account_snapshot = None
    return {
        "count": len(rows),
        "total_cost": total_cost,
        "latest_account_snapshot": account_snapshot,
        "items": rows,
    }


def format_holding_changes_for_prompt(limit: int = 20) -> str:
    data = list_holding_changes(limit=limit)
    rows = data.get("items") or []
    if not rows:
        return "## 最近持仓变动\n暂无手动提交记录。"
    lines = ["## 最近持仓变动"]
    try:
        snap = current_holdings_summary().get("latest_account_snapshot") or {}
    except Exception:
        snap = {}
    if snap:
        lines.append(
            f"- 最新账户快照 {snap.get('snapshot_at')}: 总市值 {snap.get('total_market_value')}，"
            f"可用资金 {snap.get('available_cash')}，可取资金 {snap.get('withdrawable_cash')}。"
        )
    for r in rows[:limit]:
        action = "买入/加仓" if r.get("trade_type") == "buy" else "卖出/减仓"
        lines.append(
            f"- {r.get('trade_date')} {action} {r.get('stock_code')} {r.get('stock_name') or ''} "
            f"{r.get('quantity')}股 @ {r.get('price')}，仓位 {r.get('before_qty')} -> {r.get('after_qty')}。"
            f"{r.get('note') or ''}"
        )
    try:
        images = list_holding_change_images(limit=8).get("items") or []
    except Exception:
        images = []
    if images:
        lines.append("")
        lines.append("## 最近图片上传")
        for img in images:
            action = "买入/加仓" if img.get("trade_type") == "buy" else ("卖出/减仓" if img.get("trade_type") == "sell" else "待确认")
            lines.append(
                f"- {img.get('uploaded_at')} {action} {img.get('stock_code') or ''} {img.get('stock_name') or ''} "
                f"{img.get('quantity') or ''}股 @ {img.get('price') or ''}，状态 {img.get('status')}。{img.get('note') or img.get('extracted_text') or ''}"
            )
    return "\n".join(lines)


def _safe_filename(value: str) -> str:
    name = re.sub(r"[^0-9A-Za-z._-]+", "_", str(value or "upload.jpg")).strip("._")
    return name[:80] or "upload.jpg"


def _image_ext(mime_type: str) -> str:
    mime = str(mime_type or "").lower().split(";")[0].strip()
    if mime == "image/png":
        return ".png"
    if mime == "image/webp":
        return ".webp"
    if mime in {"image/jpeg", "image/jpg"}:
        return ".jpg"
    raise ValueError("只支持 JPG、PNG、WEBP 图片")


def save_holding_change_image(payload: dict) -> dict:
    """保存持仓/推荐截图，可选同步登记为一条持仓变动。"""
    _ensure_tables()
    mime_type = str(payload.get("mime_type") or "image/jpeg").split(";")[0].strip().lower()
    ext = _image_ext(mime_type)
    image_base64 = str(payload.get("image_base64") or "")
    if "," in image_base64[:80]:
        image_base64 = image_base64.split(",", 1)[1]
    try:
        raw = base64.b64decode(image_base64, validate=True)
    except Exception:
        raise ValueError("图片内容无法解析")
    if not raw:
        raise ValueError("图片为空")
    if len(raw) > IMAGE_MAX_BYTES:
        raise ValueError("图片压缩后仍超过 2.5MB，请裁剪或降低清晰度后再上传")

    digest = hashlib.sha256(raw).hexdigest()
    day = datetime.now().strftime("%Y%m%d")
    upload_dir = UPLOAD_ROOT / "holding-images" / day
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_filename(payload.get("filename") or f"holding_{day}{ext}")
    if not safe_name.lower().endswith(ext):
        safe_name += ext
    stored_name = f"{datetime.now().strftime('%H%M%S')}_{digest[:12]}_{safe_name}"
    file_path = upload_dir / stored_name
    file_path.write_bytes(raw)

    change = dict(payload.get("change") or {})
    code = _to_code(change.get("stock_code") or change.get("code") or payload.get("stock_code"))
    name = str(change.get("stock_name") or change.get("name") or payload.get("stock_name") or "")[:80]
    trade_date = str(change.get("trade_date") or payload.get("trade_date") or "")[:10] or None
    raw_trade_type = change.get("trade_type") or change.get("action") or payload.get("trade_type")
    trade_type = None
    if raw_trade_type:
        try:
            trade_type = _clean_trade_type(raw_trade_type)
        except Exception:
            trade_type = str(raw_trade_type)[:20]
    price = change.get("price") if change.get("price") is not None else payload.get("price")
    quantity = change.get("quantity") if change.get("quantity") is not None else payload.get("quantity")
    fee = change.get("fee") if change.get("fee") is not None else payload.get("fee")
    note = str(payload.get("note") or change.get("note") or "")[:1000]
    extracted_text = str(payload.get("extracted_text") or "")[:4000]
    if not code and name:
        code = _lookup_code_by_name(name)
    if not code and (note or extracted_text):
        found = _lookup_stock_from_text("\n".join([note, extracted_text]))
        code = found.get("stock_code") or ""
        name = name or found.get("stock_name") or ""
    if code and not name:
        name = _lookup_name(code)
    width = int(payload.get("width") or 0) or None
    height = int(payload.get("height") or 0) or None
    apply_change = bool(payload.get("apply_change"))

    submission = None
    status = "saved"
    error_msg = None
    if apply_change:
        if not code:
            status = "saved_pending_code"
            error_msg = "图片已保存；因为没有股票代码且名称未唯一匹配，等待补充股票名称/代码"
        else:
            try:
                apply_payload = {
                    "stock_code": code,
                    "stock_name": name,
                    "trade_type": trade_type,
                    "trade_date": trade_date,
                    "price": price,
                    "quantity": quantity,
                    "fee": fee or 0,
                    "note": (note + f"\n图片凭证: {file_path.name}").strip(),
                    "source": "holding_image",
                }
                submission = submit_holding_change(apply_payload)
                status = "applied"
            except Exception as e:
                status = "saved_with_apply_error"
                error_msg = str(e)

    with db() as c:
        cur = c.execute(
            """
            INSERT INTO holding_change_image_uploads
            (original_filename, mime_type, file_path, file_size, sha256, width, height,
             stock_code, stock_name, trade_date, trade_type, price, quantity, fee,
             note, extracted_text, submission_id, status, error_msg)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                safe_name, mime_type, str(file_path), len(raw), digest, width, height,
                code, name, trade_date, trade_type,
                float(price) if price not in (None, "") else None,
                int(float(quantity)) if quantity not in (None, "") else None,
                float(fee or 0), note, extracted_text,
                submission.get("submission_id") if submission else None,
                status, error_msg,
            ),
        )
        upload_id = cur.lastrowid

    return {
        "ok": status in {"saved", "applied", "saved_pending_code"},
        "id": upload_id,
        "status": status,
        "error_msg": error_msg,
        "stock_code": code,
        "stock_name": name,
        "image_url": f"/api/holding-changes/images/{upload_id}/file",
        "file_size": len(raw),
        "sha256": digest,
        "submission": submission,
    }


def list_holding_change_images(limit: int = 50, stock_code: str | None = None) -> dict:
    _ensure_tables()
    limit = max(1, min(int(limit or 50), 200))
    code = _to_code(stock_code) if stock_code else ""
    if code:
        rows = query_all(
            """
            SELECT id, uploaded_at, original_filename, mime_type, file_size, width, height,
                   stock_code, stock_name, trade_date, trade_type, price, quantity, fee,
                   note, extracted_text, submission_id, status, error_msg
            FROM holding_change_image_uploads
            WHERE stock_code=?
            ORDER BY uploaded_at DESC, id DESC LIMIT ?
            """,
            (code, limit),
        )
    else:
        rows = query_all(
            """
            SELECT id, uploaded_at, original_filename, mime_type, file_size, width, height,
                   stock_code, stock_name, trade_date, trade_type, price, quantity, fee,
                   note, extracted_text, submission_id, status, error_msg
            FROM holding_change_image_uploads
            ORDER BY uploaded_at DESC, id DESC LIMIT ?
            """,
            (limit,),
        )
    for row in rows:
        row["image_url"] = f"/api/holding-changes/images/{row['id']}/file"
    return {"count": len(rows), "items": rows}


def get_holding_change_image_file(image_id: int) -> dict:
    _ensure_tables()
    row = query_one(
        "SELECT id, mime_type, file_path, original_filename FROM holding_change_image_uploads WHERE id=?",
        (int(image_id),),
    )
    if not row:
        raise FileNotFoundError("图片不存在")
    path = Path(row["file_path"])
    if not path.exists():
        raise FileNotFoundError("图片文件已不存在")
    return {
        "content": path.read_bytes(),
        "mime_type": row.get("mime_type") or "image/jpeg",
        "filename": row.get("original_filename") or path.name,
    }
