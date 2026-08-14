"""Execute a generated pytest suite against a running SUT, from the host."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from thesis_rest_tester.domain.execution import HttpExchangeRecord
from thesis_rest_tester.execution.base import RunnerResult, TestRunner
from thesis_rest_tester.execution.junit import (
    all_not_run,
    join_cases,
    load_generated_cases,
    outcome_for_exit_code,
    parse_junit,
    report_is_usable,
)

_logger = logging.getLogger(__name__)

# Pins the suite as its own rootdir. Without it pytest walks up to the repository's
# pytest.ini, whose `testpaths = tests` and `pythonpath = src` would apply to a run that
# has nothing to do with this repository's own tests -- and which only happens to work
# today because the run directory sits inside the checkout.
_SUITE_PYTEST_INI = "[pytest]\n"


class PythonRequestsRunner(TestRunner):
    """Run one suite in a subprocess and turn its report into records.

    A subprocess rather than an in-process pytest call, for two reasons. Every suite
    defines a module named ``conftest``, so two suites imported into one interpreter
    would collide in ``sys.modules`` and silently contaminate each other. And a suite is
    generated code executing against an unpredictable service: a crash or a hang must
    cost this project, not the campaign.
    """

    def run(
        self,
        test_suite_path: Path,
        *,
        project_name: str,
        base_url: str,
        suite_timeout_seconds: int,
        artifact_dir: Path,
    ) -> RunnerResult:
        # Both paths are resolved because pytest runs with the suite directory as its
        # working directory: a path relative to the repository root would no longer
        # resolve there, and the JUnit report would be written somewhere inside the suite.
        suite_dir = Path(test_suite_path).resolve()
        artifact_dir = Path(artifact_dir).resolve()
        artifact_dir.mkdir(parents=True, exist_ok=True)
        junit_path = artifact_dir / "junit.xml"
        http_log = artifact_dir / "http_exchanges.jsonl"
        for stale in (junit_path, http_log):
            stale.unlink(missing_ok=True)

        ini = suite_dir / "pytest.ini"
        if not ini.exists():
            ini.write_text(_SUITE_PYTEST_INI, encoding="utf-8")

        command = [
            sys.executable,
            "-m",
            "pytest",
            str(suite_dir),
            "-p",
            "no:cacheprovider",
            "-p",
            "thesis_rest_tester.execution.http_record_plugin",
            f"--junit-xml={junit_path}",
            "-q",
        ]
        started = time.perf_counter()
        completed, timed_out = self._execute(
            command, suite_dir, base_url, http_log, suite_timeout_seconds
        )
        duration = time.perf_counter() - started

        stdout = completed.stdout if completed else ""
        stderr = completed.stderr if completed else ""
        (artifact_dir / "pytest_stdout.txt").write_text(stdout + stderr, encoding="utf-8")

        generated = load_generated_cases(suite_dir / "generation_report.json")
        exchanges = _read_exchanges(http_log, project_name)

        if timed_out:
            _logger.warning(
                "%s: suite exceeded %ds; no report is written until the session ends, so "
                "no case outcome survives",
                project_name,
                suite_timeout_seconds,
            )
            return RunnerResult(
                exit_code=-1,
                outcome="suite_timeout",
                duration_seconds=duration,
                stdout=stdout,
                stderr=stderr,
                cases=all_not_run(
                    project_name,
                    generated,
                    f"suite exceeded {suite_timeout_seconds}s and was terminated",
                ),
                exchanges=exchanges,
            )

        exit_code = completed.returncode if completed else -1
        outcome = outcome_for_exit_code(exit_code)
        if not report_is_usable(exit_code) or not junit_path.is_file():
            return RunnerResult(
                exit_code=exit_code,
                outcome=outcome,
                duration_seconds=duration,
                stdout=stdout,
                stderr=stderr,
                cases=all_not_run(project_name, generated, _explain(exit_code)),
                exchanges=exchanges,
            )

        cases = join_cases(
            project_name, generated, parse_junit(junit_path.read_text(encoding="utf-8"))
        )
        return RunnerResult(
            exit_code=exit_code,
            outcome=outcome,
            duration_seconds=duration,
            stdout=stdout,
            stderr=stderr,
            cases=cases,
            exchanges=exchanges,
            metadata={"junit_path": str(junit_path), "http_log": str(http_log)},
        )

    @staticmethod
    def _execute(
        command: list[str],
        suite_dir: Path,
        base_url: str,
        http_log: Path,
        timeout_seconds: int,
    ) -> tuple[subprocess.CompletedProcess[str] | None, bool]:
        environment = dict(os.environ)
        environment["SUT_BASE_URL"] = base_url
        environment["THESIS_HTTP_LOG"] = str(http_log)
        # The plugin is imported by name, so the package must be importable even though
        # pytest runs with the suite directory as its rootdir.
        source_root = Path(__file__).resolve().parents[2]
        existing = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            f"{source_root}{os.pathsep}{existing}" if existing else str(source_root)
        )
        # Docker, npm and pytest all emit UTF-8; decoding with the Windows ANSI codepage
        # raises mid-stream on the first box-drawing character.
        environment["PYTHONUTF8"] = "1"
        environment["PYTHONIOENCODING"] = "utf-8"

        try:
            return (
                subprocess.run(
                    command,
                    cwd=suite_dir,
                    env=environment,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout_seconds,
                    check=False,
                ),
                False,
            )
        except subprocess.TimeoutExpired:
            return None, True


def _explain(exit_code: int) -> str:
    return {
        2: "run was interrupted",
        3: "pytest reported an internal error",
        4: "pytest usage error: the suite could not be collected",
        5: "no tests were collected",
    }.get(exit_code, f"pytest exited with code {exit_code}")


def _read_exchanges(http_log: Path, project_name: str) -> list[HttpExchangeRecord]:
    if not http_log.is_file():
        return []
    records: list[HttpExchangeRecord] = []
    for line in http_log.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            # A partial final line is possible if the process was killed mid-write.
            continue
        records.append(
            HttpExchangeRecord(
                project_name=project_name,
                test_name=payload.get("test_name"),
                method=payload.get("method", ""),
                path=payload.get("path", ""),
                status_code=payload.get("status_code"),
                duration_seconds=payload.get("duration_seconds"),
                error=payload.get("error"),
            )
        )
    return records
