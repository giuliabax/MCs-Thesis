"""The seeded-fault catalogue, and how a fault rewrites one response.

Kept apart from the proxy that applies it so the interesting part -- deciding whether a
fault applies to a response, and what it turns that response into -- can be tested as a
pure function, with no sockets involved.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

MutationType = Literal["force_status", "swap_status", "empty_collection", "drop_response_field"]


class FaultModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FaultMatch(FaultModel):
    """Which responses a fault applies to.

    Matching on the *response* status rather than only on the request is what keeps the
    catalogue honest. ``accept-invalid-create`` must corrupt the service's rejection of a
    malformed body; if it fired on every POST it would also break the creations that were
    supposed to succeed, and the resulting failures would say nothing about the fault.
    """

    method: str | None = None
    # `{}` matches exactly one path segment, so `/reports/{}` matches `/reports/7`.
    path: str | None = None
    path_contains: str | None = None
    status_in: list[int] = Field(default_factory=list)

    def applies(self, method: str, path: str, status: int) -> bool:
        if self.method and self.method.upper() != method.upper():
            return False
        if self.status_in and status not in self.status_in:
            return False
        if self.path_contains and self.path_contains not in path:
            return False
        if self.path and not _path_pattern(self.path).fullmatch(path.split("?", 1)[0]):
            return False
        return True


class FaultMutation(FaultModel):
    type: MutationType
    status: int | None = None
    fields: list[str] = Field(default_factory=list)


class SeededFault(FaultModel):
    id: str
    description: str = ""
    match: FaultMatch = Field(default_factory=FaultMatch)
    mutation: FaultMutation

    def apply(self, status: int, body: bytes) -> tuple[int, bytes]:
        """Return the corrupted response. The caller has already checked ``applies``."""

        mutation = self.mutation
        if mutation.type in {"force_status", "swap_status"}:
            return (mutation.status or status), body
        payload = _decode(body)
        if payload is None:
            # Not JSON, so there is nothing to corrupt in it. The status is left alone
            # too: a fault that silently did nothing would be counted as undetected, and
            # the runner needs to see that it did not apply.
            return status, body
        if mutation.type == "empty_collection":
            return status, _encode(_emptied(payload))
        return status, _encode(_without(payload, set(mutation.fields)))


class FaultCatalogue(FaultModel):
    version: int = 1
    faults: list[SeededFault] = Field(default_factory=list)

    def by_id(self, fault_id: str) -> SeededFault | None:
        return next((fault for fault in self.faults if fault.id == fault_id), None)


def load_faults(path: str | Path) -> FaultCatalogue:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return FaultCatalogue.model_validate(data)


def _path_pattern(pattern: str) -> re.Pattern[str]:
    escaped = re.escape(pattern).replace(r"\{\}", "[^/]+")
    return re.compile(escaped)


def _decode(body: bytes) -> Any | None:
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _encode(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _emptied(payload: Any) -> Any:
    """Empty whatever collection the payload actually is.

    Services in this corpus return a bare array, or wrap it under one of several names.
    Emptying only a top-level array would leave most projects untouched, and a fault that
    quietly does nothing looks exactly like a fault nobody detected.
    """

    if isinstance(payload, list):
        return []
    if isinstance(payload, dict):
        result = dict(payload)
        for key, value in payload.items():
            if isinstance(value, list):
                result[key] = []
        return result
    return payload


def _without(payload: Any, fields: set[str]) -> Any:
    if isinstance(payload, list):
        return [_without(item, fields) for item in payload]
    if isinstance(payload, dict):
        return {
            key: _without(value, fields)
            for key, value in payload.items()
            if key not in fields
        }
    return payload
