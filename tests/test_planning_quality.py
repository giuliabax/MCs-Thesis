from __future__ import annotations

import json
from pathlib import Path

from thesis_rest_tester.agents.api_understanding import APIUnderstandingAgent
from thesis_rest_tester.agents.test_strategy_planner import (
    TestStrategyPlannerAgent as StrategyPlannerAgent,
)
from thesis_rest_tester.artifacts.writer import ArtifactWriter
from thesis_rest_tester.config import BudgetConfig
from thesis_rest_tester.domain.coverage import (
    OperationReference,
    ProjectRequirementCoverage,
    RequirementAPIMatch,
)

# TestStrategyItem is aliased: pytest would otherwise try to collect the Test-prefixed
# model as a test class.
from thesis_rest_tester.domain.models import (
    OpenAPIOperation,
    RequirementItem,
)
from thesis_rest_tester.domain.models import (
    TestStrategyItem as StrategyItem,
)
from thesis_rest_tester.domain.schemas import (
    APIAnalysis,
    APIDependency,
    APIOperationAnalysis,
    RequirementsAnalysis,
)
from thesis_rest_tester.llm.base import MockLLMClient


def _prompt(tmp_path: Path) -> Path:
    path = tmp_path / "prompt.md"
    path.write_text("Return JSON.", encoding="utf-8")
    return path


def _empty_coverage() -> ProjectRequirementCoverage:
    return ProjectRequirementCoverage.from_matches(
        project_name="test-project", openapi_path="openapi.yaml", matches=[]
    )


def test_api_agent_preserves_operations_and_infers_resource_dependencies(tmp_path: Path) -> None:
    operations = [
        OpenAPIOperation(method="POST", path="/reports", response_codes=["201"]),
        OpenAPIOperation(
            method="GET",
            path="/reports/{reportId}",
            parameters=[{"name": "reportId", "in": "path"}],
            response_codes=["200", "404"],
            auth_required=True,
        ),
    ]
    response = {
        "summary": "Reports API",
        "operations": [
            {
                "path": operation.path,
                "method": operation.method,
                "operation_id": None,
                "auth_required": operation.auth_required,
                "dependencies": [],
                "notes": [],
            }
            for operation in operations
        ],
        "authentication_notes": [],
        "dependencies": [],
        "dependency_edges": [],
        "risks": [],
    }
    agent = APIUnderstandingAgent(
        llm_client=MockLLMClient([json.dumps(response)]),
        prompt_path=_prompt(tmp_path),
        artifact_writer=ArtifactWriter(tmp_path / "run"),
    )

    analysis, _ = agent.run(operations)

    assert [(item.method, item.path) for item in analysis.operations] == [
        ("POST", "/reports"),
        ("GET", "/reports/{reportId}"),
    ]
    assert len(analysis.dependency_edges) == 1
    assert analysis.dependency_edges[0].prerequisite_path == "/reports"
    assert analysis.operations[1].dependencies


