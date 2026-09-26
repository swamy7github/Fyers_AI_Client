"""
fyers_service.py
-----------------
Thin, defensive wrapper around the Fyers API v3 SDK (`fyers-apiv3`).

Two responsibilities are kept separate:

  1. FyersAuthManager  - obtains a valid access token, either automatically
     (TOTP-based headless login) or by prompting you to paste the auth code
     from a manual, one-time browser login.

  2. FyersDataService  - the only place in the codebase that actually talks
     to Fyers for market data (quotes / historical candles), with retries
     and structured logging around every call, per the brief's requirement
     to "include logging to track successful API calls and connection
     errors."

IMPORTANT - read before relying on the automated login
--------------------------------------------------------
`generate_access_token_auto()` replicates the multi-step login sequence
Fyers' own web login page uses internally (send OTP -> verify OTP -> verify
PIN -> exchange for an auth code). This is a widely used community pattern
for headless logins to a **retail trader's own account**, but it is not an
officially published, versioned API - Fyers can change their login page at
any time and silently break it. Treat it as best-effort automation:

  - If it breaks, `get_access_token()` automatically falls back to
    `generate_access_token_manual()`, which uses Fyers' officially
    documented auth-code flow (you log in once in a browser and paste a
    code back into the terminal).
  - Re-verify this flow yourself against Fyers' current login page /
    official docs before depending on it for unattended daily runs.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import datetime, timedelta

import pyotp
import requests
from fyers_apiv3 import fyersModel

import config

logger = logging.getLogger("orb_agent.fyers_service")


class RateLimiter:
    """
    Thread-safe sliding-window limiter: blocks (sleeps) as needed so that no
    more than `max_calls_per_second` calls go out in any rolling 1-second
    window, across ALL Fyers calls made through FyersDataService.

    Fyers enforces a per-second cap on API calls (confirm the exact current
    number for your account/plan in their API dashboard/docs - it can vary
    by endpoint category and subscription). config.FYERS_MAX_CALLS_PER_SECOND
    is intentionally set a little under the documented ceiling to leave
    headroom for clock jitter and any calls made outside this codebase
    (e.g. if you also have the Fyers WebSocket or another script running
    against the same API key).
    """

    def __init__(self, max_calls_per_second: int):
        self.max_calls_per_second = max_calls_per_second
        self._lock = threading.Lock()
        self._call_times: deque[float] = deque()

    def acquire(self):
        with self._lock:
            while True:
                now = time.monotonic()
                # drop timestamps older than the 1-second window
                while self._call_times and now - self._call_times[0] >= 1.0:
                    self._call_times.popleft()

                if len(self._call_times) < self.max_calls_per_second:
                    self._call_times.append(now)
                    return

                sleep_for = 1.0 - (now - self._call_times[0])
                if sleep_for > 0:
                    time.sleep(sleep_for)
                # loop again: re-check the window after sleeping


class FyersAuthManager:
    """Handles everything needed to get a usable Fyers access token."""

    LOGIN_BASE_URL = "https://api-t2.fyers.in/vagator/v2"
    TOKEN_URL = "https://api.fyers.in/api/v2/token"

    def __init__(self):
        self.app_id = config.FYERS_APP_ID
        self.secret_key = config.FYERS_SECRET_KEY
        self.redirect_uri = config.FYERS_REDIRECT_URI

    # ------------------------------------------------------------------
    # Option A (for unattended daily runs): TOTP auto login
    # ------------------------------------------------------------------
    def generate_access_token_auto(self) -> str:
        """Headless login using fy_id + PIN + TOTP secret. Returns an access token."""
        session = requests.Session()

        # Step 1: request a login OTP challenge
        r1 = session.post(f"{self.LOGIN_BASE_URL}/send_login_otp",
                           json={"fy_id": config.FYERS_FY_ID, "app_id": "2"})
        r1.raise_for_status()
        request_key = r1.json()["request_key"]

        # Step 2: answer it with a freshly generated TOTP
        totp_code = pyotp.TOTP(config.FYERS_TOTP_SECRET).now()
        r2 = session.post(f"{self.LOGIN_BASE_URL}/verify_otp",
                           json={"request_key": request_key, "otp": totp_code})
        r2.raise_for_status()
        request_key_2 = r2.json()["request_key"]

        # Step 3: verify the 4-digit trading PIN
        r3 = session.post(f"{self.LOGIN_BASE_URL}/verify_pin",
                           json={"request_key": request_key_2, "identity_type": "pin",
                                 "identifier": config.FYERS_PIN})
        r3.raise_for_status()
        step3_token = r3.json()["data"]["access_token"]

        # Step 4: exchange that session for an auth_code scoped to YOUR app
        headers = {"Authorization": f"Bearer {step3_token}"}
        payload = {
            "fyers_id": config.FYERS_FY_ID,
            "app_id": self.app_id.split("-")[0],
            "redirect_uri": self.redirect_uri,
            "appType": "100",
            "code_challenge": "",
            "state": "orb_agent",
            "scope": "",
            "nonce": "",
            "response_type": "code",
            "create_cookie": True,
        }
        r4 = session.post(self.TOKEN_URL, json=payload, headers=headers)
        r4.raise_for_status()
        auth_code = r4.json()["Url"].split("auth_code=")[1].split("&")[0]

        # Step 5: exchange the auth_code for a real access token via the SDK
        session_model = fyersModel.SessionModel(
            client_id=self.app_id,
            secret_key=self.secret_key,
            redirect_uri=self.redirect_uri,
            response_type="code",
            grant_type="authorization_code",
        )
        session_model.set_token(auth_code)
        token_response = session_model.generate_token()
        access_token = token_response["access_token"]
        logger.info("Fyers access token generated via automated TOTP login.")
        return access_token

    # ------------------------------------------------------------------
    # Option B (fallback): manual, one-time browser login
    # ------------------------------------------------------------------
    def generate_access_token_manual(self) -> str:
        session_model = fyersModel.SessionModel(
            client_id=self.app_id,
            secret_key=self.secret_key,
            redirect_uri=self.redirect_uri,
            response_type="code",
            grant_type="authorization_code",
        )
        print("1. Open this URL in a browser and log in to your Fyers account:")
        print(session_model.generate_authcode())
        auth_code = input("2. Paste the 'auth_code' value from the redirected URL here: ").strip()
        session_model.set_token(auth_code)
        token_response = session_model.generate_token()
        access_token = token_response["access_token"]
        logger.info("Fyers access token generated via manual login.")
        return access_token

    def get_access_token(self) -> str:
        """Tries the automated flow first (if credentials are present), else manual."""
        if all([config.FYERS_FY_ID, config.FYERS_PIN, config.FYERS_TOTP_SECRET]):
            try:
                return self.generate_access_token_auto()
            except Exception as exc:
                logger.warning("Automated TOTP login failed (%s). Falling back to manual login.", exc)
        return self.generate_access_token_manual()


class FyersDataService:
    """All market-data reads go through this class so retries/logging live in one place."""

    def __init__(self, access_token: str):
        self.client = fyersModel.FyersModel(
            client_id=config.FYERS_APP_ID,
            is_async=False,
            token=access_token,
            log_path=config.LOG_DIR,
        )
        self._rate_limiter = RateLimiter(config.FYERS_MAX_CALLS_PER_SECOND)

    def _call_with_retry(self, fn, *args, **kwargs):
        last_exc = None
        for attempt in range(1, config.API_MAX_RETRIES + 1):
            try:
                self._rate_limiter.acquire()  # blocks briefly if we're at the per-second cap
                response = fn(*args, **kwargs)
                if isinstance(response, dict) and response.get("s") == "error":
                    raise RuntimeError(response.get("message", "Unknown Fyers API error"))
                # logger.info(f" {response}")
                return response
            except Exception as exc:  # broad on purpose: network, auth, and API-level errors all retry
                last_exc = exc
                logger.error("Fyers API call failed (attempt %d/%d): %s",
                             attempt, config.API_MAX_RETRIES, exc)
                time.sleep(config.API_RETRY_BACKOFF_SECONDS * attempt)
        logger.critical("Fyers API call failed after %d attempts: %s", config.API_MAX_RETRIES, last_exc)
        raise last_exc

    def get_quotes(self, symbols: list[str]) -> dict:
        """symbols like ['NSE:SBIN-EQ', 'NSE:IDEA-EQ']. Caller must respect Fyers' per-request symbol cap."""
        data = {"symbols": ",".join(symbols)}
        response = self._call_with_retry(self.client.quotes, data=data)
        logger.info("Fetched quotes for %d symbol(s).", len(symbols))
        return response

    def get_quotes_batched(self, symbols: list[str]) -> dict[str, dict]:
        """
        Fetches quotes for an arbitrarily large symbol list by chunking it
        into config.QUOTES_BATCH_SIZE-sized requests (each of which still
        goes through the shared rate limiter). Returns a flat
        {symbol: quote_dict} lookup merged across all batches.

        This is the low-call-count path: use it instead of looping
        get_5min_candle()/get_previous_close() per symbol whenever a
        quotes-derived OHLCV proxy is good enough (see
        opening_range_scanner.py for exactly when that's true).
        """
        merged: dict[str, dict] = {}
        for i in range(0, len(symbols), config.QUOTES_BATCH_SIZE):
            batch = symbols[i:i + config.QUOTES_BATCH_SIZE]
            response = self.get_quotes(batch)
            for item in response.get("d", []):
                merged[item["n"]] = item.get("v", {})
        logger.info("Fetched batched quotes for %d symbol(s) across %d request(s).",
                    len(symbols), (len(symbols) + config.QUOTES_BATCH_SIZE - 1) // config.QUOTES_BATCH_SIZE)
        return merged

    def get_5min_candle(self, symbol: str, date_str: str, from_epoch: int, to_epoch: int) -> list:
        """Returns the raw candle list [[epoch, o, h, l, c, v], ...] for one 5-min window."""
        data = {
            "symbol": symbol,
            "resolution": "5",
            "date_format": "0",      # 0 = epoch timestamps in range_from/range_to
            "range_from": str(from_epoch),
            "range_to": str(to_epoch),
            "cont_flag": "1",
        }
        response = self._call_with_retry(self.client.history, data=data)
        candles = response.get("candles", [])
        logger.info("Fetched %d x 5-min candle(s) for %s on %s.", len(candles), symbol, date_str)
        return candles

    def get_candles_range(self, symbol: str, resolution: str, range_from_date: str, range_to_date: str) -> list:
        """
        Generic historical range fetch using date-string range_from/range_to
        (YYYY-MM-DD, date_format=1) - works for both intraday resolutions
        ('5', '15', ...) and daily ('D'). Used by backtest.py to pull a
        symbol's full history for a date range in as few calls as possible.

        Fyers caps how far a single call can span - see
        config.INTRADAY_HISTORY_MAX_DAYS_PER_CHUNK and
        config.DAILY_HISTORY_MAX_DAYS_PER_CHUNK. This method does NOT chunk
        for you; callers requesting a longer range should split it
        themselves (backtest.py does this) - exceeding the limit here
        raises via _call_with_retry after Fyers returns an error response.
        """
        data = {
            "symbol": symbol, "resolution": resolution, "date_format": "1",
            "range_from": range_from_date, "range_to": range_to_date, "cont_flag": "1",
        }
        response = self._call_with_retry(self.client.history, data=data)
        candles = response.get("candles", [])
        logger.info("Fetched %d %s-resolution candle(s) for %s (%s to %s).",
                    len(candles), resolution, symbol, range_from_date, range_to_date)
        return candles

    def get_previous_close(self, symbol: str) -> float | None:
        """Fetches the previous trading day's daily close, used for gain% calculations."""
        data = {
            "symbol": symbol,
            "resolution": "D",
            "date_format": "1",      # 1 = yyyy-mm-dd strings in range_from/range_to
            "range_from": (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d"),
            "range_to": datetime.now().strftime("%Y-%m-%d"),
            "cont_flag": "1",
        }
        response = self._call_with_retry(self.client.history, data=data)
        candles = response.get("candles", [])
        if len(candles) < 2:
            logger.warning("Not enough daily candles to determine previous close for %s.", symbol)
            return None
        return candles[-2][4]  # candles[-1] is today (still forming); candles[-2] is the prior close
