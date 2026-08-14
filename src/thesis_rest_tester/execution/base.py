"""Non-LLM execution boundary for generated test suites."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from thesis_rest_tester.domain.execution import (
    CaseExecutionRecord,
    HttpExchangeRecord,
    ProjectOutcome,
)


@dataclass(frozen=True, slots=True)
class RunnerResult:
    """What one suite execution produced.

    Transport only: the persisted schemas live in ``domain.execution``. ``cases`` and
    ``exchanges`` are already those models, so the executor can write them straight into
    a report without a second conversion.
    """

    exit_code: int
    outcome: ProjectOutcome
    duration_seconds: float
    stdout: str = ""
    stderr: str = ""
    cases: list[CaseExecutionRecord] = field(default_factory=list)
    exchanges: list[HttpExchangeRecord] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class TestRunner(ABC):
    """Static execution interface; implementations must never invoke an LLM."""

    @abstractmethod
    def run(
        self,
        test_suite_path: Path,
        *,
        project_name: str,
        base_url: str,
        suite_timeout_seconds: int,
        artifact_dir: Path,
    ) -> RunnerResult:
        """Execute one generated suite against a running SUT.

        ``suite_timeout_seconds`` bounds the whole suite. It is deliberately not called
        ``timeout_seconds``: the per-request timeout is a different quantity, fixed when
        the suite was generated and baked into its ``conftest.py``, and cannot be changed
        here without regenerating.

        ``artifact_dir`` receives the evidence of the run -- the JUnit report, the
        captured output, the HTTP exchange log -- because those are results, not
        scratch files.
        """
