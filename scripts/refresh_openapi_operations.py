"""Rewrite a run's `openapi_operations.json` from the specification it came from.

A run's planning artifacts are written once and then carried forward: `replan` copies
`openapi_operations.json` verbatim, and `generate` reads whatever the project folder
holds. That is deliberate---the coverage decisions a replan must not disturb live in those
files---but it also means a run started before a loader fix keeps the operations as they
were read then, and no amount of replanning or regeneration will refresh them.

That is not hypothetical. The campaign in `data/runs/20260816T235801Z-*` inherits planning
artifacts written on 7 August, before `$ref` pointers were followed into request bodies, so
77 of the corpus's 258 described bodies reach the Test Writer as a bare `{"$ref": ...}`.
The writer cannot see the fields such a body requires, so it guesses them, and team13's
registration fails with "request/body must have required property 'firstName'".

This script re-reads each project's specification with the current loader and writes the
operations again. Nothing else is touched: the strategy, the coverage and the analysis
stay exactly as they were, so a suite regenerated afterwards differs from its predecessor
only in what the writer was told about the contract.

    python scripts/refresh_openapi_operations.py --config CONFIG --run-dir RUN [--project NAME]
    python scripts/refresh_openapi_operations.py --config CONFIG --run-dir RUN --check
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from thesis_rest_tester.artifacts import ArtifactWriter
from thesis_rest_tester.config import load_config
from thesis_rest_tester.loaders import OpenAPILoader


def _bare_refs(schemas: list[dict | None]) -> int:
    """Bodies that are still an unfollowed pointer, and so carry no fields."""

    return sum(1 for s in schemas if isinstance(s, dict) and set(s) == {"$ref"})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--project", action="append", default=None)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report what would change and write nothing",
    )
    arguments = parser.parse_args()

    config = load_config(arguments.config)
    total_before = total_after = 0

    for project in config.inputs.configured_projects(config.project_name):
        if arguments.project and project.name not in arguments.project:
            continue
        target = arguments.run_dir / "projects" / project.name / "openapi_operations.json"
        if not target.is_file():
            print(f"{project.name}: no operations artifact in the run, skipped")
            continue

        current = json.loads(target.read_text(encoding="utf-8"))
        before = _bare_refs([entry.get("request_body_schema") for entry in current])
        operations = OpenAPILoader().load(project.openapi_path).operations
        refreshed = [operation.model_dump(mode="json") for operation in operations]
        after = _bare_refs([entry.get("request_body_schema") for entry in refreshed])

        total_before += before
        total_after += after
        verb = "would resolve" if arguments.check else "resolved"
        note = f"{verb} {before - after}" if before != after else "no change"
        print(
            f"{project.name}: {len(current)} -> {len(refreshed)} operations, "
            f"unresolved bodies {before} -> {after} ({note})"
        )

        if arguments.check or refreshed == current:
            continue
        # Written through the same writer the orchestrator uses, so the result is
        # byte-identical to what a fresh planning run would have produced.
        ArtifactWriter(target.parent).write_json("openapi_operations.json", refreshed)

    print(f"\nunresolved request bodies: {total_before} -> {total_after}")
    if arguments.check:
        print("nothing written (--check)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
