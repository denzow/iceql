"""SELECT の実行: sqlglot.executor への委譲と前処理。

sqlglot.executor は次の制約があるため、ORDER BY / LIMIT / OFFSET は
トップレベルで AST から取り外し、iceql 側(Python)で適用する:
- JOIN を含むクエリで射影に含まれない列の ORDER BY が黙って無視される
- NULL 混在キーの DESC ソートが TypeError で落ちる
- OFFSET が無視される
射影に無いソートキーは隠し列(__ord_N)として SELECT 句に追加して値を計算させ、
結果から取り除く。NULL の位置は SQLite と同じ既定(NULL 最小)。

取り外せるのはトップレベルだけなので、サブクエリに残る LIMIT / OFFSET は
_materialize_nested_limits で実体化する。内側のサブクエリを単体で評価し、
結果を合成テーブルに置き換えてから外側を実行する。LIMIT が AST から消えるので
sqlglot の optimizer が IN サブクエリの unnest を諦めなくなり、OFFSET も
iceql 側で適用できる。外側の列を参照するサブクエリ(相関サブクエリ)は
一度の評価で結果が決まらないので拒否する。

テーブルは sqlglot.executor.table.Table として組み立てて渡す。行を dict の
リストで渡すと、sqlglot が行ごと・列ごとに列名を正規化し直して別表現へ複製し、
その分だけ時間とメモリを使う。組み立てと使い回しは iceql.tablecache が持つ。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import cast

from sqlglot import exp
from sqlglot.errors import ExecuteError, OptimizeError, SqlglotError
from sqlglot.executor import execute as sqlglot_execute
from sqlglot.executor.env import ENV, null_if_any
from sqlglot.executor.table import Table
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import build_scope

from iceql.catalog import Catalog
from iceql.engine import SQL_DIALECT, StatementResult, node_arg
from iceql.errors import NotSupportedError, OperationalError, ProgrammingError
from iceql.tablecache import sqlglot_table
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
    limit_node = node_arg(ast, "limit")
    if isinstance(limit_node, exp.Limit):
        limit = _int_literal(limit_node.expression, "LIMIT")
        ast.set("limit", None)
    offset_node = node_arg(ast, "offset")
    if isinstance(offset_node, exp.Offset):
        offset = _int_literal(offset_node.expression, "OFFSET")
        ast.set("offset", None)

    keys: list[_OrderKey] = []
    hidden = 0
    order_node = node_arg(ast, "order")
    if isinstance(order_node, exp.Order):
        is_select = isinstance(ast, exp.Select)
        projections = list(ast.expressions) if is_select else []
        distinct = bool(node_arg(ast, "distinct")) if is_select else True
        # SELECT * があると AST 上の射影位置と結果の列位置が一致しないため、
        # その場合は列名で解決する(隠し列も名前で引く)
        has_star = any(
            isinstance(p, exp.Star)
            or (isinstance(p, exp.Column) and isinstance(p.this, exp.Star))
            for p in projections
        )
        for ordered in order_node.expressions:
            expr = ordered.this
            desc = bool(node_arg(ordered, "desc"))
            nulls_first = node_arg(ordered, "nulls_first")
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
) -> tuple[dict[str, Table], dict[str, dict[str, str]]]:
    tables: dict[str, Table] = {}
    schema: dict[str, dict[str, str]] = {}
    for name in sorted(names):
        table_schema = catalog.load_schema(name)  # 存在しなければ ProgrammingError
        # CSV が前回のクエリから変わっていなければ Table を作り直さない
        tables[name] = catalog.query_table(name, table_schema)
        schema[name] = {c.name: _SQLGLOT_TYPES[c.type] for c in table_schema.columns}
    return tables, schema


def evaluate(
    ast: exp.Expression,
    tables: dict[str, Table],
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


_SYNTHETIC_PREFIX = "__iceql_sub_"
_WITH_ARGS = ("with", "with_")


def _column_type(values: list[Value]) -> str:
    """実体化した列の型注釈を、実値から決める。

    サブクエリの結果には型情報が付いてこないので、executor に渡す注釈は
    値から推定するしかない。値が 1 つも無い(全行 NULL / 0 行)列は text とみなす。
    比較は Python の値どうしで行われるため、注釈が実際の値と食い違っても
    結果は変わらない。
    """
    seen = set()
    for value in values:
        if value is None:
            continue
        if isinstance(value, bool):
            seen.add("boolean")
        elif isinstance(value, int):
            seen.add("bigint")
        elif isinstance(value, float):
            seen.add("double")
        else:
            seen.add("text")
    if len(seen) == 1:
        return seen.pop()
    if seen == {"bigint", "double"}:
        return "double"
    return "text"


def _visible_ctes(node: exp.Expression) -> list[exp.CTE]:
    """node の外側で定義されている CTE を、内側の定義を優先して集める。

    サブクエリを切り離して単体で評価するとき、そのサブクエリが参照する CTE も
    一緒に持っていく必要がある。node 自身を含む CTE は循環するので外す。
    使われない CTE は sqlglot の optimizer(eliminate_ctes)が落とす。
    """
    ctes: list[exp.CTE] = []
    names: set[str] = set()
    enclosing: set[int] = set()
    current = node.parent
    while current is not None:
        if isinstance(current, exp.CTE):
            enclosing.add(id(current))
        with_clause = _with_clause(current)
        if with_clause is not None:
            for cte in with_clause.expressions:
                name = cte.alias_or_name
                if id(cte) in enclosing or name in names:
                    continue
                names.add(name)
                ctes.append(cte)
        current = current.parent
    return ctes


def _with_clause(node: exp.Expr) -> exp.With | None:
    """ノードの WITH 句を返す(引数名は sqlglot のバージョンで with / with_)。"""
    if not isinstance(node, exp.Expression):
        return None
    if not any(name in type(node).arg_types for name in _WITH_ARGS):
        return None
    clause = node_arg(node, *_WITH_ARGS)
    return clause if isinstance(clause, exp.With) else None


def _set_with_clause(node: exp.Expression, ctes: list[exp.CTE]) -> None:
    key = next(name for name in _WITH_ARGS if name in type(node).arg_types)
    node.set(key, exp.With(expressions=ctes))


def _with_ctes(node: exp.Expression, ctes: list[exp.CTE]) -> exp.Expression:
    """切り離したサブクエリに、外側から見えていた CTE を付け直す。"""
    own = _with_clause(node)
    inner = list(own.expressions) if own is not None else []
    defined = {cte.alias_or_name for cte in inner}
    extra = [cte.copy() for cte in ctes if cte.alias_or_name not in defined]
    if not extra:
        return node
    if not isinstance(node, exp.Select):
        # 集合演算(UNION 等)は WITH 句を持てないので、派生表に包んで付ける
        node = exp.Select(expressions=[exp.Star()]).from_(
            exp.Subquery(this=node, alias=exp.TableAlias(this=exp.to_identifier("_q")))
        )
        inner = []
    _set_with_clause(node, extra + inner)
    return node


def _materialize(
    node: exp.Expression,
    name: str,
    tables: dict[str, Table],
    schema: dict[str, dict[str, str]],
) -> None:
    """サブクエリを単体で評価し、結果の合成テーブルへの参照に置き換える。"""
    sub = node.copy()
    # ORDER BY / LIMIT / OFFSET の取り外しは、CTE を付けて包む前に行う
    # (包んだあとでは外側の Select に無く、内側に残ったまま executor へ渡る)
    keys, hidden, limit, offset = _extract_order_limit(sub)
    sub = _with_ctes(sub, _visible_ctes(node))
    columns, rows = evaluate(sub, tables, schema)
    if keys:
        rows = _sort_rows(rows, columns, keys)
    if hidden:
        columns = columns[:-hidden]
        rows = [row[:-hidden] for row in rows]
    if offset:
        rows = rows[offset:]
    if limit is not None:
        rows = rows[:limit]
    if isinstance(node.parent, exp.Exists):
        # 相関していない EXISTS は行の有無で値が決まる。sqlglot の executor は
        # EXISTS 式そのものを評価できない(生成コードが構文エラーになる)ので、
        # 実体化した時点で真偽値に畳む。
        node.parent.replace(exp.true() if rows else exp.false())
        return
    tables[name] = sqlglot_table(columns, rows)
    schema[name] = {
        column: _column_type([row[i] for row in rows]) for i, column in enumerate(columns)
    }
    node.replace(exp.select(exp.Star()).from_(exp.to_table(name)))


def _check_not_in(node: exp.Expression) -> None:
    """NOT IN のサブクエリは実体化しても誤答になるので拒否する。

    sqlglot の optimizer は、NULL を含むと三値論理が崩れることを理由に
    NOT IN の unnest を意図的に見送る。残された NOT IN を executor は
    評価できず、行が黙って素通りする。実体化しても IN が式のまま残る点は
    変わらないため、ここで止める。
    """
    predicate = node.parent
    if isinstance(predicate, exp.Subquery):
        predicate = predicate.parent
    if isinstance(predicate, exp.In) and isinstance(predicate.parent, exp.Not):
        raise NotSupportedError(
            "LIMIT / OFFSET inside a NOT IN subquery is not supported; "
            "rewrite it as a LEFT JOIN, e.g. "
            "SELECT t.x FROM t LEFT JOIN (SELECT x FROM u ORDER BY x LIMIT 2) s "
            "ON s.x = t.x WHERE s.x IS NULL"
        )


def _materialize_nested_limits(
    ast: exp.Expression,
    tables: dict[str, Table],
    schema: dict[str, dict[str, str]],
) -> exp.Expression:
    """トップレベル以外に残った LIMIT / OFFSET を実体化する。

    列の修飾子を見て相関を判定するため、先に qualify を通す。修飾子なしで
    外側を参照する形(``WHERE sid = id``)は qualify しないと拾えない。
    実体化は内側から順に行う(内側の結果を外側の評価が使う)。
    """
    if ast.find(exp.Limit, exp.Offset) is None:
        return ast
    try:
        ast = qualify(
            ast, schema=cast("dict[str, object] | None", schema or None), dialect=SQL_DIALECT
        )
    except OptimizeError as exc:
        raise ProgrammingError(f"invalid query: {exc}") from exc
    root = build_scope(ast)
    if root is None:
        raise NotSupportedError("LIMIT / OFFSET in this position is not supported")
    targets: list[exp.Expression] = []
    # traverse() は内側のスコープから順に返す
    for scope in root.traverse():
        node = scope.expression
        if not isinstance(node, (exp.Select, exp.SetOperation)) or node is ast:
            continue
        if not (node_arg(node, "limit") or node_arg(node, "offset")):
            continue
        if scope.external_columns:
            outer = scope.external_columns[0].sql(dialect=SQL_DIALECT)
            raise NotSupportedError(
                "LIMIT / OFFSET in a correlated subquery is not supported "
                f"(the subquery references {outer} from the outer query)"
            )
        _check_not_in(node)
        targets.append(node)
    for index, node in enumerate(targets):
        _materialize(node, f"{_SYNTHETIC_PREFIX}{index}", tables, schema)
    if ast.find(exp.Limit, exp.Offset) is not None:
        raise NotSupportedError("LIMIT / OFFSET in this position is not supported")
    return ast


def run_select(catalog: Catalog, ast: exp.Expression) -> StatementResult:
    _precheck(ast)
    keys, hidden, limit, offset = _extract_order_limit(ast)
    tables, schema = load_tables(catalog, physical_tables(ast, catalog))
    ast = _materialize_nested_limits(ast, tables, schema)
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
