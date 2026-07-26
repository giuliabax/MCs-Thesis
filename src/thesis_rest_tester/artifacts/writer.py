"""Write workflow artifacts without exposing half-written files."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel


class ArtifactWriter:
    def __init__(self, run_dir: str | Path) -> None:
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def write_text(self, name: str, content: str) -> Path:
        destination = self._destination(name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            # No newline translation: csv.writer already emits \r\n, and on Windows
            # the default would turn that into \r\r\n, which parsers read as a blank
            # row between every record.
            newline="",
            dir=destination.parent,
            delete=False,
        ) as temporary:
            temporary.write(content)
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, destination)
        return destination

    def write_json(self, name: str, value: Any) -> Path:
        serializable = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
        return self.write_text(name, json.dumps(serializable, indent=2, ensure_ascii=False) + "\n")

    def write_yaml(self, name: str, value: Any) -> Path:
        serializable = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
        content = yaml.safe_dump(serializable, sort_keys=False, allow_unicode=True)
        return self.write_text(name, content)

    def _destination(self, name: str) -> Path:
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"Artifact name must stay inside the run directory: {name}")
        return self.run_dir / relative
