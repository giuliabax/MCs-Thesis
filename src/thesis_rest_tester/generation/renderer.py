"""Render validated test cases as pytest modules.

Nothing here calls a language model. The model's contribution ended when its JSON
validated; from that point the shape of the emitted code is fixed by this module, which
is what makes every generated suite uniform: an explicit timeout on every call, the base
URL taken from a fixture, unique data per invocation, and teardown in a finally block.
"""

from __future__ import annotations

import re
from typing import Any

from thesis_rest_tester.domain.executable import (
    Assertion,
    Capture,
    ExecutableTestCase,
    TestStep,
)

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_INDENT = "    "

CONFTEST = '''"""Fixtures for the generated suite. Regenerated with it; do not edit by hand."""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
import requests

DEFAULT_BASE_URL = "{default_base_url}"
REQUEST_TIMEOUT_SECONDS = {timeout}


@pytest.fixture(scope="session")
def base_url() -> str:
    """The SUT root. Overridden with SUT_BASE_URL so the suite is host-independent."""

    return os.environ.get("SUT_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


@pytest.fixture()
def unique() -> str:
    """A value unique to one test invocation, so reruns cannot collide on a natural key."""

    return uuid.uuid4().hex[:12]


@pytest.fixture()
def session() -> Any:
    """Function-scoped session: no test may inherit another's cookies or headers."""

    with requests.Session() as open_session:
        yield open_session


def json_path(response: Any, expression: str) -> Any:
    """Read a dotted path out of a JSON body, returning None if any hop is missing."""

    try:
        current = response.json()
    except ValueError:
        return None
    for part in expression.split("."):
        if isinstance(current, list):
            try:
                current = current[int(part)]
                continue
            except (ValueError, IndexError):
                return None
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def contains(haystack: Any, needle: Any) -> bool:
    """Membership that works for lists, dicts, and strings alike."""

    if haystack is None:
        return False
    if isinstance(haystack, (list, tuple, dict)):
        return needle in haystack
    return str(needle) in str(haystack)


def is_empty(value: Any) -> bool:
    """Whether a collection endpoint returned nothing.

    A missing field counts as empty: asserting a list is non-empty should fail when the
    field is absent rather than raise.
    """

    if value is None:
        return True
    if isinstance(value, (list, tuple, dict, str)):
        return len(value) == 0
    return False
'''


def render_conftest(default_base_url: str, timeout_seconds: int) -> str:
    return CONFTEST.format(default_base_url=default_base_url, timeout=timeout_seconds)


def render_suite(project_name: str, cases: list[ExecutableTestCase]) -> str:
    """Render one project's cases as a single pytest module."""

    lines = [
        '"""Generated black-box tests for ' + project_name + ".",
        "",
        "Produced by the Test Writer agent from the project's workflow plan, then rendered",
        "deterministically. Regenerated wholesale; edits are lost.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "from conftest import REQUEST_TIMEOUT_SECONDS, contains, is_empty, json_path",
        "",
    ]
    for case in cases:
        lines.extend(_render_case(case))
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _render_case(case: ExecutableTestCase) -> list[str]:
    body: list[str] = []
    body.append("")
    body.append(f"def {case.name}(base_url, unique, session):")
    body.append(f'{_INDENT}"""{_escape_docstring(case.description)}"""')
    body.append("")
    body.append(f"{_INDENT}# requirement: {case.requirement_id}    type: {case.test_type}")

    for index, step in enumerate(case.setup, start=1):
        body.append("")
        body.append(f"{_INDENT}# setup {index}: {_escape_comment(step.description)}")
        body.extend(_render_step(step, indent=1, phase="setup"))

    body.append("")
    body.append(f"{_INDENT}try:")
    for index, step in enumerate(case.steps, start=1):
        if index > 1:
            body.append("")
        body.append(f"{_INDENT * 2}# step {index}: {_escape_comment(step.description)}")
        body.extend(_render_step(step, indent=2, phase="step"))

    body.append(f"{_INDENT}finally:")
    if not case.cleanup:
        body.append(f"{_INDENT * 2}pass")
    for index, step in enumerate(case.cleanup, start=1):
        body.append(f"{_INDENT * 2}# cleanup {index}: {_escape_comment(step.description)}")
        # Teardown must never mask the failure that brought us here.
        body.append(f"{_INDENT * 2}try:")
        body.extend(_render_step(step, indent=3, phase="cleanup"))
        body.append(f"{_INDENT * 2}except Exception:  # noqa: BLE001 - teardown is best-effort")
        body.append(f"{_INDENT * 3}pass")
    return body


