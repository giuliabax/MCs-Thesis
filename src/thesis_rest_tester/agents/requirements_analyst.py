"""Requirements analyst planning agent."""

import re
from pathlib import Path

from pydantic import TypeAdapter

from thesis_rest_tester.agents.base import BaseAgent
from thesis_rest_tester.artifacts.writer import ArtifactWriter
from thesis_rest_tester.domain.models import AgentOutput, RequirementItem
from thesis_rest_tester.domain.schemas import RequirementsAnalysis, SourceRequirement
from thesis_rest_tester.llm.base import LLMClient


class RequirementsAnalystAgent(BaseAgent[RequirementsAnalysis]):
    def __init__(
        self,
        llm_client: LLMClient,
        prompt_path: str | Path,
        artifact_writer: ArtifactWriter,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> None:
        super().__init__(
            name="requirements_analyst",
            prompt_path=prompt_path,
            llm_client=llm_client,
            artifact_writer=artifact_writer,
            response_adapter=TypeAdapter(RequirementsAnalysis),
            raw_artifact_name="requirements_analysis.raw.txt",
            temperature=temperature,
            max_tokens=max_tokens,
        )

    def run(
        self,
        compact_requirements: str,
        source_requirements: list[SourceRequirement] | None = None,
    ) -> tuple[RequirementsAnalysis, AgentOutput]:
        analysis, output = self.call_and_validate(
            "Analyze the following Participium requirements corpus. Return only strict JSON.\n\n"
            + compact_requirements
        )
        if not source_requirements:
            return analysis, output

        reconciled = self._reconcile(analysis, source_requirements)
        return reconciled, output.model_copy(
            update={"parsed_json": reconciled.model_dump(mode="json")}
        )

    @classmethod
    def _reconcile(
        cls,
        analysis: RequirementsAnalysis,
        source_requirements: list[SourceRequirement],
    ) -> RequirementsAnalysis:
        generated_by_id = {item.id: item for item in analysis.requirements}
        if len(generated_by_id) != len(analysis.requirements):
            raise ValueError("Requirements Analyst returned duplicate requirement IDs")

        reconciled: list[RequirementItem] = []
        for source in source_requirements:
            generated = generated_by_id.get(source.id)
            matches_source = generated is not None and cls._text_similarity(
                source.text,
                generated.text,
            ) >= 0.25
            generated_constraints = generated.constraints if matches_source else []
            generated_behaviors = generated.expected_behaviors if matches_source else []
            reconciled.append(
                RequirementItem(
                    id=source.id,
                    source=source.source,
                    text=source.text,
                    role=source.role,
                    business_value=source.business_value,
                    constraints=cls._unique([*source.constraints, *generated_constraints]),
                    expected_behaviors=cls._unique(
                        [*source.expected_behaviors, *generated_behaviors]
                    ),
                )
            )

        roles = cls._unique([*(item.role for item in reconciled), *analysis.roles])
        return analysis.model_copy(update={"requirements": reconciled, "roles": roles})

    @staticmethod
    def _text_similarity(left: str, right: str) -> float:
        stop_words = {"that", "with", "from", "this", "their", "they", "want", "must"}

        def tokens(value: str) -> set[str]:
            return {
                token
                for token in re.findall(r"[a-z0-9]+", value.lower())
                if len(token) > 2 and token not in stop_words
            }

        left_tokens = tokens(left)
        right_tokens = tokens(right)
        if not left_tokens or not right_tokens:
            return 0.0
        return len(left_tokens & right_tokens) / min(len(left_tokens), len(right_tokens))

    @staticmethod
    def _unique(values: list[str]) -> list[str]:
        return list(dict.fromkeys(value for value in values if value))
