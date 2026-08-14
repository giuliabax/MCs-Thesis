from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from thesis_rest_tester.domain.executable import ExecutableTestCase, RequestSpec
from thesis_rest_tester.domain.executable import TestStep as Step
from thesis_rest_tester.execution.junit import (
    all_not_run,
    classify_failure_phase,
    join_cases,
    load_generated_cases,
    outcome_for_exit_code,
    parse_junit,
    report_is_usable,
)
from thesis_rest_tester.execution.python_requests_runner import PythonRequestsRunner
from thesis_rest_tester.generation.renderer import render_conftest, render_suite

SUITE_XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" tests="4">
    <testcase classname="test_team01" name="test_pass" time="0.412"/>
    <testcase classname="test_team01" name="test_fail" time="1.003">
      <failure message="assert 404 in (200,)">POST /reports returned 404</failure>
    </testcase>
    <testcase classname="test_team01" name="test_setup_broke" time="0.221">
      <failure message="setup step failed: POST /auth/login returned 500">boom</failure>
    </testcase>
    <testcase classname="test_team01" name="test_skipped" time="0.0">
      <skipped message="no reason"/>
    </testcase>
  </testsuite>
</testsuites>
"""

BARE_SUITE_XML = """<testsuite name="pytest" tests="1">
  <testcase classname="t" name="test_only" time="0.5"/>
</testsuite>
"""


def _generated(*names: str) -> dict[str, dict[str, str | None]]:
    return {
        name: {"requirement_id": f"PT{index:02d}", "test_type": "happy_path"}
        for index, name in enumerate(names, start=1)
    }


# --- exit codes -------------------------------------------------------------------


def test_exit_codes_zero_and_one_are_both_complete_runs() -> None:
    """Exit 1 means tests failed, which is a result, not a malfunction."""

    assert outcome_for_exit_code(0) == "completed"
    assert outcome_for_exit_code(1) == "completed"
    assert report_is_usable(0) and report_is_usable(1)


def test_exit_codes_without_a_usable_report_are_distinguished() -> None:
    assert outcome_for_exit_code(3) == "runner_error"
    assert outcome_for_exit_code(4) == "collection_error"
    assert outcome_for_exit_code(5) == "empty_suite"
    assert not any(report_is_usable(code) for code in (3, 4, 5))


def test_interrupted_run_keeps_whatever_report_exists() -> None:
    assert outcome_for_exit_code(2) == "interrupted"
    assert report_is_usable(2)


def test_unknown_exit_code_is_a_runner_error() -> None:
    assert outcome_for_exit_code(99) == "runner_error"


# --- parsing ----------------------------------------------------------------------


def test_parse_junit_reads_every_outcome() -> None:
    cases = {case.name: case for case in parse_junit(SUITE_XML)}

    assert cases["test_pass"].outcome == "passed"
    assert cases["test_fail"].outcome == "failed"
    assert cases["test_skipped"].outcome == "skipped"
    assert cases["test_pass"].duration_seconds == 0.412


def test_parse_junit_accepts_a_bare_testsuite_root() -> None:
    assert [case.name for case in parse_junit(BARE_SUITE_XML)] == ["test_only"]


def test_setup_failures_are_distinguished_from_failures_under_test() -> None:
    """The renderer's marker is what separates a broken precondition from a real finding."""

    cases = {case.name: case for case in parse_junit(SUITE_XML)}

    assert classify_failure_phase(cases["test_setup_broke"].message, "failed") == "setup"
    assert classify_failure_phase(cases["test_fail"].message, "failed") == "step"
    assert classify_failure_phase(None, "error") == "collection"
    assert classify_failure_phase(None, "passed") == "unknown"


# --- join -------------------------------------------------------------------------


def test_join_restores_requirement_traceability_the_xml_lacks() -> None:
    records = join_cases("p", _generated("test_pass", "test_fail"), parse_junit(SUITE_XML))

    by_name = {record.test_name: record for record in records}
    assert by_name["test_pass"].requirement_id == "PT01"
    assert by_name["test_pass"].outcome == "passed"
    assert by_name["test_fail"].failure_phase == "step"


def test_a_generated_test_that_never_ran_is_recorded_not_dropped() -> None:
    """Silently losing it would inflate every later rate by shrinking the denominator."""

    records = join_cases("p", _generated("test_pass", "test_never"), parse_junit(SUITE_XML))

    missing = next(r for r in records if r.test_name == "test_never")
    assert missing.outcome == "not_run"
    assert missing.requirement_id == "PT02"