def test_strategy_agent_retries_when_semantic_quality_is_insufficient(tmp_path: Path) -> None:
    requirements = RequirementsAnalysis(
        summary="Requirements",
        requirements=[
            RequirementItem(id=f"R{index}", source="test", text=f"Requirement {index}", role="user")
            for index in range(1, 5)
        ],
    )
    operations = [
        OpenAPIOperation(method="GET", path="/public", response_codes=["200"]),
        OpenAPIOperation(method="GET", path="/other", response_codes=["200", "400"]),
        OpenAPIOperation(method="POST", path="/auth/login", response_codes=["200", "401"]),
        OpenAPIOperation(
            method="GET",
            path="/protected",
            response_codes=["200", "401"],
            auth_required=True,
        ),
    ]
    api_analysis = APIAnalysis(
        summary="API",
        operations=[
            APIOperationAnalysis(
                path=operation.path,
                method=operation.method,
                auth_required=operation.auth_required,
            )
            for operation in operations
        ],
        dependency_edges=[
            APIDependency(
                prerequisite_method="POST",
                prerequisite_path="/auth/login",
                dependent_method="GET",
                dependent_path="/protected",
                dependency_type="authentication",
                reason="An authenticated session is required.",
            )
        ],
    )
    insufficient = [
        {
            "requirement_id": f"R{index}",
            "requirement_summary": f"Requirement {index}",
            "api_endpoint": operation.path,
            "http_method": operation.method,
            "prompt": "Test it.",
            "test_type": "happy_path",
            "priority": "high",
            "auth_role": None,
            "setup_needed": [],
            "cleanup_strategy": None,
            "expected_status_codes": ["200"],
            "rationale": "Baseline",
        }
        for index, operation in enumerate(operations[:3], start=1)
    ]
    corrected_types = ["happy_path", "edge_case", "negative", "stateful"]
    corrected = []
    operation_types = zip(operations, corrected_types, strict=True)
    for index, (operation, test_type) in enumerate(operation_types, 1):
        corrected.append(
            {
                "requirement_id": f"R{index}",
                "requirement_summary": f"Requirement {index}",
                "api_endpoint": operation.path,
                "http_method": operation.method,
                "prompt": f"Generate a {test_type} test.",
                "test_type": test_type,
                "priority": "high" if index < 3 else "medium",
                "auth_role": "user" if operation.auth_required else None,
                "setup_needed": [],
                "cleanup_strategy": "Log out." if operation.method == "POST" else None,
                "expected_status_codes": operation.response_codes,
                "rationale": "Quality-complete strategy",
            }
        )
    writer = ArtifactWriter(tmp_path / "run")
    agent = StrategyPlannerAgent(
        llm_client=MockLLMClient([json.dumps(insufficient), json.dumps(corrected)]),
        prompt_path=_prompt(tmp_path),
        artifact_writer=writer,
    )

    strategy, _ = agent.run(
        requirements,
        api_analysis,
        operations,
        BudgetConfig(max_iterations=1, max_tests_per_iteration=4, max_llm_calls=4),
        _empty_coverage(),
    )

    assert {item.test_type for item in strategy} == set(corrected_types)
    assert strategy[-1].setup_needed == ["Authenticate as user."]
    assert (tmp_path / "run/test_strategy.attempt1.raw.txt").is_file()


def test_strategy_agent_does_not_fail_final_run_for_diversity_shortfall(
    tmp_path: Path,
) -> None:
    requirements = RequirementsAnalysis(
        summary="Requirements",
        requirements=[
            RequirementItem(id=f"R{index}", source="test", text=f"Requirement {index}", role="user")
            for index in range(1, 9)
        ],
    )
    operations = [
        OpenAPIOperation(method="GET", path=f"/operation-{index}", response_codes=["200"])
        for index in range(1, 9)
    ]
    api_analysis = APIAnalysis(
        summary="API",
        operations=[
            APIOperationAnalysis(path=operation.path, method=operation.method)
            for operation in operations
        ],
    )
    test_types = [
        "happy_path",
        "edge_case",
        "negative",
        "happy_path",
        "edge_case",
        "negative",
        "happy_path",
        "edge_case",
        "negative",
        "happy_path",
    ]
    priorities = [
        "high",
        "high",
        "high",
        "medium",
        "medium",
        "medium",
        "medium",
        "low",
        "low",
        "low",
    ]
    strategy = []
    for index, test_type in enumerate(test_types, start=1):
        operation = operations[(index - 1) % 7]
        requirement_id = f"R{((index - 1) % 7) + 1}"
        strategy.append(
            {
                "requirement_id": requirement_id,
                "requirement_summary": f"Requirement {requirement_id[1:]}",
                "api_endpoint": operation.path,
                "http_method": operation.method,
                "prompt": f"Generate a {test_type} test.",
                "test_type": test_type,
                "priority": priorities[index - 1],
                "auth_role": None,
                "setup_needed": [],
                "cleanup_strategy": None,
                "expected_status_codes": operation.response_codes,
                "rationale": "Valid strategy with slightly limited diversity.",
            }
        )
    agent = StrategyPlannerAgent(
        llm_client=MockLLMClient([json.dumps(strategy), json.dumps(strategy)]),
        prompt_path=_prompt(tmp_path),
        artifact_writer=ArtifactWriter(tmp_path / "run"),
    )

    result, _ = agent.run(
        requirements,
        api_analysis,
        operations,
        BudgetConfig(max_iterations=1, max_tests_per_iteration=10, max_llm_calls=4),
        _empty_coverage(),
    )

    assert len(result) == 10
    assert len({(item.http_method, item.api_endpoint) for item in result}) == 7
    assert (tmp_path / "run/test_strategy.attempt1.raw.txt").is_file()


