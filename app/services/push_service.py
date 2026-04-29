"""Web Push 推送服务 —— 不依赖微信，直接推到手机锁屏。

VAPID 密钥:
  - 私钥: /opt/stock-analyzer/secrets/vapid_private.pem（0600，只读）
  - 公钥: /opt/stock-analyzer/secrets/vapid_public.txt（Base64URL）
用法:
  - 前端调用 /api/push/key 拿公钥
  - 前端用 PushManager.subscribe({userVisibleOnly:true, applicationServerKey})
  - 前端 POST /api/push/subscribe 提交订阅
  - 系统产生通知时调 push_to_all(title, body, url)
"""
import json
import logging
import os
from typing import Optional

from ..db import db, execute, query_all

log = logging.getLogger(__name__)

_PRIV_PATH = "/opt/stock-analyzer/secrets/vapid_private.pem"
_PUB_PATH = "/opt/stock-analyzer/secrets/vapid_public.txt"


def _ensure_push_tables():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS push_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            endpoint TEXT UNIQUE,
            p256dh TEXT,
            auth TEXT,
            user_agent TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_used TIMESTAMP,
            active INTEGER DEFAULT 1
        );
        """)


def get_public_key() -> Optional[str]:
    if not os.path.exists(_PUB_PATH):
        return None
    with open(_PUB_PATH) as f:
        return f.read().strip()


def register_subscription(sub: dict, user_agent: str = "") -> int:
    """前端传来的 PushSubscription JSON。"""
    _ensure_push_tables()
    keys = sub.get("keys", {})
    endpoint = sub.get("endpoint", "")
    if not endpoint:
        raise ValueError("endpoint is required")
    # 用 INSERT OR REPLACE via endpoint UNIQUE
    with db() as c:
        cur = c.execute(
            "INSERT OR REPLACE INTO push_subscriptions(endpoint, p256dh, auth, user_agent, active) "
            "VALUES (?,?,?,?,1)",
            (endpoint, keys.get("p256dh"), keys.get("auth"), user_agent[:200]),
        )
        return cur.lastrowid


def list_active_subs() -> list[dict]:
    _ensure_push_tables()
    return query_all(
        "SELECT id, endpoint, p256dh, auth FROM push_subscriptions WHERE active=1"
    )


def push_to_all(title: str, body: str, url: str = "/chat",
                 tag: str = "stock", badge: Optional[str] = None) -> dict:
    """向所有活跃订阅推送。"""
    if not os.path.exists(_PRIV_PATH):
        log.warning("VAPID 私钥不存在，跳过 web push")
        return {"ok": False, "error": "vapid_not_configured"}
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        log.warning("pywebpush 未安装")
        return {"ok": False, "error": "pywebpush_not_installed"}

    subs = list_active_subs()
    if not subs:
        return {"ok": True, "sent": 0, "message": "无订阅"}

    with open(_PRIV_PATH) as f:
        priv_pem = f.read()

    payload = json.dumps({
        "title": title[:120],
        "body": body[:300],
        "url": url,
        "tag": tag,
    }, ensure_ascii=False)

    ok = fail = 0
    deactivate_ids = []
    for s in subs:
        try:
            webpush(
                subscription_info={
                    "endpoint": s["endpoint"],
                    "keys": {"p256dh": s["p256dh"], "auth": s["auth"]},
                },
                data=payload,
                vapid_private_key=priv_pem,
                vapid_claims={"sub": "mailto:stock@eskimo.wang"},
                ttl=3600,
            )
            ok += 1
            execute("UPDATE push_subscriptions SET last_used=CURRENT_TIMESTAMP WHERE id=?",
                    (s["id"],))
        except WebPushException as e:
            fail += 1
            # 410 Gone / 404 → 停用订阅
            if hasattr(e, "response") and e.response is not None and e.response.status_code in (404, 410):
                deactivate_ids.append(s["id"])
            log.warning(f"Web push 失败 sub#{s['id']}: {e}")
        except Exception as e:
            fail += 1
            log.warning(f"Web push 异常 sub#{s['id']}: {e}")

    for sid in deactivate_ids:
        execute("UPDATE push_subscriptions SET active=0 WHERE id=?", (sid,))

    return {"ok": True, "sent": ok, "failed": fail, "deactivated": len(deactivate_ids)}
