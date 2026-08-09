"""INSERT / UPDATE / DELETE の実行。

UPDATE / DELETE は「SELECT への還元」で実装する: 各行に行番号(_rowid_)を
注入したテーブルに対して SELECT を実行して対象行と新しい値を求め、
該当行だけ差し替えて書き戻す。これにより WHERE / SET の式のセマンティクスが
SELECT と完全に一致し、式評価器を自前で持たずに済む。
"""

from __future__ import annotations

from sqlglot import exp
from sqlglot.executor.table import Table

from iceql.catalog import Catalog
from iceql.engine import StatementResult, executor, node_arg
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
    indexes = [schema.column_index(c) for c in schema.primary_key]
    if not indexes:
        return
    seen: set[tuple[Value, ...]] = set()
    for row in rows:
        key = tuple(row[i] for i in indexes)
        if key in seen:
            raise IntegrityError(
                f"UNIQUE constraint failed: {schema.table} primary key {key!r}"
            )
        seen.add(key)


def _next_autoincrement(rows: list[Row], index: int) -> int:
    """自動採番の次の値。既存の最大値 + 1、行が無ければ 1。

    テーブルは既にメモリ上にあるので、最大値の算出に追加の読み込みは要らない。
    削除された値は再利用される(採番済みの値を CSV の外に覚えないため)。
    """
    used = [value for value in (row[index] for row in rows) if isinstance(value, int)]
    return max(used) + 1 if used else 1


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
) -> tuple[dict[str, Table], dict[str, dict[str, str]]]:
    """対象テーブルに _rowid_ を注入し、サブクエリが参照する他テーブルも揃える。"""
    others = executor.physical_tables(select, catalog) - {table}
    tables, annotations = executor.load_tables(catalog, others)
    tables[table] = executor.sqlglot_table(
        [ROWID, *schema.column_names], [(i, *row) for i, row in enumerate(rows)]
    )
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
            try:
                new_values.append([_eval_constant(e) for e in tup.expressions])
            except NotSupportedError:
                # 定数以外(式や CURRENT_DATE 等)は SELECT に還元して評価する
                select = exp.Select(expressions=[e.copy() for e in tup.expressions])
                _, rows_ = executor.evaluate(select, {}, {})
                new_values.append(list(rows_[0]))
    elif isinstance(source, (exp.Select, exp.Union, exp.Except, exp.Intersect)):
        result = executor.run_select(catalog, source)
        new_values.extend(list(row) for row in result.rows)
    else:
        raise NotSupportedError(f"unsupported INSERT source: {type(source).__name__}")

    rows = catalog.read_rows(table)
    auto = schema.autoincrement_column
    auto_index = schema.column_index(auto.name) if auto is not None else None
    next_id = 0 if auto_index is None else _next_autoincrement(rows, auto_index)
    last_id: int | None = None

    for values in new_values:
        if len(values) != len(columns):
            raise ProgrammingError(
                f"INSERT has {len(values)} values for {len(columns)} columns"
            )
        row = schema.arrange_row(columns, values)
        # 列指定からの省略と明示的な NULL は、どちらもここで NULL になっている
        if auto_index is not None and row[auto_index] is None:
            row[auto_index] = next_id
        validated = schema.validate_values(row)
        if auto_index is not None:
            assigned = validated[auto_index]
            assert isinstance(assigned, int)
            # 同じ文の中で明示された大きい値も、次の採番に反映する
            next_id = max(next_id, assigned + 1)
            last_id = assigned
        rows.append(validated)
    _check_primary_key(schema, rows)
    catalog.write_rows(table, rows, schema)
    return StatementResult(rowcount=len(new_values), lastrowid=last_id)


def run_update(catalog: Catalog, ast: exp.Update) -> StatementResult:
    # FROM 句のキーは sqlglot のバージョンで from / from_ のどちらかになる
    if node_arg(ast, "from", "from_"):
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
    where = node_arg(ast, "where")
    if where:
        select = select.where(where.this.copy())
    tables, annotations = _rowid_tables(catalog, select, table, schema, rows)
    _, matched = executor.evaluate(select, tables, annotations)

    set_indexes = [schema.column_index(column) for column, _ in set_items]
    for row in matched:
        rowid = row[0]
        assert isinstance(rowid, int)
        updated = list(rows[rowid])
        for index, value in zip(set_indexes, row[1:], strict=True):
            updated[index] = value
        rows[rowid] = schema.validate_values(updated)
    _check_primary_key(schema, rows)
    catalog.write_rows(table, rows, schema)
    return StatementResult(rowcount=len(matched))


def run_delete(catalog: Catalog, ast: exp.Delete) -> StatementResult:
    table = _table_name(ast.this)
    schema = catalog.load_schema(table)
    rows = catalog.read_rows(table)

    select = exp.select(ROWID).from_(table)
    where = node_arg(ast, "where")
    if where:
        select = select.where(where.this.copy())
    tables, annotations = _rowid_tables(catalog, select, table, schema, rows)
    _, matched = executor.evaluate(select, tables, annotations)

    doomed = {row[0] for row in matched}
    remaining = [row for i, row in enumerate(rows) if i not in doomed]
    catalog.write_rows(table, remaining, schema)
    return StatementResult(rowcount=len(doomed))
