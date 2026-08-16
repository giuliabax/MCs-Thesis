"""The deterministic classifier, exercised on the evidence the real runs produced.

Each test here reproduces a failure that actually occurred on team13 or team16, because a
classification rule is only worth having if it fires on the thing it was written for.
"""

from __future__ import annotations

import json
from pathlib import Path

from thesis_rest_tester.domain.execution import (
    CaseExecutionRecord,
    ExecutionReport,
    ProjectExecutionRecord,
)
from thesis_rest_tester.evaluation.evaluator import Evaluator

_PROJECT = "participium-teamNN"


def _build_run(
    tmp_path: Path,
    *,
    strategy: list[dict] | None = None,
    cases: list[dict] | None = None,
    exchanges: list[dict] | None = None,
    operations: list[dict] | None = None,
) -> Path:
    project = tmp_path / "projects" / _PROJECT
    (project / "suite").mkdir(parents=True)
    (project / "execution").mkdir(parents=True)
    (project / "test_strategy.json").write_text(json.dumps(strategy or []), encoding="utf-8")
    (project / "openapi_operations.json").write_text(
        json.dumps(operations or []), encoding="utf-8"
    )
    (project / "suite" / "generation_report.json").write_text(
        json.dumps({"cases": cases or []}), encoding="utf-8"
    )
    (project / "execution" / "http_exchanges.json").write_text(
        json.dumps(exchanges or []), encoding="utf-8"
    )
    return tmp_path


def _evaluate(tmp_path: Path, records: list[CaseExecutionRecord], *, outcome="completed", **art):
    run = _build_run(tmp_path, **art)
    report = ExecutionReport(
        run_id="r",
        started_at="2026-01-01T00:00:00Z",
        projects={
            _PROJECT: ProjectExecutionRecord(
                project_name=_PROJECT, outcome=outcome, cases=records
            )
        },
    )
    return Evaluator(run).run(report).projects[_PROJECT]


def _record(name: str, message: str, phase: str = "step") -> CaseExecutionRecord:
    return CaseExecutionRecord(
        project_name=_PROJECT,
        test_name=name,
        outcome="failed",
        failure_phase=phase,
        message=message,
    )


def _item(requirement, method, path, test_type, codes) -> dict:
    return {
        "requirement_id": requirement,
        "requirement_summary": "s",
        "api_endpoint": path,
        "http_method": method,
        "prompt": "p",
        "test_type": test_type,
        "priority": "high",
        "expected_status_codes": codes,
    }


def _case(name, requirement, test_type, method, path) -> dict:
    return {
        "name": name,
        "requirement_id": requirement,
        "test_type": test_type,
        "steps": [{"request": {"method": method, "path": path}}],
    }


def test_a_failed_verification_email_is_an_environment_cause(tmp_path) -> None:
    """team13's real failure. The team's mailbox no longer accepts logins and the
    application treats a failed send as a failed registration, so six tests died in setup
    for a reason that says nothing about the tests or the application's logic."""

    message = 'POST /citizen/signup returned 400: "Failed to send verification email"'
    evaluation = _evaluate(tmp_path, [_record("test_signup", message, phase="setup")])
    assert [d.cause for d in evaluation.diagnoses] == ["environment"]
    assert not evaluation.is_actionable


def test_a_service_name_inside_a_path_is_not_an_environment_failure(tmp_path) -> None:
    """Regression. An earlier rule listed service nouns, so team13's real
    `GET /geocode returned 400: Parameter 'address' must be url encoded` was filed as an
    environment problem because the endpoint's own name matched the pattern. It is a
    generated test sending an unencoded parameter, which is what the loop should fix."""

    message = "GET /geocode returned 400: Parameter 'address' must be url encoded"
    evaluation = _evaluate(
        tmp_path,
        [_record("test_geocode", message)],
        cases=[_case("test_geocode", "PT04", "happy_path", "GET", "/geocode")],
    )
    assert [d.cause for d in evaluation.diagnoses] == ["generation"]
    assert evaluation.diagnoses[0].rule == "request_rejected_as_malformed"


def test_a_requirement_mapped_to_the_wrong_operation_is_a_planning_cause(tmp_path) -> None:
    """team16's dominant defect, and not the one it first looked like.

    The strategy maps "register a citizen" to POST /auth/users, whose summary is *Logs
    user into the system*, and expects 201 where the contract documents only 200 and 400.
    The written test followed its instruction exactly, so regenerating it reproduces the
    same failure -- the instruction is what has to change.
    """

    evaluation = _evaluate(
        tmp_path,
        [_record("test_pt01_register", 'POST /auth/users returned 404: "User not found"')],
        strategy=[_item("PT01", "POST", "/auth/users", "happy_path", ["201"])],
        cases=[_case("test_pt01_register", "PT01", "happy_path", "POST", "/auth/users")],
        operations=[
            {
                "method": "POST",
                "path": "/auth/users",
                "summary": "Logs user into the system.",
                "response_codes": ["200", "400", "default"],
            }
        ],
    )
    assert [d.cause for d in evaluation.diagnoses] == ["planning"]
    assert evaluation.replan_requirements == ["PT01"]
    assert evaluation.regenerate_items == []


