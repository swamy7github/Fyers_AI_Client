"""Structured logging setup (console + rotating file), mirroring the repo's
``nifty_orb_agent`` convention of a module-prefixed logger name."""

from __future__ import annotations

import json
import logging
import logging.handlers
from datetime import datetime
from pathlib import Path
from typing import Optional


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(log_dir: Optional[Path | str] = None, level: str = "INFO", filename: str = "app.log",
                  json_lines: bool = False) -> logging.Logger:
    root = logging.getLogger("ghanshyam")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    if root.handlers:
        return root
    fmt = JsonFormatter() if json_lines else logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    if log_dir:
        p = Path(log_dir)
        p.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(p / filename, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    root.propagate = False
    return root
