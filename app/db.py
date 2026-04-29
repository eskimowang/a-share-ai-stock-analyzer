"""SQLite 数据库访问层。"""
import sqlite3
from contextlib import contextmanager
from .config import CONFIG


def _conn():
    # 不启用 PARSE_DECLTYPES，避免 "20260422" 格式的日期被自动转 date 失败
    c = sqlite3.connect(CONFIG["database"]["path"])
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


@contextmanager
def db():
    """用法: with db() as conn: conn.execute(...)"""
    conn = _conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def query_all(sql: str, params=()) -> list[dict]:
    with db() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def query_one(sql: str, params=()) -> dict | None:
    with db() as c:
        r = c.execute(sql, params).fetchone()
        return dict(r) if r else None


def execute(sql: str, params=()) -> int:
    """返回 lastrowid（insert）或受影响行数。"""
    with db() as c:
        cur = c.execute(sql, params)
        return cur.lastrowid or cur.rowcount
