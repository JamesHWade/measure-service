"""Bounded, process-local caches for immutable owner-scoped captures."""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from typing import Any

MAX_CAPTURES_PER_OWNER = 4
MAX_CAPTURE_COUNT = 64
CAPTURE_IDLE_SECONDS = 300.0


@dataclass(slots=True)
class _Capture:
    value: Any
    size_bytes: int
    last_access: float


class CaptureCache:
    """Store immutable captures with owner-local eviction and bounded metadata.

    ``owner_bucket`` is normally ``(store_identity, owner)``. The store
    identity belongs in the bucket so a database replacement cannot address
    captures from the previous file. ``key`` identifies a capture inside that
    owner/store bucket.

    The cache tracks the caller-supplied serialized size rather than guessing
    the in-memory object size. Its entry count, size totals, and LRU metadata
    all live in one bounded ordered mapping.
    """

    def __init__(
        self,
        *,
        max_per_owner: int = MAX_CAPTURES_PER_OWNER,
        max_entries: int = MAX_CAPTURE_COUNT,
        max_bytes: int,
        idle_seconds: float = CAPTURE_IDLE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if type(max_per_owner) is not int or max_per_owner < 1:
            raise ValueError("max_per_owner must be a positive integer")
        if type(max_entries) is not int or max_entries < 1:
            raise ValueError("max_entries must be a positive integer")
        if type(max_bytes) is not int or max_bytes < 0:
            raise ValueError("max_bytes must be a nonnegative integer")
        if (
            isinstance(idle_seconds, bool)
            or not isinstance(idle_seconds, (int, float))
            or not math.isfinite(idle_seconds)
            or idle_seconds <= 0
        ):
            raise ValueError("idle_seconds must be a positive finite number")

        self.max_per_owner = max_per_owner
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.idle_seconds = float(idle_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: OrderedDict[tuple[tuple[Hashable, ...], Hashable], _Capture] = OrderedDict()
        self._total_bytes = 0

    def put(
        self,
        owner_bucket: tuple[Hashable, ...],
        key: Hashable,
        value: Any,
        size_bytes: int,
    ) -> bool:
        """Admit or replace one capture; return false when capacity is unavailable.

        A failed admission does not evict any live capture. Capacity recovery
        first considers only least-recently-used entries in ``owner_bucket``.
        Expired entries are removed globally before admission is planned.
        """
        cache_key = self._cache_key(owner_bucket, key)
        if type(size_bytes) is not int or size_bytes < 0:
            raise ValueError("size_bytes must be a nonnegative integer")

        with self._lock:
            now = self._clock()
            self._prune_expired(now)
            previous = self._entries.get(cache_key)
            if size_bytes > self.max_bytes:
                return False

            replacement_count = 1 if previous is not None else 0
            owner_count = sum(bucket == owner_bucket for bucket, _ in self._entries)
            projected_count = len(self._entries) - replacement_count + 1
            projected_owner_count = owner_count - replacement_count + 1
            projected_bytes = (
                self._total_bytes
                - (previous.size_bytes if previous is not None else 0)
                + size_bytes
            )

            planned_evictions: list[tuple[tuple[Hashable, ...], Hashable]] = []
            if not self._fits(projected_count, projected_owner_count, projected_bytes):
                for candidate_key, entry in self._entries.items():
                    if candidate_key == cache_key or candidate_key[0] != owner_bucket:
                        continue
                    planned_evictions.append(candidate_key)
                    projected_count -= 1
                    projected_owner_count -= 1
                    projected_bytes -= entry.size_bytes
                    if self._fits(projected_count, projected_owner_count, projected_bytes):
                        break

            if not self._fits(projected_count, projected_owner_count, projected_bytes):
                return False

            for candidate_key in planned_evictions:
                evicted = self._entries.pop(candidate_key)
                self._total_bytes -= evicted.size_bytes
            replaced = self._entries.pop(cache_key, None)
            if replaced is not None:
                self._total_bytes -= replaced.size_bytes
            self._entries[cache_key] = _Capture(value, size_bytes, now)
            self._total_bytes += size_bytes
            return True

    def get(self, owner_bucket: tuple[Hashable, ...], key: Hashable) -> Any | None:
        """Return and touch a live capture, or return ``None`` when it is absent."""
        cache_key = self._cache_key(owner_bucket, key)
        with self._lock:
            now = self._clock()
            self._prune_expired(now)
            capture = self._entries.get(cache_key)
            if capture is None:
                return None
            capture.last_access = now
            self._entries.move_to_end(cache_key)
            return capture.value

    def clear(self) -> None:
        """Remove all entries and reset accounted serialized bytes."""
        with self._lock:
            self._entries.clear()
            self._total_bytes = 0

    @property
    def capture_count(self) -> int:
        """Current live entry count after pruning expired captures."""
        with self._lock:
            self._prune_expired(self._clock())
            return len(self._entries)

    @property
    def serialized_bytes(self) -> int:
        """Current accounted serialized bytes after pruning expired captures."""
        with self._lock:
            self._prune_expired(self._clock())
            return self._total_bytes

    def owner_count(self, owner_bucket: tuple[Hashable, ...]) -> int:
        """Return one bucket's live entry count without retaining owner metadata."""
        owner_bucket = self._validate_owner_bucket(owner_bucket)
        with self._lock:
            self._prune_expired(self._clock())
            return sum(bucket == owner_bucket for bucket, _ in self._entries)

    def _fits(self, count: int, owner_count: int, size_bytes: int) -> bool:
        return (
            count <= self.max_entries
            and owner_count <= self.max_per_owner
            and size_bytes <= self.max_bytes
        )

    def _prune_expired(self, now: float) -> None:
        cutoff = now - self.idle_seconds
        expired = [key for key, capture in self._entries.items() if capture.last_access <= cutoff]
        for key in expired:
            capture = self._entries.pop(key)
            self._total_bytes -= capture.size_bytes

    @classmethod
    def _cache_key(
        cls, owner_bucket: tuple[Hashable, ...], key: Hashable
    ) -> tuple[tuple[Hashable, ...], Hashable]:
        owner_bucket = cls._validate_owner_bucket(owner_bucket)
        try:
            hash(key)
        except TypeError as exc:
            raise ValueError("capture key must be hashable") from exc
        return owner_bucket, key

    @staticmethod
    def _validate_owner_bucket(owner_bucket: tuple[Hashable, ...]) -> tuple[Hashable, ...]:
        if not isinstance(owner_bucket, tuple):
            raise ValueError("owner_bucket must be a tuple")
        try:
            hash(owner_bucket)
        except TypeError as exc:
            raise ValueError("owner_bucket must be hashable") from exc
        return owner_bucket