def _coverage_from_matches(pairs: dict[str, tuple[str, str]]) -> ProjectRequirementCoverage:
    return ProjectRequirementCoverage.from_matches(
        project_name="test-project",
        openapi_path="openapi.yaml",
        matches=[
            RequirementAPIMatch(
                requirement_id=requirement_id,
                status="implemented",
                matched_operations=[OperationReference(method=method, path=path)],
                rationale="test fixture",
            )
            for requirement_id, (method, path) in pairs.items()
        ],
    )


def test_cluster_requirements_groups_by_shared_operation() -> None:
    coverage = _coverage_from_matches(
        {
            "R1": ("GET", "/shared"),
            "R2": ("GET", "/shared"),
            "R3": ("GET", "/other"),
        }
    )

    clusters = StrategyPlannerAgent._cluster_requirements(["R1", "R2", "R3"], coverage)

    grouped = {frozenset(cluster) for cluster in clusters}
    assert frozenset({"R1", "R2"}) in grouped
    assert frozenset({"R3"}) in grouped


def test_pack_batches_merges_small_clusters_and_keeps_large_ones_intact() -> None:
    clusters = [["R1"], ["R2"], ["R3", "R4", "R5", "R6", "R7", "R8"], ["R9"]]

    batches = StrategyPlannerAgent._pack_batches(clusters, target_size=4)

    assert ["R1", "R2"] in batches
    assert ["R3", "R4", "R5", "R6", "R7", "R8"] in batches
    assert ["R9"] in batches


def test_batch_scope_includes_dependency_prerequisites() -> None:
    requirements = RequirementsAnalysis(
        summary="Requirements",
        requirements=[
            RequirementItem(id="R1", source="test", text="Do the protected thing", role="user"),
            RequirementItem(id="R2", source="test", text="Unrelated", role="user"),
        ],
    )
    api_analysis = APIAnalysis(
        summary="API",
        operations=[
            APIOperationAnalysis(path="/auth/login", method="POST"),
            APIOperationAnalysis(path="/protected", method="GET", auth_required=True),
            APIOperationAnalysis(path="/other", method="GET"),
        ],
        dependency_edges=[
            APIDependency(
                prerequisite_method="POST",
                prerequisite_path="/auth/login",
                dependent_method="GET",
                dependent_path="/protected",
                dependency_type="authentication",
                reason="Needs a session.",
            )
        ],
    )
    operations = [
        OpenAPIOperation(method="POST", path="/auth/login", response_codes=["200"]),
        OpenAPIOperation(
            method="GET", path="/protected", response_codes=["200"], auth_required=True
        ),
        OpenAPIOperation(method="GET", path="/other", response_codes=["200"]),
    ]
    coverage = _coverage_from_matches({"R1": ("GET", "/protected"), "R2": ("GET", "/other")})

    batch_requirements, batch_api, batch_operations = StrategyPlannerAgent._batch_scope(
        ["R1"], requirements, api_analysis, operations, coverage
    )

    assert [item.id for item in batch_requirements.requirements] == ["R1"]
    assert {(op.method, op.path) for op in batch_operations} == {
        ("GET", "/protected"),
        ("POST", "/auth/login"),
    }
    assert {(op.method, op.path) for op in batch_api.operations} == {
        ("GET", "/protected"),
        ("POST", "/auth/login"),
    }


