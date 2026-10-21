"""Broker interface and the Indian F&O charge model.

Charges (per executed order, all parameterised in ``execution.charges``):

* brokerage: flat INR per order
* STT: ``stt_sell_pct`` of sell-side premium (options; 0.1 % since Oct-2024)
* exchange transaction charge: % of premium (NSE / MCX separately)
* SEBI turnover fee: % of premium
* stamp duty: ``stamp_duty_buy_pct`` of buy-side premium
* GST: ``gst_pct`` on brokerage + exchange + SEBI
* CTT (MCX only): ``ctt_sell_pct_mcx`` of sell-side premium

Verify every rate against your broker's contract note; they change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Mapping, Optional

from ..models import Exchange, Fill


@dataclass(frozen=True)
class ChargeModel:
    brokerage_per_order: float = 20.0
    stt_sell_pct: float = 0.001
    exchange_txn_pct_nse: float = 0.0003503
    exchange_txn_pct_mcx: float = 0.0005
    sebi_fee_pct: float = 0.000001
    stamp_duty_buy_pct: float = 0.00003
    gst_pct: float = 0.18
    ctt_sell_pct_mcx: float = 0.0005

    @classmethod
    def from_settings(cls, block: Optional[Mapping[str, Any]]) -> "ChargeModel":
        block = block or {}
        fields = {k: float(v) for k, v in block.items() if k in cls.__dataclass_fields__}
        return cls(**fields)

    def charges(self, exchange: Exchange, side: str, premium_turnover: float) -> Dict[str, float]:
        """Itemised charges for one order of total premium value ``premium_turnover`` (price x qty)."""
        side = side.upper()
        is_mcx = exchange == Exchange.MCX
        exch = premium_turnover * (self.exchange_txn_pct_mcx if is_mcx else self.exchange_txn_pct_nse)
        sebi = premium_turnover * self.sebi_fee_pct
        stt = premium_turnover * self.stt_sell_pct if (side == "SELL" and not is_mcx) else 0.0
        ctt = premium_turnover * self.ctt_sell_pct_mcx if (side == "SELL" and is_mcx) else 0.0
        stamp = premium_turnover * self.stamp_duty_buy_pct if side == "BUY" else 0.0
        gst = (self.brokerage_per_order + exch + sebi) * self.gst_pct
        items = {
            "brokerage": self.brokerage_per_order, "stt": stt, "ctt": ctt, "exchange": exch,
            "sebi": sebi, "stamp": stamp, "gst": gst,
        }
        items["total"] = round(sum(items.values()), 2)
        return items

    def total(self, exchange: Exchange, side: str, premium_turnover: float) -> float:
        return self.charges(exchange, side, premium_turnover)["total"]


@dataclass
class OrderRequest:
    symbol: str
    side: str                  # BUY / SELL
    qty: int
    exchange: Exchange
    ts: datetime
    reference_price: float     # LTP / bar price the engine expects to trade at
    tick_size: float = 0.05
    tag: str = ""
    bar_open: Optional[float] = None   # for next-bar-open fills
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class OrderResult:
    ok: bool
    fill: Optional[Fill]
    message: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


class Broker(ABC):
    """Minimal broker interface. Only long-option BUY-to-open / SELL-to-close is used."""

    name: str = "abstract"
    is_live: bool = False

    @abstractmethod
    def buy(self, req: OrderRequest) -> OrderResult:
        ...

    @abstractmethod
    def sell(self, req: OrderRequest) -> OrderResult:
        ...

    def positions(self) -> Dict[str, Any]:  # pragma: no cover - optional
        return {}

    def close(self) -> None:  # pragma: no cover - optional
        return None
