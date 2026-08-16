"""Decide, from recorded evidence, whose mistake each failure was.

The classification is deterministic. Every other stage of this pipeline lets a model
propose and deterministic code decide, and the same boundary applies here for a sharper
reason: this stage's output drives what runs next and is reported as a result, so a
non-deterministic classifier would put non-determinism into both the control flow and the
numbers. The model's turn comes afterwards, when the Orchestrator asks it to plan or write
something again.

Rules are tried in a fixed order and the first match wins, because the order encodes what
is worth knowing. ``environment`` comes first: a service that cannot reach its mail server
produces failures that look like anything you please, and attributing those to the
generator would send the loop chasing a defect that is not there. ``sut_defect`` comes
last among the specific rules, so a genuine 5xx is only called a service defect once the
cheaper explanations are ruled out.

Everything unmatched is ``unknown`` and stays visible. Folding it into the nearest bucket
would keep the loop busy regenerating tests that were never the problem.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from thesis_rest_tester.artifacts.writer import ArtifactWriter
from thesis_rest_tester.domain.compact import body_field_spec, body_problems
from thesis_rest_tester.domain.evaluation import (
    Diagnosis,
    EvaluationReport,
    ProjectEvaluation,
)
from thesis_rest_tester.domain.execution import ExecutionReport, ProjectExecutionRecord
from thesis_rest_tester.domain.models import TestStrategyItem
from thesis_rest_tester.evaluation.metrics import MetricInputs, evaluate_metrics, template_path

_logger = logging.getLogger(__name__)

# A failure to *reach* something, rather than a service's considered answer.
#
# Deliberately phrased as failures and not as service names. An earlier version listed the
# nouns -- smtp, minio, geocode, telegram -- and misfiled team13's
# `GET /geocode returned 400: Parameter 'address' must be url encoded` as an environment
# problem, because the endpoint's own name matched. That is a generated test sending an
# unencoded parameter, which is exactly the kind of thing the loop should fix. A service
# name in a path proves nothing; only the language of a failed connection or a failed
# delivery does.
_EXTERNAL_SERVICE = re.compile(
    r"failed to send|verification email|could not (?:send|reach|connect)"
    r"|smtp\b|nodemailer|mail(?:er| server| service)"
    r"|econnrefused|enotfound|getaddrinfo|socket hang up|etimedout|network error",
    re.IGNORECASE,
)
# A 400 whose text points at the request rather than at the service.
_VALIDATION_MESSAGE = re.compile(
    r"request/body|request/query|request/params|must have required property"
    r"|must be |is not allowed|validation|invalid |required property|url encoded",
    re.IGNORECASE,
)
_STATUS_IN_MESSAGE = re.compile(r"returned (\d{3})")
_AUTH_HEADER = re.compile(r"authorization", re.IGNORECASE)
_REJECTED_ENDPOINT = re.compile(r"([A-Z]+) (/[^ ]*) returned")
# A login refused because the account does not exist or the password is wrong.
_CREDENTIALS_REJECTED = re.compile(
    r"incorrect (?:username|email|password)|invalid credentials|wrong password"
    r"|not authenticated|user not found|no such user|bad credentials"
    r"|is not an admin|unauthorized",
    re.IGNORECASE,
)
# `assert None == 'x'`: a JSON path that resolved to nothing.
_ASSERTED_NONE = re.compile(r"assert None [=!]=")


def strategy_item_key(requirement_id: str, method: str, path: str, test_type: str) -> str:
    """A stable name for one strategy item.

    Strategy items carry no identifier of their own, so the loop needs a key it can
    recompute on both sides: from the item when regenerating, and from the generated case
    when reporting what to regenerate.
    """

    return f"{requirement_id}|{method.upper()} {path}|{test_type}"


@dataclass(slots=True)
class ProjectEvidence:
    """Everything on disk about one project's attempt, gathered in one place."""

    name: str
    record: ProjectExecutionRecord
    strategy_items: list[TestStrategyItem] = field(default_factory=list)
    generated_cases: dict[str, dict[str, Any]] = field(default_factory=dict)
    exchanges: list[dict[str, Any]] = field(default_factory=list)
    documented_codes: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    # Keyed by templated path, so a concrete URL from a failure message can be
    # matched against the operation it addresses.
    body_specs: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)

    def exchanges_for(self, test_name: str) -> list[dict[str, Any]]:
        return [item for item in self.exchanges if item.get("test_name") == test_name]

    def item_for(self, case: dict[str, Any]) -> TestStrategyItem | None:
        requirement = case.get("requirement_id")
        test_type = case.get("test_type")
        for item in self.strategy_items:
            if item.requirement_id == requirement and item.test_type == test_type:
                return item
        return None


