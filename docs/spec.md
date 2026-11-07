# Enterprise Multi-Agent Business Operations & Autonomous FTE Benchmark
## Architectural Specification v1.0.0

> **Spec-Driven Development (SDD) Document**
> Task IDs referenced herein are authoritative. All implementation MUST trace back to a Task ID defined in this spec.

---

## Table of Contents

1. [Overview](#overview)
2. [System Architecture](#system-architecture)
3. [Data Flow](#data-flow)
4. [Orchestrator Contract](#orchestrator-contract)
5. [Agent Contracts](#agent-contracts)
6. [Tool Schemas](#tool-schemas)
7. [Skill Registry](#skill-registry)
8. [Evaluation Harness](#evaluation-harness)
9. [Error Handling & Idempotency](#error-handling--idempotency)
10. [Versioning & Observability](#versioning--observability)

---

## 1. Overview

### Purpose

The **Enterprise Multi-Agent Business Operations & Autonomous FTE Benchmark** (`micro1-eval-suite`) is a reproducible, spec-driven evaluation harness that measures the capability of autonomous AI agents to perform real-world enterprise business operations — tasks that would otherwise be executed by a full-time employee (FTE).

### Scope

| Domain                    | Description                                                       | Task IDs         |
|---------------------------|-------------------------------------------------------------------|------------------|
| Financial Reconciliation  | Match invoices, detect discrepancies, generate audit trails       | TASK-FIN-001..010 |
| Data Extraction & ETL     | Extract structured data from unstructured documents               | TASK-ETL-001..008 |
| Report Generation         | Synthesize multi-source data into executive-ready reports         | TASK-RPT-001..006 |
| Compliance Verification   | Cross-reference policy documents against operational logs         | TASK-CPL-001..005 |

### Design Principles

1. **Spec-Driven Development (SDD)**: Every agent behavior, tool call, and evaluation metric must trace to a Task ID.
2. **Library-First**: Agents consume a shared skill library; no business logic duplication across agents.
3. **Idempotency**: All agent actions are idempotent — re-running the same task with the same input produces the same output.
4. **Observability**: Every agent action emits a structured trace event (OpenTelemetry compatible).
5. **Test-First (TDD)**: Evaluation test cases are written before agent implementations.
6. **Dead Letter Queues (DLQ)**: Failed tasks are routed to a DLQ for inspection and replay.

---

## 2. System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        BENCHMARK HARNESS                            │
│  ┌──────────────┐    ┌──────────────────────────────────────────┐   │
│  │  CLI Runner  │───▶│         Task Queue (In-Memory)           │   │
│  │ run_benchmark│    │  [TASK-FIN-001, TASK-ETL-002, ...]       │   │
│  └──────────────┘    └──────────────────┬───────────────────────┘   │
│                                         │                           │
│                              ┌──────────▼──────────┐               │
│                              │    ORCHESTRATOR      │               │
│                              │  (MasterAgent)       │               │
│                              │  - Route tasks       │               │
│                              │  - Manage context    │               │
│                              │  - Aggregate results │               │
│                              └──────┬──────┬──────┬─┘               │
│                                     │      │      │                 │
│              ┌──────────────────────┘      │      └──────────────┐  │
│              ▼                             ▼                     ▼  │
│   ┌─────────────────┐           ┌─────────────────┐  ┌──────────────┐│
│   │  DataExtraction │           │   FinancialRecon │  │  ReportGen  ││
│   │    Agent        │           │     Agent        │  │   Agent     ││
│   └────────┬────────┘           └────────┬─────────┘  └─────┬────────┘│
│            │                             │                   │        │
│            └──────────────┬──────────────┘                   │        │
│                           ▼                                  │        │
│                   ┌───────────────┐                          │        │
│                   │ Skill Library │◀─────────────────────────┘        │
│                   │ (Shared Tools)│                                   │
│                   └───────┬───────┘                                   │
│                           │                                           │
│              ┌────────────▼────────────────────┐                     │
│              │         EVAL HARNESS            │                     │
│              │  - Accuracy Scorer              │                     │
│              │  - Latency Profiler             │                     │
│              │  - Cost Estimator               │                     │
│              │  - Hallucination Detector       │                     │
│              └─────────────────────────────────┘                     │
└─────────────────────────────────────────────────────────────────────┘
```

### Component Registry

| Component             | Type        | Responsibility                                  |
|-----------------------|-------------|------------------------------------------------|
| `Orchestrator`        | Agent       | Task routing, context management, aggregation  |
| `DataExtractionAgent` | Agent       | ETL from PDFs, CSVs, emails, APIs              |
| `FinancialReconAgent` | Agent       | Invoice matching, discrepancy detection        |
| `ReportGenAgent`      | Agent       | Narrative synthesis, chart generation          |
| `SkillLibrary`        | Library     | Shared atomic tools used by all agents         |
| `EvalHarness`         | Framework   | Scoring, benchmarking, reporting               |

---

## 3. Data Flow

### 3.1 Canonical Task Lifecycle

```
INPUT (Task Spec)
  │
  ▼
[1] TASK INTAKE
    - Validate task against spec schema (JSON Schema v7)
    - Assign idempotency_key = SHA256(task_id + input_hash)
    - Enqueue to TaskQueue
  │
  ▼
[2] ORCHESTRATION
    - Orchestrator dequeues task
    - Classify task domain → route to specialist agent
    - Inject shared context (org_id, fiscal_period, policy_version)
  │
  ▼
[3] AGENT EXECUTION
    - Agent loads required skills from SkillLibrary
    - Agent emits START trace event
    - Agent executes skill chain (atomic steps)
    - Each step writes to AuditLog with step_id
    - Agent emits END trace event with result + token_usage
  │
  ▼
[4] RESULT COLLECTION
    - Orchestrator collects AgentResult
    - Validates result schema
    - On failure → route to DLQ with failure_reason
  │
  ▼
[5] EVALUATION
    - EvalHarness compares AgentResult to GoldStandard
    - Computes: accuracy, f1, latency_ms, cost_usd, hallucination_rate
    - Writes EvalReport to evals/results/
  │
  ▼
OUTPUT (EvalReport + Benchmark Score)
```

### 3.2 Event Schema

All inter-component events conform to this envelope:

```json
{
  "schema_version": "1.0",
  "event_id": "<uuid-v4>",
  "idempotency_key": "<sha256>",
  "task_id": "TASK-FIN-001",
  "timestamp_utc": "<ISO8601>",
  "source": "<component_name>",
  "event_type": "<START|STEP|END|ERROR|DLQ>",
  "payload": {},
  "trace_context": {
    "trace_id": "<otel-trace-id>",
    "span_id": "<otel-span-id>"
  }
}
```

---

## 4. Orchestrator Contract

### Task ID: `ORCH-001`

The Orchestrator is the single entry point for all task execution. It MUST:

| Responsibility                      | Spec Rule                                                              |
|-------------------------------------|------------------------------------------------------------------------|
| Task Validation                     | Reject tasks not conforming to `TaskSpec` schema with `400` error      |
| Domain Classification               | Route to correct agent using keyword + embedding classification         |
| Idempotency Enforcement             | Check `idempotency_key` in state store before execution                |
| Context Injection                   | Merge org-level context into every agent invocation                    |
| Result Aggregation                  | Collect partial results from parallel agent calls                      |
| DLQ Routing                         | Route failed tasks to DLQ after `max_retries=3` with exponential backoff|
| Observability                       | Emit OpenTelemetry spans for every orchestration step                  |

### Orchestrator Input Schema

```json
{
  "task_id": "string (required, matches TASK-[A-Z]+-[0-9]+)",
  "domain": "enum[financial, extraction, reporting, compliance]",
  "priority": "enum[critical, high, medium, low]",
  "input": {
    "documents": ["array of document references"],
    "parameters": {}
  },
  "context": {
    "org_id": "string",
    "fiscal_period": "YYYY-QN",
    "policy_version": "semver"
  },
  "constraints": {
    "max_latency_ms": 30000,
    "max_cost_usd": 0.50,
    "require_citation": true
  }
}
```

### Orchestrator Output Schema

```json
{
  "task_id": "string",
  "idempotency_key": "string",
  "status": "enum[success, partial, failed, dlq]",
  "agent_used": "string",
  "result": {},
  "audit_trail": [
    {
      "step_id": "string",
      "skill_used": "string",
      "input_tokens": 0,
      "output_tokens": 0,
      "latency_ms": 0,
      "timestamp_utc": "string"
    }
  ],
  "eval_metrics": {
    "accuracy_score": 0.0,
    "confidence": 0.0,
    "cost_usd": 0.0,
    "total_latency_ms": 0
  }
}
```

---

## 5. Agent Contracts

### 5.1 DataExtractionAgent

**Task IDs**: `TASK-ETL-001` through `TASK-ETL-008`

**Responsibilities**:
- Parse PDFs, CSVs, XLSX, JSON, email bodies, and HTML tables
- Normalize extracted data to canonical schema
- Handle OCR for scanned documents
- Detect and flag low-confidence extractions (confidence < 0.85)

**Input Contract**:
```json
{
  "source_type": "enum[pdf, csv, xlsx, email, html, api]",
  "source_ref": "string (URI or inline content)",
  "target_schema": "object (JSON Schema of expected output)",
  "extraction_hints": ["optional array of field names to prioritize"]
}
```

**Output Contract**:
```json
{
  "extracted_data": {},
  "confidence_scores": {"field_name": 0.0},
  "low_confidence_fields": ["array of field names below threshold"],
  "source_citation": "string",
  "extraction_method": "string"
}
```

**Skills Used**: `data_extraction`, `document_parsing`, `schema_validation`

---

### 5.2 FinancialReconAgent

**Task IDs**: `TASK-FIN-001` through `TASK-FIN-010`

**Responsibilities**:
- Match purchase orders to invoices (fuzzy matching, threshold ≥ 0.92)
- Detect discrepancies (amount, vendor, date, line items)
- Generate audit-ready reconciliation reports
- Flag anomalies for human review
- Support multi-currency reconciliation (ISO 4217)

**Input Contract**:
```json
{
  "purchase_orders": [{"po_id": "string", "amount": 0.0, "currency": "USD", "vendor_id": "string", "line_items": []}],
  "invoices": [{"invoice_id": "string", "amount": 0.0, "currency": "USD", "vendor_id": "string", "line_items": []}],
  "fiscal_period": "YYYY-QN",
  "matching_config": {
    "fuzzy_threshold": 0.92,
    "amount_tolerance_pct": 0.01,
    "date_tolerance_days": 5
  }
}
```

**Output Contract**:
```json
{
  "matched_pairs": [{"po_id": "string", "invoice_id": "string", "confidence": 0.0, "delta_amount": 0.0}],
  "unmatched_pos": ["array of po_ids"],
  "unmatched_invoices": ["array of invoice_ids"],
  "discrepancies": [{"type": "string", "severity": "enum[low, medium, high, critical]", "details": {}}],
  "reconciliation_summary": {"total_matched": 0, "total_discrepancies": 0, "net_exposure_usd": 0.0},
  "requires_human_review": false
}
```

**Skills Used**: `financial_reconciliation`, `data_extraction`, `anomaly_detection`

---

### 5.3 ReportGenAgent

**Task IDs**: `TASK-RPT-001` through `TASK-RPT-006`

**Responsibilities**:
- Synthesize structured data into executive narrative reports
- Generate charts and visualizations from tabular data
- Support output formats: Markdown, PDF, HTML, DOCX
- Cite all data sources in generated content
- Enforce brand voice and tone guidelines

**Input Contract**:
```json
{
  "report_type": "enum[executive_summary, audit_report, operational_dashboard, financial_statement]",
  "data_sources": [{"source_id": "string", "data": {}, "source_citation": "string"}],
  "output_format": "enum[markdown, pdf, html, docx]",
  "tone": "enum[formal, executive, technical]",
  "sections": ["array of required section names"],
  "visualizations": [{"chart_type": "string", "data_ref": "string"}]
}
```

**Output Contract**:
```json
{
  "report_content": "string (formatted content)",
  "citations": [{"id": "string", "source": "string", "accessed_at": "string"}],
  "word_count": 0,
  "sections_generated": ["array of section names"],
  "visualization_artifacts": [{"chart_id": "string", "uri": "string"}],
  "generation_metadata": {"model": "string", "tokens_used": 0, "generation_time_ms": 0}
}
```

**Skills Used**: `report_generation`, `data_visualization`, `citation_manager`

---

## 6. Tool Schemas

All tools follow OpenAI-compatible function calling schema.

### Tool: `extract_structured_data`
```json
{
  "name": "extract_structured_data",
  "description": "Extract structured fields from unstructured documents. Supports PDF, CSV, HTML, email.",
  "parameters": {
    "type": "object",
    "properties": {
      "document_content": {"type": "string", "description": "Raw document content or base64-encoded binary"},
      "document_type": {"type": "string", "enum": ["pdf", "csv", "html", "email", "text"]},
      "target_fields": {"type": "array", "items": {"type": "string"}, "description": "Field names to extract"},
      "confidence_threshold": {"type": "number", "default": 0.85}
    },
    "required": ["document_content", "document_type"]
  }
}
```

### Tool: `reconcile_financial_records`
```json
{
  "name": "reconcile_financial_records",
  "description": "Match and reconcile purchase orders against invoices with configurable thresholds.",
  "parameters": {
    "type": "object",
    "properties": {
      "purchase_orders": {"type": "array", "items": {"$ref": "#/definitions/PurchaseOrder"}},
      "invoices": {"type": "array", "items": {"$ref": "#/definitions/Invoice"}},
      "fuzzy_threshold": {"type": "number", "default": 0.92, "minimum": 0.0, "maximum": 1.0},
      "amount_tolerance_pct": {"type": "number", "default": 0.01}
    },
    "required": ["purchase_orders", "invoices"]
  }
}
```

### Tool: `generate_report`
```json
{
  "name": "generate_report",
  "description": "Generate a formatted business report from structured data sources.",
  "parameters": {
    "type": "object",
    "properties": {
      "report_type": {"type": "string", "enum": ["executive_summary", "audit_report", "operational_dashboard"]},
      "data_sources": {"type": "array"},
      "output_format": {"type": "string", "enum": ["markdown", "pdf", "html", "docx"]},
      "include_citations": {"type": "boolean", "default": true}
    },
    "required": ["report_type", "data_sources", "output_format"]
  }
}
```

### Tool: `emit_trace_event`
```json
{
  "name": "emit_trace_event",
  "description": "Emit an OpenTelemetry-compatible trace event for observability.",
  "parameters": {
    "type": "object",
    "properties": {
      "event_type": {"type": "string", "enum": ["START", "STEP", "END", "ERROR", "DLQ"]},
      "task_id": {"type": "string"},
      "span_name": {"type": "string"},
      "attributes": {"type": "object"}
    },
    "required": ["event_type", "task_id", "span_name"]
  }
}
```

---

## 7. Skill Registry

| Skill ID                       | File                                    | Used By Agents                              | Task IDs              |
|--------------------------------|-----------------------------------------|---------------------------------------------|-----------------------|
| `data_extraction`              | `skills/data_extraction.md`             | DataExtractionAgent, FinancialReconAgent    | TASK-ETL-*            |
| `financial_reconciliation`     | `skills/financial_reconciliation.md`    | FinancialReconAgent                         | TASK-FIN-*            |
| `report_generation`            | `skills/report_generation.md`           | ReportGenAgent                              | TASK-RPT-*            |
| `anomaly_detection`            | _(planned)_                             | FinancialReconAgent, ComplianceAgent        | TASK-FIN-*, TASK-CPL-*|
| `citation_manager`             | _(planned)_                             | ReportGenAgent                              | TASK-RPT-*            |

---

## 8. Evaluation Harness

### 8.1 Metrics Specification

| Metric                    | Formula / Method                                           | Target Threshold |
|---------------------------|------------------------------------------------------------|-----------------|
| **Task Accuracy**         | `correct_outputs / total_tasks`                           | ≥ 0.90          |
| **Field-Level F1**        | Harmonic mean of precision/recall on extracted fields     | ≥ 0.88          |
| **Reconciliation Recall** | `matched_pairs / total_ground_truth_pairs`                | ≥ 0.95          |
| **Hallucination Rate**    | Fraction of output claims not grounded in source          | ≤ 0.03          |
| **P95 Latency**           | 95th percentile task completion time                      | ≤ 15,000 ms     |
| **Cost per Task**         | Total LLM token cost (input + output) per task execution  | ≤ $0.10         |
| **DLQ Rate**              | `dlq_tasks / total_tasks`                                 | ≤ 0.05          |
| **Idempotency Pass Rate** | Re-runs producing identical output                        | = 1.00          |

### 8.2 Test Case Schema

Located in `evals/test_cases/`. Each test case file is a JSON object:

```json
{
  "test_case_id": "TC-FIN-001",
  "task_id": "TASK-FIN-001",
  "description": "Match 50 POs to invoices with 2 known discrepancies",
  "domain": "financial",
  "input": {},
  "gold_standard": {
    "matched_pairs": [],
    "discrepancies": [],
    "reconciliation_summary": {}
  },
  "evaluation_config": {
    "metrics": ["accuracy", "f1", "latency_ms", "cost_usd"],
    "thresholds": {"accuracy": 0.90, "f1": 0.88}
  },
  "tags": ["regression", "smoke", "financial"]
}
```

### 8.3 EvalReport Schema

Output written to `evals/results/<run_id>.json`:

```json
{
  "run_id": "<uuid>",
  "run_timestamp": "<ISO8601>",
  "benchmark_version": "1.0.0",
  "total_tasks": 0,
  "passed": 0,
  "failed": 0,
  "dlq_count": 0,
  "aggregate_metrics": {
    "task_accuracy": 0.0,
    "avg_f1": 0.0,
    "hallucination_rate": 0.0,
    "p95_latency_ms": 0,
    "avg_cost_usd": 0.0,
    "idempotency_pass_rate": 1.0
  },
  "per_task_results": [],
  "fte_equivalence_score": 0.0,
  "summary": "string"
}
```

### 8.4 FTE Equivalence Score

The headline benchmark metric. Computed as:

```
FTE_Score = (
  0.40 * task_accuracy +
  0.25 * (1 - hallucination_rate) +
  0.20 * reconciliation_recall +
  0.10 * cost_efficiency_index +
  0.05 * latency_index
)
```

Where:
- `cost_efficiency_index = max(0, 1 - avg_cost_usd / cost_budget_usd)`
- `latency_index = max(0, 1 - p95_latency_ms / latency_budget_ms)`

---

## 9. Error Handling & Idempotency

### Idempotency Protocol

```
1. Before execution: check StateStore for idempotency_key
2. If key exists AND status=success: return cached result
3. If key exists AND status=in_progress: wait with timeout
4. If key exists AND status=failed: check retry_count
   - retry_count < max_retries: re-execute
   - retry_count >= max_retries: route to DLQ
5. If key absent: proceed with execution, write key with status=in_progress
```

### Dead Letter Queue (DLQ) Entry Schema

```json
{
  "dlq_entry_id": "<uuid>",
  "original_task_id": "string",
  "idempotency_key": "string",
  "failure_reason": "string",
  "failure_category": "enum[timeout, schema_error, model_error, tool_error, budget_exceeded]",
  "retry_count": 0,
  "last_attempt_timestamp": "<ISO8601>",
  "original_input": {},
  "partial_result": {}
}
```

### Retry Policy

| Failure Category    | Max Retries | Backoff Strategy       |
|---------------------|-------------|------------------------|
| `timeout`           | 3           | Exponential (1s, 2s, 4s)|
| `schema_error`      | 1           | Immediate              |
| `model_error`       | 3           | Exponential (2s, 4s, 8s)|
| `tool_error`        | 2           | Linear (3s, 3s)        |
| `budget_exceeded`   | 0           | Route to DLQ immediately|

---

## 10. Versioning & Observability

### Versioning

- **Spec Version**: Follows SemVer (`MAJOR.MINOR.PATCH`)
- **Agent Version**: Each agent declares `__version__` matching spec version at release
- **Skill Version**: Each skill file contains `version:` frontmatter field
- **Breaking Changes**: Any change to Input/Output contracts requires MAJOR version bump

### Observability Stack

| Layer         | Technology              | Config File               |
|---------------|-------------------------|---------------------------|
| Tracing       | OpenTelemetry SDK       | `otel_config.yaml`        |
| Logging       | Structured JSON logging | `logging_config.json`     |
| Metrics       | Prometheus-compatible   | `metrics_config.yaml`     |
| Dashboards    | Grafana (optional)      | `grafana/dashboards/`     |

### Trace Attribute Standards

Every span MUST include:
- `task_id`: string
- `agent_name`: string
- `skill_name`: string (if applicable)
- `idempotency_key`: string
- `model_name`: string
- `input_tokens`: integer
- `output_tokens`: integer
- `cost_usd`: float

---

*Spec Owner: Principal AI Systems Architect*
*Last Updated: 2026-11-05*
*Status: APPROVED — Implementation may proceed*
