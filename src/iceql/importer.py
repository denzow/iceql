"""既存 CSV の取り込み(iceql import)。

型はヘッダ以下の全行を走査して推論する。判定の規則は「読んで書き戻したときに
フィールドの表記が変わらない型だけを候補にする」で、ゼロ埋めの ID (``007``) が
integer になって桁が落ちるような取り込みを防ぐ。
"""

from __future__ import annotations

import csv
import io
import math
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from iceql.api import connect
from iceql.errors import DataError, Error, OperationalError, ProgrammingError
from iceql.schema import Column, TableSchema, validate_identifier
from iceql.storage import Row
from iceql.types import NULL_MARKER, decode_null, get_type

DEFAULT_ENCODING = "utf-8-sig"

# 推論で試す順。integer は real より前(1 を 1.0 に変えない)、
# date は datetime より前(YYYY-MM-DD は datetime としても読めるため)。
_INFER_ORDER = ("integer", "real", "boolean", "date", "datetime")

_EMPTY_FIELD_NOTE = "contains empty fields; use --null-marker '' to read them as NULL"


def _fits(type_name: str, field: str) -> bool:
    """その型で読んで書き戻したとき、フィールドの表記が変わらないか。"""
    codec = get_type(type_name)
    try:
        value = codec.decode(field)
    except Error:
        return False
    # nan / inf は float としては往復してしまうが、数値列とは考えにくい
    if isinstance(value, float) and not math.isfinite(value):
        return False
    try:
        return codec.encode(value) == field
    except Error:
        return False


def infer_column_type(fields: Sequence[str | None]) -> str:
    """NULL を除いた全フィールドから列の型を推論する。"""
    present = [f for f in fields if f is not None]
    if not present:
        return "text"
    for type_name in _INFER_ORDER:
        if all(_fits(type_name, f) for f in present):
            return type_name
    # 整数と小数が混ざった列は、どちらの型でも表記を保てないので上では候補が
    # 空になる。この場合だけ real に寄せる(整数側は 1 が 1.0 に正規化される)
    if all(_fits("integer", f) or _fits("real", f) for f in present):
        return "real"
    return "text"


def parse_type_overrides(values: Sequence[str]) -> dict[str, str]:
    """``COL=TYPE`` 形式の指定を辞書にする。"""
    overrides: dict[str, str] = {}
    for item in values:
        column, sep, type_name = item.partition("=")
        if not sep or not column:
            raise DataError(f"--types: expected COL=TYPE, got {item!r}")
        if column in overrides:
            raise DataError(f"--types: column specified more than once: {column}")
        get_type(type_name)  # 未知の型名はここで弾く
        overrides[column] = type_name
    return overrides


@dataclass
class ImportPlan:
    """取り込み内容。書き込み前にそのまま表示できる。"""

    source: str
    schema: TableSchema
    rows: list[Row]
    notes: dict[str, str]

    @property
    def table(self) -> str:
        return self.schema.table


def _read_text(source: str | Path, encoding: str) -> str:
    """CSV を文字列として読む(改行変換はしない)。"""
    if str(source) == "-":
        try:
            return sys.stdin.buffer.read().decode(encoding)
        except UnicodeDecodeError as exc:
            raise DataError(f"<stdin>: cannot decode as {encoding}: {exc}") from None
    path = Path(source)
    try:
        with path.open("r", encoding=encoding, newline="") as f:
            return f.read()
    except UnicodeDecodeError as exc:
        raise DataError(f"{path}: cannot decode as {encoding}: {exc}") from None
    except OSError as exc:
        raise OperationalError(f"cannot read {path}: {exc}") from exc


def _parse_csv(text: str, label: str) -> tuple[list[str], list[list[str]]]:
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = next(reader)
    except StopIteration:
        raise DataError(f"{label}: missing header row") from None
    except csv.Error as exc:
        raise DataError(f"{label}: invalid CSV: {exc}") from exc
    if not header:
        raise DataError(f"{label}: missing header row")
    records: list[list[str]] = []
    try:
        for lineno, record in enumerate(reader, start=2):
            if len(record) != len(header):
                raise DataError(
                    f"{label}: line {lineno}: expected {len(header)} fields, got {len(record)}"
                )
            records.append(record)
    except csv.Error as exc:
        raise DataError(f"{label}: invalid CSV: {exc}") from exc
    return header, records


def _null_decoder(null_marker: str) -> Callable[[str], str | None]:
    if null_marker == NULL_MARKER:
        # 既定のマーカーでは iceql の規約をそのまま使う(\\N は文字列 \N)
        return decode_null

    def decode(field: str) -> str | None:
        return None if field == null_marker else field

    return decode