class Evaluator:
    """Turn one execution into metrics and diagnoses, and nothing else.

    It reads artifacts and returns a report. It never calls the planner or the generator:
    the Orchestrator reads what comes back and decides what to run again. That separation
    is what lets a feedback iteration be tested on a fixture report, with no model and no
    containers.
    """

    def __init__(self, run_dir: str | Path, *, iteration: int = 1) -> None:
        self._run_dir = Path(run_dir)
        self._iteration = iteration

    def run(self, execution: ExecutionReport) -> EvaluationReport:
        projects: dict[str, ProjectEvaluation] = {}
        for name, record in execution.projects.items():
            evidence = self._gather(name, record)
            projects[name] = self._evaluate_project(evidence)
        return EvaluationReport(
            run_id=execution.run_id, iteration=self._iteration, projects=projects
        )

    # --- gathering ------------------------------------------------------------------

    def _gather(self, name: str, record: ProjectExecutionRecord) -> ProjectEvidence:
        project_dir = self._run_dir / "projects" / name
        return ProjectEvidence(
            name=name,
            record=record,
            strategy_items=_load_strategy(project_dir / "test_strategy.json"),
            generated_cases=_load_generated_cases(project_dir / "suite" / "generation_report.json"),
            exchanges=_load_json_list(project_dir / "execution" / "http_exchanges.json"),
            documented_codes=_load_documented_codes(project_dir / "openapi_operations.json"),
            body_specs=_load_body_specs(project_dir / "openapi_operations.json"),
        )

    # --- evaluation -----------------------------------------------------------------

    def _evaluate_project(self, evidence: ProjectEvidence) -> ProjectEvaluation:
        record = evidence.record
        planned = {(item.http_method, item.api_endpoint) for item in evidence.strategy_items}
        documented = {
            code for codes in evidence.documented_codes.values() for code in codes
        }
        metrics = evaluate_metrics(
            MetricInputs(
                iteration=self._iteration,
                execution_records=[case.model_dump(mode="json") for case in record.cases],
                exchange_records=evidence.exchanges,
                planned_operations=planned,
                documented_status_codes=documented,
            )
        )

        # A project that never started teaches nothing about the tests it would have run,
        # and its cases are all `not_run`. Diagnosing them would manufacture evidence.
        if record.outcome != "completed":
            return ProjectEvaluation(
                project_name=evidence.name,
                metrics=metrics,
                inconclusive_reason=record.reason
                or f"the project did not run: {record.outcome}",
            )

        diagnoses = [
            self._diagnose(case.model_dump(mode="json"), evidence)
            for case in record.cases
            if case.outcome in {"failed", "error"}
        ]
        replan: list[str] = []
        regenerate: list[str] = []
        for diagnosis in diagnoses:
            case = evidence.generated_cases.get(diagnosis.test_name, {})
            item = evidence.item_for(case)
            if diagnosis.cause == "planning" and diagnosis.requirement_id:
                replan.append(diagnosis.requirement_id)
            elif diagnosis.cause == "generation" and item is not None:
                regenerate.append(
                    strategy_item_key(
                        item.requirement_id,
                        item.http_method,
                        item.api_endpoint,
                        item.test_type,
                    )
                )
        return ProjectEvaluation(
            project_name=evidence.name,
            metrics=metrics,
            diagnoses=diagnoses,
            replan_requirements=sorted(dict.fromkeys(replan)),
            regenerate_items=sorted(dict.fromkeys(regenerate)),
        )

    def _diagnose(self, case: dict[str, Any], evidence: ProjectEvidence) -> Diagnosis:
        name = str(case.get("test_name", ""))
        message = str(case.get("message") or "")
        phase = str(case.get("failure_phase") or "unknown")
        exchanges = evidence.exchanges_for(name)
        generated = evidence.generated_cases.get(name, {})
        item = evidence.item_for(generated)
        statuses = [
            item_["status_code"] for item_ in exchanges if item_.get("status_code") is not None
        ]
        message_status = _status_from_message(message)

        for rule in (
            _rule_environment,
            _rule_environment_rate_limited,
            _rule_sut_accepted_invalid_request,
            _rule_planning_contradicted_codes,
            _rule_generation_missing_auth,
            _rule_generation_login_without_account,
            _rule_generation_asserted_absent_field,
            _rule_generation_wrong_operation,
            _rule_generation_assumed_data,
            _rule_contract_mismatch,
            _rule_generation_rejected_request,
            _rule_planning_endpoint_absent,
            _rule_sut_defect,
        ):
            diagnosis = rule(
                _RuleContext(
                    test_name=name,
                    message=message,
                    phase=phase,
                    statuses=statuses,
                    message_status=message_status,
                    exchanges=exchanges,
                    generated=generated,
                    item=item,
                    evidence=evidence,
                )
            )
            if diagnosis is not None:
                return diagnosis

        return Diagnosis(
            test_name=name,
            requirement_id=case.get("requirement_id"),
            cause="unknown",
            rule="unmatched",
            evidence=[message[:300]] if message else [],
            suggestion="No rule matched; inspect the recorded exchanges by hand.",
        )


