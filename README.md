# Enterprise Multi-Agent Business Operations & Autonomous FTE Benchmark

> **Micro1 Eval Hackathon** — Spec-Driven Development (SDD) Project
> Version: `1.0.0` | Status: `Active Development`

---

## 🏆 What Is This?

The **Multi-Agent Eval Suite** is a reproducible benchmark that measures whether autonomous AI agents can perform enterprise business operations at the level of a full-time employee (FTE). It evaluates agents on real-world financial reconciliation, data extraction, and report generation tasks — with strict accuracy, latency, and cost thresholds.

**Headline Metric**: `FTE Equivalence Score` — a composite score (0–1.0) measuring how closely an agent matches human-level performance across all benchmark dimensions.

---

## 📐 Architecture at a Glance

```
run_benchmark.py (CLI)
       │
       ▼
  Orchestrator ──── route ────► DataExtractionAgent
                │                 FinancialReconAgent
                │                 ReportGenAgent
                │
                └─── evaluate ──► EvalHarness ──► Results
```

Full architectural spec: [`docs/spec.md`](docs/spec.md)

---

## 📁 Project Structure

```
multi-agent-eval-suite/
│
├── docs/
│   └── spec.md                      # Master architectural specification
│
├── skills/                          # Reusable agent skill specs (SDD)
│   ├── data_extraction.md           # ETL from PDFs, CSVs, emails
│   ├── financial_reconciliation.md  # PO-to-invoice matching & audit
│   └── report_generation.md         # Narrative synthesis & formatting
│
├── agents/                          # Agent implementations (see agents/)
│   ├── orchestrator.py              # Task validation, routing, retries, metrics, and idempotency
│   ├── data_extraction_agent.py     # ETL specialist
│   ├── financial_recon_agent.py     # Finance specialist
│   └── report_gen_agent.py         # Report synthesis agent
│
├── evals/
│   └── test_cases/                  # JSON test cases (SDD: written before code)
│       ├── TC-ETL-001.json          # Data extraction benchmark case
│       ├── TC-FIN-001.json         # Financial reconciliation benchmark case
│       └── TC-RPT-001.json         # Report generation benchmark case
│
├── README.md                        # This file
├── requirements.txt                 # Python dependencies
└── run_benchmark.py                 # CLI entrypoint
```

---

## ⚡ Quick Start

### Prerequisites

- Python `3.11+`
- `pip` or `uv` package manager
- API keys configured (see `docs/spec.md#credentials`)

### Installation

```bash
# Clone the repository
git clone <repo-url>
cd multi-agent-eval-suite

# Install dependencies
pip install -r requirements.txt

# Verify installation
python run_benchmark.py --help
```

### Run the Full Benchmark

```bash
# Run all benchmark tasks
python run_benchmark.py run

# Run specific domain
python run_benchmark.py run --domain financial

# Run specific task IDs
python run_benchmark.py run --task-ids TASK-FIN-001 TASK-FIN-002

# Run with custom output directory
python run_benchmark.py run --output-dir ./my_results

# Dry run (validate config, do not execute)
python run_benchmark.py run --dry-run
```

### Launch the Dashboard

```bash
streamlit run app.py
```

The dashboard reads prior reports from `evals/results/` and can launch the
full suite or selected test cases. The current agent implementations use
deterministic local pipelines; external model profiles are informational only.

### Email Anomaly Alerts

The orchestrator can send SMTP email alerts when financial discrepancies or
low-confidence extraction fields are found. Configure these environment
variables before starting the benchmark or dashboard:

```text
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_FROM=eval-alerts@example.com
ALERT_EMAIL_TO=finance-team@example.com
SMTP_USERNAME=eval-alerts@example.com
SMTP_PASSWORD=<smtp-app-password>
SMTP_USE_TLS=true
```

