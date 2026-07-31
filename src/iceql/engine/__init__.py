"""SQL 文のパースと AST 型による実行ディスパッチ。"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from iceql.catalog import Catalog
from iceql.errors import NotSupportedError, ProgrammingError
from iceql.storage import DatabaseLock
from iceql.types import Value

SQL_DIALECT = "sqlite"


@dataclass
class StatementResult:
    """1 文の実行結果。SELECT 以外は columns=None。"""

    columns: list[str] | None = None
    rows: list[tuple[Value, ...]] = field(default_factory=list)
    rowcount: int = -1


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
