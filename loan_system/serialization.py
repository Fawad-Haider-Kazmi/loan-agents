"""Generic dataclass <-> JSON round-tripping (handles nested dataclasses,
Optional, list, dict and Enum), so cases can be stored in SQLite and
resumed after a restart."""
from __future__ import annotations

import dataclasses
import json
import types
import typing
from enum import Enum
from typing import Any, get_args, get_origin, get_type_hints


def to_json(obj: Any) -> str:
    return json.dumps(dataclasses.asdict(obj), default=_default)


def from_json(cls: type, text: str) -> Any:
    return from_dict(cls, json.loads(text))


def to_dict(obj: Any) -> dict:
    return json.loads(to_json(obj))


def _default(value: Any):
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"Cannot serialise {type(value)}")


def from_dict(cls: type, data: dict) -> Any:
    hints = get_type_hints(cls)
    kwargs = {f.name: _convert(hints[f.name], data[f.name])
              for f in dataclasses.fields(cls) if f.name in data}
    return cls(**kwargs)


def _convert(tp: Any, value: Any) -> Any:
    if value is None:
        return None
    origin = get_origin(tp)
    if origin is typing.Union or origin is types.UnionType:
        inner = [a for a in get_args(tp) if a is not type(None)]
        return _convert(inner[0], value)
    if origin is list:
        (item,) = get_args(tp)
        return [_convert(item, v) for v in value]
    if origin is dict:
        _, val = get_args(tp)
        return {k: _convert(val, v) for k, v in value.items()}
    if isinstance(tp, type) and issubclass(tp, Enum):
        return tp(value)
    if dataclasses.is_dataclass(tp):
        return from_dict(tp, value)
    return value
