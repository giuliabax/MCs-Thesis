from __future__ import annotations

import json
from pathlib import Path

from thesis_rest_tester.agents.api_understanding import APIUnderstandingAgent
from thesis_rest_tester.agents.test_strategy_planner import (
    TestStrategyPlannerAgent as StrategyPlannerAgent,
)
from thesis_rest_tester.artifacts.writer import ArtifactWriter
from thesis_rest_tester.config import BudgetConfig
from thesis_rest_tester.domain.models import OpenAPIOperation, RequirementItem
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
    )

    assert len(result) == 10
    assert len({(item.http_method, item.api_endpoint) for item in result}) == 7
    assert (tmp_path / "run/test_strategy.attempt1.raw.txt").is_file()
