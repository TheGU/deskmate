"""Data adapters. Each one turns an external source into normalized models."""

from __future__ import annotations

from app.adapters.base import (
    Adapter,
    AdapterError,
    AdapterUnavailable,
    CachedAdapter,
    Outcome,
)

__all__ = [
    "Adapter",
    "AdapterError",
    "AdapterUnavailable",
    "CachedAdapter",
    "Outcome",
]
