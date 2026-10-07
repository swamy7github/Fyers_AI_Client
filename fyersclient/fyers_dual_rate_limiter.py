import time
import threading
from collections import deque
from typing import List, Tuple, Optional, Callable, Any
import functools


class RateLimitExceeded(Exception):
    """Raised when non-blocking acquire fails due to active rate limits."""
    pass


class RateLimitRule:
    """
    Represents a single sliding window rate limit rule.
    
    Attributes:
        max_calls (int): Maximum calls allowed in the period.
        period (float): Time window in seconds.
        call_times (deque): Queue of monotonic timestamps of recent calls.
    """
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
        # Wait until the oldest timestamp in this window expires + tiny safety buffer
        elapsed_since_oldest = now - self.call_times[0]
        wait = self.period - elapsed_since_oldest + 0.001
        return max(0.0, wait)


class FyersRateLimiter:
    """
    Thread-safe multi-window rate limiter tailored for Fyers API.
    
    Default Configuration:
    - 8 requests per 1 second
    - 150 requests per 3600 seconds (1 hour)
    """

    def __init__(
        self,
        limits: Optional[List[Tuple[int, float]]] = None,
        safety_margin: float = 0.001
    ):
        # Default limits for Fyers API if not specified
        if limits is None:
            limits = [
                (5, 1.0),       # 8 calls per second
                (480, 60.0)   # 150 calls per hour
            ]

        self._rules = [RateLimitRule(max_calls=mc, period=p) for mc, p in limits]
        self._lock = threading.Lock()
        self._safety_margin = safety_margin

    def acquire(self, blocking: bool = True, timeout: Optional[float] = None) -> bool:
        """
        Acquires permission to make an API call, blocking if necessary until rate limits clear.

        Args:
            blocking (bool): If True, sleeps until limits clear. If False, raises RateLimitExceeded.
            timeout (float, optional): Maximum time to wait in seconds if blocking.

        Returns:
            bool: True if permission was acquired.
        """
        start_time = time.monotonic()

        while True:
            now = time.monotonic()
            
            with self._lock:
                # 1. Prune expired call timestamps for all configured rules
                for rule in self._rules:
                    rule.prune(now)

                # 2. Check wait times for all rules
                max_wait_needed = 0.0
                for rule in self._rules:
                    wait = rule.get_wait_time(now)
                    if wait > max_wait_needed:
                        max_wait_needed = wait

                # 3. If no rules are violated, record the request and grant permission
                if max_wait_needed == 0.0:
                    for rule in self._rules:
                        rule.call_times.append(now)
                    return True

                # 4. If non-blocking mode, fail immediately
                if not blocking:
                    raise RateLimitExceeded(
                        f"Rate limit exceeded. Try again in {max_wait_needed:.3f} seconds."
                    )

            # Check timeout condition
            if timeout is not None:
                elapsed = time.monotonic() - start_time
                if elapsed + max_wait_needed > timeout:
                    raise TimeoutError(
                        f"Rate limiter acquire timed out after {timeout:.2f}s (needed {max_wait_needed:.2f}s)."
                    )

            # 5. Sleep OUTSIDE the lock so other threads can still access status/acquire
            time.sleep(max_wait_needed)

    def get_remaining_calls(self) -> List[dict]:
        """
        Returns snapshot of remaining capacity for each rule window.
        """
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
        """Context manager entry."""
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        pass

    def limit(self, func: Callable) -> Callable:
        """
        Decorator to automatically rate-limit calls to a specific function or method.
        """
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            self.acquire()
            return func(*args, **kwargs)
        return wrapper


def run_simulation():
    """
    Simulates high-frequency API calls against the Fyers Dual Rate Limiter
    to demonstrate per-second and per-hour throttling.
    """
    print("=" * 60)
    print("  FYERS API DUAL RATE LIMITER DEMO")
    print("  Limits: 8 calls/sec | 150 calls/hour")
    print("=" * 60 + "\n")

    # Creating limiter with 8/sec and custom small 15/10sec window for quick demo
    limiter = FyersRateLimiter(limits=[
        (8, 1.0),      # Max 8 calls per second
        (80, 10.0)     # Max 15 calls per 10 seconds (simulating 150/hour limit at small scale)
    ])

    start_time = time.monotonic()

    def api_call(request_id: int):
        # Acquire permission before calling API
        limiter.acquire()
        elapsed = time.monotonic() - start_time
        remaining = limiter.get_remaining_calls()
        sec_rem = remaining[0]['remaining_calls']
        window_rem = remaining[1]['remaining_calls']
        print(f"[{elapsed:06.3f}s] Executed Request #{request_id:02d} | 1s Rem: {sec_rem}/8 | 10s Rem: {window_rem}/15")

    print("--- Burst Test: Attempting 20 calls rapidly ---")
    threads = []
    for i in range(1, 21):
        t = threading.Thread(target=api_call, args=(i,))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    print("\n--- Simulation Complete ---")


if __name__ == "__main__":
    run_simulation()