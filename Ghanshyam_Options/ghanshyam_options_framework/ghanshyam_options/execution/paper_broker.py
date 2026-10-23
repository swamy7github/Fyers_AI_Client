"""Simulated broker: deterministic fills with slippage and itemised charges.

Fill price = reference price (or next-bar open when supplied and
``fill_on == next_bar_open``) adjusted *against* us by ``slippage_pct`` of the
premium plus ``slippage_ticks`` ticks, rounded to the tick. Charges come from
:class:`ChargeModel`. A cash ledger tracks premium outlay so the active-tranche
rule can be verified from the broker side as well.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..models import Fill
from .broker import Broker, ChargeModel, OrderRequest, OrderResult

logger = logging.getLogger("ghanshyam.paper_broker")


def round_to_tick(price: float, tick: float) -> float:
    if tick <= 0:
        return round(price, 2)
    return round(round(price / tick) * tick, 4)


@dataclass
class PaperConfig:
    slippage_pct: float = 0.0025
    slippage_ticks: int = 1
    fill_on: str = "next_bar_open"   # next_bar_open | signal_close


class PaperBroker(Broker):
    name = "paper"
    is_live = False

    def __init__(self, charges: Optional[ChargeModel] = None, config: Optional[PaperConfig] = None,
                 starting_cash: float = 0.0):
        self.charges = charges or ChargeModel()
        self.config = config or PaperConfig()
        self.cash = float(starting_cash)
        self.fills: List[Fill] = []
        self._ids = itertools.count(1)
        self._open_qty: Dict[str, int] = {}

    @classmethod
    def from_settings(cls, settings, starting_cash: Optional[float] = None) -> "PaperBroker":
        ex = settings.section("execution")
        cfg = PaperConfig(
            slippage_pct=float(ex.get("slippage_pct", 0.0025)),
            slippage_ticks=int(ex.get("slippage_ticks", 1)),
            fill_on=str(ex.get("fill_on", "next_bar_open")),
        )
        return cls(ChargeModel.from_settings(ex.get("charges")), cfg,
                   starting_cash=settings.total_capital if starting_cash is None else starting_cash)

    # -- internals ----------------------------------------------------------------
    def _base_price(self, req: OrderRequest) -> float:
        if self.config.fill_on == "next_bar_open" and req.bar_open is not None:
            return float(req.bar_open)
        return float(req.reference_price)

    def _fill_price(self, req: OrderRequest, side: str) -> tuple[float, float]:
        base = self._base_price(req)
        slip = base * self.config.slippage_pct + self.config.slippage_ticks * req.tick_size
        price = base + slip if side == "BUY" else max(req.tick_size, base - slip)
        price = round_to_tick(price, req.tick_size)
        return price, abs(price - base)

    def _execute(self, req: OrderRequest, side: str) -> OrderResult:
        if req.qty <= 0:
            return OrderResult(False, None, f"non-positive qty {req.qty}")
        if side == "SELL" and self._open_qty.get(req.symbol, 0) < req.qty:
            return OrderResult(False, None, f"cannot sell {req.qty} of {req.symbol}: open qty {self._open_qty.get(req.symbol, 0)}")
        price, slip = self._fill_price(req, side)
        turnover = price * req.qty
        charges = self.charges.total(req.exchange, side, turnover)
        if side == "BUY":
            self.cash -= turnover + charges
            self._open_qty[req.symbol] = self._open_qty.get(req.symbol, 0) + req.qty
        else:
            self.cash += turnover - charges
            self._open_qty[req.symbol] -= req.qty
            if self._open_qty[req.symbol] == 0:
                del self._open_qty[req.symbol]
        fill = Fill(symbol=req.symbol, side=side, qty=req.qty, price=price, ts=req.ts,
                    order_id=f"PAPER-{next(self._ids):06d}", slippage=slip, charges=charges)
        self.fills.append(fill)
    
        logger.info(f"=======@ {req.symbol} = {price} {req.ts}===========")
        logger.info("PAPER %s %s x%d @ %.2f (slip %.2f, charges %.2f) %s", side, req.symbol, req.qty, price, slip, charges, req.tag)
        return OrderResult(True, fill, "filled")

    # -- Broker ---------------------------------------------------------------------
    def buy(self, req: OrderRequest) -> OrderResult:
        return self._execute(req, "BUY")

    def sell(self, req: OrderRequest) -> OrderResult:
        return self._execute(req, "SELL")

    def positions(self) -> Dict[str, Any]:
        return dict(self._open_qty)
