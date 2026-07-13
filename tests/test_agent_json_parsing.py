from __future__ import annotations

import json
from pathlib import Path

from thesis_rest_tester.agents.requirement_api_matcher import RequirementAPIMatcherAgent
from thesis_rest_tester.agents.requirements_analyst import RequirementsAnalystAgent
from thesis_rest_tester.artifacts.writer import ArtifactWriter
from thesis_rest_tester.domain.models import OpenAPIOperation, RequirementItem
from thesis_rest_tester.domain.schemas import RequirementsAnalysis, SourceRequirement
from thesis_rest_tester.llm.base import MockLLMClient


def test_requirements_agent_accepts_json_fence_and_numeric_business_value(
    tmp_path: Path,
) -> None:
    payload = {
        "summary": "Test analysis",
        "requirements": [
            {
                "id": "PT01",
                "source": "user story",
                "text": "A citizen can register.",
                "role": "citizen",
                "business_value": 950,
                "constraints": [],
                "expected_behaviors": ["Registration succeeds."],
            }
        ],
        "roles": ["citizen"],
        "domain_rules": [],
        "edge_cases": [],
        "assumptions": [],
    }
    raw_response = f"```json\n{json.dumps(payload)}\n```"
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Return JSON.", encoding="utf-8")
    writer = ArtifactWriter(tmp_path / "run")
    agent = RequirementsAnalystAgent(
        llm_client=MockLLMClient([raw_response]),
        prompt_path=prompt,
        artifact_writer=writer,
    )

    analysis, output = agent.run("requirements")

    assert analysis.requirements[0].business_value == 950
    assert output.raw_text == raw_response
    assert (tmp_path / "run/requirements_analysis.raw.txt").read_text() == raw_response


def test_requirements_agent_accepts_unmatched_trailing_fence(tmp_path: Path) -> None:
    payload = {
        "summary": "Test analysis",
        "requirements": [],
        "roles": [],
        "domain_rules": [],
        "edge_cases": [],
        "assumptions": [],
    }
    raw_response = json.dumps(payload) + "\n```"
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Return JSON.", encoding="utf-8")
    agent = RequirementsAnalystAgent(
        llm_client=MockLLMClient([raw_response]),
        prompt_path=prompt,
        artifact_writer=ArtifactWriter(tmp_path / "run"),
    )

    analysis, _ = agent.run("requirements")

    assert analysis.summary == "Test analysis"


def test_requirements_agent_repairs_schema_invalid_response(tmp_path: Path) -> None:
    invalid = {
        "requirements": [],
        "roles": [],
        "domain_rules": [],
        "edge_cases": [],
        "assumptions": [],
    }
    corrected = {"summary": "Corrected analysis", **invalid}
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Return JSON.", encoding="utf-8")
    writer = ArtifactWriter(tmp_path / "run")
    agent = RequirementsAnalystAgent(
        llm_client=MockLLMClient([json.dumps(invalid), json.dumps(corrected)]),
        prompt_path=prompt,
        artifact_writer=writer,
    )

    analysis, _ = agent.run("requirements")

    assert analysis.summary == "Corrected analysis"
    attempt = tmp_path / "run/requirements_analysis.validation_attempt1.raw.txt"
    assert json.loads(attempt.read_text()) == invalid


def test_requirements_agent_preserves_all_authoritative_source_ids(tmp_path: Path) -> None:
    payload = {
        "summary": "Incomplete and mislabelled analysis",
        "requirements": [
            {
                "id": "PT02",
                "source": "user story",
                "text": "A citizen confirms registration.",
                "role": "citizen",
                "business_value": 945,
                "constraints": ["Code expires."],
                "expected_behaviors": ["Account becomes valid."],
            }
        ],
        "roles": ["citizen"],
        "domain_rules": [],
        "edge_cases": [],
        "assumptions": [],
    }
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Return JSON.", encoding="utf-8")
    agent = RequirementsAnalystAgent(
        llm_client=MockLLMClient([json.dumps(payload)]),
        prompt_path=prompt,
        artifact_writer=ArtifactWriter(tmp_path / "run"),
    )
    sources = [
        SourceRequirement(
            id="PT01",
            source="user_stories_xlsx",
            text="As a citizen I want to register.",
            role="citizen",
            business_value=950,
        ),
        SourceRequirement(
            id="PT02",
            source="user_stories_xlsx",
            text="As an administrator I want to create municipality users.",
            role="administrator",
            business_value=940,
        ),
    ]

    analysis, _ = agent.run("requirements", sources)

    assert [item.id for item in analysis.requirements] == ["PT01", "PT02"]
    assert analysis.requirements[1].text == sources[1].text
    assert analysis.requirements[1].business_value == 940
    assert "Code expires." not in analysis.requirements[1].constraints


