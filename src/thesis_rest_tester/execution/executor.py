"""Run every project's suite against its own containerised service, one at a time."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from thesis_rest_tester.artifacts.writer import ArtifactWriter
from thesis_rest_tester.config import AppConfig, load_config
from thesis_rest_tester.domain.execution import (
    ExecutionReport,
    PhaseTiming,
    ProjectExecutionRecord,
)
from thesis_rest_tester.execution.base import TestRunner
from thesis_rest_tester.execution.docker_compose import (
    ComposeError,
    DockerComposeStack,
    docker_is_available,
)
from thesis_rest_tester.execution.junit import all_not_run, load_generated_cases
from thesis_rest_tester.execution.manifest import ProjectManifest, SutManifest, load_manifest
from thesis_rest_tester.execution.python_requests_runner import PythonRequestsRunner

_logger = logging.getLogger(__name__)

DEFAULT_MANIFEST = Path("data/sut_manifest.yaml")


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    run_dir: Path
    report: ExecutionReport

    @property
    def totals(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for project in self.report.projects.values():
            for outcome, count in project.counts.items():
                tally[outcome] = tally.get(outcome, 0) + count
        return tally


class SuiteExecutor:
    """Execute the generated suites of a run, project by project.

    Mirrors ``generation.SuiteGenerator`` in shape, including its central discipline: a
    project that cannot be started is recorded and the loop continues. Eighteen rows with
    some honestly marked ``not_run`` is a result; aborting at project eight is not.
    """

    def __init__(
        self,
        run_dir: str | Path,
        config: AppConfig,
        manifest: SutManifest,
        *,
        runner: TestRunner | None = None,
        repository_root: Path | None = None,
        use_docker: bool = True,
        skip_completed: bool = False,
        reset_state: bool = False,
        attempt_unverified: bool = False,
    ) -> None:
        self._run_dir = Path(run_dir)
        self._config = config
        self._manifest = manifest
        self._runner = runner or PythonRequestsRunner()
        self._repository_root = repository_root or Path(__file__).resolve().parents[3]
        self._use_docker = use_docker
        self._skip_completed = skip_completed
        self._reset_state = reset_state
        # `unverified` means "we wrote a recipe but the project has never answered its
        # probe". Attempting one is how it earns promotion to `runnable`, so the campaign
        # needs a way to try it without first asserting in the manifest something that is
        # not yet true.
        self._attempt_unverified = attempt_unverified

    def run(self, *, only: list[str] | None = None) -> ExecutionResult:
        projects_dir = self._run_dir / "projects"
        if not projects_dir.is_dir():
            raise FileNotFoundError(f"No projects directory inside {self._run_dir}")

        names = sorted(
            path.name
            for path in projects_dir.iterdir()
            if path.is_dir() and (path / "suite").is_dir()
        )
        if only:
            names = [name for name in names if name in only]

        # Fail before anything starts rather than at project fourteen, hours in.
        missing = self._manifest.missing_for(names)
        if missing:
            raise ValueError(
                "these projects have generated suites but no manifest entry: "
                + ", ".join(missing)
            )
        if self._use_docker:
            available, detail = docker_is_available()
            if not available:
                raise RuntimeError(detail)
            _logger.info("%s", detail)

        report = ExecutionReport(
            run_id=self._run_dir.name,
            manifest_path=str(DEFAULT_MANIFEST),
            started_at=datetime.now(UTC),
            # A partial run updates the roll-up rather than replacing it. Retrying the
            # handful of projects whose start-up recipe has just been corrected is the
            # normal way this campaign proceeds, and rebuilding the report from only
            # those would drop every other project from it -- leaving the run looking as
            # though the rest had never been executed, and giving `evaluate` a run of
            # seven projects to score instead of eighteen.
            projects=self._previously_recorded(names),
        )
        for name in names:
            record = self._execute_project(name, projects_dir / name)
            report.projects[name] = record
            _logger.info("%s: %s %s", name, record.outcome, record.counts)
        report.finished_at = datetime.now(UTC)

        self._write_run_artifacts(report)
        return ExecutionResult(self._run_dir, report)

    # --- one project ----------------------------------------------------------------

    def _execute_project(self, name: str, project_dir: Path) -> ProjectExecutionRecord:
        suite_dir = project_dir / "suite"
        artifact_dir = project_dir / "execution"
        generated = load_generated_cases(suite_dir / "generation_report.json")
        entry = self._manifest.projects[name]

        if self._skip_completed and (artifact_dir / "report.json").is_file():
            _logger.info("%s already has an execution report; skipping", name)
            return ProjectExecutionRecord.model_validate_json(
                (artifact_dir / "report.json").read_text(encoding="utf-8")
            )

        attemptable = entry.status == "runnable" or (
            self._attempt_unverified and entry.status == "unverified" and entry.has_recipe
        )
        if not attemptable:
            return self._not_run(name, entry, generated, artifact_dir)

        assert entry.api is not None
        if not self._use_docker:
            return self._run_suite(name, entry, suite_dir, artifact_dir, stack=None)

        # Bound before the `with`, not by it: when __enter__ raises, the `as` target is
        # never assigned, and the phase timings and captured container logs -- the only
        # evidence of why the project would not come up -- would be lost with it.
        stack = DockerComposeStack(
            project=entry,
            readiness=self._manifest.readiness_for(name),
            repository_root=self._repository_root,
            build_timeout_seconds=self._config.execution.build_timeout_seconds,
            startup_timeout_seconds=self._config.execution.startup_timeout_seconds,
            teardown_volumes=self._config.execution.teardown_volumes,
            reset_state=self._reset_state,
        )
        try:
            with stack:
                record = self._run_suite(name, entry, suite_dir, artifact_dir, stack=stack)
        except ComposeError as exc:
            _logger.warning("%s could not be started: %s", name, exc)
            if stack.logs:
                ArtifactWriter(artifact_dir).write_text("compose_logs.txt", stack.logs)
            record = ProjectExecutionRecord(
                project_name=name,
                outcome=(
                    "image_unavailable"
                    if exc.blocker == "image_unavailable"
                    else "startup_failed"
                ),
                provenance=entry.provenance,
                provenance_notes=entry.provenance_notes,
                blocker=exc.blocker,
                reason=str(exc),
                base_url=entry.api.base_url,
                compose_files=_compose_files(entry),
                phases=_phase_timings(stack),
                cases=all_not_run(name, generated, str(exc)),
            )
        self._write_project_artifacts(artifact_dir, record)
        return record

    def _run_suite(
        self,
        name: str,
        entry: ProjectManifest,
        suite_dir: Path,
        artifact_dir: Path,
        *,
        stack: DockerComposeStack | None,
    ) -> ProjectExecutionRecord:
        assert entry.api is not None
        base_url = entry.api.base_url
        result = self._runner.run(
            suite_dir,
            project_name=name,
            base_url=base_url,
            suite_timeout_seconds=self._config.execution.suite_timeout_seconds,
            artifact_dir=artifact_dir,
        )
        phases = _phase_timings(stack)
        phases.append(
            PhaseTiming(name="suite", duration_seconds=result.duration_seconds, ok=True)
        )
        if stack is not None and stack.logs:
            ArtifactWriter(artifact_dir).write_text("compose_logs.txt", stack.logs)
        if result.exchanges:
            ArtifactWriter(artifact_dir).write_json(
                "http_exchanges.json",
                [exchange.model_dump(mode="json") for exchange in result.exchanges],
            )

        record = ProjectExecutionRecord(
            project_name=name,
            outcome=result.outcome,
            provenance=entry.provenance,
            provenance_notes=entry.provenance_notes,
            base_url=base_url,
            base_url_plausible=self._base_url_is_plausible(result, suite_dir),
            compose_files=_compose_files(entry),
            phases=phases,
            pytest_exit_code=result.exit_code,
            cases=result.cases,
            exchanges_recorded=len(result.exchanges),
        )
        self._write_project_artifacts(artifact_dir, record)
        return record

    def _not_run(
        self,
        name: str,
        entry: ProjectManifest,
        generated: dict[str, dict[str, str | None]],
        artifact_dir: Path,
    ) -> ProjectExecutionRecord:
        reason = entry.reason or f"the manifest marks this project {entry.status}"
        record = ProjectExecutionRecord(
            project_name=name,
            outcome="not_run",
            provenance=entry.provenance,
            provenance_notes=entry.provenance_notes,
            blocker=entry.blocker,
            reason=reason,
            cases=all_not_run(name, generated, reason),
        )
        self._write_project_artifacts(artifact_dir, record)
        return record

    @staticmethod
    def _base_url_is_plausible(result, suite_dir: Path) -> bool | None:
        """Did anything the suite called actually exist?

        Every documented path answering 404 almost always means the base URL is missing
        the application's path prefix, not that the service is broken. Catching it here
        turns a silently wasted campaign into a visible flag on the report.
        """

        statuses = [
            exchange.status_code for exchange in result.exchanges if exchange.status_code
        ]
        if not statuses:
            return None
        del suite_dir
        return any(status != 404 for status in statuses)

    # --- artifacts --------------------------------------------------------------------

    @staticmethod
    def _write_project_artifacts(artifact_dir: Path, record: ProjectExecutionRecord) -> None:
        ArtifactWriter(artifact_dir).write_json("report.json", record)

    def _previously_recorded(
        self, running_now: list[str]
    ) -> dict[str, ProjectExecutionRecord]:
        """Records from an earlier execution of this run, minus what is about to re-run.

        Read from the run-level report rather than from each project's own, so that a
        project deliberately removed from the roll-up stays removed.
        """

        path = self._run_dir / "execution_report.json"
        if not path.is_file():
            return {}
        try:
            previous = ExecutionReport.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError:
            _logger.warning("Could not read the previous %s; starting a fresh report", path.name)
            return {}
        carried = {
            name: record
            for name, record in previous.projects.items()
            if name not in set(running_now)
        }
        if carried:
            _logger.info("Carrying forward %d project(s) from the previous execution", len(carried))
        return carried

    def _write_run_artifacts(self, report: ExecutionReport) -> None:
        writer = ArtifactWriter(self._run_dir)
        writer.write_json("execution_report.json", report)
        writer.write_text("execution_summary.md", _summary_markdown(report))


def _phase_timings(stack: DockerComposeStack | None) -> list[PhaseTiming]:
    """Lift the stack's own phase log into the persisted record."""

    return [
        PhaseTiming(
            name=phase.name,
            duration_seconds=phase.duration_seconds,
            ok=phase.ok,
            detail=phase.detail,
        )
        for phase in (stack.phases if stack else [])
        if phase.name in {"pull", "build", "up", "ready", "down"}
    ]


