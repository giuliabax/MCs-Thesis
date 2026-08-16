from __future__ import annotations

from thesis_rest_tester.domain.evaluation import EvaluationReport, ProjectEvaluation
from thesis_rest_tester.domain.models import AgentOutput, MetricSnapshot, TokenUsage
from thesis_rest_tester.evaluation.metrics import (
    MetricInputs,
    aggregate_token_usage,
    calculate_execution_success_rate,
    calculate_operation_coverage,
    calculate_pass_rate,
    calculate_status_code_coverage,
    count_not_run,
    count_seeded_bugs_detected,
    count_server_errors,
    evaluate_metrics,
    template_path,
)


def _case(outcome: str, name: str = "test_x") -> dict[str, object]:
    return {"project_name": "p", "test_name": name, "outcome": outcome}


def _exchange(method: str, path: str, status: int | None = 200) -> dict[str, object]:
    return {"project_name": "p", "method": method, "path": path, "status_code": status}


# --- pass rate -------------------------------------------------------------------------


def test_tests_that_never_ran_are_kept_out_of_the_pass_rate() -> None:
    """A project whose containers refused to start records one not_run per test.

    Counting those as failures would report a pipeline that generated good tests as one
    that generated failing ones.
    """

    records = [_case("passed"), _case("failed"), _case("not_run"), _case("not_run")]
    assert calculate_pass_rate(records) == 0.5
    assert count_not_run(records) == 2


def test_a_suite_that_never_ran_has_no_pass_rate_rather_than_zero() -> None:
    """Zero would put it in the same column as a suite that ran and failed everything."""

    assert calculate_pass_rate([_case("not_run"), _case("not_run")]) is None
    assert calculate_pass_rate([]) is None


def test_execution_success_rate_measures_how_much_reached_the_service() -> None:
    records = [_case("passed"), _case("failed"), _case("error"), _case("not_run")]
    assert calculate_execution_success_rate(records) == 0.75


# --- operation coverage ----------------------------------------------------------------


def test_a_planned_parameter_and_a_sent_value_describe_the_same_operation() -> None:
    """The plan names /reports/{reportId}; the exchange records /reports/7."""

    planned = {("GET", "/reports/{reportId}"), ("POST", "/reports")}
    exchanges = [_exchange("GET", "/reports/7")]
    assert calculate_operation_coverage(exchanges, planned) == 0.5


def test_coverage_does_not_collapse_a_named_sub_resource_into_a_parameter() -> None:
    """Templating every segment would make /reports/search and /reports/7 the same
    operation, and inflate coverage with calls that never happened."""

    planned = {("GET", "/reports/{reportId}"), ("GET", "/reports/search")}
    assert calculate_operation_coverage([_exchange("GET", "/reports/search")], planned) == 0.5


def test_coverage_ignores_the_query_string_and_is_case_insensitive_on_the_method() -> None:
    planned = {("GET", "/reports")}
    assert calculate_operation_coverage([_exchange("get", "/reports?page=2")], planned) == 1.0


def test_coverage_is_undefined_when_nothing_was_planned() -> None:
    assert calculate_operation_coverage([_exchange("GET", "/reports")], set()) is None


def test_template_path_collapses_identifiers_but_not_names() -> None:
    assert template_path("/reports/7/messages") == "/reports/{}/messages"
    assert template_path("/reports/{reportId}") == "/reports/{}"
    assert template_path("/reports/550e8400-e29b-41d4-a716-446655440000") == "/reports/{}"
    assert template_path("/reports/search") == "/reports/search"


# --- status-code coverage --------------------------------------------------------------


def test_status_code_coverage_counts_the_documented_codes_actually_seen() -> None:
    documented = {"200", "201", "400", "404"}
    exchanges = [_exchange("GET", "/a", 200), _exchange("POST", "/a", 400)]
    assert calculate_status_code_coverage(exchanges, documented) == 0.5


