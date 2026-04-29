"""Stock Analyzer FastAPI 入口。"""
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional, List

from .config import CONFIG
from .db import db, query_all, query_one, execute
from .data_sources import UnifiedDataSource
from .api.routes import router as extended_router
from .scheduler import start_scheduler, stop_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler()
    yield
    stop_scheduler()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("stock-analyzer")

_enable_docs = os.environ.get("STOCK_ENABLE_DOCS", "0").lower() in {"1", "true", "yes"}
app = FastAPI(
    title="Stock Analyzer",
    version="0.2.0",
    lifespan=lifespan,
    docs_url="/docs" if _enable_docs else None,
    redoc_url="/redoc" if _enable_docs else None,
    openapi_url="/openapi.json" if _enable_docs else None,
)

_default_origins = "http://localhost:8000,http://127.0.0.1:8000"
_allowed_origins = [
    origin.strip().rstrip("/")
    for origin in os.environ.get("STOCK_ALLOWED_ORIGINS", _default_origins).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Admin-Token"],
)

# 静态资源（/vendor/tailwind.js 等本地打包依赖）
from fastapi.staticfiles import StaticFiles
import os as _os
_static_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "static")
if _os.path.exists(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")
    _vendor_dir = _os.path.join(_static_dir, "vendor")
    if _os.path.exists(_vendor_dir):
        app.mount("/vendor", StaticFiles(directory=_vendor_dir), name="vendor")

# 挂载扩展路由（/api/analyze, /api/strategy, /api/chat, /chat, /report）
app.include_router(extended_router)

# 数据源
_tushare_token = CONFIG.get("data_sources", {}).get("tushare", {}).get("token") or None
DS = UnifiedDataSource(tushare_token=_tushare_token)


# ========== Pydantic models ==========
class TradeIn(BaseModel):
    trade_date: str = Field(..., description="YYYY-MM-DD")
    trade_type: str = Field(..., pattern="^(buy|sell)$")
    price: float = Field(..., gt=0)
    quantity: int = Field(..., gt=0)
    fee: float = 0.0
    notes: Optional[str] = None


class PositionIn(BaseModel):
    stock_code: str
    stock_name: Optional[str] = None
    opened_at: str = Field(..., description="YYYY-MM-DD 首次买入日期")
    notes: Optional[str] = None
    trades: List[TradeIn] = []


# ========== 根路径 + 健康检查 ==========
@app.get("/")
def root():
    return {
        "app": "Stock Analyzer",
        "version": "0.1.0",
        "time": datetime.now().isoformat(),
        "endpoints": ["/health", "/api/stock/{code}/daily", "/api/stock/{code}/basics",
                      "/api/positions", "/api/watchlist"],
    }


@app.get("/health")
def health():
    # 顺便检查 DB
    with db() as c:
        n = c.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
    return {"status": "ok", "db": "connected", "positions_count": n}


# ========== 股票数据 ==========
@app.get("/api/stock/{code}/daily")
def stock_daily(code: str, start: str = "20260101", end: Optional[str] = None):
    """拉取日线（带缓存）。"""
    df, source = DS.get_daily(code, start=start, end=end)
    if df is None or df.empty:
        raise HTTPException(404, f"No data for {code}")
    return {
        "code": code,
        "source": source,
        "rows": len(df),
        "start": df["trade_date"].min(),
        "end": df["trade_date"].max(),
        "data": df.to_dict(orient="records"),
    }


@app.get("/api/stock/{code}/basics")
def stock_basics(code: str):
    """基本信息 + 实时行情（AKShare）。"""
    rt = DS.get_realtime(code)
    basics = {}
    if DS.tushare:
        try:
            basics = DS.tushare.get_basics(code)
        except Exception as e:
            log.warning(f"tushare basics fail: {e}")
    return {"code": code, "realtime": rt, "basics": basics}


# ========== 持仓 ==========
@app.get("/api/positions")
def list_positions(status: str = "holding"):
    rows = query_all(
        "SELECT p.*, "
        "  (SELECT SUM(CASE WHEN trade_type='buy' THEN quantity ELSE -quantity END) "
        "   FROM trades WHERE position_id=p.id) AS holding_qty, "
        "  (SELECT SUM(CASE WHEN trade_type='buy' THEN price*quantity+fee ELSE -price*quantity+fee END) "
        "   FROM trades WHERE position_id=p.id) AS net_cost "
        "FROM positions p WHERE status=? ORDER BY id DESC",
        (status,),
    )
    return {"count": len(rows), "items": rows}


@app.post("/api/positions")
def create_position(body: PositionIn):
    # 插入 position
    with db() as c:
        cur = c.execute(
            "INSERT INTO positions(stock_code, stock_name, opened_at, notes) VALUES (?,?,?,?)",
            (body.stock_code, body.stock_name, body.opened_at, body.notes),
        )
        pid = cur.lastrowid
        # 批量插入 trades
        for t in body.trades:
            c.execute(
                "INSERT INTO trades(position_id, trade_date, trade_type, price, quantity, fee, notes) "
                "VALUES (?,?,?,?,?,?,?)",
                (pid, t.trade_date, t.trade_type, t.price, t.quantity, t.fee, t.notes),
            )
    return {"id": pid, "trades_added": len(body.trades)}


@app.get("/api/positions/{pid}/trades")
def list_trades(pid: int):
    return query_all(
        "SELECT * FROM trades WHERE position_id=? ORDER BY trade_date, id",
        (pid,),
    )


# ========== 自选 ==========
@app.get("/api/watchlist")
def list_watchlist():
    return query_all("SELECT * FROM watchlist ORDER BY priority DESC, added_at DESC")


class WatchIn(BaseModel):
    stock_code: str
    stock_name: Optional[str] = None
    reason: Optional[str] = None
    priority: int = 0


@app.post("/api/watchlist")
def add_watchlist(body: WatchIn):
    try:
        rid = execute(
            "INSERT INTO watchlist(stock_code, stock_name, reason, priority) VALUES (?,?,?,?)",
            (body.stock_code, body.stock_name, body.reason, body.priority),
        )
        return {"id": rid}
    except Exception as e:
        raise HTTPException(400, f"已存在或数据库错误: {e}")
