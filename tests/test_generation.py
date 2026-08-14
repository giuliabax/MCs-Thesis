from __future__ import annotations

import ast
import json

import pytest

from thesis_rest_tester.agents.base import AgentResponseError

# The Test-prefixed classes are aliased: pytest would otherwise try to collect them.
from thesis_rest_tester.agents.test_writer import TestWriterAgent as WriterAgent
from thesis_rest_tester.artifacts.writer import ArtifactWriter
from thesis_rest_tester.domain.executable import (
    Assertion,
    Capture,
    ExecutableTestCase,
    RequestSpec,
)
from thesis_rest_tester.domain.executable import (
    TestStep as Step,
)
from thesis_rest_tester.domain.models import (
    OpenAPIOperation,
)
from thesis_rest_tester.domain.models import (
    TestStrategyItem as StrategyItem,
)
from thesis_rest_tester.generation.renderer import render_conftest, render_suite
from thesis_rest_tester.llm.base import MockLLMClient


def _step(method: str, path: str, **kwargs) -> Step:
    return Step(
        description=kwargs.pop("description", "A step."),
        request=RequestSpec(method=method, path=path, **kwargs.pop("request", {})),
        **kwargs,
    )


def _case(**kwargs) -> ExecutableTestCase:
    defaults = {
        "name": "test_example",
        "requirement_id": "PT01",
        "test_type": "happy_path",
        "description": "Example.",
        "steps": [_step("GET", "/reports", expect_status=[200])],
    }
    return ExecutableTestCase(**{**defaults, **kwargs})


def _operations() -> list[OpenAPIOperation]:
    return [
        OpenAPIOperation(method="GET", path="/reports", response_codes=["200"]),
        OpenAPIOperation(
            method="POST",
            path="/auth/login",
            response_codes=["200", "401"],
            request_body_schema={
                "type": "object",
                "required": ["username", "password"],
                "properties": {
                    "username": {"type": "string"},
                    "password": {"type": "string"},
                    "age": {"type": "integer"},
                },
            },
        ),
        OpenAPIOperation(method="DELETE", path="/reports/{reportId}", response_codes=["204"]),
    ]


def _agent(tmp_path, responses: list[str]) -> WriterAgent:
    prompt = tmp_path / "writer.md"
    prompt.write_text("Return JSON.", encoding="utf-8")
    return WriterAgent(
        llm_client=MockLLMClient(responses),
        prompt_path=prompt,
        artifact_writer=ArtifactWriter(tmp_path / "out"),
    )


def _item(**kwargs) -> StrategyItem:
    defaults = {
        "requirement_id": "PT01",
        "requirement_summary": "A requirement.",
        "api_endpoint": "/auth/login",
        "http_method": "POST",
        "prompt": "Log in.",
        "test_type": "happy_path",
        "priority": "high",
        "expected_status_codes": ["200"],
    }
    return StrategyItem(**{**defaults, **kwargs})


def _response(**overrides) -> str:
    payload = {
        "name": "test_login",
        "requirement_id": "PT01",
        "test_type": "happy_path",
        "description": "Log in.",
        "setup": [],
        "steps": [
            {
                "description": "Log in.",
                "request": {
                    "method": "POST",
                    "path": "/auth/login",
                    "json_body": {"username": "u_{{unique}}", "password": "secret"},
                },
                "expect_status": [200],
            }
        ],
        "cleanup": [],
    }
    payload.update(overrides)
    return json.dumps(payload)


# --- renderer ---------------------------------------------------------------------


def test_rendered_suite_is_valid_python_and_defines_one_test_per_case() -> None:
    cases = [_case(), _case(name="test_second", requirement_id="PT02")]

    source = render_suite("participium-team01", cases)

    tree = ast.parse(source)
    functions = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
    assert functions == ["test_example", "test_second"]


