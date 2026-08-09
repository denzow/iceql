"""SQL 文のパースと AST 型による実行ディスパッチ。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from iceql.catalog import Catalog
from iceql.errors import InternalError, NotSupportedError, ProgrammingError
from iceql.sql import SQL_DIALECT
from iceql.storage import DatabaseLock
from iceql.types import Value

__all__ = ["SQL_DIALECT", "StatementResult", "execute_statement", "node_arg", "parse_statement"]


def node_arg(node: exp.Expression, *names: str) -> Any:
    """sqlglot ノードの引数を、キー名を検証したうえで取り出す。

    sqlglot は予約語と衝突する引数名の末尾にアンダースコアを付ける
    (UPDATE の FROM 句は ``from_``)。綴りを外しても ``args.get`` は None を
    返すだけなので、句を未対応として弾いているつもりが黙って無視される。
    キー名はバージョンで変わりうるため、候補を複数受け取ったうえで、
    どれもそのノードの arg_types に無ければ iceql 側の不整合として落とす。
    """
    known = [name for name in names if name in type(node).arg_types]
    if not known:
        raise InternalError(
            f"{type(node).__name__} has no argument "
            f"{' / '.join(repr(name) for name in names)} in this sqlglot version"
        )
    for name in known:
        value = node.args.get(name)
        if value is not None:
            return value
    return None


@dataclass
class StatementResult:
    """1 文の実行結果。SELECT 以外は columns=None。"""

    columns: list[str] | None = None
    rows: list[tuple[Value, ...]] = field(default_factory=list)
    rowcount: int = -1
    lastrowid: int | None = None


def parse_statement(sql: str) -> exp.Expression:
    try:
        statements = [s for s in sqlglot.parse(sql, read=SQL_DIALECT) if s is not None]
    except ParseError as exc:
        raise ProgrammingError(f"SQL syntax error: {exc}") from exc
    if not statements:
        raise ProgrammingError("empty statement")
    if len(statements) > 1:
        raise ProgrammingError(
            "only one statement can be executed at a time "
            f"(got {len(statements)})"
        )
    statement = statements[0]
    assert isinstance(statement, exp.Expression)
    return statement


def execute_statement(
    catalog: Catalog, lock: DatabaseLock, ast: exp.Expression
) -> StatementResult:
    from iceql.engine import ddl, dml, executor

    if isinstance(ast, (exp.Select, exp.Union, exp.Except, exp.Intersect)):
        with lock.read():
            return executor.run_select(catalog, ast)
    if isinstance(ast, exp.Insert):
        with lock.write_statement():
            return dml.run_insert(catalog, ast)
    if isinstance(ast, exp.Update):
        with lock.write_statement():
            return dml.run_update(catalog, ast)
    if isinstance(ast, exp.Delete):
        with lock.write_statement():
            return dml.run_delete(catalog, ast)
    if isinstance(ast, exp.Create):
        with lock.write_statement():
            return ddl.run_create(catalog, ast)
    if isinstance(ast, exp.Drop):
        with lock.write_statement():
            return ddl.run_drop(catalog, ast)
    if isinstance(ast, exp.Alter):
        with lock.write_statement():
            return ddl.run_alter(catalog, ast)
    if isinstance(ast, (exp.Transaction, exp.Commit, exp.Rollback)):
        raise NotSupportedError("transactions are not supported yet")
    raise NotSupportedError(f"unsupported statement: {type(ast).__name__}")
