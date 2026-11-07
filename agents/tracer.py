"""
agents/tracer.py — OpenTelemetry-Compatible Trace Emitter

Spec Reference: docs/spec.md#32-event-schema
                docs/spec.md#10-versioning--observability
Task ID: ORCH-001

Emits structured trace events conforming to the canonical event envelope:

    {
      "schema_version":  "1.0",
      "event_id":        "<uuid-v4>",
      "idempotency_key": "<sha256>",
      "task_id":         "TASK-FIN-001",
      "timestamp_utc":   "<ISO8601>",
      "source":          "<component_name>",
      "event_type":      "<START|STEP|END|ERROR|DLQ>",
      "payload":         {},
      "trace_context":   { "trace_id": "...", "span_id": "..." }
    }

Design:
  - Works standalone (structured JSON logs) with no OTel SDK dependency.
  - If opentelemetry-sdk is installed, also exports real OTEL spans.
  - Mandatory span attributes (spec.md#10) are enforced at emit time.
  - Trace events are appended to evals/.traces/<trace_id>.jsonl for replay.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Generator, Optional

logger = logging.getLogger(__name__)

TRACES_DIR = Path(__file__).parent.parent / "evals" / ".traces"
SCHEMA_VERSION = "1.0"


# ---------------------------------------------------------------------------
# Event types — mirrors spec.md#32
# ---------------------------------------------------------------------------

class EventType(str, Enum):
    START = "START"
    STEP  = "STEP"
    END   = "END"
    ERROR = "ERROR"
    DLQ   = "DLQ"


# ---------------------------------------------------------------------------
# Required span attribute keys  (spec.md#10 — Trace Attribute Standards)
# ---------------------------------------------------------------------------

REQUIRED_SPAN_ATTRS = frozenset({
    "task_id",
    "agent_name",
    "idempotency_key",
})

# Optional but strongly recommended
RECOMMENDED_SPAN_ATTRS = frozenset({
    "skill_name",
    "model_name",
    "input_tokens",
    "output_tokens",
    "cost_usd",
})


# ---------------------------------------------------------------------------
# Tracer
# ---------------------------------------------------------------------------

class Tracer:
    """
    Structured trace emitter.

    Spec Reference: docs/spec.md#32-event-schema, docs/spec.md#10
    Task ID: ORCH-001

    Thread-safe. Each Tracer instance owns a single trace_id (a logical
    unit of work, e.g. one benchmark run). Individual spans/events get
    their own span_id.
    """

    def __init__(
        self,
        source: str,
        trace_id: Optional[str] = None,
        traces_dir: Optional[Path] = None,
        enable_file_sink: bool = True,
    ) -> None:
        self.source = source
        self.trace_id = trace_id or _new_id()
        self._traces_dir = Path(traces_dir or TRACES_DIR)
        self._enable_file_sink = enable_file_sink
        self._lock = threading.Lock()

        if enable_file_sink:
            self._traces_dir.mkdir(parents=True, exist_ok=True)
            self._trace_file = self._traces_dir / f"{self.trace_id}.jsonl"
        else:
            self._trace_file = None

        # Try to import OTel (optional)
        self._otel_tracer = _try_get_otel_tracer(source)

    # ------------------------------------------------------------------
    # Core emit
    # ------------------------------------------------------------------

    def emit(
        self,
        event_type: EventType,
        task_id: str,
        span_name: str,
        attributes: Optional[dict] = None,
        payload: Optional[dict] = None,
        idempotency_key: str = "",
    ) -> dict:
        """
        Emit one trace event.

        Returns the event envelope as a dict (useful for tests).
        Spec Reference: docs/spec.md#32-event-schema
        """
        attrs = attributes or {}
        event: dict[str, Any] = {
            "schema_version":  SCHEMA_VERSION,
            "event_id":        _new_id(),
            "idempotency_key": idempotency_key,
            "task_id":         task_id,
            "timestamp_utc":   _utcnow(),
            "source":          self.source,
            "event_type":      event_type.value,
            "span_name":       span_name,
            "payload":         payload or {},
            "attributes":      attrs,
            "trace_context": {
                "trace_id": self.trace_id,
                "span_id":  _new_id(),
            },
        }

        self._warn_missing_attrs(span_name, attrs)
        self._log(event)
        self._write_to_file(event)
        self._emit_otel(event_type, span_name, attrs)

        return event

    # ------------------------------------------------------------------
    # Convenience wrappers
    # ------------------------------------------------------------------

    def start(self, task_id: str, agent_name: str, idempotency_key: str, **extra: Any) -> dict:
        return self.emit(
            EventType.START, task_id,
            span_name=f"{agent_name}.start",
            attributes={"agent_name": agent_name, "task_id": task_id,
                        "idempotency_key": idempotency_key, **extra},
            idempotency_key=idempotency_key,
        )

    def step(self, task_id: str, agent_name: str, skill_name: str,
             idempotency_key: str = "", **extra: Any) -> dict:
        return self.emit(
            EventType.STEP, task_id,
            span_name=f"{agent_name}.{skill_name}",
            attributes={"agent_name": agent_name, "skill_name": skill_name,
                        "task_id": task_id, "idempotency_key": idempotency_key, **extra},
            idempotency_key=idempotency_key,
        )

    def end(self, task_id: str, agent_name: str, idempotency_key: str,
            status: str = "success", cost_usd: float = 0.0,
            total_latency_ms: int = 0, **extra: Any) -> dict:
        return self.emit(
            EventType.END, task_id,
            span_name=f"{agent_name}.end",
            attributes={
                "agent_name": agent_name, "task_id": task_id,
                "idempotency_key": idempotency_key, "status": status,
                "cost_usd": cost_usd, "total_latency_ms": total_latency_ms,
                **extra,
            },
            idempotency_key=idempotency_key,
        )

    def error(self, task_id: str, agent_name: str, idempotency_key: str,
              error_code: str, error_message: str, **extra: Any) -> dict:
        return self.emit(
            EventType.ERROR, task_id,
            span_name=f"{agent_name}.error",
            attributes={
                "agent_name": agent_name, "task_id": task_id,
                "idempotency_key": idempotency_key,
                "error_code": error_code, "error_message": error_message,
                **extra,
            },
            idempotency_key=idempotency_key,
        )

    def dlq(self, task_id: str, agent_name: str, idempotency_key: str,
            dlq_entry_id: str, failure_reason: str, **extra: Any) -> dict:
        return self.emit(
            EventType.DLQ, task_id,
            span_name=f"{agent_name}.dlq",
            attributes={
                "agent_name": agent_name, "task_id": task_id,
                "idempotency_key": idempotency_key,
                "dlq_entry_id": dlq_entry_id,
                "failure_reason": failure_reason,
                **extra,
            },
            idempotency_key=idempotency_key,
        )

    # ------------------------------------------------------------------
    # Context manager — timed span
    # ------------------------------------------------------------------

    @contextmanager
    def span(
        self,
        task_id: str,
        agent_name: str,
        skill_name: str,
        idempotency_key: str = "",
        **extra: Any,
    ) -> Generator[dict, None, None]:
        """
        Context manager that emits a STEP event on enter and records
        elapsed time. Emits ERROR on exception.

        Usage:
            with tracer.span(task_id, "FinancialReconAgent", "reconcile") as ctx:
                result = do_work()
                ctx["result"] = result
        """
        import time as _time
        ctx: dict[str, Any] = {}
        t0 = _time.monotonic()
        self.step(task_id, agent_name, f"{skill_name}.begin",
                  idempotency_key=idempotency_key, **extra)
        try:
            yield ctx
        except Exception as exc:  # noqa: BLE001
            elapsed_ms = int((_time.monotonic() - t0) * 1000)
            self.error(
                task_id, agent_name, idempotency_key,
                error_code="span_exception",
                error_message=str(exc),
                skill_name=skill_name,
                latency_ms=elapsed_ms,
            )
            raise
        else:
            elapsed_ms = int((_time.monotonic() - t0) * 1000)
            self.step(
                task_id, agent_name, f"{skill_name}.end",
                idempotency_key=idempotency_key,
                latency_ms=elapsed_ms,
                **{k: v for k, v in ctx.items() if isinstance(v, (str, int, float, bool))},
            )

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _warn_missing_attrs(self, span_name: str, attrs: dict) -> None:
        missing = REQUIRED_SPAN_ATTRS - set(attrs.keys())
        if missing:
            logger.debug(
                "[Tracer] span=%s is missing required attributes: %s",
                span_name, missing,
            )

    def _log(self, event: dict) -> None:
        level = logging.ERROR if event["event_type"] in ("ERROR", "DLQ") else logging.DEBUG
        logger.log(level, "[Trace] %s %s task=%s",
                   event["event_type"], event["span_name"], event["task_id"])

    def _write_to_file(self, event: dict) -> None:
        if not self._enable_file_sink or self._trace_file is None:
            return
        try:
            with self._lock:
                with self._trace_file.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(event, default=str) + "\n")
        except OSError as exc:
            logger.warning("[Tracer] Could not write trace file: %s", exc)

    def _emit_otel(self, event_type: EventType, span_name: str, attrs: dict) -> None:
        if self._otel_tracer is None:
            return
        try:
            with self._otel_tracer.start_as_current_span(span_name) as span:
                for k, v in attrs.items():
                    if isinstance(v, (str, int, float, bool)):
                        span.set_attribute(k, v)
                if event_type == EventType.ERROR:
                    span.set_status(
                        __import__(
                            "opentelemetry.trace",
                            fromlist=["StatusCode"]
                        ).StatusCode.ERROR
                    )
        except Exception:  # noqa: BLE001
            pass  # OTel is optional — never crash the main path


# ---------------------------------------------------------------------------
# Module-level convenience instance (re-use across a process lifetime)
# ---------------------------------------------------------------------------

_global_tracer: Optional[Tracer] = None
_global_tracer_lock = threading.Lock()


def get_tracer(source: str = "orchestrator") -> Tracer:
    """Return (or lazily create) the process-global Tracer instance."""
    global _global_tracer
    with _global_tracer_lock:
        if _global_tracer is None:
            _global_tracer = Tracer(source=source)
        return _global_tracer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _new_id() -> str:
    return str(uuid.uuid4())


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _try_get_otel_tracer(name: str) -> Any:
    """Return an OTel tracer if the SDK is installed, else None."""
    try:
        from opentelemetry import trace as otel_trace  # type: ignore
        return otel_trace.get_tracer(name)
    except ImportError:
        return None
