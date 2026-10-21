"""YAML settings loader with typed accessors.

The YAML file is the single source of truth. ``Settings`` wraps the parsed
dictionary and exposes a few typed helpers (session timings, per-instrument
numeric lookups, strategy blocks) so strategy code never does raw dict
plumbing. Overrides can be applied with dotted keys, e.g.
``load_settings(overrides={"capital.total": 250000})``.
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import yaml

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = PACKAGE_ROOT.parent


def default_settings_path() -> Path:
    return Path(__file__).resolve().parent / "settings.yaml"


def parse_hhmm(value: str) -> time:
    hh, mm = str(value).split(":")
    return time(int(hh), int(mm))


@dataclass(frozen=True)
class SessionTimes:
    exchange: str
    open: time
    close: time
    no_new_entries_after: time
    square_off: time
    no_entry_windows: Tuple[Tuple[time, time], ...]

    def is_open(self, t: time) -> bool:
        return self.open <= t < self.close

    def in_no_entry_window(self, t: time) -> bool:
        return any(start <= t < end for start, end in self.no_entry_windows)

    def can_enter(self, t: time) -> bool:
        return self.is_open(t) and t < self.no_new_entries_after and not self.in_no_entry_window(t)


class Settings:
    """Thin typed view over the YAML dict."""

    def __init__(self, raw: Mapping[str, Any], source: Optional[Path] = None):
        self._raw: Dict[str, Any] = copy.deepcopy(dict(raw))
        self.source = source

    # -- generic access -----------------------------------------------------
    @property
    def raw(self) -> Dict[str, Any]:
        return self._raw

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._raw
        for part in dotted.split("."):
            if not isinstance(node, Mapping) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        node = self._raw
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def section(self, name: str) -> Dict[str, Any]:
        value = self._raw.get(name, {})
        return value if isinstance(value, dict) else {}

    # -- paths ----------------------------------------------------------------
    def path(self, key: str) -> Path:
        value = self.get(f"paths.{key}")
        if value is None:
            raise KeyError(f"paths.{key} missing from settings")
        p = Path(value)
        return p if p.is_absolute() else REPO_ROOT / p

    # -- sessions -------------------------------------------------------------
    def session(self, exchange: str) -> SessionTimes:
        block = self.get(f"sessions.{exchange}")
        if not block:
            raise KeyError(f"sessions.{exchange} missing from settings")
        windows = tuple(
            (parse_hhmm(a), parse_hhmm(b)) for a, b in block.get("no_entry_windows", []) or []
        )
        return SessionTimes(
            exchange=exchange,
            open=parse_hhmm(block["open"]),
            close=parse_hhmm(block["close"]),
            no_new_entries_after=parse_hhmm(block.get("no_new_entries_after", block["close"])),
            square_off=parse_hhmm(block["square_off"]),
            no_entry_windows=windows,
        )

    # -- strategies -----------------------------------------------------------
    def strategy(self, name: str) -> Dict[str, Any]:
        block = self.get(f"strategies.{name}")
        if block is None:
            raise KeyError(f"strategies.{name} missing from settings")
        return block

    def enabled_strategies(self) -> List[str]:
        return [name for name, blk in self.section("strategies").items() if blk and blk.get("enabled", False)]

    def strategy_windows(self, name: str, exchange: str) -> List[Tuple[time, time]]:
        windows = self.get(f"strategies.{name}.windows.{exchange}")
        if not windows:
            return []
        return [(parse_hhmm(a), parse_hhmm(b)) for a, b in windows]

    # -- per-instrument numeric lookups --------------------------------------
    @staticmethod
    def per_instrument(block: Any, instrument: str, default: Optional[float] = None) -> Optional[float]:
        """``block`` is either a scalar or a mapping keyed by instrument."""
        if block is None:
            return default
        if isinstance(block, Mapping):
            if instrument in block:
                return float(block[instrument])
            if "default" in block:
                return float(block["default"])
            return default
        return float(block)

    def instrument_value(self, dotted: str, instrument: str, default: Optional[float] = None) -> Optional[float]:
        return self.per_instrument(self.get(dotted), instrument, default)

    # -- convenience ----------------------------------------------------------
    @property
    def total_capital(self) -> float:
        return float(self.get("capital.total", 0.0))

    @property
    def live_enabled(self) -> bool:
        return bool(self.get("live.enabled", False))

    def copy(self) -> "Settings":
        return Settings(self._raw, self.source)


def _apply_overrides(raw: Dict[str, Any], overrides: Mapping[str, Any]) -> None:
    for dotted, value in overrides.items():
        parts = dotted.split(".")
        node = raw
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value


def load_settings(
    path: Optional[os.PathLike | str] = None,
    overrides: Optional[Mapping[str, Any]] = None,
) -> Settings:
    """Load ``settings.yaml`` (or ``path``) and apply dotted-key overrides."""
    p = Path(path) if path else default_settings_path()
    with open(p, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{p} must contain a mapping at top level")
    if overrides:
        _apply_overrides(raw, overrides)
    _validate(raw)
    return Settings(raw, source=p)


def _validate(raw: Mapping[str, Any]) -> None:
    required: Iterable[str] = ("capital", "risk", "sessions", "strategies", "strike", "execution", "paths")
    missing = [k for k in required if k not in raw]
    if missing:
        raise ValueError(f"settings missing required sections: {missing}")
    cap = raw["capital"]
    if float(cap.get("total", 0)) <= 0:
        raise ValueError("capital.total must be > 0")
    if not 0 < float(cap.get("active_tranche_pct", 0)) <= 1:
        raise ValueError("capital.active_tranche_pct must be in (0, 1]")
    risk = raw["risk"]
    if int(risk.get("max_trades_per_day", 0)) < 1:
        raise ValueError("risk.max_trades_per_day must be >= 1")
    if not 0 < float(risk.get("max_daily_loss_pct", 0)) < 1:
        raise ValueError("risk.max_daily_loss_pct must be in (0, 1)")
    for exch, block in raw["sessions"].items():
        for key in ("open", "close", "square_off"):
            if key not in block:
                raise ValueError(f"sessions.{exch}.{key} missing")
        if parse_hhmm(block["square_off"]) > parse_hhmm(block["close"]):
            raise ValueError(f"sessions.{exch}.square_off must be <= close")
