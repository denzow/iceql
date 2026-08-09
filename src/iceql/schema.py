"""テーブルスキーマ(<table>.schema.yaml)の読み書きと行検証。"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from sqlglot import exp

from iceql.errors import DataError, IntegrityError, NotSupportedError, OperationalError
from iceql.sql import SQL_DIALECT, parse_check_expression, validate_check_expression
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
class UniqueConstraint:
    """UNIQUE 制約。列レベルの UNIQUE も 1 列の表制約として持つ。"""

    columns: list[str]
    name: str | None = None

    def __post_init__(self) -> None:
        if not self.columns:
            raise DataError("UNIQUE constraint must name at least one column")
        if self.name is not None:
            validate_identifier(self.name, "constraint name")
        seen: set[str] = set()
        for name in self.columns:
            if name in seen:
                raise DataError(f"duplicate column in UNIQUE constraint: {name!r}")
            seen.add(name)

    @property
    def label(self) -> str:
        return self.name or ", ".join(self.columns)


@dataclass
class CheckConstraint:
    """CHECK 制約。式は SQL 文字列のまま保持する。"""

    expr: str
    name: str | None = None
    node: exp.Expression = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.name is not None:
            validate_identifier(self.name, "constraint name")
        self.node = parse_check_expression(self.expr)

    def unqualify(self) -> None:
        """列参照からテーブル名を外す。

        修飾を残すと、テーブル名を変えた時点で式が別テーブルの参照になる。
        """
        qualified = [c for c in self.node.find_all(exp.Column) if c.args.get("table")]
        if not qualified:
            return
        for column in qualified:
            column.set("table", None)
        self.expr = self.node.sql(dialect=SQL_DIALECT)

    @property
    def label(self) -> str:
        return self.name or self.expr


@dataclass
class TableSchema:
    table: str
    columns: list[Column] = field(default_factory=list)
    unique: list[UniqueConstraint] = field(default_factory=list)
    checks: list[CheckConstraint] = field(default_factory=list)

    def __post_init__(self) -> None:
        validate_identifier(self.table, "table name")
        if not self.columns:
            raise DataError(f"table {self.table!r} must have at least one column")
        seen: set[str] = set()
        for col in self.columns:
            if col.name in seen:
                raise DataError(f"duplicate column name: {col.name!r}")
            seen.add(col.name)
        for constraint in self.unique:
            for name in constraint.columns:
                if name not in seen:
                    raise DataError(f"no such column: {self.table}.{name}")
        for check in self.checks:
            validate_check_expression(check.node, table=self.table, columns=seen)
            check.unqualify()

    def renamed(self, table: str) -> TableSchema:
        """テーブル名だけを差し替えた同じ定義を返す。"""
        return TableSchema(
            table=table, columns=self.columns, unique=self.unique, checks=self.checks
        )

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    @property
    def primary_key(self) -> list[str]:
        return [c.name for c in self.columns if c.primary_key]

    @property
    def autoincrement_column(self) -> Column | None:
        """値が NULL のとき自動採番する列。単一の integer 主キーのときだけ存在する。

        sqlite で INTEGER PRIMARY KEY が rowid の別名になる条件に合わせている。
        複合主キーと integer 以外の主キーは対象外。
        """
        keys = [c for c in self.columns if c.primary_key]
        if len(keys) == 1 and keys[0].type == "integer":
            return keys[0]
        return None

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

    def arrange_row(
        self, columns: Sequence[str], values: Sequence[Value]
    ) -> list[Value]:
        """列指定つきの値を列順に並べる。指定の無い列は DEFAULT。検証はしない。

        自動採番は検証(NOT NULL)より前に値を埋める必要があるため、
        並べ替えと検証を分けている。
        """
        row: list[Value] = [c.default for c in self.columns]
        assigned: set[int] = set()
        for name, value in zip(columns, values, strict=True):
            index = self.column_index(name)  # 存在しなければ DataError
            if index in assigned:
                raise DataError(f"column specified more than once: {self.table}.{name}")
            assigned.add(index)
            row[index] = value
        return row

    def build_row(
        self, columns: Sequence[str], values: Sequence[Value]
    ) -> tuple[Value, ...]:
        """列指定つきの値から、列順に並べた行を組む。指定の無い列は DEFAULT。"""
        return self.validate_values(self.arrange_row(columns, values))


def _column_to_yaml(col: Column) -> dict[str, Any]:
    data: dict[str, Any] = {"name": col.name, "type": col.type, "nullable": col.nullable}
    if col.primary_key:
        data["primary_key"] = True
    if col.default is not None:
        data["default"] = col.default
    return data


def _constraint_to_yaml(constraint: UniqueConstraint | CheckConstraint) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if constraint.name is not None:
        data["name"] = constraint.name
    if isinstance(constraint, UniqueConstraint):
        data["columns"] = list(constraint.columns)
    else:
        data["expr"] = constraint.expr
    return data


def dump_schema(schema: TableSchema) -> str:
    doc: dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "table": schema.table,
        "columns": [_column_to_yaml(c) for c in schema.columns],
    }
    # 制約は無いテーブルのほうが多いので、空のときはキーごと省く
    if schema.unique:
        doc["unique"] = [_constraint_to_yaml(u) for u in schema.unique]
    if schema.checks:
        doc["checks"] = [_constraint_to_yaml(c) for c in schema.checks]
    doc["null_marker"] = NULL_MARKER
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, default_flow_style=False)


def _constraint_entries(doc: dict[str, Any], key: str, *, source: str) -> list[Any]:
    raw = doc.get(key)
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise OperationalError(f"{source}: {key!r} must be a list")
    return raw


def _load_unique(doc: dict[str, Any], *, source: str) -> list[UniqueConstraint]:
    out: list[UniqueConstraint] = []
    for i, raw in enumerate(_constraint_entries(doc, "unique", source=source)):
        if not isinstance(raw, dict):
            raise OperationalError(f"{source}: unique[{i}] must be a mapping")
        unknown = set(raw) - {"name", "columns"}
        if unknown:
            raise OperationalError(f"{source}: unique[{i}] has unknown keys: {sorted(unknown)}")
        columns = raw.get("columns")
        if not isinstance(columns, list) or not all(isinstance(c, str) for c in columns):
            raise OperationalError(f"{source}: unique[{i}]: 'columns' must be a list of names")
        try:
            out.append(UniqueConstraint(columns=list(columns), name=raw.get("name")))
        except DataError as exc:
            raise OperationalError(f"{source}: unique[{i}]: {exc}") from exc
    return out


def _load_checks(doc: dict[str, Any], *, source: str) -> list[CheckConstraint]:
    out: list[CheckConstraint] = []
    for i, raw in enumerate(_constraint_entries(doc, "checks", source=source)):
        if not isinstance(raw, dict):
            raise OperationalError(f"{source}: checks[{i}] must be a mapping")
        unknown = set(raw) - {"name", "expr"}
        if unknown:
            raise OperationalError(f"{source}: checks[{i}] has unknown keys: {sorted(unknown)}")
        expr = raw.get("expr")
        if not isinstance(expr, str):
            raise OperationalError(f"{source}: checks[{i}]: 'expr' must be a string")
        try:
            out.append(CheckConstraint(expr=expr, name=raw.get("name")))
        except (DataError, NotSupportedError) as exc:
            raise OperationalError(f"{source}: checks[{i}]: {exc}") from exc
    return out


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
    unique = _load_unique(doc, source=source)
    checks = _load_checks(doc, source=source)
    try:
        return TableSchema(table=table, columns=columns, unique=unique, checks=checks)
    except (DataError, NotSupportedError) as exc:
        raise OperationalError(f"{source}: {exc}") from exc


def read_schema_file(path: Path) -> TableSchema:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise OperationalError(f"cannot read schema file {path}: {exc}") from exc
    return load_schema(text, source=str(path))
