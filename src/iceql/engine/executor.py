"""SELECT の実行: sqlglot.executor への委譲と前処理。

sqlglot.executor は次の制約があるため、ORDER BY / LIMIT / OFFSET は
トップレベルで AST から取り外し、iceql 側(Python)で適用する:
- JOIN を含むクエリで射影に含まれない列の ORDER BY が黙って無視される
- NULL 混在キーの DESC ソートが TypeError で落ちる
- OFFSET が無視される
射影に無いソートキーは隠し列(__ord_N)として SELECT 句に追加して値を計算させ、
結果から取り除く。NULL の位置は SQLite と同じ既定(NULL 最小)。

sqlglot の optimizer が扱えないサブクエリは rewrite_subqueries で先に片付ける。
内側のサブクエリを単体で評価し、結果に応じて AST を書き換えてから外側を実行する。
外側の列を参照するサブクエリ(相関サブクエリ)は一度の評価で結果が決まらないので、
書き換えが要る形なら拒否する。書き換えの内訳は次の 3 つ:

- LIMIT / OFFSET が残るサブクエリ: 結果を合成テーブルに置き換える。LIMIT が
  AST から消えるので optimizer が IN サブクエリの unnest を諦めなくなり、
  OFFSET も iceql 側で適用できる
- 非相関の EXISTS: 行の有無で値が決まるので真偽値に畳む
- NOT IN: 三値論理どおりの式に畳む

後ろ 2 つは、残すと sqlglot.executor が誤答や構文エラーを返す。

相関 EXISTS は sqlglot の decorrelate が join へ書き換える。書き換えられない形
(相関条件に等値が無い、OR がある、など)は EXISTS 式が AST に残り、やはり
sqlglot.executor が構文エラーを返すので、実行の前に拒否する。書き換えられても
結果が元の意味とずれる形もあるので、decorrelate に渡す前に均す
(_normalize_correlated_exists)。書き換えが通る形でも、EXISTS が join の ON 句や
HAVING にあると、decorrelate が足す LEFT JOIN より先に評価されて実行が落ちるので、
置き場所も実行の前に見る(_exists_position_rejection)。

sqlglot.executor は SQL 式を Python 式に落として評価する。IN と NOT は Python の
集合の帰属判定と ``not`` にそのまま落ちて NULL を伝播しないので、三値論理どおりに
評価する関数へ差し替える(_sql_in / _sql_not)。NOT LIKE は Like ノードの negate
フラグが Python 式に現れず否定ごと消えるので、生成側で NOT() に包み直す。

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
from sqlglot.generator import Generator
from sqlglot.generators.python import PythonGenerator
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, build_scope
from sqlglot.optimizer.unnest_subqueries import unnest_subqueries

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


def _sql_in(value: Value, *candidates: Value) -> bool | None:
    """IN を三値論理で評価する。

    左辺が NULL なら結果は NULL。一致する値があれば真。一致が無くても値側に
    NULL があれば「一致しないとは言い切れない」ので NULL、無ければ偽になる。
    """
    if value is None:
        return None
    unknown = False
    for candidate in candidates:
        if candidate is None:
            unknown = True
        elif candidate == value:
            return True
    return None if unknown else False


def _sql_not(value: Value) -> bool | None:
    """NOT を三値論理で評価する(NULL の否定は NULL)。"""
    return None if value is None else not value


ENV["IN"] = _sql_in
ENV["NOT"] = _sql_not

# sqlglot が生成する Python 式は、IN を集合の帰属判定 (``x in {...}``)、NOT を
# Python の ``not`` にそのまま落とす。どちらも NULL を伝播せず、左辺が NULL の
# IN が偽になり、NOT を通ると真に化ける。三値論理どおりに評価する ENV の関数
# 呼び出しへ差し替える。
_IN_AS_SET = PythonGenerator.TRANSFORMS[exp.In]


def _in_py(generator: Generator, node: exp.In) -> str:
    if node_arg(node, "query") is not None:
        # サブクエリが残る IN は optimizer が join へ展開し損ねた形。値の並びが
        # 無く畳めないので、sqlglot の生成に任せる。
        return _IN_AS_SET(generator, node)
    return f"IN({generator.sql(node, 'this')}, {generator.expressions(node, flat=True)})"


PythonGenerator.TRANSFORMS[exp.In] = _in_py
PythonGenerator.TRANSFORMS[exp.Not] = lambda generator, node: f"NOT({generator.sql(node.this)})"

# sqlglot は ``x NOT LIKE y`` を Not(Like(...)) ではなく Like(negate=True) にパース
# する。Python 式への変換は引数を並べて関数呼び出しにするだけで negate を落とすため、
# そのままだと ``LIKE(x, y)`` になって否定が消える。negate が立っていれば NOT() で
# 包み直す。ILIKE も同じ形なので同じ差し替えを当てる(ILIKE の評価そのものは
# sqlglot の実行環境に関数が無く、否定の有無によらず未対応のまま)。
_LIKE_AS_CALL = PythonGenerator.TRANSFORMS[exp.Like]


def _like_py(generator: Generator, node: exp.Like | exp.ILike) -> str:
    call = _LIKE_AS_CALL(generator, node)
    return f"NOT({call})" if node_arg(node, "negate") else call


PythonGenerator.TRANSFORMS[exp.Like] = _like_py
PythonGenerator.TRANSFORMS[exp.ILike] = _like_py

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


def _under_not_in(node: exp.Expression, stop: exp.Expression) -> bool:
    """node が、stop の中の NOT IN サブクエリか、その内側のサブクエリか。

    NOT IN のサブクエリは rewrite_subqueries が単体で評価して値に畳むので、
    射影に置かれていてもスカラサブクエリとしては扱わない。
    """
    current: exp.Expression | None = node
    while current is not None and current is not stop:
        if _not_in_predicate(current) is not None:
            return True
        current = cast("exp.Expression | None", current.parent)
    return False


def _precheck(ast: exp.Expression) -> None:
    if ast.find(exp.Window):
        raise NotSupportedError("window functions are not supported")
    for select in ast.find_all(exp.Select):
        for projection in select.expressions:
            for sub in projection.find_all(exp.Select):
                if not _under_not_in(sub, projection):
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


def _subquery_rows(
    node: exp.Expression,
    tables: dict[str, Table],
    schema: dict[str, dict[str, str]],
) -> tuple[list[str], list[tuple[Value, ...]]]:
    """サブクエリを切り離して単体で評価する。"""
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
    return columns, rows


def _materialize(
    node: exp.Expression,
    name: str,
    columns: list[str],
    rows: list[tuple[Value, ...]],
    tables: dict[str, Table],
    schema: dict[str, dict[str, str]],
) -> None:
    """評価済みのサブクエリを、結果を持つ合成テーブルへの参照に置き換える。"""
    tables[name] = sqlglot_table(columns, rows)
    schema[name] = {
        column: _column_type([row[i] for row in rows]) for i, column in enumerate(columns)
    }
    node.replace(exp.select(exp.Star()).from_(exp.to_table(name)))


def _fold_not_in(
    in_node: exp.In,
    negation: exp.Not,
    columns: list[str],
    rows: list[tuple[Value, ...]],
) -> None:
    """NOT IN を、評価済みのサブクエリの値から三値論理どおりの式に畳む。

    ``x NOT IN (v...)`` は、v が空なら真、x か v のどれかが NULL なら NULL、
    x が v に含まれれば偽、どれでもなければ真になる。値が空のときだけ真に畳み、
    それ以外は値の並びをそのまま並べた ``NOT (x IN (v...))`` にする。NULL も
    リテラルとして並びに入れる。NULL の伝播は IN と NOT の評価
    (_sql_in / _sql_not)が受け持つので、畳んだ式は WHERE 以外の位置でも
    サブクエリのままの意味と一致する。

    値の並びをリテラルとして埋め込むのは、合成テーブルへの参照に置き換える
    書き方が sqlglot の unnest の判定(NOT IN を見送るかどうか)に寄りかかる
    ためである。
    """
    if len(columns) != 1:
        raise NotSupportedError(
            "NOT IN with a multi-column subquery is not supported; "
            "rewrite it as NOT EXISTS"
        )
    values = [row[0] for row in rows]
    if not values:
        negation.replace(exp.true())
        return
    literals = [exp.convert(value) for value in dict.fromkeys(values)]
    negation.replace(exp.Not(this=exp.In(this=in_node.this.copy(), expressions=literals)))


def _not_in_predicate(node: exp.Expression) -> tuple[exp.In, exp.Not] | None:
    """サブクエリ node を包む NOT IN 述語(In と、それを否定する Not)を返す。

    ``x NOT IN (...)`` は ``Not(In)`` になり、``NOT (x IN (...))`` は間に
    ``Paren`` が挟まる。sqlglot の unnest は In の親だけを見るので後者を
    素通しし、三値論理を保たない anti join へ書き換えてしまう。iceql は
    両方を同じ形として拾う。
    """
    in_node = node.parent
    if isinstance(in_node, exp.Subquery):
        in_node = in_node.parent
    if not isinstance(in_node, exp.In):
        return None
    negation = in_node.parent
    while isinstance(negation, exp.Paren):
        negation = negation.parent
    if not isinstance(negation, exp.Not):
        return None
    return in_node, negation


def _reject_correlated(scope: Scope, clause: str) -> None:
    outer = scope.external_columns[0].sql(dialect=SQL_DIALECT)
    raise NotSupportedError(
        f"{clause} in a correlated subquery is not supported "
        f"(the subquery references {outer} from the outer query)"
    )


class _DeferredRejection(Exception):
    """正規化の途中で決めた、あとで報告する拒否理由。

    そもそも書き換えられない相関 EXISTS(相関条件に等値が無い、OR がある、など)は
    _reject_unnestable_exists のメッセージのほうが原因を正しく指す。正規化の中で
    拒否を決めても、その場では投げずに持ち帰って、あの判定の後ろで投げる。
    """


def _conjuncts(node: exp.Expression) -> list[exp.Expression]:
    """AND と括弧だけでたどれる連言の要素を返す。

    ここで拾える位置なら、要素を取り除いても残りの意味は変わらない。OR や NOT の
    下にある述語は要素として返らない(node そのものが 1 要素として返る)。
    """
    stack = [node]
    parts: list[exp.Expression] = []
    while stack:
        current = stack.pop()
        if isinstance(current, exp.And):
            stack.extend([current.this, current.expression])
        elif isinstance(current, exp.Paren):
            stack.append(current.this)
        else:
            parts.append(current)
    return parts


def _push_out_outer_predicates(scope: Scope, exists: exp.Exists, where: exp.Where) -> bool:
    """外側の列だけで決まる述語を、サブクエリから EXISTS の外へ出す。

    ``EXISTS (SELECT ... WHERE P AND Q)`` は、P が外側の列だけで決まるなら
    ``P AND EXISTS (SELECT ... WHERE Q)`` と同じ値になる。押し出しておくと
    decorrelate が P を相関条件として扱わなくなり、否定を落とす経路にも
    BETWEEN や IN で書き換えをあきらめる経路にも入らない。

    P を ``COALESCE(P, FALSE)`` に包むのは、P が NULL の行で ``NOT EXISTS`` の
    結果が変わるためである。押し出す前は、P が真でない行はサブクエリに残らない
    ので EXISTS は偽、その否定は真になる。``P AND EXISTS (...)`` と素朴に書くと
    ``NULL AND TRUE`` が NULL になり、NOT を通しても NULL のままで行が落ちる。
    """
    external = {id(column) for column in scope.external_columns}
    pushed: list[exp.Expression] = []
    for conjunct in _conjuncts(where.this):
        columns = list(conjunct.find_all(exp.Column))
        if not columns or conjunct.find(exp.Select) is not None:
            # 列を含まない述語は動かす意味が無く、サブクエリを含む述語は
            # 動かすとスコープが変わる
            continue
        if all(id(column) in external for column in columns):
            pushed.append(conjunct.copy())
            conjunct.replace(exp.true())
    if not pushed:
        return False
    condition: exp.Condition = exists.copy()
    for predicate in pushed:
        condition = exp.and_(
            exp.Coalesce(this=exp.paren(predicate), expressions=[exp.false()]), condition
        )
    exists.replace(exp.paren(condition))
    return True


def _normalize_exists_subquery(scope: Scope, select: exp.Select) -> bool:
    """相関 EXISTS のサブクエリを、decorrelate が正しく扱える形に均す。

    decorrelate は書き換えをあきらめるだけでなく、書き換えたうえで元の意味と
    ずれた結果を返すことがある。ずれるのは次の 4 つで、いずれも黙った誤答になる。

    - GROUP BY を持つ: 相関条件の列に加えて GROUP BY の列でも集約するので
      結合キーが一意にならず、行の有無しか見ないはずの EXISTS で外側の行が増える
    - GROUP BY の無い集約: 集約は行が無くても 1 行返すので EXISTS は常に真だが、
      decorrelate は EXISTS の射影を捨てるため、行の有無で答えてしまう
    - ORDER BY を持つ: 射影を捨てられたあとも ORDER BY が残り、ソートキーの列を
      引けずに実行が落ちる(誤答ではないが、SQL からは追えないエラーになる)
    - 外側の列を含む述語が NOT の下にある: decorrelate はその述語を TRUE に
      差し替えて親側へ移すが、差し替えるのは述語のノードだけなので、外側の NOT が
      サブクエリの WHERE に残って ``NOT TRUE`` になり、条件全体が偽になる

    前の 3 つは、行の有無を変えずに GROUP BY と ORDER BY を外す(集約は真に畳む)。
    4 つ目は、述語が外側の列しか含まないなら EXISTS の外へ押し出せる。押し出せない
    形と、GROUP BY と HAVING を両方持つ形(HAVING は行の有無を変えるので GROUP BY を
    外せない)は拒否する。

    返り値は AST を書き換えたかどうか。押し出しは一つ外側のスコープの相関を増やす
    ので、呼び出し元がスコープを組み直して変化が無くなるまで繰り返す。
    """
    exists = cast("exp.Exists", select.parent)
    group = node_arg(select, "group")
    having = node_arg(select, "having")
    if (
        group is None
        and having is None
        and any(projection.find(exp.AggFunc) for projection in select.expressions)
    ):
        exists.replace(exp.true())
        return True
    changed = False
    if node_arg(select, "order") is not None:
        select.set("order", None)
        changed = True
    if group is not None:
        if having is not None:
            raise _DeferredRejection(
                "a correlated EXISTS whose subquery has GROUP BY ... HAVING is not "
                "supported (the join rewrite would duplicate outer rows): "
                f"EXISTS ({select.sql(dialect=SQL_DIALECT)}); "
                "rewrite it as a join against the grouped subquery, e.g. "
                "SELECT DISTINCT s.id FROM s JOIN (SELECT u.id FROM u GROUP BY u.id "
                "HAVING COUNT(*) > 1) g ON g.id = s.id"
            )
        # グループの数は行の有無を変えない。射影は decorrelate が捨てるが、
        # 集約が残ったままだと「GROUP BY の無い集約」に化けるので 1 に置き換える。
        select.set("group", None)
        select.set("expressions", [exp.Literal.number(1)])
        changed = True
    where = node_arg(select, "where")
    if not isinstance(where, exp.Where):
        return changed
    changed |= _push_out_outer_predicates(scope, exists, where)
    for column in scope.external_columns:
        if column.find_ancestor(exp.Where) is not where:
            continue  # 押し出したので、この列はもうサブクエリに無い
        if isinstance(column.find_ancestor(exp.Not, exp.Where), exp.Not):
            raise _DeferredRejection(
                "a correlated EXISTS with a negated predicate on "
                f"{column.sql(dialect=SQL_DIALECT)} is not supported "
                "(the join rewrite drops the negation): "
                f"EXISTS ({select.sql(dialect=SQL_DIALECT)}); "
                "write the predicate without NOT, e.g. u.k <> s.k instead of "
                "NOT (u.k = s.k)"
            )
    return changed


def _normalize_correlated_exists(ast: exp.Expression) -> str | None:
    """相関 EXISTS のサブクエリを、変化が無くなるまで均す。

    押し出しは一つ外側のスコープの相関を増やすので、スコープを組み直して繰り返す。
    1 度で済ませると、押し出し先がさらに相関サブクエリである形
    (``EXISTS (... WHERE u.id = s.id AND EXISTS (... WHERE v.id = u.id AND s.k IS NOT NULL))``)
    で同じ誤答が残る。

    返り値は、見つかった拒否理由(無ければ None)。呼び出し元が
    _reject_unnestable_exists の後ろで投げる。
    """
    if ast.find(exp.Exists) is None:
        return None
    rejection: str | None = None
    while True:
        root = build_scope(ast)
        if root is None:
            break
        changed = False
        for scope in root.traverse():
            select = scope.expression
            if not isinstance(select, exp.Select) or not isinstance(select.parent, exp.Exists):
                continue
            if not scope.external_columns:
                continue
            if node_arg(select, "limit") or node_arg(select, "offset"):
                continue  # 相関サブクエリの LIMIT / OFFSET は呼び出し元が拒否する
            try:
                changed |= _normalize_exists_subquery(scope, select)
            except _DeferredRejection as exc:
                if rejection is None:
                    rejection = str(exc)
        if not changed:
            break
    return rejection


def _reject_unnestable_exists(ast: exp.Expression) -> None:
    """sqlglot が join へ書き換えられない相関 EXISTS を、実行の前に拒否する。

    sqlglot の decorrelate は、相関条件が「サブクエリの WHERE に直に置かれた
    二項比較の連言で、少なくとも 1 つが等値」のときだけ join へ書き換える。
    等値が無い、OR がある、BETWEEN のような二項でない述語で外側の列を参照する、
    といった形は書き換えられず、EXISTS 式が AST に残る。sqlglot.executor は
    EXISTS 式を Python 式に落とせないため、そのまま渡すと生成コードが構文
    エラーになり、SQL からは追えないエラーだけが返る。

    書き換えられるかどうかの判定は、条件を iceql 側に書き写すのではなく、
    AST の写しに unnest_subqueries を掛けて EXISTS が残るかどうかで見る。
    条件を写すと、sqlglot の更新で今まで通っていた形を拒否しかねない。
    この関数は相関 EXISTS が残っているクエリでしか走らない(非相関 EXISTS は
    呼び出し元が真偽値へ畳んだあとである)ので、費用は AST 一往復で済む。
    """
    if ast.find(exp.Exists) is None:
        return
    try:
        leftover = unnest_subqueries(ast.copy()).find(exp.Exists)
    except SqlglotError:
        # 判定そのものが失敗したら、実行時に同じ書き換えが走って同じ形で落ちる
        return
    if leftover is None:
        return
    raise NotSupportedError(
        "this correlated EXISTS cannot be rewritten as a join and is not supported: "
        f"EXISTS ({leftover.this.sql(dialect=SQL_DIALECT)}); "
        "the correlation condition must be a conjunction of comparisons in the "
        "subquery's WHERE with at least one equality. Rewrite it as a comparison "
        "against an aggregate, e.g. SELECT s.id FROM s WHERE s.id < "
        "(SELECT MAX(u.id) FROM u), or as a join, e.g. SELECT DISTINCT s.id "
        "FROM s JOIN u ON u.id > s.id"
    )


def _exists_position_rejection(select: exp.Expression) -> str | None:
    """相関 EXISTS の置き場所が、decorrelate の書き換えと噛み合うかを見る。

    decorrelate はサブクエリを別名付きの LEFT JOIN として FROM の末尾に足し、
    EXISTS をその別名の列の IS NOT NULL に置き換える。置き換えた式は元の位置に
    残るので、別名を作る LEFT JOIN より先に評価される位置に EXISTS があると、
    まだ存在しない列を参照して実行が落ちる。落ちるのは次の 2 つ。

    - 先行する join の ON 句: その join は LEFT JOIN の前に評価される
    - HAVING: 集約は join のあとに走るが、別名の列は集約の出力に無い

    どちらも sqlglot.executor のステップ名と列名だけのエラーになり、SQL からは
    追えないので、実行の前に拒否する。WHERE に置いた同じ EXISTS は通る。

    返り値は拒否理由(問題ない置き場所なら None)。案内する書き換えは位置で
    変わるため、メッセージは位置ごとに作り分ける。
    """
    exists = cast("exp.Exists", select.parent)
    position = exists.find_ancestor(exp.Join, exp.Having, exp.Select)
    subquery = f"EXISTS ({select.sql(dialect=SQL_DIALECT)})"
    if isinstance(position, exp.Having):
        return (
            "a correlated EXISTS in HAVING is not supported "
            f"(the join rewrite adds it after the aggregation): {subquery}; "
            "move it to WHERE if it only references grouped columns, e.g. "
            "SELECT s.k FROM s WHERE EXISTS (SELECT 1 FROM u WHERE u.k = s.k) GROUP BY s.k"
        )
    if not isinstance(position, exp.Join):
        return None
    if node_arg(position, "side"):
        # 外側 join の ON 句の条件は WHERE に移すと意味が変わる(移すと NULL を
        # 埋めた行が落ちる)ので、サブクエリを FROM 句に出す形を案内する。
        return (
            "a correlated EXISTS in an outer join's ON clause is not supported "
            f"(the join rewrite adds it after this join): {subquery}; "
            "move the subquery into the FROM clause and join against it, e.g. "
            "SELECT s.id FROM s LEFT JOIN (SELECT v.id FROM v WHERE v.id IN "
            "(SELECT u.id FROM u)) v ON v.id = s.id"
        )
    return (
        "a correlated EXISTS in a join's ON clause is not supported "
        f"(the join rewrite adds it after this join): {subquery}; "
        "move it to WHERE, e.g. SELECT s.id FROM s JOIN v ON v.id = s.id "
        "WHERE EXISTS (SELECT 1 FROM u WHERE u.id = s.id)"
    )


def _needs_rewrite(ast: exp.Expression) -> bool:
    if ast.find(exp.Limit, exp.Offset, exp.Exists) is not None:
        return True
    return any(node_arg(node, "query") is not None for node in ast.find_all(exp.In))


def rewrite_subqueries(
    ast: exp.Expression,
    tables: dict[str, Table],
    schema: dict[str, dict[str, str]],
) -> exp.Expression:
    """sqlglot の optimizer が扱えないサブクエリを、先に評価して畳む。

    列の修飾子を見て相関を判定するため、先に qualify を通す。修飾子なしで
    外側を参照する形(``WHERE sid = id``)は qualify しないと拾えない。
    評価は内側から順に行う(内側の結果を外側の評価が使う)。
    """
    if not _needs_rewrite(ast):
        return ast
    try:
        ast = qualify(
            ast, schema=cast("dict[str, object] | None", schema or None), dialect=SQL_DIALECT
        )
    except OptimizeError as exc:
        raise ProgrammingError(f"invalid query: {exc}") from exc
    # 均すのは評価の対象を集める前。押し出しは EXISTS を作り直すので、
    # そのサブクエリの中にある評価の対象を先に集めると参照が外れる。
    exists_rejection = _normalize_correlated_exists(ast)
    position_rejection: str | None = None
    root = build_scope(ast)
    if root is None:
        raise NotSupportedError("subqueries in this position are not supported")
    targets: list[tuple[str, exp.Expression, tuple[exp.In, exp.Not] | None]] = []
    # traverse() は内側のスコープから順に返す
    for scope in root.traverse():
        node = scope.expression
        if not isinstance(node, (exp.Select, exp.SetOperation)) or node is ast:
            continue
        limited = bool(node_arg(node, "limit") or node_arg(node, "offset"))
        if isinstance(node.parent, exp.Exists):
            # 相関 EXISTS は sqlglot の decorrelate が join へ書き換える
            if scope.external_columns:
                if limited:
                    _reject_correlated(scope, "LIMIT / OFFSET")
                if position_rejection is None:
                    position_rejection = _exists_position_rejection(node)
                continue
            targets.append(("exists", node, None))
            continue
        not_in = _not_in_predicate(node)
        if not_in is not None:
            if scope.external_columns:
                raise NotSupportedError(
                    "NOT IN with a correlated subquery is not supported; "
                    "rewrite it as NOT EXISTS, e.g. "
                    "SELECT t.x FROM t WHERE NOT EXISTS "
                    "(SELECT 1 FROM u WHERE u.x = t.x AND u.k = t.k)"
                )
            targets.append(("not_in", node, not_in))
            continue
        if limited:
            if scope.external_columns:
                _reject_correlated(scope, "LIMIT / OFFSET")
            targets.append(("table", node, None))
    for index, (kind, node, not_in) in enumerate(targets):
        columns, rows = _subquery_rows(node, tables, schema)
        if kind == "exists":
            # EXISTS は行の有無で値が決まる。sqlglot の executor は EXISTS 式
            # そのものを評価できない(生成コードが構文エラーになる)ので畳む。
            cast("exp.Expression", node.parent).replace(exp.true() if rows else exp.false())
        elif kind == "not_in":
            _fold_not_in(*cast("tuple[exp.In, exp.Not]", not_in), columns, rows)
        else:
            _materialize(node, f"{_SYNTHETIC_PREFIX}{index}", columns, rows, tables, schema)
    if ast.find(exp.Limit, exp.Offset) is not None:
        raise NotSupportedError("LIMIT / OFFSET in this position is not supported")
    # 置き場所より先に、そもそも書き換えられるか・サブクエリの形が保てるかを
    # 報告する。書き換えられない EXISTS は WHERE に移しても通らないので、
    # 置き場所のメッセージは原因を取り違えさせる。
    _reject_unnestable_exists(ast)
    if exists_rejection is not None:
        raise NotSupportedError(exists_rejection)
    if position_rejection is not None:
        raise NotSupportedError(position_rejection)
    return ast


def run_select(catalog: Catalog, ast: exp.Expression) -> StatementResult:
    _precheck(ast)
    keys, hidden, limit, offset = _extract_order_limit(ast)
    tables, schema = load_tables(catalog, physical_tables(ast, catalog))
    ast = rewrite_subqueries(ast, tables, schema)
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
