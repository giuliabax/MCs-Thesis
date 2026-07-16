"""Multi-project planning workflow orchestrator."""

from __future__ import annotations

import csv
import io
import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from thesis_rest_tester.agents import (
    APIUnderstandingAgent,
    RequirementAPIMatcherAgent,
    RequirementsAnalystAgent,
    TestStrategyPlannerAgent,
)
from thesis_rest_tester.artifacts.writer import ArtifactWriter
from thesis_rest_tester.config import AgentLLMOverride, AppConfig, ProjectInputConfig, load_config
from thesis_rest_tester.domain.coverage import ProjectRequirementCoverage
from thesis_rest_tester.domain.models import OpenAPIOperation, WorkflowPlan
from thesis_rest_tester.domain.schemas import APIAnalysis, RequirementsAnalysis, SourceRequirement
from thesis_rest_tester.llm import GroqLLMClient, LLMClient, LMStudioLLMClient, MockLLMClient
from thesis_rest_tester.loaders import OpenAPILoader, RequirementsLoader


@dataclass(frozen=True, slots=True)
class OrchestrationResult:
    run_id: str
    output_dir: Path
    workflow_plans: dict[str, WorkflowPlan]

    @property
    def workflow_plan(self) -> WorkflowPlan:
        """Return the first plan for compatibility with single-project callers."""

        return next(iter(self.workflow_plans.values()))


@dataclass(frozen=True, slots=True)
class _LoadedProject:
    config: ProjectInputConfig
    operations: list[OpenAPIOperation]


