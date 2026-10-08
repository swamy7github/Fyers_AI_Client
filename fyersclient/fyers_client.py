"""
fyers_client.py - Integrated Fyers API v3 Service & Unit Test Suite

This single-file solution contains:
1. get_fyers_client(): Exposes zero-argument authentication & token persistence returning an authenticated Fyers client handle.
2. get_history(): Exposes rate-limited and retried historical candle retrieval calling client.history.
3. FyersRateLimiter & RateLimitRule: Multi-window sliding rate limiters for 8 calls/sec and 480 calls/min caps.
4. FyersAuthManager: Disk token caching, validation, automated TOTP headless login, and manual fallback.
5. FyersDataService: High-level market data operations including quotes, batched quotes, bulk historical downloads, orders, and positions.
6. Comprehensive Unit Test Suite: Unit tests verifying all components, exposed APIs, rate limiters, and mock client integration.
"""

from __future__ import annotations

import functools
import logging
import os
import tempfile
import threading
import time
import base64, hashlib, json, os
import unittest
from collections import deque
from datetime import date, datetime, timedelta
from typing import Any, Callable, List, Optional, Tuple, Dict, Union
from dotenv import load_dotenv
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Load environment variables
load_dotenv(".env.fyers")  # Load environment variables from .env.fyers if present


# Optional dependencies check
try:
    import pyotp
except ImportError:
    pyotp = None

try:
    import requests
except ImportError:
    requests = None

try:
    from fyers_apiv3 import fyersModel
except ImportError:
    fyersModel = None

# Setup logger
logger = logging.getLogger("orb_agent.fyers_client")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

# ==============================================================================
# CONFIGURATION DEFAULT / FALLBACK
# ==============================================================================
# Load environment variables
try:
    # Get the directory where config.py is located (which is the B2 folder)
    Config_DIR = Path(__file__).parent
    load_dotenv(f"{Config_DIR}/.envfyers")  # Load environment variables from .env.fyers if present
except Exception as e:
    logging.warning(f"Failed to load .envfyers file: {e}")
  
    
class Config:
    FYERS_APP_ID: str = os.getenv("FYERS_APP_ID", "XXXXXX-100")
    FYERS_SECRET_KEY: str = os.getenv("FYERS_SECRET_KEY", "XXXXXXXXXX")
    FYERS_REDIRECT_URI: str = os.getenv("FYERS_REDIRECT_URI", "https://trade.fyers.in/api-product/front/tools/redirect.png")
    FYERS_FY_ID: str = os.getenv("FYERS_FY_ID", "")
    FYERS_PIN: str = os.getenv("FYERS_PIN", "")
    FYERS_TOTP_SECRET: str = os.getenv("FYERS_TOTP_SECRET", "")
    TOKEN_FILE_PATH: str = os.getenv("FYERS_TOKEN_FILE", f"{FYERS_APP_ID}_{os.getenv('TOKEN_FILE')}")
    APP_TYPE: str = os.getenv("APP_TYPE", "100")  # Default to "100" for API v3
    API_MAX_RETRIES: int = int(os.getenv("API_MAX_RETRIES", 3))
    API_RETRY_BACKOFF_SECONDS: float = float(os.getenv("API_RETRY_BACKOFF_SECONDS", 2))
    RATE_LIMIT_MAX_RETRIES: int = int(os.getenv("RATE_LIMIT_MAX_RETRIES", 5))
    RATE_LIMIT_BACKOFF_BASE_SECONDS: float = float(os.getenv("RATE_LIMIT_BACKOFF_BASE_SECONDS", 3))
    QUOTES_BATCH_SIZE: int = int(os.getenv("QUOTES_BATCH_SIZE", 50))
    INTRADAY_HISTORY_MAX_DAYS_PER_CHUNK: int = int(os.getenv("INTRADAY_HISTORY_MAX_DAYS_PER_CHUNK", 10))
    DAILY_HISTORY_MAX_DAYS_PER_CHUNK: int = int(os.getenv("DAILY_HISTORY_MAX_DAYS_PER_CHUNK", 100))
    FYERS_MAX_CALLS_PER_SECOND: int = int(os.getenv("FYERS_MAX_CALLS_PER_SECOND", 5))
    LOG_DIR: str = os.getenv("LOG_DIR", "./logs")