def test_rendered_test_carries_timeout_base_url_and_finally_block() -> None:
    case = _case(
        setup=[
            _step(
                "POST",
                "/auth/login",
                expect_status=[200],
                captures=[Capture(name="token", source="json", expression="token")],
            )
        ],
        cleanup=[_step("DELETE", "/reports/{{report_id}}", expect_status=[204, 404])],
        steps=[
            _step(
                "GET",
                "/reports",
                expect_status=[200],
                captures=[Capture(name="report_id", source="json", expression="id")],
                assertions=[Assertion(kind="json_field", target="id", operator="exists")],
            )
        ],
    )

    source = render_suite("p", [case])

    ast.parse(source)
    assert "timeout=REQUEST_TIMEOUT_SECONDS" in source
    # Never a hardcoded host: the URL is always built from the fixture.
    assert 'f"{base_url}/reports"' in source
    assert "finally:" in source
    # Teardown must not mask the failure that brought us into finally.
    assert "except Exception:" in source


def test_renderer_resolves_placeholders_into_fstrings_and_keeps_captured_types() -> None:
    case = _case(
        steps=[
            _step(
                "POST",
                "/auth/login",
                expect_status=[200],
                captures=[Capture(name="uid", source="json", expression="id")],
                request={"json_body": {"name": "u_{{unique}}"}},
            ),
            _step("DELETE", "/reports/{{uid}}", expect_status=[204]),
        ]
    )

    source = render_suite("p", [case])

    ast.parse(source)
    assert 'f"u_{unique}"' in source
    assert 'f"{base_url}/reports/{uid}"' in source


def test_renderer_escapes_braces_that_are_not_placeholders() -> None:
    """An un-substituted OpenAPI template must not become an f-string expression."""

    case = _case(steps=[_step("DELETE", "/reports/{reportId}", expect_status=[204])])

    source = render_suite("p", [case])

    ast.parse(source)
    assert "{{reportId}}" in source


def test_conftest_is_valid_python_and_reads_the_base_url_from_the_environment() -> None:
    source = render_conftest("http://localhost:8085", 30)

    ast.parse(source)
    assert 'os.environ.get("SUT_BASE_URL"' in source
    assert "REQUEST_TIMEOUT_SECONDS = 30" in source


# --- model ------------------------------------------------------------------------


def test_case_reports_placeholders_no_earlier_step_captures() -> None:
    case = _case(steps=[_step("DELETE", "/reports/{{missing_id}}", expect_status=[204])])

    assert case.undefined_placeholders() == ["missing_id"]


def test_case_accepts_a_placeholder_captured_by_an_earlier_step() -> None:
    case = _case(
        steps=[
            _step(
                "GET",
                "/reports",
                expect_status=[200],
                captures=[Capture(name="rid", source="json", expression="id")],
            ),
            _step("DELETE", "/reports/{{rid}}", expect_status=[204]),
        ]
    )

    assert case.undefined_placeholders() == []


def test_case_name_is_normalized_to_a_test_function_identifier() -> None:
    assert _case(name="Register a citizen!").name == "test_register_a_citizen"


# --- agent reconciliation ---------------------------------------------------------


def test_agent_rejects_a_step_calling_an_undocumented_endpoint(tmp_path) -> None:
    response = _response(
        steps=[
            {
                "description": "Call an invented endpoint.",
                "request": {"method": "PATCH", "path": "/users/me"},
                "expect_status": [200],
            }
        ]
    )

    with pytest.raises(AgentResponseError, match="absent from the contract"):
        _agent(tmp_path, [response]).run(_item(), _operations(), artifact_stem="s")


def test_agent_accepts_a_path_whose_parameter_is_spelled_differently(tmp_path) -> None:
    """/reports/{id} and /reports/{reportId} address the same documented operation."""

    response = _response(
        steps=[
            {
                "description": "Delete a report.",
                "request": {"method": "DELETE", "path": "/reports/{{rid}}"},
                "expect_status": [204],
            }
        ],
        setup=[
            {
                "description": "Create a report.",
                "request": {"method": "GET", "path": "/reports"},
                "expect_status": [200],
                "captures": [{"name": "rid", "source": "json", "expression": "id"}],
            }
        ],
    )

    case, _ = _agent(tmp_path, [response]).run(_item(), _operations(), artifact_stem="s")

    assert case.steps[0].request.path == "/reports/{{rid}}"