class Orchestrator:
    def __init__(
        self,
        config_path: str | Path,
        *,
        dry_run: bool = False,
        llm_client: LLMClient | None = None,
        prompt_root: str | Path | None = None,
    ) -> None:
        self._config_path = Path(config_path)
        self._dry_run = dry_run
        self._injected_llm_client = llm_client
        repository_root = Path(__file__).resolve().parents[2]
        self._prompt_root = (
            Path(prompt_root) if prompt_root is not None else repository_root / "prompts"
        )
        self._logger = logging.getLogger(__name__)

    def run(self) -> OrchestrationResult:
        config = load_config(self._config_path)
        if config.run_id is None:
            raise RuntimeError("Configuration has no run_id")

        output_dir = config.output.runs_dir / config.run_id
        root_writer = ArtifactWriter(output_dir)
        root_writer.write_yaml("config.resolved.yaml", config)

        requirements_config = config.inputs.requirements
        corpus = RequirementsLoader().load(
            requirements_config.description_pdf,
            requirements_config.user_stories_xlsx,
            requirements_config.faq_pdf,
        )
        root_writer.write_text("requirements_compact.txt", corpus.compact_text)

        loaded_projects = self._load_projects(config)
        default_client = self._select_llm_client(
            config,
            loaded_projects,
            corpus.compact_text,
            corpus.source_requirements,
        )
        agent_clients = self._resolve_agent_clients(config, default_client)

        requirements_agent = RequirementsAnalystAgent(
            prompt_path=self._prompt_root / "planning/requirements_analyst.md",
            llm_client=agent_clients["requirements_analyst"],
            artifact_writer=root_writer,
            temperature=config.llm.temperature,
            max_tokens=config.llm.max_tokens,
            think="requirements_analyst" in config.llm.reasoning_agents,
        )
        requirements_analysis, _ = requirements_agent.run(
            corpus.compact_text,
            corpus.source_requirements,
        )
        root_writer.write_json("requirements_analysis.json", requirements_analysis)

        plans: dict[str, WorkflowPlan] = {}
        coverages: dict[str, ProjectRequirementCoverage] = {}
        for project in loaded_projects:
            project_writer = ArtifactWriter(output_dir / "projects" / project.config.name)
            plan, coverage = self._plan_project(
                config,
                project,
                requirements_analysis,
                agent_clients,
                project_writer,
            )
            plans[project.config.name] = plan
            coverages[project.config.name] = coverage

        root_writer.write_json(
            "requirement_coverage_matrix.json",
            {
                "run_id": config.run_id,
                "assessment_basis": "openapi_documentation",
                "projects": {
                    name: coverage.model_dump(mode="json")
                    for name, coverage in coverages.items()
                },
            },
        )
        root_writer.write_text(
            "requirement_coverage_matrix.csv",
            self._coverage_csv(requirements_analysis, coverages),
        )
        root_writer.write_text(
            "summary.md",
            self._summary(config, output_dir, requirements_analysis, plans, coverages),
        )

        self._logger.info("Planning workflow completed in %s", output_dir)
        return OrchestrationResult(config.run_id, output_dir, plans)

    @staticmethod
    def _load_projects(config: AppConfig) -> list[_LoadedProject]:
        loaded: list[_LoadedProject] = []
        for project in config.inputs.configured_projects(config.project_name):
            openapi = OpenAPILoader().load(project.openapi_path)
            if not openapi.operations:
                raise ValueError(
                    f"The OpenAPI/Swagger document for {project.name} contains no supported "
                    "HTTP operations"
                )
            loaded.append(_LoadedProject(project, openapi.operations))
        return loaded

    def _plan_project(
        self,
        config: AppConfig,
        project: _LoadedProject,
        shared_requirements: RequirementsAnalysis,
        agent_clients: dict[str, LLMClient],
        writer: ArtifactWriter,
    ) -> tuple[WorkflowPlan, ProjectRequirementCoverage]:
        writer.write_json(
            "openapi_operations.json",
            [operation.model_dump(mode="json") for operation in project.operations],
        )
        agent_arguments = {
            "artifact_writer": writer,
            "temperature": config.llm.temperature,
            "max_tokens": config.llm.max_tokens,
        }

        api_agent = APIUnderstandingAgent(
            prompt_path=self._prompt_root / "planning/api_understanding.md",
            llm_client=agent_clients["api_understanding"],
            think="api_understanding" in config.llm.reasoning_agents,
            **agent_arguments,
        )
        api_analysis, _ = api_agent.run(project.operations)
        writer.write_json("api_analysis.json", api_analysis)

        matcher = RequirementAPIMatcherAgent(
            prompt_path=self._prompt_root / "planning/requirement_api_matcher.md",
            llm_client=agent_clients["requirement_api_matcher"],
            think="requirement_api_matcher" in config.llm.reasoning_agents,
            **agent_arguments,
        )
        coverage, _ = matcher.run(
            project.config.name,
            project.config.openapi_path,
            shared_requirements,
            project.operations,
        )
        writer.write_json("requirement_coverage.json", coverage)

        scoped_requirements, scoped_api, scoped_operations = self._project_scope(
            shared_requirements,
            api_analysis,
            project.operations,
            coverage,
        )
        strategy_items = []
        if scoped_requirements.requirements and scoped_operations:
            strategy_agent = TestStrategyPlannerAgent(
                prompt_path=self._prompt_root / "planning/test_strategy_planner.md",
                llm_client=agent_clients["test_strategy_planner"],
                # Batching keeps each local request within the context window; the mock
                # dry run returns a single fixture per project, so plan in one call there.
                batch_by_requirement=(
                    not self._dry_run
                    and self._effective_provider(config, "test_strategy_planner") == "lmstudio"
                ),
                think="test_strategy_planner" in config.llm.reasoning_agents,
                **agent_arguments,
            )
            strategy_items, _ = strategy_agent.run(
                scoped_requirements,
                scoped_api,
                scoped_operations,
                config.budget,
                coverage,
            )
        else:
            writer.write_text(
                "test_strategy.raw.txt",
                "No strategy call: no requirement-to-operation match was available.\n",
            )
        writer.write_json(
            "test_strategy.json",
            [item.model_dump(mode="json") for item in strategy_items],
        )

        risks = list(api_analysis.risks)
        risks.append(
            "Requirement coverage is inferred from OpenAPI documentation and must be verified "
            "against the running SUT during execution."
        )
        if coverage.validation_warnings:
            risks.append(
                f"Coverage reconciliation produced {len(coverage.validation_warnings)} warning(s)."
            )
        plan = WorkflowPlan(
            run_id=config.run_id,
            project_name=project.config.name,
            requirements_summary=scoped_requirements.model_dump(mode="json"),
            api_summary=api_analysis.model_dump(mode="json"),
            strategy_items=strategy_items,
            assumptions=shared_requirements.assumptions,
            risks=risks,
            created_at=datetime.now(UTC),
            sut_base_url=project.config.sut_base_url,
            requirement_coverage=coverage.model_dump(mode="json"),
        )
        writer.write_json("workflow_plan.json", plan)
        writer.write_text("summary.md", self._project_summary(plan, coverage))
        return plan, coverage

    @staticmethod
    def _project_scope(
        requirements: RequirementsAnalysis,
        api_analysis: APIAnalysis,
        operations: list[OpenAPIOperation],
        coverage: ProjectRequirementCoverage,
    ) -> tuple[RequirementsAnalysis, APIAnalysis, list[OpenAPIOperation]]:
        eligible = {
            match.requirement_id
            for match in coverage.matches
            if match.status in {"implemented", "partially_implemented"}
        }
        operation_keys = {
            (reference.method, reference.path)
            for match in coverage.matches
            if match.requirement_id in eligible
            for reference in match.matched_operations
        }

        changed = True
        while changed:
            changed = False
            for edge in api_analysis.dependency_edges:
                dependent = (edge.dependent_method, edge.dependent_path)
                prerequisite = (edge.prerequisite_method, edge.prerequisite_path)
                if dependent in operation_keys and prerequisite not in operation_keys:
                    operation_keys.add(prerequisite)
                    changed = True

        scoped_requirements = requirements.model_copy(
            update={
                "requirements": [item for item in requirements.requirements if item.id in eligible]
            }
        )
        scoped_operations = [
            operation
            for operation in operations
            if (operation.method, operation.path) in operation_keys
        ]
        scoped_api = api_analysis.model_copy(
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
        return scoped_requirements, scoped_api, scoped_operations

    def _select_llm_client(
        self,
        config: AppConfig,
        projects: list[_LoadedProject],
        requirements_compact: str,
        source_requirements: list[SourceRequirement],
    ) -> LLMClient:
        if self._injected_llm_client is not None:
            return self._injected_llm_client
        if self._dry_run:
            return MockLLMClient(
                self._mock_responses(
                    config,
                    projects,
                    requirements_compact,
                    source_requirements,
                )
            )
        if config.llm.provider == "lmstudio":
            return LMStudioLLMClient(
                model=config.llm.model,
                base_url=config.llm.base_url,
                default_temperature=config.llm.temperature,
                default_max_tokens=config.llm.max_tokens,
                timeout=config.llm.timeout_seconds,
            )
        return GroqLLMClient(
            model=config.llm.model,
            default_temperature=config.llm.temperature,
            default_max_tokens=config.llm.max_tokens,
        )

    def _resolve_agent_clients(
        self, config: AppConfig, default_client: LLMClient
    ) -> dict[str, LLMClient]:
        """Map each planning agent to its client, honoring per-agent provider overrides."""

        agent_names = (
            "requirements_analyst",
            "api_understanding",
            "requirement_api_matcher",
            "test_strategy_planner",
        )
        # A dry run or an injected client bypasses per-agent routing entirely.
        if self._injected_llm_client is not None or self._dry_run:
            return {name: default_client for name in agent_names}
        cache: dict[tuple[str, str, str | None], LLMClient] = {}
        clients: dict[str, LLMClient] = {}
        for name in agent_names:
            override = config.llm.overrides.get(name)
            if override is None:
                clients[name] = default_client
                continue
            key = (override.provider, override.model, override.base_url)
            if key not in cache:
                cache[key] = self._build_override_client(config, override)
            clients[name] = cache[key]
        return clients

    @staticmethod
    def _build_override_client(config: AppConfig, override: AgentLLMOverride) -> LLMClient:
        if override.provider == "lmstudio":
            return LMStudioLLMClient(
                model=override.model,
                base_url=override.base_url or config.llm.base_url,
                default_temperature=config.llm.temperature,
                default_max_tokens=config.llm.max_tokens,
                timeout=config.llm.timeout_seconds,
            )
        return GroqLLMClient(
            model=override.model,
            default_temperature=config.llm.temperature,
            default_max_tokens=config.llm.max_tokens,
        )

    @staticmethod
    def _effective_provider(config: AppConfig, agent_name: str) -> str:
        override = config.llm.overrides.get(agent_name)
        return override.provider if override is not None else config.llm.provider

    @classmethod
    def _mock_responses(
        cls,
        config: AppConfig,
        projects: list[_LoadedProject],
        compact_text: str,
        source_requirements: list[SourceRequirement],
    ) -> list[str]:
        requirement_items = source_requirements or [
            SourceRequirement(
                id="DRY-REQ-001",
                source="dry-run",
                text="Exercise a documented API operation.",
                role="unspecified",
            )
        ]
        requirements = {
            "summary": f"Dry-run analysis of a {len(compact_text)}-character corpus.",
            "requirements": [item.model_dump(mode="json") for item in requirement_items],
            "roles": list(dict.fromkeys(item.role for item in requirement_items)),
            "domain_rules": ["Each test plans independent setup and cleanup."],
            "edge_cases": ["Invalid or boundary input."],
            "assumptions": ["Dry-run outputs are deterministic fixtures."],
        }
        responses = [json.dumps(requirements, ensure_ascii=False)]
        for project in projects:
            api = cls._mock_api(project.operations)
            coverage = cls._mock_coverage(requirement_items, project.operations)
            eligible_ids = {
                match["requirement_id"]
                for match in coverage["matches"]
                if match["status"] in {"implemented", "partially_implemented"}
            }
            relevant_keys = {
                (reference["method"], reference["path"])
                for match in coverage["matches"]
                if match["requirement_id"] in eligible_ids
                for reference in match["matched_operations"]
            }
            relevant_operations = [
                operation
                for operation in project.operations
                if (operation.method, operation.path) in relevant_keys
            ]
            relevant_requirements = [
                requirement
                for requirement in requirement_items
                if requirement.id in eligible_ids
            ]
            responses.extend(
                [
                    json.dumps(api, ensure_ascii=False),
                    json.dumps(coverage, ensure_ascii=False),
                ]
            )
            if relevant_operations and relevant_requirements:
                responses.append(
                    json.dumps(
                        cls._mock_strategy(config, relevant_requirements, relevant_operations),
                        ensure_ascii=False,
                    )
                )
        return responses

    @staticmethod
    def _mock_api(operations: list[OpenAPIOperation]) -> dict[str, object]:
        return {
            "summary": f"Dry-run analysis of {len(operations)} operation(s).",
            "operations": [
                {
                    "path": item.path,
                    "method": item.method,
                    "operation_id": item.operation_id,
                    "auth_required": item.auth_required,
                    "dependencies": [],
                    "notes": [item.summary] if item.summary else [],
                }
                for item in operations
            ],
            "authentication_notes": [],
            "dependencies": [],
            "dependency_edges": [],
            "risks": ["Dry-run relationships are illustrative."],
        }

    @staticmethod
    def _mock_coverage(
        requirements: list[SourceRequirement],
        operations: list[OpenAPIOperation],
    ) -> dict[str, object]:
        generic_terms = {
            "participium",
            "report",
            "reports",
            "system",
            "user",
            "users",
            "citizen",
            "want",
        }

        def tokens(value: str) -> set[str]:
            return {
                token
                for token in re.findall(r"[a-z0-9]+", value.lower())
                if len(token) > 3 and token not in generic_terms
            }

        operation_tokens = [
            tokens(
                " ".join(
                    filter(None, [item.path, item.operation_id, item.summary, item.description])
                )
            )
            for item in operations
        ]
        matches = []
        any_match = False
        for requirement in requirements:
            requirement_tokens = tokens(requirement.text)
            indexes = [
                index
                for index, candidate_tokens in enumerate(operation_tokens)
                if len(requirement_tokens & candidate_tokens) >= 2
            ]
            any_match = any_match or bool(indexes)
            matches.append(
                {
                    "requirement_id": requirement.id,
                    "status": "implemented" if indexes else "not_assessable",
                    "matched_operations": [
                        {
                            "method": operations[index].method,
                            "path": operations[index].path,
                            "operation_id": operations[index].operation_id,
                        }
                        for index in indexes
                    ],
                    "evidence": ["Deterministic dry-run lexical overlap"] if indexes else [],
                    "missing_behaviors": [],
                    "rationale": "Deterministic mock assessment; not a semantic conclusion.",
                }
            )
        if not any_match and matches and operations:
            matches[0]["status"] = "implemented"
            matches[0]["matched_operations"] = [
                {
                    "method": operations[0].method,
                    "path": operations[0].path,
                    "operation_id": operations[0].operation_id,
                }
            ]
            matches[0]["evidence"] = ["Synthetic dry-run mapping"]
        return {"matches": matches}

    @staticmethod
    def _mock_strategy(
        config: AppConfig,
        requirements: list[SourceRequirement],
        operations: list[OpenAPIOperation],
    ) -> list[dict[str, object]]:
        target = min(
            config.budget.max_tests_per_iteration,
            max(3, math.ceil(config.budget.max_tests_per_iteration * 0.8)),
        )
        test_types = ["happy_path", "edge_case", "negative"]
        strategy = []
        for index in range(target):
            operation = operations[index % len(operations)]
            requirement = requirements[index % len(requirements)]
            test_type = test_types[index % len(test_types)]
            setup = []
            if operation.auth_required:
                setup.append("Authenticate as a user with the required role.")
            if "{" in operation.path:
                setup.append("Create the resource referenced by the path parameter.")
            cleanup = None
            if operation.method in {"POST", "PUT", "PATCH", "DELETE"}:
                cleanup = "Delete created resources or restore their previous state."
            codes = operation.response_codes or ["200"]
            strategy.append(
                {
                    "requirement_id": requirement.id,
                    "requirement_summary": requirement.text,
                    "api_endpoint": operation.path,
                    "http_method": operation.method,
                    "prompt": f"Generate an independent {test_type} requests test.",
                    "test_type": test_type,
                    "priority": "high" if index < 3 else "medium",
                    "auth_role": requirement.role if operation.auth_required else None,
                    "setup_needed": setup,
                    "cleanup_strategy": cleanup,
                    "expected_status_codes": codes,
                    "rationale": "Deterministic dry-run strategy item.",
                }
            )
        return strategy

    @staticmethod
    def _coverage_csv(
        requirements: RequirementsAnalysis,
        coverages: dict[str, ProjectRequirementCoverage],
    ) -> str:
        output = io.StringIO()
        project_names = list(coverages)
        writer = csv.writer(output)
        writer.writerow(["requirement_id", "requirement_summary", *project_names])
        status_maps = {
            name: {match.requirement_id: match.status for match in coverage.matches}
            for name, coverage in coverages.items()
        }
        for requirement in requirements.requirements:
            writer.writerow(
                [
                    requirement.id,
                    requirement.text,
                    *(
                        status_maps[name].get(requirement.id, "not_assessable")
                        for name in project_names
                    ),
                ]
            )
        return output.getvalue()

    @staticmethod
    def _project_summary(
        plan: WorkflowPlan,
        coverage: ProjectRequirementCoverage,
    ) -> str:
        counts = coverage.status_counts
        return (
            f"# Project plan: {plan.project_name}\n\n"
            f"- Shared requirements assessed: {coverage.requirements_total}\n"
            f"- Implemented in OpenAPI: {counts.get('implemented', 0)}\n"
            f"- Partially implemented in OpenAPI: {counts.get('partially_implemented', 0)}\n"
            f"- Not implemented in OpenAPI: {counts.get('not_implemented', 0)}\n"
            f"- Not assessable from OpenAPI: {counts.get('not_assessable', 0)}\n"
            f"- Strategy items: {len(plan.strategy_items)}\n"
            f"- Reconciliation warnings: {len(coverage.validation_warnings)}\n\n"
            "Statuses describe documentation-level OpenAPI evidence, not verified runtime "
            "behavior.\n"
        )

    @staticmethod
    def _summary(
        config: AppConfig,
        output_dir: Path,
        requirements: RequirementsAnalysis,
        plans: dict[str, WorkflowPlan],
        coverages: dict[str, ProjectRequirementCoverage],
    ) -> str:
        lines = [
            f"# Multi-project workflow: {config.project_name}",
            "",
            f"- Run ID: `{config.run_id}`",
            f"- Shared requirements analyzed once: {len(requirements.requirements)}",
            f"- Projects analyzed: {len(plans)}",
            f"- Output directory: `{output_dir}`",
            "",
            "## Project coverage",
            "",
        ]
        for name, coverage in coverages.items():
            counts = coverage.status_counts
            lines.append(
                f"- `{name}`: {counts.get('implemented', 0)} implemented, "
                f"{counts.get('partially_implemented', 0)} partial, "
                f"{counts.get('not_implemented', 0)} not implemented, "
                f"{counts.get('not_assessable', 0)} not assessable; "
                f"{len(plans[name].strategy_items)} strategy items"
            )
        lines.extend(
            [
                "",
                "Coverage is inferred from each OpenAPI document. Runtime execution is required ",
                "to prove that documented behavior is actually implemented.",
                "",
            ]
        )
        return "\n".join(lines)
