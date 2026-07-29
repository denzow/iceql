"""INSERT / UPDATE / DELETE の実行。

UPDATE / DELETE は「SELECT への還元」で実装する: 各行に行番号(_rowid_)を
注入したテーブルに対して SELECT を実行して対象行と新しい値を求め、
該当行だけ差し替えて書き戻す。これにより WHERE / SET の式のセマンティクスが
SELECT と完全に一致し、式評価器を自前で持たずに済む。
"""

from __future__ import annotations

from sqlglot import exp

from iceql.catalog import Catalog
from iceql.engine import StatementResult, executor
from iceql.errors import IntegrityError, NotSupportedError, ProgrammingError
from iceql.schema import TableSchema
from iceql.storage import Row
from iceql.types import Value

ROWID = "_rowid_"


def _eval_constant(node: exp.Expression) -> Value:
    """VALUES 句のリテラル(定数畳み込みのみ)を評価する。"""
    if isinstance(node, exp.Null):
        return None
    if isinstance(node, exp.Boolean):
        return bool(node.this)
    if isinstance(node, exp.Literal):
        if node.is_string:
            return node.name
        text = node.name
        return float(text) if ("." in text or "e" in text or "E" in text) else int(text)
    if isinstance(node, exp.Neg):
        value = _eval_constant(node.this)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return -value
        raise ProgrammingError(f"cannot negate {value!r}")
    if isinstance(node, exp.Paren):
        return _eval_constant(node.this)
    raise NotSupportedError(
        f"only constant values are supported in VALUES: {node.sql(dialect='sqlite')!r}"
    )


def _check_primary_key(schema: TableSchema, rows: list[Row]) -> None:
    pk = schema.primary_key
    if not pk:
        return
    seen: set[tuple[Value, ...]] = set()
    for row in rows:
        key = tuple(row[c] for c in pk)
        if key in seen:
            raise IntegrityError(
                f"UNIQUE constraint failed: {schema.table} primary key {key!r}"
            )
        seen.add(key)


def _table_name(node: exp.Expression) -> str:
    if isinstance(node, exp.Schema):
        node = node.this
    if isinstance(node, exp.Table):
        return node.name
    raise ProgrammingError(f"cannot determine target table from {node.sql()!r}")


def _rowid_tables(
    catalog: Catalog,
    select: exp.Select,
    table: str,
    schema: TableSchema,
    rows: list[Row],
) -> tuple[dict[str, list[Row]], dict[str, dict[str, str]]]:
    """対象テーブルに _rowid_ を注入し、サブクエリが参照する他テーブルも揃える。"""
    others = executor.physical_tables(select, catalog) - {table}
    tables, annotations = executor.load_tables(catalog, others)
    tables[table] = [{ROWID: i, **row} for i, row in enumerate(rows)]
    annotation = {ROWID: "bigint"}
    annotation.update(
        {c.name: executor._SQLGLOT_TYPES[c.type] for c in schema.columns}
    )
    annotations[table] = annotation
    return tables, annotations


def run_insert(catalog: Catalog, ast: exp.Insert) -> StatementResult:
    table = _table_name(ast.this)
    schema = catalog.load_schema(table)

    if isinstance(ast.this, exp.Schema) and ast.this.expressions:
        columns = [ident.name for ident in ast.this.expressions]
        for name in columns:
            schema.column(name)  # 存在チェック
    else:
        columns = schema.column_names

    source = ast.expression
    new_values: list[list[Value]] = []
    if isinstance(source, exp.Values):
        for tup in source.expressions:
            new_values.append([_eval_constant(e) for e in tup.expressions])
    elif isinstance(source, (exp.Select, exp.Union, exp.Except, exp.Intersect)):
        result = executor.run_select(catalog, source)
        new_values.extend(list(row) for row in result.rows)
    else:
        raise NotSupportedError(f"unsupported INSERT source: {type(source).__name__}")

    rows = catalog.read_rows(table)
    for values in new_values:
        if len(values) != len(columns):
            raise ProgrammingError(
                f"INSERT has {len(values)} values for {len(columns)} columns"
            )
        rows.append(schema.validate_row(dict(zip(columns, values, strict=True))))
    _check_primary_key(schema, rows)
    catalog.write_rows(table, rows, schema)
    return StatementResult(rowcount=len(new_values))


def run_update(catalog: Catalog, ast: exp.Update) -> StatementResult:
    if ast.args.get("from"):
        raise NotSupportedError("UPDATE ... FROM is not supported")
    table = _table_name(ast.this)
    schema = catalog.load_schema(table)
    rows = catalog.read_rows(table)

    set_items: list[tuple[str, exp.Expression]] = []
    for item in ast.expressions:
        if not (isinstance(item, exp.EQ) and isinstance(item.this, exp.Column)):
            raise NotSupportedError(f"unsupported SET clause: {item.sql(dialect='sqlite')!r}")
        column = item.this.name
        schema.column(column)  # 存在チェック
        set_items.append((column, item.expression))

    # SELECT _rowid_, <e1> AS __set_0, ... FROM t WHERE c に還元して評価する
    set_projections = [
        exp.alias_(value_expr.copy(), f"__set_{i}")
        for i, (_, value_expr) in enumerate(set_items)
    ]
    select = exp.select(ROWID, *set_projections).from_(table)
    where = ast.args.get("where")
    if where:
        select = select.where(where.this.copy())
    tables, annotations = _rowid_tables(catalog, select, table, schema, rows)
    _, matched = executor.evaluate(select, tables, annotations)

    for row in matched:
        rowid = row[0]
        assert isinstance(rowid, int)
        updated = dict(rows[rowid])
        for (column, _), value in zip(set_items, row[1:], strict=True):
            updated[column] = value
        rows[rowid] = schema.validate_row(updated)
    _check_primary_key(schema, rows)
    catalog.write_rows(table, rows, schema)
    return StatementResult(rowcount=len(matched))


def run_delete(catalog: Catalog, ast: exp.Delete) -> StatementResult:
    table = _table_name(ast.this)
    schema = catalog.load_schema(table)
    rows = catalog.read_rows(table)

    select = exp.select(ROWID).from_(table)
    where = ast.args.get("where")
    if where:
        select = select.where(where.this.copy())
    tables, annotations = _rowid_tables(catalog, select, table, schema, rows)
    _, matched = executor.evaluate(select, tables, annotations)

    doomed = {row[0] for row in matched}
    remaining = [row for i, row in enumerate(rows) if i not in doomed]
    catalog.write_rows(table, remaining, schema)
    return StatementResult(rowcount=len(doomed))
