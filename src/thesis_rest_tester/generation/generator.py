"""Turn the workflow plans of a completed run into one pytest suite per project."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from thesis_rest_tester.agents.test_writer import TestWriterAgent
from thesis_rest_tester.artifacts.writer import ArtifactWriter
from thesis_rest_tester.config import AppConfig, load_config
from thesis_rest_tester.domain.compact import body_field_spec
from thesis_rest_tester.domain.executable import ExecutableTestCase
from thesis_rest_tester.domain.models import (
    OpenAPIOperation,
    TestStrategyItem,
    WorkflowPlan,
)
from thesis_rest_tester.evaluation.evaluator import strategy_item_key
from thesis_rest_tester.generation.renderer import render_conftest, render_suite
from thesis_rest_tester.llm import LMStudioLLMClient, MockLLMClient
from thesis_rest_tester.llm.base import LLMClient
from thesis_rest_tester.llm.usage import UsageRecorder

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProjectSuite:
    project_name: str
    suite_dir: Path
    cases: list[ExecutableTestCase]
    # Strategy items that produced no test, with the reason. A partial suite is a
    # reportable outcome, not a failure: one unusable item must not cost the project.
    skipped: list[tuple[str, str]] = field(default_factory=list)

    @property
    def requested(self) -> int:
        return len(self.cases) + len(self.skipped)


@dataclass(frozen=True, slots=True)
class GenerationResult:
    run_dir: Path
    suites: list[ProjectSuite]

    @property
    def total_cases(self) -> int:
        return sum(len(suite.cases) for suite in self.suites)

    @property
    def total_skipped(self) -> int:
        return sum(len(suite.skipped) for suite in self.suites)


class SuiteGenerator:
    """Generate executable suites from the artifacts of a completed planning run."""

    def __init__(
        self,
        run_dir: str | Path,
        llm_client: LLMClient,
        config: AppConfig,
        *,
        prompt_root: str | Path | None = None,
        regenerate: dict[str, list[str]] | None = None,
        corrections: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self._run_dir = Path(run_dir)
        self._llm_client = llm_client
        self._config = config
        # Per project, the strategy items to write again; every other test in that
        # project's suite is carried over from the previous iteration untouched.
        #
        # Rewriting a whole suite to repair three tests would be worse than wasteful: it
        # would churn the tests that already worked, so any change in the totals could be
        # attributed to the reshuffle as easily as to the feedback, and the comparison
        # between iterations -- the thing the loop exists to produce -- would say nothing.
        self._regenerate = regenerate or {}
        # Per project, per item, the note the Feedback Manager wrote for it.
        self._corrections = corrections or {}
        repository_root = Path(__file__).resolve().parents[3]
        self._prompt_root = (
            Path(prompt_root) if prompt_root is not None else repository_root / "prompts"
        )

    def run(self, *, only: list[str] | None = None) -> GenerationResult:
        projects_dir = self._run_dir / "projects"
        if not projects_dir.is_dir():
            raise FileNotFoundError(f"No projects directory inside {self._run_dir}")

        suites: list[ProjectSuite] = []
        for project_dir in sorted(projects_dir.iterdir()):
            if not project_dir.is_dir():
                continue
            if only and project_dir.name not in only:
                continue
            plan_file = project_dir / "workflow_plan.json"
            if not plan_file.is_file():
                _logger.warning("%s has no workflow_plan.json; skipping", project_dir.name)
                continue
            suites.append(self._generate_project(project_dir, plan_file))
        return GenerationResult(self._run_dir, suites)

    def _generate_project(self, project_dir: Path, plan_file: Path) -> ProjectSuite:
        plan = WorkflowPlan.model_validate_json(plan_file.read_text(encoding="utf-8"))
        operations = _load_operations(project_dir)
        suite_dir = project_dir / "suite"
        writer = ArtifactWriter(suite_dir / "agent")

        agent = TestWriterAgent(
            llm_client=self._llm_client,
            prompt_path=self._prompt_root / "generation/test_writer_agent.md",
            artifact_writer=writer,
            temperature=self._config.llm.temperature,
            max_tokens=self._config.llm.max_tokens_for("test_writer"),
            think="test_writer" in self._config.llm.reasoning_agents,
        )

        wanted = set(self._regenerate.get(plan.project_name, []))
        notes = self._corrections.get(plan.project_name, {})
        previous = _previous_cases(suite_dir) if wanted else {}
        if wanted:
            # A targeted regeneration that can carry nothing forward is not targeted; it
            # is a destructive rewrite wearing the same name. This happens for real: a
            # suite generated before the report recorded which item produced each test has
            # no join key at all, and an earlier version of this code silently emitted an
            # empty suite for such a project -- 54 planned items, 0 tests, and nothing in
            # the skip list to say so. Refusing is the only safe answer, because the
            # alternative looks exactly like a very bad iteration.
            if not previous:
                raise RuntimeError(
                    f"{plan.project_name}: asked to regenerate {len(wanted)} item(s), but "
                    f"its existing suite records no strategy_item keys, so nothing can be "
                    f"carried over. Regenerate this project in full instead."
                )
            _logger.info(
                "Regenerating %d of %d test(s) for %s; the rest are carried over",
                len(wanted),
                len(plan.strategy_items),
                plan.project_name,
            )
        else:
            _logger.info(
                "Generating %d test(s) for %s", len(plan.strategy_items), plan.project_name
            )

        cases: list[ExecutableTestCase] = []
        skipped: list[tuple[str, str]] = []
        item_keys: dict[str, str] = {}
        used_names: set[str] = set()

        for index, item in enumerate(plan.strategy_items, start=1):
            key = strategy_item_key(
                item.requirement_id, item.http_method, item.api_endpoint, item.test_type
            )
            # Carry over what already has a test, regenerate what was asked about, and
            # write what is new. The third case is not hypothetical: replanning a project
            # introduces items that never had a test, and treating them like the first
            # case -- "not asked about, so leave it alone" -- silently drops them, which is
            # how an iteration once turned 375 tests into 201.
            if wanted and key not in wanted:
                carried = previous.get(key)
                if carried is not None:
                    carried = _deduplicate_name(carried, used_names)
                    used_names.add(carried.name)
                    item_keys[carried.name] = key
                    cases.append(carried)
                    continue
                _logger.info(
                    "%s: %s is new since the last generation; writing it", plan.project_name, key
                )

            stem = f"item{index:02d}_{item.requirement_id}"
            try:
                case, _ = agent.run(
                    item, operations, artifact_stem=stem, correction=notes.get(key)
                )
            except Exception as exc:  # noqa: BLE001 - one item must not fail the project
                reason = f"{type(exc).__name__}: {exc}"
                _logger.warning(
                    "%s item %d (%s) produced no test: %s",
                    plan.project_name,
                    index,
                    item.requirement_id,
                    reason,
                )
                skipped.append((f"{item.requirement_id} {item.http_method} {item.api_endpoint}",
                                reason))
                continue
            case = _deduplicate_name(case, used_names)
            used_names.add(case.name)
            item_keys[case.name] = key
            cases.append(case)

        _write_suite(suite_dir, plan, cases, skipped, self._config, item_keys)
        return ProjectSuite(plan.project_name, suite_dir, cases, skipped)


def generate_suites(
    config_path: str | Path,
    run_dir: str | Path,
    *,
    projects: list[str] | None = None,
    dry_run: bool = False,
    llm_client: LLMClient | None = None,
) -> GenerationResult:
    """Entry point used by the CLI: build the client, then generate every suite."""

    config = load_config(config_path)
    client = llm_client or _build_client(config, Path(run_dir), projects, dry_run=dry_run)
    recorder = UsageRecorder(client)
    result = SuiteGenerator(run_dir, recorder, config).run(only=projects)
    # Written per stage rather than per run: a later stage would otherwise overwrite it,
    # and RQ3 asks what each part of the pipeline costs, not only the total.
    ArtifactWriter(Path(run_dir)).write_json("usage.generation.json", recorder.report())
    return result


def _build_client(
    config: AppConfig,
    run_dir: Path,
    projects: list[str] | None,
    *,
    dry_run: bool,
) -> LLMClient:
    if dry_run:
        return MockLLMClient(_mock_responses(run_dir, projects))
    override = config.llm.overrides.get("test_writer")
    model = override.model if override is not None and override.reroutes else config.llm.model
    return LMStudioLLMClient(
        model=model,
        base_url=config.llm.base_url,
        default_temperature=config.llm.temperature,
        default_max_tokens=config.llm.max_tokens_for("test_writer"),
        timeout=config.llm.timeout_seconds,
    )


def _mock_responses(run_dir: Path, projects: list[str] | None) -> list[str]:
    """One deterministic test case per strategy item, in the order the generator asks.

    The fixture exercises the whole path---schema validation, contract reconciliation,
    rendering---without a model call, which is what makes the generator testable.
    """

    responses: list[str] = []
    projects_dir = run_dir / "projects"
    if not projects_dir.is_dir():
        return responses
    for project_dir in sorted(projects_dir.iterdir()):
        if not project_dir.is_dir() or (projects and project_dir.name not in projects):
            continue
        plan_file = project_dir / "workflow_plan.json"
        if not plan_file.is_file():
            continue
        plan = WorkflowPlan.model_validate_json(plan_file.read_text(encoding="utf-8"))
        operations = {
            (operation.method, operation.path): operation
            for operation in _load_operations(project_dir)
        }
        for index, item in enumerate(plan.strategy_items, start=1):
            operation = operations.get((item.http_method, item.api_endpoint))
            responses.append(json.dumps(_mock_case(index, item, operation), ensure_ascii=False))
    return responses


def _mock_case(index: int, item: TestStrategyItem, operation: OpenAPIOperation | None) -> dict:
    body: dict[str, object] | None = None
    spec = body_field_spec(operation.request_body_schema) if operation else None
    if spec is not None:
        types: dict[str, str] = spec["properties"]  # type: ignore[assignment]
        body = {name: _mock_value(types.get(name, "string")) for name in types}
    status = [int(code) for code in item.expected_status_codes if code.isdigit()] or [200]
    return {
        "name": f"test_{item.requirement_id.lower()}_{index:02d}",
        "requirement_id": item.requirement_id,
        "test_type": item.test_type,
        "description": f"Dry-run fixture for {item.requirement_id}.",
        "setup": [],
        "steps": [
            {
                "description": item.prompt[:120],
                "request": {
                    "method": item.http_method,
                    "path": item.api_endpoint,
                    "json_body": body,
                },
                "expect_status": status,
                "captures": [],
                "assertions": [],
            }
        ],
        "cleanup": [],
    }


def _mock_value(declared: str) -> object:
    return {
        "integer": 1,
        "number": 1,
        "boolean": True,
        "array": [],
        "object": {},
    }.get(declared, "value_{{unique}}")


def _deduplicate_name(case: ExecutableTestCase, used: set[str]) -> ExecutableTestCase:
    """Two items on the same requirement can yield the same name; pytest needs unique ones."""

    if case.name not in used:
        return case
    suffix = 2
    while f"{case.name}_{suffix}" in used:
        suffix += 1
    return case.model_copy(update={"name": f"{case.name}_{suffix}"})


def _load_operations(project_dir: Path) -> list[OpenAPIOperation]:
    operations_file = project_dir / "openapi_operations.json"
    if not operations_file.is_file():
        raise FileNotFoundError(f"No openapi_operations.json in {project_dir}")
    raw = json.loads(operations_file.read_text(encoding="utf-8"))
    return [OpenAPIOperation.model_validate(entry) for entry in raw]


def _write_suite(
    suite_dir: Path,
    plan: WorkflowPlan,
    cases: list[ExecutableTestCase],
    skipped: list[tuple[str, str]],
    config: AppConfig,
    item_keys: dict[str, str] | None = None,
) -> None:
    """Write the suite and the report that records how it was produced.

    ``item_keys`` maps a test's name to the strategy item that produced it, and exists so
    that a later iteration can regenerate named tests and leave the rest untouched. The
    join cannot be reconstructed afterwards: a case records its requirement and test type
    but not its endpoint, and two items may share that pair.
    """

    writer = ArtifactWriter(suite_dir)
    base_url = plan.sut_base_url or "http://localhost:8080"
    writer.write_text(
        "conftest.py", render_conftest(base_url, config.execution.timeout_seconds)
    )
    module_name = f"test_{plan.project_name.replace('-', '_')}.py"
    writer.write_text(module_name, render_suite(plan.project_name, cases))
    # Pins the suite as its own pytest rootdir. Without it, pytest walks up to the
    # repository's own pytest.ini, whose testpaths and pythonpath have nothing to do with
    # a generated suite -- and which only happens to be harmless while the run directory
    # sits inside the checkout.
    writer.write_text("pytest.ini", "[pytest]\n")
    writer.write_json(
        "generation_report.json",
        {
            "project_name": plan.project_name,
            "run_id": plan.run_id,
            "sut_base_url": base_url,
            "strategy_items": len(plan.strategy_items),
            "tests_generated": len(cases),
            "tests_skipped": len(skipped),
            "skipped": [{"item": item, "reason": reason} for item, reason in skipped],
            "cases": [
                {
                    **case.model_dump(mode="json"),
                    "strategy_item": (item_keys or {}).get(case.name),
                }
                for case in cases
            ],
        },
    )


def _previous_cases(suite_dir: Path) -> dict[str, ExecutableTestCase]:
    """The tests an earlier iteration produced, keyed by the strategy item behind each.

    Only cases whose report records a `strategy_item` can be carried over. A suite written
    before that key existed has none, and such a case is dropped rather than guessed at:
    matching on requirement and test type alone would silently pair a repaired item with
    a stale test, which is worse than regenerating it.
    """

    report = suite_dir / "generation_report.json"
    if not report.is_file():
        return {}
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        _logger.warning("Could not read %s; regenerating this project in full", report)
        return {}

    carried: dict[str, ExecutableTestCase] = {}
    for entry in data.get("cases", []):
        key = entry.get("strategy_item") if isinstance(entry, dict) else None
        if not key:
            continue
        payload = {name: value for name, value in entry.items() if name != "strategy_item"}
        try:
            carried[str(key)] = ExecutableTestCase.model_validate(payload)
        except ValueError:
            _logger.debug("A previously generated case in %s no longer validates", report)
    return carried
