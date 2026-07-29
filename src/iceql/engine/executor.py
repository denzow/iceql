"""SELECT の実行: sqlglot.executor への委譲と前処理。

sqlglot.executor は次の制約があるため、ORDER BY / LIMIT / OFFSET は
トップレベルで AST から取り外し、iceql 側(Python)で適用する:
- JOIN を含むクエリで射影に含まれない列の ORDER BY が黙って無視される
- NULL 混在キーの DESC ソートが TypeError で落ちる
- OFFSET が無視される
射影に無いソートキーは隠し列(__ord_N)として SELECT 句に追加して値を計算させ、
結果から取り除く。NULL の位置は SQLite と同じ既定(NULL 最小)。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time

from sqlglot import exp
from sqlglot.errors import ExecuteError, OptimizeError, SqlglotError
from sqlglot.executor import execute as sqlglot_execute
from sqlglot.executor.env import ENV, null_if_any

from iceql.catalog import Catalog
from iceql.engine import SQL_DIALECT, StatementResult
from iceql.errors import NotSupportedError, OperationalError, ProgrammingError
from iceql.storage import Row
from iceql.types import Value


def _to_datetime(value: object) -> datetime:
    """ISO 文字列(iceql の date/datetime 内部表現)を datetime に変換する。"""
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time())
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    raise OperationalError(f"cannot interpret {value!r} as a datetime")


# sqlglot.executor の実行環境に足りない SQLite 系の関数を補う
ENV.setdefault("LENGTH", null_if_any(lambda x: len(x)))  # type: ignore[no-untyped-call]
ENV.setdefault("REPLACE", null_if_any(lambda s, old, new: s.replace(old, new)))  # type: ignore[no-untyped-call]
ENV.setdefault("DPIPE", null_if_any(lambda *xs: "".join(str(x) for x in xs)))  # type: ignore[no-untyped-call]
ENV.setdefault("NULLIF", lambda a, b: None if a == b else a)
ENV.setdefault("NOW", datetime.now)
# STRFTIME(fmt, value) は TIMETOSTR(TSORDSTOTIMESTAMP(value), fmt) に展開される
ENV.setdefault("TSORDSTOTIMESTAMP", null_if_any(_to_datetime))  # type: ignore[no-untyped-call]
ENV.setdefault("TIMETOSTR", null_if_any(lambda v, fmt: _to_datetime(v).strftime(fmt)))  # type: ignore[no-untyped-call]

# executor に渡すスキーマ注釈。date/datetime は ISO 文字列のまま比較するので text
_SQLGLOT_TYPES = {
    "integer": "bigint",
    "real": "double",
    "boolean": "boolean",
    "text": "text",
    "date": "text",
    "datetime": "text",
}

_HIDDEN_PREFIX = "__ord_"


@dataclass
class _OrderKey:
    index: int | str  # 列位置、または実行後に列名で解決する場合は列名
    desc: bool
    nulls_first: bool


def _precheck(ast: exp.Expression) -> None:
    if ast.find(exp.Window):
        raise NotSupportedError("window functions are not supported")
    for select in ast.find_all(exp.Select):
        for projection in select.expressions:
            if projection.find(exp.Select):
                raise NotSupportedError(
                    "scalar subqueries in the SELECT list are not supported"
                )
    for agg in ast.find_all(exp.AggFunc):
        # sqlglot の optimizer が集約内の DISTINCT を黙って落とし誤答になるため拒否する
        if agg.find(exp.Distinct):
            raise NotSupportedError(
                "DISTINCT inside aggregate functions is not supported; "
                "use a subquery instead, e.g. "
                "SELECT COUNT(*) FROM (SELECT DISTINCT x FROM t WHERE x IS NOT NULL)"
            )


def _int_literal(node: exp.Expression, clause: str) -> int:
    if isinstance(node, exp.Literal) and node.is_int:
        return int(node.name)
    raise NotSupportedError(f"{clause} must be an integer literal")


def _projection_index(projections: list[exp.Expression], expr: exp.Expression) -> int | None:
    """ソートキーが既存の射影に一致するならその位置を返す。"""
    target = expr.sql(dialect=SQL_DIALECT)
    bare_name = expr.name if isinstance(expr, exp.Column) and not expr.table else None
    for i, proj in enumerate(projections):
        if isinstance(proj, exp.Alias):
            if bare_name is not None and bare_name == proj.alias:
                return i
            if proj.this.sql(dialect=SQL_DIALECT) == target:
                return i
        elif proj.sql(dialect=SQL_DIALECT) == target or (
            bare_name is not None
            and isinstance(proj, exp.Column)
            and proj.name == bare_name
        ):
            return i
    return None


def _extract_order_limit(
    ast: exp.Expression,
) -> tuple[list[_OrderKey], int, int | None, int]:
    """トップレベルの ORDER BY / LIMIT / OFFSET を AST から取り外す。

    返り値: (ソートキー, 隠し射影列の数, LIMIT, OFFSET)
    """
    limit: int | None = None
    offset = 0
    limit_node = ast.args.get("limit")
    if isinstance(limit_node, exp.Limit):
        limit = _int_literal(limit_node.expression, "LIMIT")
        ast.set("limit", None)
    offset_node = ast.args.get("offset")
    if isinstance(offset_node, exp.Offset):
        offset = _int_literal(offset_node.expression, "OFFSET")
        ast.set("offset", None)

    keys: list[_OrderKey] = []
    hidden = 0
    order_node = ast.args.get("order")
    if isinstance(order_node, exp.Order):
        is_select = isinstance(ast, exp.Select)
        projections = list(ast.expressions) if is_select else []
        distinct = bool(ast.args.get("distinct")) if is_select else True
        # SELECT * があると AST 上の射影位置と結果の列位置が一致しないため、
        # その場合は列名で解決する(隠し列も名前で引く)
        has_star = any(
            isinstance(p, exp.Star)
            or (isinstance(p, exp.Column) and isinstance(p.this, exp.Star))
            for p in projections
        )
        for ordered in order_node.expressions:
            expr = ordered.this
            desc = bool(ordered.args.get("desc"))
            nulls_first = ordered.args.get("nulls_first")
            if nulls_first is None:
                nulls_first = not desc  # SQLite の既定: NULL 最小
            index: int | str
            if isinstance(expr, exp.Literal) and expr.is_int:
                # ORDER BY 1 のような序数指定(結果の列位置に対して常に有効)
                ordinal = int(expr.name)
                if is_select and not has_star and not 1 <= ordinal <= len(projections):
                    raise ProgrammingError(f"ORDER BY position {ordinal} is out of range")
                index = ordinal - 1
            elif not is_select:
                if isinstance(expr, exp.Column) and not expr.table:
                    # UNION 等: 実行後に列名で解決する
                    index = expr.name
                else:
                    raise NotSupportedError(
                        "ORDER BY on a set operation must reference an output column name"
                    )
            elif has_star:
                if isinstance(expr, exp.Column):
                    index = expr.name
                elif distinct:
                    raise NotSupportedError(
                        "ORDER BY expressions must appear in the SELECT list "
                        "when using DISTINCT"
                    )
                else:
                    alias = f"{_HIDDEN_PREFIX}{hidden}"
                    ast.append("expressions", exp.alias_(expr.copy(), alias))
                    index = alias
                    hidden += 1
            else:
                matched = _projection_index(projections, expr)
                if matched is not None:
                    index = matched
                elif distinct:
                    raise NotSupportedError(
                        "ORDER BY expressions must appear in the SELECT list "
                        "when using DISTINCT"
                    )
                else:
                    alias = f"{_HIDDEN_PREFIX}{hidden}"
                    ast.append("expressions", exp.alias_(expr.copy(), alias))
                    index = len(projections) + hidden
                    hidden += 1
            keys.append(_OrderKey(index=index, desc=desc, nulls_first=nulls_first))
        ast.set("order", None)
    return keys, hidden, limit, offset


def _sort_rows(
    rows: list[tuple[Value, ...]],
    columns: list[str],
    keys: list[_OrderKey],
) -> list[tuple[Value, ...]]:
    result = list(rows)
    for key in reversed(keys):
        if isinstance(key.index, str):
            try:
                idx = columns.index(key.index)
            except ValueError:
                raise ProgrammingError(f"ORDER BY: no such column: {key.index}") from None
        else:
            idx = key.index
        # 昇順キー + reverse=desc で安定ソート。NULL の位置はフラグで制御する。
        # (nulls_first XOR desc) が真なら NULL を「小さい側」に置く
        null_small = key.nulls_first != key.desc

        def sort_key(
            row: tuple[Value, ...], idx: int = idx, null_small: bool = null_small
        ) -> tuple[int, Value]:
            value = row[idx]
            if value is None:
                return (0 if null_small else 1, 0)
            return (1 if null_small else 0, value)

        try:
            result.sort(key=sort_key, reverse=key.desc)
        except TypeError as exc:
            raise OperationalError(f"ORDER BY: cannot compare values: {exc}") from exc
    return result


def physical_tables(ast: exp.Expression, catalog: Catalog) -> set[str]:
    """AST が参照する実テーブル名(CTE を除く)を返す。"""
    cte_names = {cte.alias_or_name for cte in ast.find_all(exp.CTE)}
    names = set()
    for table in ast.find_all(exp.Table):
        if table.name and table.name not in cte_names:
            names.add(table.name)
    return names


def load_tables(
    catalog: Catalog, names: set[str]
) -> tuple[dict[str, list[Row]], dict[str, dict[str, str]]]:
    tables: dict[str, list[Row]] = {}
    schema: dict[str, dict[str, str]] = {}
    for name in sorted(names):
        table_schema = catalog.load_schema(name)  # 存在しなければ ProgrammingError
        tables[name] = catalog.read_rows(name)
        schema[name] = {c.name: _SQLGLOT_TYPES[c.type] for c in table_schema.columns}
    return tables, schema


def evaluate(
    ast: exp.Expression,
    tables: dict[str, list[Row]],
    schema: dict[str, dict[str, str]],
) -> tuple[list[str], list[tuple[Value, ...]]]:
    """前処理済み AST を sqlglot.executor で評価する。"""
    try:
        result = sqlglot_execute(ast, schema=schema or None, tables=tables)
    except OptimizeError as exc:
        raise ProgrammingError(f"invalid query: {exc}") from exc
    except ExecuteError as exc:
        raise OperationalError(f"query execution failed: {exc}") from exc
    except SqlglotError as exc:
        raise OperationalError(f"query execution failed: {exc}") from exc
    return list(result.columns), [tuple(row) for row in result.rows]


def run_select(catalog: Catalog, ast: exp.Expression) -> StatementResult:
    _precheck(ast)
    keys, hidden, limit, offset = _extract_order_limit(ast)
    tables, schema = load_tables(catalog, physical_tables(ast, catalog))
    columns, rows = evaluate(ast, tables, schema)
    if keys:
        rows = _sort_rows(rows, columns, keys)
    if hidden:
        columns = columns[:-hidden]
        rows = [row[:-hidden] for row in rows]
    if offset:
        rows = rows[offset:]
    if limit is not None:
        rows = rows[:limit]
    return StatementResult(columns=columns, rows=rows, rowcount=-1)