def _independent_requirements_fixture(
    count: int,
) -> tuple[RequirementsAnalysis, APIAnalysis, list[OpenAPIOperation], ProjectRequirementCoverage]:
    requirements = RequirementsAnalysis(
        summary="Requirements",
        requirements=[
            RequirementItem(id=f"R{index}", source="test", text=f"Requirement {index}", role="user")
            for index in range(1, count + 1)
        ],
    )
    operations = [
        OpenAPIOperation(method="GET", path=f"/op{index}", response_codes=["200"])
        for index in range(1, count + 1)
    ]
    api_analysis = APIAnalysis(
        summary="API",
        operations=[
            APIOperationAnalysis(path=operation.path, method=operation.method)
            for operation in operations
        ],
    )
    coverage = _coverage_from_matches(
        {f"R{index}": ("GET", f"/op{index}") for index in range(1, count + 1)}
    )
    return requirements, api_analysis, operations, coverage


def _batch_items(requirement_ids: list[str], test_types: list[str]) -> list[dict]:
    return [
        {
            "requirement_id": requirement_id,
            "requirement_summary": f"Requirement {requirement_id[1:]}",
            "api_endpoint": f"/op{requirement_id[1:]}",
            "http_method": "GET",
            "prompt": f"Generate a {test_type} test.",
            "test_type": test_type,
            "priority": "high" if index % 2 == 0 else "medium",
            "auth_role": None,
            "setup_needed": [],
            "cleanup_strategy": None,
            "expected_status_codes": ["200"],
            "rationale": "Batch fixture item.",
        }
        for index, (requirement_id, test_type) in enumerate(
            zip(requirement_ids, test_types, strict=True)
        )
    ]


def test_strategy_agent_batches_by_requirement_and_merges_results(tmp_path: Path) -> None:
    requirements, api_analysis, operations, coverage = _independent_requirements_fixture(8)
    batch1 = _batch_items(
        ["R1", "R2", "R3", "R4", "R5", "R6"],
        ["happy_path", "happy_path", "edge_case", "happy_path", "happy_path", "negative"],
    )
    batch2 = _batch_items(["R7", "R8"], ["happy_path", "happy_path"])
    writer = ArtifactWriter(tmp_path / "run")
    agent = StrategyPlannerAgent(
        llm_client=MockLLMClient([json.dumps(batch1), json.dumps(batch2)]),
        prompt_path=_prompt(tmp_path),
        artifact_writer=writer,
        batch_by_requirement=True,
    )

    strategy, _ = agent.run(
        requirements,
        api_analysis,
        operations,
        BudgetConfig(max_iterations=1, max_tests_per_iteration=8, max_llm_calls=10),
        coverage,
    )

    assert {item.requirement_id for item in strategy} == {f"R{i}" for i in range(1, 9)}
    assert (tmp_path / "run/test_strategy.batch1.raw.txt").is_file()
    assert (tmp_path / "run/test_strategy.batch2.raw.txt").is_file()


def test_strategy_agent_batching_uses_compact_correction_when_needed(tmp_path: Path) -> None:
    requirements, api_analysis, operations, coverage = _independent_requirements_fixture(8)
    # every batch item uses the same test type, so the merged result fails the
    # "missing required test types" quality check and a correction call is needed.
    batch1 = _batch_items(["R1", "R2", "R3", "R4", "R5", "R6"], ["happy_path"] * 6)
    batch2 = _batch_items(["R7", "R8"], ["happy_path"] * 2)
    corrected = _batch_items(
        ["R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8"],
        [
            "happy_path",
            "edge_case",
            "negative",
            "happy_path",
            "edge_case",
            "negative",
            "happy_path",
            "happy_path",
        ],
    )
    writer = ArtifactWriter(tmp_path / "run")
    agent = StrategyPlannerAgent(
        llm_client=MockLLMClient([json.dumps(batch1), json.dumps(batch2), json.dumps(corrected)]),
        prompt_path=_prompt(tmp_path),
        artifact_writer=writer,
        batch_by_requirement=True,
    )

    strategy, _ = agent.run(
        requirements,
        api_analysis,
        operations,
        BudgetConfig(max_iterations=1, max_tests_per_iteration=8, max_llm_calls=10),
        coverage,
    )

    assert {item.test_type for item in strategy} == {"happy_path", "edge_case", "negative"}
    assert (tmp_path / "run/test_strategy.correction.raw.txt").is_file()


