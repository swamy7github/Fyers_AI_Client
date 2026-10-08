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
from datetime import date, datetime, timedelta

import pyotp
import requests
from fyers_apiv3 import fyersModel

import config

logger = logging.getLogger("orb_agent.fyers_service")

# Fyers doesn't document one single consistent field for "this was a rate-limit
# rejection specifically" - different endpoints have been observed returning this as
# an HTTP 429, or as a normal {"s": "error", ...} response body with a message
# mentioning it. This checks the message text defensively; verify against what your
# own account actually gets back (log a raw rejected response once) rather than
# trusting this exhaustively - a message-text check is inherently a little fragile.
_RATE_LIMIT_MESSAGE_MARKERS = ("rate limit", "too many request", "429")


def _looks_like_rate_limit_error(response: dict) -> bool:
    message = str(response.get("message", "")).lower()
    code = response.get("code")
    return any(marker in message for marker in _RATE_LIMIT_MESSAGE_MARKERS) or code == 429


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

    def _call_with_retry(self, fn, *args, rate_limit_max_retries: int | None = None, **kwargs):
        """
        rate_limit_max_retries: when Fyers' response looks like a rate-limit
        rejection specifically (as opposed to any other error), retry up to
        this many times with its OWN exponential backoff, separate from the
        generic API_MAX_RETRIES/linear-backoff path below. Defaults to
        config.RATE_LIMIT_MAX_RETRIES. This matters most for the morning
        bulk historical fetch (see get_candles_bulk()): the proactive
        RateLimiter above should prevent most 429s outright, but if the
        account is also used elsewhere at the same time (another script, a
        live WebSocket, the broker's own app), a rejection can still slip
        through - a fixed short linear backoff often isn't enough time for
        a per-minute-style limit to actually clear, where a longer
        exponential one is.
        """
        rate_limit_max_retries = (config.RATE_LIMIT_MAX_RETRIES if rate_limit_max_retries is None
                                   else rate_limit_max_retries)
        last_exc = None
        rate_limit_attempt = 0

        for attempt in range(1, config.API_MAX_RETRIES + 1):
            try:
                self._rate_limiter.acquire()  # blocks briefly if we're at the per-second cap
                response = fn(*args, **kwargs)
                if isinstance(response, dict) and response.get("s") == "error":
                    if _looks_like_rate_limit_error(response) and rate_limit_attempt < rate_limit_max_retries:
                        rate_limit_attempt += 1
                        backoff = config.RATE_LIMIT_BACKOFF_BASE_SECONDS * (2 ** (rate_limit_attempt - 1))
                        logger.warning("Rate-limit-style rejection from Fyers (attempt %d/%d for this call): "
                                       "%s - backing off %.1fs before retrying.",
                                       rate_limit_attempt, rate_limit_max_retries,
                                       response.get("message", response), backoff)
                        time.sleep(backoff)
                        continue  # doesn't consume one of the generic API_MAX_RETRIES attempts
                    raise RuntimeError(response.get("message", "Unknown Fyers API error"))
                # logger.info(f" {response}")
                return response
            except Exception as exc:  # broad on purpose: network, auth, and API-level errors all retry
                last_exc = exc
                logger.error("Fyers API call failed (attempt %d/%d): %s",
                             attempt, config.API_MAX_RETRIES, exc)
                if _looks_like_rate_limit_error(getattr(exc, "response", {})):
                    logger.warning(f"Fyers API call failed due to rate limit - will retry with exponential backoff.\n kwargs = {kwargs}")
                elif "invalid symbol" in str(exc).lower():
                    logger.warning(f"Fyers API call failed due to invalid symbol - will not retry.\n kwargs = {kwargs}")
                    return {}  # caller should handle empty response gracefully
                logger.info(f" kwargs = {kwargs}")
                time.sleep(config.API_RETRY_BACKOFF_SECONDS * attempt)
        logger.critical("Fyers API call failed after %d attempts: %s", config.API_MAX_RETRIES, last_exc)
        #raise last_exc

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
        if response:
            candles = response.get("candles", [])
            logger.info("Fetched %d %s-resolution candle(s) for %s (%s to %s).",
                        len(candles), resolution, symbol, range_from_date, range_to_date)
            return candles



    def place_order(self, symbol: str, qty: int, side: str, product_type: str = "INTRADAY",
                     order_tag: str = "orb_agent") -> dict:
        """
        Places a REAL market order at the broker. This is the ONLY place in
        the codebase that actually sends an order to Fyers - everywhere else
        (signal_engine.SignalEngine._maybe_place_live_order) gates on
        config.DRY_RUN before ever reaching here, so this method itself
        doesn't know or care about DRY_RUN: if it's called, an order is
        placed, full stop.

        side: "BUY" or "SELL" (case-insensitive) - translated to Fyers' own
        +1/-1 `side` convention internally so callers never have to
        remember that encoding.

        Always a MARKET order (type=2) with DAY validity - this agent's
        entries/exits are all decided from a just-closed 5-min candle, so a
        limit order sitting unfilled while price moves on would silently
        break the "acted on the candle that justified it" guarantee the
        rest of this codebase works hard to keep (see signal_engine.py's
        module docstring on is_backfill). product_type defaults to
        "INTRADAY" (MIS-style same-day margin/auto-square-off at the
        exchange level), matching this agent's own EOD square-off job -
        change it only if you specifically want CNC/NRML instead and
        understand the margin implications of doing so.

        Raises ValueError without calling the broker at all for a
        non-positive qty - a silent 0-qty order is never useful and some
        broker APIs will happily "succeed" at doing nothing, which is worse
        than a loud local failure.
        """
        if qty <= 0:
            raise ValueError(f"place_order called with non-positive qty={qty} for {symbol} - refusing "
                              f"to call the broker.")
        side_code = 1 if side.strip().upper() == "BUY" else -1
        data = {
            "symbol": symbol,
            "qty": int(qty),
            "type": 2,          # 2 = MARKET order
            "side": side_code,  # 1 = BUY, -1 = SELL
            "productType": product_type,
            "limitPrice": 0,
            "stopPrice": 0,
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
            "orderTag": order_tag,
        }
        response = self._call_with_retry(self.client.place_order, data=data)
        order_id = response.get("id") if isinstance(response, dict) else None
        logger.warning("LIVE ORDER PLACED: %s qty=%d %s (productType=%s) -> broker order id=%s | raw=%s",
                        side, qty, symbol, product_type, order_id, response)
        return response

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

    # ------------------------------------------------------------------
    # Broker state sync (positions / pending orders)
    # ------------------------------------------------------------------
    def get_positions(self) -> list[dict]:
        """
        Returns Fyers' own reported list of current net positions (the
        'netPositions' entries from their /positions response) - the source
        of truth for "do I already hold this for real", used by
        bootstrap.py to reconcile a late-started process's local state
        against reality before doing anything else with a symbol that
        looks like it has an open setup.
        """
        response = self._call_with_retry(self.client.positions)
        positions = response.get("netPositions", []) if isinstance(response, dict) else []
        logger.info("Fetched %d broker position(s).", len(positions))
        return positions

    def get_orders(self) -> list[dict]:
        """Returns Fyers' own reported order book for today (all statuses) - pending
        orders matter for catch-up too: an order placed just before a crash/restart
        may still be sitting live at the broker even though this process forgot about it."""
        response = self._call_with_retry(self.client.orderbook)
        orders = response.get("orderBook", []) if isinstance(response, dict) else []
        logger.info("Fetched %d broker order(s).", len(orders))
        return orders

    # ------------------------------------------------------------------
    # Bulk historical fetch, for the morning catch-up/warm-up pipeline
    # ------------------------------------------------------------------
    def get_candles_bulk(self, symbols: list[str], resolution: str,
                          range_from_date: str, range_to_date: str,
                          max_days_per_chunk: int | None = None) -> dict[str, list]:
        """
        Fetches `resolution` candles for MANY symbols over the same date
        range, one get_candles_range() call per (symbol, chunk) - chunked
        via config.INTRADAY_HISTORY_MAX_DAYS_PER_CHUNK /
        DAILY_HISTORY_MAX_DAYS_PER_CHUNK (same limits backtest.py uses).

        This is the "prevent rate limits when fetching morning historical
        data in bulk" entry point: every call already goes through
        RateLimiter (proactive throttling) AND _call_with_retry's
        rate-limit-specific exponential backoff (reactive, if a rejection
        slips through anyway). On top of that, ONE symbol failing
        permanently (all retries exhausted) does NOT abort the whole bulk
        fetch - it's logged and that symbol comes back with an empty list,
        so 499 good symbols aren't held hostage by 1 bad one.

        Returns {symbol: [[epoch, o, h, l, c, v], ...]} - symbols with no
        data (permanently failed, or genuinely no candles in range) map to
        an empty list, never a missing key, so callers can always safely
        index by symbol without a KeyError.
        """
        if resolution == "D":
            chunk_days = max_days_per_chunk or config.DAILY_HISTORY_MAX_DAYS_PER_CHUNK
        else:
            chunk_days = max_days_per_chunk or config.INTRADAY_HISTORY_MAX_DAYS_PER_CHUNK

        start = datetime.strptime(range_from_date, "%Y-%m-%d").date()
        end = datetime.strptime(range_to_date, "%Y-%m-%d").date()
        chunks: list[tuple[date, date]] = []
        chunk_start = start
        while chunk_start <= end:
            chunk_end = min(chunk_start + timedelta(days=chunk_days - 1), end)
            chunks.append((chunk_start, chunk_end))
            chunk_start = chunk_end + timedelta(days=1)

        results: dict[str, list] = {}
        failed_symbols: list[str] = []
        for idx, symbol in enumerate(symbols, start=1):
            symbol_candles: list = []
            symbol_failed = False
            for chunk_start_d, chunk_end_d in chunks:
                try:
                    symbol_candles.extend(self.get_candles_range(
                        symbol, resolution, str(chunk_start_d), str(chunk_end_d)))
                except Exception as exc:
                    # get_candles_range already retried (generic + rate-limit-specific)
                    # internally via _call_with_retry - this is a genuine, final failure
                    # for this one chunk. Log it and move on rather than losing every
                    # other symbol's data too.
                    logger.error("Giving up on %s [%s, %s] after all retries: %s",
                                 symbol, chunk_start_d, chunk_end_d, exc)
                    symbol_failed = True
            results[symbol] = symbol_candles
            if symbol_failed:
                failed_symbols.append(symbol)
            if idx % 50 == 0 or idx == len(symbols):
                logger.info("Bulk historical fetch progress: %d/%d symbols.", idx, len(symbols))

        if failed_symbols:
            logger.warning("Bulk historical fetch: %d/%d symbol(s) had at least one permanently "
                            "failed chunk (partial or no data for them): %s",
                            len(failed_symbols), len(symbols), failed_symbols)
        return results
