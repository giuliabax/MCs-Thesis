"""Record every HTTP exchange a generated suite makes.

A test verdict says whether a test passed. It does not say which endpoint was called or
what the service answered, and those are the only facts from which operation coverage,
status-code coverage and server-error counts can be computed. JUnit XML does not carry
them, so without this plugin those metrics would require a second full execution campaign
-- eighteen container stacks rebuilt -- to recover information that was present and
discarded the first time.

The plugin wraps ``requests.Session.request``, which is the single choke point: the
generated suites call ``session.request(...)``, and the module-level ``requests.get`` and
friends construct a Session and call the same method. It is enabled by the runner with
``-p thesis_rest_tester.execution.http_record_plugin`` and writes one JSON object per
line to the path in ``THESIS_HTTP_LOG``; with that variable unset the plugin loads and
does nothing, so a suite stays runnable by hand.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
import requests

_LOG_VARIABLE = "THESIS_HTTP_LOG"
_BASE_URL_VARIABLE = "SUT_BASE_URL"


class HttpRecorder:
    """Append one record per request, keeping the path relative to the SUT root."""

    def __init__(self, log_path: Path, base_url: str) -> None:
        self._log_path = log_path
        self._base_url = base_url.rstrip("/")
        self._current_test: str | None = None
        self._original = requests.Session.request
        log_path.parent.mkdir(parents=True, exist_ok=True)

    def install(self) -> None:
        recorder = self

        def request(self: requests.Session, method: str, url: str, **kwargs: Any) -> Any:
            started = time.perf_counter()
            try:
                response = recorder._original(self, method, url, **kwargs)
            except Exception as exc:  # noqa: BLE001 - a failed call is still an exchange
                recorder._write(method, url, None, started, error=repr(exc)[:200])
                raise
            recorder._write(method, url, response.status_code, started)
            return response

        requests.Session.request = request  # type: ignore[method-assign]

    def uninstall(self) -> None:
        requests.Session.request = self._original  # type: ignore[method-assign]

    def set_current_test(self, name: str | None) -> None:
        self._current_test = name

    def _write(
        self,
        method: str,
        url: str,
        status_code: int | None,
        started: float,
        error: str | None = None,
    ) -> None:
        record = {
            "test_name": self._current_test,
            "method": str(method).upper(),
            "path": self._relative_path(url),
            "url": url,
            "status_code": status_code,
            "duration_seconds": round(time.perf_counter() - started, 4),
        }
        if error is not None:
            record["error"] = error
        with self._log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _relative_path(self, url: str) -> str:
        """Strip the SUT root so a path joins onto an OpenAPI operation."""

        if self._base_url and url.startswith(self._base_url):
            remainder = url[len(self._base_url) :]
            return remainder.split("?", 1)[0] or "/"
        return url.split("?", 1)[0]


def pytest_configure(config: pytest.Config) -> None:
    destination = os.environ.get(_LOG_VARIABLE)
    if not destination:
        return
    recorder = HttpRecorder(Path(destination), os.environ.get(_BASE_URL_VARIABLE, ""))
    recorder.install()
    config.stash_recorder = recorder  # type: ignore[attr-defined]


def pytest_unconfigure(config: pytest.Config) -> None:
    recorder = getattr(config, "stash_recorder", None)
    if recorder is not None:
        recorder.uninstall()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None):
    """Attribute each exchange to the test that made it."""

    recorder = getattr(item.config, "stash_recorder", None)
    if recorder is not None:
        recorder.set_current_test(item.name)
    yield
    if recorder is not None:
        recorder.set_current_test(None)
