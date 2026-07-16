"""OpenAPI understanding planning agent."""

import json
from pathlib import Path

from pydantic import TypeAdapter

from thesis_rest_tester.agents.base import BaseAgent
from thesis_rest_tester.artifacts.writer import ArtifactWriter
from thesis_rest_tester.domain.models import AgentOutput, OpenAPIOperation
from thesis_rest_tester.domain.schemas import (
    APIAnalysis,
    APIDependency,
    APIOperationAnalysis,
)
from thesis_rest_tester.llm.base import LLMClient


class APIUnderstandingAgent(BaseAgent[APIAnalysis]):
    def __init__(
        self,
        llm_client: LLMClient,
        prompt_path: str | Path,
        artifact_writer: ArtifactWriter,
        temperature: float | None = None,
        max_tokens: int | None = None,
        think: bool = True,
    ) -> None:
        super().__init__(
            name="api_understanding",
            prompt_path=prompt_path,
            llm_client=llm_client,
            artifact_writer=artifact_writer,
            response_adapter=TypeAdapter(APIAnalysis),
            raw_artifact_name="api_analysis.raw.txt",
            temperature=temperature,
            max_tokens=max_tokens,
            think=think,
        )

    def run(self, operations: list[OpenAPIOperation]) -> tuple[APIAnalysis, AgentOutput]:
        payload = [operation.model_dump(mode="json") for operation in operations]
        analysis, output = self.call_and_validate(
            "Analyze these normalized OpenAPI operations. Return only strict JSON.\n\n"
            + json.dumps(payload, indent=2, ensure_ascii=False)
        )
        reconciled = self._reconcile(analysis, operations)
        return reconciled, output.model_copy(
            update={"parsed_json": reconciled.model_dump(mode="json")}
        )

    @classmethod
    def _reconcile(
        cls,
        analysis: APIAnalysis,
        operations: list[OpenAPIOperation],
    ) -> APIAnalysis:
        generated_by_key = {(item.method, item.path): item for item in analysis.operations}
        if len(generated_by_key) != len(analysis.operations):
            raise ValueError("API Understanding Agent returned duplicate operations")

        valid_keys = {(item.method, item.path) for item in operations}
        inferred_edges = cls._infer_dependencies(operations)
        model_edges = [
            edge
            for edge in analysis.dependency_edges
            if (edge.prerequisite_method, edge.prerequisite_path) in valid_keys
            and (edge.dependent_method, edge.dependent_path) in valid_keys
        ]
        edges = cls._deduplicate_edges([*inferred_edges, *model_edges])

        reconciled_operations: list[APIOperationAnalysis] = []
        for operation in operations:
            generated = generated_by_key.get((operation.method, operation.path))
            notes = list(generated.notes) if generated is not None else []
            if operation.summary and operation.summary not in notes:
                notes.insert(0, operation.summary)
            if operation.parameters:
                parameter_names = [
                    str(parameter.get("name", parameter.get("$ref", "unnamed")))
                    for parameter in operation.parameters
                ]
                notes.append("Parameters: " + ", ".join(parameter_names))
            if operation.request_body_schema is not None:
                notes.append("Request body is documented in OpenAPI")
            notes.append("Documented responses: " + ", ".join(operation.response_codes))
            prerequisites = [
                f"{edge.prerequisite_method} {edge.prerequisite_path}: {edge.reason}"
                for edge in edges
                if (edge.dependent_method, edge.dependent_path)
                == (operation.method, operation.path)
            ]
            if generated is not None:
                prerequisites.extend(generated.dependencies)
            reconciled_operations.append(
                APIOperationAnalysis(
                    path=operation.path,
                    method=operation.method,
                    operation_id=operation.operation_id,
                    auth_required=operation.auth_required,
                    dependencies=list(dict.fromkeys(prerequisites)),
                    notes=list(dict.fromkeys(notes)),
                )
            )

        dependency_summaries = [
            (
                f"{edge.prerequisite_method} {edge.prerequisite_path} -> "
                f"{edge.dependent_method} {edge.dependent_path}: {edge.reason}"
            )
            for edge in edges
        ]
        authentication_notes = list(analysis.authentication_notes)
        if any(operation.auth_required for operation in operations):
            authentication_notes.append(
                "Operations marked auth_required need an authenticated session during test setup."
            )
        return analysis.model_copy(
            update={
                "operations": reconciled_operations,
                "authentication_notes": list(dict.fromkeys(authentication_notes)),
                "dependencies": list(
                    dict.fromkeys([*analysis.dependencies, *dependency_summaries])
                ),
                "dependency_edges": edges,
            }
        )

    @staticmethod
    def _infer_dependencies(operations: list[OpenAPIOperation]) -> list[APIDependency]:
        keys = {(operation.method, operation.path) for operation in operations}
        edges: list[APIDependency] = []

        def add(
            prerequisite: tuple[str, str],
            dependent: tuple[str, str],
            dependency_type: str,
            reason: str,
        ) -> None:
            if prerequisite in keys and dependent in keys and prerequisite != dependent:
                edges.append(
                    APIDependency(
                        prerequisite_method=prerequisite[0],
                        prerequisite_path=prerequisite[1],
                        dependent_method=dependent[0],
                        dependent_path=dependent[1],
                        dependency_type=dependency_type,
                        reason=reason,
                    )
                )

        add(
            ("POST", "/auth/register-request"),
            ("POST", "/auth/verify-registration"),
            "state",
            "A pending registration and verification code must exist.",
        )
        add(
            ("POST", "/auth/verify-registration"),
            ("POST", "/auth/login"),
            "state",
            "A verified account is needed for credential-based login tests.",
        )

        for operation in operations:
            dependent = (operation.method, operation.path)
            if "{reportId}" in operation.path:
                add(
                    ("POST", "/reports"),
                    dependent,
                    "resource",
                    "A report must exist before an operation can address reportId.",
                )
            if "{userId}" in operation.path and operation.path.startswith("/admin/users"):
                add(
                    ("POST", "/admin/users"),
                    dependent,
                    "resource",
                    "A municipality user must exist before an operation can address userId.",
                )

        municipal_review = ("PUT", "/municipal/reports/{reportId}")
        for dependent in (
            ("GET", "/offices/reports/assigned"),
            ("PUT", "/offices/reports/{reportId}/status"),
            ("PUT", "/offices/reports/{reportId}/assign-external"),
            ("GET", "/offices/reports/{reportId}/companies"),
        ):
            add(
                municipal_review,
                dependent,
                "state",
                "The report must be approved and assigned to a technical office.",
            )
        add(
            ("PUT", "/offices/reports/{reportId}/assign-external"),
            ("PUT", "/external-maintainer/reports/{reportId}/status"),
            "state",
            "The report must be assigned to an external maintainer.",
        )
        add(
            ("POST", "/messages/reports/{reportId}"),
            ("GET", "/messages/reports/{reportId}"),
            "state",
            "At least one message must exist to verify message retrieval.",
        )
        return APIUnderstandingAgent._deduplicate_edges(edges)

    @staticmethod
    def _deduplicate_edges(edges: list[APIDependency]) -> list[APIDependency]:
        unique: dict[tuple[str, str, str, str, str], APIDependency] = {}
        for edge in edges:
            key = (
                edge.prerequisite_method,
                edge.prerequisite_path,
                edge.dependent_method,
                edge.dependent_path,
                edge.dependency_type,
            )
            unique.setdefault(key, edge)
        return list(unique.values())
