"""INSERT / UPDATE / DELETE の実行。

UPDATE / DELETE は「SELECT への還元」で実装する: 各行に行番号(_rowid_)を
注入したテーブルに対して SELECT を実行して対象行と新しい値を求め、
該当行だけ差し替えて書き戻す。これにより WHERE / SET の式のセマンティクスが
SELECT と完全に一致し、式評価器を自前で持たずに済む。

INSERT の競合解決(OR IGNORE / OR REPLACE / ON CONFLICT)も同じ考え方で、
DO UPDATE の SET 式は対象行と excluded 行を 1 行ずつのテーブルにして
SELECT で評価する。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from itertools import islice
from operator import itemgetter

from sqlglot import exp
from sqlglot.executor.table import Table

from iceql.catalog import Catalog
from iceql.engine import StatementResult, executor, node_arg
from iceql.errors import IntegrityError, NotSupportedError, ProgrammingError
from iceql.schema import TableSchema
from iceql.storage import Row
from iceql.tablecache import sqlglot_table
from iceql.types import Value

ROWID = "_rowid_"

# ON CONFLICT DO UPDATE の中から、挿入しようとした行を指す名前(sqlite と同じ)
EXCLUDED = "excluded"

# 単一文では既定(競合したらエラー)と区別が付かない競合解決
_ABORTING_ALTERNATIVES = frozenset({"ABORT", "FAIL"})


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


def _check_primary_key(schema: TableSchema, rows: list[Row], checked: int = 0) -> None:
    """主キーの重複を検査する。

    先頭 ``checked`` 行は検査済みとして扱い、キー集合を作るためだけに走査する。
    INSERT のように末尾へ足すだけの操作では、重複判定が新しい行だけで済む。
    """
    indexes = [schema.column_index(c) for c in schema.primary_key]
    if not indexes:
        return
    # 単一列なら値そのものを鍵にする(行ごとの tuple 生成を避ける)
    key_of = itemgetter(*indexes)
    seen = set(map(key_of, islice(rows, checked)))
    for row in islice(rows, checked, None):
        key = key_of(row)
        if key in seen:
            raise IntegrityError(
                f"UNIQUE constraint failed: {schema.table} primary key {key!r}"
            )
        seen.add(key)


def check_unique(schema: TableSchema, rows: list[Row], checked: int = 0) -> None:
    """UNIQUE 制約の重複を検査する。

    キーに NULL を含む行は対象外にする。sqlite と同じで、NULL どうしは
    重複とみなさない。``checked`` の扱いは _check_primary_key と同じで、
    先頭のその行数はキー集合を作るためだけに走査する。
    """
    for constraint in schema.unique:
        indexes = [schema.column_index(name) for name in constraint.columns]
        seen: set[tuple[Value, ...]] = set()
        for position, row in enumerate(rows):
            key = tuple(row[i] for i in indexes)
            if any(value is None for value in key):
                continue
            if position >= checked and key in seen:
                target = ", ".join(f"{schema.table}.{c}" for c in constraint.columns)
                raise IntegrityError(f"UNIQUE constraint failed: {target}")
            seen.add(key)


def check_expressions(
    schema: TableSchema, rows: list[Row], changed: Sequence[int] | None = None
) -> None:
    """CHECK 制約を検査する。``changed`` を渡すとその行だけを対象にする。

    式は WHERE と同じ経路(SELECT への還元)で評価する。CHECK は式が FALSE の
    ときだけ違反なので、``WHERE NOT (式)`` が返した行がそのまま違反行になる
    (NULL の行は WHERE を通らない)。
    """
    if not schema.checks:
        return
    target_rows = rows if changed is None else [rows[i] for i in changed]
    if not target_rows:
        return
    tables = {schema.table: sqlglot_table(schema.column_names, target_rows)}
    annotations = {
        schema.table: {c.name: executor._SQLGLOT_TYPES[c.type] for c in schema.columns}
    }
    for check in schema.checks:
        select = (
            exp.select(exp.Literal.number(1))
            .from_(schema.table)
            .where(exp.not_(check.node.copy()))
        )
        _, failed = executor.evaluate(select, tables, annotations)
        if failed:
            raise IntegrityError(f"CHECK constraint failed: {check.label}")


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
    tables[table] = sqlglot_table(
        [ROWID, *schema.column_names], [(i, *row) for i, row in enumerate(rows)]
    )
    annotation = {ROWID: "bigint"}
    annotation.update(
        {c.name: executor._SQLGLOT_TYPES[c.type] for c in schema.columns}
    )
    annotations[table] = annotation
    return tables, annotations


@dataclass
class _KeyIndex:
    """一意キー(主キーまたは UNIQUE 制約)の値から行の位置を引く索引。

    ``resolve`` が真なら、このキーの競合は競合解決の対象になる。偽のキーで
    競合したときは、ON CONFLICT の対象外の制約に違反したことになるので、
    競合解決の指定があってもエラーにする(sqlite と同じ)。
    """

    indexes: list[int]
    resolve: bool
    table: str
    label: str | None  # UNIQUE 制約の対象列。主キーなら None
    positions: dict[tuple[Value, ...], int] = field(default_factory=dict)

    def key(self, row: Row) -> tuple[Value, ...] | None:
        """行のキー。NULL を含むなら索引の対象外として None を返す。"""
        key = tuple(row[i] for i in self.indexes)
        return None if any(value is None for value in key) else key

    def find(self, row: Row) -> tuple[tuple[Value, ...], int] | None:
        """行と同じキーを持つ既存行の (キー, 位置)。無ければ None。"""
        key = self.key(row)
        if key is None:
            return None
        position = self.positions.get(key)
        return None if position is None else (key, position)

    def add(self, position: int, row: Row) -> None:
        key = self.key(row)
        if key is not None:
            self.positions[key] = position

    def remove(self, row: Row) -> None:
        key = self.key(row)
        if key is not None:
            self.positions.pop(key, None)

    def violation(self, key: tuple[Value, ...]) -> IntegrityError:
        if self.label is not None:
            return IntegrityError(f"UNIQUE constraint failed: {self.label}")
        # 単一列の主キーは _check_primary_key と同じく値そのものを見せる
        shown = key[0] if len(key) == 1 else key
        return IntegrityError(
            f"UNIQUE constraint failed: {self.table} primary key {shown!r}"
        )


@dataclass
class _Conflict:
    """INSERT の競合解決の指定。"""

    action: str  # "ignore" | "replace" | "update"
    keys: list[_KeyIndex]
    set_items: list[tuple[str, exp.Expression]] = field(default_factory=list)
    where: exp.Expression | None = None
    # OR IGNORE は一意制約以外(NOT NULL / CHECK)の違反も飛ばす
    lenient: bool = False


def _key_indexes(schema: TableSchema, targets: set[str] | None) -> list[_KeyIndex]:
    """テーブルの一意キーを列挙する。``targets`` に一致するキーだけ解決対象にする。"""
    table = schema.table
    keys: list[_KeyIndex] = []
    if schema.primary_key:
        keys.append(
            _KeyIndex(
                indexes=[schema.column_index(c) for c in schema.primary_key],
                resolve=targets is None or targets == set(schema.primary_key),
                table=table,
                label=None,
            )
        )
    for constraint in schema.unique:
        keys.append(
            _KeyIndex(
                indexes=[schema.column_index(c) for c in constraint.columns],
                resolve=targets is None or targets == set(constraint.columns),
                table=table,
                label=", ".join(f"{table}.{c}" for c in constraint.columns),
            )
        )
    if targets is not None and not any(key.resolve for key in keys):
        raise ProgrammingError(
            "ON CONFLICT clause does not match any PRIMARY KEY or UNIQUE constraint"
        )
    return keys


def _conflict_targets(node: exp.OnConflict) -> set[str] | None:
    """ON CONFLICT の対象列。省略されていれば None(すべての一意制約が対象)。"""
    conflict_keys = node.args.get("conflict_keys")
    if not conflict_keys:
        return None
    names: set[str] = set()
    for item in conflict_keys:
        column = item if isinstance(item, exp.Column) else item.find(exp.Column)
        if column is None:
            raise NotSupportedError(
                f"unsupported ON CONFLICT target: {item.sql(dialect='sqlite')!r}"
            )
        names.add(column.name)
    return names


def _conflict(ast: exp.Insert, schema: TableSchema) -> _Conflict | None:
    """INSERT の競合解決の指定を読む。既定(競合したらエラー)なら None。"""
    node = node_arg(ast, "conflict")
    if node is None:
        alternative = node_arg(ast, "alternative")
        if alternative is None or alternative in _ABORTING_ALTERNATIVES:
            return None
        if alternative == "IGNORE":
            return _Conflict("ignore", _key_indexes(schema, None), lenient=True)
        if alternative == "REPLACE":
            return _Conflict("replace", _key_indexes(schema, None))
        # OR ROLLBACK は開いているトランザクション全体の巻き戻しを伴う
        raise NotSupportedError(f"INSERT OR {alternative} is not supported")

    action_node = node.args.get("action")
    action = action_node.name.upper() if action_node is not None else ""
    keys = _key_indexes(schema, _conflict_targets(node))
    if action == "DO NOTHING":
        return _Conflict("ignore", keys)
    if action != "DO UPDATE":
        raise NotSupportedError(
            f"unsupported ON CONFLICT action: {node.sql(dialect='sqlite')!r}"
        )
    set_items: list[tuple[str, exp.Expression]] = []
    for item in node.expressions:
        if not (isinstance(item, exp.EQ) and isinstance(item.this, exp.Column)):
            raise NotSupportedError(
                f"unsupported SET clause: {item.sql(dialect='sqlite')!r}"
            )
        schema.column(item.this.name)  # 存在チェック
        set_items.append((item.this.name, item.expression))
    if not set_items:
        # sqlglot は SET の無い DO UPDATE も通すが、更新する内容が無い
        raise ProgrammingError("ON CONFLICT DO UPDATE requires a SET clause")
    where = node.args.get("where")
    return _Conflict("update", keys, set_items, where.this if where else None)


def _qualify(node: exp.Expression, table: str) -> exp.Expression:
    """修飾の無い列参照に対象テーブル名を付ける。

    DO UPDATE の SET と WHERE は対象行と excluded 行の 2 つのテーブルで
    評価するため、修飾を省いた列がどちらを指すかを決めておく必要がある。
    sqlite と同じく対象行を指す。
    """
    for column in node.find_all(exp.Column):
        if not column.args.get("table"):
            column.set("table", exp.to_identifier(table))
    return node


def _constant_getter(
    schema: TableSchema, table: str, node: exp.Expression
) -> Callable[[Row, Row], Value] | None:
    """SET の右辺が定数か単純な列参照なら、値を取り出す関数を返す。

    ``SET name = excluded.name`` のような形はこれで済ませ、SELECT への還元を
    競合行ごとに走らせずに済ませる。
    """
    try:
        value = _eval_constant(node)
    except (NotSupportedError, ProgrammingError):
        pass
    else:
        return lambda _existing, _incoming: value
    if isinstance(node, exp.Column):
        index = schema.column_index(node.name)
        if node.table == EXCLUDED:
            return lambda _existing, incoming: incoming[index]
        if node.table in ("", table):
            return lambda existing, _incoming: existing[index]
    return None


def _set_evaluator(
    schema: TableSchema, table: str, conflict: _Conflict
) -> Callable[[Row, Row], Row | None]:
    """DO UPDATE の SET を評価して、更新後の行を返す関数を組む。

    返す関数は WHERE が偽のときだけ None を返す。
    """
    indexes = [schema.column_index(column) for column, _ in conflict.set_items]

    def apply(existing: Row, values: Sequence[Value]) -> Row:
        updated = list(existing)
        for index, value in zip(indexes, values, strict=True):
            updated[index] = value
        return schema.validate_values(updated)

    getters: list[Callable[[Row, Row], Value]] = []
    for _, node in conflict.set_items:
        getter = _constant_getter(schema, table, node) if conflict.where is None else None
        if getter is None:
            getters = []
            break
        getters.append(getter)

    if getters:

        def evaluate_fast(existing: Row, incoming: Row) -> Row | None:
            return apply(existing, [get(existing, incoming) for get in getters])

        return evaluate_fast

    # SELECT <set 式...> FROM t CROSS JOIN excluded WHERE ... に還元する
    select = exp.select(
        *[
            exp.alias_(_qualify(node.copy(), table), f"__set_{i}")
            for i, (_, node) in enumerate(conflict.set_items)
        ]
    ).from_(table)
    select = select.join(
        exp.Table(this=exp.to_identifier(EXCLUDED)), join_type="CROSS"
    )
    if conflict.where is not None:
        select = select.where(_qualify(conflict.where.copy(), table))
    columns = schema.column_names
    annotation = {c.name: executor._SQLGLOT_TYPES[c.type] for c in schema.columns}
    annotations = {table: annotation, EXCLUDED: annotation}

    def evaluate(existing: Row, incoming: Row) -> Row | None:
        tables = {
            table: sqlglot_table(columns, [existing]),
            EXCLUDED: sqlglot_table(columns, [incoming]),
        }
        _, matched = executor.evaluate(select, tables, annotations)
        return apply(existing, matched[0]) if matched else None

    return evaluate


class _RowBuilder:
    """VALUES の 1 組から検証済みの行を組む。自動採番の状態を持つ。"""

    def __init__(self, schema: TableSchema, columns: list[str], rows: list[Row]) -> None:
        self.schema = schema
        self.columns = columns
        auto = schema.autoincrement_column
        self.index = schema.column_index(auto.name) if auto is not None else None
        self.next_id = 0 if self.index is None else _next_autoincrement(rows, self.index)
        self.last_id: int | None = None

    def build(self, values: Sequence[Value]) -> Row:
        if len(values) != len(self.columns):
            raise ProgrammingError(
                f"INSERT has {len(values)} values for {len(self.columns)} columns"
            )
        row = self.schema.arrange_row(self.columns, values)
        # 列指定からの省略と明示的な NULL は、どちらもここで NULL になっている
        if self.index is not None and row[self.index] is None:
            row[self.index] = self.next_id
        return self.schema.validate_values(row)

    def accept(self, row: Row) -> None:
        """行が実際に入ったときに採番の状態を進める。

        競合して入らなかった行は採番を消費しない。既存の最大値 + 1 で採る
        以上、入らなかった行が次の値を動かすことはないため。
        """
        if self.index is None:
            return
        assigned = row[self.index]
        assert isinstance(assigned, int)
        # 同じ文の中で明示された大きい値も、次の採番に反映する
        self.next_id = max(self.next_id, assigned + 1)
        self.last_id = assigned


def _upsert_rows(
    catalog: Catalog,
    table: str,
    schema: TableSchema,
    rows: list[Row],
    builder: _RowBuilder,
    new_values: Iterable[Sequence[Value]],
    conflict: _Conflict,
) -> StatementResult:
    """競合解決の指定つきで行を追加する。

    一意キーの索引を持ちながら 1 行ずつ処理する。既存行との競合も、同じ文の
    中で先に入った行との競合も同じ経路で見えるため、sqlite と同じく後の行が
    前の行の結果を見る。
    """
    for position, row in enumerate(rows):
        for key in conflict.keys:
            key.add(position, row)

    evaluate_set = (
        _set_evaluator(schema, table, conflict) if conflict.action == "update" else None
    )
    dead: set[int] = set()
    count = 0

    for values in new_values:
        try:
            row = builder.build(values)
            # NOT NULL と CHECK は、競合して入らない行にも sqlite が課している。
            # OR IGNORE だけがその違反も飛ばす
            check_expressions(schema, [row])
        except IntegrityError:
            if conflict.lenient:
                continue
            raise
        hits: list[int] = []
        outside: list[IntegrityError] = []
        for key in conflict.keys:
            found = key.find(row)
            if found is None:
                continue
            value, position = found
            if not key.resolve:
                outside.append(key.violation(value))
            elif position not in hits:
                hits.append(position)
        if not hits:
            # 競合を解決するなら行は入らないので、対象外の制約に当たるのは
            # そのまま追加する場合だけになる
            if outside:
                raise outside[0]
            added = len(rows)
            rows.append(row)
            for key in conflict.keys:
                key.add(added, row)
            builder.accept(row)
            count += 1
            continue
        if conflict.action == "ignore":
            continue
        if conflict.action == "replace":
            # 競合した既存行をすべて捨てて、いちばん前の位置に新しい行を置く
            target = min(hits)
            for position in hits:
                for key in conflict.keys:
                    key.remove(rows[position])
                dead.add(position)
            dead.discard(target)
            rows[target] = row
            for key in conflict.keys:
                key.add(target, row)
            builder.accept(row)
            count += 1
            continue
        assert evaluate_set is not None
        target = min(hits)
        updated = evaluate_set(rows[target], row)
        if updated is None:  # WHERE が偽なので何もしない
            continue
        check_expressions(schema, [updated])
        for key in conflict.keys:
            # 更新後の行が別の行と一意キーで並ぶなら、それは解決できない違反
            found = key.find(updated)
            if found is not None and found[1] != target:
                raise key.violation(found[0])
        for key in conflict.keys:
            key.remove(rows[target])
        rows[target] = updated
        for key in conflict.keys:
            key.add(target, updated)
        count += 1

    if dead:
        rows = [row for position, row in enumerate(rows) if position not in dead]
    catalog.write_rows(table, rows, schema)
    return StatementResult(rowcount=count, lastrowid=builder.last_id)


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

    conflict = _conflict(ast, schema)
    rows = catalog.read_rows(table)
    builder = _RowBuilder(schema, columns, rows)
    if conflict is not None:
        return _upsert_rows(catalog, table, schema, rows, builder, new_values, conflict)

    existing = len(rows)
    for values in new_values:
        validated = builder.build(values)
        builder.accept(validated)
        rows.append(validated)
    _check_primary_key(schema, rows, existing)
    check_unique(schema, rows, existing)
    check_expressions(schema, rows, range(existing, len(rows)))
    catalog.write_rows(table, rows, schema)
    return StatementResult(rowcount=len(new_values), lastrowid=builder.last_id)


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
    changed: list[int] = []
    for row in matched:
        rowid = row[0]
        assert isinstance(rowid, int)
        updated = list(rows[rowid])
        for index, value in zip(set_indexes, row[1:], strict=True):
            updated[index] = value
        rows[rowid] = schema.validate_values(updated)
        changed.append(rowid)
    _check_primary_key(schema, rows)
    check_unique(schema, rows)
    check_expressions(schema, rows, changed)
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
