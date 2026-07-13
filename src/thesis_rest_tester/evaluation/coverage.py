"""Offline evaluation of inferred requirement coverage against a manual oracle."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class CoverageEvaluationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RequirementCoverageEvaluationRow(CoverageEvaluationModel):
    requirement_id: str
    expected_implemented: bool
    predicted_implemented: bool
    predicted_status: str
    outcome: Literal["true_positive", "false_positive", "false_negative", "true_negative"]


class ProjectCoverageEvaluation(CoverageEvaluationModel):
    project_name: str
    requirements_total: int = Field(ge=0)
    expected_implemented_total: int = Field(ge=0)
    predicted_implemented_total: int = Field(ge=0)
    true_positives: int = Field(ge=0)
    false_positives: int = Field(ge=0)
    false_negatives: int = Field(ge=0)
    true_negatives: int = Field(ge=0)
    precision: float | None = Field(default=None, ge=0.0, le=1.0)
    recall: float | None = Field(default=None, ge=0.0, le=1.0)
    f1: float | None = Field(default=None, ge=0.0, le=1.0)
    rows: list[RequirementCoverageEvaluationRow] = Field(default_factory=list)


class CoverageEvaluationReport(CoverageEvaluationModel):
    run_id: str
    ground_truth_path: str
    positive_statuses: list[str]
    projects: dict[str, ProjectCoverageEvaluation]


def evaluate_requirement_coverage(
    run_dir: str | Path,
    ground_truth_path: str | Path,
) -> CoverageEvaluationReport:
    """Compare run coverage artifacts with a manual ground-truth file.

    The ground truth is intentionally consumed only after a run has completed, so it can be used as
    an oracle for analysis without influencing planning.
    """

    run_path = Path(run_dir)
    truth_path = Path(ground_truth_path)
    if not run_path.is_dir():
        raise FileNotFoundError(f"Run directory not found: {run_path}")
    if not truth_path.is_file():
        raise FileNotFoundError(f"Ground-truth file not found: {truth_path}")

    ground_truth = _load_ground_truth(truth_path)
    positive_statuses = {"implemented", "partially_implemented"}
    projects: dict[str, ProjectCoverageEvaluation] = {}
    for project_name, expected_ids in ground_truth.items():
        coverage_path = run_path / "projects" / project_name / "requirement_coverage.json"
        if not coverage_path.is_file():
            raise FileNotFoundError(
                f"Coverage artifact for project {project_name!r} not found: {coverage_path}"
            )
        coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
        matches = coverage.get("matches")
        if not isinstance(matches, list):
            raise ValueError(f"Invalid coverage artifact, missing matches list: {coverage_path}")

        status_by_id = {
            str(match["requirement_id"]): str(match["status"])
            for match in matches
            if isinstance(match, dict) and "requirement_id" in match and "status" in match
        }
        all_ids = set(status_by_id)
        unknown_truth_ids = sorted(expected_ids - all_ids)
        if unknown_truth_ids:
            raise ValueError(
                f"Ground truth for {project_name} contains IDs absent from run coverage: "
                + ", ".join(unknown_truth_ids)
            )
        predicted_ids = {
            requirement_id
            for requirement_id, status in status_by_id.items()
            if status in positive_statuses
        }
        rows = [
            _row(
                requirement_id=requirement_id,
                expected_ids=expected_ids,
                predicted_ids=predicted_ids,
                predicted_status=status_by_id[requirement_id],
            )
            for requirement_id in sorted(all_ids)
        ]
        projects[project_name] = _project_report(project_name, expected_ids, predicted_ids, rows)

    report = CoverageEvaluationReport(
        run_id=run_path.name,
        ground_truth_path=str(truth_path),
        positive_statuses=sorted(positive_statuses),
        projects=projects,
    )
    _write_outputs(run_path, report)
    return report


def _load_ground_truth(path: Path) -> dict[str, set[str]]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("projects"), dict):
        raise ValueError("Ground-truth YAML must contain a projects mapping")

    projects: dict[str, set[str]] = {}
    for project_name, value in raw["projects"].items():
        if isinstance(value, dict):
            raw_ids = value.get("implemented_requirement_ids")
        else:
            raw_ids = value
        if not isinstance(raw_ids, list):
            raise ValueError(
                f"Ground truth for project {project_name!r} must be a list or contain "
                "implemented_requirement_ids"
            )
        projects[str(project_name)] = {_normalize_requirement_id(item) for item in raw_ids}
    return projects


def _normalize_requirement_id(value: object) -> str:
    text = str(value).strip().upper()
    if text.startswith("PT"):
        suffix = text[2:]
    else:
        suffix = text
    if suffix.isdigit():
        return f"PT{int(suffix):02d}"
    return text


def _row(
    *,
    requirement_id: str,
    expected_ids: set[str],
    predicted_ids: set[str],
    predicted_status: str,
) -> RequirementCoverageEvaluationRow:
    expected = requirement_id in expected_ids
    predicted = requirement_id in predicted_ids
    if expected and predicted:
        outcome = "true_positive"
    elif not expected and predicted:
        outcome = "false_positive"
    elif expected and not predicted:
        outcome = "false_negative"
    else:
        outcome = "true_negative"
    return RequirementCoverageEvaluationRow(
        requirement_id=requirement_id,
        expected_implemented=expected,
        predicted_implemented=predicted,
        predicted_status=predicted_status,
        outcome=outcome,
    )


def _project_report(
    project_name: str,
    expected_ids: set[str],
    predicted_ids: set[str],
    rows: list[RequirementCoverageEvaluationRow],
) -> ProjectCoverageEvaluation:
    true_positives = sum(row.outcome == "true_positive" for row in rows)
    false_positives = sum(row.outcome == "false_positive" for row in rows)
    false_negatives = sum(row.outcome == "false_negative" for row in rows)
    true_negatives = sum(row.outcome == "true_negative" for row in rows)
    precision = _ratio(true_positives, true_positives + false_positives)
    recall = _ratio(true_positives, true_positives + false_negatives)
    f1 = (
        None
        if precision is None or recall is None or precision + recall == 0
        else 2 * precision * recall / (precision + recall)
    )
    return ProjectCoverageEvaluation(
        project_name=project_name,
        requirements_total=len(rows),
        expected_implemented_total=len(expected_ids),
        predicted_implemented_total=len(predicted_ids),
        true_positives=true_positives,
        false_positives=false_positives,
        false_negatives=false_negatives,
        true_negatives=true_negatives,
        precision=precision,
        recall=recall,
        f1=f1,
        rows=rows,
    )


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return numerator / denominator


def _write_outputs(run_path: Path, report: CoverageEvaluationReport) -> None:
    (run_path / "coverage_evaluation.json").write_text(
        json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (run_path / "coverage_evaluation.csv").write_text(
        _csv(report),
        encoding="utf-8",
    )
    (run_path / "coverage_evaluation.md").write_text(
        _markdown(report),
        encoding="utf-8",
    )


def _csv(report: CoverageEvaluationReport) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "project_name",
            "requirement_id",
            "expected_implemented",
            "predicted_implemented",
            "predicted_status",
            "outcome",
        ],
    )
    writer.writeheader()
    for project_name, project in report.projects.items():
        for row in project.rows:
            writer.writerow({"project_name": project_name, **row.model_dump(mode="json")})
    return output.getvalue()


def _markdown(report: CoverageEvaluationReport) -> str:
    lines = [
        f"# Coverage Evaluation: {report.run_id}",
        "",
        f"- Ground truth: `{report.ground_truth_path}`",
        f"- Positive statuses: {', '.join(report.positive_statuses)}",
        "",
        "| Project | Precision | Recall | F1 | TP | FP | FN | TN |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for project in report.projects.values():
        lines.append(
            "| "
            + " | ".join(
                [
                    project.project_name,
                    _format_metric(project.precision),
                    _format_metric(project.recall),
                    _format_metric(project.f1),
                    str(project.true_positives),
                    str(project.false_positives),
                    str(project.false_negatives),
                    str(project.true_negatives),
                ]
            )
            + " |"
        )
    lines.append("")
    lines.append("Ground truth is used only after planning and does not influence the run.")
    lines.append("")
    return "\n".join(lines)


def _format_metric(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.3f}"