def test_agent_rejects_a_missing_required_body_field_on_a_happy_path(tmp_path) -> None:
    response = _response(
        steps=[
            {
                "description": "Log in without a password.",
                "request": {
                    "method": "POST",
                    "path": "/auth/login",
                    "json_body": {"username": "u"},
                },
                "expect_status": [200],
            }
        ]
    )

    with pytest.raises(AgentResponseError, match="missing required field 'password'"):
        _agent(tmp_path, [response]).run(_item(), _operations(), artifact_stem="s")


def test_agent_rejects_an_undocumented_body_field_on_a_happy_path(tmp_path) -> None:
    response = _response(
        steps=[
            {
                "description": "Log in with an invented field.",
                "request": {
                    "method": "POST",
                    "path": "/auth/login",
                    "json_body": {"username": "u", "password": "p", "nickname": "n"},
                },
                "expect_status": [200],
            }
        ]
    )

    with pytest.raises(AgentResponseError, match="'nickname' is not documented"):
        _agent(tmp_path, [response]).run(_item(), _operations(), artifact_stem="s")


def test_agent_rejects_a_body_field_of_the_wrong_type(tmp_path) -> None:
    response = _response(
        steps=[
            {
                "description": "Log in with a non-numeric age.",
                "request": {
                    "method": "POST",
                    "path": "/auth/login",
                    "json_body": {"username": "u", "password": "p", "age": "thirty"},
                },
                "expect_status": [200],
            }
        ]
    )

    with pytest.raises(AgentResponseError, match="'age' should be integer"):
        _agent(tmp_path, [response]).run(_item(), _operations(), artifact_stem="s")


def test_agent_lets_a_negative_test_violate_the_body_schema(tmp_path) -> None:
    """Sending an invalid body is the point of a negative test, not a defect in it."""

    response = _response(
        test_type="negative",
        steps=[
            {
                "description": "Log in without a password, expecting rejection.",
                "request": {
                    "method": "POST",
                    "path": "/auth/login",
                    "json_body": {"username": "u"},
                },
                "expect_status": [400, 401],
            }
        ],
    )

    case, _ = _agent(tmp_path, [response]).run(
        _item(test_type="negative", expected_status_codes=["400"]),
        _operations(),
        artifact_stem="s",
    )

    assert case.test_type == "negative"


def test_agent_still_checks_setup_bodies_of_a_negative_test(tmp_path) -> None:
    """A setup call is meant to succeed whatever the test type is."""

    response = _response(
        test_type="negative",
        setup=[
            {
                "description": "Log in as a precondition.",
                "request": {
                    "method": "POST",
                    "path": "/auth/login",
                    "json_body": {"username": "u"},
                },
                "expect_status": [200],
            }
        ],
        steps=[
            {
                "description": "Read reports.",
                "request": {"method": "GET", "path": "/reports"},
                "expect_status": [401],
            }
        ],
    )

    with pytest.raises(AgentResponseError, match="setup 1.*missing required field 'password'"):
        _agent(tmp_path, [response]).run(
            _item(test_type="negative"), _operations(), artifact_stem="s"
        )


def test_agent_skips_type_checking_for_a_value_that_is_only_a_placeholder(tmp_path) -> None:
    """A capture decides its own type at run time; nothing can be concluded statically."""

    response = _response(
        setup=[
            {
                "description": "Read reports.",
                "request": {"method": "GET", "path": "/reports"},
                "expect_status": [200],
                "captures": [{"name": "age", "source": "json", "expression": "age"}],
            }
        ],
        steps=[
            {
                "description": "Log in.",
                "request": {
                    "method": "POST",
                    "path": "/auth/login",
                    "json_body": {"username": "u", "password": "p", "age": "{{age}}"},
                },
                "expect_status": [200],
            }
        ],
    )

    case, _ = _agent(tmp_path, [response]).run(_item(), _operations(), artifact_stem="s")

    assert case.steps[0].request.json_body["age"] == "{{age}}"


def test_agent_restores_traceability_the_model_altered(tmp_path) -> None:
    response = _response(requirement_id="PT99", test_type="edge_case")

    case, _ = _agent(tmp_path, [response]).run(_item(), _operations(), artifact_stem="s")

    assert (case.requirement_id, case.test_type) == ("PT01", "happy_path")


