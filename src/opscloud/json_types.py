"""Shared recursive types for JSON-compatible data in OpsCloud."""

from __future__ import annotations

from pydantic import JsonValue as PydanticJsonValue, TypeAdapter

type JsonScalar = str | int | float | bool | None
type JsonValue = PydanticJsonValue
type JsonObject = dict[str, JsonValue]

JSON_VALUE_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
JSON_OBJECT_ADAPTER: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)

__all__ = [
    "JSON_OBJECT_ADAPTER",
    "JSON_VALUE_ADAPTER",
    "JsonObject",
    "JsonScalar",
    "JsonValue",
]
