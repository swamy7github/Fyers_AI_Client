"""Central configuration for the Ghanshyam Options Trading Framework.

Everything that is likely to change (lot sizes, expiry rules, charges, risk
limits, strategy thresholds) lives here so the rest of the code stays free of
magic numbers.

IMPORTANT - verify before trading
    Lot sizes, expiry weekdays and statutory charges are set by NSE / MCX /
    SEBI / the government and change from time to time. The NSE defaults below
    were checked in October 2026 (Nifty lot 65, BankNifty lot 30, NSE index
    expiries on Tuesday, options STT 0.15% on sell-side premium). The MCX values
    were NOT verified. Confirm everything against the exchange contract master
    and your broker's charge sheet.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, time
from pathlib import Path
from typing import Dict, List, Optional

BASE_DIR: Path = Path(__file__).resolve().parent.parent
LOG_DIR: Path = BASE_DIR / "logs"
TRADES_LOG_PATH: Path = LOG_DIR / "trades_log.csv"
TIMEZONE: str = "Asia/Kolkata"
CANDLE_MINUTES: int = 5


# --------------------------------------------------------------------------- #
# Instruments
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class InstrumentConfig:
    """Static description of a tradable underlying and its option series."""

    name: str
    exchange: str                 # "NSE" or "MCX"
    underlying_symbol: str        # Fyers symbol used for the 5-min signal candles
    option_prefix: str            # Fyers option root, e.g. "NSE:NIFTY"
    lot_size: int
    strike_step: int
    session_open: time
    session_close: time
    square_off: time              # force-exit time for intraday positions
    expiry_weekday: int           # Monday=0 ... Sunday=6
    expiry_cycle: str             # "weekly" or "monthly"
    default_iv: float             # annualised vol used by the Black-Scholes pricer
    stt_sell_pct: float           # transaction tax on SELL-side premium (fraction)
    exchange_txn_pct: float       # exchange transaction charge on premium (fraction)
    expiry_time: time = time(15, 30)
    tick_size: float = 0.05

    def __post_init__(self) -> None:
        if self.expiry_cycle not in {"weekly", "monthly"}:
            raise ValueError(f"expiry_cycle must be weekly/monthly, got {self.expiry_cycle!r}")
        if self.lot_size <= 0 or self.strike_step <= 0:
            raise ValueError("lot_size and strike_step must be positive")


INSTRUMENTS: Dict[str, InstrumentConfig] = {
    "NIFTY": InstrumentConfig(
        name="NIFTY", exchange="NSE",
        underlying_symbol="NSE:NIFTY50-INDEX", option_prefix="NSE:NIFTY",
        lot_size=65, strike_step=50,
        session_open=time(9, 15), session_close=time(15, 30), square_off=time(15, 15),
        expiry_weekday=1, expiry_cycle="weekly", default_iv=0.14,
        stt_sell_pct=0.0015, exchange_txn_pct=0.0003503,
    ),
    "BANKNIFTY": InstrumentConfig(
        name="BANKNIFTY", exchange="NSE",
        underlying_symbol="NSE:NIFTYBANK-INDEX", option_prefix="NSE:BANKNIFTY",
        lot_size=30, strike_step=100,
        session_open=time(9, 15), session_close=time(15, 30), square_off=time(15, 15),
        expiry_weekday=1, expiry_cycle="monthly", default_iv=0.16,
        stt_sell_pct=0.0015, exchange_txn_pct=0.0003503,
    ),
    # MCX: the underlying is the near-month future, whose symbol rolls monthly.
    # "{yy}" / "{mon}" are filled in by core.strategy.resolve_underlying_symbol.
    # UNVERIFIED defaults: check lot sizes, strike steps, expiry dates and the
    # commodity transaction tax rate with MCX / your broker.
    "CRUDEOIL": InstrumentConfig(
        name="CRUDEOIL", exchange="MCX",
        underlying_symbol="MCX:CRUDEOIL{yy}{mon}FUT", option_prefix="MCX:CRUDEOIL",
        lot_size=100, strike_step=50,
        session_open=time(9, 0), session_close=time(23, 30), square_off=time(23, 15),
        expiry_weekday=3, expiry_cycle="monthly", default_iv=0.35,
        stt_sell_pct=0.0005, exchange_txn_pct=0.0003, expiry_time=time(23, 30),
    ),
    "NATURALGAS": InstrumentConfig(
        name="NATURALGAS", exchange="MCX",
        underlying_symbol="MCX:NATURALGAS{yy}{mon}FUT", option_prefix="MCX:NATURALGAS",
        lot_size=1250, strike_step=5,
        session_open=time(9, 0), session_close=time(23, 30), square_off=time(23, 15),
        expiry_weekday=3, expiry_cycle="monthly", default_iv=0.60,
        stt_sell_pct=0.0005, exchange_txn_pct=0.0003, expiry_time=time(23, 30),
    ),
}

# Explicit expiry calendars override the weekday rules. Use these for MCX (whose
# option expiries do not follow a simple weekday pattern) and for holiday shifts.
EXPIRY_OVERRIDES: Dict[str, List[date]] = {}

# Trading holidays used to shift an expiry back to the previous trading day.
HOLIDAYS: List[date] = []


def get_instrument(name: str) -> InstrumentConfig:
    """Return the instrument config for ``name`` (case-insensitive)."""
    key = name.strip().upper()
    if key not in INSTRUMENTS:
        raise KeyError(f"Unknown symbol {name!r}. Choose from: {', '.join(INSTRUMENTS)}")
    return INSTRUMENTS[key]


# --------------------------------------------------------------------------- #
# Strategy
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class StrategyConfig:
    """Thresholds for the Ghanshyam opening-range breakout setup.

    The publicly documented setup marks the high/low of the opening range at
    09:20 and buys a CE/PE on the side that breaks out. The original rules quote
    absolute index points (70-80 point stop, 300 point gap filter, 100-150 point
    move) from a period when BankNifty traded near 43,000. They are expressed
    here as a PERCENTAGE of the underlying so they scale with the index level.
    These are assumptions to tune through backtesting, not facts.
    """

    opening_range_candles: int = 1        # 1 x 5-min candle -> range closes at 09:20
    entry_window_minutes: int = 30        # only take breakouts early in the session
    confirm_on_close: bool = True         # require a 5-min CLOSE beyond the range
    max_gap_pct: float = 0.70             # skip day if open gaps more than this % (~300 pts)
    min_sl_pct: float = 0.18              # minimum stop distance in % of spot (~75 pts)
    large_range_pct: float = 0.45         # range wider than this -> use min stop (~200 pts)
    target_pct: float = 0.30              # target in % of spot (~125 pts)
    trail_to_cost_pct: float = 0.15       # move stop to cost after this favourable move
    stall_candles: int = 4                # candles after entry before stall check applies
    stall_min_progress_pct: float = 0.05  # exit if best move is below this after stall_candles
    strike_mode: str = "ATM"              # "ATM", "ITM" or "OTM"
    strike_steps: int = 0                 # steps away from ATM when mode is ITM/OTM
    use_vwap_filter: bool = False
    use_ema_filter: bool = False
    ema_fast: int = 9
    ema_slow: int = 21
    use_rsi_filter: bool = False
    rsi_period: int = 14
    rsi_bull: float = 50.0
    rsi_bear: float = 50.0

    def __post_init__(self) -> None:
        if self.strike_mode not in {"ATM", "ITM", "OTM"}:
            raise ValueError("strike_mode must be ATM, ITM or OTM")
        if self.opening_range_candles < 1:
            raise ValueError("opening_range_candles must be >= 1")


# --------------------------------------------------------------------------- #
# Risk and execution
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RiskConfig:
    """Position sizing and loss limits."""

    lots: int = 1
    max_lots: int = 10
    risk_per_trade_inr: Optional[float] = None   # if set, size lots from stop distance
    assumed_delta: float = 0.55                  # premium move per 1 point of underlying
    max_daily_loss_inr: float = 5000.0
    max_consecutive_losses: int = 2
    max_trades_per_day: int = 2


@dataclass(frozen=True)
class ExecutionConfig:
    """Virtual slippage and brokerage model. Verify against your broker's sheet."""

    slippage_pct: float = 0.005          # 0.5% of premium, adverse on both legs
    brokerage_flat_inr: float = 20.0     # per executed order
    brokerage_max_pct: float = 0.0003    # brokerage is min(flat, pct of turnover)
    sebi_pct: float = 0.000001           # Rs 10 per crore
    stamp_buy_pct: float = 0.00003       # buy side only
    gst_pct: float = 0.18                # on brokerage + exchange + SEBI charges


@dataclass(frozen=True)
class PaperConfig:
    """Real-time loop timing."""

    poll_seconds: float = 5.0            # LTP poll interval for intrabar stop checks
    candle_delay_seconds: float = 3.0    # wait after a candle closes before fetching it


def with_overrides(cfg, **changes):
    """Return a copy of a frozen config dataclass with non-None ``changes`` applied."""
    return replace(cfg, **{k: v for k, v in changes.items() if v is not None})
