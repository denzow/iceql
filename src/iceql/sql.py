"""SQL 方言の設定と、スキーマが保持する式(CHECK)の解析。

CHECK 式はスキーマ YAML に SQL 文字列のまま置き、読み込みのたびにここで
パースし直す。schema.py から使うため engine パッケージには置けない
(engine は catalog 経由で schema に依存する)。
"""

from __future__ import annotations

from collections.abc import Container

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from iceql.errors import DataError, NotSupportedError

SQL_DIALECT = "sqlite"


def parse_check_expression(expr: str) -> exp.Expression:
    """CHECK 式をパースする。式として成立しない文字列は DataError。"""
    try:
        parsed = [node for node in sqlglot.parse(expr, read=SQL_DIALECT) if node]
    except ParseError as exc:
        raise DataError(f"invalid CHECK expression {expr!r}: {exc}") from exc
    if len(parsed) != 1:
        raise DataError(f"invalid CHECK expression {expr!r}")
    node = parsed[0]
    assert isinstance(node, exp.Expression)
    if isinstance(node, (exp.Select, exp.Union, exp.Command)):
        raise NotSupportedError(f"CHECK must be an expression, not a statement: {expr!r}")
    return node


def validate_check_expression(
    node: exp.Expression, *, table: str, columns: Container[str]
) -> None:
    """CHECK 式が対象テーブルの列と定数だけで書かれていることを確かめる。

    sqlite と同じく、サブクエリ・集約・他テーブルの参照は許さない。
    行ごとに独立して判定できない式を許すと、変更行だけを検査する
    (dml.py) 方針が成り立たなくなる。
    """
    text = node.sql(dialect=SQL_DIALECT)
    if node.find(exp.Select, exp.Subquery):
        raise NotSupportedError(f"subqueries are not allowed in CHECK: {text}")
    if node.find(exp.AggFunc, exp.Window):
        raise NotSupportedError(f"aggregates are not allowed in CHECK: {text}")
    for column in node.find_all(exp.Column):
        qualifier = column.table
        if qualifier and qualifier != table:
            raise NotSupportedError(
                f"CHECK cannot reference another table: {column.sql(dialect=SQL_DIALECT)}"
            )
        if column.name not in columns:
            raise DataError(f"no such column: {table}.{column.name}")
