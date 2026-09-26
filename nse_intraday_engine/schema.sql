-- ================================================================
-- NSE Intraday Trading & Backtesting Engine -- SQLite DDL
-- Executed automatically by DatabaseManager on startup (idempotent).
-- ================================================================

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Universe snapshot taken by NiftyScreener at the start of each session/day
CREATE TABLE IF NOT EXISTS universe_snapshot (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date      TEXT    NOT NULL,
    symbol          TEXT    NOT NULL,
    sector_index    TEXT    NOT NULL,
    avg_turnover    REAL,
    spread_pct      REAL,
    rank            INTEGER,
    created_at      TEXT    DEFAULT (datetime('now')),
    UNIQUE(trade_date, symbol)
);

-- Raw signals produced by SignalEngine, whether or not they were traded
CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date      TEXT    NOT NULL,
    "timestamp"     TEXT    NOT NULL,
    symbol          TEXT    NOT NULL,
    direction       TEXT    NOT NULL CHECK(direction IN ('LONG','SHORT')),
    trigger_price   REAL    NOT NULL,
    atr             REAL    NOT NULL,
    stock_rs        REAL,
    sector_rs       REAL,
    reason          TEXT,
    taken           INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    DEFAULT (datetime('now'))
);

-- Simulated order book (both PAPER_TRADING and BACKTEST write here)
CREATE TABLE IF NOT EXISTS orders (
    order_id        TEXT    PRIMARY KEY,
    "timestamp"     TEXT    NOT NULL,
    symbol          TEXT    NOT NULL,
    side            TEXT    NOT NULL CHECK(side IN ('BUY','SELL')),
    qty             INTEGER NOT NULL,
    order_type      TEXT    NOT NULL DEFAULT 'MARKET',
    requested_price REAL,
    executed_price  REAL,
    slippage_pct    REAL,
    status          TEXT    NOT NULL DEFAULT 'PENDING',
    mode            TEXT    NOT NULL,
    position_id     INTEGER,
    created_at      TEXT    DEFAULT (datetime('now'))
);

-- Open / closed positions
CREATE TABLE IF NOT EXISTS positions (
    position_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT    NOT NULL,
    side            TEXT    NOT NULL CHECK(side IN ('LONG','SHORT')),
    initial_qty     INTEGER NOT NULL,
    total_qty       INTEGER NOT NULL,
    avg_entry_price REAL    NOT NULL,
    atr_at_entry    REAL    NOT NULL,
    stop_loss       REAL    NOT NULL,
    target1         REAL    NOT NULL,
    target1_hit     INTEGER NOT NULL DEFAULT 0,
    breakeven_moved INTEGER NOT NULL DEFAULT 0,
    pyramided       INTEGER NOT NULL DEFAULT 0,
    status          TEXT    NOT NULL DEFAULT 'OPEN' CHECK(status IN ('OPEN','CLOSED')),
    mode            TEXT    NOT NULL,
    opened_at       TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    closed_at       TEXT
);

-- Completed (partial or full) trade fills -- the analytics source of truth
CREATE TABLE IF NOT EXISTS trades (
    trade_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id     INTEGER NOT NULL REFERENCES positions(position_id),
    symbol          TEXT    NOT NULL,
    side            TEXT    NOT NULL CHECK(side IN ('LONG','SHORT')),
    qty             INTEGER NOT NULL,
    entry_price     REAL    NOT NULL,
    exit_price      REAL    NOT NULL,
    entry_time      TEXT    NOT NULL,
    exit_time       TEXT    NOT NULL,
    gross_pnl       REAL    NOT NULL,
    charges         REAL    NOT NULL,
    net_pnl         REAL    NOT NULL,
    r_multiple      REAL,
    exit_reason     TEXT    NOT NULL,
    is_pyramid_leg  INTEGER NOT NULL DEFAULT 0,
    mode            TEXT    NOT NULL,
    created_at      TEXT    DEFAULT (datetime('now'))
);

-- End-of-day rollup used for the performance teardown & drawdown curve
CREATE TABLE IF NOT EXISTS daily_metrics (
    trade_date      TEXT    NOT NULL,
    mode            TEXT    NOT NULL,
    gross_pnl       REAL    NOT NULL DEFAULT 0,
    total_charges   REAL    NOT NULL DEFAULT 0,
    net_pnl         REAL    NOT NULL DEFAULT 0,
    num_trades      INTEGER NOT NULL DEFAULT 0,
    wins            INTEGER NOT NULL DEFAULT 0,
    losses          INTEGER NOT NULL DEFAULT 0,
    equity_close    REAL    NOT NULL,
    created_at      TEXT    DEFAULT (datetime('now')),
    PRIMARY KEY (trade_date, mode)
);

CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_exit_time ON trades(exit_time);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_orders_symbol ON orders(symbol);
