"""
Fork: JSON stored as text (docs/ai/PHASE2.md §13), generalizing `mealie.db.models.ai_mcp.StringList`.

Not `sa.JSON`: backups restore a dict-valued JSON column wrongly and crash on a list of plain values
(`AlchemyExporter.convert_types`), while a text column round-trips untouched.
"""

import json
from typing import Any

import sqlalchemy as sa
from pydantic_core import to_jsonable_python
from sqlalchemy.types import TypeDecorator


class JsonText(TypeDecorator):
    """
    Any JSON value, stored as text. Pydantic models are stored as `model_dump(mode="json")` would give them (field
    names, not aliases), and UUIDs, datetimes and enums as their JSON forms, at any depth. Values read back are plain
    JSON (dicts, lists, strings, numbers), for the caller to validate into its schema.
    """

    impl = sa.Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: sa.Dialect) -> str | None:
        if value is None:
            return None
        jsonable = to_jsonable_python(value, by_alias=False, inf_nan_mode="null")
        return json.dumps(jsonable, ensure_ascii=False, separators=(",", ":"))

    def process_result_value(self, value: Any, dialect: sa.Dialect) -> Any:
        if value is None:
            return None
        return json.loads(value)
