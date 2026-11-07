"""Streamlit dashboard for the multi-agent operations benchmark."""

from __future__ import annotations

import hashlib
import html
import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.express as px
import streamlit as st

from agents.orchestrator import Orchestrator, OrchestratorInput, TaskDomain
from run_benchmark import RESULTS_DIR, TEST_CASES_DIR, BenchmarkRunner, RunConfig


ROOT = Path(__file__).parent.resolve()
UPLOADS_DIR = ROOT / "evals" / "fixtures" / "uploads"
ACCENT = "#00F2FE"
VIOLET = "#7F00FF"

st.set_page_config(
    page_title="Micro1 Operations Benchmark",
    page_icon="✦",
    layout="wide",
    initial_sidebar_state="expanded",
)


def _apply_theme() -> None:
    st.markdown(
        """
        <style>
        :root {
            color-scheme: dark;
            --bg: #0E1117;
            --panel: #151A23;
            --panel-soft: #111720;
            --line: rgba(148, 163, 184, .15);
            --muted: #91A0B5;
            --cyan: #00F2FE;
            --violet: #7F00FF;
        }
        html, body, [data-testid="stAppViewContainer"], [data-testid="stHeader"] {
            background: #0E1117;
        }
        [data-testid="stAppViewContainer"] {
            background:
                radial-gradient(ellipse at 86% 0%, rgba(127, 0, 255, .13), transparent 34%),
                radial-gradient(ellipse at 4% 12%, rgba(0, 242, 254, .09), transparent 30%),
                #0E1117;
            color: #E7EDF6;
        }
        [data-testid="stHeader"] { background: transparent; }
        [data-testid="stSidebar"] {
            background: linear-gradient(180deg, #121722, #0E1117 75%);
            border-right: 1px solid rgba(148, 163, 184, .12);
        }
        [data-testid="stSidebar"] > div:first-child { padding-top: 1.1rem; }
        .block-container { max-width: 1500px; padding-top: 2rem; padding-bottom: 3rem; }
        h1, h2, h3 { letter-spacing: -.035em; color: #F4F7FC; }
        p, label, [data-testid="stCaptionContainer"] { color: #A7B3C5; }
        .hero {
            position: relative; overflow: hidden; padding: 2rem 2.2rem;
            border: 1px solid rgba(0, 242, 254, .25); border-radius: 24px;
            background: linear-gradient(115deg, rgba(20, 29, 42, .97), rgba(20, 17, 38, .92));
            box-shadow: 0 0 42px rgba(0, 242, 254, .07), inset 0 1px rgba(255,255,255,.04);
            margin-bottom: 1.35rem;
        }
        .hero:after {
            content: ""; position: absolute; width: 270px; height: 270px; right: -80px; top: -150px;
            border-radius: 50%; background: rgba(127,0,255,.23); filter: blur(55px);
        }
        .eyebrow {
            text-transform: uppercase; letter-spacing: .19em; font-size: .72rem;
            font-weight: 700; color: #70F7FF; margin-bottom: .65rem;
        }
        .hero-title {
            position: relative; z-index: 1; font-size: clamp(1.7rem, 3.2vw, 2.8rem);
            line-height: 1.12; font-weight: 800; margin: 0;
            background: linear-gradient(90deg, #F4FBFF 12%, #00F2FE 62%, #B17BFF 100%);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent;
        }
        .hero-sub { position: relative; z-index: 1; margin: .85rem 0 1.25rem; max-width: 780px; color: #A8B5C9; }
        .badge-row { display: flex; gap: .55rem; flex-wrap: wrap; position: relative; z-index: 1; }
        .badge {
            display: inline-flex; gap: .4rem; align-items: center; padding: .36rem .7rem;
            border-radius: 999px; font-size: .75rem; font-weight: 700;
            border: 1px solid rgba(148,163,184,.2); background: rgba(255,255,255,.035); color: #D7E1EF;
        }
        .badge-live { color: #6FFFC2; border-color: rgba(56,240,161,.3); background: rgba(56,240,161,.08); }
        .badge-cyan { color: #6BF8FF; border-color: rgba(0,242,254,.3); background: rgba(0,242,254,.07); }
        .section-head {
            display: flex; align-items: center; gap: .65rem; margin: 1.7rem 0 .85rem;
            color: #F0F5FC; font-size: 1.05rem; font-weight: 750;
        }
        .section-head:before {
            content: ""; width: 4px; height: 19px; border-radius: 5px;
            background: linear-gradient(180deg, #00F2FE, #7F00FF);
            box-shadow: 0 0 12px rgba(0,242,254,.45);
        }
        .metric-card {
            min-height: 132px; padding: 1.05rem 1.1rem; border-radius: 17px;
            border: 1px solid rgba(148,163,184,.16);
            background: linear-gradient(145deg, rgba(22,29,41,.96), rgba(16,21,31,.96));
            box-shadow: 0 8px 26px rgba(0,0,0,.19), inset 0 1px rgba(255,255,255,.035);
            transition: transform .18s ease, border-color .18s ease, box-shadow .18s ease;
        }
        .metric-card:hover {
            transform: translateY(-3px); border-color: rgba(0,242,254,.42);
            box-shadow: 0 10px 32px rgba(0,242,254,.09), 0 0 18px rgba(127,0,255,.08);
        }
        .metric-label { color: #9EACC0; font-size: .8rem; font-weight: 650; letter-spacing: .035em; }
        .metric-value {
            margin-top: .65rem; font-size: clamp(1.5rem, 2.4vw, 2.1rem);
            line-height: 1; color: #F5FAFF; font-weight: 800; letter-spacing: -.045em;
        }
        .metric-foot { color: #6F8097; font-size: .72rem; margin-top: .6rem; }
        .status {
            display: inline-block; border-radius: 999px; padding: .25rem .62rem;
            text-transform: uppercase; font-size: .68rem; letter-spacing: .06em; font-weight: 800;
        }
        .status-pass { color: #70F7B1; background: rgba(56,240,161,.1); border: 1px solid rgba(56,240,161,.25); }
        .status-fail { color: #FF7E8C; background: rgba(255,78,100,.1); border: 1px solid rgba(255,78,100,.25); }
        .status-partial { color: #FFD36F; background: rgba(255,190,59,.1); border: 1px solid rgba(255,190,59,.25); }
        .status-neutral { color: #AFC0D6; background: rgba(148,163,184,.1); border: 1px solid rgba(148,163,184,.2); }
        .panel {
            padding: 1.1rem 1.2rem; border-radius: 18px; border: 1px solid rgba(148,163,184,.14);
            background: rgba(18,24,34,.82); box-shadow: 0 8px 28px rgba(0,0,0,.16);
        }
        .task-card {
            padding: 1rem; border-radius: 15px; border: 1px solid rgba(148,163,184,.15);
            background: linear-gradient(145deg, rgba(22,29,41,.96), rgba(16,21,31,.96));
            min-height: 150px; margin-bottom: .75rem;
        }
        .sidebar-info-box, .sidebar-success-box {
            padding: .8rem .9rem; border-radius: 12px; font-size: .88rem;
            line-height: 1.5; font-weight: 600; margin: .5rem 0 1rem;
        }
        .sidebar-info-box {
            color: #FFFFFF !important;
            background: linear-gradient(135deg, #162C46, #1E3A5F);
            border: 1px solid rgba(0, 242, 254, .45);
        }
        .sidebar-info-box * { color: #FFFFFF !important; }
        .sidebar-success-box {
            color: #FFFFFF !important;
            background: linear-gradient(135deg, #134E35, #1B6947);
            border: 1px solid rgba(56, 240, 161, .5);
        }
        .sidebar-success-box * { color: #FFFFFF !important; }
        .sidebar-success-box .saved-filename {
            color: #FFE600 !important; font-weight: 700; overflow-wrap: anywhere;
        }
        .task-name { color: #EEF5FF; font-weight: 750; margin: .55rem 0 .2rem; }
        .task-domain { color: #8393AA; font-size: .73rem; text-transform: uppercase; letter-spacing: .09em; }
        .task-stats { color: #9EACC0; font-size: .78rem; margin-top: .75rem; }
        div.stButton > button[kind="primary"] {
            border: 0; border-radius: 12px; color: #000000 !important; font-weight: 900;
            background: linear-gradient(100deg, #00F2FE, #65C8FF 48%, #A45BFF);
            box-shadow: 0 5px 20px rgba(0,242,254,.2); transition: transform .18s ease, box-shadow .18s ease;
        }
        div.stButton > button[kind="primary"] * {
            color: #000000 !important;
        }
        div.stButton > button[kind="primary"]:hover {
            transform: translateY(-2px); box-shadow: 0 8px 28px rgba(0,242,254,.32);
        }
        [data-testid="stMetric"] {
            padding: .8rem; border: 1px solid var(--line); border-radius: 14px;
            background: rgba(255,255,255,.025);
        }
        [data-testid="stDataFrame"] { border: 1px solid var(--line); border-radius: 14px; overflow: hidden; }
        hr { border-color: rgba(148,163,184,.14); }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _read_report(path: Path) -> dict[str, Any] | None:
    try:
        with path.open(encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError) as exc:
        st.warning(f"Could not read `{path.name}`: {exc}")
        return None


def _load_reports() -> list[dict[str, Any]]:
    if not RESULTS_DIR.exists():
        return []
    reports = []
    for path in RESULTS_DIR.glob("*.json"):
        report = _read_report(path)
        if report:
            report["_path"] = str(path)
            reports.append(report)
    return sorted(reports, key=lambda report: report.get("run_timestamp", ""), reverse=True)


def _fmt_number(value: Any, suffix: str = "", decimals: int = 2) -> str:
    if not isinstance(value, (int, float)):
        return "—"
    return f"{value:,.{decimals}f}{suffix}"


def _status(value: str) -> tuple[str, str]:
    normalized = str(value).lower()
    if normalized == "success":
        return "PASS", "status-pass"
    if normalized == "partial":
        return "PARTIAL", "status-partial"
    if normalized in ("failed", "dlq"):
        return "FAIL", "status-fail"
    return normalized.upper() or "UNKNOWN", "status-neutral"


def _metric_card(label: str, value: str, note: str, accent: str) -> None:
    st.markdown(
        f"""
        <div class="metric-card" style="border-top: 2px solid {accent};">
            <div class="metric-label">{label}</div>
            <div class="metric-value">{value}</div>
            <div class="metric-foot">{note}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _load_test_case_ids() -> list[str]:
    ids = []
    for path in sorted(TEST_CASES_DIR.glob("*.json")):
        try:
            with path.open(encoding="utf-8") as file:
                case = json.load(file)
            case_id = case.get("task_id")
            if case_id:
                ids.append(case_id)
        except (OSError, json.JSONDecodeError) as exc:
            st.sidebar.warning(f"Skipped {path.name}: {exc}")
    return ids


def _save_upload(uploaded_file: Any) -> Path:
    data = uploaded_file.getvalue()
    if not data:
        raise ValueError("The uploaded file is empty.")
    if len(data) > 50 * 1024 * 1024:
        raise ValueError("Uploads must be 50 MB or smaller.")

    extension = Path(uploaded_file.name).suffix.lower()
    if extension not in {".pdf", ".csv", ".xlsx", ".txt"}:
        raise ValueError("Unsupported file type. Upload a PDF, CSV, XLSX, or TXT file.")

    digest = hashlib.sha256(data).hexdigest()
    previous_upload = st.session_state.get("saved_upload")
    if previous_upload and previous_upload.get("sha256") == digest:
        existing_path = Path(previous_upload["path"])
        if existing_path.is_file():
            return existing_path

    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    safe_stem = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(uploaded_file.name).stem).strip("-")
    safe_stem = safe_stem[:48] or "test-document"
    saved_path = UPLOADS_DIR / f"{safe_stem}-{uuid.uuid4().hex[:12]}{extension}"
    saved_path.write_bytes(data)
    st.session_state["saved_upload"] = {
        "path": str(saved_path),
        "name": Path(uploaded_file.name).name,
        "sha256": digest,
        "size_bytes": len(data),
    }
    return saved_path