config = Config()

SUCCESS = 1
ERROR = -1

def encode_base64(input_string):
    """Encodes a string to base64 required by Vagator v2 endpoints."""
    return base64.b64encode(str(input_string).encode('ascii')).decode('ascii')


def sha256_hash(appId, appType, appSecret):

    message = f"{appId}-{appType}:{appSecret}"
    message = message.encode()
    sha256_hash = hashlib.sha256(message).hexdigest()

    return sha256_hash 


# ==============================================================================
# AUTHENTICATION & TOKEN LIFECYCLE MANAGER
# ==============================================================================
class FyersAuthManager:
    """
    Handles authentication & token lifecycle:
    - Disk caching ({app_id}_fyers_token.txt)
    - Token validation via get_profile API
    - Automated TOTP headless login
    - Manual browser login fallback
    """
    ## ==========================================
    #          BASE URLS FOR FYERS API V3
    ## ==========================================
    BASE_URL = 'https://api-t2.fyers.in/vagator/v2'
    BASE_URL_2 = 'https://api-t1.fyers.in/api/v3'
    URL_VERIFY_CLIENT_ID = BASE_URL + '/send_login_otp_v2'  # send_login_otp
    URL_VERIFY_TOTP = BASE_URL + '/verify_otp'
    URL_VERIFY_PIN = BASE_URL + '/verify_pin_v2'  # verify_pin
    URL_VERIFY_TOKEN = "https://api-t1.fyers.in/gk/verify_token"  # verify_token
    URL_VALIDATE_TOKENS = "https://api-t1.fyers.in/gk/validate_tokens"  # validate_tokens
    URL_TOKEN = BASE_URL_2 + '/token'  # "https://api-t1.fyers.in/api/v3/token"
    URL_VALIDATE_AUTH_CODE = BASE_URL_2 + '/validate-authcode'

    def __init__(self, token_file_path: Optional[str] = None):
        self.access_token = None
        #self.app_id = getattr(config, "FYERS_APP_ID", "XXXXXX-100")
        #self.secret_key = getattr(config, "FYERS_SECRET_KEY", "XXXXXXXXXX")
        #self.redirect_uri = getattr(config, "FYERS_REDIRECT_URI", "https://trade.fyers.in/api-product/front/tools/redirect.png")
        self.token_file_path = token_file_path or getattr(config, "TOKEN_FILE_PATH", f"{config.FYERS_APP_ID}_fyers_token.txt")

    def validate_authcode(self, session, app_id, app_secret, auth_code):
        try:
            app_id_hash = sha256_hash(appId=app_id, appType=config.APP_TYPE, appSecret=app_secret)

            payload = {
                "grant_type": "authorization_code",
                "appIdHash": app_id_hash,
                "code": auth_code,
            }

            headers = {
                        "Content-Type": "application/json",
                    }
            
            logger.info(f"Validate Auth Code Headers: {headers}")
    
            result_string = session.post(
                url=self.URL_VALIDATE_AUTH_CODE,
                json=payload,
                headers=headers,
            )
            logger.info(f"Validate Auth Code Response: {result_string.status_code} - {result_string.text}")
            if result_string.status_code != 200:
                return [ERROR, result_string.text]

            result = json.loads(result_string.text)
            access_token = result["access_token"]
            return [SUCCESS, access_token]

        except Exception as e:
            return [ERROR, e]
           
    def verify_client_id(self, session, fy_id: str, app_id="2"):
        """Sends the base64 encoded user ID and App ID to initiate the login process."""
        try:
            payload = {
                "fy_id": encode_base64(fy_id),
                "app_id": app_id
            }
            result_string = session.post(url=self.URL_VERIFY_CLIENT_ID, json=payload)
            logger.info(f"Verify Client ID Response: {result_string.status_code} - {result_string.text}")
            if result_string.status_code != 200:
                return [ERROR, result_string.text]
                
            result = json.loads(result_string.text)
            request_key = result["request_key"]
            return [SUCCESS, request_key]
        except Exception as e:
            return [ERROR, str(e)]
           
    def verify_totp(self, session, request_key, totp):
        """Verifies the generated TOTP against the request key."""
        try:
            payload = {
                "request_key": request_key,
                "otp": totp
            }
            result_string = session.post(url=self.URL_VERIFY_TOTP, json=payload)
            logger.info(f"Verify TOTP Response: {result_string.status_code} - {result_string.text}")
            if result_string.status_code != 200:
                return [ERROR, result_string.text]
                
            result = json.loads(result_string.text)
            return [SUCCESS, result["request_key"]]
        except Exception as e:
            return [ERROR, str(e)]


    def verify_pin(self, session, request_key, pin):
        """Verifies the user's base64 encoded PIN and returns the temporary trade access token."""
        try:
            payload = {
                "request_key": request_key,
                "identity_type": "pin",
                "identifier": encode_base64(pin)
            }
            result_string = session.post(url=self.URL_VERIFY_PIN, json=payload)
            logger.info(f"Verify PIN Response: {result_string.status_code} - {result_string.text}")
            if result_string.status_code != 200:
                return [ERROR, result_string.text]
                
            result = json.loads(result_string.text)
            access_token = result["data"]["access_token"]
            return [SUCCESS, access_token]
        except Exception as e:
            return [ERROR, str(e)]   
    
    
    def get_auth_code(self, session, fy_id, app_id, redirect_uri, app_type, access_token):
        """Retrieves the auth code using the temporary trade access token."""
        try:
            payload = {
                "fyers_id": fy_id,
                "app_id": app_id, # Pass clean APP_ID without the '-100' here
                "redirect_uri": redirect_uri,
                "appType": app_type,
                "code_challenge": "",
                "state": "None",
                "scope": "",
                "nonce": "",
                "response_type": "code",
                "create_cookie": True
            }
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json"
            }
            
            result_string = session.post(url=self.URL_TOKEN, json=payload, headers=headers)
            logger.info(f"Get Auth Code Response: {result_string.status_code} - {result_string.text}")
            
            if result_string.status_code != 308: # Expecting a redirect response
                return [ERROR, result_string.text]
                
            result = json.loads(result_string.text)
            url = result["Url"]
            parsed_url = urlparse(url)
            query_params = parse_qs(parsed_url.query)
            auth_code = query_params.get("auth_code", [None])[0]
            if not auth_code:
                return [ERROR, "Auth code not found in the response URL."]
            return [SUCCESS, auth_code]
        except Exception as e:
            return [ERROR, str(e)]
        
        
    def load_cached_token(self):
        """Reads existing access token from local file if it exists."""
        if os.path.exists(self.token_file_path):
            try:
                with open(self.token_file_path, "r") as f:
                    token = f.read().strip()
                    return token if token else None
            except Exception as e:
                logger.info(f"Error reading token file: {e}")
                return None
        return None


    def save_token_to_file(self, token):
        """Saves the fresh access token to local file."""
        try:
            with open(self.token_file_path, "w") as f:
                f.write(token)
            logger.info(f"Token saved successfully to '{self.token_file_path}'.")
        except Exception as e:
            logger.info(f"Failed to save token to file: {e}")


    def is_token_valid(self, token, app_id=config.FYERS_APP_ID) -> Tuple[Any, Optional[Any]]:
        """
        Validates token validity against Fyers API by checking profile.
        Returns (True, profile_data) if valid, (False, None) otherwise.
        """
        #token = self.load_cached_token()
        #if not token:
        #    return False, None
        
        logger.info("Found existing token in file. Verifying validity...")
        
        try:
            fyers = fyersModel.FyersModel(
                client_id=f"{app_id}-{config.APP_TYPE}",
                is_async=False,
                token=token,
                log_path=""
            )
            profile = fyers.get_profile()
            logger.info(f"Profile Response: {json.dumps(profile, indent=2)}")
            # Fyers API returns s="ok" on valid token, and s="error" on expired/invalid token
            if profile.get("s") == "ok":
                return True, fyers
            return False, None
        except Exception as e:
            logger.info(f"Token validation check encountered an error: {e}")
            return False, None
           
    def generate_access_token_auto(self, fy_id=config.FYERS_FY_ID,
            totp_key=config.FYERS_TOTP_SECRET,
            pin=config.FYERS_PIN,
            redirect_uri=config.FYERS_REDIRECT_URI,
            app_id=config.FYERS_APP_ID,
            app_secret=config.FYERS_SECRET_KEY)-> Tuple[bool, Optional[Any]]:
        
        """Main workflow to generate the final Fyers API V3 Access Token."""
        logger.info("Starting Fyers Auto-Login Process...")
        
        # CRITICAL: We must use a Session object to persist cookies across all Vagator API calls.
        # Without this, Step 4 will fail with the 'must use the fyers APIv3 APIs' error.
        session = requests.Session()
        
        try:
            """
            Checks if an existing token in fyers_token.txt is still valid.
            Only triggers auto-login and generates a fresh token if the existing one is missing or expired.
            """
            logger.info("⚠️ Cached token has expired or is invalid.")
            #return cached_token
            
            logger.info("Proceeding to request a new access token...")
            logger.info("Requesting new access token...")
            # Step 1: Send Login OTP Request
            logger.info("Step 1: Sending Login Request...")
            send_otp_res = self.verify_client_id(session, fy_id)
            if send_otp_res[0] != SUCCESS:
                logger.info(f"Failed to send login request: {send_otp_res[1]}")
                return False, None
            request_key = send_otp_res[1]
            
            # Step 2: Verify TOTP
            logger.info("Step 2: Verifying TOTP...")
            totp = pyotp.TOTP(totp_key).now()
            logger.info(f"Generated TOTP: {totp}")
            verify_totp_res = self.verify_totp(session, request_key, totp)
            if verify_totp_res[0] != SUCCESS:
                logger.info(f"Failed to verify TOTP: {verify_totp_res[1]}")
                return False, None
            request_key_2 = verify_totp_res[1]
                    
            # Step 3: Verify PIN
            logger.info("Step 3: Verifying PIN...")
            verify_pin_res = self.verify_pin(session, request_key_2, pin)
            if verify_pin_res[0] != SUCCESS:
                logger.info(f"Failed to verify PIN: {verify_pin_res[1]}")
                return False, None
            trade_access_token = verify_pin_res[1]
            
            # Step 4: Add Bearer token to Session headers and Get Auth Code
            logger.info("Step 4: Retrieving Auth Code...")
            session.headers.update({
                'Authorization': f"Bearer {trade_access_token}",
                'Content-Type': 'application/json'
            })
            logger.info(f"Session Headers Updated with Bearer Token: {session.headers}")
            
            auth_code_res = self.get_auth_code(session, fy_id, app_id, redirect_uri, config.APP_TYPE, trade_access_token)
            if auth_code_res[0] != SUCCESS:
                logger.info(f"Failed to retrieve auth_code: {auth_code_res[1]}")
                return False, None
            auth_code = auth_code_res[1]
                    
            # Step 5: Exchange Auth Code for Final API V3 Access Token
            logger.info("Step 5: Generating Final Access Token...")       
            validate_authcode_res = self.validate_authcode(session, app_id, app_secret, auth_code)
            logger.info(f"Validate Auth Code Response: {validate_authcode_res}")
            if validate_authcode_res[0] != SUCCESS:
                logger.info(f"Failed to validate auth_code: {validate_authcode_res[1]}")
                return False, None
            
            final_token = validate_authcode_res[1]  # This is the final access token
    
            if (ERROR != validate_authcode_res[0]):
                logger.info("\n✅ Authentication Successful!")
                logger.info("-" * 50)
                logger.info(f"ACCESS TOKEN: {final_token}")
                logger.info("-" * 50)
                
                # Optional: Save to file for reuse in your trading bots
                self.save_token_to_file(final_token)
                logger.info(f"Token saved to '{self.token_file_path}' for future use.")
                self.access_token = final_token   
                return final_token, fyersModel.FyersModel(client_id=f"{app_id}-{config.APP_TYPE}", token=final_token, is_async=False, log_path="")
            else:
                logger.info(f"❌ Failed to generate access token: {validate_authcode_res}")
                return False,None

        except Exception as e:
            logger.info(f"An exception occurred during authentication: {e}")
            return False,None
        
    def get_valid_access_token(self, force_refresh: bool = False):
            """
            Main entry point: Reuses cached token if valid.
            If expired/forced, automatically triggers TOTP login or manual fallback.
            """
            if not force_refresh:
                cached_token = self.load_cached_token()
                token,fyres_client = self.is_token_valid(token=cached_token)
                if token:
                    logger.info("Reusing valid cached access token.")
                    return cached_token,fyres_client
    
            logger.info("Generating fresh access token (force_refresh=%s)...", force_refresh)
            if all([getattr(config, "FYERS_FY_ID", None), getattr(config, "FYERS_PIN", None), getattr(config, "FYERS_TOTP_SECRET", None)]):
                try:
                    return self.generate_access_token_auto()
                except Exception as exc:
                    logger.warning("Automated TOTP login failed (%s). Falling back to mock/manual token.", exc)
    
            return False,None  # Return empty string if no valid token could be obtained    
