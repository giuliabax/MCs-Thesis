"""Application configuration loading and validation."""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_UNRESOLVED_ENV = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9_.-]+$")

# Identifiers of the planning agents, used to validate per-agent reasoning settings.
_PLANNING_AGENTS = (
    "requirements_analyst",
    "api_understanding",
    "requirement_api_matcher",
    "test_strategy_planner",
)
# Agents of the generation stage, configured through the same per-agent settings.
_GENERATION_AGENTS = ("test_writer",)
# The evaluation stage's only agent. Metric collection and failure classification are
# deterministic; the model is used solely to turn a diagnosis into an instruction.
_EVALUATION_AGENTS = ("feedback_manager",)
_CONFIGURABLE_AGENTS = (*_PLANNING_AGENTS, *_GENERATION_AGENTS, *_EVALUATION_AGENTS)


class StrictConfigModel(BaseModel):
    """Base class that rejects unknown configuration keys."""

    model_config = ConfigDict(extra="forbid")


class AgentLLMOverride(StrictConfigModel):
    """Per-agent LLM settings that depart from the parent llm config.

    An override may reroute the agent to a different provider/model, give it its own
    max_tokens, or both. Temperature and timeout are always inherited.

    max_tokens is not merely a length cap for a reasoning agent: it is also how much
    room the model has to deliberate before answering. Agents whose accuracy depends
    on that budget therefore need to set it independently of the rest of the run.
    """

    provider: Literal["groq", "lmstudio"] | None = None
    model: str | None = None
    base_url: str | None = None
    max_tokens: int | None = Field(default=None, gt=0)

    @field_validator("model")
    @classmethod
    def model_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("llm.overrides[...].model must not be blank")
        return value.strip()

    @model_validator(mode="after")
    def validate_override(self) -> AgentLLMOverride:
        # Rerouting needs both halves: a provider without a model (or the reverse)
        # cannot name a target, and silently inheriting one half would send the agent
        # somewhere the configuration never states.
        if (self.provider is None) != (self.model is None):
            raise ValueError(
                "llm.overrides[...] must set provider and model together, or neither"
            )
        if self.provider is None and self.base_url is not None:
            raise ValueError("llm.overrides[...].base_url requires provider and model")
        if self.provider is None and self.max_tokens is None:
            raise ValueError(
                "llm.overrides[...] must set provider/model, max_tokens, or both"
            )
        return self

    @property
    def reroutes(self) -> bool:
        """Whether this override names a different provider/model to call."""

        return self.provider is not None and self.model is not None


class LLMConfig(StrictConfigModel):
    provider: Literal["groq", "lmstudio"] = "lmstudio"
    model: str
    temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4096, gt=0)
    base_url: str = Field(default="http://localhost:1234/v1")
    timeout_seconds: float = Field(default=1200.0, gt=0)
    # Planning agents allowed to use the model's reasoning phase. Reasoning is
    # load-bearing for schema completeness on the matcher and planning depth on the
    # strategy planner, but superfluous and slow on the extractive agents (verified
    # with Qwen3.5-9b on 8 GB VRAM). Set to [] to disable reasoning everywhere.
    reasoning_agents: list[str] = Field(
        default_factory=lambda: [
            "requirement_api_matcher",
            "test_strategy_planner",
        ]
    )
    # Split strategy planning into batches of requirements instead of one call per
    # project. Introduced when a 16k context window could not hold a whole project's
    # planning payload; a batch is planned blind to the project-wide budget and
    # diversity targets, which are then enforced post hoc on the merged result. With a
    # context window large enough for the whole payload, one call plans against the
    # real objective and is both faster and better informed, so this defaults off.
    batch_strategy_planner: bool = False
    # Per-agent provider/model overrides, keyed by planning agent identifier. Used to
    # route heavy agents (e.g. the planner) to a remote provider while the rest stay
    # local. Names must match the planning agent identifiers.
    overrides: dict[str, AgentLLMOverride] = Field(default_factory=dict)

    @field_validator("model")
    @classmethod
    def model_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("llm.model must not be blank")
        return value.strip()

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("llm.base_url must be an HTTP(S) URL")
        return value.rstrip("/")

    @field_validator("reasoning_agents")
    @classmethod
    def validate_reasoning_agents(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - set(_CONFIGURABLE_AGENTS))
        if unknown:
            raise ValueError(
                "llm.reasoning_agents contains unknown agent names: "
                + ", ".join(unknown)
                + "; valid names are: "
                + ", ".join(_CONFIGURABLE_AGENTS)
            )
        return value

    def max_tokens_for(self, agent_name: str) -> int:
        """Resolve one agent's output budget, honoring a per-agent override."""

        override = self.overrides.get(agent_name)
        if override is not None and override.max_tokens is not None:
            return override.max_tokens
        return self.max_tokens

    @field_validator("overrides")
    @classmethod
    def validate_overrides(
        cls, value: dict[str, AgentLLMOverride]
    ) -> dict[str, AgentLLMOverride]:
        unknown = sorted(set(value) - set(_CONFIGURABLE_AGENTS))
        if unknown:
            raise ValueError(
                "llm.overrides contains unknown agent names: "
                + ", ".join(unknown)
                + "; valid names are: "
                + ", ".join(_CONFIGURABLE_AGENTS)
            )
        return value


