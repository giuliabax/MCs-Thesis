from __future__ import annotations

import re
from pathlib import Path

import pytest

from thesis_rest_tester.config import load_config


def _config_text(run_id: str = "null") -> str:
    return f"""
project_name: config-test
run_id: {run_id}
llm:
  provider: groq
  model: ${{GROQ_MODEL}}
  temperature: 0.1
  max_tokens: 100
inputs:
  requirements:
    description_pdf: description.pdf
    user_stories_xlsx: stories.xlsx
    faq_pdf: faq.pdf
  openapi_path: openapi.yaml
  sut_base_url: http://localhost:8080/
execution:
  runner: python_requests
  reset_command: null
  timeout_seconds: 10
budget:
  max_iterations: 1
  max_tests_per_iteration: 3
  max_llm_calls: 3
output:
  runs_dir: runs
"""


def test_config_loading_expands_environment_and_creates_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GROQ_MODEL", "test-model")
    path = tmp_path / "config.yaml"
    path.write_text(_config_text(), encoding="utf-8")

    config = load_config(path)

    assert config.llm.model == "test-model"
    assert config.inputs.sut_base_url == "http://localhost:8080"
    assert config.run_id is not None
    assert re.fullmatch(r"\d{8}T\d{6}Z", config.run_id)


def test_config_preserves_explicit_run_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_MODEL", "test-model")
    path = tmp_path / "config.yaml"
    path.write_text(_config_text("fixed-run"), encoding="utf-8")

    assert load_config(path).run_id == "fixed-run"


def test_config_rejects_unresolved_environment_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GROQ_MODEL", raising=False)
    path = tmp_path / "config.yaml"
    path.write_text(_config_text(), encoding="utf-8")

    with pytest.raises(ValueError, match="Unresolved environment variables"):
        load_config(path)


def test_config_accepts_multiple_projects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_MODEL", "test-model")
    text = _config_text().replace(
        "  openapi_path: openapi.yaml\n  sut_base_url: http://localhost:8080/",
        """  projects:
    - name: team-a
      openapi_path: team-a.yaml
      sut_base_url: http://localhost:8080/
    - name: team-b
      openapi_path: team-b.yaml
      sut_base_url: http://localhost:8081/""",
    )
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")

    config = load_config(path)

    projects = config.inputs.configured_projects(config.project_name)
    assert [project.name for project in projects] == ["team-a", "team-b"]
    assert projects[1].sut_base_url == "http://localhost:8081"


def test_config_defaults_to_lmstudio_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LMSTUDIO_MODEL", "qwen-test-model")
    text = _config_text().replace(
        "llm:\n  provider: groq\n  model: ${GROQ_MODEL}\n  temperature: 0.1\n  max_tokens: 100",
        "llm:\n  model: ${LMSTUDIO_MODEL}\n  temperature: 0.1\n  max_tokens: 100",
    )
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")

    config = load_config(path)

    assert config.llm.provider == "lmstudio"
    assert config.llm.model == "qwen-test-model"
    assert config.llm.base_url == "http://localhost:1234/v1"
    assert config.llm.timeout_seconds == 1200.0


def test_config_accepts_lmstudio_provider_with_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LMSTUDIO_MODEL", "qwen-test-model")
    text = _config_text().replace(
        "llm:\n  provider: groq\n  model: ${GROQ_MODEL}\n  temperature: 0.1\n  max_tokens: 100",
        "llm:\n  provider: lmstudio\n  model: ${LMSTUDIO_MODEL}\n  temperature: 0.1\n"
        "  max_tokens: 100\n  base_url: http://127.0.0.1:5678/v1\n  timeout_seconds: 60",
    )
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")

    config = load_config(path)

    assert config.llm.provider == "lmstudio"
    assert config.llm.base_url == "http://127.0.0.1:5678/v1"
    assert config.llm.timeout_seconds == 60.0


def test_config_rejects_duplicate_project_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GROQ_MODEL", "test-model")
    text = _config_text().replace(
        "  openapi_path: openapi.yaml\n  sut_base_url: http://localhost:8080/",
        """  projects:
    - name: duplicate
      openapi_path: team-a.yaml
      sut_base_url: http://localhost:8080
    - name: duplicate
      openapi_path: team-b.yaml
      sut_base_url: http://localhost:8081""",
    )
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate project names"):
        load_config(path)