`SMTP_USERNAME` and `SMTP_PASSWORD` are optional when the SMTP server does not
require authentication. `SMTP_TIMEOUT_SECONDS` can optionally set the SMTP
connection timeout (default: 10 seconds). Without `SMTP_HOST`, `SMTP_FROM`, or
`ALERT_EMAIL_TO`, email delivery is skipped with a warning. Critical/high
financial discrepancies are sent with `FAIL`; other discrepancies and
low-confidence extractions are sent with `PARTIAL`. SMTP secrets should be
provided through the environment or a secret manager, not committed to source
control.

### View Results

```bash
# Print benchmark summary
python run_benchmark.py report --run-id <run-id>

# List all historical runs
python run_benchmark.py list-runs

# Compare two runs
python run_benchmark.py compare --run-id-a <run-id-a> --run-id-b <run-id-b>
```

---

## 📊 Benchmark Metrics

| Metric                  | Description                                   | Target    |
|-------------------------|-----------------------------------------------|-----------|
| **FTE Equivalence Score** | Composite headline metric (0–1.0)           | ≥ 0.85    |
| Task Accuracy           | Fraction of tasks with correct output         | ≥ 0.90    |
| Field-Level F1          | Precision/recall on extracted fields          | ≥ 0.88    |
| Reconciliation Recall   | Matched pairs / ground truth pairs            | ≥ 0.95    |
| Hallucination Rate      | Ungrounded claims in generated reports        | ≤ 0.03    |
| P95 Latency             | 95th percentile task completion               | ≤ 15,000ms|
| Cost per Task           | Average LLM token cost                        | ≤ $0.10   |
| Idempotency Pass Rate   | Re-runs producing identical output            | = 1.00    |
| DLQ Rate                | Tasks routed to Dead Letter Queue             | ≤ 0.05    |

---

## 🗺️ Spec-Driven Development (SDD) Index

All implementation MUST trace to a Task ID:

| Domain         | Task ID Range       | Skill Spec                                 | Status        |
|----------------|---------------------|--------------------------------------------|---------------|
| Data Extraction| `TASK-ETL-001..008` | [`skills/data_extraction.md`](skills/data_extraction.md) | 📋 Spec Ready |
| Financial Recon| `TASK-FIN-001..010` | [`skills/financial_reconciliation.md`](skills/financial_reconciliation.md) | 📋 Spec Ready |
| Report Gen     | `TASK-RPT-001..006` | [`skills/report_generation.md`](skills/report_generation.md) | 📋 Spec Ready |
| Compliance     | `TASK-CPL-001..005` | _(planned)_                                | 🔲 Not Started |

---

## 🏗️ Development Workflow

This project follows strict **Spec-Driven Development**:

```
1. ✅ Spec written  →  docs/spec.md defines contracts
2. ✅ Skill specs   →  skills/*.md define atomic steps
3. 🔲 Test cases    →  evals/test_cases/*.json (write BEFORE implementation)
4. ✅ Agent impl.   →  agents/*.py implements skill contracts
5. ✅ Eval harness  →  run_benchmark.py scores agent outputs vs gold standards
```

> **Rule**: No implementation (`agents/*.py`) may be written until its test case JSON exists in `evals/test_cases/`.

---

## 🧪 Testing

```bash
# Run all unit tests
pytest tests/ -v

# Run integration tests (requires API keys)
pytest tests/integration/ -v --run-integration

# Run only smoke tests (fast)
pytest tests/ -m smoke

# Generate coverage report
pytest tests/ --cov=agents --cov=skills --cov-report=html
```

---

## 📋 Contributing

1. All changes must reference a Task ID from `docs/spec.md`
2. Write test cases in `evals/test_cases/` before implementing
3. Agent actions must be idempotent (see `docs/spec.md#9`)
4. Emit trace events for all orchestration steps
5. Run the benchmark before submitting a PR

---

## 📄 License

MIT License — See `LICENSE` for details.

---

*Micro1 Eval Hackathon | Multi-Agent Eval Suite v1.0.0*
