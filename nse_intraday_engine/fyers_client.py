"""
FyersClient
===========
Thin wrapper around `fyers_apiv3` handling: one-time OAuth login, daily
access-token caching, historical candle retrieval and live quotes -- all
behind the shared retry/backoff + rate-limit utilities.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
from fyers_apiv3 import fyersModel

from utils import RateLimiter, is_between, now_ist, retry_with_backoff, setup_logger


class FyersAuthError(RuntimeError):
    pass


class FyersClient:
    def __init__(self, config):
        self.cfg = config
        self.log = setup_logger("fyers_client", config.LOG_DIR, config.LOG_LEVEL)
        self._limiter = RateLimiter(config.API_MIN_INTERVAL_SEC)
        self.client_id = f"{config.FYERS_APP_ID}-100"
        self.access_token = config.FYERS_ACCESS_TOKEN
        self.model: Optional[fyersModel.FyersModel] = None
        self._authenticate()

    # ------------------------------------------------------------------ auth
    def _token_cache_path(self) -> Path:
        return Path(self.cfg.FYERS_ACCESS_TOKEN_PATH)

    def _load_cached_token(self) -> Optional[str]:
        p = self._token_cache_path()
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text())
        except json.JSONDecodeError:
            return None
        if data.get("date") != datetime.now().strftime("%Y-%m-%d"):
            return None  # Fyers access tokens expire daily
        return data.get("access_token")

    def _cache_token(self, token: str) -> None:
        self._token_cache_path().write_text(json.dumps({
            "date": datetime.now().strftime("%Y-%m-%d"),
            "access_token": token,
        }))

    def _authenticate(self) -> None:
        #token = self._load_cached_token()
        #if not token:
        #    token = self._interactive_login()
        #    self._cache_token(token)
        #self.access_token = token
        self.model = fyersModel.FyersModel(
            client_id=self.client_id, token=self.access_token, is_async=False, log_path=self.cfg.LOG_DIR
        )

    def _interactive_login(self) -> str:
        """Standard, documented Fyers OAuth flow: open the auth URL, log in
        in the browser, then paste the `auth_code` query-param from the
        redirected URL back here. Run once per trading day; the resulting
        token is cached to FYERS_ACCESS_TOKEN_PATH for the rest of the day."""
        session = fyersModel.SessionModel(
            client_id=self.client_id,
            secret_key=self.cfg.FYERS_APP_SECRET,
            redirect_uri=self.cfg.FYERS_REDIRECT_URI,
            response_type="code",
            grant_type="authorization_code",
        )
        auth_url = session.generate_authcode()
        print(
            "\n[FyersClient] Open this URL, log in, then paste the 'auth_code' "
            f"query-param from the redirect URL below:\n{auth_url}\n"
        )
        auth_code = input("auth_code: ").strip()
        session.set_token(auth_code)
        response = session.generate_token()
        if "access_token" not in response:
            raise FyersAuthError(f"Fyers auth failed: {response}")
        return response["access_token"]

    # -------------------------------------------------------------- history
    @retry_with_backoff(exceptions=(requests.RequestException, ConnectionError))
    def get_history(self, symbol: str, resolution: str, date_from: str, date_to: str) -> pd.DataFrame:
        """`resolution`: "D" for daily candles, "5" for 5-minute intraday candles."""
        self._limiter.wait()
        payload = {
            "symbol": symbol,
            "resolution": resolution,
            "date_format": "1",
            "range_from": date_from,
            "range_to": date_to,
            "cont_flag": "1",
        }
        resp = self.model.history(payload)
        if resp.get("s") != "ok":
            self.log.warning("history() non-ok for %s: %s", symbol, resp)
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
        candles = resp.get("candles", [])
        df = pd.DataFrame(candles, columns=["epoch", "open", "high", "low", "close", "volume"])
        if df.empty:
            return df.assign(timestamp=[])[["timestamp", "open", "high", "low", "close", "volume"]]
        df["timestamp"] = pd.to_datetime(df["epoch"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        return df[["timestamp", "open", "high", "low", "close", "volume"]]

    # --------------------------------------------------------------- quotes
    @retry_with_backoff(exceptions=(requests.RequestException, ConnectionError))
    def get_quotes(self, symbols: list[str]) -> dict:
        """Batches up to 50 symbols per Fyers quotes call."""
        out: dict = {}
        for i in range(0, len(symbols), 50):
            chunk = symbols[i:i + 50]
            self._limiter.wait()
            resp = self.model.quotes({"symbols": ",".join(chunk)})
            if resp.get("s") != "ok":
                self.log.warning("quotes() non-ok: %s", resp)
                continue
            for row in resp.get("d", []):
                out[row["n"]] = row.get("v", {})
        return out

    @staticmethod
    def get_bid_ask_spread_pct(symbol: str, quote: dict) -> Optional[float]:
        bid, ask, ltp = quote.get("bid"), quote.get("ask"), quote.get("lp")
        if not (bid and ask and ltp):
            return None
        return (ask - bid) / ltp * 100

    def is_market_open(self) -> bool:
        ts = now_ist()
        return ts.weekday() < 5 and is_between(ts, "09:15", self.cfg.MARKET_CLOSE)
