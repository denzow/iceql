"""iceql CLI: REPL・ワンショット実行・check・init。"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path

import click
import sqlglot
from sqlglot.errors import ParseError

import iceql
from iceql.catalog import init_database
from iceql.engine import SQL_DIALECT, StatementResult
from iceql.errors import Error, ProgrammingError
from iceql.types import NULL_MARKER, Value

FORMATS = ("table", "csv", "json")


def _format_value(value: Value, *, null: str) -> str:
    if value is None:
        return null
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value)
    return str(value)


def _print_table(result: StatementResult) -> None:
    assert result.columns is not None
    header = list(result.columns)
    body = [[_format_value(v, null="") for v in row] for row in result.rows]
    widths = [len(h) for h in header]
    for row in body:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], max(len(line) for line in cell.splitlines() or [""]))

    def line(cells: list[str]) -> str:
        return " | ".join(cell.ljust(w) for cell, w in zip(cells, widths, strict=True)).rstrip()

    click.echo(line(header))
    click.echo("-+-".join("-" * w for w in widths))
    for row in body:
        click.echo(line(row))


def _print_csv(result: StatementResult) -> None:
    from iceql.storage import _format_record

    assert result.columns is not None
    out = [_format_record(list(result.columns))]
    for row in result.rows:
        out.append(_format_record([_format_value(v, null=NULL_MARKER) for v in row]))
    click.echo("".join(out), nl=False)


def _print_json(result: StatementResult) -> None:
    assert result.columns is not None
    for row in result.rows:
        obj = dict(zip(result.columns, row, strict=True))
        click.echo(json.dumps(obj, ensure_ascii=False))


def _print_result(result: StatementResult, fmt: str, *, feedback: bool) -> None:
    if result.columns is None:
        if feedback and result.rowcount >= 0:
            click.echo(f"({result.rowcount} rows affected)", err=True)
        return
    if fmt == "table":
        _print_table(result)
    elif fmt == "csv":
        _print_csv(result)
    else:
        _print_json(result)


def _run_script(conn: iceql.Connection, sql: str, fmt: str, *, feedback: bool) -> None:
    """複数文を含みうる SQL 文字列を順に実行して結果を出力する。"""
    try:
        statements = [s for s in sqlglot.parse(sql, read=SQL_DIALECT) if s is not None]
    except ParseError as exc:
        raise ProgrammingError(f"SQL syntax error: {exc}") from exc
    for statement in statements:
        assert isinstance(statement, sqlglot.exp.Expression)
        result = conn._execute_ast(statement)
        _print_result(result, fmt, feedback=feedback)


class DefaultToShellGroup(click.Group):
    """`iceql <dbdir>` をサブコマンド無しで shell として扱う。"""

    def resolve_command(
        self, ctx: click.Context, args: list[str]
    ) -> tuple[str | None, click.Command | None, list[str]]:
        try:
            return super().resolve_command(ctx, args)
        except click.UsageError:
            return "shell", self.commands["shell"], args


@click.group(cls=DefaultToShellGroup, invoke_without_command=False)
@click.version_option(iceql.__version__)
def main() -> None:
    """iceql: a local RDBMS with plaintext (CSV + YAML) storage."""


@main.command()
@click.argument("dbdir", type=click.Path(path_type=Path))
@click.option("-c", "--command", "commands", multiple=True, help="SQL を実行して終了する")
@click.option(
    "-f",
    "--format",
    "fmt",
    type=click.Choice(FORMATS),
    default=None,
    help="出力形式(既定: TTY なら table、パイプなら csv)",
)
def shell(dbdir: Path, commands: tuple[str, ...], fmt: str | None) -> None:
    """DB ディレクトリに接続して REPL を開くか、-c の SQL を実行する。"""
    if fmt is None:
        fmt = "table" if sys.stdout.isatty() else "csv"
    conn = iceql.connect(dbdir)
    try:
        if commands:
            for sql in commands:
                try:
                    _run_script(conn, sql, fmt, feedback=False)
                except Error as exc:
                    raise click.ClickException(str(exc)) from exc
        else:
            _repl(conn, fmt)
    finally:
        conn.close()


def _repl(conn: iceql.Connection, fmt: str) -> None:
    try:
        import readline

        histfile: Path | None = Path.home() / ".iceql_history"
        assert histfile is not None
        with contextlib.suppress(OSError):
            readline.read_history_file(histfile)
    except ImportError:  # readline が無い環境でも REPL 自体は動かす
        readline = None  # type: ignore[assignment]
        histfile = None
    click.echo(f"iceql {iceql.__version__} — connected to {conn._catalog.root}")
    click.echo('SQL 文は ";" で終端。".help" でメタコマンド一覧、".quit" で終了。')
    buffer = ""
    while True:
        prompt = "iceql> " if not buffer else "  ...> "
        try:
            line = input(prompt)
        except EOFError:
            click.echo("")
            break
        except KeyboardInterrupt:
            buffer = ""
            click.echo("")
            continue
        if not buffer and line.strip().startswith("."):
            new_fmt = _run_meta(conn, line.strip(), fmt)
            if new_fmt is None:
                break
            fmt = new_fmt
            continue
        buffer += line + "\n"
        if not buffer.strip():
            buffer = ""
            continue
        if not buffer.rstrip().endswith(";"):
            continue
        sql, buffer = buffer, ""
        try:
            _run_script(conn, sql, fmt, feedback=True)
        except Error as exc:
            click.echo(f"error: {exc}", err=True)
    if readline is not None and histfile is not None:
        with contextlib.suppress(OSError):
            readline.write_history_file(histfile)


def _run_meta(conn: iceql.Connection, line: str, fmt: str) -> str | None:
    """メタコマンドを処理する。戻り値は新しい出力形式(終了時は None)。"""
    parts = line.split()
    cmd, args = parts[0], parts[1:]
    if cmd in (".quit", ".exit"):
        return None
    if cmd == ".help":
        click.echo(
            ".tables          テーブル一覧\n"
            ".schema <table>  スキーマ(YAML)を表示\n"
            ".format <fmt>    出力形式を変更 (table/csv/json)\n"
            ".quit            終了"
        )
    elif cmd == ".tables":
        for name in conn._catalog.list_tables():
            click.echo(name)
    elif cmd == ".schema":
        if not args:
            click.echo("usage: .schema <table>", err=True)
        else:
            try:
                path = conn._catalog.schema_path(args[0])
                click.echo(path.read_text(encoding="utf-8"), nl=False)
            except (Error, OSError) as exc:
                click.echo(f"error: {exc}", err=True)
    elif cmd == ".format":
        if args and args[0] in FORMATS:
            fmt = args[0]
            click.echo(f"format: {fmt}", err=True)
        else:
            click.echo(f"usage: .format {{{'|'.join(FORMATS)}}}", err=True)
    else:
        click.echo(f"unknown meta command: {cmd} (try .help)", err=True)
    return fmt


@main.command()
@click.argument("dbdir", type=click.Path(path_type=Path))
def check(dbdir: Path) -> None:
    """スキーマと CSV の整合性を検証する。問題があれば非ゼロで終了する。"""
    from iceql.check import check_database

    issues = check_database(dbdir)
    for issue in issues:
        click.echo(str(issue))
    errors = [i for i in issues if i.severity == "error"]
    if errors:
        raise SystemExit(1)
    click.echo("ok" if not issues else f"ok ({len(issues)} warnings)")


@main.command()
@click.argument("dbdir", type=click.Path(path_type=Path))
def init(dbdir: Path) -> None:
    """空の DB ディレクトリを作成する。"""
    catalog = init_database(dbdir)
    click.echo(f"initialized empty database at {catalog.root}")


@main.command()
@click.argument("dbdir", type=click.Path(path_type=Path))
@click.option("--read-only", is_flag=True, help="書き込み系ツール(execute)を無効にする")
def mcp(dbdir: Path, read_only: bool) -> None:
    """MCP サーバーとして起動する(stdio)。要 iceql[mcp]。"""
    from iceql.mcp_server import create_server

    try:
        server = create_server(dbdir, read_only=read_only)
    except ImportError as exc:
        raise click.ClickException(str(exc)) from exc
    server.run()