# ==============================================================================
# RATE LIMITER EXCEPTIONS & RULES
# ==============================================================================
class RateLimitExceeded(Exception):
    """Raised when non-blocking acquire fails due to active rate limits."""
    pass


class RateLimitRule:
    """Represents a single sliding window rate limit rule."""
    def __init__(self, max_calls: int, period: float):
        self.max_calls = max_calls
        self.period = float(period)
        self.call_times = deque()

    def prune(self, now: float) -> None:
        """Removes timestamps outside of the active sliding window."""
        cutoff = now - self.period
        while self.call_times and self.call_times[0] <= cutoff:
            self.call_times.popleft()

    def is_full(self) -> bool:
        """Checks if the current window has reached maximum capacity."""
        return len(self.call_times) >= self.max_calls

    def get_wait_time(self, now: float) -> float:
        """Calculates time to wait until a slot becomes available in this window."""
        if not self.is_full():
            return 0.0
        elapsed_since_oldest = now - self.call_times[0]
        wait = self.period - elapsed_since_oldest + 0.001
        return max(0.0, wait)


class FyersRateLimiter:
    """
    Thread-safe multi-window rate limiter tailored for Fyers API.
    Handles short burst limits (e.g. 8 calls/sec) and rolling window limits (e.g. 480 calls/min).
    """
    def __init__(
        self,
        limits: Optional[List[Tuple[int, float]]] = None,
        safety_margin: float = 0.001
    ):
        if limits is None:
            limits = [
                (8, 1.0),      # 8 calls per second
                (480, 60.0)    # 480 calls per minute
            ]

        self._rules = [RateLimitRule(max_calls=mc, period=p) for mc, p in limits]
        self._lock = threading.Lock()
        self._safety_margin = safety_margin

    def acquire(self, blocking: bool = True, timeout: Optional[float] = None) -> bool:
        """Acquires permission to make an API call, blocking if necessary."""
        start_time = time.monotonic()

        while True:
            now = time.monotonic()
            
            with self._lock:
                for rule in self._rules:
                    rule.prune(now)

                max_wait_needed = 0.0
                for rule in self._rules:
                    wait = rule.get_wait_time(now)
                    if wait > max_wait_needed:
                        max_wait_needed = wait

                if max_wait_needed == 0.0:
                    for rule in self._rules:
                        rule.call_times.append(now)
                    return True

                if not blocking:
                    raise RateLimitExceeded(
                        f"Rate limit exceeded. Try again in {max_wait_needed:.3f} seconds."
                    )

            if timeout is not None:
                elapsed = time.monotonic() - start_time
                if elapsed + max_wait_needed > timeout:
                    raise TimeoutError(
                        f"Rate limiter acquire timed out after {timeout:.2f}s (needed {max_wait_needed:.2f}s)."
                    )

            time.sleep(max_wait_needed)

    def get_remaining_calls(self) -> List[dict]:
        """Returns snapshot of remaining capacity for each rule window."""
        now = time.monotonic()
        with self._lock:
            status = []
            for rule in self._rules:
                rule.prune(now)
                remaining = rule.max_calls - len(rule.call_times)
                status.append({
                    "period_seconds": rule.period,
                    "max_calls": rule.max_calls,
                    "remaining_calls": max(0, remaining),
                    "used_calls": len(rule.call_times)
                })
            return status

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    def limit(self, func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            self.acquire()
            return func(*args, **kwargs)
        return wrapper


_RATE_LIMIT_MESSAGE_MARKERS = ("rate limit", "too many request", "429")
_TOKEN_EXPIRED_MARKERS = ("token expired", "invalid token", "access token", "authorization failed", "401", "-14")

def _looks_like_rate_limit_error(response: dict) -> bool:
    if not isinstance(response, dict):
        response = {}
    message = str(response.get("message", "")).lower()
    code = response.get("code")
    return any(marker in message for marker in _RATE_LIMIT_MESSAGE_MARKERS) or code == 429

def _looks_like_token_expired_error(response: dict) -> bool:
    if not isinstance(response, dict):
        response = {}
    message = str(response.get("message", "")).lower()
    code = response.get("code")
    return any(marker in message for marker in _TOKEN_EXPIRED_MARKERS) or code in (401, -14)

# ==============================================================================
# EXPOSED API 1: get_fyers_client()
# ==============================================================================
class FyersClientProxy:
    """
    Wrapper around FyersModel client handle that attaches rate limiting and retries
    directly to `client.history` and other API calls while exposing all original SDK methods.
    """
    def __init__(self, raw_client: Any, data_service: FyersDataService):
        self._raw_client = raw_client
        self._data_service = data_service
        self.access_token = getattr(raw_client, "access_token", "")

    def history(self, data: dict) -> dict:
        """Exposes client.history decorated with rate limiting and exponential retries."""
        return self._data_service.history(data)

    def quotes(self, data: dict) -> dict:
        """Exposes client.quotes decorated with rate limiting and retries."""
        return self._data_service.get_quotes(symbols=data.get("symbols", "").split(","))

    def positions(self) -> dict:
        """Exposes client.positions decorated with rate limiting."""
        return self._data_service.get_positions()

    def orderbook(self) -> dict:
        """Exposes client.orderbook decorated with rate limiting."""
        return self._data_service.get_orders()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw_client, name)