def test_requirements_agent_discards_pdf_only_requirements(tmp_path: Path) -> None:
    payload = {
        "summary": "Analysis with contextual PDF material",
        "requirements": [
            {
                "id": "PT01",
                "source": "user story",
                "text": "As a citizen I want to register.",
                "role": "citizen",
                "business_value": 950,
                "constraints": ["Username must be unique."],
                "expected_behaviors": ["Account is created."],
            },
            {
                "id": "DESC-001",
                "source": "description",
                "text": "The system sends generic notification emails.",
                "role": "system",
                "business_value": None,
                "constraints": [],
                "expected_behaviors": [],
            },
            {
                "id": "FAQ-001",
                "source": "FAQ",
                "text": "The map should explain report statuses.",
                "role": "citizen",
                "business_value": None,
                "constraints": [],
                "expected_behaviors": [],
            },
        ],
        "roles": ["citizen", "system"],
        "domain_rules": ["PDF-only information can inform planning context."],
        "edge_cases": [],
        "assumptions": [],
    }
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Return JSON.", encoding="utf-8")
    agent = RequirementsAnalystAgent(
        llm_client=MockLLMClient([json.dumps(payload)]),
        prompt_path=prompt,
        artifact_writer=ArtifactWriter(tmp_path / "run"),
    )
    sources = [
        SourceRequirement(
            id="PT01",
            source="user_stories_xlsx",
            text="As a citizen I want to register.",
            role="citizen",
            business_value=950,
        )
    ]

    analysis, output = agent.run("requirements", sources)

    assert [item.id for item in analysis.requirements] == ["PT01"]
    assert output.parsed_json is not None
    assert [item["id"] for item in output.parsed_json["requirements"]] == ["PT01"]
    assert analysis.domain_rules == ["PDF-only information can inform planning context."]


def test_matcher_agent_lifts_operation_level_evidence(tmp_path: Path) -> None:
    payload = {
        "matches": [
            {
                "requirement_id": "PT01",
                "status": "implemented",
                "matched_operations": [
                    {
                        "method": "post",
                        "path": "/registration",
                        "operation_id": "registerCitizen",
                        "evidence": ["Register a new citizen"],
                    }
                ],
                "evidence": ["Citizen registration endpoint"],
                "missing_behaviors": [],
                "rationale": "Registration is exposed.",
            }
        ]
    }
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Return JSON.", encoding="utf-8")
    agent = RequirementAPIMatcherAgent(
        llm_client=MockLLMClient([json.dumps(payload)]),
        prompt_path=prompt,
        artifact_writer=ArtifactWriter(tmp_path / "run"),
    )
    requirements = RequirementsAnalysis(
        summary="Requirements",
        requirements=[
            RequirementItem(
                id="PT01",
                source="user_stories_xlsx",
                text="As a citizen I want to register.",
                role="citizen",
            )
        ],
    )
    operations = [
        OpenAPIOperation(
            method="POST",
            path="/registration",
            operation_id="registerCitizen",
            response_codes=["201"],
        )
    ]

    coverage, _ = agent.run("team-a", "swagger.yaml", requirements, operations)

    match = coverage.matches[0]
    assert match.matched_operations[0].method == "POST"
    assert match.evidence == ["Citizen registration endpoint", "Register a new citizen"]
    assert coverage.validation_warnings == []
