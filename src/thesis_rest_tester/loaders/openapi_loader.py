"""Load and normalize OpenAPI 3 or Swagger 2 specifications."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from thesis_rest_tester.domain.models import OpenAPIOperation
from thesis_rest_tester.domain.schemas import LoadedOpenAPI

_HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}


class OpenAPILoader:
    def load(self, path: str | Path) -> LoadedOpenAPI:
        spec_path = Path(path)
        if not spec_path.is_file():
            raise FileNotFoundError(f"OpenAPI/Swagger file not found: {spec_path}")

        try:
            text = spec_path.read_text(encoding="utf-8")
            raw = json.loads(text) if spec_path.suffix.lower() == ".json" else yaml.safe_load(text)
        except (OSError, json.JSONDecodeError, yaml.YAMLError) as exc:
            raise ValueError(f"Could not parse OpenAPI/Swagger file {spec_path}: {exc}") from exc

        if not isinstance(raw, dict):
            raise ValueError(f"OpenAPI/Swagger root must be a mapping: {spec_path}")
        paths = raw.get("paths")
        if not isinstance(paths, dict):
            raise ValueError(f"OpenAPI/Swagger document has no valid 'paths' mapping: {spec_path}")

        operations: list[OpenAPIOperation] = []
        for endpoint, path_item in paths.items():
            if not isinstance(path_item, dict):
                continue
            path_parameters = self._parameter_list(path_item.get("parameters"))
            for method, operation in path_item.items():
                if (
                    not isinstance(method, str)
                    or method.lower() not in _HTTP_METHODS
                    or not isinstance(operation, dict)
                ):
                    continue
                operation_parameters = self._parameter_list(operation.get("parameters"))
                parameters = self._merge_parameters(path_parameters, operation_parameters)
                responses = operation.get("responses")
                response_codes = list(responses) if isinstance(responses, dict) else []
                operations.append(
                    OpenAPIOperation(
                        operation_id=operation.get("operationId"),
                        method=method,
                        path=str(endpoint),
                        summary=operation.get("summary"),
                        description=operation.get("description"),
                        tags=[str(tag) for tag in operation.get("tags", [])],
                        parameters=parameters,
                        request_body_schema=self._request_schema(operation, parameters, raw),
                        response_codes=[str(code) for code in response_codes],
                        auth_required=self._auth_required(operation, raw),
                    )
                )
        return LoadedOpenAPI(raw_document=raw, operations=operations)

    @staticmethod
    def _parameter_list(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        return [parameter for parameter in value if isinstance(parameter, dict)]

    @staticmethod
    def _merge_parameters(
        path_parameters: list[dict[str, Any]],
        operation_parameters: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        merged: dict[tuple[Any, Any], dict[str, Any]] = {}
        anonymous_index = 0
        for parameter in [*path_parameters, *operation_parameters]:
            if "$ref" in parameter:
                key = ("$ref", parameter["$ref"])
            elif parameter.get("name") is not None:
                key = (parameter.get("in"), parameter.get("name"))
            else:
                anonymous_index += 1
                key = ("anonymous", anonymous_index)
            merged[key] = parameter
        return list(merged.values())

    @staticmethod
    def _request_schema(
        operation: dict[str, Any],
        parameters: list[dict[str, Any]],
        document: dict[str, Any],
    ) -> dict[str, Any] | None:
        """The body schema for an operation, with its references followed.

        Most specifications in this corpus describe a body by pointing at a named
        schema rather than by writing it out, and returning the pointer meant nothing
        downstream could read the fields: the compaction step made no claim about the
        body, the Test Writer received no field list and invented plausible names, and
        the service rejected them with a 400 that said nothing about the behaviour under
        test. Resolving here rather than at each use keeps the rest of the pipeline
        working on plain schemas.
        """

        request_body = operation.get("requestBody")
        if isinstance(request_body, dict):
            # A requestBody may itself be a reference into components/requestBodies,
            # in which case the media types live on the target, not here.
            request_body = _resolve(request_body, document) or request_body
            content = request_body.get("content")
            if isinstance(content, dict) and content:
                media = content.get("application/json") or next(iter(content.values()))
                if isinstance(media, dict) and isinstance(media.get("schema"), dict):
                    return _resolve_schema(media["schema"], document)

        for parameter in parameters:
            if parameter.get("in") == "body" and isinstance(parameter.get("schema"), dict):
                return _resolve_schema(parameter["schema"], document)
        return None

    @staticmethod
    def _auth_required(operation: dict[str, Any], raw: dict[str, Any]) -> bool | None:
        if "security" in operation:
            security = operation["security"]
        elif "security" in raw:
            security = raw["security"]
        else:
            return None
        return bool(security)


# How deep a chain of references is followed before giving up. Bodies in this corpus
# nest a level or two at most, and a bound keeps a pathological document from costing
# the whole run.
_MAX_REF_DEPTH = 4


def _resolve(node: dict[str, Any], document: dict[str, Any]) -> dict[str, Any] | None:
    """Follow one local ``$ref``, or return None when there is nothing to follow."""

    pointer = node.get("$ref")
    if not isinstance(pointer, str) or not pointer.startswith("#/"):
        # An external reference ("common.yaml#/Foo") would mean reading another file;
        # none of the specifications in this corpus use one.
        return None
    target: Any = document
    for token in pointer[2:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if not isinstance(target, dict) or token not in target:
            return None
        target = target[token]
    return target if isinstance(target, dict) else None


def _resolve_schema(
    schema: dict[str, Any],
    document: dict[str, Any],
    *,
    depth: int = 0,
    seen: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Return a schema whose fields can be read without chasing pointers.

    A reference that cannot be followed is returned untouched rather than dropped: a
    dangling pointer is a fact about the specification, and leaving it visible lets the
    compaction step tell "this body is undescribed" apart from "this body takes no
    fields" -- a distinction the Test Writer's prompt depends on.
    """

    if depth >= _MAX_REF_DEPTH:
        return schema

    pointer = schema.get("$ref")
    if isinstance(pointer, str):
        if pointer in seen:
            # Self-referential schemas are legal and do occur; stop rather than recurse.
            return schema
        target = _resolve(schema, document)
        if target is None:
            return schema
        # Sibling keys alongside a $ref are an override in OpenAPI 3.1 and ignored in
        # 3.0; keeping them costs nothing and matches the stricter reading.
        overrides = {key: value for key, value in schema.items() if key != "$ref"}
        return _resolve_schema(
            {**target, **overrides}, document, depth=depth + 1, seen=seen | {pointer}
        )

    resolved = dict(schema)

    # allOf is how these specifications express "the create request, plus an id": the
    # fields live in the branches, so a body that only ever appears as an allOf would
    # otherwise arrive with no properties at all.
    branches = resolved.pop("allOf", None)
    if isinstance(branches, list):
        properties: dict[str, Any] = {}
        required: list[Any] = []
        inherited: dict[str, Any] = {}
        for branch in branches:
            if not isinstance(branch, dict):
                continue
            flattened = _resolve_schema(branch, document, depth=depth + 1, seen=seen)
            properties.update(flattened.get("properties") or {})
            required.extend(flattened.get("required") or [])
            for key, value in flattened.items():
                inherited.setdefault(key, value)
        # What the schema declares alongside its allOf wins over what it composes, and
        # comes last so the field order follows the document rather than the merge.
        properties.update(resolved.get("properties") or {})
        required.extend(resolved.get("required") or [])
        if properties:
            resolved["properties"] = properties
        if required:
            resolved["required"] = list(dict.fromkeys(required))
        for key, value in inherited.items():
            if key not in {"properties", "required"}:
                resolved.setdefault(key, value)

    properties = resolved.get("properties")
    if isinstance(properties, dict):
        # One level deeper, so a field's declared type survives: a property that is
        # itself a reference would otherwise be typed "unknown", and the writer is
        # checked against those types.
        resolved["properties"] = {
            name: _resolve_schema(value, document, depth=depth + 1, seen=seen)
            if isinstance(value, dict)
            else value
            for name, value in properties.items()
        }

    items = resolved.get("items")
    if isinstance(items, dict):
        resolved["items"] = _resolve_schema(items, document, depth=depth + 1, seen=seen)
    return resolved