def _upload_text(saved_path: Path) -> tuple[str, str]:
    if saved_path.suffix == ".pdf":
        from PyPDF2 import PdfReader

        pages = PdfReader(str(saved_path)).pages
        content = "\n\n".join(page.extract_text() or "" for page in pages)
        if not content.strip():
            raise ValueError("No selectable text was found in the PDF.")
        return content, "text"
    if saved_path.suffix == ".xlsx":
        sheets = pd.read_excel(saved_path, sheet_name=None, dtype=str)
        if not sheets:
            raise ValueError("The XLSX workbook contains no readable sheets.")
        content = "\n\n".join(
            f"Sheet: {sheet_name}\n{frame.fillna('').to_csv(index=False)}"
            for sheet_name, frame in sheets.items()
        )
        return content, "text"
    if saved_path.suffix == ".csv":
        return saved_path.read_text(encoding="utf-8-sig", errors="replace"), "csv"
    return saved_path.read_text(encoding="utf-8-sig", errors="replace"), "text"


def _run_uploaded_file(saved_path: Path) -> dict[str, Any]:
    source_text, source_type = _upload_text(saved_path)
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    task_id = f"TASK-ETL-{timestamp}"
    orchestrator = Orchestrator()
    output = orchestrator.execute(OrchestratorInput(
        task_id=task_id,
        domain=TaskDomain.EXTRACTION,
        priority="medium",
        input={
            "source_ref": source_text,
            "source_type": source_type,
            "extraction_hints": ["invoice_number", "invoice_date", "vendor_name", "total_amount"],
        },
        context={"uploaded_filename": saved_path.name},
    ))
    serialized = output.to_dict()
    extracted = serialized.get("result") or {}
    extracted["source_citation"] = f"Uploaded file: {saved_path.name}"
    metrics = serialized["eval_metrics"]
    metrics["accuracy_score"] = None
    metrics["f1_score"] = None
    metrics["hallucination_rate"] = None
    metrics["confidence"] = extracted.get("overall_confidence")
    task_result = {
        "task_id": task_id,
        "domain": TaskDomain.EXTRACTION.value,
        "status": "partial",
        "agent_used": serialized["agent_used"],
        "idempotency_key": serialized["idempotency_key"],
        "result": extracted,
        "eval_metrics": metrics,
        "uploaded_filename": saved_path.name,
    }
    run_id = str(uuid.uuid4())
    aggregate_metrics = {
        "task_accuracy": None,
        "avg_f1": None,
        "hallucination_rate": None,
        "p95_latency_ms": metrics.get("p95_latency_ms"),
        "avg_cost_usd": metrics.get("token_cost_usd"),
        "idempotency_pass_rate": 1.0,
        "average_confidence": metrics.get("confidence"),
    }
    report = {
        "run_id": run_id,
        "run_timestamp": datetime.now().astimezone().isoformat(),
        "benchmark_version": "1.0.0",
        "run_type": "uploaded_file",
        "uploaded_filename": saved_path.name,
        "total_tasks": 1,
        "passed": 0,
        "partial": 1,
        "failed": 0,
        "dlq_count": 1 if serialized["status"] == "dlq" else 0,
        "aggregate_metrics": aggregate_metrics,
        "fte_equivalence_score": None,
        "per_task_results": [task_result],
        "summary": (
            "Uploaded document processed. Accuracy and F1 are not scored because "
            "no gold-standard output was provided."
        ),
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = RESULTS_DIR / f"{run_id}.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    st.session_state["latest_run_id"] = run_id
    return report


def _run_benchmark(selected_cases: list[str], model_profile: str) -> dict[str, Any] | None:
    try:
        config = RunConfig(
            task_ids=selected_cases or None,
            output_dir=RESULTS_DIR,
            verbose=False,
        )
        runner = BenchmarkRunner(config)
        report_path = runner.run()
        report = _read_report(report_path)
        if report is not None:
            st.session_state["latest_run_id"] = report.get("run_id")
            st.session_state["selected_model_profile"] = model_profile
        return report
    except Exception as exc:
        st.error(f"Benchmark run failed: {exc}")
        return None


def _task_frame(report: dict[str, Any]) -> pd.DataFrame:
    rows = []
    for task in report.get("per_task_results", []):
        metrics = task.get("eval_metrics") or {}
        status_text, _ = _status(task.get("status", "unknown"))
        rows.append({
            "Task": task.get("task_id", "Unknown"),
            "Domain": str(task.get("domain", "—")).title(),
            "Status": status_text,
            "Accuracy": metrics.get("accuracy_score"),
            "F1": metrics.get("f1_score"),
            "Latency (ms)": metrics.get("p95_latency_ms", metrics.get("latency_ms")),
            "Cost (USD)": metrics.get("token_cost_usd", metrics.get("cost_usd")),
        })
    return pd.DataFrame(rows)


_apply_theme()

with st.sidebar:
    st.markdown("### ✦ Control Center")
    st.caption("Configure and launch an evaluation run.")
    model_profile = st.selectbox(
        "Model profile",
        [
            "Deterministic local agents",
            "Claude 3.5 Sonnet (not connected)",
            "GPT-4o (not connected)",
        ],
        help="The current benchmark agents run locally and deterministically. External model profiles are labels only until a provider is integrated.",
    )
    st.markdown(
        '<div class="sidebar-info-box" role="note">'
        "Current agents use deterministic local pipelines. External model selection does not change execution."
        "</div>",
        unsafe_allow_html=True,
    )
    case_ids = _load_test_case_ids()
    selected_cases = st.multiselect(
        "Test cases",
        options=case_ids,
        default=case_ids,
        help="Leave all selected to run the full benchmark suite.",
    )
    run_clicked = st.button(
        "▶  Run Full Benchmark" if len(selected_cases) == len(case_ids) else "▶  Run Selected Cases",
        type="primary",
        width="stretch",
        disabled=not bool(selected_cases),
    )
    uploaded_file = st.file_uploader(
        "Upload Test Document / Invoice",
        type=["pdf", "csv", "xlsx", "txt"],
        key="benchmark_upload",
    )
    saved_upload_path = None
    if uploaded_file is not None:
        try:
            saved_upload_path = _save_upload(uploaded_file)
            st.markdown(
                '<div class="sidebar-success-box" role="status">'
                "<strong>Upload saved securely</strong><br>"
                f'<span class="saved-filename">{html.escape(saved_upload_path.name)}</span>'
                "</div>",
                unsafe_allow_html=True,
            )
        except (OSError, ValueError) as exc:
            st.error(f"Could not save uploaded file: {exc}")
    upload_run_clicked = st.button(
        "▶  Run Evaluation on Uploaded File",
        type="primary",
        width="stretch",
        disabled=saved_upload_path is None,
        key="run_uploaded_file",
    )
    st.markdown("---")
    st.caption(f"Workspace · `{ROOT.name}`")
    st.caption("Results are saved to `evals/results/`.")


st.markdown(
    """
    <section class="hero">
        <div class="eyebrow">AUTONOMOUS OPERATIONS EVALUATION CONSOLE</div>
        <h1 class="hero-title">Multi-Agent Eval Suite</h1>
        <p class="hero-sub">
            Observe agent quality, throughput, and FTE-equivalent performance across
            extraction, financial reconciliation, and report-generation workflows.
        </p>
        <div class="badge-row">
            <span class="badge badge-live">● SYSTEM READY</span>
            <span class="badge badge-cyan">3 SPECIALIST AGENTS</span>
            <span class="badge">BENCHMARK v1.0</span>
        </div>
    </section>
    """,
    unsafe_allow_html=True,
)

if run_clicked:
    with st.spinner("Executing selected agents and collecting evaluation metrics…"):
        _run_benchmark(selected_cases, model_profile)
if upload_run_clicked and saved_upload_path is not None:
    try:
        with st.spinner("Extracting and evaluating the uploaded document…"):
            _run_uploaded_file(saved_upload_path)
    except Exception as exc:
        st.error(f"Uploaded-file evaluation failed: {exc}")

reports = _load_reports()
if reports:
    report_ids = [str(report.get("run_id", Path(report["_path"]).stem)) for report in reports]
    latest_id = st.session_state.get("latest_run_id")
    default_index = report_ids.index(latest_id) if latest_id in report_ids else 0
    selected_run_id = st.selectbox(
        "Displayed evaluation run",
        options=report_ids,
        index=default_index,
        format_func=lambda run_id: f"{run_id[:8]} · {next((r.get('run_timestamp', 'timestamp unavailable')[:19] for r in reports if r.get('run_id') == run_id), '')}",
    )
    active_report = next(
        report for report in reports
        if str(report.get("run_id", Path(report["_path"]).stem)) == selected_run_id
    )
else:
    active_report = None

if active_report is None:
    st.markdown(
        """
        <div class="panel">
            <div class="section-head">Awaiting first benchmark run</div>
            <p>Select test cases in the control center and launch a run. Existing
            reports in <code>evals/results/</code> will appear here automatically.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.stop()

metrics = active_report.get("aggregate_metrics") or {}
tasks = active_report.get("per_task_results") or []
if active_report.get("failed", 0) or active_report.get("dlq_count", 0):
    run_status, run_status_css = "FAIL", "status-fail"
elif active_report.get("partial", 0) or any(task.get("status") == "partial" for task in tasks):
    run_status, run_status_css = "PARTIAL", "status-partial"
else:
    run_status, run_status_css = "PASS", "status-pass"
timestamp = str(active_report.get("run_timestamp", "Unknown timestamp"))
try:
    timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone().strftime("%b %d, %Y · %H:%M:%S")
except ValueError:
    pass

st.markdown(
    f'<div style="display:flex;justify-content:space-between;align-items:center;margin:.9rem 0 .2rem;">'
    f'<div class="section-head" style="margin:0;">Run Overview</div>'
    f'<span class="status {run_status_css}">{run_status} · {active_report.get("total_tasks", 0)} TASKS</span></div>',
    unsafe_allow_html=True,
)
st.caption(f"Run `{active_report.get('run_id', 'unknown')}` · {timestamp} · {active_report.get('summary', '')}")

kpi_cols = st.columns(5, gap="medium")
is_uploaded_run = active_report.get("run_type") == "uploaded_file"
if is_uploaded_run:
    kpis = [
        ("FTE Equivalence", "—", "Requires a gold-standard benchmark", "#00F2FE"),
        (
            "Extraction Confidence",
            _fmt_number(metrics.get("average_confidence") * 100, "%", 1)
            if isinstance(metrics.get("average_confidence"), (int, float)) else "—",
            "Agent confidence; not an accuracy score",
            "#65D9FF",
        ),
        ("Fields Extracted", str(len(tasks[0].get("result", {}).get("extracted_data", {}))) if tasks else "0", "Structured fields found", "#8F75FF"),
        ("Latency", _fmt_number(metrics.get("p95_latency_ms"), " ms", 0), "Document processing time", "#B06BFF"),
        ("Token Cost", _fmt_number(metrics.get("avg_cost_usd"), " USD", 4), "Estimated token cost", "#00D6B2"),
    ]
else:
    kpis = [
        ("FTE Equivalence", _fmt_number(active_report.get("fte_equivalence_score"), decimals=4), "Composite benchmark score", "#00F2FE"),
        (
            "Task Accuracy",
            _fmt_number(metrics.get("task_accuracy") * 100, "%", 1)
            if isinstance(metrics.get("task_accuracy"), (int, float)) else "—",
            "Gold-standard correctness",
            "#65D9FF",
        ),
        ("F1 Score", _fmt_number(metrics.get("avg_f1"), decimals=3), "Field / task-level F1", "#8F75FF"),
        ("P95 Latency", _fmt_number(metrics.get("p95_latency_ms"), " ms", 0), "95th percentile completion", "#B06BFF"),
        ("Token Cost", _fmt_number(metrics.get("avg_cost_usd"), " USD", 4), "Average per task", "#00D6B2"),
    ]
for column, (label, value, note, accent) in zip(kpi_cols, kpis):
    with column:
        _metric_card(label, value, note, accent)

st.markdown('<div class="section-head">Execution Analytics</div>', unsafe_allow_html=True)
task_df = _task_frame(active_report)
chart_col, latency_col = st.columns([1.45, 1], gap="medium")
plot_layout = {
    "paper_bgcolor": "rgba(0,0,0,0)",
    "plot_bgcolor": "rgba(0,0,0,0)",
    "font": {"color": "#B7C4D7", "family": "Inter, sans-serif"},
    "margin": {"l": 8, "r": 8, "t": 28, "b": 8},
}
with chart_col:
    st.markdown('<div class="panel">', unsafe_allow_html=True)
    st.markdown("**Task Quality Scores**")
    if not task_df.empty:
        quality = task_df.melt(
            id_vars=["Task", "Domain", "Status"],
            value_vars=["Accuracy", "F1"],
            var_name="Metric",
            value_name="Score",
        )
        quality["Score"] = pd.to_numeric(quality["Score"], errors="coerce")
        if quality["Score"].notna().any():
            fig = px.bar(
                quality,
                x="Task",
                y="Score",
                color="Metric",
                barmode="group",
                color_discrete_map={"Accuracy": ACCENT, "F1": VIOLET},
                hover_data=["Domain", "Status"],
            )
            fig.update_layout(
                **plot_layout,
                yaxis={
                    "range": [0, 1.05],
                    "gridcolor": "rgba(148,163,184,.12)",
                    "zerolinecolor": "rgba(148,163,184,.12)",
                    "title": None,
                },
                xaxis={
                    "gridcolor": "rgba(148,163,184,.12)",
                    "zerolinecolor": "rgba(148,163,184,.12)",
                    "title": None,
                },
                legend_title=None,
                height=330,
            )
            st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
        else:
            st.caption("Accuracy and F1 require a gold-standard result; this run reports extraction confidence instead.")
    else:
        st.caption("No per-task scores are available in this report.")
    st.markdown("</div>", unsafe_allow_html=True)

with latency_col:
    st.markdown('<div class="panel">', unsafe_allow_html=True)
    st.markdown("**Latency by Task**")
    if not task_df.empty:
        latency_df = task_df.copy()
        latency_df["Latency (ms)"] = pd.to_numeric(latency_df["Latency (ms)"], errors="coerce")
        latency_fig = px.bar(
            latency_df,
            x="Latency (ms)",
            y="Task",
            orientation="h",
            color="Latency (ms)",
            color_continuous_scale=[[0, ACCENT], [1, VIOLET]],
            hover_data=["Domain", "Status"],
        )
        latency_fig.update_layout(
            **plot_layout,
            coloraxis_showscale=False,
            xaxis={
                "title": "Milliseconds",
                "gridcolor": "rgba(148,163,184,.12)",
                "zerolinecolor": "rgba(148,163,184,.12)",
            },
            yaxis={"title": None},
            height=330,
        )
        st.plotly_chart(latency_fig, width="stretch", config={"displayModeBar": False})
    else:
        st.caption("No task latency data is available.")
    st.markdown("</div>", unsafe_allow_html=True)

st.markdown('<div class="section-head">Task Execution Detail</div>', unsafe_allow_html=True)
if is_uploaded_run and tasks:
    uploaded_result = tasks[0].get("result") or {}
    extracted_col, confidence_col = st.columns(2, gap="medium")
    with extracted_col:
        st.markdown("**Extracted fields**")
        st.json(uploaded_result.get("extracted_data", {}))
    with confidence_col:
        st.markdown("**Field confidence**")
        st.json(uploaded_result.get("confidence_scores", {}))
        low_confidence = uploaded_result.get("low_confidence_fields", [])
        if low_confidence:
            st.warning(f"Low-confidence fields: {', '.join(low_confidence)}")
if task_df.empty:
    st.info("This run has no per-task results.")
else:
    for row_start in range(0, len(tasks), 3):
        task_columns = st.columns(3, gap="medium")
        for column, task in zip(task_columns, tasks[row_start:row_start + 3]):
            task_metrics = task.get("eval_metrics") or {}
            badge_text, badge_class = _status(task.get("status", "unknown"))
            task_id = html.escape(str(task.get("task_id", "Unknown task")))
            domain_name = html.escape(str(task.get("domain", "unknown")).title())
            accuracy = task_metrics.get("accuracy_score")
            f1 = task_metrics.get("f1_score")
            latency = task_metrics.get("p95_latency_ms", task_metrics.get("latency_ms"))
            with column:
                st.markdown(
                    f"""
                    <div class="task-card">
                        <span class="status {badge_class}">{badge_text}</span>
                        <div class="task-name">{task_id}</div>
                        <div class="task-domain">{domain_name} agent</div>
                        <div class="task-stats">
                            Accuracy <b>{_fmt_number(accuracy, decimals=3)}</b>
                            &nbsp;·&nbsp; F1 <b>{_fmt_number(f1, decimals=3)}</b><br>
                            Latency <b>{_fmt_number(latency, " ms", 0)}</b>
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

with st.expander("Run metadata and raw report"):
    st.json({
        key: value for key, value in active_report.items()
        if key not in {"per_task_results", "_path"}
    })
    st.download_button(
        "Download run report",
        data=json.dumps({key: value for key, value in active_report.items() if key != "_path"}, indent=2),
        file_name=f"{active_report.get('run_id', 'benchmark-run')}.json",
        mime="application/json",
    )

st.markdown(
    '<div style="text-align:center;color:#64748B;font-size:.75rem;padding-top:2rem;">'
    'MICRO1 OPERATIONS CONSOLE · RESULTS PERSISTED LOCALLY'
    '</div>',
    unsafe_allow_html=True,
)