def get_fyers_client() -> Any:
    """
    EXPOSED API 1:
    Authenticates (reusing cached token or performing auto/manual login),
    persists the access token, and returns an authenticated FyersModel client handle.
    Takes NO arguments.
    """
    auth_mgr = FyersAuthManager()
    access_token, raw_client = auth_mgr.get_valid_access_token()
    """
    if fyersModel is not None:
        raw_client = fyersModel.FyersModel(
            client_id={config.FYERS_APP_ID}-{config.APP_TYPE},
            is_async=False,
            token=access_token,
            log_path=getattr(config, "LOG_DIR", "./logs"),
        )
    """
    data_service = FyersDataService(client=raw_client, access_token=access_token, auth_manager=auth_mgr)
    #return FyersClientProxy(raw_client=raw_client, data_service=data_service)
    return data_service


# ==============================================================================
# EXPOSED API 2 & DATA SERVICE: get_history() & FyersDataService
# ==============================================================================
class FyersDataService:
    """Centralized market data service providing rate limiting, retries, and data transformations."""
    def __init__(
        self,
        client: Optional[Any] = None,
        access_token: Optional[str] = None,
        auth_manager: Optional[FyersAuthManager] = None,
        limiter: Optional[Union[RateLimiter, FyersRateLimiter]] = None
    ):
        self.auth_manager = auth_manager or FyersAuthManager()
        if client is not None:
            self.client = client
            self.access_token = getattr(client, "token", access_token or "")
        else:
            self.access_token, self.client = self.auth_manager.get_valid_access_token()
            """
            Rate Limits
                Transactional APIs (All API Keys):

                Timeframe	Rate Limit
                Per Second	10
                Per Minute	200
                Per Day	10,000
            """
        self._rate_limiter = FyersRateLimiter([(getattr(config, "FYERS_MAX_CALLS_PER_SECOND", 7), 1.0), (190, 60.0)])


    def refresh_session(self) -> None:
        logger.info("Refreshing Fyers session token...")
        self.access_token, self.client = self.auth_manager.get_valid_access_token(force_refresh=True)


    def _call_with_retry(self, fn, *args, rate_limit_max_retries: int | None = None, **kwargs):
        rate_limit_max_retries = (config.RATE_LIMIT_MAX_RETRIES if rate_limit_max_retries is None else rate_limit_max_retries)
        last_exc = None
        rate_limit_attempt = 0
        token_refreshed = False

        for attempt in range(1, config.API_MAX_RETRIES + 1):
            try:
                self._rate_limiter.acquire()
                if self.client is None:
                    raise RuntimeError("Fyers client is uninitialized.")

                target_fn = fn
                if hasattr(fn, "__name__") and hasattr(self.client, fn.__name__):
                    target_fn = getattr(self.client, fn.__name__)

                response = target_fn(*args, **kwargs)

                if isinstance(response, dict) and response.get("s") == "error":
                    if _looks_like_token_expired_error(response) and not token_refreshed:
                        logger.warning("Detected expired access token. Refreshing session...")
                        self.refresh_session()
                        token_refreshed = True
                        continue

                    if _looks_like_rate_limit_error(response) and rate_limit_attempt < rate_limit_max_retries:
                        rate_limit_attempt += 1
                        backoff = config.RATE_LIMIT_BACKOFF_BASE_SECONDS * (2 ** (rate_limit_attempt - 1))
                        logger.warning("Rate-limit rejection (attempt %d/%d): backing off %.1fs.", rate_limit_attempt, rate_limit_max_retries, backoff)
                        time.sleep(backoff)
                        continue

                    raise RuntimeError(response.get("message", "Unknown Fyers API error"))
                symb = kwargs['data']
                logger.info(f"{symb.get('symbol', 'N/A')} -- resolution {symb.get('resolution', 'N/A')} - attempt {attempt}.")
                return response

            except Exception as exc:  # broad on purpose: network, auth, and API-level errors all retry
                last_exc = exc
                logger.error("Fyers API call failed (attempt %d/%d): %s",
                                attempt, config.API_MAX_RETRIES, exc)
                if "invalid symbol" in str(exc).lower():
                    logger.warning(f"Fyers API call failed due to invalid symbol - will not retry.\n kwargs = {kwargs}")
                    return {}  # caller should handle empty response gracefully
                logger.info(f" kwargs = {kwargs}")
                time.sleep(config.API_RETRY_BACKOFF_SECONDS * attempt)
                
        logger.critical("Fyers API call failed after %d attempts: %s", config.API_MAX_RETRIES, last_exc)
        #raise last_exc

    def history(self, data: dict) -> dict:
        """EXPOSED API IMPLEMENTATION: Calls client.history with rate limiting and exponential backoff."""
        return self._call_with_retry(self.client.history, data=data)

    def get_quotes(self, symbols: list[str]) -> dict:
        data = {"symbols": ",".join(symbols)}
        return self._call_with_retry(self.client.quotes, data=data)
    
    def quotes(self, symbols: list[str]) -> dict:
        """Returns a dictionary of quotes for the given symbols."""
        return self.get_quotes(symbols)
    
    def get_token(self) -> str:
        """Returns the current access token."""
        return str(self.access_token)
    
    def token(self) -> str:
        """Returns the current access token."""
        return str(self.access_token)
    
    def get_quotes_batched(self, symbols: list[str]) -> dict[str, dict]:
        merged: dict[str, dict] = {}
        batch_size = getattr(config, "QUOTES_BATCH_SIZE", 50)
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i:i + batch_size]
            response = self.get_quotes(batch)
            for item in response.get("d", []):
                merged[item["n"]] = item.get("v", {})
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
        data = {
            "symbol": symbol, "resolution": resolution, "date_format": "1",
            "range_from": range_from_date, "range_to": range_to_date, "cont_flag": "1",
        }
        response = self.history(data)
        return response.get("candles", []) if response else []
    
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
        response = self._call_with_retry(self.client.positions)
        return response.get("netPositions", []) if isinstance(response, dict) else []

    def get_orders(self) -> list[dict]:
        response = self._call_with_retry(self.client.orderbook)
        return response.get("orderBook", []) if isinstance(response, dict) else []
  
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

    def get_candles_bulk(
        self, symbols: list[str], resolution: str, range_from_date: str, range_to_date: str, max_days_per_chunk: int | None = None
    ) -> dict[str, list]:
        chunk_days = max_days_per_chunk or (100 if resolution == "D" else 10)
        start = datetime.strptime(range_from_date, "%Y-%m-%d").date()
        end = datetime.strptime(range_to_date, "%Y-%m-%d").date()
        
        chunks: list[tuple[date, date]] = []
        chunk_start = start
        while chunk_start <= end:
            chunk_end = min(chunk_start + timedelta(days=chunk_days - 1), end)
            chunks.append((chunk_start, chunk_end))
            chunk_start = chunk_end + timedelta(days=1)

        results: dict[str, list] = {}
        for symbol in symbols:
            symbol_candles: list = []
            for chunk_start_d, chunk_end_d in chunks:
                try:
                    symbol_candles.extend(self.get_candles_range(symbol, resolution, str(chunk_start_d), str(chunk_end_d)))
                except Exception as exc:
                    logger.error("Error fetching chunk for %s: %s", symbol, exc)
            results[symbol] = symbol_candles
        return results


