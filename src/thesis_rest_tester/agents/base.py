"""Reusable behavior for strict-JSON planning agents."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from thesis_rest_tester.artifacts.writer import ArtifactWriter
from thesis_rest_tester.domain.models import AgentOutput
from thesis_rest_tester.llm.base import LLMClient


class AgentResponseError(RuntimeError):
    """Raised after an invalid model response has been saved as an artifact."""


class BaseAgent[T]:
    def __init__(
        self,
        *,
        name: str,
        prompt_path: str | Path,
        llm_client: LLMClient,
        artifact_writer: ArtifactWriter,
        response_adapter: TypeAdapter[T],
        raw_artifact_name: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        think: bool = True,
    ) -> None:
        self.name = name
        self._llm_client = llm_client
        self._artifact_writer = artifact_writer
        self._response_adapter = response_adapter
        self._raw_artifact_name = raw_artifact_name
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._think = think
        self._logger = logging.getLogger(f"{__name__}.{name}")

        path = Path(prompt_path)
        if not path.is_file():
            raise FileNotFoundError(f"Prompt template for {name} not found: {path}")
        self._system_prompt = path.read_text(encoding="utf-8")

    def call_and_validate(
        self,
        user_prompt: str,
        *,
        max_validation_retries: int = 1,
    ) -> tuple[T, AgentOutput]:
        self._logger.info("Running %s", self.name)
        current_prompt = user_prompt
        last_error: json.JSONDecodeError | ValidationError | None = None

        for attempt in range(max_validation_retries + 1):
            response = self._llm_client.generate(
                system_prompt=self._system_prompt,
                user_prompt=current_prompt,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
                think=self._think,
            )
            self._artifact_writer.write_text(self._raw_artifact_name, response.text)

            try:
                parsed = self._parse_json(response.text)
                parsed = self._preprocess_parsed_json(parsed)
                validated = self._response_adapter.validate_python(parsed)
            except (json.JSONDecodeError, ValidationError) as exc:
                last_error = exc
                if attempt >= max_validation_retries:
                    error_kind = (
                        "invalid JSON"
                        if isinstance(exc, json.JSONDecodeError)
                        else "JSON that does not match its schema"
                    )
                    raise AgentResponseError(
                        f"{self.name} returned {error_kind}; raw output was saved to "
                        f"{self._raw_artifact_name}: {exc}"
                    ) from exc

                attempt_name = self._validation_attempt_name(attempt + 1)
                self._artifact_writer.write_text(attempt_name, response.text)
                self._logger.warning(
                    "%s returned an invalid response; requesting one schema repair",
                    self.name,
                )
                current_prompt = self._repair_prompt(user_prompt, exc)
                continue

            normalized = self._response_adapter.dump_python(validated, mode="json")
            self._logger.info("Completed %s", self.name)
            return validated, AgentOutput(
                agent_name=self.name,
                raw_text=response.text,
                parsed_json=normalized,
                token_usage=response.token_usage,
                model=response.model,
            )

        raise AgentResponseError(f"{self.name} could not produce a valid response: {last_error}")

    @staticmethod
    def _parse_json(raw_text: str) -> object:
        """Parse JSON while tolerating only boundary Markdown fences."""

        candidate = raw_text.strip()
        lines = candidate.splitlines()
        opening_fences = {"```", "```json", "~~~", "~~~json"}
        closing_fences = {"```", "~~~"}
        if lines and lines[0].strip().lower() in opening_fences:
            lines = lines[1:]
        if lines and lines[-1].strip() in closing_fences:
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()
        return json.loads(candidate)

    def _preprocess_parsed_json(self, parsed: object) -> object:
        """Allow subclasses to normalize common model response variants before validation."""

        return parsed

    def _validation_attempt_name(self, attempt: int) -> str:
        suffix = ".raw.txt"
        if self._raw_artifact_name.endswith(suffix):
            base = self._raw_artifact_name[: -len(suffix)]
            return f"{base}.validation_attempt{attempt}.raw.txt"
        return f"{self._raw_artifact_name}.validation_attempt{attempt}"

    @staticmethod
    def _repair_prompt(original_prompt: str, error: Exception) -> str:
        error_text = str(error)
        if len(error_text) > 1200:
            error_text = error_text[:1200] + "..."
        return (
            original_prompt
            + "\n\nCORRECTION REQUIRED: Your previous response could not be parsed or did not "
            "match the required schema. Return exactly one complete JSON value matching the "
            "system-prompt schema. Do not use Markdown fences, prose, comments, or null for "
            "required string fields. Keep all arrays and objects properly closed.\n"
            "Validation error: "
            + error_text
        )