@dataclass(slots=True)
class _RuleContext:
    test_name: str
    message: str
    phase: str
    statuses: list[int]
    message_status: int | None
    exchanges: list[dict[str, Any]]
    generated: dict[str, Any]
    item: TestStrategyItem | None
    evidence: ProjectEvidence

    @property
    def requirement_id(self) -> str | None:
        value = self.generated.get("requirement_id")
        return str(value) if value else (self.item.requirement_id if self.item else None)

    def diagnose(self, cause: str, rule: str, evidence: list[str], suggestion: str):
        return Diagnosis(
            test_name=self.test_name,
            requirement_id=self.requirement_id,
            cause=cause,  # type: ignore[arg-type]
            rule=rule,
            evidence=evidence,
            suggestion=suggestion,
        )


def _rule_environment(context: _RuleContext) -> Diagnosis | None:
    """Something the service depends on is not present on this machine.

    First, and deliberately so. team13's signup answered `400 "Failed to send verification
    email"` because the team's mailbox no longer accepts logins; six tests died in setup
    for a reason that has nothing to do with the tests or with the application's logic.
    Attributing that to the generator would send the loop rewriting perfectly good tests.
    """

    if _EXTERNAL_SERVICE.search(context.message):
        return context.diagnose(
            "environment",
            "external_service_unavailable",
            [context.message[:300]],
            "Provide the missing service in the project's compose overlay, or accept this "
            "as an environment limitation and exclude it from the loop.",
        )
    if any(item.get("error") for item in context.exchanges):
        errors = [str(item["error"])[:120] for item in context.exchanges if item.get("error")]
        return context.diagnose(
            "environment",
            "request_never_completed",
            errors[:3],
            "The service did not answer at all; check the container is still up.",
        )
    return None