def test_an_executed_case_matching_nothing_generated_is_kept() -> None:
    """A collection error presents exactly this way; dropping it hides the evidence."""

    records = join_cases("p", _generated("test_pass"), parse_junit(SUITE_XML))

    stray = {r.test_name for r in records} - {"test_pass"}
    assert stray == {"test_fail", "test_setup_broke", "test_skipped"}
    assert all(r.requirement_id is None for r in records if r.test_name in stray)


def test_all_not_run_marks_every_generated_case(tmp_path) -> None:
    records = all_not_run("p", _generated("a", "b"), message="SUT never became ready")

    assert [r.outcome for r in records] == ["not_run", "not_run"]
    assert all(r.message == "SUT never became ready" for r in records)


# --- generation report ------------------------------------------------------------


def test_load_generated_cases_reads_the_generation_report(tmp_path) -> None:
    report = tmp_path / "generation_report.json"
    report.write_text(
        json.dumps(
            {
                "cases": [
                    {"name": "test_a", "requirement_id": "PT01", "test_type": "negative"},
                    {"name": "test_b", "requirement_id": "PT02", "test_type": "happy_path"},
                ]
            }
        ),
        encoding="utf-8",
    )

    generated = load_generated_cases(report)

    assert generated["test_a"]["test_type"] == "negative"
    assert set(generated) == {"test_a", "test_b"}


def test_load_generated_cases_tolerates_a_missing_report(tmp_path) -> None:
    assert load_generated_cases(tmp_path / "absent.json") == {}


# --- runner against a stub server --------------------------------------------------


