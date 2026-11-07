"""
agents/dlq.py — Dead Letter Queue Handler

Spec Reference: docs/spec.md#9 (Dead Letter Queue Entry Schema)
Task ID: ORCH-001

The DLQ is the terminal destination for tasks that have exhausted all retries
or hit an immediate-fail category (budget_exceeded).

DLQ Entry Schema (from spec):
  {
    "dlq_entry_id":          "<uuid>",
    "original_task_id":      "string",
    "idempotency_key":       "string",
    "failure_reason":        "string",
    "failure_category":      "enum[timeout, schema_error, model_error,
                                   tool_error, budget_exceeded]",
    "retry_count":           0,
    "last_attempt_timestamp": "<ISO8601>",
    "original_input":        {},
    "partial_result":        {}
  }

Retry Policy (from spec):
  timeout        → max_retries=3, exponential backoff (1s, 2s, 4s)
  schema_error   → max_retries=1, immediate
  model_error    → max_retries=3, exponential backoff (2s, 4s, 8s)
  tool_error     → max_retries=2, linear (3s, 3s)
  budget_exceeded → max_retries=0, route to DLQ immediately
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_DLQ_DIR = Path(__file__).parent.parent / "evals" / ".dlq"


# ---------------------------------------------------------------------------
# Retry Policy Registry
# Spec Reference: docs/spec.md#9 (Retry Policy table)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int
    backoff_strategy: str           # "exponential" | "linear" | "immediate" | "none"
    base_delay_seconds: float = 0.0 # base for backoff calculation
    max_delay_seconds: float = 30.0 # safety cap

    def compute_delay(self, attempt: int) -> float:
        """
        Return the sleep duration (seconds) before attempt number `attempt`
        (0-indexed, so attempt=0 is the first retry).
        """
        if self.backoff_strategy == "exponential":
            delay = self.base_delay_seconds * (2 ** attempt)
        elif self.backoff_strategy == "linear":
            delay = self.base_delay_seconds
        elif self.backoff_strategy == "immediate":
            delay = 0.0
        else:  # "none" or unknown
            delay = 0.0
        return min(delay, self.max_delay_seconds)


# Spec Reference: docs/spec.md#9 — Retry Policy table
RETRY_POLICIES: dict[str, RetryPolicy] = {
    "timeout":         RetryPolicy(max_retries=3, backoff_strategy="exponential", base_delay_seconds=1.0),
    "schema_error":    RetryPolicy(max_retries=1, backoff_strategy="immediate"),
    "model_error":     RetryPolicy(max_retries=3, backoff_strategy="exponential", base_delay_seconds=2.0),
    "tool_error":      RetryPolicy(max_retries=2, backoff_strategy="linear",      base_delay_seconds=3.0),
    "budget_exceeded": RetryPolicy(max_retries=0, backoff_strategy="none"),        # → DLQ immediately
}

# Default for unknown categories
_DEFAULT_POLICY = RetryPolicy(max_retries=1, backoff_strategy="immediate")


def get_retry_policy(failure_category: str) -> RetryPolicy:
    """Return the spec-mandated retry policy for a given failure category."""
    return RETRY_POLICIES.get(failure_category, _DEFAULT_POLICY)


def should_retry(failure_category: str, retry_count: int) -> bool:
    """True if another retry is permitted under the policy."""
    policy = get_retry_policy(failure_category)
    return retry_count < policy.max_retries


def compute_backoff_delay(failure_category: str, attempt: int) -> float:
    """Return sleep duration before the next attempt (in seconds)."""
    policy = get_retry_policy(failure_category)
    return policy.compute_delay(attempt)


# ---------------------------------------------------------------------------
# DLQ Entry
# ---------------------------------------------------------------------------

@dataclass
class DLQEntry:
    """
    Spec Reference: docs/spec.md#9 (Dead Letter Queue Entry Schema)
    """
    dlq_entry_id: str              = field(default_factory=lambda: str(uuid.uuid4()))
    original_task_id: str          = ""
    idempotency_key: str           = ""
    failure_reason: str            = ""
    failure_category: str          = "unknown"
    retry_count: int               = 0
    last_attempt_timestamp: str    = field(default_factory=lambda: _utcnow())
    original_input: dict           = field(default_factory=dict)
    partial_result: Optional[dict] = None
    agent_used: str                = ""
    trace_id: str                  = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "DLQEntry":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# DLQ Manager
# ---------------------------------------------------------------------------

class DeadLetterQueue:
    """
    Persistent Dead Letter Queue backed by JSON files on disk.

    Spec Reference: docs/spec.md#9 (Dead Letter Queue Entry Schema)
    Task ID: ORCH-001

    Storage layout:
        <dlq_dir>/
            <YYYY-MM-DD>/           ← date sharding
                <dlq_entry_id>.json
    """

    def __init__(self, dlq_dir: Optional[Path] = None) -> None:
        self._dlq_dir = Path(dlq_dir or DEFAULT_DLQ_DIR)
        self._dlq_dir.mkdir(parents=True, exist_ok=True)
        logger.debug("DeadLetterQueue initialised at %s", self._dlq_dir)

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def enqueue(
        self,
        task_id: str,
        idempotency_key: str,
        failure_reason: str,
        failure_category: str,
        original_input: dict,
        retry_count: int = 0,
        partial_result: Optional[dict] = None,
        agent_used: str = "",
        trace_id: str = "",
    ) -> DLQEntry:
        """
        Persist a failed task to the DLQ.
        Returns the written DLQEntry.
        """
        entry = DLQEntry(
            original_task_id=task_id,
            idempotency_key=idempotency_key,
            failure_reason=failure_reason,
            failure_category=failure_category,
            retry_count=retry_count,
            original_input=original_input,
            partial_result=partial_result,
            agent_used=agent_used,
            trace_id=trace_id,
        )
        path = self._entry_path(entry.dlq_entry_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(entry.to_dict(), indent=2, default=str),
            encoding="utf-8",
        )
        logger.error(
            "[DLQ] Enqueued — entry_id=%s task=%s category=%s retries=%d reason=%s",
            entry.dlq_entry_id[:8],
            task_id,
            failure_category,
            retry_count,
            failure_reason,
        )
        return entry

    # ------------------------------------------------------------------
    # Read / replay path
    # ------------------------------------------------------------------

    def list_entries(self, limit: int = 100) -> list[DLQEntry]:
        """Return up to `limit` DLQ entries, newest first."""
        paths = sorted(
            self._dlq_dir.rglob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:limit]
        entries = []
        for path in paths:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                entries.append(DLQEntry.from_dict(data))
            except Exception as exc:  # noqa: BLE001
                logger.warning("[DLQ] Could not read entry %s: %s", path.name, exc)
        return entries

    def get(self, dlq_entry_id: str) -> Optional[DLQEntry]:
        """Retrieve a specific DLQ entry by ID."""
        for path in self._dlq_dir.rglob(f"{dlq_entry_id}.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return DLQEntry.from_dict(data)
            except Exception as exc:  # noqa: BLE001
                logger.error("[DLQ] Could not parse entry %s: %s", dlq_entry_id, exc)
        return None

    def count(self) -> int:
        """Return total number of entries in the DLQ."""
        return sum(1 for _ in self._dlq_dir.rglob("*.json"))

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _entry_path(self, dlq_entry_id: str) -> Path:
        date_shard = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self._dlq_dir / date_shard / f"{dlq_entry_id}.json"


# ---------------------------------------------------------------------------
# Retry executor helper (used by Orchestrator)
# ---------------------------------------------------------------------------

class RetryExecutor:
    """
    Wraps a callable with spec-mandated retry logic + backoff.

    Spec Reference: docs/spec.md#9 (Retry Policy table)

    Usage:
        result = RetryExecutor("model_error").run(my_callable, *args, **kwargs)
    """

    def __init__(self, failure_category: str, dry_run: bool = False) -> None:
        self.policy = get_retry_policy(failure_category)
        self.failure_category = failure_category
        self.dry_run = dry_run

    def run(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """
        Execute `fn(*args, **kwargs)` with retries as defined by the policy.

        Raises the last exception if all retries are exhausted.
        """
        last_exc: Optional[Exception] = None
        for attempt in range(self.policy.max_retries + 1):
            if attempt > 0:
                delay = self.policy.compute_delay(attempt - 1)
                if delay > 0 and not self.dry_run:
                    logger.info(
                        "[RetryExecutor] Waiting %.1fs before retry %d/%d (category=%s)",
                        delay, attempt, self.policy.max_retries, self.failure_category,
                    )
                    time.sleep(delay)

            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning(
                    "[RetryExecutor] Attempt %d/%d failed: %s",
                    attempt + 1, self.policy.max_retries + 1, exc,
                )

        raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()