def _rule_environment_rate_limited(context: _RuleContext) -> Diagnosis | None:
    """The service throttled the suite.

    A property of running dozens of tests back to back against one instance, not of any
    individual test: the same test in isolation would pass. Attributing it to the
    generator would send the loop rewriting tests whose only fault was arriving quickly.
    """

    if 429 not in context.statuses and context.message_status != 429:
        return None
    return context.diagnose(
        "environment",
        "rate_limited",
        [context.message[:200]],
        "The service rate-limits; pace the suite or raise the limit for the test run.",
    )


def _rule_generation_login_without_account(context: _RuleContext) -> Diagnosis | None:
    """The test tried to authenticate as a user it never created.

    The commonest failure of all once setup failures stopped being misfiled. Every project
    starts from an empty database, so a test that posts invented credentials to a login
    endpoint can only be told they are wrong. The fix belongs to the generator: register
    the account in setup, then log in with the credentials just used.

    Distinguished from ``authentication_missing``, which is about a request that carried no
    token at all; here the test did try to obtain one and was refused.
    """

    if 401 not in context.statuses and context.message_status != 401:
        return None
    if context.generated.get("test_type") == "negative":
        return None
    if not _CREDENTIALS_REJECTED.search(context.message):
        return None
    return context.diagnose(
        "generation",
        "login_with_unregistered_credentials",
        [context.message[:250], "the database starts empty, so no such account exists"],
        "Regenerate: create the account in setup and log in with those credentials, or "
        "capture them from the registration response.",
    )


def _rule_sut_accepted_invalid_request(context: _RuleContext) -> Diagnosis | None:
    """A negative test expected a rejection and the service accepted the request.

    This is a finding rather than a fault: the test did exactly what it was written to do,
    and the service failed to enforce a constraint its own contract documents. It must
    never be fed back, because the only way to make such a test pass is to stop asking the
    question.
    """

    expected_rejection = re.search(r"assert 2\d\d in \((?:4\d\d|5\d\d)", context.message)
    if not expected_rejection:
        return None
    return context.diagnose(
        "sut_defect",
        "invalid_request_accepted",
        [context.message[:250]],
        "Do not feed this back: the service accepted a request its contract says it "
        "should reject.",
    )


def _rule_generation_asserted_absent_field(context: _RuleContext) -> Diagnosis | None:
    """The request succeeded, and the assertion named a field the response does not carry.

    Recognisable because the comparison resolved to None: `assert None == 'Test Citizen'`
    where the left-hand side came from a JSON path. The call worked, so the fault is in
    what the test expected to find, which is the generator's to correct.
    """

    if not _ASSERTED_NONE.search(context.message):
        return None
    if not any(200 <= code < 300 for code in context.statuses) and not (
        context.message_status and 200 <= context.message_status < 300
    ):
        return None
    return context.diagnose(
        "generation",
        "asserted_field_absent_from_response",
        [context.message[:250], "the request itself succeeded"],
        "Regenerate: assert on a field the response actually returns, or capture the "
        "field name from the contract's response schema.",
    )


def _rule_planning_contradicted_codes(context: _RuleContext) -> Diagnosis | None:
    """The strategy expected a status the contract never documents for that operation.

    The written test is faithful to its instruction, so rewriting it reproduces the same
    failure: the instruction is what has to change.
    """

    item = context.item
    if item is None or not item.expected_status_codes:
        return None
    documented = context.evidence.documented_codes.get(
        (item.http_method.upper(), item.api_endpoint)
    )
    if not documented:
        return None
    expected = {str(code).strip() for code in item.expected_status_codes}
    if expected and not (expected & documented):
        return context.diagnose(
            "planning",
            "expected_codes_absent_from_contract",
            [
                f"strategy expects {sorted(expected)} for "
                f"{item.http_method} {item.api_endpoint}",
                f"contract documents {sorted(documented)}",
            ],
            "Replan this requirement: the expected outcome contradicts the contract.",
        )
    return None


