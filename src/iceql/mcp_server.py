"""MCP サーバー: LLM エージェントから iceql の DB を直接読み書きする。

`mcp` SDK は optional dependency(`iceql[mcp]`)。
ツール本体はプレーンな関数に分離し、FastMCP への登録は薄く保つ。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlglot import exp

import iceql
from iceql.engine import parse_statement
from iceql.errors import ProgrammingError

_READ_ONLY_TYPES = (exp.Select, exp.Union, exp.Except, exp.Intersect)


def query_tool(conn: iceql.Connection, sql: str) -> dict[str, Any]:
    """SELECT 系の文だけを許可して実行する。"""
    ast = parse_statement(sql)
    if not isinstance(ast, _READ_ONLY_TYPES):
        raise ProgrammingError("query tool accepts SELECT statements only; use execute")
    cur = conn.execute(sql)
    assert cur.description is not None
    return {
        "columns": [d[0] for d in cur.description],
        "rows": [list(row) for row in cur.fetchall()],
    }


def execute_tool(conn: iceql.Connection, sql: str) -> dict[str, Any]:
    """DML / DDL を実行する。"""
    cur = conn.execute(sql)
    return {"rowcount": cur.rowcount}


def list_tables_tool(conn: iceql.Connection) -> list[str]:
    return conn._catalog.list_tables()


def describe_table_tool(conn: iceql.Connection, table: str) -> str:
    """テーブルのスキーマ(YAML)を返す。"""
    conn._catalog.load_schema(table)  # 存在チェック
    return conn._catalog.schema_path(table).read_text(encoding="utf-8")


def create_server(dbdir: str | Path, *, read_only: bool = False) -> Any:
    try:
        try:
            from mcp.server import MCPServer as _ServerClass
        except ImportError:  # 旧バージョンの SDK
            from mcp.server.fastmcp import FastMCP as _ServerClass  # type: ignore[no-redef]
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "the MCP server requires the 'mcp' package; install with: pip install 'iceql[mcp]'"
        ) from exc

    conn = iceql.connect(dbdir)
    server = _ServerClass("iceql")

    @server.tool()
    def query(sql: str) -> dict[str, Any]:
        """Run a read-only SQL query (SELECT) and return columns and rows."""
        return query_tool(conn, sql)

    @server.tool()
    def list_tables() -> list[str]:
        """List all table names in the database."""
        return list_tables_tool(conn)

    @server.tool()
    def describe_table(table: str) -> str:
        """Return the schema (YAML) of a table."""
        return describe_table_tool(conn, table)

    if not read_only:

        @server.tool()
        def execute(sql: str) -> dict[str, Any]:
            """Execute a DML/DDL statement (INSERT/UPDATE/DELETE/CREATE/...)."""
            return execute_tool(conn, sql)

    return server
