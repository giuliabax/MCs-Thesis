"""Deterministic metric collection over one project's execution evidence.

Nothing here calls a model. Every figure the results chapter reports has to be
reproducible from the artifacts on disk, so the collectors are pure functions over the
records ``domain.execution`` defines, and the only judgement they encode is in their
definitions -- which is where the care is needed.

Two decisions shape almost every number below.

**Tests that never ran are excluded from pass rate, and counted separately.** A project
whose containers refused to start records one ``not_run`` case per generated test. Folding
those into the denominator would report a pipeline that generated good tests as one that
generated failing ones, and folding them into neither numerator nor denominator without
saying so would quietly shrink the sample. They get their own figure.

**Coverage comes from HTTP exchanges, not from test verdicts.** Whether a test passed says
nothing about which endpoints it touched: a test that fails in setup may still have
exercised three operations, and a test that passes may have called one. Only the recorded
traffic can answer "how much of the API did this suite reach", which is why the executor
records every exchange rather than only outcomes.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from thesis_rest_tester.domain.models import AgentOutput, MetricSnapshot

# Outcomes that mean the test was actually run against the service.
_EXECUTED = frozenset({"passed", "failed", "error"})


@dataclass(slots=True)
class MetricInputs:
    iteration: int
    execution_records: list[dict[str, Any]] = field(default_factory=list)
    # Every HTTP call the suite made, in the shape of ``HttpExchangeRecord``.
    exchange_records: list[dict[str, Any]] = field(default_factory=list)
    planned_operations: set[tuple[str, str]] = field(default_factory=set)
    # Status codes the contract documents for the planned operations, as strings.
    documented_status_codes: set[str] = field(default_factory=set)
    coverage_report: Path | None = None
    seeded_bug_ids: set[str] = field(default_factory=set)
    detected_bug_ids: set[str] = field(default_factory=set)
    token_usage: int | None = None
    execution_time_seconds: float | None = None
    estimated_cost_usd: float | None = None


def calculate_pass_rate(execution_records: list[dict[str, Any]]) -> float | None:
    """Passed over executed. ``None`` when nothing executed, which is not zero.

    A suite that could not run has no pass rate; reporting 0.0 would put it in the same
    column as a suite that ran and failed everything, and those are different results.
    """

    executed = [record for record in execution_records if record.get("outcome") in _EXECUTED]
    if not executed:
        return None
    passed = sum(1 for record in executed if record.get("outcome") == "passed")
    return passed / len(executed)


def calculate_execution_success_rate(execution_records: list[dict[str, Any]]) -> float | None:
    """How much of what was generated actually reached the service."""

    if not execution_records:
        return None
    executed = sum(1 for record in execution_records if record.get("outcome") in _EXECUTED)
    return executed / len(execution_records)


def count_not_run(execution_records: list[dict[str, Any]]) -> int:
    """Generated tests that never reached the service, kept out of the pass rate."""

    return sum(1 for record in execution_records if record.get("outcome") == "not_run")


def calculate_operation_coverage(
    exchange_records: list[dict[str, Any]],
    planned_operations: set[tuple[str, str]],
) -> float | None:
    """Share of the planned operations the suite actually called.

    Paths are templated before comparing, because an exchange records the concrete URL
    (``/reports/7``) while a plan names the operation (``/reports/{reportId}``).
    """

    if not planned_operations:
        return None
    planned = {(method.upper(), template_path(path)) for method, path in planned_operations}
    called = {
        (str(record.get("method", "")).upper(), template_path(str(record.get("path", ""))))
        for record in exchange_records
    }
    return len(planned & called) / len(planned)


def calculate_status_code_coverage(
    exchange_records: list[dict[str, Any]],
    documented_status_codes: set[str],
) -> float | None:
    """Share of the documented response codes the suite actually observed.

    Low coverage here is the signature of a suite that only ever walks happy paths: the
    4xx branches a contract documents stay unvisited.
    """

    if not documented_status_codes:
        return None
    documented = {str(code).strip() for code in documented_status_codes if str(code).strip()}
    observed = {
        str(record["status_code"])
        for record in exchange_records
        if record.get("status_code") is not None
    }
    matched = {code for code in documented if _code_matches(code, observed)}
    return len(matched) / len(documented) if documented else None


def count_server_errors(exchange_records: list[dict[str, Any]]) -> int:
    """5xx responses, which are findings about the service rather than about the tests."""

    return sum(
        1
        for record in exchange_records
        if isinstance(record.get("status_code"), int) and 500 <= record["status_code"] < 600
    )


def count_seeded_bugs_detected(
    seeded_bug_ids: set[str],
    detected_bug_ids: set[str],
) -> int:
    """Seeded faults the suite noticed.

    Detection is decided by the fault runner, not here: a fault counts as detected when a
    test that passed against the clean service fails with the fault active. This only
    counts what that comparison produced, and ignores any id that was never seeded.
    """

    return len(seeded_bug_ids & detected_bug_ids)


def ingest_coverage_report(coverage_report: Path) -> float | None:
    """Read a line-coverage ratio produced by a language-specific tool.

    Deliberately unimplemented rather than removed. Line coverage of the system under test
    would need a coverage agent inside each container, in four different toolchains; the
    metric is defined here so its absence is visible in the snapshot instead of silently
    missing from the results.
    """

    del coverage_report
    return None


def aggregate_token_usage(agent_outputs: Sequence[AgentOutput]) -> int:
    """Total tokens across agent calls.

    ``total_tokens`` is preferred where the provider reported it, falling back to the sum
    of the two halves: LM Studio does not always return all three fields.
    """

    total = 0
    for output in agent_outputs:
        usage = output.token_usage
        if usage is None:
            continue
        if usage.total_tokens is not None:
            total += usage.total_tokens
        else:
            total += (usage.prompt_tokens or 0) + (usage.completion_tokens or 0)
    return total


def measure_execution_time(started_at_seconds: float, finished_at_seconds: float) -> float:
    return max(0.0, finished_at_seconds - started_at_seconds)


def estimate_cost(
    agent_outputs: Sequence[AgentOutput],
    *,
    prompt_cost_per_million: float,
    completion_cost_per_million: float,
) -> float:
    """Cost of the model calls, for a hosted provider.

    Zero for the local model this study runs on, which is the point of reporting it: the
    figure is what the same pipeline would cost against a hosted API.
    """

    usages = [output.token_usage for output in agent_outputs if output.token_usage is not None]
    prompt_tokens = sum(usage.prompt_tokens or 0 for usage in usages)
    completion_tokens = sum(usage.completion_tokens or 0 for usage in usages)
    return (
        prompt_tokens * prompt_cost_per_million / 1_000_000
        + completion_tokens * completion_cost_per_million / 1_000_000
    )


def evaluate_metrics(inputs: MetricInputs) -> MetricSnapshot:
    """Collect every metric that the supplied evidence supports."""

    return MetricSnapshot(
        iteration=inputs.iteration,
        pass_rate=calculate_pass_rate(inputs.execution_records),
        execution_success_rate=calculate_execution_success_rate(inputs.execution_records),
        operation_coverage=calculate_operation_coverage(
            inputs.exchange_records, inputs.planned_operations
        ),
        status_code_coverage=calculate_status_code_coverage(
            inputs.exchange_records, inputs.documented_status_codes
        ),
        coverage=(
            ingest_coverage_report(inputs.coverage_report)
            if inputs.coverage_report is not None
            else None
        ),
        seeded_bugs_detected=(
            count_seeded_bugs_detected(inputs.seeded_bug_ids, inputs.detected_bug_ids)
            if inputs.seeded_bug_ids
            else None
        ),
        server_errors_count=count_server_errors(inputs.exchange_records),
        token_usage=inputs.token_usage,
        execution_time_seconds=inputs.execution_time_seconds,
        estimated_cost_usd=inputs.estimated_cost_usd,
    )


_PATH_PARAMETER = re.compile(r"\{[^}]*\}")
# A path segment that is an id rather than a resource name: digits, a UUID, or the
# opaque identifiers these services hand out.
_IDENTIFIER_SEGMENT = re.compile(r"^(\d+|[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}|[0-9a-fA-F]{16,})$")


def template_path(path: str) -> str:
    """Reduce a path to the operation it addresses.

    Two different things must meet in the middle. A planned operation spells its
    parameters (``/reports/{reportId}``), while a recorded exchange carries the value that
    was actually sent (``/reports/7``); neither can be compared to the other as written.
    Both are rewritten to ``/reports/{}``.

    Only segments that look like identifiers are collapsed. Rewriting every segment would
    make ``/reports/search`` and ``/reports/7`` the same operation and inflate coverage.
    """

    path = path.split("?", 1)[0].split("#", 1)[0]
    path = _PATH_PARAMETER.sub("{}", path)
    segments = [
        "{}" if _IDENTIFIER_SEGMENT.match(segment) else segment for segment in path.split("/")
    ]
    return "/".join(segments).rstrip("/") or "/"


def _code_matches(documented: str, observed: Iterable[str]) -> bool:
    """Whether a documented code was seen, allowing the ``4XX`` wildcard form.

    Contracts in this corpus mix exact codes with ranges, and a suite that produced a 404
    has exercised a documented ``4XX`` response.
    """

    documented = documented.upper()
    if "X" not in documented:
        return documented in set(observed)
    prefix = documented.split("X", 1)[0]
    return any(code.startswith(prefix) for code in observed)