def _compose_files(entry: ProjectManifest) -> list[str]:
    return [str(path) for path in entry.compose.files] if entry.compose else []


def _summary_markdown(report: ExecutionReport) -> str:
    lines = [
        f"# Execution: {report.run_id}",
        "",
        f"- Started: `{report.started_at.isoformat()}`",
        f"- Projects: {len(report.projects)}",
        "",
        "| Project | Outcome | Provenance | passed | failed | error | not run | base URL |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for name, project in report.projects.items():
        counts = project.counts
        lines.append(
            f"| {name} | {project.outcome} | {project.provenance} "
            f"| {counts.get('passed', 0)} | {counts.get('failed', 0)} "
            f"| {counts.get('error', 0)} | {counts.get('not_run', 0)} "
            f"| {project.base_url or '—'} |"
        )
    suspicious = [
        name
        for name, project in report.projects.items()
        if project.base_url_plausible is False
    ]
    if suspicious:
        lines += [
            "",
            "## Base URL warning",
            "",
            "Every request these projects made returned 404, which usually means the "
            "configured base URL is missing the application's path prefix rather than "
            "that the service is broken: " + ", ".join(suspicious),
        ]
    lines.append("")
    return "\n".join(lines)


def execute_suites(
    run_dir: str | Path,
    *,
    config_path: str | Path | None = None,
    manifest_path: str | Path | None = None,
    projects: list[str] | None = None,
    use_docker: bool = True,
    skip_completed: bool = False,
    reset_state: bool = False,
    attempt_unverified: bool = False,
) -> ExecutionResult:
    """Entry point used by the CLI."""

    run_path = Path(run_dir)
    # The resolved config the orchestrator wrote is the one that produced these suites;
    # asking the user to re-supply one risks executing against a config that has drifted.
    resolved = Path(config_path) if config_path else run_path / "config.resolved.yaml"
    config = load_config(resolved)
    manifest = load_manifest(manifest_path or DEFAULT_MANIFEST)
    executor = SuiteExecutor(
        run_path,
        config,
        manifest,
        use_docker=use_docker,
        skip_completed=skip_completed,
        reset_state=reset_state,
        attempt_unverified=attempt_unverified,
    )
    return executor.run(only=projects)

