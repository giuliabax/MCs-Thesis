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


class StrictConfigModel(BaseModel):
    """Base class that rejects unknown configuration keys."""

    model_config = ConfigDict(extra="forbid")


class LLMConfig(StrictConfigModel):
    provider: Literal["groq"] = "groq"
    model: str
    temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4096, gt=0)

    @field_validator("model")
    @classmethod
    def model_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("llm.model must not be blank")
        return value.strip()


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
    reset_command: str | None = None
    timeout_seconds: int = Field(default=30, gt=0)


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
