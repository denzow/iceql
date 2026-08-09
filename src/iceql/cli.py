"""iceql CLI: REPL・ワンショット実行・check・init。"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path
from types import ModuleType

import click
import sqlglot
from sqlglot.errors import ParseError

import iceql
from iceql import importer
from iceql.catalog import init_database
from iceql.engine import SQL_DIALECT, StatementResult
from iceql.errors import Error, ProgrammingError
from iceql.schema import dump_schema
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


def _print_csv(result: StatementResult, *, null_marker: str = NULL_MARKER) -> None:
    from iceql.storage import _format_record

    assert result.columns is not None
    out = [_format_record(list(result.columns))]
    for row in result.rows:
        out.append(_format_record([_format_value(v, null=null_marker) for v in row]))
    click.echo("".join(out), nl=False)


def _print_json(result: StatementResult) -> None:
    assert result.columns is not None
    for row in result.rows:
        obj = dict(zip(result.columns, row, strict=True))
        click.echo(json.dumps(obj, ensure_ascii=False))


def _print_expanded(result: StatementResult) -> None:
    """psql の \\x に相当する縦持ち表示。"""
    assert result.columns is not None
    width = max((len(c) for c in result.columns), default=0)
    for i, row in enumerate(result.rows, start=1):
        click.echo(f"-[ RECORD {i} ]-")
        for col, value in zip(result.columns, row, strict=True):
            click.echo(f"{col.ljust(width)} | {_format_value(value, null='')}")


def _print_result(
    result: StatementResult,
    fmt: str,
    *,
    feedback: bool,
    expanded: bool = False,
    null_marker: str = NULL_MARKER,
) -> None:
    if result.columns is None:
        if feedback and result.rowcount >= 0:
            click.echo(f"({result.rowcount} rows affected)", err=True)
        return
    if expanded:
        _print_expanded(result)
    elif fmt == "table":
        _print_table(result)
    elif fmt == "csv":
        _print_csv(result, null_marker=null_marker)
    else:
        _print_json(result)


def _run_script(
    conn: iceql.Connection,
    sql: str,
    fmt: str,
    *,
    feedback: bool,
    expanded: bool = False,
    null_marker: str = NULL_MARKER,
) -> None:
    """複数文を含みうる SQL 文字列を順に実行して結果を出力する。"""
    try:
        statements = [s for s in sqlglot.parse(sql, read=SQL_DIALECT) if s is not None]
    except ParseError as exc:
        raise ProgrammingError(f"SQL syntax error: {exc}") from exc
    for statement in statements:
        assert isinstance(statement, sqlglot.exp.Expression)
        result = conn._execute_ast(statement)
        _print_result(result, fmt, feedback=feedback, expanded=expanded, null_marker=null_marker)


class DefaultToShellGroup(click.Group):
    """`iceql <dbdir>` をサブコマンド無しで shell として扱う。"""

    def resolve_command(
        self, ctx: click.Context, args: list[str]
    ) -> tuple[str | None, click.Command | None, list[str]]:
        try:
            return super().resolve_command(ctx, args)
        except click.UsageError:
            return "shell", self.commands["shell"], args


_FORMAT_OPTION_HELP = "Output format (default: table on a TTY, csv when piped)"
_NULL_MARKER_OPTION_HELP = (
    "Value printed for NULL in csv output. With '' a NULL and an empty string "
    "look the same in the output."
)


@click.group(cls=DefaultToShellGroup, invoke_without_command=False)
@click.version_option(iceql.__version__)
def main() -> None:
    """iceql: a local RDBMS with plaintext (CSV + YAML) storage.

    Running `iceql DBDIR` opens an interactive REPL; add -c to run SQL and exit.
    """


@main.command()
@click.argument("dbdir", type=click.Path(path_type=Path))
@click.option("-c", "--command", "commands", multiple=True, help="Run SQL and exit (repeatable)")
@click.option(
    "-f",
    "--format",
    "fmt",
    type=click.Choice(FORMATS),
    default=None,
    help=_FORMAT_OPTION_HELP,
)
@click.option(
    "--null-marker",
    default=NULL_MARKER,
    show_default=True,
    help=_NULL_MARKER_OPTION_HELP,
)
def shell(dbdir: Path, commands: tuple[str, ...], fmt: str | None, null_marker: str) -> None:
    """Connect to DBDIR and open a REPL, or run SQL given with -c.

    This is the default command: `iceql DBDIR` is equivalent to `iceql shell DBDIR`.
    """
    if fmt is None:
        fmt = "table" if sys.stdout.isatty() else "csv"
    conn = iceql.connect(dbdir)
    try:
        if commands:
            for sql in commands:
                try:
                    _run_script(conn, sql, fmt, feedback=False, null_marker=null_marker)
                except Error as exc:
                    raise click.ClickException(str(exc)) from exc
        else:
            _repl(conn, fmt, null_marker=null_marker)
    finally:
        conn.close()


@main.command()
@click.argument("dbdir", type=click.Path(path_type=Path))
@click.option(
    "-f",
    "--format",
    "fmt",
    type=click.Choice(FORMATS),
    default="table",
    show_default=True,
    help="Output format",
)
@click.option(
    "--null-marker",
    default=NULL_MARKER,
    show_default=True,
    help=_NULL_MARKER_OPTION_HELP,
)
def repl(dbdir: Path, fmt: str, null_marker: str) -> None:
    """Connect to DBDIR and open an interactive REPL."""
    conn = iceql.connect(dbdir)
    try:
        _repl(conn, fmt, null_marker=null_marker)
    finally:
        conn.close()


class _ReplState:
    def __init__(self, fmt: str, null_marker: str) -> None:
        self.fmt = fmt
        self.null_marker = null_marker
        self.expanded = False
        self.quit = False


def _repl(conn: iceql.Connection, fmt: str, *, null_marker: str = NULL_MARKER) -> None:
    # 履歴は対話利用(TTY)のときだけ扱う。パイプやテストで readline の
    # プロセス内履歴に read_history_file が「追記」される仕様のまま書き戻すと、
    # 履歴ファイルが読み書きのたびに倍々で膨らんでしまう
    histfile: Path | None = None
    readline: ModuleType | None = None
    if sys.stdin.isatty():
        try:
            import readline as _readline

            _readline.clear_history()
            _readline.set_history_length(1000)
            histfile = Path.home() / ".iceql_history"
            with contextlib.suppress(OSError):
                _readline.read_history_file(histfile)
            readline = _readline
        except ImportError:  # readline が無い環境でも REPL 自体は動かす
            histfile = None
    click.echo(f"iceql ({iceql.__version__})")
    click.echo('Type "\\?" for help.')
    state = _ReplState(fmt, null_marker)
    dbname = conn._catalog.root.name
    buffer = ""
    while not state.quit:
        prompt = f"{dbname}=# " if not buffer else f"{dbname}-# "
        try:
            line = input(prompt)
        except EOFError:
            click.echo("")
            break
        except KeyboardInterrupt:
            buffer = ""
            click.echo("")
            continue
        stripped = line.strip()
        if not buffer:
            if stripped.startswith("\\"):
                _run_backslash(conn, stripped, state)
                continue
            if stripped in ("exit", "quit"):
                break
            if stripped == "help":
                _print_backslash_help()
                continue
        buffer += line + "\n"
        if not buffer.strip():
            buffer = ""
            continue
        if not buffer.rstrip().endswith(";"):
            continue
        sql, buffer = buffer, ""
        try:
            _run_script(
                conn,
                sql,
                state.fmt,
                feedback=True,
                expanded=state.expanded,
                null_marker=state.null_marker,
            )
        except Error as exc:
            click.echo(f"error: {exc}", err=True)
    if readline is not None and histfile is not None:
        with contextlib.suppress(OSError):
            readline.write_history_file(histfile)


def _print_backslash_help() -> None:
    click.echo(
        "General\n"
        "  \\q                   quit iceql\n"
        "Informational\n"
        "  \\d [TABLE]           list tables, or describe a table (YAML schema)\n"
        "  \\dt                  list tables\n"
        "Formatting\n"
        "  \\x                   toggle expanded output\n"
        "  \\pset format FORMAT  set output format (table/csv/json)"
    )


def _list_tables(conn: iceql.Connection) -> None:
    tables = conn._active_catalog.list_tables()
    if not tables:
        click.echo("No tables found.")
        return
    click.echo("List of tables")
    for name in tables:
        click.echo(f"  {name}")


def _run_backslash(conn: iceql.Connection, line: str, state: _ReplState) -> None:
    """psql 風のバックスラッシュコマンドを処理する。"""
    parts = line.split()
    cmd, args = parts[0], parts[1:]
    if cmd == "\\q":
        state.quit = True
    elif cmd == "\\?":
        _print_backslash_help()
    elif cmd in ("\\d", "\\dt"):
        if cmd == "\\d" and args:
            try:
                schema = conn._active_catalog.load_schema(args[0])
            except (Error, OSError):
                click.echo(f'Did not find any table named "{args[0]}".', err=True)
            else:
                click.echo(dump_schema(schema), nl=False)
        else:
            _list_tables(conn)
    elif cmd == "\\x":
        state.expanded = not state.expanded
        click.echo(f"Expanded display is {'on' if state.expanded else 'off'}.")
    elif cmd == "\\pset":
        if len(args) == 2 and args[0] == "format" and args[1] in FORMATS:
            state.fmt = args[1]
            click.echo(f"Output format is {state.fmt}.")
        else:
            click.echo(f"\\pset: usage: \\pset format {{{'|'.join(FORMATS)}}}", err=True)
    else:
        click.echo(f"invalid command {cmd}. Try \\? for help.", err=True)


@main.command()
@click.argument("dbdir", type=click.Path(path_type=Path))
def check(dbdir: Path) -> None:
    """Validate schema/CSV consistency. Exits non-zero if errors are found."""
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
    """Create an empty database directory."""
    catalog = init_database(dbdir)
    click.echo(f"initialized empty database at {catalog.root}")


@main.command("import")
@click.argument("dbdir", type=click.Path(path_type=Path))
@click.argument("csvfile", type=click.Path(path_type=Path, allow_dash=True))
@click.option("--table", default=None, help="Table name (default: the CSV file name)")
@click.option(
    "--types",
    multiple=True,
    metavar="COL=TYPE",
    help="Override the inferred type of a column (repeatable)",
)
@click.option(
    "--null-marker",
    default=NULL_MARKER,
    show_default=True,
    help="Field value read as NULL",
)
@click.option(
    "--encoding",
    default=importer.DEFAULT_ENCODING,
    show_default=True,
    help="Encoding of the input CSV",
)
@click.option("--dry-run", is_flag=True, help="Show the inferred schema without writing")
def import_(
    dbdir: Path,
    csvfile: Path,
    table: str | None,
    types: tuple[str, ...],
    null_marker: str,
    encoding: str,
    dry_run: bool,
) -> None:
    """Import CSVFILE into DBDIR as a new table.

    Column names come from the header row and types are inferred from every row.
    Pass - as CSVFILE to read from standard input.
    """
    try:
        plan = importer.build_plan(
            csvfile,
            table=table,
            types=importer.parse_type_overrides(types),
            null_marker=null_marker,
            encoding=encoding,
        )
    except Error as exc:
        raise click.ClickException(str(exc)) from exc
    # 推論結果は書き込みの前に見せる
    click.echo(importer.format_plan(plan), nl=False, err=True)
    if dry_run:
        click.echo("dry run: no changes written", err=True)
        return
    try:
        importer.apply_plan(dbdir, plan)
    except Error as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"imported {len(plan.rows)} rows into {plan.table}", err=True)


@main.command()
@click.argument("dbdir", type=click.Path(path_type=Path))
@click.option("--read-only", is_flag=True, help="Disable write tools (execute)")
def mcp(dbdir: Path, read_only: bool) -> None:
    """Run as an MCP server (stdio). Requires iceql[mcp]."""
    from iceql.mcp_server import create_server

    try:
        server = create_server(dbdir, read_only=read_only)
    except ImportError as exc:
        raise click.ClickException(str(exc)) from exc
    server.run()
