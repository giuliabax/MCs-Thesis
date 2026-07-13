from __future__ import annotations

import csv
import json
from pathlib import Path

import yaml

from thesis_rest_tester.evaluation.coverage import evaluate_requirement_coverage


def test_evaluate_requirement_coverage_writes_metrics(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "run-1"
    project_dir = run_dir / "projects" / "team-a"
    project_dir.mkdir(parents=True)
    coverage = {
        "matches": [
            {
                "requirement_id": "PT01",
                "status": "implemented",
                "matched_operations": [],
                "evidence": [],
                "missing_behaviors": [],
                "rationale": "covered",
            },
            {
                "requirement_id": "PT02",
                "status": "partially_implemented",
                "matched_operations": [],
                "evidence": [],
                "missing_behaviors": [],
                "rationale": "partial",
            },
            {
                "requirement_id": "PT03",
                "status": "implemented",
                "matched_operations": [],
                "evidence": [],
                "missing_behaviors": [],
                "rationale": "false positive",
            },
            {
                "requirement_id": "PT04",
                "status": "not_implemented",
                "matched_operations": [],
                "evidence": [],
                "missing_behaviors": [],
                "rationale": "missed",
            },
            {
                "requirement_id": "PT05",
                "status": "not_assessable",
                "matched_operations": [],
                "evidence": [],
                "missing_behaviors": [],
                "rationale": "negative",
            },
        ]
    }
    (project_dir / "requirement_coverage.json").write_text(
        json.dumps(coverage),
        encoding="utf-8",
    )
    ground_truth = tmp_path / "truth.yaml"
    ground_truth.write_text(
        yaml.safe_dump(
            {
                "projects": {
                    "team-a": {
                        "implemented_requirement_ids": [1, "PT02", "4"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    report = evaluate_requirement_coverage(run_dir, ground_truth)

    project = report.projects["team-a"]
    assert project.true_positives == 2
    assert project.false_positives == 1
    assert project.false_negatives == 1
    assert project.true_negatives == 1
    assert project.precision == 2 / 3
    assert project.recall == 2 / 3
    assert project.f1 == 2 / 3

    assert (run_dir / "coverage_evaluation.json").is_file()
    assert (run_dir / "coverage_evaluation.md").is_file()
    with (run_dir / "coverage_evaluation.csv").open(encoding="utf-8", newline="") as source:
        rows = list(csv.DictReader(source))
    assert rows[0]["project_name"] == "team-a"
    assert {row["outcome"] for row in rows} == {
        "true_positive",
        "false_positive",
        "false_negative",
        "true_negative",
    }
