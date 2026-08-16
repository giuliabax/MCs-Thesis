"""Test strategy planning agent."""

import json
import logging
import math
import re
from pathlib import Path

from pydantic import TypeAdapter

from thesis_rest_tester.agents.base import AgentResponseError, BaseAgent
from thesis_rest_tester.artifacts.writer import ArtifactWriter
from thesis_rest_tester.config import BudgetConfig
from thesis_rest_tester.domain.compact import compact_api_analysis, compact_requirements
from thesis_rest_tester.domain.coverage import ProjectRequirementCoverage
from thesis_rest_tester.domain.models import (
    AgentOutput,
    OpenAPIOperation,
    RequirementItem,
    TestStrategyItem,
)
from thesis_rest_tester.domain.schemas import APIAnalysis, RequirementsAnalysis
from thesis_rest_tester.llm.base import LLMClient

_BATCH_SIZE = 6
# Module-level: the normalization helpers are class/static methods reached from
# _finalize_strategy, so they have no access to the agent instance logger.
_logger = logging.getLogger(__name__)


class TestStrategyPlannerAgent(BaseAgent[list[TestStrategyItem]]):
    # Test types the quality gate demands of every accepted strategy.
    _REQUIRED_TEST_TYPES = ("happy_path", "edge_case", "negative")

    def __init__(
        self,
        llm_client: LLMClient,
        prompt_path: str | Path,
        artifact_writer: ArtifactWriter,
        temperature: float | None = None,
        max_tokens: int | None = None,
        batch_by_requirement: bool = False,
        think: bool = True,
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
            think=think,
        )
        self._batch_by_requirement = batch_by_requirement

    def run(
        self,
        requirements_analysis: RequirementsAnalysis,
        api_analysis: APIAnalysis,
        operations: list[OpenAPIOperation],
        budget: BudgetConfig,
        coverage: ProjectRequirementCoverage,
        correction: str | None = None,
    ) -> tuple[list[TestStrategyItem], AgentOutput]:
        """Plan the strategy. ``correction`` says what a previous plan got wrong.

        A feedback iteration that re-issued this call unchanged would reproduce the plan
        it is trying to repair, so the note is the only thing distinguishing the two.
        """

        self._correction = correction
        if self._batch_by_requirement:
            return self._run_batched(
                requirements_analysis, api_analysis, operations, budget, coverage
            )
        return self._run_single_call(requirements_analysis, api_analysis, operations, budget)

    def _with_correction(self, user_prompt: str) -> str:
        """Append the feedback note, where it reads as the last instruction given."""

        correction = getattr(self, "_correction", None)
        if not correction:
            return user_prompt
        return user_prompt + (
            "\n\nA previous plan for this project produced tests that could not succeed. "
            "Correct it as described below:\n" + correction
        )

    def _run_single_call(
        self,
        requirements_analysis: RequirementsAnalysis,
        api_analysis: APIAnalysis,
        operations: list[OpenAPIOperation],
        budget: BudgetConfig,
    ) -> tuple[list[TestStrategyItem], AgentOutput]:
        payload = {
            "requirements_analysis": {
                "requirements": compact_requirements(requirements_analysis.requirements),
                "domain_rules": requirements_analysis.domain_rules,
                "edge_cases": requirements_analysis.edge_cases,
            },
            "api_analysis": compact_api_analysis(api_analysis),
            "budget": budget.model_dump(mode="json"),
        }
        user_prompt = self._with_correction(
            "Create the test strategy from this planning context. "
            "Return only a strict JSON array.\n\n"
            + json.dumps(payload, ensure_ascii=False)
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
        # Last attempt: the model has had its corrective call, so a still-missing
        # required type is now rebuilt from the contract instead of failing the project.
        corrected = self._finalize_strategy(
            corrected,
            requirements_analysis,
            api_analysis,
            operations,
            budget,
            synthesize_missing_types=True,
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

    def _run_batched(
        self,
        requirements_analysis: RequirementsAnalysis,
        api_analysis: APIAnalysis,
        operations: list[OpenAPIOperation],
        budget: BudgetConfig,
        coverage: ProjectRequirementCoverage,
    ) -> tuple[list[TestStrategyItem], AgentOutput]:
        requirement_ids = [item.id for item in requirements_analysis.requirements]
        clusters = self._cluster_requirements(requirement_ids, coverage)
        batches = self._pack_batches(clusters)
        self._logger.info(
            "Batching test strategy planning into %d batch(es) for %d requirement(s)",
            len(batches),
            len(requirement_ids),
        )

        original_raw_name = self._raw_artifact_name
        collected: list[TestStrategyItem] = []
        raw_texts: list[str] = []
        last_output: AgentOutput | None = None
        try:
            for index, batch_ids in enumerate(batches, start=1):
                batch_requirements, batch_api, _ = self._batch_scope(
                    batch_ids, requirements_analysis, api_analysis, operations, coverage
                )
                batch_budget = self._batch_budget(budget, batch_ids, requirement_ids)
                payload = {
                    "requirements_analysis": {
                        "requirements": compact_requirements(batch_requirements.requirements),
                        "domain_rules": batch_requirements.domain_rules,
                        "edge_cases": batch_requirements.edge_cases,
                    },
                    "api_analysis": compact_api_analysis(batch_api),
                    "budget": batch_budget.model_dump(mode="json"),
                }
                user_prompt = self._with_correction(
                    f"Create test strategy items for batch {index}/{len(batches)} of this "
                    "project's requirements. Each batch is planned independently and merged "
                    "afterward, so do not worry about the diversity targets for the whole "
                    "project; those are enforced once every batch is merged. The budget "
                    f"below is this batch's own share: produce at most "
                    f"{batch_budget.max_tests_per_iteration} items. "
                    "Cover each requirement in this batch with an appropriate mix of test types. "
                    "Return only a strict JSON array.\n\n"
                    + json.dumps(payload, ensure_ascii=False)
                )
                self._raw_artifact_name = f"test_strategy.batch{index}.raw.txt"
                batch_items, batch_output = self.call_and_validate(user_prompt)
                collected.extend(batch_items)
                raw_texts.append(batch_output.raw_text)
                last_output = batch_output
        finally:
            self._raw_artifact_name = original_raw_name

        if last_output is None:
            raise AgentResponseError("Test Strategy Planner produced no batches to plan from")

        strategy = self._finalize_strategy(
            collected, requirements_analysis, api_analysis, operations, budget
        )
        output = last_output.model_copy(
            update={
                "raw_text": "\n\n---\n\n".join(raw_texts),
                "parsed_json": [item.model_dump(mode="json") for item in strategy],
            }
        )
        issues = self._quality_issues(
            strategy, requirements_analysis, api_analysis, operations, budget
        )
        if not issues:
            return strategy, output

        self._artifact_writer.write_text("test_strategy.attempt1.raw.txt", output.raw_text)
        if budget.max_llm_calls <= 3:
            raise AgentResponseError(
                "Test Strategy Planner failed semantic quality checks and the LLM-call budget "
                "does not permit a corrective call: " + "; ".join(issues)
            )

        try:
            self._raw_artifact_name = "test_strategy.correction.raw.txt"
            corrected, corrected_output = self._run_compact_correction(
                strategy, issues, requirements_analysis, api_analysis, budget
            )
        except Exception as exc:
            self._logger.warning(
                "Test Strategy Planner corrective call failed (%s); keeping the pre-correction "
                "batched draft",
                exc,
            )
            return strategy, output
        finally:
            self._raw_artifact_name = original_raw_name

        corrected = self._finalize_strategy(
            corrected,
            requirements_analysis,
            api_analysis,
            operations,
            budget,
            synthesize_missing_types=True,
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
            self._logger.warning(
                "Test Strategy Planner still failed quality checks after correction (%s); "
                "keeping the pre-correction batched draft",
                "; ".join(corrected_issues),
            )
            return strategy, output
        return corrected, corrected_output

    def _run_compact_correction(
        self,
        strategy: list[TestStrategyItem],
        issues: list[str],
        requirements_analysis: RequirementsAnalysis,
        api_analysis: APIAnalysis,
        budget: BudgetConfig,
    ) -> tuple[list[TestStrategyItem], AgentOutput]:
        """Ask for a replacement strategy using a compact summary instead of the full context.

        The full requirements/API analyses were already sent once per batch; resending them in
        full alongside the previous draft (as the single-call correction path does) risks
        exceeding a context-constrained local model's window. This keeps only the fields needed
        to reason about the reported quality issues.
        """

        compact_items = [
            {
                "requirement_id": item.requirement_id,
                "http_method": item.http_method,
                "api_endpoint": item.api_endpoint,
                "test_type": item.test_type,
            }
            for item in strategy
        ]
        compact_requirements = [
            {"id": item.id, "text": item.text, "role": item.role}
            for item in requirements_analysis.requirements
        ]
        compact_operations = [
            {"method": op.method, "path": op.path, "auth_required": op.auth_required}
            for op in api_analysis.operations
        ]
        payload = {
            "current_strategy_summary": compact_items,
            "available_requirements": compact_requirements,
            "available_operations": compact_operations,
            "budget": budget.model_dump(mode="json"),
            "quality_issues": issues,
        }
        correction_prompt = (
            "The current test strategy (summarized below) failed these quality checks:\n- "
            + "\n- ".join(issues)
            + "\n\nCurrent strategy summary, plus the catalogue of available requirements and "
            "operations to draw additional or replacement items from:\n\n"
            + json.dumps(payload, indent=2, ensure_ascii=False)
            + "\n\nReturn a complete replacement JSON array of test strategy items that fixes "
            "every issue. Reuse unaffected items from the current strategy summary where "
            "appropriate, and add or replace items as needed."
        )
        return self.call_and_validate(correction_prompt)

    @staticmethod
    def _cluster_requirements(
        requirement_ids: list[str],
        coverage: ProjectRequirementCoverage,
    ) -> list[list[str]]:
        """Group requirement IDs that share at least one matched OpenAPI operation."""

        operations_by_requirement = {
            match.requirement_id: {(ref.method, ref.path) for ref in match.matched_operations}
            for match in coverage.matches
        }
        parent = {requirement_id: requirement_id for requirement_id in requirement_ids}

        def find(requirement_id: str) -> str:
            while parent[requirement_id] != requirement_id:
                parent[requirement_id] = parent[parent[requirement_id]]
                requirement_id = parent[requirement_id]
            return requirement_id

        def union(left: str, right: str) -> None:
            root_left, root_right = find(left), find(right)
            if root_left != root_right:
                parent[root_left] = root_right

        operation_owners: dict[tuple[str, str], str] = {}
        for requirement_id in requirement_ids:
            for operation_key in operations_by_requirement.get(requirement_id, set()):
                if operation_key in operation_owners:
                    union(requirement_id, operation_owners[operation_key])
                else:
                    operation_owners[operation_key] = requirement_id

        clusters: dict[str, list[str]] = {}
        for requirement_id in requirement_ids:
            clusters.setdefault(find(requirement_id), []).append(requirement_id)
        return list(clusters.values())

    @staticmethod
    def _pack_batches(
        clusters: list[list[str]],
        target_size: int = _BATCH_SIZE,
    ) -> list[list[str]]:
        """Pack small clusters together up to target_size; keep larger clusters intact."""

        batches: list[list[str]] = []
        current: list[str] = []
        for cluster in clusters:
            if len(cluster) >= target_size:
                if current:
                    batches.append(current)
                    current = []
                batches.append(cluster)
                continue
            if current and len(current) + len(cluster) > target_size:
                batches.append(current)
                current = []
            current.extend(cluster)
        if current:
            batches.append(current)
        return batches

    @staticmethod
    def _batch_budget(
        budget: BudgetConfig,
        batch_ids: list[str],
        all_requirement_ids: list[str],
    ) -> BudgetConfig:
        """Give a batch its own share of the test budget, not the project's whole budget.

        Sending the full budget to every batch asks each one for as many tests as the
        entire project may contain, so N batches produce roughly N times the plan and the
        trim discards the rest. That was merely wasteful at a budget of ten; at thirty it
        makes each response large enough to exhaust the model's token ceiling, which is
        what made batched planning appear unable to fill a larger budget.

        The share is proportional to the batch's requirements, rounded up, and never
        below the three items the quality gate requires of any plan.
        """

        total = len(all_requirement_ids) or 1
        share = math.ceil(budget.max_tests_per_iteration * len(batch_ids) / total)
        capped = max(3, min(budget.max_tests_per_iteration, share))
        return budget.model_copy(update={"max_tests_per_iteration": capped})

    @staticmethod
    def _batch_scope(
        requirement_ids: list[str],
        requirements_analysis: RequirementsAnalysis,
        api_analysis: APIAnalysis,
        operations: list[OpenAPIOperation],
        coverage: ProjectRequirementCoverage,
    ) -> tuple[RequirementsAnalysis, APIAnalysis, list[OpenAPIOperation]]:
        """Scope requirements/API analysis/operations down to one batch, including prerequisite
        operations pulled in via dependency-edge closure (e.g. authentication setup)."""

        id_set = set(requirement_ids)
        operations_by_requirement = {
            match.requirement_id: {(ref.method, ref.path) for ref in match.matched_operations}
            for match in coverage.matches
        }
        operation_keys: set[tuple[str, str]] = set()
        for requirement_id in requirement_ids:
            operation_keys |= operations_by_requirement.get(requirement_id, set())

        changed = True
        while changed:
            changed = False
            for edge in api_analysis.dependency_edges:
                dependent = (edge.dependent_method, edge.dependent_path)
                prerequisite = (edge.prerequisite_method, edge.prerequisite_path)
                if dependent in operation_keys and prerequisite not in operation_keys:
                    operation_keys.add(prerequisite)
                    changed = True

        batch_requirements = requirements_analysis.model_copy(
            update={
                "requirements": [
                    item for item in requirements_analysis.requirements if item.id in id_set
                ]
            }
        )
        batch_operations = [
            operation
            for operation in operations
            if (operation.method, operation.path) in operation_keys
        ]
        batch_api = api_analysis.model_copy(
            update={
                "operations": [
                    item
                    for item in api_analysis.operations
                    if (item.method, item.path) in operation_keys
                ],
                "dependency_edges": [
                    edge
                    for edge in api_analysis.dependency_edges
                    if (edge.prerequisite_method, edge.prerequisite_path) in operation_keys
                    and (edge.dependent_method, edge.dependent_path) in operation_keys
                ],
            }
        )
        return batch_requirements, batch_api, batch_operations

    @classmethod
    def _finalize_strategy(
        cls,
        strategy: list[TestStrategyItem],
        requirements_analysis: RequirementsAnalysis,
        api_analysis: APIAnalysis,
        operations: list[OpenAPIOperation],
        budget: BudgetConfig,
        *,
        synthesize_missing_types: bool = False,
    ) -> list[TestStrategyItem]:
        """Reconcile a draft against the contract.

        ``synthesize_missing_types`` is a last resort, enabled only on a final attempt:
        filling a missing required type deterministically would otherwise rob the
        corrective call of its purpose, which is to make the *model* produce a real test
        rather than a template.
        """

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

        # A required test type can go missing because the model's only item of that type
        # named an endpoint the contract does not document, and normalization dropped it.
        # Rebuild it from a real operation rather than failing the project, exactly as
        # the stateful item above is rebuilt when the model omits it.
        if synthesize_missing_types and budget.max_tests_per_iteration >= 3:
            missing = [
                test_type
                for test_type in cls._REQUIRED_TEST_TYPES
                if not any(item.test_type == test_type for item in normalized)
            ]
            synthesized = [
                item
                for item in (
                    cls._required_type_item(test_type, requirements_analysis, operations)
                    for test_type in missing
                )
                if item is not None
            ]
            if synthesized:
                _logger.warning(
                    "Synthesized %d strategy item(s) for required test type(s) the strategy "
                    "was missing: %s",
                    len(synthesized),
                    ", ".join(item.test_type for item in synthesized),
                )
                normalized = cls._normalize_strategy(normalized + synthesized, operations)
        return cls._trim_to_budget(normalized, budget.max_tests_per_iteration)

    @classmethod
    def _required_type_item(
        cls,
        test_type: str,
        requirements_analysis: RequirementsAnalysis,
        operations: list[OpenAPIOperation],
    ) -> TestStrategyItem | None:
        """Build a deterministic item of one required test type from a real operation."""

        if not operations or not requirements_analysis.requirements:
            return None
        operation = cls._operation_for_type(test_type, operations)
        success_codes = [
            code for code in operation.response_codes if not code.startswith(("4", "5"))
        ]
        rejection_codes = [code for code in operation.response_codes if code.startswith("4")]

        if test_type == "negative":
            codes = rejection_codes or ["400"]
            prompt = (
                f"Generate an independent negative test that calls {operation.method} "
                f"{operation.path} with invalid, missing, or unauthorized input and asserts "
                "the request is rejected."
            )
        elif test_type == "edge_case":
            codes = success_codes or operation.response_codes or ["200"]
            prompt = (
                f"Generate an independent edge-case test that calls {operation.method} "
                f"{operation.path} with boundary values and asserts the documented behavior."
            )
        else:
            codes = success_codes or operation.response_codes or ["200"]
            prompt = (
                f"Generate an independent happy-path test that calls {operation.method} "
                f"{operation.path} with valid input and asserts a successful response."
            )

        requirement = cls._best_requirement(
            requirements_analysis.requirements,
            " ".join(filter(None, [operation.path, operation.summary, operation.description])),
        )
        return TestStrategyItem(
            requirement_id=requirement.id,
            requirement_summary=requirement.text,
            api_endpoint=operation.path,
            http_method=operation.method,
            prompt=prompt,
            test_type=test_type,
            priority="medium",
            auth_role=requirement.role if operation.auth_required else None,
            expected_status_codes=codes,
            rationale=(
                f"Synthesized from the OpenAPI contract: the strategy contained no "
                f"{test_type} test."
            ),
        )

    @staticmethod
    def _operation_for_type(
        test_type: str,
        operations: list[OpenAPIOperation],
    ) -> OpenAPIOperation:
        """Pick the operation that best supports a given test type, by documented shape."""

        if test_type == "negative":
            # An operation documenting a 4xx gives the test a status code to assert.
            return next(
                (
                    operation
                    for operation in operations
                    if any(code.startswith("4") for code in operation.response_codes)
                ),
                operations[0],
            )
        if test_type == "edge_case":
            # Boundary values need somewhere to put them: a parameter or a request body.
            return next(
                (
                    operation
                    for operation in operations
                    if operation.parameters or operation.request_body_schema
                ),
                operations[0],
            )
        return next(
            (operation for operation in operations if operation.method == "GET"),
            operations[0],
        )

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
        """Drop items the OpenAPI does not support, then add deterministic setup/cleanup."""

        operation_map = {(operation.method, operation.path): operation for operation in operations}
        mutating_methods = {"POST", "PUT", "PATCH", "DELETE"}
        normalized: list[TestStrategyItem] = []
        hallucinated: list[str] = []
        for item in strategy:
            operation = operation_map.get((item.http_method, item.api_endpoint))
            # An item naming an endpoint the contract does not document is not merely
            # unverified, it is untestable: the generated test would call a URL that
            # does not exist. Drop it here so one invented endpoint cannot fail a whole
            # project, and let the quality gate judge whatever survives.
            if operation is None:
                hallucinated.append(f"{item.http_method} {item.api_endpoint}")
                continue
            setup = list(item.setup_needed)
            cleanup = item.cleanup_strategy

            if operation.auth_required:
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
        if hallucinated:
            _logger.warning(
                "Dropped %d strategy item(s) naming operations absent from the OpenAPI "
                "document: %s",
                len(hallucinated),
                ", ".join(sorted(set(hallucinated))),
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
