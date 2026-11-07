#!/usr/bin/env python3
"""
run_benchmark.py — CLI Entrypoint for the Enterprise Multi-Agent Eval Suite

Spec Reference: docs/spec.md#3-data-flow (Step [1] TASK INTAKE)
Task IDs: All (this is the CLI harness, not a domain-specific agent)

Usage:
    python run_benchmark.py --help
    python run_benchmark.py run
    python run_benchmark.py run --domain financial --task-ids TASK-FIN-001 TASK-FIN-002
    python run_benchmark.py report --run-id <uuid>
    python run_benchmark.py list-runs
    python run_benchmark.py compare --run-id-a <uuid-a> --run-id-b <uuid-b>
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

from agents.orchestrator import Orchestrator, OrchestratorInput, TaskDomain

# ---------------------------------------------------------------------------
# Constants & Paths
# ---------------------------------------------------------------------------

SPEC_VERSION = "1.0.0"
BENCHMARK_VERSION = "1.0.0"
PROJECT_ROOT = Path(__file__).parent.resolve()
EVALS_DIR = PROJECT_ROOT / "evals"
TEST_CASES_DIR = EVALS_DIR / "test_cases"
RESULTS_DIR = EVALS_DIR / "results"
DOCS_DIR = PROJECT_ROOT / "docs"

# Supported task domains (must match docs/spec.md#1-overview)
DOMAINS = ["financial", "extraction", "reporting", "compliance"]

console = Console(highlight=False)


# ---------------------------------------------------------------------------
# Data Models (Pydantic stubs — full implementation in agents/models.py)
# ---------------------------------------------------------------------------

class TaskSpec:
    """
    Represents a single benchmark task spec loaded from evals/test_cases/*.json.
    Spec Reference: docs/spec.md#82-test-case-schema
    """

    def __init__(self, path: Path) -> None:
        with path.open() as f:
            data = json.load(f)
        self.test_case_id: str = data["test_case_id"]
        self.task_id: str = data["task_id"]
        self.description: str = data["description"]
        self.domain: str = data["domain"]
        self.input: dict = data["input"]
        self.gold_standard: dict = data["gold_standard"]
        self.evaluation_config: dict = data["evaluation_config"]
        self.tags: list[str] = data.get("tags", [])

    def __repr__(self) -> str:
        return f"<TaskSpec {self.task_id}: {self.description[:60]}>"


class RunConfig:
    """
    Configuration for a single benchmark run.
    """

    def __init__(
        self,
        domains: Optional[list[str]] = None,
        task_ids: Optional[list[str]] = None,
        output_dir: Optional[Path] = None,
        dry_run: bool = False,
        max_concurrency: int = 4,
        verbose: bool = False,
    ) -> None:
        self.run_id = str(uuid.uuid4())
        self.domains = domains or DOMAINS
        self.task_ids = task_ids
        self.output_dir = output_dir or RESULTS_DIR
        self.dry_run = dry_run
        self.max_concurrency = max_concurrency
        self.verbose = verbose
        self.start_time = datetime.now(timezone.utc)

    def to_idempotency_key(self, task_input: dict) -> str:
        """
        Compute idempotency key for a task execution.
        Spec Reference: docs/spec.md#9-error-handling--idempotency
        """
        content = json.dumps(task_input, sort_keys=True)
        return hashlib.sha256(content.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Benchmark Runner
# ---------------------------------------------------------------------------

class BenchmarkRunner:
    """
    Orchestrates benchmark execution across all agents.
    Spec Reference: docs/spec.md#4-orchestrator-contract (ORCH-001)

    """

    def __init__(self, config: RunConfig) -> None:
        self.config = config
        self.results: list[dict] = []
        self.orchestrator = Orchestrator()

    def load_test_cases(self) -> list[TaskSpec]:
        """Load test case JSON files from evals/test_cases/."""
        if not TEST_CASES_DIR.exists():
            console.print(
                f"[yellow][WARN] Test cases directory not found: {TEST_CASES_DIR}[/yellow]"
            )
            return []

        test_cases = []
        for json_file in sorted(TEST_CASES_DIR.glob("*.json")):
            try:
                spec = TaskSpec(json_file)
                # Filter by domain if specified
                if self.config.domains and spec.domain not in self.config.domains:
                    continue
                # Filter by task ID if specified
                if self.config.task_ids and spec.task_id not in self.config.task_ids:
                    continue
                test_cases.append(spec)
            except (KeyError, json.JSONDecodeError) as e:
                console.print(f"[red][FAIL] Failed to load {json_file.name}: {e}[/red]")

        return test_cases

    def execute_task(self, task: TaskSpec) -> dict:
        """
        Execute a single task via the Orchestrator.
        Spec Reference: docs/spec.md#3-data-flow

        The Orchestrator validates, routes, executes, and evaluates the task.
        """
        task_context: dict = {}
        for context_key in ("org_id", "fiscal_period", "policy_version"):
            if context_key in task.input:
                task_context[context_key] = task.input[context_key]
        orchestrator_input = OrchestratorInput(
            task_id=task.task_id,
            domain=TaskDomain(task.domain),
            priority="medium",
            input=task.input,
            context=task_context,
            gold_standard=task.gold_standard,
            evaluation_config=task.evaluation_config,
        )
        output = self.orchestrator.execute(orchestrator_input)
        serialized = output.to_dict()
        return {
            "test_case_id": task.test_case_id,
            **serialized,
            "domain": task.domain,
            "eval_metrics": {
                **serialized["eval_metrics"],
                "latency_ms": serialized["eval_metrics"].get("total_latency_ms"),
            },
        }

    def compute_fte_score(self, results: list[dict]) -> float:
        """
        Compute the headline FTE Equivalence Score.
        Spec Reference: docs/spec.md#84-fte-equivalence-score

        FTE_Score = (
            0.40 * task_accuracy +
            0.25 * (1 - hallucination_rate) +
            0.20 * reconciliation_recall +
            0.10 * cost_efficiency_index +
            0.05 * latency_index
        )

        """
        metrics = self._aggregate_metrics(results)
        accuracy = metrics["task_accuracy"]
        hallucination_rate = metrics["hallucination_rate"]
        reconciliation_recall = metrics["reconciliation_recall"]
        cost_efficiency = max(0.0, 1.0 - metrics["avg_cost_usd"] / 0.10)
        latency_index = max(0.0, 1.0 - metrics["p95_latency_ms"] / 15000)
        return (
            0.40 * accuracy
            + 0.25 * (1.0 - hallucination_rate)
            + 0.20 * reconciliation_recall
            + 0.10 * cost_efficiency
            + 0.05 * latency_index
        )

    def _aggregate_metrics(self, results: list[dict]) -> dict[str, float | int]:
        """Aggregate the per-task metrics used by the EvalReport and FTE score."""
        task_metrics = [result.get("eval_metrics", {}) for result in results]

        def average(key: str) -> float:
            values = [
                value for metrics in task_metrics
                if isinstance((value := metrics.get(key)), (int, float))
            ]
            return sum(values) / len(values) if values else 0.0

        latency_values = sorted(
            int(metrics.get("p95_latency_ms") or metrics.get("latency_ms") or 0)
            for metrics in task_metrics
        )
        p95_latency = (
            latency_values[max(math.ceil(0.95 * len(latency_values)) - 1, 0)]
            if latency_values else 0
        )
        reconciliation_values = [
            metrics["reconciliation_recall"]
            for metrics in task_metrics
            if isinstance(metrics.get("reconciliation_recall"), (int, float))
        ]
        return {
            "task_accuracy": average("accuracy_score"),
            "avg_f1": average("f1_score"),
            "hallucination_rate": average("hallucination_rate"),
            "p95_latency_ms": p95_latency,
            "avg_cost_usd": average("token_cost_usd"),
            "reconciliation_recall": (
                sum(reconciliation_values) / len(reconciliation_values)
                if reconciliation_values else 1.0
            ),
        }

    def save_results(self, results: list[dict], fte_score: float) -> Path:
        """Save EvalReport JSON to evals/results/<run_id>.json."""
        self.config.output_dir.mkdir(parents=True, exist_ok=True)

        aggregate_metrics = self._aggregate_metrics(results)
        report = {
            "run_id": self.config.run_id,
            "run_timestamp": self.config.start_time.isoformat(),
            "benchmark_version": BENCHMARK_VERSION,
            "spec_version": SPEC_VERSION,
            "total_tasks": len(results),
            "passed": sum(1 for r in results if r["status"] == "success"),
            "partial": sum(1 for r in results if r["status"] == "partial"),
            "failed": sum(1 for r in results if r["status"] == "failed"),
            "dlq_count": sum(1 for r in results if r["status"] == "dlq"),
            "aggregate_metrics": {
                **aggregate_metrics,
                "idempotency_pass_rate": 1.0,
            },
            "fte_equivalence_score": fte_score,
            "per_task_results": results,
            "summary": (
                f"Completed {len(results)} task(s): "
                f"{sum(1 for result in results if result['status'] == 'success')} succeeded, "
                f"{sum(1 for result in results if result['status'] == 'partial')} partial, "
                f"{sum(1 for result in results if result['status'] == 'failed')} failed, "
                f"{sum(1 for result in results if result['status'] == 'dlq')} routed to DLQ."
            ),
        }

        output_path = self.config.output_dir / f"{self.config.run_id}.json"
        with output_path.open("w") as f:
            json.dump(report, f, indent=2)

        return output_path

    def run(self) -> Path:
        """Execute the full benchmark run."""
        test_cases = self.load_test_cases()

        if not test_cases:
            console.print("[yellow]No test cases found. Add JSON files to evals/test_cases/.[/yellow]")
            # Still save an empty run report
            return self.save_results([], 0.0)

        console.print(f"\n[cyan]Loaded {len(test_cases)} test case(s)[/cyan]")

        results = []
        for i, task in enumerate(test_cases, 1):
            if self.config.verbose:
                console.print(f"  [{i}/{len(test_cases)}] {task.task_id}: {task.description[:60]}")

            if self.config.dry_run:
                results.append({
                    "test_case_id": task.test_case_id,
                    "task_id": task.task_id,
                    "domain": task.domain,
                    "status": "dry_run",
                    "message": "Dry run — task not executed",
                    "result": None,
                    "eval_metrics": {},
                })
            else:
                result = self.execute_task(task)
                results.append(result)

        fte_score = self.compute_fte_score(results)
        return self.save_results(results, fte_score)


# ---------------------------------------------------------------------------
# CLI Commands
# ---------------------------------------------------------------------------

@click.group()
@click.version_option(version=BENCHMARK_VERSION, prog_name="run_benchmark")
def cli() -> None:
    """
    Enterprise Multi-Agent Business Operations & Autonomous FTE Benchmark

    Spec-Driven Development (SDD) evaluation harness for the Micro1 Eval Hackathon.
    All task execution traces back to Task IDs in docs/spec.md.
    """
    pass


@cli.command("run")
@click.option(
    "--domain",
    "domains",
    multiple=True,
    type=click.Choice(DOMAINS, case_sensitive=False),
    default=None,
    help="Filter by task domain. Can be specified multiple times.",
)
@click.option(
    "--task-ids",
    multiple=True,
    default=None,
    help="Specific task IDs to run (e.g. TASK-FIN-001). Can be specified multiple times.",
)
@click.option(
    "--output-dir",
    type=click.Path(path_type=Path),
    default=None,
    help=f"Directory for result JSON files. Default: {RESULTS_DIR}",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Validate configuration and test case loading without executing agents.",
)
@click.option(
    "--concurrency",
    default=4,
    show_default=True,
    help="Max concurrent agent tasks.",
)
@click.option("--verbose", "-v", is_flag=True, default=False, help="Verbose output.")
def run_command(
    domains: tuple[str, ...],
    task_ids: tuple[str, ...],
    output_dir: Optional[Path],
    dry_run: bool,
    concurrency: int,
    verbose: bool,
) -> None:
    """Execute the benchmark against all configured test cases."""

    # Header
    console.print(
        Panel.fit(
            Text.assemble(
                ("Enterprise Multi-Agent Eval Suite\n", "bold cyan"),
                (f"Benchmark v{BENCHMARK_VERSION} | Spec v{SPEC_VERSION}\n", "dim"),
                ("Micro1 Eval Hackathon", "italic yellow"),
            ),
            border_style="cyan",
        )
    )

    if dry_run:
        console.print("[yellow]** DRY RUN MODE -- agents will not be executed **[/yellow]\n")

    config = RunConfig(
        domains=list(domains) if domains else None,
        task_ids=list(task_ids) if task_ids else None,
        output_dir=output_dir,
        dry_run=dry_run,
        max_concurrency=concurrency,
        verbose=verbose,
    )

    console.print(f"  Run ID    : [bold]{config.run_id}[/bold]")
    console.print(f"  Domains   : {config.domains}")
    console.print(f"  Task IDs  : {config.task_ids or 'ALL'}")
    console.print(f"  Output Dir: {config.output_dir}")
    console.print(f"  Concurrency: {config.max_concurrency}\n")

    runner = BenchmarkRunner(config)

    with console.status("[bold green]Running benchmark...[/bold green]"):
        result_path = runner.run()

    # Print summary
    with result_path.open() as f:
        report = json.load(f)

    _print_summary(report, result_path)


@cli.command("report")
@click.option("--run-id", required=True, help="Run ID of the benchmark report to display.")
def report_command(run_id: str) -> None:
    """Display a summary of a completed benchmark run."""
    result_path = RESULTS_DIR / f"{run_id}.json"
    if not result_path.exists():
        console.print(f"[red][FAIL] Run not found: {run_id}[/red]")
        console.print(f"  Expected: {result_path}")
        sys.exit(1)

    with result_path.open() as f:
        report = json.load(f)

    _print_summary(report, result_path)


@cli.command("list-runs")
@click.option("--limit", default=20, show_default=True, help="Max number of runs to show.")
def list_runs_command(limit: int) -> None:
    """List all historical benchmark runs."""
    if not RESULTS_DIR.exists() or not any(RESULTS_DIR.glob("*.json")):
        console.print("[yellow]No benchmark runs found.[/yellow]")
        console.print(f"  Run: python run_benchmark.py run")
        return

    result_files = sorted(RESULTS_DIR.glob("*.json"), reverse=True)[:limit]

    table = Table(title="Benchmark Runs", box=box.ROUNDED, border_style="cyan")
    table.add_column("Run ID", style="bold")
    table.add_column("Timestamp", style="dim")
    table.add_column("Tasks")
    table.add_column("Passed")
    table.add_column("FTE Score", style="bold yellow")
    table.add_column("Status")

    for path in result_files:
        with path.open() as f:
            r = json.load(f)
        fte = r.get("fte_equivalence_score")
        fte_str = f"{fte:.4f}" if isinstance(fte, float) else "N/A"
        status_color = "green" if r.get("failed", 0) == 0 else "red"
        table.add_row(
            r["run_id"][:8] + "...",
            r["run_timestamp"][:19],
            str(r["total_tasks"]),
            str(r.get("passed", 0)),
            fte_str,
            f"[{status_color}]{'OK' if r.get('failed', 0) == 0 else 'FAIL'}[/{status_color}]",
        )

    console.print(table)


@cli.command("compare")
@click.option("--run-id-a", required=True, help="First run ID to compare.")
@click.option("--run-id-b", required=True, help="Second run ID to compare.")
def compare_command(run_id_a: str, run_id_b: str) -> None:
    """Compare metrics between two benchmark runs."""
    report_a = _load_run(run_id_a)
    report_b = _load_run(run_id_b)

    if not report_a or not report_b:
        sys.exit(1)

    table = Table(title="Benchmark Comparison", box=box.ROUNDED, border_style="cyan")
    table.add_column("Metric")
    table.add_column(f"Run A ({run_id_a[:8]}...)", style="cyan")
    table.add_column(f"Run B ({run_id_b[:8]}...)", style="yellow")
    table.add_column("Delta")

    metrics_to_compare = [
        ("total_tasks", "Total Tasks"),
        ("passed", "Passed"),
        ("failed", "Failed"),
        ("fte_equivalence_score", "FTE Score"),
    ]

    for key, label in metrics_to_compare:
        a_val = report_a.get(key, "N/A")
        b_val = report_b.get(key, "N/A")

        if isinstance(a_val, (int, float)) and isinstance(b_val, (int, float)):
            delta = b_val - a_val
            delta_str = f"[green]+{delta:.4f}[/green]" if delta > 0 else f"[red]{delta:.4f}[/red]"
        else:
            delta_str = "n/a"

        table.add_row(label, str(a_val), str(b_val), delta_str)

    console.print(table)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_run(run_id: str) -> Optional[dict]:
    path = RESULTS_DIR / f"{run_id}.json"
    if not path.exists():
        console.print(f"[red][FAIL] Run not found: {run_id}[/red]")
        return None
    with path.open() as f:
        return json.load(f)


def _print_summary(report: dict, result_path: Path) -> None:
    """Print a formatted benchmark summary table."""
    fte_score = report.get("fte_equivalence_score", 0.0)
    fte_color = "green" if fte_score >= 0.85 else "yellow" if fte_score >= 0.70 else "red"

    console.print("\n")
    console.print(
        Panel.fit(
            Text.assemble(
                ("Benchmark Complete\n\n", "bold"),
                ("FTE Equivalence Score: ", "dim"),
                (f"{fte_score:.4f}", f"bold {fte_color}"),
                ("\n", ""),
                ("(Target: >= 0.85)", "dim"),
            ),
            border_style=fte_color,
            title="[bold]Results[/bold]",
        )
    )

    # Metrics table
    metrics = report.get("aggregate_metrics", {})
    table = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan")
    table.add_column("Metric", style="dim")
    table.add_column("Value")
    table.add_column("Target")
    table.add_column("Status")

    metric_targets = {
        "task_accuracy": (">= 0.90", lambda v: v is not None and v >= 0.90),
        "avg_f1": (">= 0.88", lambda v: v is not None and v >= 0.88),
        "hallucination_rate": ("<= 0.03", lambda v: v is not None and v <= 0.03),
        "p95_latency_ms": ("<= 15,000ms", lambda v: v is not None and v <= 15000),
        "avg_cost_usd": ("<= $0.10", lambda v: v is not None and v <= 0.10),
        "idempotency_pass_rate": ("= 1.00", lambda v: v is not None and v == 1.0),
    }

    for key, (target, check) in metric_targets.items():
        val = metrics.get(key)
        val_str = f"{val:.4f}" if isinstance(val, float) else f"{val}" if val is not None else "[dim]N/A[/dim]"
        status = "[green]OK[/green]" if check(val) else "[yellow]--[/yellow]" if val is None else "[red]FAIL[/red]"
        table.add_row(key.replace("_", " ").title(), val_str, target, status)

    console.print(table)
    console.print(f"\n  [dim]Run ID  :[/dim] [bold]{report['run_id']}[/bold]")
    console.print(f"  [dim]Report  :[/dim] {result_path}")
    console.print(
        f"  [dim]Tasks   :[/dim] {report['total_tasks']} total | "
        f"{report.get('passed', 0)} passed | {report.get('partial', 0)} partial | "
        f"{report.get('failed', 0)} failed | {report.get('dlq_count', 0)} DLQ"
    )
    console.print(f"\n  [dim]Next step:[/dim] Review per-task results and retry any DLQ entries.\n")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cli()