def test_a_rejection_without_credentials_is_a_generation_cause(tmp_path) -> None:
    evaluation = _evaluate(
        tmp_path,
        [_record("test_create", "POST /maintainers returned 401: Missing Authorization")],
        strategy=[_item("PT26", "POST", "/maintainers", "happy_path", ["201"])],
        cases=[_case("test_create", "PT26", "happy_path", "POST", "/maintainers")],
        exchanges=[
            {
                "test_name": "test_create",
                "method": "POST",
                "path": "/maintainers",
                "status_code": 401,
            }
        ],
    )
    assert evaluation.diagnoses[0].rule == "authentication_missing"
    assert evaluation.regenerate_items == ["PT26|POST /maintainers|happy_path"]


def test_a_negative_test_about_missing_authorization_is_not_a_defect(tmp_path) -> None:
    """Its whole subject is the 401, so provoking one is the test working."""

    evaluation = _evaluate(
        tmp_path,
        [_record("test_requires_auth", "POST /maintainers returned 401")],
        strategy=[_item("PT26", "POST", "/maintainers", "negative", ["401"])],
        cases=[_case("test_requires_auth", "PT26", "negative", "POST", "/maintainers")],
        exchanges=[
            {
                "test_name": "test_requires_auth",
                "method": "POST",
                "path": "/maintainers",
                "status_code": 401,
            }
        ],
    )
    assert evaluation.diagnoses[0].rule != "authentication_missing"


def test_asserting_a_collection_is_populated_without_creating_anything(tmp_path) -> None:
    """Every execution starts from an empty database, so such a test can only pass by
    accident. It occurred on both projects executed so far."""

    evaluation = _evaluate(
        tmp_path,
        [_record("test_map", "AssertionError: assert not True where True = is_empty(None)")],
        strategy=[_item("PT28", "GET", "/reports", "happy_path", ["200"])],
        cases=[_case("test_map", "PT28", "happy_path", "GET", "/reports")],
        exchanges=[
            {"test_name": "test_map", "method": "GET", "path": "/reports", "status_code": 200}
        ],
    )
    assert evaluation.diagnoses[0].rule == "assumed_pre_existing_data"


def test_a_test_that_never_called_its_own_operation_is_regenerated(tmp_path) -> None:
    evaluation = _evaluate(
        tmp_path,
        [_record("test_create_user", "POST /auth/users returned 404")],
        strategy=[_item("PT01", "POST", "/users", "happy_path", ["200"])],
        cases=[_case("test_create_user", "PT01", "happy_path", "POST", "/auth/users")],
        exchanges=[
            {
                "test_name": "test_create_user",
                "method": "POST",
                "path": "/auth/users",
                "status_code": 404,
            }
        ],
        operations=[{"method": "POST", "path": "/users", "response_codes": ["200"]}],
    )
    assert evaluation.diagnoses[0].rule == "planned_operation_never_called"
    assert evaluation.regenerate_items == ["PT01|POST /users|happy_path"]


def test_a_server_error_is_reported_and_never_fed_back(tmp_path) -> None:
    """The finding the whole pipeline exists to produce. Repairing our own side would
    hide it."""

    evaluation = _evaluate(
        tmp_path,
        [_record("test_reports", "GET /reports returned 500: Internal Server Error")],
        strategy=[_item("PT22", "GET", "/reports", "happy_path", ["200"])],
        cases=[_case("test_reports", "PT22", "happy_path", "GET", "/reports")],
        exchanges=[
            {"test_name": "test_reports", "method": "GET", "path": "/reports", "status_code": 500}
        ],
    )
    assert [d.cause for d in evaluation.diagnoses] == ["sut_defect"]
    assert not evaluation.is_actionable


def test_a_project_that_never_started_is_inconclusive_rather_than_diagnosed(tmp_path) -> None:
    """Its cases are all not_run, so diagnosing them would manufacture evidence."""

    evaluation = _evaluate(
        tmp_path,
        [
            CaseExecutionRecord(
                project_name=_PROJECT, test_name="t", outcome="not_run", message="no start"
            )
        ],
        outcome="startup_failed",
    )
    assert evaluation.diagnoses == []
    assert evaluation.inconclusive_reason is not None
    assert not evaluation.is_actionable
