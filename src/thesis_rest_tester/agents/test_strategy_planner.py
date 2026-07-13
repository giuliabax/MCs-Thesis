"""Test strategy planning agent."""

import json
import math
import re
from pathlib import Path

from pydantic import TypeAdapter

from thesis_rest_tester.agents.base import AgentResponseError, BaseAgent
from thesis_rest_tester.artifacts.writer import ArtifactWriter
from thesis_rest_tester.config import BudgetConfig
from thesis_rest_tester.domain.models import (
    AgentOutput,
    OpenAPIOperation,
    RequirementItem,
    TestStrategyItem,
)
from thesis_rest_tester.domain.schemas import APIAnalysis, RequirementsAnalysis
from thesis_rest_tester.llm.base import LLMClient


class TestStrategyPlannerAgent(BaseAgent[list[TestStrategyItem]]):
    def __init__(
        self,
        llm_client: LLMClient,
        prompt_path: str | Path,
        artifact_writer: ArtifactWriter,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> None:
        super().__init__(
            name="test_strategy_planner",
            prompt_path=prompt_path,
            llm_client=llm_client,
            artifact_writer=artifact_writer,
            response_adapter=TypeAdapter(list[TestStrategyItem]),
            raw_artifact_name="test_strategy.raw.txt",
            temperature=temperature,
            max_tokens=max_tokens,
        )

    def run(
        self,
        requirements_analysis: RequirementsAnalysis,
        api_analysis: APIAnalysis,
        operations: list[OpenAPIOperation],
        budget: BudgetConfig,
    ) -> tuple[list[TestStrategyItem], AgentOutput]:
        payload = {
            "requirements_analysis": requirements_analysis.model_dump(mode="json"),
            "api_analysis": api_analysis.model_dump(mode="json"),
            "openapi_operations": [operation.model_dump(mode="json") for operation in operations],
            "budget": budget.model_dump(mode="json"),
        }
        user_prompt = (
            "Create the test strategy from this planning context. "
            "Return only a strict JSON array.\n\n"
            + json.dumps(payload, indent=2, ensure_ascii=False)
        )
        strategy, output = self.call_and_validate(user_prompt)
        strategy = self._finalize_strategy(
            strategy,
            requirements_analysis,
            api_analysis,
            operations,
            budget,
        )
        output = output.model_copy(
            update={"parsed_json": [item.model_dump(mode="json") for item in strategy]}
        )
        issues = self._quality_issues(
            strategy,
            requirements_analysis,
            api_analysis,
            operations,
            budget,
        )
        if not issues:
            return strategy, output

        self._artifact_writer.write_text("test_strategy.attempt1.raw.txt", output.raw_text)
        if budget.max_llm_calls <= 3:
            raise AgentResponseError(
                "Test Strategy Planner failed semantic quality checks and the LLM-call budget "
                "does not permit a corrective call: " + "; ".join(issues)
            )

        correction_prompt = (
            user_prompt
            + "\n\nYour previous strategy draft failed these mandatory quality checks:\n- "
            + "\n- ".join(issues)
            + "\n\nPrevious draft:\n"
            + json.dumps([item.model_dump(mode="json") for item in strategy], indent=2)
            + "\n\nReturn a complete replacement JSON array that fixes every issue."
        )
        corrected, corrected_output = self.call_and_validate(correction_prompt)
        corrected = self._finalize_strategy(
            corrected,
            requirements_analysis,
            api_analysis,
            operations,
            budget,
        )
        corrected_output = corrected_output.model_copy(
            update={"parsed_json": [item.model_dump(mode="json") for item in corrected]}
        )
        corrected_issues = self._quality_issues(
            corrected,
            requirements_analysis,
            api_analysis,
            operations,
            budget,
            enforce_diversity=False,
        )
        if corrected_issues:
            raise AgentResponseError(
                "Test Strategy Planner still failed semantic quality checks after one corrective "
                "call: " + "; ".join(corrected_issues)
            )
        return corrected, corrected_output

    @classmethod
    def _finalize_strategy(
        cls,
        strategy: list[TestStrategyItem],
        requirements_analysis: RequirementsAnalysis,
        api_analysis: APIAnalysis,
        operations: list[OpenAPIOperation],
        budget: BudgetConfig,
    ) -> list[TestStrategyItem]:
        normalized = cls._normalize_strategy(strategy, operations)
        if (
            api_analysis.dependency_edges
            and budget.max_tests_per_iteration >= 4
            and not any(item.test_type == "stateful" for item in normalized)
        ):
            normalized.append(
                cls._stateful_item(requirements_analysis, api_analysis, operations)
            )
            normalized = cls._normalize_strategy(normalized, operations)
        return cls._trim_to_budget(normalized, budget.max_tests_per_iteration)

    @staticmethod
    def _stateful_item(
        requirements_analysis: RequirementsAnalysis,
        api_analysis: APIAnalysis,
        operations: list[OpenAPIOperation],
    ) -> TestStrategyItem:
        operation_map = {(operation.method, operation.path): operation for operation in operations}
        edge = next(
            edge
            for edge in api_analysis.dependency_edges
            if (edge.dependent_method, edge.dependent_path) in operation_map
        )
        dependent = operation_map[(edge.dependent_method, edge.dependent_path)]
        requirement = TestStrategyPlannerAgent._best_requirement(
            requirements_analysis.requirements,
            f"{edge.prerequisite_path} {edge.dependent_path} {edge.reason}",
        )
        success_codes = [
            code for code in dependent.response_codes if not code.startswith(("4", "5"))
        ] or dependent.response_codes or ["200"]
        return TestStrategyItem(
            requirement_id=requirement.id,
            requirement_summary=requirement.text,
            api_endpoint=dependent.path,
            http_method=dependent.method,
            prompt=(
                f"Generate an independent workflow test that performs "
                f"{edge.prerequisite_method} {edge.prerequisite_path}, captures its state, then "
                f"calls {edge.dependent_method} {edge.dependent_path}."
            ),
            test_type="stateful",
            priority="high",
            auth_role=requirement.role if dependent.auth_required else None,
            setup_needed=[
                f"Complete {edge.prerequisite_method} {edge.prerequisite_path} and retain "
                "the resulting identifiers or session state."
            ],
            cleanup_strategy="Delete resources created by the workflow and restore prior state.",
            expected_status_codes=success_codes,
            rationale=edge.reason,
        )

    @staticmethod
    def _best_requirement(
        requirements: list[RequirementItem],
        context: str,
    ) -> RequirementItem:
        context_tokens = set(re.findall(r"[a-z0-9]+", context.lower()))

        def score(requirement: RequirementItem) -> int:
            requirement_tokens = set(re.findall(r"[a-z0-9]+", requirement.text.lower()))
            return len(context_tokens & requirement_tokens)

        return max(requirements, key=score)

    @staticmethod
    def _trim_to_budget(
        strategy: list[TestStrategyItem],
        maximum: int,
    ) -> list[TestStrategyItem]:
        unique: list[TestStrategyItem] = []
        signatures: set[tuple[str, str, str, str]] = set()
        for item in strategy:
            signature = (
                item.requirement_id,
                item.http_method,
                item.api_endpoint,
                item.test_type,
            )
            if signature not in signatures:
                signatures.add(signature)
                unique.append(item)
        if len(unique) <= maximum:
            return unique

        selected: list[TestStrategyItem] = []
        required_types = ["happy_path", "edge_case", "negative"]
        if any(item.test_type == "stateful" for item in unique) and maximum >= 4:
            required_types.append("stateful")
        for test_type in required_types:
            candidate = next((item for item in unique if item.test_type == test_type), None)
            if candidate is not None and candidate not in selected:
                selected.append(candidate)

        remaining = [item for item in unique if item not in selected]
        priority_score = {"high": 2, "medium": 1, "low": 0}
        while len(selected) < maximum and remaining:
            selected_operations = {
                (item.http_method, item.api_endpoint) for item in selected
            }
            selected_requirements = {item.requirement_id for item in selected}

            def diversity_score(
                item: TestStrategyItem,
                operation_keys: set[tuple[str, str]] = selected_operations,
                requirement_keys: set[str] = selected_requirements,
            ) -> tuple[int, int]:
                novelty = 0
                if (item.http_method, item.api_endpoint) not in operation_keys:
                    novelty += 3
                if item.requirement_id not in requirement_keys:
                    novelty += 3
                return novelty, priority_score[item.priority]

            candidate = max(remaining, key=diversity_score)
            selected.append(candidate)
            remaining.remove(candidate)

        original_order = {id(item): index for index, item in enumerate(unique)}
        return sorted(selected, key=lambda item: original_order[id(item)])

    @staticmethod
    def _normalize_strategy(
        strategy: list[TestStrategyItem],
        operations: list[OpenAPIOperation],
    ) -> list[TestStrategyItem]:
        """Add setup and cleanup facts that are deterministic from OpenAPI."""

        operation_map = {(operation.method, operation.path): operation for operation in operations}
        mutating_methods = {"POST", "PUT", "PATCH", "DELETE"}
        normalized: list[TestStrategyItem] = []
        for item in strategy:
            operation = operation_map.get((item.http_method, item.api_endpoint))
            setup = list(item.setup_needed)
            cleanup = item.cleanup_strategy

            if operation is not None and operation.auth_required:
                has_auth_setup = any(
                    keyword in step.lower()
                    for step in setup
                    for keyword in ("auth", "login", "session")
                )
                if not has_auth_setup:
                    role = item.auth_role or "a user with the required role"
                    setup.insert(0, f"Authenticate as {role}.")

            if "{" in item.api_endpoint:
                has_resource_setup = any(
                    keyword in step.lower()
                    for step in setup
                    for keyword in ("create", "existing", "resource", "report", "user")
                )
                if not has_resource_setup:
                    setup.append("Create or locate the resource referenced by the path parameter.")

            if item.http_method in mutating_methods and not cleanup:
                cleanup = (
                    "Verify no resource was created; delete it if unexpectedly present."
                    if item.test_type in {"negative", "edge_case"}
                    else "Delete created resources or restore their previous state."
                )

            normalized.append(
                item.model_copy(
                    update={
                        "setup_needed": list(dict.fromkeys(setup)),
                        "cleanup_strategy": cleanup,
                    }
                )
            )
        return normalized

    @staticmethod
    def _quality_issues(
        strategy: list[TestStrategyItem],
        requirements_analysis: RequirementsAnalysis,
        api_analysis: APIAnalysis,
        operations: list[OpenAPIOperation],
        budget: BudgetConfig,
        *,
        enforce_diversity: bool = True,
    ) -> list[str]:
        issues: list[str] = []
        operation_map = {(operation.method, operation.path): operation for operation in operations}
        requirement_ids = {item.id for item in requirements_analysis.requirements}

        if len(strategy) > budget.max_tests_per_iteration:
            issues.append(
                f"strategy has {len(strategy)} items but the maximum is "
                f"{budget.max_tests_per_iteration}"
            )
        minimum_items = min(3, budget.max_tests_per_iteration)
        if len(strategy) < minimum_items:
            issues.append(f"strategy must contain at least {minimum_items} items")

        required_types = {"happy_path", "edge_case", "negative"}
        present_types = {item.test_type for item in strategy}
        if budget.max_tests_per_iteration >= 3:
            missing_types = sorted(required_types - present_types)
            if missing_types:
                issues.append("missing required test types: " + ", ".join(missing_types))
        if api_analysis.dependency_edges and budget.max_tests_per_iteration >= 4:
            if "stateful" not in present_types:
                issues.append("at least one stateful test is required for API dependency edges")

        unknown_requirements = sorted(
            {item.requirement_id for item in strategy if item.requirement_id not in requirement_ids}
        )
        if unknown_requirements:
            issues.append("unknown requirement IDs: " + ", ".join(unknown_requirements))

        unknown_operations = sorted(
            {
                f"{item.http_method} {item.api_endpoint}"
                for item in strategy
                if (item.http_method, item.api_endpoint) not in operation_map
            }
        )
        if unknown_operations:
            issues.append("operations absent from OpenAPI: " + ", ".join(unknown_operations))

        mutating_methods = {"POST", "PUT", "PATCH", "DELETE"}
        for index, item in enumerate(strategy, start=1):
            operation = operation_map.get((item.http_method, item.api_endpoint))
            if not item.expected_status_codes:
                issues.append(f"item {index} has no expected status codes")
            if operation is not None and operation.auth_required:
                has_auth_setup = any(
                    keyword in step.lower()
                    for step in item.setup_needed
                    for keyword in ("auth", "login", "session")
                )
                if not has_auth_setup:
                    issues.append(
                        f"item {index} targets an authenticated operation without auth setup"
                    )
            if item.http_method in mutating_methods and not item.cleanup_strategy:
                issues.append(f"item {index} mutates state without a cleanup strategy")

        signatures = [
            (item.requirement_id, item.http_method, item.api_endpoint, item.test_type)
            for item in strategy
        ]
        if len(signatures) != len(set(signatures)):
            issues.append("strategy contains duplicate requirement/operation/test-type items")

        budget_coverage_target = math.ceil(budget.max_tests_per_iteration * 0.8)
        if enforce_diversity:
            distinct_operations = {(item.http_method, item.api_endpoint) for item in strategy}
            operation_diversity_target = max(3, budget_coverage_target)
            operation_target = min(
                len(operations),
                max(1, min(budget.max_tests_per_iteration, operation_diversity_target)),
            )
            if len(distinct_operations) < operation_target:
                issues.append(
                    f"strategy covers {len(distinct_operations)} distinct operations; "
                    f"at least {operation_target} are required"
                )

            distinct_requirements = {item.requirement_id for item in strategy}
            requirement_target = min(
                len(requirements_analysis.requirements),
                max(1, budget_coverage_target),
            )
            if len(distinct_requirements) < requirement_target:
                issues.append(
                    f"strategy covers {len(distinct_requirements)} requirements; "
                    f"at least {requirement_target} are required"
                )

        if len(strategy) >= 5 and len({item.priority for item in strategy}) < 2:
            issues.append("strategies with five or more items must use at least two priorities")
        return issues
