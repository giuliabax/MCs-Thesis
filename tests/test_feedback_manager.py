"""The Feedback Manager: what it is shown, and what it is allowed to return."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from thesis_rest_tester.agents.base import AgentResponseError
from thesis_rest_tester.agents.feedback_manager import FeedbackManagerAgent, _payload
from thesis_rest_tester.artifacts.writer import ArtifactWriter
from thesis_rest_tester.domain.evaluation import Diagnosis, ProjectEvaluation
from thesis_rest_tester.domain.models import MetricSnapshot
from thesis_rest_tester.llm import MockLLMClient

PROMPT = Path("prompts/evaluation/feedback_manager.md")


def _diagnosis(cause: str, rule: str, test_name: str = "test_x", requirement="PT01"):
    return Diagnosis(
        test_name=test_name,
        requirement_id=requirement,
        cause=cause,
        rule=rule,
        evidence=["evidence line"],
    )


def _evaluation(*diagnoses, regenerate=(), replan=()) -> ProjectEvaluation:
    return ProjectEvaluation(
        project_name="participium-teamNN",
        metrics=MetricSnapshot(iteration=1),
        diagnoses=list(diagnoses),
        replan_requirements=list(replan),
        regenerate_items=list(regenerate),
    )


def _agent(tmp_path: Path, response: dict) -> FeedbackManagerAgent:
    return FeedbackManagerAgent(
        llm_client=MockLLMClient([json.dumps(response)]),
        prompt_path=PROMPT,
        artifact_writer=ArtifactWriter(tmp_path),
    )


# --- what the model is shown -----------------------------------------------------------


def test_only_repairable_failures_reach_the_model() -> None:
    """Showing the rest would invite the one note that must never be written: telling a
    test to lower its expectations until a broken service passes."""

    evaluation = _evaluation(
        _diagnosis("generation", "authentication_missing"),
        _diagnosis("planning", "planned_endpoint_not_implemented"),
        _diagnosis("environment", "external_service_unavailable"),
        _diagnosis("sut_defect", "server_error"),
        _diagnosis("contract_mismatch", "conformant_body_rejected"),
    )

    shown = {entry["cause"] for entry in _payload(evaluation)["diagnoses"]}

    assert shown == {"generation", "planning"}


def test_repeated_diagnoses_of_one_rule_are_capped() -> None:
    """Twenty near-identical failures teach a local model no more than four, and crowd
    the context it has to write the notes in."""

    evaluation = _evaluation(
        *[
            _diagnosis("generation", "login_with_unregistered_credentials", f"test_{i}")
            for i in range(20)
        ]
    )

    assert len(_payload(evaluation)["diagnoses"]) == 4


def test_the_payload_carries_what_has_to_be_repaired() -> None:
    evaluation = _evaluation(
        _diagnosis("planning", "planned_endpoint_not_implemented"),
        regenerate=["PT02|POST /reports|happy_path"],
        replan=["PT01"],
    )

    payload = _payload(evaluation)

    assert payload["requirements_to_replan"] == ["PT01"]
    assert payload["items_to_regenerate"] == ["PT02|POST /reports|happy_path"]


# --- what the model may return ---------------------------------------------------------


def test_notes_are_returned_for_the_items_that_were_asked_about(tmp_path) -> None:
    item = "PT28|GET /reports|happy_path"
    agent = _agent(
        tmp_path,
        {
            "planning_note": "PT01 maps to the login operation; registration is POST /users.",
            "generation_notes": [{"item": item, "note": "Create the report in setup first."}],
        },
    )

    notes, _ = agent.run(_evaluation(_diagnosis("generation", "x"), regenerate=[item]))

    assert notes.note_for(item) == "Create the report in setup first."
    assert notes.planning_note.startswith("PT01 maps")


def test_a_note_naming_an_item_nobody_asked_about_is_discarded(tmp_path) -> None:
    """Models paraphrase keys. A note whose key matches nothing can never be attached to a
    prompt, so keeping it would make the report claim a correction that is not applied."""

    agent = _agent(
        tmp_path,
        {
            "planning_note": "",
            "generation_notes": [
                {"item": "PT28|GET /reports|happy_path", "note": "keep me"},
                {"item": "PT99 - some paraphrased key", "note": "drop me"},
            ],
        },
    )

    notes, _ = agent.run(
        _evaluation(_diagnosis("generation", "x"), regenerate=["PT28|GET /reports|happy_path"])
    )

    assert [entry.item for entry in notes.generation_notes] == ["PT28|GET /reports|happy_path"]


def test_an_unparsable_response_is_an_error_rather_than_silent_emptiness(tmp_path) -> None:
    """Feedback that could not be produced must be visible to the caller.

    The loop decides whether another iteration is worth running from what it gets back;
    an empty note that looks like "no corrections needed" would be read as convergence.
    """

    agent = FeedbackManagerAgent(
        llm_client=MockLLMClient(["not json at all", "still not json"]),
        prompt_path=PROMPT,
        artifact_writer=ArtifactWriter(tmp_path),
    )

    with pytest.raises(AgentResponseError):
        agent.run(_evaluation(_diagnosis("generation", "x")))
