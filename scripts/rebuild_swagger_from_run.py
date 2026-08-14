"""Rebuild each project's swagger.yaml from a run's normalized operations.

The student projects' OpenAPI documents are local, gitignored inputs. When they are
lost, the normalized ``openapi_operations.json`` saved with every run still holds
every field the planning pipeline consumes, so the specs can be rebuilt from a past
run instead of re-derived from source code.

That matters for comparability: re-reading the sources would produce different prose,
and spec provenance is a documented confounder for recall (see
docs/run-results-2026-07-21.md section 4). Rebuilding from a run reproduces the exact
input that run used.

The rebuilt document is faithful to what the loader reads, not to the original file:
components, servers, and schemas the loader discards are not restored. This is
lossless for the pipeline (OpenAPILoader.load exposes ``raw_document`` but nothing
consumes it) and lossy for a human reader.

Usage:
    python scripts/rebuild_swagger_from_run.py --run-dir data/runs/<run_id>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from thesis_rest_tester.loaders import OpenAPILoader  # noqa: E402

_BEARER_SCHEME = {"type": "http", "scheme": "bearer"}


def _operation_document(operation: dict[str, Any]) -> dict[str, Any]:
    """Render one normalized operation back into an OpenAPI 3 operation object."""

    document: dict[str, Any] = {}
    # Keys are emitted only when present so a reload cannot invent a value the
    # original document did not carry (notably summary/description, whose absence
    # is itself meaningful signal for the matcher).
    if operation.get("operation_id") is not None:
        document["operationId"] = operation["operation_id"]
    if operation.get("summary") is not None:
        document["summary"] = operation["summary"]
    if operation.get("description") is not None:
        document["description"] = operation["description"]
    if operation.get("tags"):
        document["tags"] = operation["tags"]
    if operation.get("parameters"):
        document["parameters"] = operation["parameters"]
    if operation.get("request_body_schema") is not None:
        document["requestBody"] = {
            "content": {"application/json": {"schema": operation["request_body_schema"]}}
        }
    # auth_required is tri-state: None means the source document said nothing about
    # security, which the loader reports differently from an explicit empty list.
    auth_required = operation.get("auth_required")
    if auth_required is True:
        document["security"] = [{"bearerAuth": []}]
    elif auth_required is False:
        document["security"] = []
    document["responses"] = {
        str(code): {"description": "Reconstructed from a previous run."}
        for code in operation.get("response_codes") or []
    }
    return document


def build_specification(operations: list[dict[str, Any]], project_name: str) -> dict[str, Any]:
    paths: dict[str, dict[str, Any]] = {}
    for operation in operations:
        path = operation["path"]
        method = operation["method"].lower()
        paths.setdefault(path, {})[method] = _operation_document(operation)

    specification: dict[str, Any] = {
        "openapi": "3.0.3",
        "info": {
            "title": f"{project_name} REST API",
            "version": "1.0.0",
            "description": (
                "Rebuilt from a previous run's normalized operations. Faithful to the "
                "operations the planning pipeline consumed; not a hand-written document."
            ),
        },
        "paths": paths,
    }
    if any(operation.get("auth_required") for operation in operations):
        specification["components"] = {"securitySchemes": {"bearerAuth": _BEARER_SCHEME}}
    return specification


def _normalize(operations: list[Any]) -> list[dict[str, Any]]:
    """Project operations onto comparable dicts, ordered so reload order cannot matter."""

    rendered = []
    for operation in operations:
        item = operation if isinstance(operation, dict) else operation.model_dump(mode="json")
        rendered.append({key: item[key] for key in sorted(item)})
    return sorted(rendered, key=lambda item: (item["path"], item["method"]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--projects-dir", default=REPOSITORY_ROOT / "projects", type=Path)
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace existing swagger.yaml files instead of leaving them untouched.",
    )
    arguments = parser.parse_args()

    source_root = arguments.run_dir / "projects"
    if not source_root.is_dir():
        parser.error(f"No projects directory inside {arguments.run_dir}")

    written = 0
    skipped = 0
    failures: list[str] = []
    for project_dir in sorted(source_root.iterdir()):
        operations_file = project_dir / "openapi_operations.json"
        if not operations_file.is_file():
            continue
        name = project_dir.name
        target = arguments.projects_dir / name / "swagger.yaml"
        if target.exists() and not arguments.overwrite:
            print(f"  = {name}: swagger.yaml already present, left untouched")
            skipped += 1
            continue
        if not target.parent.is_dir():
            print(f"  ! {name}: no {target.parent} directory, skipped")
            failures.append(name)
            continue

        operations = json.loads(operations_file.read_text(encoding="utf-8"))
        specification = build_specification(operations, name)
        target.write_text(
            yaml.safe_dump(specification, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

        # A rebuild is only useful if it reloads to the same operations the run saw.
        reloaded = OpenAPILoader().load(target).operations
        if _normalize(reloaded) == _normalize(operations):
            print(f"  + {name}: {len(operations)} operations, round-trip verified")
            written += 1
        else:
            print(f"  ! {name}: round-trip MISMATCH")
            failures.append(name)

    print(f"\n{written} rebuilt, {skipped} left untouched, {len(failures)} failed")
    if failures:
        print("failed: " + ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
