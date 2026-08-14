"""Run the planning pipeline N times and report metrics as mean +/- standard deviation.

A single run cannot support a claim about a configuration change: measured per-project
F1 moved by 0.054 on average between two runs of the same pipeline, which is several
times larger than the aggregate differences worth detecting. Repeating the same
configuration and reporting dispersion is what makes a comparison meaningful.

Each repeat is a separate `plan` subprocess, so one failed run is recorded and the
remaining repeats still execute. Coverage evaluation runs in-process against the
ground truth, and every run also keeps its own coverage_evaluation.* artifacts.

Usage:
    python scripts/repeated_runs.py \
        --config configs/participium.example.yaml \
        --ground-truth data/ground_truth/participium_implemented_stories.yaml \
        --repeats 3 \
        --baseline data/runs/20260721T235416Z-consolidated
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import statistics
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from thesis_rest_tester.evaluation.coverage import (  # noqa: E402
    evaluate_requirement_coverage,
)

_RUN_ID = re.compile(r"^run_id:\s*(\S+)\s*$", re.MULTILINE)
_POSITIVE_STATUSES = ("implemented", "partially_implemented")
METRICS = ("precision", "recall", "f1")


@dataclass
class RunOutcome:
    run_id: str | None
    run_dir: Path | None
    failed: bool = False
    error: str = ""
    # project -> {precision, recall, f1, tp, fp, fn, tn}
    projects: dict[str, dict[str, float | int | None]] = field(default_factory=dict)
    # project -> {status: count}, used to see which labels the model actually reaches for
    status_counts: dict[str, dict[str, int]] = field(default_factory=dict)


def _mean_sd(values: list[float]) -> tuple[float | None, float | None]:
    """Mean and sample standard deviation; sd is undefined for a single observation."""

    usable = [value for value in values if value is not None]
    if not usable:
        return None, None
    if len(usable) == 1:
        return usable[0], None
    return statistics.mean(usable), statistics.stdev(usable)


def _format(value: float | None, digits: int = 3) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _cell(mean: float | None, sd: float | None) -> str:
    if mean is None:
        return "n/a"
    if sd is None:
        return _format(mean)
    return f"{_format(mean)} ± {_format(sd)}"


def execute_run(config: Path, log_dir: Path, index: int, dry_run: bool = False) -> RunOutcome:
    """Run one `plan` subprocess and return its run id, or the failure."""

    command = [
        sys.executable,
        "-m",
        "thesis_rest_tester.cli",
        "plan",
        "--config",
        str(config),
    ]
    if dry_run:
        command.append("--dry-run")
    print(f"[{index}] planning: {' '.join(command[-3:])}", flush=True)
    completed = subprocess.run(
        command,
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    output = (completed.stdout or "") + "\n" + (completed.stderr or "")
    (log_dir / f"run{index}.log").write_text(output, encoding="utf-8")

    match = _RUN_ID.search(completed.stdout or "")
    if completed.returncode != 0 or match is None:
        tail = (completed.stderr or completed.stdout or "").strip().splitlines()
        reason = tail[-1] if tail else f"exit code {completed.returncode}"
        print(f"[{index}] FAILED: {reason}", flush=True)
        return RunOutcome(run_id=None, run_dir=None, failed=True, error=reason)

    run_id = match.group(1)
    print(f"[{index}] completed: {run_id}", flush=True)
    return RunOutcome(run_id=run_id, run_dir=REPOSITORY_ROOT / "data" / "runs" / run_id)


def collect_metrics(outcome: RunOutcome, ground_truth: Path) -> None:
    """Evaluate one run against the ground truth and record its per-project numbers."""

    if outcome.run_dir is None:
        return
    report = evaluate_requirement_coverage(outcome.run_dir, ground_truth)
    for name, project in report.projects.items():
        outcome.projects[name] = {
            "precision": project.precision,
            "recall": project.recall,
            "f1": project.f1,
            "tp": project.true_positives,
            "fp": project.false_positives,
            "fn": project.false_negatives,
            "tn": project.true_negatives,
        }

    # Which status labels the matcher reached for. partially_implemented counts as a
    # positive prediction, so a run that stops using it loses recall on that alone.
    for coverage_file in sorted(outcome.run_dir.glob("projects/*/requirement_coverage.json")):
        name = coverage_file.parent.name
        counts: dict[str, int] = {}
        for match in json.loads(coverage_file.read_text(encoding="utf-8"))["matches"]:
            status = str(match.get("status"))
            counts[status] = counts.get(status, 0) + 1
        outcome.status_counts[name] = counts


def _run_aggregates(outcome: RunOutcome) -> dict[str, float | None]:
    """Macro and micro metrics for one run, so dispersion is measured across runs."""

    projects = outcome.projects.values()
    if not projects:
        return {}
    macro = {
        f"macro_{metric}": _mean_sd([p[metric] for p in projects])[0] for metric in METRICS
    }
    tp = sum(int(p["tp"]) for p in projects)
    fp = sum(int(p["fp"]) for p in projects)
    fn = sum(int(p["fn"]) for p in projects)
    tn = sum(int(p["tn"]) for p in projects)
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = (
        None
        if precision is None or recall is None or precision + recall == 0
        else 2 * precision * recall / (precision + recall)
    )
    return {
        **macro,
        "micro_precision": precision,
        "micro_recall": recall,
        "micro_f1": f1,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
    }


def _load_baseline(path: Path | None) -> dict[str, dict[str, float | None]]:
    if path is None:
        return {}
    evaluation = path / "coverage_evaluation.json"
    if not evaluation.is_file():
        print(f"warning: no coverage_evaluation.json in {path}; skipping baseline comparison")
        return {}
    projects = json.loads(evaluation.read_text(encoding="utf-8"))["projects"]
    return {name: {m: project.get(m) for m in METRICS} for name, project in projects.items()}


def build_report(
    outcomes: list[RunOutcome],
    baseline: dict[str, dict[str, float | None]],
) -> dict[str, object]:
    successful = [outcome for outcome in outcomes if not outcome.failed]
    project_names = sorted({name for outcome in successful for name in outcome.projects})

    per_project: dict[str, dict[str, object]] = {}
    for name in project_names:
        entry: dict[str, object] = {"runs": sum(name in o.projects for o in successful)}
        for metric in METRICS:
            values = [o.projects[name][metric] for o in successful if name in o.projects]
            mean, sd = _mean_sd([v for v in values if v is not None])
            entry[metric] = {"mean": mean, "sd": sd, "values": values}
            if name in baseline and baseline[name].get(metric) is not None and mean is not None:
                entry[metric]["baseline"] = baseline[name][metric]
                entry[metric]["delta_vs_baseline"] = mean - baseline[name][metric]
        entry["partially_implemented"] = [
            o.status_counts.get(name, {}).get("partially_implemented", 0) for o in successful
        ]
        entry["not_assessable"] = [
            o.status_counts.get(name, {}).get("not_assessable", 0) for o in successful
        ]
        per_project[name] = entry

    run_level = [_run_aggregates(o) for o in successful]
    overall: dict[str, object] = {}
    for key in (
        "macro_precision",
        "macro_recall",
        "macro_f1",
        "micro_precision",
        "micro_recall",
        "micro_f1",
    ):
        values = [r.get(key) for r in run_level if r.get(key) is not None]
        mean, sd = _mean_sd(values)
        overall[key] = {"mean": mean, "sd": sd, "values": values}

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "repeats_requested": len(outcomes),
        "repeats_successful": len(successful),
        "run_ids": [o.run_id for o in outcomes],
        "failures": [
            {"run_id": o.run_id, "error": o.error} for o in outcomes if o.failed
        ],
        "overall": overall,
        "per_project": per_project,
    }


def _csv_text(report: dict[str, object]) -> str:
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        [
            "project",
            "runs",
            "precision_mean",
            "precision_sd",
            "recall_mean",
            "recall_sd",
            "f1_mean",
            "f1_sd",
            "f1_baseline",
            "f1_delta",
            "partially_implemented_per_run",
            "not_assessable_per_run",
        ]
    )
    for name, entry in report["per_project"].items():  # type: ignore[index]
        f1 = entry["f1"]
        writer.writerow(
            [
                name,
                entry["runs"],
                _format(entry["precision"]["mean"]),
                _format(entry["precision"]["sd"]),
                _format(entry["recall"]["mean"]),
                _format(entry["recall"]["sd"]),
                _format(f1["mean"]),
                _format(f1["sd"]),
                _format(f1.get("baseline")),
                _format(f1.get("delta_vs_baseline")),
                "|".join(str(v) for v in entry["partially_implemented"]),
                "|".join(str(v) for v in entry["not_assessable"]),
            ]
        )
    return output.getvalue()


def _markdown(report: dict[str, object]) -> str:
    overall = report["overall"]  # type: ignore[index]
    lines = [
        "# Repeated runs",
        "",
        f"- Generated: `{report['generated_at']}`",
        f"- Repeats: {report['repeats_successful']} successful of {report['repeats_requested']}",
        f"- Run IDs: {', '.join(str(r) for r in report['run_ids'])}",  # type: ignore[index]
        "",
    ]
    if report["failures"]:
        lines.append("## Failed runs")
        lines.append("")
        for failure in report["failures"]:  # type: ignore[union-attr]
            lines.append(f"- `{failure['run_id']}`: {failure['error']}")
        lines.append("")
    lines.extend(["## Overall (mean ± sd across runs)", "", "| Metric | Value |", "| --- | ---: |"])
    for key, entry in overall.items():
        lines.append(f"| {key} | {_cell(entry['mean'], entry['sd'])} |")
    lines.extend(
        [
            "",
            "## Per project",
            "",
            "`partial` and `n/a` list the per-run count of `partially_implemented` and "
            "`not_assessable` labels; both track how willing the matcher was to commit.",
            "",
            "| Project | Precision | Recall | F1 | F1 baseline | Δ | partial | n/a |",
            "| --- | ---: | ---: | ---: | ---: | ---: | --- | --- |",
        ]
    )
    for name, entry in report["per_project"].items():  # type: ignore[index]
        f1 = entry["f1"]
        lines.append(
            f"| {name} "
            f"| {_cell(entry['precision']['mean'], entry['precision']['sd'])} "
            f"| {_cell(entry['recall']['mean'], entry['recall']['sd'])} "
            f"| {_cell(f1['mean'], f1['sd'])} "
            f"| {_format(f1.get('baseline'))} "
            f"| {_format(f1.get('delta_vs_baseline'))} "
            f"| {'|'.join(str(v) for v in entry['partially_implemented'])} "
            f"| {'|'.join(str(v) for v in entry['not_assessable'])} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--ground-truth", required=True, type=Path)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="A run directory with coverage_evaluation.json to compare against.",
    )
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Use mock LLM responses. Validates the harness end to end; the resulting "
        "metrics are fixtures and must never be reported.",
    )
    arguments = parser.parse_args()

    if arguments.repeats < 1:
        parser.error("--repeats must be at least 1")
    if not arguments.config.is_file():
        parser.error(f"Config not found: {arguments.config}")
    if not arguments.ground_truth.is_file():
        parser.error(f"Ground truth not found: {arguments.ground_truth}")

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir = arguments.out or REPOSITORY_ROOT / "data" / "runs" / f"{stamp}-repeated"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"output: {out_dir}\n")

    outcomes: list[RunOutcome] = []
    for index in range(1, arguments.repeats + 1):
        outcome = execute_run(arguments.config, out_dir, index, dry_run=arguments.dry_run)
        if not outcome.failed:
            try:
                collect_metrics(outcome, arguments.ground_truth)
            except Exception as exc:  # noqa: BLE001 - one bad run must not lose the others
                outcome.failed = True
                outcome.error = f"evaluation failed: {type(exc).__name__}: {exc}"
                print(f"[{index}] {outcome.error}", flush=True)
        outcomes.append(outcome)

    report = build_report(outcomes, _load_baseline(arguments.baseline))
    (out_dir / "aggregate.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (out_dir / "aggregate.csv").write_text(_csv_text(report), encoding="utf-8", newline="")
    (out_dir / "aggregate.md").write_text(_markdown(report), encoding="utf-8")

    print(f"\n{report['repeats_successful']}/{report['repeats_requested']} runs succeeded")
    overall = report["overall"]
    for key in ("macro_f1", "micro_f1", "macro_precision", "macro_recall"):
        entry = overall[key]
        # ASCII here: the Windows console encoding mangles the "±" the files use.
        spread = "" if entry["sd"] is None else f" +/- {_format(entry['sd'])}"
        print(f"  {key}: {_format(entry['mean'])}{spread}")
    print(f"\nwrote aggregate.{{json,csv,md}} to {out_dir}")
    return 0 if report["repeats_successful"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
