"""Pytest configuration: make the project root importable. No API credentials are
needed anywhere in the test suite; all market data is synthetic."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
