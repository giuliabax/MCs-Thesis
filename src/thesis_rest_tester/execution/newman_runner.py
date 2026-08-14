"""Placeholder for Postman/Newman collection execution."""

from pathlib import Path

from thesis_rest_tester.execution.base import RunnerResult, TestRunner


class NewmanRunner(TestRunner):
    def run(
        self,
        test_suite_path: Path,
        *,
        project_name: str,
        base_url: str,
        suite_timeout_seconds: int,
        artifact_dir: Path,
    ) -> RunnerResult:
        del test_suite_path, project_name, base_url, suite_timeout_seconds, artifact_dir
        raise NotImplementedError("Newman test execution is not implemented yet")

