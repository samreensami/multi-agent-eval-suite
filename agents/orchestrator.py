"""
agents/orchestrator.py — Master Orchestrator Agent

Spec Reference: docs/spec.md#4-orchestrator-contract
Task ID: ORCH-001
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional

from jsonschema import Draft7Validator, ValidationError

from agents.data_extraction_agent import DataExtractionAgent, ExtractionInput
from agents.dlq import DeadLetterQueue, RetryPolicy, compute_backoff_delay, get_retry_policy
from agents.financial_recon_agent import FinancialReconAgent, FinancialReconInput
from agents.notifications import send_email_alert
from agents.report_gen_agent import ReportGenAgent, ReportGenInput
from agents.state_store import (
    EntryStatus,
    IdempotencyDecision,
    StateStore,
)
from agents.tracer import EventType as TraceEventType
from agents.tracer import Tracer, get_tracer

logger = logging.getLogger(__name__)


class TaskDomain(str, Enum):
    """Supported task domains. Spec Reference: docs/spec.md#1-overview."""

    FINANCIAL = "financial"
    EXTRACTION = "extraction"
    REPORTING = "reporting"
    COMPLIANCE = "compliance"


class TaskStatus(str, Enum):
    """Task execution status. Spec Reference: docs/spec.md#4."""

    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    DLQ = "dlq"
    IN_PROGRESS = "in_progress"
    PLACEHOLDER = "placeholder"


class EventType(str, Enum):
    """Trace event types. Spec Reference: docs/spec.md#32-event-schema."""

    START = "START"
    STEP = "STEP"
    END = "END"
    ERROR = "ERROR"
    DLQ = "DLQ"


@dataclass
class OrchestratorInput:
    """Input contract for the Orchestrator. Spec Reference: docs/spec.md#4."""

    task_id: str
    domain: TaskDomain
    priority: str
    input: dict[str, Any]
    context: dict[str, Any] = field(default_factory=dict)
    constraints: dict[str, Any] = field(default_factory=lambda: {
        "max_latency_ms": 30000,
        "max_cost_usd": 0.50,
        "require_citation": True,
    })
    gold_standard: Optional[dict[str, Any]] = None
    evaluation_config: dict[str, Any] = field(default_factory=dict)

    def to_idempotency_key(self) -> str:
        """SHA256(task_id + canonical input). Spec Reference: docs/spec.md#9."""
        content = json.dumps(
            {"task_id": self.task_id, "input": self.input},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(content.encode("utf-8")).hexdigest()


@dataclass
class AuditStep:
    """One audited specialist execution step. Spec Reference: docs/spec.md#4."""

    step_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    skill_used: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    timestamp_utc: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class EvalMetrics:
    """Per-run evaluation metrics."""

    accuracy_score: Optional[float] = None
    f1_score: Optional[float] = None
    hallucination_rate: Optional[float] = None
    p95_latency_ms: Optional[int] = None
    confidence: Optional[float] = None
    cost_usd: Optional[float] = None
    token_cost_usd: Optional[float] = None
    total_latency_ms: Optional[int] = None
    reconciliation_recall: Optional[float] = None


@dataclass
class OrchestratorOutput:
    """Output contract for the Orchestrator. Spec Reference: docs/spec.md#4."""

    task_id: str
    idempotency_key: str
    status: TaskStatus
    agent_used: str
    result: Optional[dict[str, Any]] = None
    audit_trail: list[AuditStep] = field(default_factory=list)
    eval_metrics: EvalMetrics = field(default_factory=EvalMetrics)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "idempotency_key": self.idempotency_key,
            "status": self.status.value,
            "agent_used": self.agent_used,
            "result": self.result,
            "audit_trail": [asdict(step) for step in self.audit_trail],
            "eval_metrics": asdict(self.eval_metrics),
        }


