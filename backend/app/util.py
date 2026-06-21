"""Tiny shared helpers used across the backend modules."""
from __future__ import annotations

import time
import uuid


def now_ms() -> int:
    """Current wall-clock time in integer milliseconds."""
    return int(time.time() * 1000)


def new_id() -> str:
    """A short, URL/filesystem-safe unique id."""
    return uuid.uuid4().hex
