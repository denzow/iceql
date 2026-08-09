"""DB-API 2.0 (PEP 249) ライクな Connection / Cursor。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlglot import exp

from iceql import journal
from iceql.catalog import Catalog, init_database
from iceql.engine import StatementResult, execute_statement, parse_statement
from iceql.errors import InterfaceError, ProgrammingError
from iceql.staging import StagedCatalog
from iceql.storage import DatabaseLock
from iceql.types import Value

Params = Sequence[Any] | Mapping[str, Any]


def _to_literal(value: Any) -> exp.Expression:
    if value is None:
        return exp.Null()
    if isinstance(value, bool):
        return exp.Boolean(this=value)
    if isinstance(value, (int, float)):
        return exp.Literal.number(repr(value))
    if isinstance(value, str):
        return exp.Literal.string(value)
    if isinstance(value, (date, datetime)):
        sep: dict[str, Any] = {"sep": " "} if isinstance(value, datetime) else {}
        return exp.Literal.string(value.isoformat(**sep))
    raise ProgrammingError(f"unsupported parameter type: {type(value).__name__}")


def bind_parameters(ast: exp.Expression, params: Params | None) -> None:
    """AST 中のプレースホルダ(? / :name)をリテラルに置き換える。"""
    placeholders = list(ast.find_all(exp.Placeholder))
    positional = [ph for ph in placeholders if ph.this is None]
    named = [ph for ph in placeholders if ph.this is not None]

    if isinstance(params, Mapping):
        if positional:
            raise ProgrammingError("cannot bind positional placeholders with named parameters")
        for ph in named:
            if ph.this not in params:
                raise ProgrammingError(f"missing named parameter: {ph.this}")
            ph.replace(_to_literal(params[ph.this]))
        return

    values = list(params) if params is not None else []
    if named:
        raise ProgrammingError("cannot bind named placeholders with positional parameters")
    if len(values) != len(positional):
        raise ProgrammingError(
            f"expected {len(positional)} parameters, got {len(values)}"
        )
    for ph, value in zip(positional, values, strict=True):
        ph.replace(_to_literal(value))


def _batch_insert(ast: exp.Expression, seq_of_params: Sequence[Params]) -> exp.Insert | None:
    """INSERT ... VALUES を、全パラメータ分の行を持つ 1 文にまとめる。

    1 行ずつ実行すると、文ごとにテーブル全件の読み書きが起きるため、
    総コストが行数の二乗になる。まとめて 1 文にすれば書き出しは 1 回で済む。
    まとめられない文(INSERT 以外、INSERT ... SELECT)には None を返す。
    """
    if not isinstance(ast, exp.Insert) or not isinstance(ast.expression, exp.Values):
        return None
    tuples: list[exp.Expression] = []
    for params in seq_of_params:
        bound = ast.copy()
        bind_parameters(bound, params)
        tuples.extend(bound.expression.expressions)
    batched = ast.copy()
    batched.expression.set("expressions", tuples)
    return batched


class Cursor:
    arraysize = 1

    def __init__(self, connection: Connection) -> None:
        self.connection = connection
        self.description: list[tuple[Any, ...]] | None = None
        self.rowcount = -1
        # 直前の INSERT が最後に入れた行の主キー値。単一の integer 主キーを持つ
        # テーブルへの INSERT 以外では None(PEP 249 の任意属性)
        self.lastrowid: int | None = None
        self._rows: list[tuple[Value, ...]] = []
        self._pos = 0
        self._closed = False

    def _check_open(self) -> None:
        if self._closed:
            raise InterfaceError("cursor is closed")
        self.connection._check_open()

    def execute(self, sql: str, params: Params | None = None) -> Cursor:
        self._check_open()
        ast = parse_statement(sql)
        bind_parameters(ast, params)
        result = self.connection._execute_ast(ast)
        self._apply(result)
        return self

    def executemany(self, sql: str, seq_of_params: Sequence[Params]) -> Cursor:
        self._check_open()
        seq_of_params = list(seq_of_params)
        ast = parse_statement(sql)
        if not seq_of_params:
            self._apply(StatementResult(rowcount=0))
            return self
        batched = _batch_insert(ast, seq_of_params)
        if batched is not None:
            # 全行を 1 文にまとめたので、失敗したときは 1 行も入らない
            self._apply(self.connection._execute_ast(batched))
            return self
        total = 0
        for params in seq_of_params:
            self.execute(sql, params)
            if self.rowcount > 0:
                total += self.rowcount
        self.rowcount = total
        return self

    def _apply(self, result: StatementResult) -> None:
        if result.columns is None:
            self.description = None
        else:
            self.description = [
                (name, None, None, None, None, None, None) for name in result.columns
            ]
        self.rowcount = result.rowcount
        self.lastrowid = result.lastrowid
        self._rows = result.rows
        self._pos = 0

    def fetchone(self) -> tuple[Value, ...] | None:
        self._check_open()
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchmany(self, size: int | None = None) -> list[tuple[Value, ...]]:
        self._check_open()
        size = self.arraysize if size is None else size
        rows = self._rows[self._pos : self._pos + size]
        self._pos += len(rows)
        return rows

    def fetchall(self) -> list[tuple[Value, ...]]:
        self._check_open()
        rows = self._rows[self._pos :]
        self._pos = len(self._rows)
        return rows

    def __iter__(self) -> Cursor:
        return self

    def __next__(self) -> tuple[Value, ...]:
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row

    def close(self) -> None:
        self._closed = True
        self._rows = []


class Connection:
    def __init__(self, dbdir: str | Path, *, timeout: float = 5.0) -> None:
        self._catalog = init_database(dbdir)
        self._lock = DatabaseLock(self._catalog.root, timeout=timeout)
        self._staged: StagedCatalog | None = None
        self._closed = False
        journal.recover_if_needed(self._catalog, self._lock)

    def _check_open(self) -> None:
        if self._closed:
            raise InterfaceError("connection is closed")

    @property
    def in_transaction(self) -> bool:
        return self._staged is not None

    @property
    def _active_catalog(self) -> Catalog:
        """トランザクション中はステージ、それ以外はディスクのカタログ。

        トランザクション内の DDL は COMMIT までディスクに現れないため、
        テーブル一覧やスキーマの参照もこちらを通す。
        """
        return self._staged if self._staged is not None else self._catalog

    def _execute_ast(self, ast: exp.Expression) -> StatementResult:
        # 他プロセスのクラッシュで残ったコミットジャーナルがあれば先に再適用する
        journal.recover_if_needed(self._catalog, self._lock)
        if isinstance(ast, exp.Transaction):
            self._begin()
            return StatementResult()
        if isinstance(ast, exp.Commit):
            if not self.in_transaction:
                raise ProgrammingError("cannot COMMIT: no transaction is active")
            self.commit()
            return StatementResult()
        if isinstance(ast, exp.Rollback):
            if not self.in_transaction:
                raise ProgrammingError("cannot ROLLBACK: no transaction is active")
            self.rollback()
            return StatementResult()
        return execute_statement(self._active_catalog, self._lock, ast)

    def _begin(self) -> None:
        if self.in_transaction:
            raise ProgrammingError("a transaction is already active")
        # 他の書き手が COMMIT するまでここで待機する(timeout 超過で OperationalError)
        self._lock.begin()
        self._staged = StagedCatalog(self._catalog)

    def cursor(self) -> Cursor:
        self._check_open()
        return Cursor(self)

    def execute(self, sql: str, params: Params | None = None) -> Cursor:
        return self.cursor().execute(sql, params)

    def executemany(self, sql: str, seq_of_params: Sequence[Params]) -> Cursor:
        return self.cursor().executemany(sql, seq_of_params)

    def commit(self) -> None:
        """トランザクション中ならステージ済みの変更を書き出す。それ以外は何もしない。"""
        self._check_open()
        if self._staged is not None:
            try:
                self._staged.flush(self._lock)
            finally:
                self._staged = None
                self._lock.end()

    def rollback(self) -> None:
        """トランザクション中ならステージ済みの変更を破棄する。それ以外は何もしない。"""
        self._check_open()
        self._staged = None
        self._lock.end()

    def close(self) -> None:
        # 未コミットの変更は破棄される(ROLLBACK と同じ)
        self._staged = None
        self._lock.end()
        self._closed = True

    def __enter__(self) -> Connection:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def connect(dbdir: str | Path, *, timeout: float = 5.0) -> Connection:
    """DB ディレクトリに接続する。存在しなければ作成する(sqlite3 と同様)。

    timeout は他の書き込みトランザクションのロック解放を待つ秒数
    (sqlite3 の timeout 相当。超過すると OperationalError)。
    """
    return Connection(dbdir, timeout=timeout)