class _UnrecoverableFailure(Exception):
    def __init__(
        self,
        reason: str,
        category: str,
        retry_count: int,
        partial_result: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(reason)
        self.category = category
        self.retry_count = retry_count
        self.partial_result = partial_result


class Orchestrator:
    """Routes benchmark tasks to the three implemented specialist agents."""

    AGENT_REGISTRY: dict[TaskDomain, str] = {
        TaskDomain.FINANCIAL: "FinancialReconAgent",
        TaskDomain.EXTRACTION: "DataExtractionAgent",
        TaskDomain.REPORTING: "ReportGenAgent",
    }

    EXECUTION_GRAPH: dict[TaskDomain, str] = {
        TaskDomain.EXTRACTION: "data_extraction",
        TaskDomain.FINANCIAL: "financial_reconciliation",
        TaskDomain.REPORTING: "report_generation",
    }

    MAX_RETRIES = 3

    _INPUT_SCHEMA = {
        "type": "object",
        "required": ["task_id", "domain", "priority", "input"],
        "properties": {
            "task_id": {"type": "string", "pattern": r"^TASK-[A-Z]+-[0-9]+$"},
            "domain": {"type": "string", "enum": [domain.value for domain in TaskDomain]},
            "priority": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
            "input": {"type": "object"},
            "context": {"type": "object"},
            "constraints": {
                "type": "object",
                "properties": {
                    "max_latency_ms": {"type": "number", "minimum": 0},
                    "max_cost_usd": {"type": "number", "minimum": 0},
                    "require_citation": {"type": "boolean"},
                },
                "additionalProperties": True,
            },
        },
        "additionalProperties": True,
    }

    def __init__(self, config: Optional[dict[str, Any]] = None) -> None:
        self.config = config or {}
        self._tracer: Tracer = self.config.get("tracer") or get_tracer("Orchestrator")
        self._state_store: StateStore = self.config.get("state_store") or StateStore(
            self.config.get("state_store_dir")
        )
        self._dlq: DeadLetterQueue = self.config.get("dlq") or DeadLetterQueue(
            self.config.get("dlq_dir")
        )
        self._sleep = self.config.get("sleep_fn", time.sleep)
        self._alert_sender: Callable[[str, str, str], bool] = self.config.get(
            "alert_sender", send_email_alert
        )
        logger.info("Orchestrator initialized", extra={"spec_ref": "ORCH-001"})

    def execute(self, task_input: OrchestratorInput) -> OrchestratorOutput:
        """Validate, deduplicate, route, evaluate, and persist one task run."""
        idempotency_key = task_input.to_idempotency_key()
        self._emit_trace(
            EventType.START,
            task_input.task_id,
            "orchestrator.execute",
            {"idempotency_key": idempotency_key},
        )

        if not self._validate_input(task_input):
            message = "Task input does not conform to the Orchestrator input schema."
            self._emit_trace(EventType.ERROR, task_input.task_id, "orchestrator.validate", {
                "idempotency_key": idempotency_key,
                "failure_category": "schema_error",
                "error_message": message,
            })
            return OrchestratorOutput(
                task_id=task_input.task_id,
                idempotency_key=idempotency_key,
                status=TaskStatus.FAILED,
                agent_used="Orchestrator",
                result={"error": message},
                eval_metrics=_empty_metrics(),
            )

        try:
            cached = self._check_idempotency(idempotency_key, task_input)
            if cached is not None:
                self._emit_trace(EventType.END, task_input.task_id, "orchestrator.cached", {
                    "idempotency_key": idempotency_key,
                    "status": cached.status.value,
                })
                return cached

            domain = self._classify_domain(task_input)
            skill_name = self.EXECUTION_GRAPH.get(domain)
            if skill_name is None:
                raise ValueError(f"No execution graph is registered for domain {domain.value!r}.")
            self._emit_trace(EventType.STEP, task_input.task_id, "orchestrator.route", {
                "idempotency_key": idempotency_key,
                "domain": domain.value,
                "skill_name": skill_name,
            })

            started = time.monotonic()
            agent_name, result = self._execute_with_retries(task_input, domain, idempotency_key)
            latency_ms = int((time.monotonic() - started) * 1000)

            tokens_in = _token_count(result, "input_tokens")
            tokens_out = _token_count(result, "output_tokens")
            audit_trail = [AuditStep(
                skill_used=skill_name,
                input_tokens=tokens_in,
                output_tokens=tokens_out,
                latency_ms=latency_ms,
            )]
            metrics = self._evaluate(domain, result, task_input, audit_trail)
            status = self._result_status(domain, result, metrics)
            output = OrchestratorOutput(
                task_id=task_input.task_id,
                idempotency_key=idempotency_key,
                status=status,
                agent_used=agent_name,
                result=result,
                audit_trail=audit_trail,
                eval_metrics=metrics,
            )
            self._state_store.mark_success(idempotency_key, output.to_dict())
            self._emit_trace(EventType.END, task_input.task_id, "orchestrator.complete", {
                "idempotency_key": idempotency_key,
                "agent_name": agent_name,
                "input_tokens": tokens_in,
                "output_tokens": tokens_out,
                "cost_usd": metrics.token_cost_usd or 0.0,
                "total_latency_ms": latency_ms,
                "status": status.value,
            })
            self._notify_if_anomalous(task_input, domain, result)
            return output
        except _UnrecoverableFailure as exc:
            return self._handle_dlq(
                task_input,
                idempotency_key,
                str(exc),
                exc.retry_count,
                exc.category,
                exc.partial_result,
            )
        except Exception as exc:
            logger.exception(
                "Orchestration failed",
                extra={"task_id": task_input.task_id, "idempotency_key": idempotency_key},
            )
            entry = self._state_store.get(idempotency_key)
            retry_count = entry.retry_count if entry else 0
            failure_category, _ = self._policy_for_error(exc)
            if entry is not None and entry.status != EntryStatus.DLQ:
                self._state_store.mark_failed(
                    idempotency_key,
                    str(exc),
                    failure_category,
                    partial_result=None,
                )
                retry_count = self._state_store.get(idempotency_key).retry_count
            return self._handle_dlq(
                task_input,
                idempotency_key,
                str(exc),
                retry_count,
                failure_category,
            )

    def _notify_if_anomalous(
        self,
        task_input: OrchestratorInput,
        domain: TaskDomain,
        result: dict[str, Any],
    ) -> None:
        """Email a concise alert for financial discrepancies or low confidence."""
        alert_status: Optional[str] = None
        summary = ""

        if domain == TaskDomain.FINANCIAL:
            discrepancies = [
                item for item in result.get("discrepancies", [])
                if isinstance(item, dict)
            ]
            if discrepancies:
                severe = any(
                    str(item.get("severity", "")).lower() in {"high", "critical"}
                    for item in discrepancies
                )
                alert_status = "FAIL" if severe else "PARTIAL"
                descriptions = []
                for item in discrepancies[:3]:
                    severity = str(item.get("severity", "unspecified")).upper()
                    kind = str(item.get("discrepancy_type", "financial discrepancy"))
                    details = str(item.get("details", "")).strip()
                    invoice_id = item.get("invoice_id")
                    label = f"{severity} {kind}"
                    if invoice_id:
                        label += f" for invoice {invoice_id}"
                    if details:
                        label += f": {details}"
                    descriptions.append(label)
                remaining = len(discrepancies) - len(descriptions)
                summary = "; ".join(descriptions)
                if remaining:
                    summary += f"; and {remaining} additional discrepancy(ies)"

        elif domain == TaskDomain.EXTRACTION:
            low_confidence_fields = result.get("low_confidence_fields", [])
            if isinstance(low_confidence_fields, list) and low_confidence_fields:
                confidence_scores = result.get("confidence_scores", {})
                field_details = []
                for field_name in low_confidence_fields[:8]:
                    confidence = (
                        confidence_scores.get(field_name)
                        if isinstance(confidence_scores, dict) else None
                    )
                    if isinstance(confidence, (int, float)):
                        field_details.append(f"{field_name} ({confidence:.2f})")
                    else:
                        field_details.append(str(field_name))
                summary = "Low-confidence extraction fields: " + ", ".join(field_details)
                if len(low_confidence_fields) > len(field_details):
                    summary += f"; and {len(low_confidence_fields) - len(field_details)} more"
                alert_status = "PARTIAL"

        if alert_status is None:
            return

        try:
            self._alert_sender(task_input.task_id, alert_status, summary)
        except Exception:
            logger.exception(
                "Failed to send anomaly email alert",
                extra={
                    "task_id": task_input.task_id,
                    "alert_status": alert_status,
                    "domain": domain.value,
                },
            )

    def _validate_input(self, task_input: OrchestratorInput) -> bool:
        """Validate the typed input using the TaskSpec-compatible JSON Schema."""
        if not isinstance(task_input, OrchestratorInput):
            logger.error("Expected OrchestratorInput, received %s", type(task_input).__name__)
            return False
        payload = {
            "task_id": task_input.task_id,
            "domain": task_input.domain.value if isinstance(task_input.domain, TaskDomain) else task_input.domain,
            "priority": task_input.priority,
            "input": task_input.input,
            "context": task_input.context,
            "constraints": task_input.constraints,
        }
        try:
            Draft7Validator(self._INPUT_SCHEMA).validate(payload)
        except ValidationError as exc:
            logger.error(
                "Invalid task input at %s: %s",
                ".".join(str(part) for part in exc.absolute_path) or "<root>",
                exc.message,
            )
            return False
        return True

    def _check_idempotency(
        self,
        idempotency_key: str,
        task_input: OrchestratorInput,
    ) -> Optional[OrchestratorOutput]:
        """Lock new work, return a successful cache hit, or route exhausted work."""
        previous = self._state_store.get(idempotency_key)
        if previous is not None and not previous.verify():
            raise RuntimeError(f"Idempotency state hash validation failed for {idempotency_key}.")
        if previous is not None and previous.status == EntryStatus.DLQ:
            return OrchestratorOutput(
                task_id=task_input.task_id,
                idempotency_key=idempotency_key,
                status=TaskStatus.DLQ,
                agent_used=self.AGENT_REGISTRY.get(TaskDomain(task_input.domain), "Orchestrator"),
                result={
                    "error": previous.failure_reason or "Task is already in the DLQ.",
                    "failure_category": previous.failure_category,
                    "retry_count": previous.retry_count,
                },
                eval_metrics=_empty_metrics(TaskDomain(task_input.domain)),
            )
        policy = get_retry_policy(previous.failure_category or "tool_error") if previous else None
        retry_limit = (policy.max_retries + 1) if policy else self.MAX_RETRIES + 1
        decision, entry = self._state_store.check_or_lock(
            idempotency_key,
            task_input.task_id,
            max_retries=retry_limit,
        )
        self._emit_trace(EventType.STEP, task_input.task_id, "orchestrator.idempotency", {
            "idempotency_key": idempotency_key,
            "decision": decision.value,
            "state_hash": entry.entry_hash if entry else "",
        })

        if entry is not None and not entry.verify():
            raise RuntimeError(f"Idempotency state hash validation failed for {idempotency_key}.")
        if decision == IdempotencyDecision.CACHED and entry and entry.result:
            return _output_from_dict(entry.result)
        if decision == IdempotencyDecision.ROUTE_TO_DLQ:
            reason = (entry.failure_reason if entry else None) or "Retries exhausted."
            category = (entry.failure_category if entry else None) or "tool_error"
            return self._handle_dlq(
                task_input,
                idempotency_key,
                reason,
                entry.retry_count if entry else retry_limit,
                category,
                entry.result if entry else None,
            )
        if decision == IdempotencyDecision.WAIT_TIMEOUT:
            reason = "Timed out waiting for an existing in-progress task."
            raise _UnrecoverableFailure(reason, "timeout", entry.retry_count if entry else 0)
        return None

    def _classify_domain(self, task_input: OrchestratorInput) -> TaskDomain:
        """Use task IDs and task payload signals, honoring a valid explicit domain."""
        task_prefix = task_input.task_id.split("-")[1] if "-" in task_input.task_id else ""
        prefix_domain = {
            "FIN": TaskDomain.FINANCIAL,
            "ETL": TaskDomain.EXTRACTION,
            "RPT": TaskDomain.REPORTING,
            "CPL": TaskDomain.COMPLIANCE,
        }.get(task_prefix)
        explicit_domain = TaskDomain(task_input.domain)
        if prefix_domain is not None:
            if explicit_domain != prefix_domain:
                raise ValueError(
                    f"Task ID domain {prefix_domain.value!r} conflicts with "
                    f"input domain {explicit_domain.value!r}."
                )
            return prefix_domain

        searchable = json.dumps(task_input.input, sort_keys=True, default=str).lower()
        signals = (
            (TaskDomain.FINANCIAL, ("purchase_orders", "invoices", "reconciliation", "po_id")),
            (TaskDomain.EXTRACTION, ("source_ref", "source_type", "target_schema", "extraction_hints")),
            (TaskDomain.REPORTING, ("report_type", "data_sources", "output_format", "sections")),
        )
        for domain, keywords in signals:
            if any(keyword in searchable for keyword in keywords):
                return domain
        return explicit_domain

    def _route_to_agent(
        self,
        task_input: OrchestratorInput,
        domain: TaskDomain,
        idempotency_key: str,
    ) -> tuple[str, dict[str, Any]]:
        """Run the domain agent with its typed input and injected shared context."""
        context = dict(task_input.context)
        input_data = task_input.input

        if domain == TaskDomain.EXTRACTION:
            agent_input = ExtractionInput(
                source_ref=input_data.get("source_ref", input_data.get("source", "")),
                source_type=input_data.get("source_type", "auto"),
                target_schema=input_data.get("target_schema"),
                extraction_hints=input_data.get("extraction_hints", []),
                confidence_threshold=input_data.get("confidence_threshold", 0.85),
            )
            agent = DataExtractionAgent(tracer=self._tracer)
        elif domain == TaskDomain.FINANCIAL:
            agent_input = FinancialReconInput(
                purchase_orders=input_data.get("purchase_orders", []),
                invoices=input_data.get("invoices", []),
                fiscal_period=input_data.get(
                    "fiscal_period", context.get("fiscal_period", "UNKNOWN")
                ),
                base_currency=input_data.get("base_currency", "USD"),
                exchange_rates=input_data.get("exchange_rates", {}),
                matching_config=input_data.get("matching_config", {}),
            )
            agent = FinancialReconAgent(tracer=self._tracer)
        elif domain == TaskDomain.REPORTING:
            agent_input = ReportGenInput(
                report_type=input_data.get("report_type", "executive_summary"),
                data_sources=input_data.get("data_sources", []),
                output_format=input_data.get("output_format", "markdown"),
                tone=input_data.get("tone", "executive"),
                sections=input_data.get("sections"),
                metadata=input_data.get("metadata"),
            )
            agent = ReportGenAgent(tracer=self._tracer)
        else:
            raise ValueError(f"No specialist agent is implemented for {domain.value!r}.")

        output = agent.run(
            task_id=task_input.task_id,
            agent_input=agent_input,
            idempotency_key=idempotency_key,
            context=context,
        )
        result = _as_dict(output)
        return self.AGENT_REGISTRY[domain], result

    def _execute_with_retries(
        self,
        task_input: OrchestratorInput,
        domain: TaskDomain,
        idempotency_key: str,
    ) -> tuple[str, dict[str, Any]]:
        """Retry specialist execution according to the spec's category matrix."""
        while True:
            try:
                agent_name, result = self._route_to_agent(task_input, domain, idempotency_key)
                self._validate_result(domain, result)
                return agent_name, result
            except Exception as exc:
                category, policy = self._policy_for_error(exc)
                partial_result = getattr(exc, "partial_result", None)
                failed_entry = self._state_store.mark_failed(
                    idempotency_key,
                    str(exc),
                    category,
                    partial_result=partial_result,
                )
                retry_limit = policy.max_retries + 1
                decision, _ = self._state_store.check_or_lock(
                    idempotency_key,
                    task_input.task_id,
                    max_retries=retry_limit,
                )
                self._emit_trace(EventType.ERROR, task_input.task_id, "orchestrator.agent_attempt", {
                    "idempotency_key": idempotency_key,
                    "failure_category": category,
                    "retry_count": failed_entry.retry_count,
                    "error_message": str(exc),
                })
                if decision != IdempotencyDecision.RETRY_ALLOWED:
                    raise _UnrecoverableFailure(
                        str(exc),
                        category,
                        failed_entry.retry_count,
                        partial_result,
                    ) from exc

                delay = compute_backoff_delay(category, max(failed_entry.retry_count - 1, 0))
                if delay > 0 and not self.config.get("dry_run_retries", False):
                    self._sleep(delay)

    def _policy_for_error(self, error: Exception) -> tuple[str, RetryPolicy]:
        """Map specialist error codes onto the documented retry matrix."""
        code = getattr(error, "code", "")
        code_policies: dict[str, tuple[str, RetryPolicy]] = {
            "ETL-003": ("timeout", get_retry_policy("timeout")),
            "ETL-006": ("tool_error", get_retry_policy("tool_error")),
            "FIN-004": ("timeout", get_retry_policy("timeout")),
            "FIN-005": ("model_error", get_retry_policy("model_error")),
            "FIN-007": ("timeout", get_retry_policy("timeout")),
            "RPT-003": ("tool_error", get_retry_policy("tool_error")),
            "RPT-004": ("tool_error", get_retry_policy("tool_error")),
        }
        if code in code_policies:
            return code_policies[code]

        permanent_codes = {
            "ETL-001", "ETL-002", "ETL-004", "ETL-005",
            "FIN-001", "FIN-002", "FIN-003", "FIN-006",
            "RPT-001", "RPT-002", "RPT-005", "RPT-006",
        }
        if code in permanent_codes:
            return "schema_error", RetryPolicy(max_retries=0, backoff_strategy="none")
        if isinstance(error, (TimeoutError,)):
            return "timeout", get_retry_policy("timeout")
        if isinstance(error, ValidationError):
            return "schema_error", get_retry_policy("schema_error")
        if isinstance(error, (ValueError, KeyError, TypeError)):
            return "schema_error", get_retry_policy("schema_error")
        if isinstance(error, (ConnectionError, OSError)):
            return "tool_error", get_retry_policy("tool_error")
        return "model_error", get_retry_policy("model_error")

    def _validate_result(self, domain: TaskDomain, result: dict[str, Any]) -> None:
        """Reject malformed agent outputs before caching them as successful."""
        required_fields = {
            TaskDomain.EXTRACTION: {
                "extracted_data", "confidence_scores", "low_confidence_fields",
                "source_citation", "extraction_method",
            },
            TaskDomain.FINANCIAL: {
                "matched_pairs", "unmatched_pos", "unmatched_invoices",
                "discrepancies", "reconciliation_summary", "requires_human_review",
            },
            TaskDomain.REPORTING: {
                "report_content", "citations", "word_count", "sections_generated",
                "visualization_artifacts", "generation_metadata", "hallucination_report",
            },
        }
        if not isinstance(result, dict):
            raise ValidationError("Agent output must be a JSON object.")
        missing = required_fields.get(domain, set()) - result.keys()
        if missing:
            raise ValidationError(f"Agent output is missing required fields: {sorted(missing)}")

    def _evaluate(
        self,
        domain: TaskDomain,
        result: dict[str, Any],
        task_input: OrchestratorInput,
        audit_trail: list[AuditStep],
    ) -> EvalMetrics:
        gold = task_input.gold_standard or {}
        if domain == TaskDomain.EXTRACTION:
            expected = gold.get("extracted_data", {})
            accuracy, f1 = _object_scores(
                result.get("extracted_data", {}),
                expected,
                task_input.evaluation_config.get("field_weights", {}),
            )
            confidence_values = result.get("confidence_scores", {}).values()
            confidence = (
                sum(confidence_values) / len(confidence_values)
                if confidence_values else 0.0
            )
            hallucination_rate = 0.0
            reconciliation_recall = None
        elif domain == TaskDomain.FINANCIAL:
            accuracy, f1, reconciliation_recall = _financial_scores(result, gold)
            confidence = _mean(
                pair.get("confidence", 0.0) for pair in result.get("matched_pairs", [])
            )
            hallucination_rate = 0.0
        elif domain == TaskDomain.REPORTING:
            accuracy, f1 = _report_scores(result, gold)
            confidence = _number(result.get("generation_metadata", {}).get("generation_confidence"), 0.97)
            hallucination_rate = _number(
                result.get("hallucination_report", {}).get("overall_hallucination_rate"),
                0.0,
            )
            reconciliation_recall = None
        else:
            accuracy, f1, confidence = 0.0, 0.0, 0.0
            hallucination_rate, reconciliation_recall = 0.0, None

        latency_samples = [step.latency_ms for step in audit_trail]
        total_latency_ms = sum(latency_samples)
        p95_latency_ms = _percentile_95(latency_samples)
        input_tokens = sum(step.input_tokens for step in audit_trail)
        output_tokens = sum(step.output_tokens for step in audit_trail)
        input_rate = _number(self.config.get("input_cost_per_1k_tokens"), 0.0)
        output_rate = _number(self.config.get("output_cost_per_1k_tokens"), 0.0)
        token_cost = (
            input_tokens * input_rate + output_tokens * output_rate
        ) / 1000
        generated_cost = result.get("generation_metadata", {}).get("cost_usd")
        if generated_cost is not None:
            token_cost = _number(generated_cost, token_cost)

        return EvalMetrics(
            accuracy_score=round(accuracy, 4),
            f1_score=round(f1, 4),
            hallucination_rate=round(hallucination_rate, 4),
            p95_latency_ms=p95_latency_ms,
            confidence=round(confidence, 4),
            cost_usd=round(token_cost, 8),
            token_cost_usd=round(token_cost, 8),
            total_latency_ms=total_latency_ms,
            reconciliation_recall=(
                round(reconciliation_recall, 4)
                if reconciliation_recall is not None else None
            ),
        )

    def _result_status(
        self,
        domain: TaskDomain,
        result: dict[str, Any],
        metrics: EvalMetrics,
    ) -> TaskStatus:
        if domain == TaskDomain.EXTRACTION and result.get("is_valid") is False:
            return TaskStatus.PARTIAL
        if metrics.hallucination_rate and metrics.hallucination_rate > 0.03:
            return TaskStatus.PARTIAL
        return TaskStatus.SUCCESS

    def _emit_trace(
        self,
        event_type: EventType,
        task_id: str,
        span_name: str,
        attributes: dict[str, Any],
    ) -> None:
        """Emit an OpenTelemetry-compatible event through the shared Tracer."""
        trace_attributes = {
            "task_id": task_id,
            "agent_name": "Orchestrator",
            **attributes,
        }
        self._tracer.emit(
            TraceEventType(event_type.value),
            task_id,
            span_name,
            attributes=trace_attributes,
            idempotency_key=str(attributes.get("idempotency_key", "")),
        )

    def _handle_dlq(
        self,
        task_input: OrchestratorInput,
        idempotency_key: str,
        failure_reason: str,
        retry_count: int,
        failure_category: str,
        partial_result: Optional[dict[str, Any]] = None,
    ) -> OrchestratorOutput:
        """Persist unrecoverable work to the DLQ and mark its idempotency entry."""
        entry = self._state_store.get(idempotency_key)
        if entry is None:
            _, entry = self._state_store.check_or_lock(
                idempotency_key, task_input.task_id, max_retries=self.MAX_RETRIES + 1
            )
        dlq_entry = self._dlq.enqueue(
            task_id=task_input.task_id,
            idempotency_key=idempotency_key,
            failure_reason=failure_reason,
            failure_category=failure_category,
            original_input=task_input.input,
            retry_count=retry_count,
            partial_result=partial_result,
            agent_used=self.AGENT_REGISTRY.get(TaskDomain(task_input.domain), "Orchestrator"),
            trace_id=self._tracer.trace_id,
        )
        self._state_store.mark_dlq(idempotency_key, failure_reason)
        self._emit_trace(EventType.DLQ, task_input.task_id, "orchestrator.dlq", {
            "idempotency_key": idempotency_key,
            "dlq_entry_id": dlq_entry.dlq_entry_id,
            "failure_reason": failure_reason,
            "failure_category": failure_category,
            "retry_count": retry_count,
        })
        return OrchestratorOutput(
            task_id=task_input.task_id,
            idempotency_key=idempotency_key,
            status=TaskStatus.DLQ,
            agent_used=self.AGENT_REGISTRY.get(TaskDomain(task_input.domain), "Orchestrator"),
            result={
                "error": failure_reason,
                "failure_category": failure_category,
                "retry_count": retry_count,
                "dlq_entry_id": dlq_entry.dlq_entry_id,
                "partial_result": partial_result,
            },
            eval_metrics=_empty_metrics(TaskDomain(task_input.domain)),
        )


def _as_dict(output: Any) -> dict[str, Any]:
    if isinstance(output, dict):
        return output
    if hasattr(output, "to_dict"):
        return output.to_dict()
    return asdict(output)


def _empty_metrics(domain: Optional[TaskDomain] = None) -> EvalMetrics:
    return EvalMetrics(
        accuracy_score=0.0,
        f1_score=0.0,
        hallucination_rate=0.0,
        p95_latency_ms=0,
        confidence=0.0,
        cost_usd=0.0,
        token_cost_usd=0.0,
        total_latency_ms=0,
        reconciliation_recall=0.0 if domain == TaskDomain.FINANCIAL else None,
    )


def _output_from_dict(data: dict[str, Any]) -> OrchestratorOutput:
    metrics_data = data.get("eval_metrics") or {}
    metric_fields = EvalMetrics.__dataclass_fields__
    metrics = EvalMetrics(**{
        key: value for key, value in metrics_data.items() if key in metric_fields
    })
    audit_trail = [
        AuditStep(**{
            key: value for key, value in item.items()
            if key in AuditStep.__dataclass_fields__
        })
        for item in data.get("audit_trail", [])
    ]
    return OrchestratorOutput(
        task_id=data["task_id"],
        idempotency_key=data["idempotency_key"],
        status=TaskStatus(data["status"]),
        agent_used=data["agent_used"],
        result=data.get("result"),
        audit_trail=audit_trail,
        eval_metrics=metrics,
    )


def _flatten_values(value: Any, prefix: str = "") -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            flattened.update(_flatten_values(child, path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            flattened.update(_flatten_values(child, f"{prefix}[{index}]"))
    else:
        flattened[prefix] = value
    return flattened


def _values_equal(actual: Any, expected: Any) -> bool:
    if isinstance(actual, bool) or isinstance(expected, bool):
        return actual is expected
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return math.isclose(actual, expected, rel_tol=1e-4, abs_tol=0.01)
    return actual == expected


def _object_scores(
    actual: Any,
    expected: Any,
    field_weights: Optional[dict[str, Any]] = None,
) -> tuple[float, float]:
    actual_fields = _flatten_values(actual)
    expected_fields = _flatten_values(expected)
    weights = field_weights or {}

    def weight_for(path: str) -> float:
        field_name = path.split(".", 1)[0].split("[", 1)[0]
        weight = weights.get(field_name, 1.0)
        return float(weight) if isinstance(weight, (int, float)) and weight > 0 else 1.0

    true_positives = sum(
        weight_for(path) for path, value in expected_fields.items()
        if path in actual_fields and _values_equal(actual_fields[path], value)
    )
    false_negatives = sum(
        weight_for(path) for path, value in expected_fields.items()
        if path not in actual_fields or not _values_equal(actual_fields[path], value)
    )
    false_positives = sum(
        weight_for(path) for path, value in actual_fields.items()
        if path not in expected_fields or not _values_equal(value, expected_fields[path])
    )
    expected_weight = sum(weight_for(path) for path in expected_fields)
    accuracy = true_positives / expected_weight if expected_weight else 1.0
    return accuracy, _f1(true_positives, false_positives, false_negatives)


def _financial_scores(
    result: dict[str, Any],
    gold: dict[str, Any],
) -> tuple[float, float, float]:
    expected_pairs = {
        (pair.get("po_id"), pair.get("invoice_id"))
        for pair in gold.get("matched_pairs", [])
    }
    actual_pairs = {
        (pair.get("po_id"), pair.get("invoice_id"))
        for pair in result.get("matched_pairs", [])
    }
    matched = len(expected_pairs & actual_pairs)
    recall = matched / len(expected_pairs) if expected_pairs else 1.0
    pair_f1 = _f1(matched, len(actual_pairs) - matched, len(expected_pairs) - matched)

    expected_discrepancies = {
        (item.get("po_id"), item.get("invoice_id"), item.get("discrepancy_type"))
        for item in gold.get("discrepancies", [])
    }
    actual_discrepancies = {
        (item.get("po_id"), item.get("invoice_id"), item.get("discrepancy_type"))
        for item in result.get("discrepancies", [])
    }
    discrepancy_tp = len(expected_discrepancies & actual_discrepancies)
    discrepancy_f1 = _f1(
        discrepancy_tp,
        len(actual_discrepancies) - discrepancy_tp,
        len(expected_discrepancies) - discrepancy_tp,
    )
    accuracy = 0.8 * recall + 0.2 * discrepancy_f1
    return accuracy, pair_f1 if expected_pairs else discrepancy_f1, recall


def _report_scores(result: dict[str, Any], gold: dict[str, Any]) -> tuple[float, float]:
    content = str(result.get("report_content", ""))
    required_sections = set(gold.get("required_sections", []))
    actual_sections = set(result.get("sections_generated", []))
    section_tp = len(required_sections & actual_sections)
    section_f1 = _f1(
        section_tp,
        len(actual_sections - required_sections),
        len(required_sections - actual_sections),
    )
    checks: list[bool] = []
    if required_sections:
        checks.append(required_sections.issubset(actual_sections))
    for kpi in gold.get("required_kpis_present", []):
        aliases = {
            "match_rate_pct": ("match rate",),
            "net_exposure_usd": ("net exposure",),
            "total_discrepancies": ("discrepanc",),
        }.get(kpi, (kpi.replace("_", " "),))
        checks.append(any(alias in content.lower() for alias in aliases))
    required_citations = set(gold.get("required_citations", []))
    actual_citations = {
        citation.get("source_id") for citation in result.get("citations", [])
    }
    if required_citations:
        checks.append(required_citations.issubset(actual_citations))
    maximum_words = gold.get("max_word_count")
    if maximum_words is not None:
        checks.append(_number(result.get("word_count"), 0) <= maximum_words)
    tone = gold.get("tone_check", {})
    if tone.get("must_not_contain"):
        lower_content = content.lower()
        checks.append(not any(phrase.lower() in lower_content for phrase in tone["must_not_contain"]))
    if tone.get("must_contain_numbers"):
        checks.append(all(phrase.lower() in content.lower() for phrase in tone["must_contain_numbers"]))
    accuracy = sum(checks) / len(checks) if checks else 1.0
    return accuracy, section_f1


def _f1(true_positives: float, false_positives: float, false_negatives: float) -> float:
    denominator = 2 * true_positives + false_positives + false_negatives
    return (2 * true_positives / denominator) if denominator else 1.0


def _mean(values: Any) -> float:
    materialized = list(values)
    return sum(materialized) / len(materialized) if materialized else 0.0


def _number(value: Any, default: float) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def _token_count(result: dict[str, Any], field_name: str) -> int:
    value = result.get(field_name)
    if value is None:
        value = result.get("generation_metadata", {}).get(
            "tokens_used" if field_name == "output_tokens" else "input_tokens",
            0,
        )
    return int(value) if isinstance(value, (int, float)) else 0


def _percentile_95(samples: list[int]) -> int:
    if not samples:
        return 0
    ordered = sorted(samples)
    return ordered[max(math.ceil(0.95 * len(ordered)) - 1, 0)]
