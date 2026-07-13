"""Match the shared requirement catalogue against one project's OpenAPI contract."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import TypeAdapter

from thesis_rest_tester.agents.base import BaseAgent
from thesis_rest_tester.artifacts.writer import ArtifactWriter
from thesis_rest_tester.domain.coverage import (
    OperationReference,
    ProjectRequirementCoverage,
    RequirementAPIMatch,
    RequirementCoverageDraft,
)
from thesis_rest_tester.domain.models import AgentOutput, OpenAPIOperation
from thesis_rest_tester.domain.schemas import RequirementsAnalysis
from thesis_rest_tester.llm.base import LLMClient


class RequirementAPIMatcherAgent(BaseAgent[RequirementCoverageDraft]):
    def __init__(
        self,
        llm_client: LLMClient,
        prompt_path: str | Path,
        artifact_writer: ArtifactWriter,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> None:
        super().__init__(
            name="requirement_api_matcher",
            prompt_path=prompt_path,
            llm_client=llm_client,
            artifact_writer=artifact_writer,
            response_adapter=TypeAdapter(RequirementCoverageDraft),
            raw_artifact_name="requirement_coverage.raw.txt",
            temperature=temperature,
            max_tokens=max_tokens,
        )

    def _preprocess_parsed_json(self, parsed: object) -> object:
        if not isinstance(parsed, dict) or not isinstance(parsed.get("matches"), list):
            return parsed

        normalized_matches = []
        for match in parsed["matches"]:
            if not isinstance(match, dict):
                normalized_matches.append(match)
                continue

            evidence = (
                list(match.get("evidence", []))
                if isinstance(match.get("evidence"), list)
                else []
            )
            normalized_operations = []
            operations = match.get("matched_operations", [])
            if isinstance(operations, list):
                for operation in operations:
                    if not isinstance(operation, dict):
                        normalized_operations.append(operation)
                        continue
                    operation_evidence = operation.get("evidence")
                    if isinstance(operation_evidence, list):
                        evidence.extend(str(item) for item in operation_evidence if item)
                    elif isinstance(operation_evidence, str) and operation_evidence:
                        evidence.append(operation_evidence)
                    normalized_operations.append(
                        {
                            "method": operation.get("method"),
                            "path": operation.get("path"),
                            "operation_id": operation.get("operation_id"),
                        }
                    )

            normalized = dict(match)
            normalized["matched_operations"] = normalized_operations
            normalized["evidence"] = list(dict.fromkeys(evidence))
            normalized_matches.append(normalized)

        return {**parsed, "matches": normalized_matches}

    def run(
        self,
        project_name: str,
        openapi_path: str | Path,
        requirements_analysis: RequirementsAnalysis,
        operations: list[OpenAPIOperation],
    ) -> tuple[ProjectRequirementCoverage, AgentOutput]:
        payload = {
            "project_name": project_name,
            "requirements": [
                {
                    "id": requirement.id,
                    "text": requirement.text,
                    "role": requirement.role,
                    "expected_behaviors": requirement.expected_behaviors,
                }
                for requirement in requirements_analysis.requirements
            ],
            "openapi_operations": [
                {
                    "method": operation.method,
                    "path": operation.path,
                    "operation_id": operation.operation_id,
                    "summary": operation.summary,
                    "description": operation.description,
                    "tags": operation.tags,
                    "parameters": [
                        str(parameter.get("name", parameter.get("$ref", "unnamed")))
                        for parameter in operation.parameters
                    ],
                    "request_body_fields": list(
                        (operation.request_body_schema or {}).get("properties", {})
                    ),
                    "response_codes": operation.response_codes,
                    "auth_required": operation.auth_required,
                }
                for operation in operations
            ],
        }
        draft, output = self.call_and_validate(
            "Match every shared requirement against this project's OpenAPI operations. "
            "Return only strict JSON.\n\n"
            + json.dumps(payload, ensure_ascii=False)
        )
        report = self._reconcile(
            project_name,
            str(openapi_path),
            requirements_analysis,
            operations,
            draft,
        )
        return report, output.model_copy(
            update={"parsed_json": report.model_dump(mode="json")}
        )

    @staticmethod
    def _reconcile(
        project_name: str,
        openapi_path: str,
        requirements_analysis: RequirementsAnalysis,
        operations: list[OpenAPIOperation],
        draft: RequirementCoverageDraft,
    ) -> ProjectRequirementCoverage:
        warnings: list[str] = []
        requirement_ids = [item.id for item in requirements_analysis.requirements]
        valid_requirement_ids = set(requirement_ids)
        operation_map = {(item.method, item.path): item for item in operations}

        generated: dict[str, RequirementAPIMatch] = {}
        for match in draft.matches:
            if match.requirement_id not in valid_requirement_ids:
                warnings.append(
                    f"Ignored unknown requirement ID returned by the model: {match.requirement_id}"
                )
                continue
            if match.requirement_id in generated:
                warnings.append(
                    f"Ignored duplicate assessment for requirement: {match.requirement_id}"
                )
                continue

            valid_references: list[OperationReference] = []
            seen_operations: set[tuple[str, str]] = set()
            for reference in match.matched_operations:
                key = (reference.method, reference.path)
                operation = operation_map.get(key)
                if operation is None:
                    warnings.append(
                        f"Removed operation absent from {project_name} OpenAPI: "
                        f"{reference.method} {reference.path}"
                    )
                    continue
                if key not in seen_operations:
                    seen_operations.add(key)
                    valid_references.append(
                        OperationReference(
                            method=operation.method,
                            path=operation.path,
                            operation_id=operation.operation_id,
                        )
                    )

            status = match.status
            rationale = match.rationale
            if status in {"not_implemented", "not_assessable"} and valid_references:
                warnings.append(
                    f"Cleared contradictory operation matches for {match.requirement_id}"
                )
                valid_references = []
            elif status in {"implemented", "partially_implemented"} and not valid_references:
                warnings.append(
                    f"Downgraded {match.requirement_id} to not_assessable because no valid "
                    "operation evidence was supplied"
                )
                status = "not_assessable"
                rationale = rationale + " No valid operation evidence was supplied."

            generated[match.requirement_id] = match.model_copy(
                update={
                    "status": status,
                    "matched_operations": valid_references,
                    "rationale": rationale,
                }
            )

        matches: list[RequirementAPIMatch] = []
        for requirement_id in requirement_ids:
            match = generated.get(requirement_id)
            if match is None:
                warnings.append(
                    f"Model omitted {requirement_id}; recorded as not_assessable"
                )
                match = RequirementAPIMatch(
                    requirement_id=requirement_id,
                    status="not_assessable",
                    matched_operations=[],
                    evidence=[],
                    missing_behaviors=[],
                    rationale="The model did not return an assessment for this requirement.",
                )
            matches.append(match)

        return ProjectRequirementCoverage.from_matches(
            project_name=project_name,
            openapi_path=openapi_path,
            matches=matches,
            warnings=warnings,
        )
