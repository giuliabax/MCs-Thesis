"""Feedback Manager: turn diagnoses into instructions the next attempt can act on.

This is the one place in the evaluation stage where a model is used, and the division is
deliberate. Deciding *why* a test failed and *which stage* must act is done in code, so
that the classification is reproducible and auditable. Deciding *what to say* about it is
a writing task, and a model does it better than a template: the note that helps is the one
naming the concrete operation, field or credential involved, which no fixed string can.

The agent exists because the alternative is doing nothing. Re-issuing an unchanged prompt
to the same model at the same temperature reliably reproduces the same output, so without
an instruction that differs, a second iteration is an expensive way to obtain the first
one again.

Its output is advisory and is never trusted structurally: notes are attached to prompts,
and an item key that does not match a planned item is dropped rather than acted on.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import Field, TypeAdapter

from thesis_rest_tester.agents.base import BaseAgent
from thesis_rest_tester.artifacts.writer import ArtifactWriter
from thesis_rest_tester.domain.evaluation import ProjectEvaluation
from thesis_rest_tester.domain.models import AgentOutput, DomainModel
from thesis_rest_tester.llm.base import LLMClient

_logger = logging.getLogger(__name__)

# How many diagnoses of one cause are shown to the model. The notes should be specific,
# and twenty near-identical failures teach it no more than four do while crowding the
# context of a local model.
_MAX_EXAMPLES_PER_RULE = 4


class GenerationNote(DomainModel):
    """A correction addressed to one strategy item."""

    # Copied verbatim from the input; the pipeline joins on it.
    item: str
    note: str


class FeedbackNotes(DomainModel):
    """What the Feedback Manager returns for one project."""

    planning_note: str = ""
    generation_notes: list[GenerationNote] = Field(default_factory=list)

    def note_for(self, item_key: str) -> str | None:
        for entry in self.generation_notes:
            if entry.item == item_key:
                return entry.note
        return None


class FeedbackManagerAgent(BaseAgent[FeedbackNotes]):
    """One call per project, given only the failures that planning or generation can fix."""

    def __init__(
        self,
        llm_client: LLMClient,
        prompt_path: str | Path,
        artifact_writer: ArtifactWriter,
        temperature: float | None = None,
        max_tokens: int | None = None,
        think: bool = False,
    ) -> None:
        super().__init__(
            name="feedback_manager",
            prompt_path=prompt_path,
            llm_client=llm_client,
            artifact_writer=artifact_writer,
            response_adapter=TypeAdapter(FeedbackNotes),
            raw_artifact_name="feedback.raw.txt",
            temperature=temperature,
            max_tokens=max_tokens,
            think=think,
        )

    def run(self, evaluation: ProjectEvaluation) -> tuple[FeedbackNotes, AgentOutput]:
        payload = _payload(evaluation)
        notes, output = self.call_and_validate(
            "Write the corrections for this project's next attempt. Return only a strict "
            "JSON object.\n\n" + json.dumps(payload, ensure_ascii=False)
        )
        notes = _keep_known_items(notes, evaluation)
        return notes, output.model_copy(update={"parsed_json": notes.model_dump(mode="json")})


def _payload(evaluation: ProjectEvaluation) -> dict[str, object]:
    """What the model is shown.

    Only the actionable diagnoses. A failure attributed to the environment, to a defect in
    the service, or to a contract the service contradicts cannot be repaired by planning or
    generation, and showing them would invite exactly the note that must never be written:
    one telling a test to lower its expectations until a broken service passes.
    """

    actionable = [
        diagnosis
        for diagnosis in evaluation.diagnoses
        if diagnosis.cause in {"planning", "generation"}
    ]
    seen: dict[str, int] = {}
    shown = []
    for diagnosis in actionable:
        count = seen.get(diagnosis.rule, 0)
        if count >= _MAX_EXAMPLES_PER_RULE:
            continue
        seen[diagnosis.rule] = count + 1
        shown.append(
            {
                "test_name": diagnosis.test_name,
                "requirement_id": diagnosis.requirement_id,
                "cause": diagnosis.cause,
                "rule": diagnosis.rule,
                "evidence": diagnosis.evidence,
            }
        )
    return {
        "project": evaluation.project_name,
        "requirements_to_replan": evaluation.replan_requirements,
        "items_to_regenerate": evaluation.regenerate_items,
        "diagnoses": shown,
        "diagnosis_counts": evaluation.cause_counts,
    }


def _keep_known_items(notes: FeedbackNotes, evaluation: ProjectEvaluation) -> FeedbackNotes:
    """Drop notes addressed to items that were never asked about.

    A note whose key does not match a planned item cannot be attached to anything, and
    silently keeping it would make the report claim a correction that no prompt will ever
    carry. Models do paraphrase keys, which is why the prompt insists on copying them.
    """

    known = set(evaluation.regenerate_items)
    kept = [entry for entry in notes.generation_notes if entry.item in known]
    dropped = [entry.item for entry in notes.generation_notes if entry.item not in known]
    if dropped:
        _logger.warning(
            "Discarding %d feedback note(s) for %s naming unknown items: %s",
            len(dropped),
            evaluation.project_name,
            "; ".join(dropped[:3]),
        )
    return notes.model_copy(update={"generation_notes": kept})
