from __future__ import annotations

import csv
import json
from pathlib import Path

import yaml
from openpyxl import Workbook
from pypdf import PdfWriter

from thesis_rest_tester.cli import main
from thesis_rest_tester.orchestrator import Orchestrator

ROOT_ARTIFACTS = {
    "config.resolved.yaml",
    "requirements_compact.txt",
    "requirements_analysis.raw.txt",
    "requirements_analysis.json",
    "requirement_coverage_matrix.json",
    "requirement_coverage_matrix.csv",
    "summary.md",
    "projects",
}
PROJECT_ARTIFACTS = {
    "openapi_operations.json",
    "api_analysis.raw.txt",
    "api_analysis.json",
    "requirement_coverage.raw.txt",
    "requirement_coverage.json",
    "test_strategy.raw.txt",
    "test_strategy.json",
    "workflow_plan.json",
    "summary.md",
}


def _blank_pdf(path: Path) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    with path.open("wb") as output:
        writer.write(output)


def _write_openapi(path: Path, endpoint: str, operation_id: str, summary: str) -> None:
    path.write_text(
        f"""
openapi: 3.0.3
paths:
  {endpoint}:
    get:
      operationId: {operation_id}
      summary: {summary}
      responses:
        "200": {{description: Success}}
""",
        encoding="utf-8",
    )


def _fixtures(tmp_path: Path) -> Path:
    description = tmp_path / "description.pdf"
    faq = tmp_path / "faq.pdf"
    stories = tmp_path / "stories.xlsx"
    openapi_a = tmp_path / "team-a.yaml"
    openapi_b = tmp_path / "team-b.yaml"
    runs = tmp_path / "runs"
    _blank_pdf(description)
    _blank_pdf(faq)

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["ID", "Role", "Story"])
    sheet.append(["US-1", "Citizen", "Create a proposal"])
    sheet.append(["US-2", "Admin", "Manage users"])
    workbook.save(stories)

    _write_openapi(openapi_a, "/proposals", "listProposals", "List proposals")
    _write_openapi(openapi_b, "/users", "listUsers", "Manage users")

    config = {
        "project_name": "multi-project-dry-run",
        "run_id": "test-run",
        "llm": {
            "provider": "groq",
            "model": "mock-model",
            "temperature": 0.1,
            "max_tokens": 512,
        },
        "inputs": {
            "requirements": {
                "description_pdf": str(description),
                "user_stories_xlsx": str(stories),
                "faq_pdf": str(faq),
            },
            "projects": [
                {
                    "name": "team-a",
                    "openapi_path": str(openapi_a),
                    "sut_base_url": "http://localhost:8080",
                },
                {
                    "name": "team-b",
                    "openapi_path": str(openapi_b),
                    "sut_base_url": "http://localhost:8081",
                },
            ],
        },
        "execution": {
            "runner": "python_requests",
            "reset_command": None,
            "timeout_seconds": 30,
        },
        "budget": {
            "max_iterations": 1,
            "max_tests_per_iteration": 3,
            "max_llm_calls": 10,
        },
        "output": {"runs_dir": str(runs)},
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return config_path


def test_orchestrator_dry_run_creates_multi_project_artifacts(tmp_path: Path) -> None:
    result = Orchestrator(_fixtures(tmp_path), dry_run=True).run()

    assert result.run_id == "test-run"
    assert ROOT_ARTIFACTS == {path.name for path in result.output_dir.iterdir()}
    assert set(result.workflow_plans) == {"team-a", "team-b"}

    shared = json.loads(
        (result.output_dir / "requirements_analysis.json").read_text(encoding="utf-8")
    )
    assert [item["id"] for item in shared["requirements"]] == ["US-1", "US-2"]

    for project_name in result.workflow_plans:
        project_dir = result.output_dir / "projects" / project_name
        assert PROJECT_ARTIFACTS == {path.name for path in project_dir.iterdir()}
        coverage = json.loads(
            (project_dir / "requirement_coverage.json").read_text(encoding="utf-8")
        )
        assert coverage["requirements_total"] == 2
        assert {item["requirement_id"] for item in coverage["matches"]} == {
            "US-1",
            "US-2",
        }
        workflow = json.loads(
            (project_dir / "workflow_plan.json").read_text(encoding="utf-8")
        )
        assert workflow["project_name"] == project_name
        assert {item["test_type"] for item in workflow["strategy_items"]} == {
            "happy_path",
            "edge_case",
            "negative",
        }

    with (result.output_dir / "requirement_coverage_matrix.csv").open(
        encoding="utf-8", newline=""
    ) as source:
        rows = list(csv.DictReader(source))
    assert len(rows) == 2
    assert set(rows[0]) == {
        "requirement_id",
        "requirement_summary",
        "team-a",
        "team-b",
    }


def test_cli_dry_run_reports_output(tmp_path: Path, capsys) -> None:
    config_path = _fixtures(tmp_path)

    assert main(["plan", "--config", str(config_path), "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "run_id: test-run" in output
    assert "output_folder:" in output
    assert "projects: team-a, team-b" in output