def test_agent_drops_an_undocumented_cleanup_step_but_keeps_the_test(tmp_path) -> None:
    """Reproduces the team01 failure: 9 of 10 tests were lost to their teardown.

    The model creates a resource and tries to delete it, which is right, but the contract
    exposes no deletion. The test still exercises what it was asked to, so only the
    cleanup is discarded.
    """

    response = _response(
        steps=[
            {
                "description": "Log in.",
                "request": {
                    "method": "POST",
                    "path": "/auth/login",
                    "json_body": {"username": "u", "password": "p"},
                },
                "expect_status": [200],
                "captures": [{"name": "rid", "source": "json", "expression": "id"}],
            }
        ],
        cleanup=[
            {
                "description": "Delete the citizen, which the contract cannot do.",
                "request": {"method": "DELETE", "path": "/citizens/{{rid}}"},
                "expect_status": [204, 404],
            }
        ],
    )

    case, _ = _agent(tmp_path, [response]).run(_item(), _operations(), artifact_stem="s")

    assert case.cleanup == []
    assert len(case.steps) == 1


def test_agent_keeps_a_cleanup_step_the_contract_documents(tmp_path) -> None:
    response = _response(
        steps=[
            {
                "description": "Log in.",
                "request": {
                    "method": "POST",
                    "path": "/auth/login",
                    "json_body": {"username": "u", "password": "p"},
                },
                "expect_status": [200],
                "captures": [{"name": "rid", "source": "json", "expression": "id"}],
            }
        ],
        cleanup=[
            {
                "description": "Delete the report.",
                "request": {"method": "DELETE", "path": "/reports/{{rid}}"},
                "expect_status": [204],
            }
        ],
    )

    case, _ = _agent(tmp_path, [response]).run(_item(), _operations(), artifact_stem="s")

    assert [step.request.method for step in case.cleanup] == ["DELETE"]


def test_agent_drops_a_cleanup_step_orphaned_by_an_earlier_drop(tmp_path) -> None:
    """Dropping one teardown step must not leave the next one referencing a dead capture."""

    response = _response(
        steps=[
            {
                "description": "Log in.",
                "request": {
                    "method": "POST",
                    "path": "/auth/login",
                    "json_body": {"username": "u", "password": "p"},
                },
                "expect_status": [200],
            }
        ],
        cleanup=[
            {
                "description": "Undocumented call that would have captured an id.",
                "request": {"method": "DELETE", "path": "/citizens"},
                "expect_status": [200],
                "captures": [{"name": "orphan", "source": "json", "expression": "id"}],
            },
            {
                "description": "Documented call, but it needs the orphaned capture.",
                "request": {"method": "DELETE", "path": "/reports/{{orphan}}"},
                "expect_status": [204],
            },
        ],
    )

    case, _ = _agent(tmp_path, [response]).run(_item(), _operations(), artifact_stem="s")

    assert case.cleanup == []


def test_agent_still_rejects_an_undocumented_endpoint_in_the_steps(tmp_path) -> None:
    """The leniency is for teardown only: an unrunnable test is still rejected."""

    response = _response(
        steps=[
            {
                "description": "Call an invented endpoint.",
                "request": {"method": "DELETE", "path": "/citizens"},
                "expect_status": [204],
            }
        ]
    )

    with pytest.raises(AgentResponseError, match="absent from the contract"):
        _agent(tmp_path, [response]).run(_item(), _operations(), artifact_stem="s")


