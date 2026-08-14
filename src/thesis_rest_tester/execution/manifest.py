"""How each system under test is brought up, as data rather than as branches.

The eighteen projects are heterogeneous in ways no amount of code can normalize: some
publish their API on 3000 and some on 5000, some declare an ``env_file`` they never
shipped, some define no HTTP service at all. Encoding that as conditionals in the
executor would produce a function with eighteen special cases; encoding it as a manifest
keeps the executor uniform and makes the differences reviewable.

The manifest is also a thesis artifact in its own right. ``projects/`` is gitignored, so
this file is the only durable record of how each project was started -- and of what we
had to supply ourselves to start it. Its precedent is
``data/ground_truth/participium_implemented_stories.yaml``: hand-curated, version
controlled, passed to the CLI by path, and consulted only outside the pipeline proper.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

# "unverified" is the honest default for a project whose startup recipe has not yet been
# established. It is distinct from "unrunnable": one means we have not tried, the other
# means we tried and it cannot be done. Both are reported; neither aborts a campaign.
Status = Literal["runnable", "unverified", "unrunnable"]
Blocker = Literal[
    "no_http_service",
    "missing_env_file",
    "invalid_compose",
    "external_dependency",
    "image_unavailable",
    "other",
]
# Mirrors the spec-provenance column of docs/run-results-2026-07-21.md section 4: the
# results are only interpretable if a reader can tell which projects ran as delivered.
Provenance = Literal["original", "env_supplied", "compose_extended", "adapted"]


class ManifestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadinessProbe(ManifestModel):
    """When the service counts as ready to be tested.

    ``reject_content_type`` exists because several projects publish a frontend as well as
    an API, sometimes on the port we are probing: an nginx-served index page answers 200
    long before the API works, and accepting it would start the suite against a service
    that cannot serve it.

    It applies only to *successful* responses, though. An Express API with no route at
    ``/`` answers 404 with an HTML error page, and that page is positive evidence the
    server is listening and routing. Rejecting it would make the default probe fail on
    almost every project in this study.
    """

    method: str = "GET"
    path: str = "/"
    # Health endpoints are routinely mounted outside the API prefix -- an Express app
    # registers ``/health`` before mounting its router under ``/api``, so team13 answers
    # 200 on ``/health`` and 404 on ``/api/health``. Resolving the probe against the
    # origin instead of the base URL reaches it without giving up a real application
    # healthcheck for a weaker probe on the prefix.
    relative_to: Literal["base_url", "origin"] = "base_url"
    # 4xx proves a server is listening and routing; only 5xx and no answer mean not ready.
    accept_status: list[int] = Field(default_factory=lambda: [200, 204, 400, 401, 403, 404])
    reject_content_type: str | None = "text/html"
    # Grace after the first good answer: several stacks seed data through a sidecar that
    # finishes after the API starts responding.
    settle_seconds: float = Field(default=3.0, ge=0.0)
    poll_interval_seconds: float = Field(default=2.0, gt=0.0)


class MaterializedEnvFile(ManifestModel):
    """A file we write into the project before ``up`` and remove afterwards.

    Not interchangeable with ``interpolation_env_file``. A service-level ``env_file:`` in
    a compose file is read by the container, and ``docker compose --env-file`` does not
    satisfy it -- that flag only supplies variables for ``${...}`` substitution. The only
    way to honour it without editing a student's compose file is to put a real file where
    the compose file says one is.
    """

    source: Path
    target: str


class ComposeSpec(ManifestModel):
    files: list[Path] = Field(min_length=1)
    # Always passed as -p: several projects hardcode container_name, which escapes
    # compose's own scoping, so teardown must be able to name the project explicitly.
    project_name: str
    services: list[str] = Field(default_factory=list)
    # For ${VAR} substitution inside the compose file itself, e.g. a port with no default.
    interpolation_env_file: Path | None = None
    materialize_env_files: list[MaterializedEnvFile] = Field(default_factory=list)


class ApiSpec(ManifestModel):
    """Where the API answers.

    ``base_url`` is an origin *and a path prefix*. The prefix cannot be recovered from any
    artifact: the OpenAPI loader never read ``servers``, and the reconstructed contracts
    do not carry it, yet several projects mount their routes under ``/api``. Getting it
    wrong makes every request 404 and looks like a broken service.

    Use 127.0.0.1 rather than localhost: Windows resolves localhost to ::1 first, while
    Docker publishes on IPv4.
    """

    base_url: str
    # The service that must stay up; watched so a crash-looping container is noticed
    # immediately instead of after the whole startup timeout.
    service: str


class ProjectManifest(ManifestModel):
    status: Status = "runnable"
    provenance: Provenance = "original"
    provenance_notes: str | None = None
    blocker: Blocker | None = None
    reason: str | None = None
    root: Path | None = None
    compose: ComposeSpec | None = None
    api: ApiSpec | None = None
    readiness: ReadinessProbe | None = None
    # Bind-mounted directories holding state that `down -v` does not remove, so two runs
    # would not start from the same place. Deleting paths inside a student's repository
    # is destructive, so this is opt-in per project and gated by a flag.
    reset_paths: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_project(self) -> ProjectManifest:
        if self.status == "runnable":
            required = (("root", self.root), ("compose", self.compose), ("api", self.api))
            missing = [name for name, value in required if value is None]
            if missing:
                raise ValueError(
                    f"a runnable project needs {', '.join(missing)}; mark it unverified "
                    "while its recipe is unknown, or unrunnable with a blocker"
                )
        elif self.status == "unrunnable" and self.blocker is None:
            raise ValueError("an unrunnable project must state a blocker")

        # Anything we supplied must be visible in the provenance, or the results table
        # would present a project we completed as one that ran as delivered.
        supplied = bool(self.compose and self.compose.materialize_env_files) or bool(
            self.compose and self.compose.interpolation_env_file
        )
        if supplied and self.provenance == "original":
            raise ValueError(
                "this project is started with files we supplied, so its provenance cannot "
                "be 'original'; use env_supplied, compose_extended or adapted"
            )
        if self.provenance != "original" and not self.provenance_notes:
            raise ValueError("a non-original provenance must say what was supplied and why")
        return self


class SutManifest(ManifestModel):
    version: int = 1
    defaults: ReadinessProbe = Field(default_factory=ReadinessProbe)
    projects: dict[str, ProjectManifest] = Field(default_factory=dict)

    def readiness_for(self, project_name: str) -> ReadinessProbe:
        project = self.projects.get(project_name)
        if project is None or project.readiness is None:
            return self.defaults
        return project.readiness

    def missing_for(self, project_names: list[str]) -> list[str]:
        """Projects present in a run but absent from the manifest.

        Checked before anything starts: discovering a forgotten entry at project fourteen,
        hours into a campaign, is a failure that costs an evening and is entirely avoidable.
        """

        return sorted(name for name in project_names if name not in self.projects)


def load_manifest(path: str | Path) -> SutManifest:
    manifest_path = Path(path)
    if not manifest_path.is_file():
        raise FileNotFoundError(f"SUT manifest not found: {manifest_path}")
    payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"SUT manifest root must be a mapping: {manifest_path}")
    return SutManifest.model_validate(payload)