def _default_table_name(source: str | Path) -> str:
    if str(source) == "-":
        raise DataError("--table is required when reading from stdin")
    stem = Path(source).stem
    try:
        return validate_identifier(stem, "table name")
    except DataError:
        raise DataError(
            f"cannot derive a table name from {Path(source).name!r}; use --table"
        ) from None


def build_plan(
    source: str | Path,
    *,
    table: str | None = None,
    types: dict[str, str] | None = None,
    null_marker: str = NULL_MARKER,
    encoding: str = DEFAULT_ENCODING,
) -> ImportPlan:
    """CSV を読み、型を推論して取り込み内容を組み立てる(書き込みはしない)。"""
    label = "<stdin>" if str(source) == "-" else str(source)
    header, records = _parse_csv(_read_text(source, encoding), label)
    table = _default_table_name(source) if table is None else validate_identifier(table)
    for name in header:
        try:
            validate_identifier(name, "column name")
        except DataError as exc:
            raise DataError(f"{label}: {exc}") from None
    overrides = dict(types or {})
    unknown = sorted(set(overrides) - set(header))
    if unknown:
        raise DataError(f"--types: no such column in {label}: {', '.join(unknown)}")

    decode_field = _null_decoder(null_marker)
    by_column: list[list[str | None]] = [[] for _ in header]
    for record in records:
        for fields, raw in zip(by_column, record, strict=True):
            fields.append(decode_field(raw))

    columns: list[Column] = []
    notes: dict[str, str] = {}
    for name, fields in zip(header, by_column, strict=True):
        if name in overrides:
            type_name = overrides[name]
            notes[name] = "from --types"
        else:
            type_name = infer_column_type(fields)
            if type_name == "text" and any(f == "" for f in fields):
                notes[name] = _EMPTY_FIELD_NOTE
        columns.append(
            Column(name=name, type=type_name, nullable=any(f is None for f in fields))
        )
    try:
        schema = TableSchema(table=table, columns=columns)
    except DataError as exc:
        raise DataError(f"{label}: {exc}") from None
    rows = _decode_rows(records, schema, decode_field, label)
    return ImportPlan(source=label, schema=schema, rows=rows, notes=notes)


def _decode_rows(
    records: list[list[str]],
    schema: TableSchema,
    decode_field: Callable[[str], str | None],
    label: str,
) -> list[Row]:
    codecs = [get_type(c.type) for c in schema.columns]
    rows: list[Row] = []
    for lineno, record in enumerate(records, start=2):
        values = []
        for col, codec, raw in zip(schema.columns, codecs, record, strict=True):
            try:
                values.append(codec.decode(decode_field(raw)))
            except Error as exc:
                raise DataError(
                    f"{label}: line {lineno}: column {col.name!r}: {exc}"
                ) from None
        rows.append(tuple(values))
    return rows


def format_plan(plan: ImportPlan) -> str:
    """推論結果を人が読める形にする。"""
    columns = plan.schema.columns
    head = (
        f"{plan.source}: {len(plan.rows)} rows, {len(columns)} columns "
        f"-> table {plan.table!r}"
    )
    name_width = max(len(c.name) for c in columns)
    type_width = max(len(c.type) for c in columns)
    lines = [head]
    for col in columns:
        nullable = "null" if col.nullable else "not null"
        line = f"  {col.name.ljust(name_width)}  {col.type.ljust(type_width)}  {nullable.ljust(8)}"
        note = plan.notes.get(col.name)
        if note:
            line = f"{line}  ({note})"
        lines.append(line.rstrip())
    return "\n".join(lines) + "\n"


def apply_plan(dbdir: str | Path, plan: ImportPlan) -> None:
    """取り込み内容を新しいテーブルとして書き出す。"""
    conn = connect(dbdir)
    try:
        catalog = conn._catalog
        # SQL の INSERT を経由せず、CSV の書き出しを 1 回にまとめる
        with conn._lock.write_statement():
            if catalog.has_table(plan.table):
                raise ProgrammingError(f"table already exists: {plan.table}")
            catalog.write_table(plan.schema, plan.rows)
    finally:
        conn.close()


def import_csv(
    dbdir: str | Path,
    source: str | Path,
    *,
    table: str | None = None,
    types: dict[str, str] | None = None,
    null_marker: str = NULL_MARKER,
    encoding: str = DEFAULT_ENCODING,
) -> ImportPlan:
    """CSV を DBDIR に新しいテーブルとして取り込む。"""
    plan = build_plan(
        source, table=table, types=types, null_marker=null_marker, encoding=encoding
    )
    apply_plan(dbdir, plan)
    return plan