class RequirementsInputConfig(StrictConfigModel):
    description_pdf: Path
    user_stories_xlsx: Path
    faq_pdf: Path


class ProjectInputConfig(StrictConfigModel):
    name: str
    openapi_path: Path
    sut_base_url: str

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value or not _SAFE_RUN_ID.fullmatch(value):
            raise ValueError(
                "project name may contain only letters, numbers, dots, dashes, and underscores"
            )
        return value

    @field_validator("sut_base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("project sut_base_url must be an HTTP(S) URL")
        return value.rstrip("/")


class InputsConfig(StrictConfigModel):
    requirements: RequirementsInputConfig
    projects: list[ProjectInputConfig] = Field(default_factory=list)
    # Legacy single-project fields remain supported for existing configurations.
    openapi_path: Path | None = None
    sut_base_url: str | None = None

    @model_validator(mode="after")
    def validate_project_inputs(self) -> InputsConfig:
        uses_legacy = self.openapi_path is not None or self.sut_base_url is not None
        if self.projects and uses_legacy:
            raise ValueError(
                "use either inputs.projects or the legacy openapi_path/sut_base_url fields"
            )
        if not self.projects and (self.openapi_path is None or self.sut_base_url is None):
            raise ValueError(
                "configure at least one project, or both openapi_path and sut_base_url"
            )
        names = [project.name for project in self.projects]
        if len(names) != len(set(names)):
            raise ValueError("inputs.projects contains duplicate project names")
        if self.sut_base_url is not None:
            parsed = urlparse(self.sut_base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("inputs.sut_base_url must be an HTTP(S) URL")
            self.sut_base_url = self.sut_base_url.rstrip("/")
        return self

    def configured_projects(self, legacy_name: str) -> list[ProjectInputConfig]:
        if self.projects:
            return self.projects
        return [
            ProjectInputConfig(
                name=legacy_name,
                openapi_path=self.openapi_path,
                sut_base_url=self.sut_base_url,
            )
        ]


class ExecutionConfig(StrictConfigModel):
    runner: Literal["python_requests", "newman"] = "python_requests"
    # Deprecated and unread: per-project reset now lives in the SUT manifest, where the
    # heterogeneity between projects actually is. The field is kept because every run's
    # config.resolved.yaml is a persisted artifact that the executor loads, and this model
    # forbids unknown keys -- dropping the field would make every past run unexecutable.
    reset_command: str | None = None
    # Per-HTTP-request timeout. Consumed at generation time and baked into the suite's
    # conftest.py as REQUEST_TIMEOUT_SECONDS, so changing it only affects suites
    # generated afterwards. The two timeouts below bound execution instead.
    timeout_seconds: int = Field(default=30, gt=0)
    # A cold Node build with no node_modules takes minutes; keeping it separate from the
    # readiness wait is what makes "the build failed" distinguishable from "the app never
    # came up", instead of waiting half an hour to learn which.
    build_timeout_seconds: int = Field(default=1800, gt=0)
    startup_timeout_seconds: int = Field(default=180, gt=0)
    # Wall clock for one suite. JUnit XML is written only at session end, so exceeding
    # this loses every case outcome for that project; size it generously.
    suite_timeout_seconds: int = Field(default=3600, gt=0)
    teardown_volumes: bool = True


class BudgetConfig(StrictConfigModel):
    max_iterations: int = Field(default=3, gt=0)
    max_tests_per_iteration: int = Field(default=30, gt=0)
    max_llm_calls: int = Field(default=50, ge=3)


class OutputConfig(StrictConfigModel):
    runs_dir: Path = Path("data/runs")


class AppConfig(StrictConfigModel):
    project_name: str
    run_id: str | None = None
    llm: LLMConfig
    inputs: InputsConfig
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)

    @field_validator("project_name")
    @classmethod
    def project_name_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("project_name must not be blank")
        return value.strip()

    @field_validator("run_id")
    @classmethod
    def validate_run_id(cls, value: str | None) -> str | None:
        if value is not None and not _SAFE_RUN_ID.fullmatch(value):
            raise ValueError(
                "run_id may contain only letters, numbers, dots, dashes, and underscores"
            )
        return value


def load_config(path: str | Path) -> AppConfig:
    """Load YAML configuration, expand environment variables, and validate it."""

    config_path = Path(path)
    if not config_path.is_file():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    dotenv_path = Path.cwd() / ".env"
    if dotenv_path.is_file():
        load_dotenv(dotenv_path=dotenv_path, override=False)

    expanded = os.path.expandvars(config_path.read_text(encoding="utf-8"))
    unresolved = sorted(set(_UNRESOLVED_ENV.findall(expanded)))
    if unresolved:
        variables = ", ".join(unresolved)
        raise ValueError(f"Unresolved environment variables in {config_path}: {variables}")

    raw = yaml.safe_load(expanded)
    if not isinstance(raw, dict):
        raise ValueError(f"Configuration root must be a YAML mapping: {config_path}")

    if raw.get("run_id") is None:
        raw["run_id"] = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return AppConfig.model_validate(raw)