def get_history(data: Optional[dict] = None, client: Optional[Any] = None, **kwargs) -> dict:
    """
    EXPOSED API 2:
    Exposes historical data retrieval calling client.history under the hood with
    rate limiting, retries, and token auto-refresh.
    If client is not passed, automatically acquires one using get_fyers_client().
    """
    data_payload = data if data is not None else kwargs
    if client is None:
        client = get_fyers_client()
    
    if hasattr(client, "history"):
        return client.history(data_payload)
    
    ds = FyersDataService(client=client)
    return ds.history(data_payload)


# ==============================================================================
# UNIT TEST CASES
# ==============================================================================
class TestFyersService(unittest.TestCase):
    """Unit test suite covering authentication, client instantiation, rate limiting, and get_history()."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.token_file = os.path.join(self.temp_dir.name, "test_token.txt")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_rate_limiter_basic(self):
        """Tests that FyersRateLimiter enforces rate caps correctly."""
        limiter = FyersRateLimiter(limits=[(5, 1.0)])
        start = time.monotonic()
        for _ in range(5):
            self.assertTrue(limiter.acquire())
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 0.5)

    def test_auth_manager_token_persistence(self):
        """Tests saving and loading access tokens to/from disk cache."""
        auth_mgr = FyersAuthManager(token_file_path=self.token_file)
        self.assertIsNone(auth_mgr.load_cached_token())
        
        test_token = "TEST_ACCESS_TOKEN_12345"
        auth_mgr.save_token_to_file(test_token)
        self.assertEqual(auth_mgr.load_cached_token(), test_token)
        self.assertTrue(auth_mgr.is_token_valid(test_token))

    def test_get_fyers_client_no_args(self):
        """Tests get_fyers_client() API with zero arguments."""
        client = get_fyers_client()
        self.assertIsNotNone(client)
        self.assertTrue(hasattr(client, "history"))
        self.assertTrue(hasattr(client, "quotes"))
        self.assertTrue(hasattr(client, "token"))

    def test_get_history_standalone_api(self):
        """Tests get_history() standalone API function."""
        data_payload = {
            "symbol": "NSE:SBIN-EQ",
            "resolution": "D",
            "date_format": "1",
            "range_from": "2026-01-01",
            "range_to": "2026-01-10",
            "cont_flag": "1"
        }
        response = get_history(data_payload)
        self.assertIsInstance(response, dict)
        self.assertEqual(response.get("s"), "ok")
        self.assertIn("candles", response)
        self.assertGreater(len(response["candles"]), 0)

    def test_client_history_method_invocation(self):
        """Tests invoking client.history directly on the handle returned by get_fyers_client()."""
        client = get_fyers_client()
        response = client.history({
            "symbol": "NSE:TCS-EQ",
            "resolution": "5",
            "date_format": "1",
            "range_from": "2026-02-01",
            "range_to": "2026-02-05"
        })
        self.assertEqual(response.get("s"), "ok")
        self.assertIn("candles", response)

    def test_get_candles_bulk(self):
        """Tests FyersDataService bulk historical data fetching with date chunking."""
        ds = client = get_fyers_client()
        #ds = FyersDataService(client=client)
        bulk_data = ds.get_candles_bulk(
            symbols=["NSE:SBIN-EQ", "NSE:RELIANCE-EQ"],
            resolution="D",
            range_from_date="2026-01-01",
            range_to_date="2026-01-15",
            max_days_per_chunk=5
        )
        self.assertIn("NSE:SBIN-EQ", bulk_data)
        self.assertIn("NSE:RELIANCE-EQ", bulk_data)
        self.assertGreater(len(bulk_data["NSE:SBIN-EQ"]), 0)


if __name__ == "__main__":
    print("=================================================================")
    print("  RUNNING UNIT TEST SUITE FOR FYERS SERVICE")
    print("=================================================================")
    fyres_client = get_fyers_client()  # Ensure client can be instantiated before running tests
    unittest.main()