def _rule_generation_missing_auth(context: _RuleContext) -> Diagnosis | None:
    """Rejected for want of credentials the test never obtained.

    Only when the test is not *about* being unauthorised: a negative test that omits the
    header deliberately and expects 401 has done its job.
    """

    rejected = 401 in context.statuses or 403 in context.statuses
    if not rejected and context.message_status not in (401, 403):
        return None
    if context.generated.get("test_type") == "negative":
        return None
    if _sends_authorization(context.generated):
        return None
    return context.diagnose(
        "generation",
        "authentication_missing",
        [f"observed {sorted(set(context.statuses))}", "no Authorization header in any step"],
        "Regenerate: the test must log in during setup and send the token on every step.",
    )


def _rule_generation_wrong_operation(context: _RuleContext) -> Diagnosis | None:
    """The test called a documented operation, but not the one it was asked to test.

    team16 is the case this rule exists for. Its contract says `POST /auth/users` *Logs
    user into the system* and `POST /users` *Create user*; the model registered against
    the login endpoint, which answered `404 "User ... not found"`, and thirteen of its
    twenty-four failures follow from that single confusion.

    A failure in ``setup`` is excluded, and the exclusion is not a detail. A test whose
    precondition fails never proceeds to the behaviour it was written for, so its target
    operation is missing from the traffic as a *consequence* of the failure rather than as
    its cause. Without this guard the rule was the most frequently fired of all -- 65
    diagnoses on the first full run, of which 63 were setup failures being told they had
    called the wrong endpoint, which would have sent the loop rewriting tests whose only
    fault was that they could not log in.
    """

    item = context.item
    if item is None or not context.exchanges or context.phase == "setup":
        return None
    target = (item.http_method.upper(), template_path(item.api_endpoint))
    called = {
        (str(exchange.get("method", "")).upper(), template_path(str(exchange.get("path", ""))))
        for exchange in context.exchanges
    }
    if target in called:
        return None
    return context.diagnose(
        "generation",
        "planned_operation_never_called",
        [
            f"strategy names {item.http_method} {item.api_endpoint}",
            f"test called {sorted(f'{m} {p}' for m, p in called)[:4]}",
        ],
        "Regenerate: the test never exercised the operation it was written for. Read each "
        "operation's summary rather than its path.",
    )


def _rule_generation_assumed_data(context: _RuleContext) -> Diagnosis | None:
    """It asserted a collection was populated without ever populating it.

    Every project starts from an empty database by design, so a test that reads a list it
    did not create can only pass by accident. Observed on both executed projects.
    """

    if "is_empty" not in context.message and "not_empty" not in context.message:
        return None
    if _creates_something(context.generated):
        return None
    return context.diagnose(
        "generation",
        "assumed_pre_existing_data",
        [context.message[:200], "no POST/PUT in setup or steps"],
        "Regenerate: the test must create the resource it then expects to find, because "
        "each execution starts from an empty database.",
    )


def _rule_contract_mismatch(context: _RuleContext) -> Diagnosis | None:
    """The body satisfied the contract and the service rejected it anyway.

    This must be decided before blaming the generator, and the two are indistinguishable
    from the error message alone. team05 is the case: its contract states that
    ``POST /users/login`` takes ``email`` and ``password``, the generated test sends
    exactly those two fields, and the service answers ``400 "Username and password are
    required"``. Its signup likewise demands ``emailNotificationsEnabled``, which the
    contract never mentions. Nothing is wrong with the test; the application and its own
    documentation disagree, and no iteration of the loop can reconcile them.
    """

    if not _rejected_as_malformed(context):
        return None
    offending = _first_rejected_step(context)
    if offending is None:
        return None
    step, spec = offending
    problems = body_problems((step.get("request") or {}).get("json_body"), spec)
    if problems:
        # The body genuinely failed the contract, so this is the generator's to fix.
        return None
    return context.diagnose(
        "contract_mismatch",
        "conformant_body_rejected",
        [
            context.message[:300],
            f"the body satisfies the documented fields: {sorted(spec.get('properties') or {})}",
        ],
        "Do not feed this back: the request matches the contract and the service "
        "contradicts it. Report the disagreement.",
    )