def test_strategy_agent_batching_falls_back_when_correction_call_fails(tmp_path: Path) -> None:
    requirements, api_analysis, operations, coverage = _independent_requirements_fixture(8)
    batch1 = _batch_items(["R1", "R2", "R3", "R4", "R5", "R6"], ["happy_path"] * 6)
    batch2 = _batch_items(["R7", "R8"], ["happy_path"] * 2)
    writer = ArtifactWriter(tmp_path / "run")
    agent = StrategyPlannerAgent(
        # no third response queued for the correction call: it will raise, and the
        # pre-correction merged draft must be returned instead of propagating the error.
        llm_client=MockLLMClient([json.dumps(batch1), json.dumps(batch2)]),
        prompt_path=_prompt(tmp_path),
        artifact_writer=writer,
        batch_by_requirement=True,
    )

    strategy, _ = agent.run(
        requirements,
        api_analysis,
        operations,
        BudgetConfig(max_iterations=1, max_tests_per_iteration=8, max_llm_calls=10),
        coverage,
    )

    assert {item.requirement_id for item in strategy} == {f"R{i}" for i in range(1, 9)}
    assert {item.test_type for item in strategy} == {"happy_path"}


def test_normalize_drops_items_naming_operations_absent_from_openapi() -> None:
    """One invented endpoint must cost its own item, not the whole project.

    Reproduces the team11 failure: the planner named PATCH /sessions/current/config,
    which the contract does not document, and the run aborted even though every other
    item was valid.
    """

    operations = [
        OpenAPIOperation(method="GET", path="/op1", response_codes=["200"]),
        OpenAPIOperation(method="POST", path="/op2", response_codes=["201"]),
    ]
    strategy = [
        item
        for item in _strategy_from(
            [("R1", "GET", "/op1", "happy_path"), ("R2", "POST", "/op2", "negative")]
        )
    ]
    hallucinated = _strategy_from(
        [("R3", "PATCH", "/sessions/current/config", "edge_case")]
    )

    normalized = StrategyPlannerAgent._normalize_strategy(strategy + hallucinated, operations)

    assert [(item.http_method, item.api_endpoint) for item in normalized] == [
        ("GET", "/op1"),
        ("POST", "/op2"),
    ]


def test_normalize_keeps_every_item_backed_by_the_contract() -> None:
    operations = [OpenAPIOperation(method="GET", path="/op1", response_codes=["200"])]
    strategy = _strategy_from([("R1", "GET", "/op1", "happy_path")])

    normalized = StrategyPlannerAgent._normalize_strategy(strategy, operations)

    assert len(normalized) == 1


def _strategy_from(rows: list[tuple[str, str, str, str]]) -> list[StrategyItem]:
    return [
        StrategyItem(
            requirement_id=requirement_id,
            requirement_summary=f"Requirement {requirement_id}",
            api_endpoint=endpoint,
            http_method=method,
            prompt=f"Generate a {test_type} test.",
            test_type=test_type,
            priority="high",
            expected_status_codes=["200"],
            rationale="Fixture item.",
        )
        for requirement_id, method, endpoint, test_type in rows
    ]


