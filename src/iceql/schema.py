"""テーブルスキーマ(<table>.schema.yaml)の読み書きと行検証。"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from iceql.errors import DataError, IntegrityError, OperationalError
from iceql.types import NULL_MARKER, Value, get_type

SCHEMA_VERSION = 1

IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_identifier(name: str, kind: str = "identifier") -> str:
    if not IDENTIFIER.match(name):
        raise DataError(
            f"invalid {kind}: {name!r} (must match [A-Za-z_][A-Za-z0-9_]*)"
        )
    return name


@dataclass
class Column:
    name: str
    type: str
    nullable: bool = True
    primary_key: bool = False
    default: Value = None

    def __post_init__(self) -> None:
        validate_identifier(self.name, "column name")
        get_type(self.type)
        if self.primary_key and self.nullable:
            self.nullable = False


@dataclass
class TableSchema:
    table: str
    columns: list[Column] = field(default_factory=list)

    def __post_init__(self) -> None:
        validate_identifier(self.table, "table name")
        if not self.columns:
            raise DataError(f"table {self.table!r} must have at least one column")
        seen: set[str] = set()
        for col in self.columns:
            if col.name in seen:
                raise DataError(f"duplicate column name: {col.name!r}")
            seen.add(col.name)

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    @property
    def primary_key(self) -> list[str]:
        return [c.name for c in self.columns if c.primary_key]

    def column(self, name: str) -> Column:
        for col in self.columns:
            if col.name == name:
                return col
        raise DataError(f"no such column: {self.table}.{name}")

    def column_index(self, name: str) -> int:
        for i, col in enumerate(self.columns):
            if col.name == name:
                return i
        raise DataError(f"no such column: {self.table}.{name}")

    def validate_values(self, values: Sequence[Value]) -> tuple[Value, ...]:
        """列順に並んだ値を検証し、型を正規化した行を返す。"""
        if len(values) != len(self.columns):
            raise DataError(
                f"table {self.table!r} has {len(self.columns)} columns, "
                f"got {len(values)} values"
            )
        out: list[Value] = []
        for col, value in zip(self.columns, values, strict=True):
            if value is None:
                if not col.nullable:
                    raise IntegrityError(
                        f"NOT NULL constraint failed: {self.table}.{col.name}"
                    )
                out.append(None)
                continue
            codec = get_type(col.type)
            encoded = codec.encode(value)
            assert encoded is not None
            out.append(codec.decode(encoded))
        return tuple(out)

    def build_row(
        self, columns: Sequence[str], values: Sequence[Value]
    ) -> tuple[Value, ...]:
        """列指定つきの値から、列順に並べた行を組む。指定の無い列は DEFAULT。"""
        row: list[Value] = [c.default for c in self.columns]
        assigned: set[int] = set()
        for name, value in zip(columns, values, strict=True):
            index = self.column_index(name)  # 存在しなければ DataError
            if index in assigned:
                raise DataError(f"column specified more than once: {self.table}.{name}")
            assigned.add(index)
            row[index] = value
        return self.validate_values(row)


def _column_to_yaml(col: Column) -> dict[str, Any]:
    data: dict[str, Any] = {"name": col.name, "type": col.type, "nullable": col.nullable}
    if col.primary_key:
        data["primary_key"] = True
    if col.default is not None:
        data["default"] = col.default
    return data


def dump_schema(schema: TableSchema) -> str:
    doc: dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "table": schema.table,
        "columns": [_column_to_yaml(c) for c in schema.columns],
        "null_marker": NULL_MARKER,
    }
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, default_flow_style=False)


def load_schema(text: str, *, source: str = "<schema>") -> TableSchema:
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise OperationalError(f"{source}: invalid YAML: {exc}") from exc
    if not isinstance(doc, dict):
        raise OperationalError(f"{source}: schema must be a mapping")
    version = doc.get("version")
    if version != SCHEMA_VERSION:
        raise OperationalError(f"{source}: unsupported schema version: {version!r}")
    table = doc.get("table")
    if not isinstance(table, str):
        raise OperationalError(f"{source}: 'table' must be a string")
    raw_columns = doc.get("columns")
    if not isinstance(raw_columns, list):
        raise OperationalError(f"{source}: 'columns' must be a list")
    columns: list[Column] = []
    for i, raw in enumerate(raw_columns):
        if not isinstance(raw, dict):
            raise OperationalError(f"{source}: columns[{i}] must be a mapping")
        unknown = set(raw) - {"name", "type", "nullable", "primary_key", "default"}
        if unknown:
            raise OperationalError(f"{source}: columns[{i}] has unknown keys: {sorted(unknown)}")
        try:
            columns.append(
                Column(
                    name=raw.get("name", ""),
                    type=raw.get("type", ""),
                    nullable=bool(raw.get("nullable", True)),
                    primary_key=bool(raw.get("primary_key", False)),
                    default=raw.get("default"),
                )
            )
        except DataError as exc:
            raise OperationalError(f"{source}: columns[{i}]: {exc}") from exc
    try:
        return TableSchema(table=table, columns=columns)
    except DataError as exc:
        raise OperationalError(f"{source}: {exc}") from exc


def read_schema_file(path: Path) -> TableSchema:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise OperationalError(f"cannot read schema file {path}: {exc}") from exc
    return load_schema(text, source=str(path))