def _rule_generation_rejected_request(context: _RuleContext) -> Diagnosis | None:
    """The service rejected a request that did not, in fact, satisfy the contract."""

    if not _rejected_as_malformed(context):
        return None
    return context.diagnose(
        "generation",
        "request_rejected_as_malformed",
        [context.message[:300]],
        "Regenerate: the request does not satisfy the contract it was written against.",
    )


def _rejected_as_malformed(context: _RuleContext) -> bool:
    """A 400 complaining about the request, where success was expected."""

    if context.message_status != 400 and 400 not in context.statuses:
        return False
    if context.generated.get("test_type") in {"negative", "edge_case"}:
        return False
    return bool(_VALIDATION_MESSAGE.search(context.message))


def _first_rejected_step(context: _RuleContext) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """The step whose endpoint the failure message names, with its documented body spec.

    Returns None when the operation carries no readable schema, since nothing can then be
    concluded about whether the body conformed -- which is the honest answer for the
    quarter of this corpus whose request bodies are undescribed.
    """

    endpoint = _REJECTED_ENDPOINT.search(context.message)
    if endpoint is None:
        return None
    method, path = endpoint.group(1).upper(), endpoint.group(2)
    spec = context.evidence.body_specs.get((method, template_path(path)))
    if not spec:
        return None
    for step in _all_steps(context.generated):
        request = step.get("request") or {}
        if str(request.get("method", "")).upper() == method and template_path(
            str(request.get("path", ""))
        ) == template_path(path):
            return step, spec
    return None


def _rule_planning_endpoint_absent(context: _RuleContext) -> Diagnosis | None:
    """The planned operation answered 404 on a path carrying no identifier.

    A 404 on `/reports/{id}` says the resource is missing, which is a test's own doing. A
    404 on a path with no parameter says the route does not exist at all, so the
    requirement was mapped to an operation the service does not implement -- a planning
    mistake, not a writing one.
    """

    item = context.item
    if item is None or context.message_status != 404:
        return None
    if "{" in item.api_endpoint:
        return None
    return context.diagnose(
        "planning",
        "planned_endpoint_not_implemented",
        [f"{item.http_method} {item.api_endpoint} answered 404", context.message[:200]],
        "Replan this requirement: the contract documents an operation the service does "
        "not serve.",
    )


def _rule_sut_defect(context: _RuleContext) -> Diagnosis | None:
    """A 5xx that nothing else explains: the finding the pipeline exists to produce."""

    server_error = [code for code in context.statuses if 500 <= code < 600]
    if not server_error and (context.message_status or 0) < 500:
        return None
    return context.diagnose(
        "sut_defect",
        "server_error",
        [f"observed {sorted(set(server_error)) or context.message_status}", context.message[:200]],
        "Do not feed this back: the test appears sound and the service failed.",
    )


# --- helpers over a generated case -------------------------------------------------------


def _all_steps(case: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        step
        for phase in ("setup", "steps", "cleanup")
        for step in (case.get(phase) or [])
        if isinstance(step, dict)
    ]


def _sends_authorization(case: dict[str, Any]) -> bool:
    for step in _all_steps(case):
        headers = (step.get("request") or {}).get("headers") or {}
        if any(_AUTH_HEADER.match(str(key)) for key in headers):
            return True
    return False


def _creates_something(case: dict[str, Any]) -> bool:
    return any(
        str((step.get("request") or {}).get("method", "")).upper() in {"POST", "PUT", "PATCH"}
        for step in (case.get("setup") or []) + (case.get("steps") or [])
        if isinstance(step, dict)
    )


def _status_from_message(message: str) -> int | None:
    match = _STATUS_IN_MESSAGE.search(message)
    return int(match.group(1)) if match else None