def test_finalize_synthesizes_a_required_type_lost_to_a_dropped_item() -> None:
    """Reproduces the run2 failure: the only negative item named a fake endpoint.

    Dropping it left the strategy without a `negative` test, which the quality gate
    rejects. The missing type must be rebuilt from a documented operation instead.
    """

    operations = [
        OpenAPIOperation(method="GET", path="/reports", response_codes=["200", "400"]),
        OpenAPIOperation(
            method="POST",
            path="/reports",
            request_body_schema={"type": "object"},
            response_codes=["201", "422"],
        ),
    ]
    requirements = RequirementsAnalysis(
        summary="Requirements",
        requirements=[
            RequirementItem(id="R1", source="test", text="Report handling", role="user")
        ],
    )
    api_analysis = APIAnalysis(
        summary="API",
        operations=[
            APIOperationAnalysis(path=item.path, method=item.method) for item in operations
        ],
    )
    strategy = _strategy_from(
        [
            ("R1", "GET", "/reports", "happy_path"),
            ("R1", "POST", "/reports", "edge_case"),
            # The model's only negative test names an endpoint that does not exist.
            ("R1", "DELETE", "/users/reports", "negative"),
        ]
    )

    finalized = StrategyPlannerAgent._finalize_strategy(
        strategy,
        requirements,
        api_analysis,
        operations,
        BudgetConfig(max_iterations=1, max_tests_per_iteration=10, max_llm_calls=10),
        synthesize_missing_types=True,
    )

    assert {item.test_type for item in finalized} >= {"happy_path", "edge_case", "negative"}
    negative = next(item for item in finalized if item.test_type == "negative")
    # The replacement must point at a documented operation and assert a rejection.
    assert (negative.http_method, negative.api_endpoint) in {
        (item.method, item.path) for item in operations
    }
    assert all(code.startswith("4") for code in negative.expected_status_codes)


def test_finalize_does_not_synthesize_before_the_corrective_call() -> None:
    """Filling a gap deterministically must not pre-empt the model's corrective call."""

    operations = [OpenAPIOperation(method="GET", path="/reports", response_codes=["200", "400"])]
    requirements = RequirementsAnalysis(
        summary="Requirements",
        requirements=[RequirementItem(id="R1", source="test", text="Reports", role="user")],
    )
    api_analysis = APIAnalysis(
        summary="API", operations=[APIOperationAnalysis(path="/reports", method="GET")]
    )
    strategy = _strategy_from([("R1", "GET", "/reports", "happy_path")])

    finalized = StrategyPlannerAgent._finalize_strategy(
        strategy,
        requirements,
        api_analysis,
        operations,
        BudgetConfig(max_iterations=1, max_tests_per_iteration=10, max_llm_calls=10),
    )

    assert {item.test_type for item in finalized} == {"happy_path"}


def test_finalize_leaves_a_complete_strategy_untouched() -> None:
    operations = [
        OpenAPIOperation(method="GET", path="/reports", response_codes=["200", "400"]),
    ]
    requirements = RequirementsAnalysis(
        summary="Requirements",
        requirements=[RequirementItem(id="R1", source="test", text="Reports", role="user")],
    )
    api_analysis = APIAnalysis(
        summary="API",
        operations=[APIOperationAnalysis(path="/reports", method="GET")],
    )
    strategy = _strategy_from(
        [
            ("R1", "GET", "/reports", "happy_path"),
            ("R1", "GET", "/reports", "edge_case"),
            ("R1", "GET", "/reports", "negative"),
        ]
    )

    finalized = StrategyPlannerAgent._finalize_strategy(
        strategy,
        requirements,
        api_analysis,
        operations,
        BudgetConfig(max_iterations=1, max_tests_per_iteration=10, max_llm_calls=10),
    )

    assert len(finalized) == 3
    assert all(item.rationale == "Fixture item." for item in finalized)


def test_each_batch_receives_its_own_share_of_the_budget() -> None:
    """Sending the whole budget to every batch asks N times for the whole plan.

    That was merely wasteful at a budget of ten; at thirty it made each response large
    enough to exhaust the model's token ceiling, which is what made batched planning look
    incapable of filling a larger budget.
    """

    budget = BudgetConfig(max_iterations=1, max_tests_per_iteration=30, max_llm_calls=50)
    all_ids = [f"R{index}" for index in range(29)]

    share = StrategyPlannerAgent._batch_budget(budget, all_ids[:6], all_ids)

    assert share.max_tests_per_iteration == 7
    # The rest of the budget is untouched.
    assert share.max_llm_calls == 50


def test_a_batch_never_drops_below_the_minimum_the_quality_gate_demands() -> None:
    budget = BudgetConfig(max_iterations=1, max_tests_per_iteration=10, max_llm_calls=50)
    all_ids = [f"R{index}" for index in range(29)]

    share = StrategyPlannerAgent._batch_budget(budget, all_ids[:1], all_ids)

    assert share.max_tests_per_iteration == 3


