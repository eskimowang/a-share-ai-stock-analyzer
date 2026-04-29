'''初始化 SQLite 数据库。'''
import sqlite3
import os

DB_PATH = os.environ.get('STOCK_DB_PATH', os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data-cache', 'stock.db'))

SCHEMA = '''
-- 持仓（一只股票一条记录，同一只股票可持仓多次通过 closed 标记区分）
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code TEXT NOT NULL,      -- 600519.SH / 300244.SZ
    stock_name TEXT,
    status TEXT DEFAULT 'holding', -- holding / closed
    opened_at DATE NOT NULL,
    closed_at DATE,
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_positions_code ON positions(stock_code, status);

-- 交易记录（买入/卖出明细）
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER NOT NULL,
    trade_date DATE NOT NULL,
    trade_type TEXT NOT NULL,      -- buy / sell
    price REAL NOT NULL,
    quantity INTEGER NOT NULL,     -- 股数
    fee REAL DEFAULT 0,            -- 手续费
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (position_id) REFERENCES positions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_trades_position ON trades(position_id);

-- 自选股票池（关注但未持仓的）
CREATE TABLE IF NOT EXISTS watchlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code TEXT UNIQUE NOT NULL,
    stock_name TEXT,
    added_at DATE DEFAULT (date('now')),
    reason TEXT,
    priority INTEGER DEFAULT 0     -- 0 普通 / 1 重点关注
);

-- 每日行情缓存
CREATE TABLE IF NOT EXISTS daily_quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code TEXT NOT NULL,
    trade_date DATE NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume INTEGER, amount REAL,
    change_pct REAL,
    turnover_rate REAL,
    data_source TEXT,              -- tushare / akshare / baostock
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (stock_code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_quotes_code_date ON daily_quotes(stock_code, trade_date);

-- 每日分析结果
CREATE TABLE IF NOT EXISTS daily_analysis (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code TEXT NOT NULL,
    analysis_date DATE NOT NULL,
    close_price REAL,
    ma5 REAL, ma20 REAL, ma60 REAL, ma120 REAL,
    rsi_14 REAL,
    macd_dif REAL, macd_dea REAL, macd_bar REAL,
    volume_ratio REAL,
    pe_ttm REAL, pb REAL,
    ai_summary TEXT,
    signal TEXT,                   -- buy / sell / hold / alert / normal
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (stock_code, analysis_date)
);

-- 推送记录
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    channel TEXT,                  -- wechat / email
    title TEXT,
    content TEXT,
    status TEXT                    -- sent / failed
);

-- 财务数据快照（三大表+指标）
CREATE TABLE IF NOT EXISTS financials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code TEXT NOT NULL,
    report_period TEXT NOT NULL,   -- 2024-12-31 / 2025-03-31
    report_type TEXT,              -- annual / q1 / h1 / q3
    revenue REAL, net_profit REAL,
    total_assets REAL, total_liab REAL, equity REAL,
    operating_cashflow REAL,
    roe REAL, gross_margin REAL, net_margin REAL,
    data_source TEXT,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (stock_code, report_period)
);
'''

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(SCHEMA)
    print(f'Database initialized: {DB_PATH}')

if __name__ == '__main__':
    init_db()
