"""Regenerating named tests while leaving the rest of a suite untouched.

An error here is silent: the suite still renders and the counts still add up, but the
comparison between iterations quietly stops meaning anything. These tests pin the two
properties that comparison depends on -- that untouched tests are carried over
byte-for-byte, and that a correction reaches the model that is asked to rewrite one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from thesis_rest_tester.config import load_config
from thesis_rest_tester.generation.generator import SuiteGenerator
from thesis_rest_tester.llm.base import MockLLMClient

CONFIG = """
project_name: regen
run_id: r1
llm:
  provider: lmstudio
  model: m
  base_url: http://127.0.0.1:1234/v1
inputs:
  requirements:
    description_pdf: data/requirements/participium-description.pdf
    user_stories_xlsx: data/requirements/participium-userstories.xlsx
    faq_pdf: data/requirements/participium-faq.pdf
  projects:
    - name: proj
      openapi_path: data/requirements/participium-description.pdf
      sut_base_url: http://127.0.0.1:9
"""


def _item(requirement: str, method: str, path: str) -> dict:
    return {
        "requirement_id": requirement,
        "requirement_summary": "s",
        "api_endpoint": path,
        "http_method": method,
        "prompt": "p",
        "test_type": "happy_path",
        "priority": "high",
        "expected_status_codes": ["200"],
    }


def _written_case(name: str, requirement: str, path: str) -> str:
    return json.dumps(
        {
            "name": name,
            "requirement_id": requirement,
            "test_type": "happy_path",
            "description": "d",
            "steps": [
                {
                    "description": "call",
                    "request": {"method": "GET", "path": path},
                    "expect_status": [200],
                    "assertions": [{"kind": "json_field", "target": "id", "operator": "exists"}],
                }
            ],
        }
    )


def _build_run(tmp_path: Path) -> tuple[Path, Path]:
    run_dir = tmp_path / "run"
    project = run_dir / "projects" / "proj"
    project.mkdir(parents=True)
    (project / "openapi_operations.json").write_text(
        json.dumps(
            [
                {"method": "GET", "path": "/alpha", "response_codes": ["200"]},
                {"method": "GET", "path": "/beta", "response_codes": ["200"]},
            ]
        ),
        encoding="utf-8",
    )
    (project / "workflow_plan.json").write_text(
        json.dumps(
            {
                "run_id": "r1",
                "project_name": "proj",
                "requirements_summary": {},
                "api_summary": {},
                "strategy_items": [
                    _item("PT01", "GET", "/alpha"),
                    _item("PT02", "GET", "/beta"),
                ],
                "assumptions": [],
                "risks": [],
                "created_at": "2026-01-01T00:00:00Z",
                "sut_base_url": "http://127.0.0.1:9",
                "requirement_coverage": {},
            }
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    return run_dir, config_path


def _report(run_dir: Path) -> dict:
    return json.loads(
        (run_dir / "projects" / "proj" / "suite" / "generation_report.json").read_text(
            encoding="utf-8"
        )
    )


def test_the_report_records_which_item_produced_each_test(tmp_path) -> None:
    """The join cannot be reconstructed later: a case records its requirement and test
    type but not its endpoint, and two items may share that pair."""

    run_dir, config_path = _build_run(tmp_path)
    client = MockLLMClient(
        [_written_case("test_alpha", "PT01", "/alpha"), _written_case("test_beta", "PT02", "/beta")]
    )

    SuiteGenerator(run_dir, client, load_config(config_path)).run()

    keys = {case["name"]: case["strategy_item"] for case in _report(run_dir)["cases"]}
    assert keys == {
        "test_alpha": "PT01|GET /alpha|happy_path",
        "test_beta": "PT02|GET /beta|happy_path",
    }


def test_only_the_named_item_is_rewritten_and_the_rest_carried_over(tmp_path) -> None:
    run_dir, config_path = _build_run(tmp_path)
    config = load_config(config_path)
    SuiteGenerator(
        run_dir,
        MockLLMClient(
            [
                _written_case("test_alpha", "PT01", "/alpha"),
                _written_case("test_beta", "PT02", "/beta"),
            ]
        ),
        config,
    ).run()

    # One response only: a second call would exhaust the mock and fail loudly, which is
    # the point -- the untouched item must not reach the model at all.
    rewritten = MockLLMClient([_written_case("test_beta_v2", "PT02", "/beta")])
    SuiteGenerator(
        run_dir,
        rewritten,
        config,
        regenerate={"proj": ["PT02|GET /beta|happy_path"]},
    ).run()

    names = [case["name"] for case in _report(run_dir)["cases"]]
    assert names == ["test_alpha", "test_beta_v2"]


def test_the_correction_reaches_the_model_rewriting_the_test(tmp_path) -> None:
    """Without it, re-issuing the same prompt reproduces the same output."""

    run_dir, config_path = _build_run(tmp_path)
    config = load_config(config_path)
    SuiteGenerator(
        run_dir,
        MockLLMClient(
            [
                _written_case("test_alpha", "PT01", "/alpha"),
                _written_case("test_beta", "PT02", "/beta"),
            ]
        ),
        config,
    ).run()

    client = MockLLMClient([_written_case("test_beta_v2", "PT02", "/beta")])
    SuiteGenerator(
        run_dir,
        client,
        config,
        regenerate={"proj": ["PT02|GET /beta|happy_path"]},
        corrections={"proj": {"PT02|GET /beta|happy_path": "Create the resource first."}},
    ).run()

    sent = " ".join(str(prompt) for prompt in client.prompts)
    assert "Create the resource first." in sent
    assert "A previous attempt at this test failed" in sent


def test_an_item_with_no_previous_test_is_written_rather_than_dropped(tmp_path) -> None:
    """Replanning introduces items that never had a test.

    Treating them as "not asked about, so leave alone" silently drops them. That is how
    one iteration turned 375 tests into 201, with the missing ones absent from the skip
    list as well, so nothing in the report said they had gone.
    """

    run_dir, config_path = _build_run(tmp_path)
    config = load_config(config_path)
    SuiteGenerator(
        run_dir,
        MockLLMClient(
            [
                _written_case("test_alpha", "PT01", "/alpha"),
                _written_case("test_beta", "PT02", "/beta"),
            ]
        ),
        config,
    ).run()

    # A replan adds a third item that has never been generated.
    plan_file = run_dir / "projects" / "proj" / "workflow_plan.json"
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    plan["strategy_items"].append(_item("PT03", "GET", "/alpha"))
    plan_file.write_text(json.dumps(plan), encoding="utf-8")

    SuiteGenerator(
        run_dir,
        MockLLMClient([_written_case("test_gamma", "PT03", "/alpha")]),
        config,
        regenerate={"proj": ["PT99|GET /nothing|happy_path"]},
    ).run()

    names = [case["name"] for case in _report(run_dir)["cases"]]
    assert names == ["test_alpha", "test_beta", "test_gamma"]


def test_a_targeted_regeneration_that_can_carry_nothing_is_refused(tmp_path) -> None:
    """A suite written before the report recorded item keys has no join. Proceeding would
    empty it, which looks exactly like a very bad iteration rather than a defect."""

    run_dir, config_path = _build_run(tmp_path)
    config = load_config(config_path)
    SuiteGenerator(
        run_dir,
        MockLLMClient(
            [
                _written_case("test_alpha", "PT01", "/alpha"),
                _written_case("test_beta", "PT02", "/beta"),
            ]
        ),
        config,
    ).run()

    # Strip the keys, as an older generation report would have them.
    report_file = run_dir / "projects" / "proj" / "suite" / "generation_report.json"
    report = json.loads(report_file.read_text(encoding="utf-8"))
    for case in report["cases"]:
        case.pop("strategy_item", None)
    report_file.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(RuntimeError, match="no strategy_item keys"):
        SuiteGenerator(
            run_dir,
            MockLLMClient([]),
            config,
            regenerate={"proj": ["PT02|GET /beta|happy_path"]},
        ).run()
