"""Pytest bootstrap: ensure the project root is importable as ``src``.

Placing this at the project root puts the root on ``sys.path`` so tests can use
absolute imports such as ``from src.forecasting import seasonal_naive``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
