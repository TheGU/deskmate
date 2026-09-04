"""Structured logging: stdlib logging with a compact key=value formatter.

Every record renders as::

    2026-09-04T09:12:33+07:00 INFO app.render msg="rendered page" page=today ms=412

Extra fields are attached with ``logger.info("msg", extra={"fields": {...}})``
or through the :func:`log` helper.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone as dt_timezone
from typing import Any, Final

_RESERVED: Final[frozenset[str]] = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys()
) | {"fields", "message", "asctime", "taskName"}


def _quote(value: Any) -> str:
    text = "true" if value is True else "false" if value is False else str(value)
    if text == "":
        return '""'
    if any(ch in text for ch in ' "=\n\t'):
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'
    return text


class KeyValueFormatter(logging.Formatter):
    """Render log records as ``time level logger msg=... key=value``."""

    def format(self, record: logging.LogRecord) -> str:
        stamp = datetime.fromtimestamp(record.created, dt_timezone.utc).isoformat(
            timespec="seconds"
        )
        parts: list[str] = [
            stamp,
            record.levelname,
            record.name,
            f"msg={_quote(record.getMessage())}",
        ]
        fields: dict[str, Any] = {}
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            fields.update(extra)
        for key, value in record.__dict__.items():
            if key not in _RESERVED:
                fields[key] = value
        for key in sorted(fields):
            parts.append(f"{key}={_quote(fields[key])}")
        if record.exc_info:
            parts.append(f"exc={_quote(self.formatException(record.exc_info))}")
        return " ".join(parts)


def configure_logging(level: str = "INFO") -> None:
    """Install the key=value formatter on the root logger (idempotent)."""
    root = logging.getLogger()
    root.setLevel(level.upper())
    for existing in list(root.handlers):
        root.removeHandler(existing)
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(KeyValueFormatter())
    root.addHandler(handler)
    logging.getLogger("uvicorn.access").propagate = True
    logging.getLogger("uvicorn.error").propagate = True
    for noisy in ("uvicorn.access", "uvicorn.error", "uvicorn"):
        logging.getLogger(noisy).handlers.clear()


def log(logger: logging.Logger, level: int, message: str, /, **fields: Any) -> None:
    """Log ``message`` with structured ``fields``."""
    logger.log(level, message, extra={"fields": fields})