def test_a_documented_4xx_range_is_satisfied_by_any_4xx_seen() -> None:
    """Contracts here mix exact codes with ranges; a 404 exercises a documented 4XX."""

    assert calculate_status_code_coverage([_exchange("GET", "/a", 404)], {"4XX"}) == 1.0


def test_an_exchange_with_no_status_code_cannot_satisfy_anything() -> None:
    """A request that never got an answer -- a connection error -- has no status."""

    assert calculate_status_code_coverage([_exchange("GET", "/a", None)], {"200"}) == 0.0


# --- server errors and seeded bugs -----------------------------------------------------


def test_server_errors_count_only_5xx() -> None:
    exchanges = [
        _exchange("GET", "/a", 500),
        _exchange("GET", "/b", 503),
        _exchange("GET", "/c", 404),
        _exchange("GET", "/d", None),
    ]
    assert count_server_errors(exchanges) == 2


def test_a_detected_bug_that_was_never_seeded_is_not_counted() -> None:
    assert count_seeded_bugs_detected({"F1", "F2"}, {"F1", "F9"}) == 1


# --- token usage -----------------------------------------------------------------------


def test_token_usage_falls_back_to_the_two_halves_when_the_total_is_missing() -> None:
    """LM Studio does not always return all three fields."""

    outputs = [
        AgentOutput(
            agent_name="a",
            raw_text="",
            token_usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
        ),
        AgentOutput(agent_name="b", raw_text="", token_usage=TokenUsage(total_tokens=100)),
        AgentOutput(agent_name="c", raw_text=""),
    ]
    assert aggregate_token_usage(outputs) == 115


# --- the snapshot ----------------------------------------------------------------------


def test_the_snapshot_reports_nothing_it_has_no_evidence_for() -> None:
    """An absent metric must read as absent, not as zero."""

    snapshot = evaluate_metrics(MetricInputs(iteration=1))
    assert snapshot.pass_rate is None
    assert snapshot.operation_coverage is None
    assert snapshot.status_code_coverage is None
    assert snapshot.seeded_bugs_detected is None
    assert snapshot.server_errors_count == 0


def test_the_snapshot_collects_every_metric_its_evidence_supports() -> None:
    snapshot = evaluate_metrics(
        MetricInputs(
            iteration=2,
            execution_records=[_case("passed"), _case("failed")],
            exchange_records=[_exchange("GET", "/reports", 200), _exchange("GET", "/x", 500)],
            planned_operations={("GET", "/reports")},
            documented_status_codes={"200", "404"},
        )
    )
    assert snapshot.iteration == 2
    assert snapshot.pass_rate == 0.5
    assert snapshot.operation_coverage == 1.0
    assert snapshot.status_code_coverage == 0.5
    assert snapshot.server_errors_count == 1


# --- the loop's stopping condition -----------------------------------------------------


def _report(iteration: int, rates: dict[str, float | None]) -> EvaluationReport:
    return EvaluationReport(
        run_id="r",
        iteration=iteration,
        projects={
            name: ProjectEvaluation(
                project_name=name,
                metrics=MetricSnapshot(iteration=iteration, pass_rate=rate),
            )
            for name, rate in rates.items()
        },
    )


def test_the_first_iteration_always_counts_as_improvement() -> None:
    assert _report(1, {"p": 0.0}).improved_over(None) is True


def test_the_loop_stops_when_no_project_improved() -> None:
    previous = _report(1, {"p": 0.5, "q": 0.2})
    assert _report(2, {"p": 0.5, "q": 0.1}).improved_over(previous) is False
    assert _report(2, {"p": 0.6, "q": 0.2}).improved_over(previous) is True


def test_a_newly_runnable_project_stuck_at_zero_does_not_keep_the_loop_going() -> None:
    """It appeared for the first time, but it proved nothing; only a pass counts."""

    previous = _report(1, {"p": 0.5})
    assert _report(2, {"p": 0.5, "new": 0.0}).improved_over(previous) is False
    assert _report(2, {"p": 0.5, "new": 0.1}).improved_over(previous) is True
