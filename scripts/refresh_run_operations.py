"""Re-read the contracts into an existing run's ``openapi_operations.json``.

Generation reads its operations from the run directory, not from the specification, so
a run planned before a loader fix keeps whatever the loader produced at the time. That
is normally the point -- a run is a record of what was actually used -- but it also
means a correction to how contracts are read cannot reach a run without replanning it.

Replanning would be the wrong instrument here. The correction this script exists for
resolves ``$ref`` pointers into the schemas they name: it changes how request bodies are
*described*, not which operations exist. The strategy was planned against the operations,
and those are unchanged, so the plan remains exactly as valid as it was. Replanning would
discard a strategy that took hours to produce and replace it with a different one, making
the before-and-after incomparable for no reason.

So the refresh is deliberately narrow, and refuses anything wider: if the reloaded
contract does not describe exactly the same operations, the run is left untouched and the
difference is reported. Whoever asked for it can then decide to replan properly.

    python scripts/refresh_run_operations.py --run-dir data/runs/<id> \
        --config configs/participium.example.yaml [--project NAME ...]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from thesis_rest_tester.config import load_config  # noqa: E402
from thesis_rest_tester.domain.compact import body_field_spec  # noqa: E402
from thesis_rest_tester.loaders.openapi_loader import OpenAPILoader  # noqa: E402

_ARTIFACT = "openapi_operations.json"


def _signature(entries: list[dict]) -> set[tuple[str, str]]:
    return {(str(entry["method"]).upper(), str(entry["path"])) for entry in entries}


def _described(entries: list[dict]) -> int:
    return sum(1 for entry in entries if body_field_spec(entry.get("request_body_schema")))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--project", action="append", default=None)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change without writing anything",
    )
    arguments = parser.parse_args()

    config = load_config(arguments.config)
    wanted = set(arguments.project) if arguments.project else None
    loader = OpenAPILoader()
    refreshed = skipped = 0

    for project in config.inputs.projects:
        if wanted is not None and project.name not in wanted:
            continue
        artifact = arguments.run_dir / "projects" / project.name / _ARTIFACT
        if not artifact.is_file():
            continue

        current = json.loads(artifact.read_text(encoding="utf-8"))
        operations = loader.load(project.openapi_path).operations
        reloaded = [operation.model_dump(mode="json") for operation in operations]

        added = _signature(reloaded) - _signature(current)
        removed = _signature(current) - _signature(reloaded)
        if added or removed:
            skipped += 1
            print(f"{project.name}: SKIPPED -- the operations differ, this needs a replan")
            for method, path in sorted(removed)[:5]:
                print(f"    only in the run:      {method} {path}")
            for method, path in sorted(added)[:5]:
                print(f"    only in the contract: {method} {path}")
            continue

        before, after = _described(current), _described(reloaded)
        if not arguments.dry_run:
            artifact.write_text(
                json.dumps(reloaded, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        refreshed += 1
        print(
            f"{project.name}: {len(reloaded)} operations, "
            f"request bodies described {before} -> {after}"
        )

    verb = "would refresh" if arguments.dry_run else "refreshed"
    print(f"\n{verb} {refreshed} project(s); {skipped} skipped")
    return 1 if skipped else 0


if __name__ == "__main__":
    raise SystemExit(main())
