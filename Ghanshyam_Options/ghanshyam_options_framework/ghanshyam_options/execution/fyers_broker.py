"""LIVE broker adapter over the existing ``FyersDataService.place_order``.

DISABLED BY DEFAULT. Three independent gates must all be open before a real
order is sent:

1. ``live.enabled: true`` in settings.yaml,
2. the CLI flag ``--i-understand-live-risk`` (passed through as ``armed=True``),
3. the environment variable ``GHANSHYAM_LIVE=1``.

If any gate is closed the adapter raises :class:`LiveTradingDisabled` *before*
touching the client. Nothing in the test-suite constructs this class with an
armed state, and the client is only imported lazily inside :meth:`client`.

The existing order method we map to (``fyersclient/fyers_client.py``)::

    FyersDataService.place_order(symbol: str, qty: int, side: str,
                                 product_type: str = "INTRADAY", order_tag: str = "orb_agent") -> dict

It always sends a MARKET / DAY order and returns the raw Fyers response
(``{"s": "ok", "id": "<order id>", ...}``).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Optional

from ..models import Fill
from .broker import Broker, ChargeModel, OrderRequest, OrderResult

logger = logging.getLogger("ghanshyam.fyers_broker")


class LiveTradingDisabled(RuntimeError):
    pass


class FyersBroker(Broker):
    name = "fyers_live"
    is_live = True

    def __init__(self, settings, *, armed: bool = False, client: Any = None):
        self.settings = settings
        self.armed = bool(armed)
        self._client = client
        self.charges = ChargeModel.from_settings(settings.get("execution.charges"))
        self.product_type = str(settings.get("live.product_type", "INTRADAY"))
        self.order_tag = str(settings.get("live.order_tag", "ghanshyam"))

    # -- gates --------------------------------------------------------------------
    def _check_gates(self) -> None:
        if not self.settings.live_enabled:
            raise LiveTradingDisabled("live.enabled is false in settings.yaml")
        if not self.armed:
            raise LiveTradingDisabled("live broker not armed: pass --i-understand-live-risk")
        if os.environ.get("GHANSHYAM_LIVE") != "1":
            raise LiveTradingDisabled("environment variable GHANSHYAM_LIVE=1 is required")

    @property
    def client(self) -> Any:
        self._check_gates()
        if self._client is None:
            from fyersclient.fyers_client import get_fyers_client  # lazy, never at import time

            self._client = get_fyers_client()
        if not hasattr(self._client, "place_order"):
            raise LiveTradingDisabled("existing client has no place_order method")
        return self._client

    # -- orders -------------------------------------------------------------------
    def _send(self, req: OrderRequest, side: str) -> OrderResult:
        self._check_gates()
        client = self.client
        try:
            resp = client.place_order(symbol=req.symbol, qty=int(req.qty), side=side,
                                      product_type=self.product_type, order_tag=self.order_tag)
        except Exception as exc:
            logger.error("LIVE order failed %s %s x%d: %s", side, req.symbol, req.qty, exc)
            return OrderResult(False, None, str(exc))
        ok = isinstance(resp, dict) and resp.get("s") == "ok"
        order_id = str(resp.get("id", "")) if isinstance(resp, dict) else ""
        if not ok:
            return OrderResult(False, None, f"broker rejected: {resp}", raw=resp if isinstance(resp, dict) else {})
        # Market orders: the fill price is not in the placement response; the
        # paper runner re-reads the quote. We record the reference price and let
        # the trade log note that the live fill price is approximate.
        price = float(req.reference_price)
        charges = self.charges.total(req.exchange, side, price * req.qty)
        fill = Fill(symbol=req.symbol, side=side, qty=req.qty, price=price, ts=req.ts or datetime.now(),
                    order_id=order_id, slippage=0.0, charges=charges)
        logger.warning("LIVE ORDER %s %s x%d placed, broker id=%s", side, req.symbol, req.qty, order_id)
        return OrderResult(True, fill, "placed", raw=resp)

    def buy(self, req: OrderRequest) -> OrderResult:
        return self._send(req, "BUY")

    def sell(self, req: OrderRequest) -> OrderResult:
        return self._send(req, "SELL")

    def positions(self) -> dict:
        try:
            return {p.get("symbol"): p for p in self.client.get_positions()}
        except LiveTradingDisabled:
            raise
        except Exception as exc:  # pragma: no cover - network
            logger.error("positions failed: %s", exc)
            return {}


def make_broker(settings, mode: str, armed: bool = False, client: Optional[Any] = None) -> Broker:
    """Factory used by the CLI: ``paper``/``backtest`` -> PaperBroker, ``live`` -> FyersBroker (gated)."""
    from .paper_broker import PaperBroker

    if mode == "live":
        broker = FyersBroker(settings, armed=armed, client=client)
        broker._check_gates()  # fail fast, before any data is pulled
        return broker
    return PaperBroker.from_settings(settings)
