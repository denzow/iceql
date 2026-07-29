"""テーブルスキーマ(<table>.schema.yaml)の読み書きと行検証。"""

from __future__ import annotations

import re
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

    def validate_row(self, row: dict[str, Value]) -> dict[str, Value]:
        """行を検証し、列順を揃えた dict を返す。余分・不足キーはエラー。"""
        unknown = set(row) - set(self.column_names)
        if unknown:
            raise DataError(f"unknown columns for {self.table!r}: {sorted(unknown)}")
        out: dict[str, Value] = {}
        for col in self.columns:
            value = row.get(col.name, col.default)
            if value is None:
                if not col.nullable:
                    raise IntegrityError(
                        f"NOT NULL constraint failed: {self.table}.{col.name}"
                    )
                out[col.name] = None
                continue
            codec = get_type(col.type)
            encoded = codec.encode(value)
            assert encoded is not None
            out[col.name] = codec.decode(encoded)
        return out


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