def test_agent_makes_no_type_claim_about_an_unresolvable_ref(tmp_path) -> None:
    """A `$ref` field is often a string enum, and the components section is not preserved.

    Guessing "object" rejects the correct string value; the honest answer is no claim.
    """

    operations = [
        OpenAPIOperation(
            method="PATCH",
            path="/reports/{id}/status",
            response_codes=["200"],
            request_body_schema={
                "type": "object",
                "required": ["status"],
                "properties": {"status": {"$ref": "#/components/schemas/ReportStatus"}},
            },
        )
    ]
    response = _response(
        steps=[
            {
                "description": "Move the report to a new status.",
                "request": {
                    "method": "PATCH",
                    "path": "/reports/{id}/status",
                    "json_body": {"status": "IN_PROGRESS"},
                },
                "expect_status": [200],
            }
        ]
    )

    case, _ = _agent(tmp_path, [response]).run(
        _item(api_endpoint="/reports/{id}/status", http_method="PATCH"),
        operations,
        artifact_stem="s",
    )

    assert case.steps[0].request.json_body == {"status": "IN_PROGRESS"}


def test_agent_still_type_checks_a_field_the_contract_declares(tmp_path) -> None:
    """The leniency is only for unresolvable references, not for declared types."""

    response = _response(
        steps=[
            {
                "description": "Log in with a non-numeric age.",
                "request": {
                    "method": "POST",
                    "path": "/auth/login",
                    "json_body": {"username": "u", "password": "p", "age": "thirty"},
                },
                "expect_status": [200],
            }
        ]
    )

    with pytest.raises(AgentResponseError, match="'age' should be integer"):
        _agent(tmp_path, [response]).run(_item(), _operations(), artifact_stem="s")


def test_not_empty_assertion_replaces_the_javascript_length_idiom() -> None:
    """An observed test asserted `data.length` > 0, which is not a JSON path."""

    case = _case(
        steps=[
            _step(
                "GET",
                "/reports",
                expect_status=[200],
                assertions=[
                    Assertion(kind="json_field", target="data", operator="not_empty")
                ],
            )
        ]
    )

    source = render_suite("p", [case])

    ast.parse(source)
    assert 'assert not is_empty(json_path(response, "data"))' in source
    assert "is_empty" in render_conftest("http://127.0.0.1:5000", 30)


def test_generated_conftest_treats_a_missing_field_as_empty() -> None:
    """Asserting a list is non-empty must fail when the field is absent, not raise."""

    namespace: dict = {}
    exec(compile(render_conftest("http://127.0.0.1:5000", 30), "conftest", "exec"), namespace)
    is_empty = namespace["is_empty"]

    assert is_empty(None) and is_empty([]) and is_empty("") and is_empty({})
    assert not is_empty([1]) and not is_empty("x") and not is_empty(0)


def test_capture_names_may_be_camel_case(tmp_path) -> None:
    """`reportId` is a legal Python name and the spelling the contracts use themselves.

    Requiring lower case rejected roughly a quarter of all discarded tests, and would
    also have left `{{reportId}}` unrecognised as a placeholder -- rendering it into the
    URL as literal text rather than substituting the captured value.
    """

    response = _response(
        setup=[
            {
                "description": "Read reports.",
                "request": {"method": "GET", "path": "/reports"},
                "expect_status": [200],
                "captures": [{"name": "reportId", "source": "json", "expression": "id"}],
            }
        ],
        steps=[
            {
                "description": "Delete the report.",
                "request": {"method": "DELETE", "path": "/reports/{{reportId}}"},
                "expect_status": [204],
            }
        ],
    )

    case, _ = _agent(tmp_path, [response]).run(_item(), _operations(), artifact_stem="s")

    assert case.setup[0].captures[0].name == "reportId"
    assert case.undefined_placeholders() == []
    source = render_suite("p", [case])
    ast.parse(source)
    assert 'f"{base_url}/reports/{reportId}"' in source


def test_long_placeholder_becomes_a_repetition_not_a_literal() -> None:
    """A boundary test needs the length, not the characters.

    Without this the model types the padding out: one observed case emitted ten thousand
    literal characters, exhausted its token budget, and was discarded anyway.
    """

    case = _case(
        test_type="edge_case",
        steps=[
            _step(
                "POST",
                "/auth/login",
                expect_status=[400],
                request={"json_body": {"username": "{{long_5000}}", "password": "p"}},
            )
        ],
    )

    assert case.undefined_placeholders() == []
    source = render_suite("p", [case])
    ast.parse(source)
    assert '"A" * 5000' in source
    # And the padding itself never appears in the generated module.
    assert "AAAA" not in source