def test_a_single_batch_project_keeps_the_whole_budget() -> None:
    """With one batch there is nothing to share the budget with."""

    budget = BudgetConfig(max_iterations=1, max_tests_per_iteration=30, max_llm_calls=50)
    all_ids = ["R1", "R2", "R3"]

    share = StrategyPlannerAgent._batch_budget(budget, all_ids, all_ids)

    assert share.max_tests_per_iteration == 30


def test_replan_reuses_coverage_and_only_replaces_the_strategy(tmp_path: Path) -> None:
    """Re-planning must not re-decide coverage: that is what the run was selected on."""

    from thesis_rest_tester.config import load_config
    from thesis_rest_tester.replanner import Replanner

    source = tmp_path / "source"
    project = source / "projects" / "team-a"
    project.mkdir(parents=True)
    (source / "requirements_analysis.json").write_text(
        RequirementsAnalysis(
            summary="Requirements",
            requirements=[
                RequirementItem(id="R1", source="test", text="Create a report", role="user")
            ],
            assumptions=["an assumption"],
        ).model_dump_json(),
        encoding="utf-8",
    )
    (project / "openapi_operations.json").write_text(
        json.dumps(
            [
                {"method": "GET", "path": "/reports", "response_codes": ["200"]},
                {"method": "POST", "path": "/reports", "response_codes": ["201", "400"]},
            ]
        ),
        encoding="utf-8",
    )
    (project / "api_analysis.json").write_text(
        APIAnalysis(
            summary="API",
            operations=[
                APIOperationAnalysis(path="/reports", method="GET"),
                APIOperationAnalysis(path="/reports", method="POST"),
            ],
        ).model_dump_json(),
        encoding="utf-8",
    )
    coverage = ProjectRequirementCoverage.from_matches(
        project_name="team-a",
        openapi_path="swagger.yaml",
        matches=[
            RequirementAPIMatch(
                requirement_id="R1",
                status="implemented",
                matched_operations=[
                    OperationReference(method="GET", path="/reports"),
                    OperationReference(method="POST", path="/reports"),
                ],
                rationale="Reports are exposed.",
            )
        ],
    )
    (project / "requirement_coverage.json").write_text(
        coverage.model_dump_json(), encoding="utf-8"
    )
    (project / "test_strategy.json").write_text(json.dumps([{}, {}]), encoding="utf-8")
    (project / "workflow_plan.json").write_text("{}", encoding="utf-8")

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
project_name: replan-test
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
budget:
  max_iterations: 1
  max_tests_per_iteration: 3
  max_llm_calls: 10
output:
  runs_dir: runs
""",
        encoding="utf-8",
    )
    planned = [
        {
            "requirement_id": "R1",
            "requirement_summary": "Create a report",
            "api_endpoint": path,
            "http_method": method,
            "prompt": f"Generate a {test_type} test.",
            "test_type": test_type,
            "priority": "high",
            "setup_needed": [],
            "cleanup_strategy": None,
            "expected_status_codes": ["200"],
            "rationale": "Replanned.",
        }
        for method, path, test_type in (
            ("GET", "/reports", "happy_path"),
            ("POST", "/reports", "negative"),
            ("POST", "/reports", "edge_case"),
        )
    ]

    result = Replanner(
        source,
        load_config(config_path),
        llm_client=MockLLMClient([json.dumps(planned)]),
    ).run(output_run=tmp_path / "out")

    assert [(p.items_before, p.items_after) for p in result.projects] == [(2, 3)]
    destination = tmp_path / "out" / "projects" / "team-a"
    # The coverage file is carried over byte for byte, not recomputed.
    assert (destination / "requirement_coverage.json").read_text(encoding="utf-8") == (
        project / "requirement_coverage.json"
    ).read_text(encoding="utf-8")
    # And the new plan carries that same coverage plus the fresh strategy.
    plan = json.loads((destination / "workflow_plan.json").read_text(encoding="utf-8"))
    assert len(plan["strategy_items"]) == 3
    assert plan["requirement_coverage"]["matches"][0]["requirement_id"] == "R1"
    assert (tmp_path / "out" / "replan.json").is_file()
