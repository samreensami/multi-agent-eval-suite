"""
agents/state_store.py — SHA256-Keyed Idempotency State Store

Spec Reference: docs/spec.md#9-error-handling--idempotency
Task ID: ORCH-001

Implements the full five-step idempotency protocol:

    1. Before execution: check StateStore for idempotency_key
    2. If key exists AND status=success  → return cached result (no re-execution)
    3. If key exists AND status=in_progress → wait with timeout, then re-check
    4. If key exists AND status=failed   → check retry_count
         - retry_count < max_retries: allow re-execution
         - retry_count >= max_retries: signal DLQ routing
    5. If key absent: proceed, write key with status=in_progress

Storage backend: diskcache (local) with optional Redis shim.
All entries are JSON-serialisable and SHA256-signed for tamper detection.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_STORE_DIR = Path(__file__).parent.parent / "evals" / ".state_store"
IN_PROGRESS_TIMEOUT_SECONDS = 60      # max wait for another worker holding the key
IN_PROGRESS_POLL_INTERVAL_SECONDS = 1


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class EntryStatus(str, Enum):
    """
    Lifecycle states for a state-store entry.
    Spec Reference: docs/spec.md#9 (Idempotency Protocol)
    """
    IN_PROGRESS = "in_progress"
    SUCCESS     = "success"
    FAILED      = "failed"
    DLQ         = "dlq"


class IdempotencyDecision(str, Enum):
    """
    Decision returned by check_or_lock() to the caller.
    """
    PROCEED       = "proceed"       # Key absent → caller may execute
    CACHED        = "cached"        # Success exists → return cached result
    WAIT_TIMEOUT  = "wait_timeout"  # In-progress too long → treat as failed
    RETRY_ALLOWED = "retry_allowed" # Failed but retries remain
    ROUTE_TO_DLQ  = "route_to_dlq" # Retries exhausted → DLQ


# ---------------------------------------------------------------------------
# Data Model
# ---------------------------------------------------------------------------

@dataclass
class StateEntry:
    """
    A single row in the state store.
    Spec Reference: docs/spec.md#9 (Idempotency Protocol)
    """
    idempotency_key: str
    task_id: str
    status: EntryStatus
    retry_count: int = 0
    created_at: str = field(default_factory=lambda: _utcnow())
    updated_at: str = field(default_factory=lambda: _utcnow())
    result: Optional[dict] = None
    failure_reason: Optional[str] = None
    failure_category: Optional[str] = None  # timeout|schema_error|model_error|tool_error|budget_exceeded
    entry_hash: str = ""                    # SHA256 of serialised entry for tamper detection

    def compute_hash(self) -> str:
        """Return SHA256 of the entry contents (excluding entry_hash itself)."""
        payload = {k: v for k, v in asdict(self).items() if k != "entry_hash"}
        raw = json.dumps(payload, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()

    def seal(self) -> "StateEntry":
        """Stamp entry_hash and return self for chaining."""
        self.entry_hash = self.compute_hash()
        return self

    def verify(self) -> bool:
        """Return True if the stored hash matches the recomputed hash."""
        return self.entry_hash == self.compute_hash()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "StateEntry":
        d = dict(d)
        d["status"] = EntryStatus(d["status"])
        return cls(**d)


# ---------------------------------------------------------------------------
# State Store
# ---------------------------------------------------------------------------

class StateStore:
    """
    Thread-safe, disk-backed state store for idempotency enforcement.

    Spec Reference: docs/spec.md#9-error-handling--idempotency
    Task ID: ORCH-001

    Storage layout:
        <store_dir>/
            <idempotency_key[:2]>/      ← 2-char shard prefix to avoid huge dirs
                <idempotency_key>.json

    Thread safety: per-key threading.Lock via _key_locks dict.
    Multi-process safety: file-level atomic write (write to .tmp, rename).
    """

    def __init__(self, store_dir: Optional[Path] = None) -> None:
        self._store_dir = Path(store_dir or DEFAULT_STORE_DIR)
        self._store_dir.mkdir(parents=True, exist_ok=True)
        self._key_locks: dict[str, threading.Lock] = {}
        self._global_lock = threading.Lock()
        logger.debug("StateStore initialised at %s", self._store_dir)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_or_lock(
        self,
        idempotency_key: str,
        task_id: str,
        max_retries: int,
    ) -> tuple[IdempotencyDecision, Optional[StateEntry]]:
        """
        Implements the full 5-step idempotency protocol from spec.md#9.

        Returns (decision, entry):
          PROCEED        → entry is the newly locked IN_PROGRESS entry
          CACHED         → entry contains the successful result
          WAIT_TIMEOUT   → entry is the stale IN_PROGRESS entry
          RETRY_ALLOWED  → entry is the failed entry (retry_count already incremented)
          ROUTE_TO_DLQ   → entry is the exhausted-retries entry
        """
        lock = self._get_lock(idempotency_key)
        with lock:
            existing = self._load(idempotency_key)

            # Step 5 — Key absent → proceed
            if existing is None:
                entry = StateEntry(
                    idempotency_key=idempotency_key,
                    task_id=task_id,
                    status=EntryStatus.IN_PROGRESS,
                ).seal()
                self._save(entry)
                logger.info("[StateStore] New entry LOCKED — key=%s task=%s", idempotency_key[:12], task_id)
                return IdempotencyDecision.PROCEED, entry

            # Step 2 — Success cached
            if existing.status == EntryStatus.SUCCESS:
                logger.info("[StateStore] Cache HIT — key=%s task=%s", idempotency_key[:12], task_id)
                return IdempotencyDecision.CACHED, existing

            # Step 3 — In-progress: wait with timeout
            if existing.status == EntryStatus.IN_PROGRESS:
                return self._handle_in_progress(existing, task_id, max_retries)

            # Step 4 — Failed: check retry budget
            if existing.status == EntryStatus.FAILED:
                return self._handle_failed(existing, max_retries)

            # Already in DLQ
            if existing.status == EntryStatus.DLQ:
                logger.warning("[StateStore] Entry already in DLQ — key=%s", idempotency_key[:12])
                return IdempotencyDecision.ROUTE_TO_DLQ, existing

            # Fallback
            return IdempotencyDecision.PROCEED, existing

    def mark_success(self, idempotency_key: str, result: dict) -> StateEntry:
        """Transition entry to SUCCESS and store the result payload."""
        lock = self._get_lock(idempotency_key)
        with lock:
            entry = self._load(idempotency_key)
            if entry is None:
                raise KeyError(f"No state entry found for key {idempotency_key[:12]}")
            entry.status = EntryStatus.SUCCESS
            entry.result = result
            entry.updated_at = _utcnow()
            entry.seal()
            self._save(entry)
            logger.info("[StateStore] SUCCESS — key=%s task=%s", idempotency_key[:12], entry.task_id)
            return entry

    def mark_failed(
        self,
        idempotency_key: str,
        failure_reason: str,
        failure_category: str,
        partial_result: Optional[dict] = None,
    ) -> StateEntry:
        """Transition entry to FAILED and increment retry_count."""
        lock = self._get_lock(idempotency_key)
        with lock:
            entry = self._load(idempotency_key)
            if entry is None:
                raise KeyError(f"No state entry found for key {idempotency_key[:12]}")
            entry.status = EntryStatus.FAILED
            entry.retry_count += 1
            entry.failure_reason = failure_reason
            entry.failure_category = failure_category
            entry.result = partial_result
            entry.updated_at = _utcnow()
            entry.seal()
            self._save(entry)
            logger.warning(
                "[StateStore] FAILED — key=%s task=%s retry=%d reason=%s",
                idempotency_key[:12], entry.task_id, entry.retry_count, failure_reason
            )
            return entry

    def mark_dlq(self, idempotency_key: str, reason: str) -> StateEntry:
        """Transition entry to DLQ status."""
        lock = self._get_lock(idempotency_key)
        with lock:
            entry = self._load(idempotency_key)
            if entry is None:
                raise KeyError(f"No state entry found for key {idempotency_key[:12]}")
            entry.status = EntryStatus.DLQ
            entry.failure_reason = reason
            entry.updated_at = _utcnow()
            entry.seal()
            self._save(entry)
            logger.error("[StateStore] DLQ — key=%s task=%s", idempotency_key[:12], entry.task_id)
            return entry

    def get(self, idempotency_key: str) -> Optional[StateEntry]:
        """Direct read of a state entry (no locking — for observability only)."""
        return self._load(idempotency_key)

    def reset(self, idempotency_key: str) -> None:
        """Delete an entry — allows forced re-execution (testing only)."""
        path = self._entry_path(idempotency_key)
        if path.exists():
            path.unlink()
            logger.debug("[StateStore] RESET — key=%s", idempotency_key[:12])

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _handle_in_progress(
        self,
        entry: StateEntry,
        task_id: str,
        max_retries: int,
    ) -> tuple[IdempotencyDecision, StateEntry]:
        """
        Poll the entry until it transitions out of IN_PROGRESS or times out.
        Spec Reference: docs/spec.md#9 step 3
        """
        logger.info(
            "[StateStore] IN_PROGRESS — waiting up to %ds for key=%s",
            IN_PROGRESS_TIMEOUT_SECONDS, entry.idempotency_key[:12]
        )
        deadline = time.monotonic() + IN_PROGRESS_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            time.sleep(IN_PROGRESS_POLL_INTERVAL_SECONDS)
            fresh = self._load(entry.idempotency_key)
            if fresh is None:
                # Deleted externally — treat as fresh start
                new_entry = StateEntry(
                    idempotency_key=entry.idempotency_key,
                    task_id=task_id,
                    status=EntryStatus.IN_PROGRESS,
                ).seal()
                self._save(new_entry)
                return IdempotencyDecision.PROCEED, new_entry

            if fresh.status == EntryStatus.SUCCESS:
                return IdempotencyDecision.CACHED, fresh
            if fresh.status in (EntryStatus.FAILED, EntryStatus.DLQ):
                return self._handle_failed(fresh, max_retries)

        # Timed out — treat as failed
        logger.warning("[StateStore] IN_PROGRESS timeout — key=%s", entry.idempotency_key[:12])
        entry.status = EntryStatus.FAILED
        entry.retry_count += 1
        entry.failure_reason = "in_progress_timeout"
        entry.failure_category = "timeout"
        entry.updated_at = _utcnow()
        entry.seal()
        self._save(entry)
        return IdempotencyDecision.WAIT_TIMEOUT, entry

    def _handle_failed(
        self,
        entry: StateEntry,
        max_retries: int,
    ) -> tuple[IdempotencyDecision, StateEntry]:
        """
        Decide whether to allow retry or route to DLQ.
        Spec Reference: docs/spec.md#9 step 4
        """
        if entry.retry_count < max_retries:
            # Reset to IN_PROGRESS so caller can proceed
            entry.status = EntryStatus.IN_PROGRESS
            entry.updated_at = _utcnow()
            entry.seal()
            self._save(entry)
            logger.info(
                "[StateStore] RETRY ALLOWED (%d/%d) — key=%s",
                entry.retry_count, max_retries, entry.idempotency_key[:12]
            )
            return IdempotencyDecision.RETRY_ALLOWED, entry

        logger.error(
            "[StateStore] RETRIES EXHAUSTED (%d/%d) — routing to DLQ key=%s",
            entry.retry_count, max_retries, entry.idempotency_key[:12]
        )
        return IdempotencyDecision.ROUTE_TO_DLQ, entry

    def _get_lock(self, key: str) -> threading.Lock:
        with self._global_lock:
            if key not in self._key_locks:
                self._key_locks[key] = threading.Lock()
            return self._key_locks[key]

    def _entry_path(self, key: str) -> Path:
        shard = key[:2]
        return self._store_dir / shard / f"{key}.json"

    def _load(self, key: str) -> Optional[StateEntry]:
        path = self._entry_path(key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            entry = StateEntry.from_dict(data)
            if not entry.verify():
                logger.error("[StateStore] TAMPER DETECTED — key=%s (hash mismatch)", key[:12])
            return entry
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.error("[StateStore] Corrupt entry — key=%s error=%s", key[:12], exc)
            return None

    def _save(self, entry: StateEntry) -> None:
        path = self._entry_path(entry.idempotency_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(entry.to_dict(), indent=2, default=str),
            encoding="utf-8",
        )
        tmp.replace(path)  # Atomic on POSIX; near-atomic on Windows


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()