def evaluate_run(
    run_dir: str | Path,
    *,
    iteration: int = 1,
    projects: list[str] | None = None,
) -> EvaluationReport:
    """Entry point used by the CLI and by the feedback loop.

    Reads the execution report the executor wrote, evaluates it, and persists the result
    beside it. Evaluation is cheap and deterministic, so it is always recomputed rather
    than cached: the artifacts it reads may have been regenerated since.
    """

    run_path = Path(run_dir)
    execution_path = run_path / "execution_report.json"
    if not execution_path.is_file():
        raise FileNotFoundError(
            f"No execution_report.json in {run_path}; run `execute` before `evaluate`"
        )
    execution = ExecutionReport.model_validate_json(execution_path.read_text(encoding="utf-8"))
    if projects:
        wanted = set(projects)
        execution = execution.model_copy(
            update={
                "projects": {
                    name: record
                    for name, record in execution.projects.items()
                    if name in wanted
                }
            }
        )
    report = Evaluator(run_path, iteration=iteration).run(execution)
    writer = ArtifactWriter(run_path)
    writer.write_json(f"evaluation_report.iteration{iteration}.json", report)
    writer.write_text(f"evaluation.iteration{iteration}.md", _markdown(report))
    return report


def _markdown(report: EvaluationReport) -> str:
    lines = [
        f"# Evaluation: iteration {report.iteration}",
        "",
        f"- Run: `{report.run_id}`",
        f"- Projects evaluated: {len(report.projects)}",
        f"- Projects another iteration could improve: {len(report.actionable_projects)}",
        "",
        "| project | pass rate | operation coverage | status-code coverage | 5xx | causes |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for name, project in sorted(report.projects.items()):
        metrics = project.metrics
        counted = ", ".join(
            f"{cause} {count}" for cause, count in sorted(project.cause_counts.items())
        )
        causes = project.inconclusive_reason or counted
        lines.append(
            f"| {name} | {_ratio(metrics.pass_rate)} | {_ratio(metrics.operation_coverage)} "
            f"| {_ratio(metrics.status_code_coverage)} | {metrics.server_errors_count or 0} "
            f"| {causes or '-'} |"
        )
    lines.extend(
        [
            "",
            "Causes are assigned by deterministic rules over the recorded evidence; each "
            "diagnosis in the JSON names the rule that fired and quotes what it matched, so "
            "a classification can be audited rather than taken on trust.",
            "",
        ]
    )
    return "\n".join(lines)


def _ratio(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


# --- loading -----------------------------------------------------------------------------


def _load_json_list(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        _logger.warning("Could not parse %s; treating it as empty", path)
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _load_strategy(path: Path) -> list[TestStrategyItem]:
    items: list[TestStrategyItem] = []
    for entry in _load_json_list(path):
        try:
            items.append(TestStrategyItem.model_validate(entry))
        except ValueError:
            _logger.debug("Skipping an unreadable strategy item in %s", path)
    return items


def _load_generated_cases(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    cases = report.get("cases") if isinstance(report, dict) else None
    if not isinstance(cases, list):
        return {}
    return {str(case["name"]): case for case in cases if isinstance(case, dict) and "name" in case}


def _load_documented_codes(path: Path) -> dict[tuple[str, str], set[str]]:
    codes: dict[tuple[str, str], set[str]] = {}
    for operation in _load_json_list(path):
        key = (str(operation.get("method", "")).upper(), str(operation.get("path", "")))
        codes[key] = {str(code) for code in operation.get("response_codes") or []}
    return codes


def _load_body_specs(path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    specs: dict[tuple[str, str], dict[str, Any]] = {}
    for operation in _load_json_list(path):
        spec = body_field_spec(operation.get("request_body_schema"))
        if not spec:
            continue
        key = (
            str(operation.get("method", "")).upper(),
            template_path(str(operation.get("path", ""))),
        )
        specs[key] = spec
    return specs
