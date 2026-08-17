"""Consolidate per-project planning runs into a single run folder.

The 18 Participium projects were planned across many runs: a few batched attempts
that died partway, then one run per project. This picks the newest COMPLETE result
for each project, copies them into one folder, and regenerates the root-level
coverage artifacts, so `evaluate-coverage --run-dir` can score all projects at once.

Two safety rules, because a silent mistake here would corrupt the thesis results:

* Dry-run folders (deterministic mock LLM fixtures) are detected and refused. They
  look "complete" but contain fabricated strategies.
* Every contributing run must have produced the byte-identical requirements
  analysis, otherwise the projects were not scored against the same requirements
  and the merged coverage matrix would be meaningless.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from thesis_rest_tester.config import load_config  # noqa: E402
from thesis_rest_tester.domain.coverage import ProjectRequirementCoverage  # noqa: E402
from thesis_rest_tester.domain.models import WorkflowPlan  # noqa: E402
from thesis_rest_tester.domain.schemas import RequirementsAnalysis  # noqa: E402
from thesis_rest_tester.orchestrator import Orchestrator  # noqa: E402

# Phrases only the dry-run fixtures emit.
_MOCK_MARKERS = ("Generate an independent happy_path requests test",)
_ROOT_FILES = (
    "requirements_analysis.json",
    "requirements_analysis.raw.txt",
    "requirements_compact.txt",
)


def _is_complete(project_dir: Path) -> bool:
    return (project_dir / "workflow_plan.json").is_file()


def _is_mock(project_dir: Path) -> bool:
    strategy = project_dir / "test_strategy.json"
    if not strategy.is_file():
        return False
    try:
        items = json.loads(strategy.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    prompts = " ".join(str(item.get("prompt", "")) for item in items if isinstance(item, dict))
    return any(marker in prompts for marker in _MOCK_MARKERS)


def _requirements_signature(run_dir: Path) -> str | None:
    path = run_dir / "requirements_analysis.json"
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    items = payload.get("requirements") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return None
    normalized = sorted(
        (str(item.get("id")), str(item.get("text", "")).strip()) for item in items
    )
    return json.dumps(normalized, ensure_ascii=False)


def select_sources(runs_dir: Path, wanted: set[str]) -> tuple[dict[str, Path], list[str]]:
    """Newest complete, non-mock project folder wins. Returns (project -> dir, notes).

    Only projects named in the config are considered, so unrelated leftovers (old
    quicktest runs from a different architecture) cannot slip into the results.
    """
    chosen: dict[str, tuple[float, Path]] = {}
    notes: list[str] = []
    for run_dir in sorted(p for p in runs_dir.iterdir() if p.is_dir()):
        projects_dir = run_dir / "projects"
        if not projects_dir.is_dir():
            continue
        for project_dir in sorted(p for p in projects_dir.iterdir() if p.is_dir()):
            name = project_dir.name
            if name not in wanted:
                continue
            if _is_mock(project_dir):
                notes.append(f"skipped {run_dir.name}/{name}: mock (dry-run) artifacts")
                continue
            if not _is_complete(project_dir):
                continue
            stamp = max(f.stat().st_mtime for f in project_dir.iterdir() if f.is_file())
            if name not in chosen or stamp > chosen[name][0]:
                chosen[name] = (stamp, project_dir)
    return {name: path for name, (_, path) in sorted(chosen.items())}, notes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", default=str(REPO_ROOT / "data" / "runs"))
    parser.add_argument("--config", default=str(REPO_ROOT / "configs" / "participium.example.yaml"))
    parser.add_argument(
        "--out",
        default=None,
        help="Output run folder (default: data/runs/<ts>-consolidated)",
    )
    parser.add_argument("--expect", type=int, default=None, help="Fail if fewer projects are found")
    args = parser.parse_args()

    config = load_config(args.config)
    wanted = {p.name for p in config.inputs.configured_projects(config.project_name)}
    expected = args.expect if args.expect is not None else len(wanted)

    runs_dir = Path(args.runs_dir)
    sources, notes = select_sources(runs_dir, wanted)
    for note in notes:
        print(f"  ! {note}")
    for name in sorted(wanted - set(sources)):
        print(f"  ! MISSING: {name} has no complete run")
    if not sources:
        print("No complete projects found.", file=sys.stderr)
        return 1

    # Every contributing run must share one requirements analysis.
    signatures: dict[str, list[str]] = {}
    for name, project_dir in sources.items():
        signature = _requirements_signature(project_dir.parent.parent)
        if signature is None:
            print(f"{name}: source run has no requirements_analysis.json", file=sys.stderr)
            return 1
        signatures.setdefault(signature, []).append(name)
    if len(signatures) > 1:
        print(
            f"ABORT: the sources disagree on the requirements analysis "
            f"({len(signatures)} distinct sets). Merging them would compare projects against "
            f"different requirements.",
            file=sys.stderr,
        )
        for index, names in enumerate(signatures.values(), start=1):
            print(f"  set {index}: {', '.join(names)}", file=sys.stderr)
        return 1

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-consolidated"
    out_dir = Path(args.out) if args.out else runs_dir / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "projects").mkdir(exist_ok=True)

    reference_run = next(iter(sources.values())).parent.parent
    for filename in _ROOT_FILES:
        source = reference_run / filename
        if source.is_file():
            shutil.copy2(source, out_dir / filename)

    provenance: dict[str, str] = {}
    for name, project_dir in sources.items():
        shutil.copytree(project_dir, out_dir / "projects" / name, dirs_exist_ok=True)
        provenance[name] = project_dir.parent.parent.name

    requirements = RequirementsAnalysis.model_validate_json(
        (out_dir / "requirements_analysis.json").read_text(encoding="utf-8")
    )
    coverages = {
        name: ProjectRequirementCoverage.model_validate_json(
            (out_dir / "projects" / name / "requirement_coverage.json").read_text(encoding="utf-8")
        )
        for name in sources
    }
    plans = {
        name: WorkflowPlan.model_validate_json(
            (out_dir / "projects" / name / "workflow_plan.json").read_text(encoding="utf-8")
        )
        for name in sources
    }

    (out_dir / "requirement_coverage_matrix.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "assessment_basis": "openapi_documentation",
                "projects": {n: c.model_dump(mode="json") for n, c in coverages.items()},
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    # newline="": csv.writer already emits \r\n; letting Windows translate it too
    # would yield \r\r\n and a blank row between every record.
    (out_dir / "requirement_coverage_matrix.csv").write_text(
        Orchestrator._coverage_csv(requirements, coverages), encoding="utf-8", newline=""
    )

    (out_dir / "summary.md").write_text(
        Orchestrator._summary(
            config.model_copy(update={"run_id": run_id}), out_dir, requirements, plans, coverages
        ),
        encoding="utf-8",
    )
    (out_dir / "provenance.json").write_text(
        json.dumps({"run_id": run_id, "sources": provenance}, indent=2), encoding="utf-8"
    )

    print(f"\nConsolidated {len(sources)} projects into {out_dir}")
    for name, run in provenance.items():
        print(f"  {name:26} <- {run}")
    if len(sources) < expected:
        missing = expected - len(sources)
        print(f"\nWARNING: {missing} project(s) short of the expected {expected}.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