def _render_step(step: TestStep, *, indent: int, phase: str) -> list[str]:
    pad = _INDENT * indent
    lines = [f"{pad}response = session.request("]
    lines.append(f"{pad}{_INDENT}{_literal(step.request.method)},")
    lines.append(f"{pad}{_INDENT}{_url(step.request.path)},")
    if step.request.query:
        lines.append(f"{pad}{_INDENT}params={_mapping(step.request.query)},")
    if step.request.headers:
        lines.append(f"{pad}{_INDENT}headers={_mapping(step.request.headers)},")
    if step.request.json_body is not None:
        lines.append(f"{pad}{_INDENT}json={_value(step.request.json_body)},")
    lines.append(f"{pad}{_INDENT}timeout=REQUEST_TIMEOUT_SECONDS,")
    lines.append(f"{pad})")

    if step.expect_status and phase != "cleanup":
        expected = ", ".join(str(code) for code in step.expect_status)
        suffix = "," if len(step.expect_status) == 1 else ""
        # The one place a custom message earns its keep: the response body explains a
        # rejection that the status code alone does not, and a setup failure must be
        # distinguishable from a genuine failure of the behaviour under test.
        label = "setup step failed: " if phase == "setup" else ""
        lines.append(f"{pad}assert response.status_code in ({expected}{suffix}), (")
        lines.append(
            f'{pad}{_INDENT}f"{label}{step.request.method} '
            f'{_escape_message(step.request.path)} returned "'
        )
        lines.append(f'{pad}{_INDENT}f"{{response.status_code}}: {{response.text[:300]}}"')
        lines.append(f"{pad})")

    for capture in step.captures:
        lines.append(f"{pad}{capture.name} = {_capture_expression(capture)}")

    if phase != "cleanup":
        for assertion in step.assertions:
            lines.extend(_render_assertion(assertion, pad))
    return lines


def _capture_expression(capture: Capture) -> str:
    if capture.source == "header":
        return f"response.headers.get({_literal(capture.expression)})"
    return f"json_path(response, {_literal(capture.expression)})"


def _render_assertion(assertion: Assertion, pad: str) -> list[str]:
    """Render one assertion.

    Deliberately without a custom message: pytest rewrites assertions and reports both
    operands, which is more informative than any string built here and avoids nesting
    f-strings inside generated f-strings, the likeliest source of a broken suite.
    """

    target = assertion.target or ""
    if assertion.kind == "body_contains":
        actual = "response.text"
    elif assertion.kind == "header":
        actual = f"response.headers.get({_literal(target)})"
    else:
        actual = f"json_path(response, {_literal(target)})"

    expected = _value(assertion.value)
    if assertion.operator == "exists":
        return [f"{pad}assert {actual} is not None"]
    if assertion.operator == "not_empty":
        return [f"{pad}assert not is_empty({actual})"]
    if assertion.operator == "equals":
        return [f"{pad}assert {actual} == {expected}"]
    if assertion.operator == "not_equals":
        return [f"{pad}assert {actual} != {expected}"]
    if assertion.operator == "greater_than":
        return [f"{pad}assert {actual} is not None and {actual} > {expected}"]
    return [f"{pad}assert contains({actual}, {expected})"]


def _url(path: str) -> str:
    """Join the base-url fixture with the templated path, always as one f-string."""

    inner = _interpolate(path, force_fstring=True)[2:-1]
    return f'f"{{base_url}}{inner}"'


def _mapping(values: dict[str, str]) -> str:
    items = ", ".join(f"{_literal(key)}: {_string(value)}" for key, value in values.items())
    return "{" + items + "}"


def _value(value: Any) -> str:
    """Render a JSON value as Python source, resolving placeholders inside strings."""

    if isinstance(value, str):
        return _string(value)
    if isinstance(value, bool) or value is None:
        return repr(value)
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{_literal(k)}: {_value(v)}" for k, v in value.items()) + "}"
    return repr(value)


def _string(text: str) -> str:
    names = _PLACEHOLDER.findall(text)
    if not names:
        return _literal(text)
    # A value that is exactly one placeholder keeps the captured type: an integer id
    # must not become the string "17" merely because it passed through a template.
    stripped = _PLACEHOLDER.sub("", text)
    if len(names) == 1 and not stripped.strip():
        return _expression_for(names[0])
    return _interpolate(text)


def _interpolate(text: str, *, force_fstring: bool = False) -> str:
    """Turn ``a_{{b}}`` into ``f"a_{b}"``, escaping braces that are not placeholders."""

    if not _PLACEHOLDER.search(text):
        return _literal(text) if not force_fstring else f'f"{_escape_message(text)}"'
    out: list[str] = []
    position = 0
    for match in _PLACEHOLDER.finditer(text):
        out.append(_escape_message(text[position : match.start()]))
        out.append("{" + _expression_for(match.group(1)) + "}")
        position = match.end()
    out.append(_escape_message(text[position:]))
    return 'f"' + "".join(out) + '"'


def _expression_for(name: str) -> str:
    """The Python expression a placeholder resolves to.

    ``long_N`` becomes a repetition rather than a literal: a boundary test needs the
    length, not the characters, and asking a model to type them out costs thousands of
    tokens for a value the renderer can build in one.
    """

    match = re.fullmatch(r"long_(\d+)", name)
    return f'"A" * {match.group(1)}' if match else name


def _literal(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _escape_message(text: str) -> str:
    """Escape a fragment destined for an f-string literal."""

    return (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("{", "{{")
        .replace("}", "}}")
    )


def _escape_docstring(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"""', "'''").strip()


def _escape_comment(text: str) -> str:
    return " ".join(text.split())
