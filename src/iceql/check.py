"""DB ディレクトリの整合性検証(iceql check)。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlglot import exp

from iceql.catalog import Catalog
from iceql.engine import executor
from iceql.engine.dml import ROWID
from iceql.errors import Error
from iceql.schema import TableSchema
from iceql.storage import Row, encode_rows, read_rows
from iceql.tablecache import sqlglot_table


@dataclass
class Issue:
    severity: str  # "error" | "warning"
    table: str
    message: str

    def __str__(self) -> str:
        return f"{self.severity}: {self.table}: {self.message}"


def _check_primary_key(schema: TableSchema, rows: list[Row]) -> list[Issue]:
    indexes = [schema.column_index(c) for c in schema.primary_key]
    if not indexes:
        return []
    issues = []
    seen = set()
    for i, row in enumerate(rows, start=2):
        key = tuple(row[j] for j in indexes)
        if key in seen:
            issues.append(
                Issue("error", schema.table, f"line {i}: duplicate primary key {key!r}")
            )
        seen.add(key)
    return issues


def _check_unique(schema: TableSchema, rows: list[Row]) -> list[Issue]:
    """UNIQUE の重複を報告する。NULL を含むキーは対象外(DML 側と同じ)。"""
    issues = []
    for constraint in schema.unique:
        indexes = [schema.column_index(name) for name in constraint.columns]
        seen = set()
        for i, row in enumerate(rows, start=2):
            key = tuple(row[j] for j in indexes)
            if any(value is None for value in key):
                continue
            if key in seen:
                target = ", ".join(constraint.columns)
                issues.append(
                    Issue(
                        "error",
                        schema.table,
                        f"line {i}: duplicate value for UNIQUE ({target}): {key!r}",
                    )
                )
            seen.add(key)
    return issues


def _check_expressions(schema: TableSchema, rows: list[Row]) -> list[Issue]:
    """CHECK 違反を報告する。評価は DML と同じく SELECT への還元で行う。"""
    if not schema.checks or not rows:
        return []
    tables = {
        schema.table: sqlglot_table(
            [ROWID, *schema.column_names], [(i, *row) for i, row in enumerate(rows)]
        )
    }
    annotation = {ROWID: "bigint"}
    annotation.update({c.name: executor._SQLGLOT_TYPES[c.type] for c in schema.columns})
    issues = []
    for check in schema.checks:
        select = (
            exp.select(ROWID).from_(schema.table).where(exp.not_(check.node.copy()))
        )
        _, failed = executor.evaluate(select, tables, {schema.table: annotation})
        for rowid in sorted(row[0] for row in failed if isinstance(row[0], int)):
            issues.append(
                Issue(
                    "error",
                    schema.table,
                    f"line {rowid + 2}: CHECK constraint failed: {check.label}",
                )
            )
    return issues


def check_database(dbdir: str | Path) -> list[Issue]:
    issues: list[Issue] = []
    root = Path(dbdir)
    if not root.is_dir():
        return [Issue("error", "-", f"database directory does not exist: {root}")]
    catalog = Catalog(root)

    if (root / ".iceql" / "journal").is_file():
        issues.append(
            Issue(
                "warning",
                "-",
                "unapplied commit journal found (a COMMIT was interrupted); "
                "it will be re-applied on the next connection",
            )
        )

    tables = catalog.list_tables()
    for table in tables:
        try:
            schema = catalog.load_schema(table)
        except Error as exc:
            issues.append(Issue("error", table, f"invalid schema: {exc}"))
            continue
        csv_path = catalog.csv_path(table)
        if not csv_path.is_file():
            issues.append(Issue("error", table, f"missing CSV file: {csv_path.name}"))
            continue
        with csv_path.open("r", encoding="utf-8", newline="") as f:
            raw = f.read()  # 改行変換せず生のまま(CRLF 検出のため)
        try:
            rows = read_rows(csv_path, schema)
        except Error as exc:
            issues.append(Issue("error", table, str(exc)))
            continue
        # NOT NULL は read_rows では検査されない(型 decode は NULL を素通しする)
        for i, row in enumerate(rows, start=2):
            for j, col in enumerate(schema.columns):
                if not col.nullable and row[j] is None:
                    issues.append(
                        Issue(
                            "error",
                            table,
                            f"line {i}: NOT NULL constraint failed: {col.name}",
                        )
                    )
        issues.extend(_check_primary_key(schema, rows))
        issues.extend(_check_unique(schema, rows))
        issues.extend(_check_expressions(schema, rows))
        canonical = encode_rows(rows, schema)
        if raw != canonical:
            issues.append(
                Issue(
                    "warning",
                    table,
                    "CSV is not in canonical form (CRLF, quoting or float format "
                    "differs); it will be rewritten on the next write",
                )
            )

    # スキーマの無い孤立 CSV
    for path in sorted(root.glob("*.csv")):
        name = path.stem
        if name and not name.startswith(".") and name not in tables:
            issues.append(
                Issue("warning", name, f"CSV file without schema: {path.name}")
            )
    return issues
