"""Adapter protocol, error types, and the safe TTL-cached wrapper.

Rules that every adapter obeys:

* ``fetch()`` either returns a fully normalized value or raises.
* :class:`AdapterUnavailable` means "not configured / nothing to read"
  (status ``unavailable``); any other exception means "it broke"
  (status ``error``).
* :class:`CachedAdapter` never lets an exception escape. On failure it hands
  back the previous value (status ``stale``) when it has one, otherwise no
  value at all. It never fabricates.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar

from app.logging_setup import log
from app.models import AdapterStatus
from app.timeutil import to_local

T = TypeVar("T")

logger = logging.getLogger("app.adapters")

#: How long a failed fetch is replayed instead of retried. Bounded below by
#: the adapter's own ttl (see CachedAdapter._backoff_seconds): a failure is
#: never remembered longer than a success would have been cached for.
FAILURE_BACKOFF_SECONDS = 60.0


class AdapterError(RuntimeError):
    """The adapter tried and failed."""


class AdapterUnavailable(AdapterError):
    """The source is not configured or has nothing to offer."""


class Adapter(Protocol[T]):
    """Minimal adapter interface."""

    #: Adapter slot, e.g. ``tasks``.
    name: str
    #: Selected source, e.g. ``fixture`` or ``obsidian``.
    source: str

    async def fetch(self) -> T:
        """Return a normalized value or raise."""
        ...


@dataclass(slots=True)
class Outcome(Generic[T]):
    """Result of one cached adapter read."""

    value: T | None
    status: AdapterStatus
    source: str
    updated_at: datetime | None = None
    error: str | None = None
    #: When the underlying file was received (its own ``received_at`` key, or
    #: its mtime). Only the file adapters for ai_usage, brief and tasks set
    #: this (via ``last_received_at`` on the adapter instance); every other
    #: adapter leaves it ``None`` and it is silently dropped when the block
    #: model has no such field (pydantic's default ``extra="ignore"``).
    received_at: datetime | None = None


def _now() -> datetime:
    return datetime.now(tz=dt_timezone.utc)


def received_at_or_mtime(value: Any, path: Path, timezone_name: str) -> datetime:
    """A pushed file's own ``received_at`` string, or its mtime, localized.

    Shared by the file adapters for ai_usage, brief and tasks so the three
    ``received_at``-bearing blocks (models.py) get one consistent rule.
    """
    if isinstance(value, str):
        try:
            return to_local(datetime.fromisoformat(value), timezone_name)
        except ValueError:
            pass
    stamp = datetime.fromtimestamp(path.stat().st_mtime, tz=dt_timezone.utc)
    return to_local(stamp, timezone_name)


class CachedAdapter(Generic[T]):
    """TTL cache plus a failure-tolerant wrapper around one adapter."""

    def __init__(self, adapter: Adapter[T], ttl_seconds: float) -> None:
        self._adapter = adapter
        self._ttl = max(0.0, ttl_seconds)
        self._lock = asyncio.Lock()
        self._value: T | None = None
        self._value_at: datetime | None = None
        self._monotonic_at: float | None = None
        self._last_error: str | None = None
        #: (monotonic time of the failure, the Outcome it produced). Set by
        #: either except branch below and replayed by get() instead of
        #: re-fetching until _backoff_seconds() has passed. Cleared by a
        #: success or by invalidate().
        self._failure: tuple[float, Outcome[T]] | None = None
        #: The Outcome the most recent get() returned, however it got there
        #: (TTL hit, replayed failure, or a fresh fetch). /healthz reads this
        #: so it can answer without ever fetching.
        self.last_outcome: Outcome[T] | None = None
        #: Bumped by invalidate(). invalidate() is called synchronously (a
        #: push handler is not inside this adapter's async lock), so a slow
        #: fetch already in flight when it fires must not resurrect the
        #: cache: get() compares the generation it started with against the
        #: current one before marking its result fresh.
        self._generation = 0

    @property
    def name(self) -> str:
        return self._adapter.name

    @property
    def source(self) -> str:
        return self._adapter.source

    def resolve(self) -> str:
        """What the underlying adapter's *next* fetch would use, checked
        live and without fetching. Adapters with an ambiguous source (the
        Auto adapters for ai_usage/brief/tasks) define their own pure
        ``resolve()``; anything else falls back to the static ``source``
        above. Distinct from ``source``, which mirrors the *last* fetch and
        is what the DEMO mark needs (it must match what is currently drawn).
        """
        resolver = getattr(self._adapter, "resolve", None)
        return resolver() if resolver is not None else self.source

    def invalidate(self) -> None:
        """Force the next :meth:`get` to hit the underlying source.

        Safe to call while a :meth:`get` is mid-fetch: bumping the
        generation counter here means that fetch, even though it started
        before this call, cannot mark the cache fresh when it completes.
        """
        self._monotonic_at = None
        self._failure = None
        self._generation += 1

    def _fresh(self) -> bool:
        return (
            self._monotonic_at is not None
            and (time.monotonic() - self._monotonic_at) < self._ttl
        )

    def _backoff_seconds(self) -> float:
        """A failure is never held longer than a success would be cached
        for; with ttl 0 (as in most tests) that makes the backoff 0, i.e.
        every get() retries."""
        return min(FAILURE_BACKOFF_SECONDS, self._ttl)

    def _received_at(self) -> datetime | None:
        """The adapter's own ``last_received_at``, when it tracks one.

        Only the file adapters for ai_usage, brief and tasks set this
        instance attribute; every other adapter has none, so this is
        ``None`` for them. It reflects the *last successful* fetch, so it
        stays correct through a cache hit or a subsequent failure too.
        """
        return getattr(self._adapter, "last_received_at", None)

    async def get(self, *, force: bool = False) -> Outcome[T]:
        """Read the adapter, using the TTL cache unless ``force`` is set."""
        async with self._lock:
            if not force and self._fresh() and self._value is not None:
                outcome = Outcome(
                    value=self._value,
                    status=AdapterStatus.OK,
                    source=self.source,
                    updated_at=self._value_at,
                    received_at=self._received_at(),
                )
                self.last_outcome = outcome
                return outcome

            if not force and self._failure is not None:
                failed_at, outcome = self._failure
                if (time.monotonic() - failed_at) < self._backoff_seconds():
                    # Replaying the outcome as-is freezes its `source`. That
                    # is inert today - the Auto adapters (ai_usage, brief,
                    # tasks) never raise, they fall back to fixture
                    # themselves - but would matter if that changed.
                    self.last_outcome = outcome
                    return outcome

            generation = self._generation
            started = time.monotonic()
            try:
                value = await self._adapter.fetch()
            except AdapterUnavailable as exc:
                self._last_error = str(exc)
                log(
                    logger,
                    logging.INFO,
                    "adapter unavailable",
                    adapter=self.name,
                    source=self.source,
                    reason=str(exc),
                )
                outcome = Outcome(
                    value=self._value,
                    status=AdapterStatus.STALE if self._value is not None else AdapterStatus.UNAVAILABLE,
                    source=self.source,
                    updated_at=self._value_at,
                    error=str(exc),
                    received_at=self._received_at(),
                )
                # An invalidate() that landed while this fetch was in flight
                # (e.g. a push writing new data) must not be masked behind a
                # 60s backoff: only remember the failure if nothing invali-
                # dated the cache since this fetch started.
                if generation == self._generation:
                    self._failure = (time.monotonic(), outcome)
                self.last_outcome = outcome
                return outcome
            except Exception as exc:  # noqa: BLE001 - one adapter must not kill the page
                self._last_error = f"{type(exc).__name__}: {exc}"
                log(
                    logger,
                    logging.WARNING,
                    "adapter failed",
                    adapter=self.name,
                    source=self.source,
                    error=self._last_error,
                )
                outcome = Outcome(
                    value=self._value,
                    status=AdapterStatus.STALE if self._value is not None else AdapterStatus.ERROR,
                    source=self.source,
                    updated_at=self._value_at,
                    error=self._last_error,
                    received_at=self._received_at(),
                )
                if generation == self._generation:
                    self._failure = (time.monotonic(), outcome)
                self.last_outcome = outcome
                return outcome

            self._value = value
            self._value_at = _now()
            # An invalidate() that landed while this fetch was in flight must
            # not be erased: only mark the cache fresh if the generation is
            # still the one this fetch started with.
            self._monotonic_at = time.monotonic() if generation == self._generation else None
            self._last_error = None
            self._failure = None
            log(
                logger,
                logging.DEBUG,
                "adapter fetched",
                adapter=self.name,
                source=self.source,
                ms=round((time.monotonic() - started) * 1000),
            )
            outcome = Outcome(
                value=value,
                status=AdapterStatus.OK,
                source=self.source,
                updated_at=self._value_at,
                received_at=self._received_at(),
            )
            self.last_outcome = outcome
            return outcome
