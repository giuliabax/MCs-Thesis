"""Bring one student project up, wait for it to answer, and take it down again.

Knows nothing about tests. Its whole job is to make a service exist at a URL and to
guarantee that nothing is left running afterwards, whatever happened in between.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests

from thesis_rest_tester.execution.manifest import ProjectManifest, ReadinessProbe

_logger = logging.getLogger(__name__)


class ComposeError(RuntimeError):
    """Raised when a project cannot be started, with the reason already classified."""

    def __init__(self, message: str, blocker: str = "other") -> None:
        super().__init__(message)
        self.blocker = blocker


@dataclass
class PhaseResult:
    name: str
    ok: bool
    duration_seconds: float
    detail: str | None = None


@dataclass
class DockerComposeStack:
    """One project's containers, for the duration of a ``with`` block."""

    project: ProjectManifest
    readiness: ReadinessProbe
    repository_root: Path
    build_timeout_seconds: int = 1800
    startup_timeout_seconds: int = 180
    teardown_volumes: bool = True
    reset_state: bool = False
    phases: list[PhaseResult] = field(default_factory=list)
    logs: str = ""
    _materialized: list[Path] = field(default_factory=list, init=False)

    @property
    def root(self) -> Path:
        assert self.project.root is not None
        return self.repository_root / self.project.root

    @property
    def compose_files(self) -> list[Path]:
        assert self.project.compose is not None
        return [self.root / name for name in self.project.compose.files]

    def __enter__(self) -> DockerComposeStack:
        try:
            self._materialize_env_files()
            # Before, not only after: several projects hardcode container_name, which
            # escapes compose's project scoping, so a container left by a crashed attempt
            # would collide by name and no amount of -p isolation would prevent it.
            self._compose("down", "--remove-orphans", timeout=120)
            self._reset_state()
            self._pull()
            self._up()
            self._await_ready()
        except Exception:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_: object) -> None:
        # Capture before teardown: once the containers are gone their logs are the only
        # remaining explanation of why anything failed.
        self.logs = self._capture_logs()
        started = time.perf_counter()
        arguments = ["down", "--remove-orphans"]
        if self.teardown_volumes:
            arguments.append("-v")
        result = self._compose(*arguments, timeout=300, check=False)
        self.phases.append(
            PhaseResult("down", result.returncode == 0, time.perf_counter() - started)
        )
        self._remove_materialized()

    # --- phases -------------------------------------------------------------------

    def _pull(self) -> None:
        """Fetch the images the stack needs, as a phase of its own.

        ``up`` is documented to pull what is missing, but the Compose version in use here
        does not: on a stack mixing built and pulled services it built the one service
        with a ``build:`` section, pulled none of the other three, and then failed on
        container creation with ``No such image``. Pulling explicitly removes the
        dependency on that behaviour, and it separates two failures worth telling apart --
        an image that cannot be fetched (registry down, rate limit, tag withdrawn) is not
        a project that cannot start.

        ``--ignore-buildable`` leaves the services compose is about to build alone, and
        pull failures are not fatal here: an image may be present locally already, and
        ``up`` gives the clearer error if one is genuinely missing.
        """

        started = time.perf_counter()
        services = self.project.compose.services if self.project.compose else []
        result = self._compose(
            "pull",
            "--ignore-buildable",
            *services,
            timeout=self.build_timeout_seconds,
            check=False,
        )
        self.phases.append(
            PhaseResult(
                "pull",
                result.returncode == 0,
                time.perf_counter() - started,
                result.stderr[-2000:],
            )
        )

    def _up(self) -> None:
        started = time.perf_counter()
        services = self.project.compose.services if self.project.compose else []
        # Deliberately no --wait: it returns non-zero when any container exits, which is
        # exactly what a one-shot seeding sidecar does on success, and on a stack without
        # an application healthcheck it only waits for "running", which proves nothing.
        result = self._compose(
            "up", "-d", *services, timeout=self.build_timeout_seconds, check=False
        )
        duration = time.perf_counter() - started
        self.phases.append(
            PhaseResult("up", result.returncode == 0, duration, result.stderr[-2000:])
        )
        if result.returncode != 0:
            raise ComposeError(
                f"docker compose up failed: {_last_line(result.stderr)}",
                blocker=_classify_up_failure(result.stderr),
            )

    def _await_ready(self) -> None:
        started = time.perf_counter()
        assert self.project.api is not None
        url = _probe_url(self.project.api.base_url, self.readiness)
        deadline = time.monotonic() + self.startup_timeout_seconds
        last: str = "no attempt made"

        while time.monotonic() < deadline:
            crashed = self._service_has_stopped()
            if crashed is not None:
                duration = time.perf_counter() - started
                self.phases.append(PhaseResult("ready", False, duration, crashed))
                raise ComposeError(f"the API service stopped: {crashed}", blocker="other")
            ready, last = self._probe(url)
            if ready:
                time.sleep(self.readiness.settle_seconds)
                self.phases.append(
                    PhaseResult("ready", True, time.perf_counter() - started, last)
                )
                return
            time.sleep(self.readiness.poll_interval_seconds)

        duration = time.perf_counter() - started
        self.phases.append(PhaseResult("ready", False, duration, last))
        raise ComposeError(
            f"the service did not answer {url} within {self.startup_timeout_seconds}s "
            f"(last: {last})",
            blocker="other",
        )

    def _probe(self, url: str) -> tuple[bool, str]:
        try:
            response = requests.request(
                self.readiness.method, url, timeout=10, allow_redirects=False
            )
        except requests.RequestException as exc:
            return False, type(exc).__name__
        if response.status_code not in self.readiness.accept_status:
            return False, f"HTTP {response.status_code}"
        rejected = self.readiness.reject_content_type
        content_type = response.headers.get("Content-Type", "")
        # Only a *successful* HTML response betrays a frontend on the API's port. An HTML
        # 404 is an Express error page, which proves the server is listening and routing;
        # treating it as not-ready would make the default probe fail on nearly every
        # project here.
        if rejected and rejected in content_type and response.status_code < 400:
            return False, f"served {content_type}, which is a page and not the API"
        return True, f"HTTP {response.status_code}"

    def _service_has_stopped(self) -> str | None:
        """Detect a container that exited or is crash-looping, rather than waiting it out."""

        assert self.project.api is not None
        result = self._compose(
            "ps", "--format", "json", timeout=60, check=False
        )
        if result.returncode != 0:
            return None
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            entries = entry if isinstance(entry, list) else [entry]
            for item in entries:
                if item.get("Service") != self.project.api.service:
                    continue
                state = str(item.get("State", "")).lower()
                if state in {"exited", "dead"}:
                    return f"state={state} exit={item.get('ExitCode')}"
        return None

    def _capture_logs(self) -> str:
        result = self._compose(
            "logs", "--no-color", "--tail", "2000", timeout=120, check=False
        )
        return (result.stdout or "") + (result.stderr or "")

    def _reset_state(self) -> None:
        """Delete the state that ``down -v`` leaves behind.

        ``-v`` removes named and anonymous volumes but never touches bind mounts, and bind
        mounts are where the persistent state of these projects actually lives: team01
        mounts ``./db_data`` as its Postgres data directory, team02 keeps a SQLite file in
        ``./database``. Without this, the second execution of a project starts from the
        first execution's data and the two are not comparable -- and a seeding script
        mounted into ``docker-entrypoint-initdb.d`` never runs again, because Postgres only
        seeds an empty directory.

        Off unless explicitly requested: these paths are inside a student's repository, so
        deleting them must be a deliberate act rather than a side effect of running tests.
        """

        if not self.reset_state or not self.project.reset_paths:
            return
        for relative in self.project.reset_paths:
            target = (self.root / relative).resolve()
            # Refuse anything that escapes the project, whatever the manifest says.
            if not target.is_relative_to(self.root.resolve()) or target == self.root.resolve():
                raise ComposeError(
                    f"reset_paths entry {relative!r} does not stay inside {self.root}"
                )
            if not target.exists():
                continue
            _logger.info("Resetting %s", target)
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink(missing_ok=True)

    # --- env files ----------------------------------------------------------------

    def _materialize_env_files(self) -> None:
        """Place the .env files a project declares but never shipped.

        A service-level ``env_file:`` is read by the container, so ``--env-file`` does not
        satisfy it; only a real file where the compose file expects one does. Written
        before ``up`` and removed afterwards, so the student repository is left as found.
        """

        if self.project.compose is None:
            return
        for entry in self.project.compose.materialize_env_files:
            source = self.repository_root / entry.source
            if not source.is_file():
                raise ComposeError(
                    f"the manifest supplies {entry.target} from {source}, which is missing",
                    blocker="missing_env_file",
                )
            destination = self.root / entry.target
            if destination.exists():
                _logger.info("%s already exists; leaving it alone", destination)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            self._materialized.append(destination)

    def _remove_materialized(self) -> None:
        for path in self._materialized:
            path.unlink(missing_ok=True)
        self._materialized.clear()

    # --- process ------------------------------------------------------------------

    def _compose(
        self,
        *arguments: str,
        timeout: int,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        assert self.project.compose is not None
        command = ["docker", "compose", "-p", self.project.compose.project_name]
        for file in self.compose_files:
            command += ["-f", str(file)]
        if self.project.compose.interpolation_env_file is not None:
            command += [
                "--env-file",
                str(self.repository_root / self.project.compose.interpolation_env_file),
            ]
        command += list(arguments)

        try:
            result = subprocess.run(
                command,
                cwd=self.root,
                capture_output=True,
                text=True,
                # Docker emits UTF-8 progress glyphs; the Windows ANSI codepage raises on
                # the first one and would abort a build that is working fine.
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ComposeError(
                f"`docker compose {' '.join(arguments)}` exceeded {timeout}s", blocker="other"
            ) from exc
        if check and result.returncode != 0:
            raise ComposeError(
                f"`docker compose {' '.join(arguments)}` failed: {_last_line(result.stderr)}"
            )
        return result


def _probe_url(base_url: str, readiness: ReadinessProbe) -> str:
    """Where to send the readiness probe.

    The default is the base URL, which is the stronger signal: it proves the API prefix
    itself routes, not merely that something is listening on the port. A probe declared
    ``relative_to: origin`` drops the prefix, for the projects whose health endpoint sits
    outside it.
    """

    if readiness.relative_to == "origin":
        parts = urlsplit(base_url)
        return urlunsplit((parts.scheme, parts.netloc, readiness.path, "", ""))
    return base_url.rstrip("/") + readiness.path


def docker_is_available() -> tuple[bool, str]:
    """Check the daemon once, before a campaign, rather than eighteen times after."""

    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"docker is not usable: {exc}"
    if result.returncode != 0:
        return False, f"the Docker daemon is not responding: {_last_line(result.stderr)}"
    return True, f"Docker server {result.stdout.strip()}"


def _classify_up_failure(stderr: str) -> str:
    lowered = (stderr or "").lower()
    if "manifest unknown" in lowered or "pull access denied" in lowered:
        return "image_unavailable"
    if "env file" in lowered and "not found" in lowered:
        return "missing_env_file"
    if "invalid" in lowered and "port" in lowered:
        return "invalid_compose"
    return "other"


def _last_line(text: str | None) -> str:
    lines = [line for line in (text or "").splitlines() if line.strip()]
    return lines[-1][:300] if lines else "no output"


def repository_root() -> Path:
    return Path(__file__).resolve().parents[3]