class _StubHandler(BaseHTTPRequestHandler):
    """Answers /reports with JSON and everything else with 404."""

    def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        if self.path.startswith("/reports"):
            body = json.dumps({"id": 7, "title": "a report"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args) -> None:  # keep the test output clean
        return


@pytest.fixture()
def stub_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def _write_suite(suite_dir: Path, cases: list[ExecutableTestCase]) -> None:
    """Materialize a suite exactly as the generator would, using the real renderer."""

    suite_dir.mkdir(parents=True, exist_ok=True)
    (suite_dir / "conftest.py").write_text(
        render_conftest("http://127.0.0.1:1", 10), encoding="utf-8"
    )
    (suite_dir / "test_stub.py").write_text(render_suite("stub", cases), encoding="utf-8")
    (suite_dir / "generation_report.json").write_text(
        json.dumps({"cases": [case.model_dump(mode="json") for case in cases]}),
        encoding="utf-8",
    )


def _case(name: str, path: str, expect: list[int], **kwargs) -> ExecutableTestCase:
    return ExecutableTestCase(
        name=name,
        requirement_id=kwargs.pop("requirement_id", "PT01"),
        test_type="happy_path",
        description="Stub case.",
        steps=[
            Step(
                description="Call the stub.",
                request=RequestSpec(method="GET", path=path),
                expect_status=expect,
                assertions=kwargs.pop("assertions", []),
            )
        ],
        **kwargs,
    )


def test_runner_executes_a_suite_and_records_each_outcome(tmp_path, stub_server) -> None:
    suite = tmp_path / "suite"
    _write_suite(
        suite,
        [
            _case("test_ok", "/reports", [200], requirement_id="PT01"),
            _case("test_wrong_status", "/reports", [201], requirement_id="PT02"),
            _case("test_missing_endpoint", "/absent", [200], requirement_id="PT03"),
        ],
    )

    result = PythonRequestsRunner().run(
        suite,
        project_name="stub",
        base_url=stub_server,
        suite_timeout_seconds=120,
        artifact_dir=tmp_path / "execution",
    )

    outcomes = {case.test_name: case.outcome for case in result.cases}
    assert outcomes == {
        "test_ok": "passed",
        "test_wrong_status": "failed",
        "test_missing_endpoint": "failed",
    }
    # exit 1 means tests ran and some failed, which is a completed run
    assert result.outcome == "completed"
    assert result.exit_code == 1


def test_runner_uses_the_base_url_it_is_given_not_the_generated_default(
    tmp_path, stub_server
) -> None:
    """The suite's DEFAULT_BASE_URL points at a dead port; SUT_BASE_URL must win."""

    suite = tmp_path / "suite"
    _write_suite(suite, [_case("test_ok", "/reports", [200])])

    result = PythonRequestsRunner().run(
        suite,
        project_name="stub",
        base_url=stub_server,
        suite_timeout_seconds=120,
        artifact_dir=tmp_path / "execution",
    )

    assert [case.outcome for case in result.cases] == ["passed"]


def test_runner_records_every_http_exchange_with_a_relative_path(
    tmp_path, stub_server
) -> None:
    """Test verdicts cannot yield operation or status-code coverage; exchanges can."""

    suite = tmp_path / "suite"
    _write_suite(
        suite,
        [
            _case("test_ok", "/reports", [200]),
            _case("test_missing", "/absent", [404], requirement_id="PT02"),
        ],
    )

    result = PythonRequestsRunner().run(
        suite,
        project_name="stub",
        base_url=stub_server,
        suite_timeout_seconds=120,
        artifact_dir=tmp_path / "execution",
    )

    seen = {(exchange.path, exchange.status_code) for exchange in result.exchanges}
    assert ("/reports", 200) in seen
    assert ("/absent", 404) in seen
    assert all(exchange.project_name == "stub" for exchange in result.exchanges)
    assert (tmp_path / "execution" / "http_exchanges.jsonl").is_file()


def test_runner_writes_its_evidence_to_the_artifact_directory(tmp_path, stub_server) -> None:
    suite = tmp_path / "suite"
    _write_suite(suite, [_case("test_ok", "/reports", [200])])

    PythonRequestsRunner().run(
        suite,
        project_name="stub",
        base_url=stub_server,
        suite_timeout_seconds=120,
        artifact_dir=tmp_path / "execution",
    )

    assert (tmp_path / "execution" / "junit.xml").is_file()
    assert (tmp_path / "execution" / "pytest_stdout.txt").is_file()
    # The suite is pinned as its own rootdir so the repository pytest.ini cannot apply.
    assert (suite / "pytest.ini").is_file()


def test_runner_reports_a_suite_that_cannot_be_collected(tmp_path, stub_server) -> None:
    """A generated conftest is model output; a collection error is a live risk."""

    suite = tmp_path / "suite"
    _write_suite(suite, [_case("test_ok", "/reports", [200])])
    (suite / "conftest.py").write_text("this is not valid python(", encoding="utf-8")

    result = PythonRequestsRunner().run(
        suite,
        project_name="stub",
        base_url=stub_server,
        suite_timeout_seconds=120,
        artifact_dir=tmp_path / "execution",
    )

    assert result.outcome in {"collection_error", "runner_error"}
    # Every generated case is still accounted for, rather than silently vanishing.
    assert [case.outcome for case in result.cases] == ["not_run"]


# --- executor ----------------------------------------------------------------------


from thesis_rest_tester.config import load_config  # noqa: E402
from thesis_rest_tester.domain.execution import CaseExecutionRecord  # noqa: E402
from thesis_rest_tester.execution.base import RunnerResult  # noqa: E402
from thesis_rest_tester.execution.executor import SuiteExecutor  # noqa: E402
from thesis_rest_tester.execution.manifest import SutManifest  # noqa: E402

CONFIG_TEXT = """
project_name: exec-test
run_id: fixed
llm:
  provider: lmstudio
  model: a-model
  temperature: 0.1
  max_tokens: 100
inputs:
  requirements:
    description_pdf: d.pdf
    user_stories_xlsx: s.xlsx
    faq_pdf: f.pdf
  openapi_path: openapi.yaml
  sut_base_url: http://127.0.0.1:8080
execution:
  runner: python_requests
  startup_timeout_seconds: 5
budget:
  max_iterations: 1
  max_tests_per_iteration: 3
  max_llm_calls: 3
output:
  runs_dir: runs
"""


class _RecordingRunner:
    """Stands in for pytest: records the arguments and returns a fixed result."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run(self, suite_dir, *, project_name, base_url, suite_timeout_seconds, artifact_dir):
        self.calls.append({"project": project_name, "base_url": base_url})
        return RunnerResult(
            exit_code=0,
            outcome="completed",
            duration_seconds=1.0,
            cases=[
                CaseExecutionRecord(
                    project_name=project_name, test_name="test_a", outcome="passed"
                )
            ],
        )


def _run_dir_with_suites(tmp_path: Path, *projects: str) -> Path:
    run_dir = tmp_path / "run"
    for project in projects:
        suite = run_dir / "projects" / project / "suite"
        suite.mkdir(parents=True)
        (suite / "generation_report.json").write_text(
            json.dumps(
                {"cases": [{"name": "test_a", "requirement_id": "PT01", "test_type": "happy_path"}]}
            ),
            encoding="utf-8",
        )
    (run_dir / "config.resolved.yaml").write_text(CONFIG_TEXT, encoding="utf-8")
    return run_dir


def _manifest(**projects) -> SutManifest:
    return SutManifest.model_validate({"version": 1, "projects": projects})


def _executor(run_dir: Path, manifest: SutManifest, runner) -> SuiteExecutor:
    return SuiteExecutor(
        run_dir,
        load_config(run_dir / "config.resolved.yaml"),
        manifest,
        runner=runner,
        use_docker=False,
    )


def test_executor_runs_a_runnable_project_at_the_manifest_base_url(tmp_path) -> None:
    """The manifest URL wins: the config's sut_base_url has no path prefix and is wrong."""

    run_dir = _run_dir_with_suites(tmp_path, "team-a")
    manifest = _manifest(
        **{
            "team-a": {
                "status": "runnable",
                "root": "projects/team-a",
                "compose": {"files": ["docker-compose.yml"], "project_name": "exec-a"},
                "api": {"base_url": "http://127.0.0.1:5000/api/v1", "service": "server"},
            }
        }
    )
    runner = _RecordingRunner()

    result = _executor(run_dir, manifest, runner).run()

    assert runner.calls == [{"project": "team-a", "base_url": "http://127.0.0.1:5000/api/v1"}]
    assert result.report.projects["team-a"].outcome == "completed"


def test_executor_records_an_unrunnable_project_without_stopping(tmp_path) -> None:
    """One project that cannot run must cost itself, not the campaign."""

    run_dir = _run_dir_with_suites(tmp_path, "team-a", "team-b")
    manifest = _manifest(
        **{
            "team-a": {
                "status": "unrunnable",
                "blocker": "external_dependency",
                "reason": "needs a cloud database",
            },
            "team-b": {
                "status": "runnable",
                "root": "projects/team-b",
                "compose": {"files": ["docker-compose.yml"], "project_name": "exec-b"},
                "api": {"base_url": "http://127.0.0.1:3000/api", "service": "server"},
            },
        }
    )
    runner = _RecordingRunner()

    result = _executor(run_dir, manifest, runner).run()

    blocked = result.report.projects["team-a"]
    assert blocked.outcome == "not_run"
    assert blocked.blocker == "external_dependency"
    # Its generated tests are still counted, so later denominators stay honest.
    assert [case.outcome for case in blocked.cases] == ["not_run"]
    # And the next project still ran.
    assert result.report.projects["team-b"].outcome == "completed"
    assert [call["project"] for call in runner.calls] == ["team-b"]


def test_executor_refuses_to_start_when_a_project_has_no_manifest_entry(tmp_path) -> None:
    """Discovering this at project fourteen, hours in, is avoidable."""

    run_dir = _run_dir_with_suites(tmp_path, "team-a", "team-forgotten")
    manifest = _manifest(
        **{
            "team-a": {
                "status": "runnable",
                "root": "projects/team-a",
                "compose": {"files": ["docker-compose.yml"], "project_name": "exec-a"},
                "api": {"base_url": "http://127.0.0.1:3000", "service": "server"},
            }
        }
    )

    with pytest.raises(ValueError, match="team-forgotten"):
        _executor(run_dir, manifest, _RecordingRunner()).run()


def test_executor_writes_a_report_per_project_and_for_the_run(tmp_path) -> None:
    run_dir = _run_dir_with_suites(tmp_path, "team-a")
    manifest = _manifest(
        **{
            "team-a": {
                "status": "runnable",
                "root": "projects/team-a",
                "compose": {"files": ["docker-compose.yml"], "project_name": "exec-a"},
                "api": {"base_url": "http://127.0.0.1:3000/api", "service": "server"},
            }
        }
    )

    _executor(run_dir, manifest, _RecordingRunner()).run()

    assert (run_dir / "execution_report.json").is_file()
    assert (run_dir / "execution_summary.md").is_file()
    assert (run_dir / "projects" / "team-a" / "execution" / "report.json").is_file()


def test_manifest_rejects_claiming_original_provenance_for_files_we_supplied() -> None:
    """A project we had to complete must never appear as one that ran as delivered."""

    with pytest.raises(ValueError, match="provenance cannot be 'original'"):
        SutManifest.model_validate(
            {
                "version": 1,
                "projects": {
                    "team-a": {
                        "status": "runnable",
                        "provenance": "original",
                        "root": "projects/team-a",
                        "compose": {
                            "files": ["docker-compose.yml"],
                            "project_name": "exec-a",
                            "materialize_env_files": [
                                {"source": "data/sut_env/team-a.env", "target": ".env"}
                            ],
                        },
                        "api": {"base_url": "http://127.0.0.1:3000", "service": "server"},
                    }
                },
            }
        )


def test_manifest_rejects_a_runnable_project_without_a_recipe() -> None:
    with pytest.raises(ValueError, match="mark it unverified"):
        SutManifest.model_validate(
            {"version": 1, "projects": {"team-a": {"status": "runnable"}}}
        )


def test_runner_accepts_a_suite_path_relative_to_the_current_directory(
    tmp_path, stub_server, monkeypatch
) -> None:
    """pytest runs with the suite as its cwd, so a repo-relative path would not resolve.

    The other runner tests all pass absolute tmp_path values and so never exercised this.
    """

    _write_suite(tmp_path / "run" / "suite", [_case("test_ok", "/reports", [200])])
    monkeypatch.chdir(tmp_path)

    result = PythonRequestsRunner().run(
        Path("run/suite"),
        project_name="stub",
        base_url=stub_server,
        suite_timeout_seconds=120,
        artifact_dir=Path("run/execution"),
    )

    assert [case.outcome for case in result.cases] == ["passed"]
    # The report lands in the artifact directory, not inside the suite.
    assert (tmp_path / "run" / "execution" / "junit.xml").is_file()
    assert not (tmp_path / "run" / "suite" / "run").exists()


# --- state reset -------------------------------------------------------------------


from thesis_rest_tester.execution.docker_compose import (  # noqa: E402
    ComposeError,
    DockerComposeStack,
)
from thesis_rest_tester.execution.manifest import ProjectManifest, ReadinessProbe  # noqa: E402


def _stack(tmp_path: Path, reset_paths: list[str], *, reset_state: bool) -> DockerComposeStack:
    project = ProjectManifest.model_validate(
        {
            "status": "runnable",
            "root": "proj",
            "compose": {"files": ["docker-compose.yml"], "project_name": "exec-x"},
            "api": {"base_url": "http://127.0.0.1:3000", "service": "server"},
            "reset_paths": reset_paths,
        }
    )
    return DockerComposeStack(
        project=project,
        readiness=ReadinessProbe(),
        repository_root=tmp_path,
        reset_state=reset_state,
    )


def test_reset_state_removes_bind_mounted_data_that_down_v_leaves_behind(tmp_path) -> None:
    """`down -v` clears volumes but never bind mounts, where the real state lives."""

    data = tmp_path / "proj" / "db_data"
    data.mkdir(parents=True)
    (data / "postgres.db").write_text("rows", encoding="utf-8")

    _stack(tmp_path, ["db_data"], reset_state=True)._reset_state()

    assert not data.exists()


def test_reset_state_does_nothing_unless_asked(tmp_path) -> None:
    """Deleting inside a student's repository must be deliberate, not a side effect."""

    data = tmp_path / "proj" / "db_data"
    data.mkdir(parents=True)
    (data / "postgres.db").write_text("rows", encoding="utf-8")

    _stack(tmp_path, ["db_data"], reset_state=False)._reset_state()

    assert (data / "postgres.db").is_file()


def test_reset_state_refuses_a_path_that_escapes_the_project(tmp_path) -> None:
    outside = tmp_path / "precious"
    outside.mkdir()
    (tmp_path / "proj").mkdir()

    with pytest.raises(ComposeError, match="does not stay inside"):
        _stack(tmp_path, ["../precious"], reset_state=True)._reset_state()

    assert outside.is_dir()


def test_reset_state_refuses_to_delete_the_project_root(tmp_path) -> None:
    (tmp_path / "proj").mkdir()

    with pytest.raises(ComposeError, match="does not stay inside"):
        _stack(tmp_path, ["."], reset_state=True)._reset_state()

    assert (tmp_path / "proj").is_dir()
