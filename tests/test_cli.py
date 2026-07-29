import pytest
from click.testing import CliRunner

from iceql.cli import main


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def dbdir(tmp_path):
    return str(tmp_path / "db")


def run_ok(runner, args):
    result = runner.invoke(main, args, catch_exceptions=False)
    assert result.exit_code == 0, result.output
    return result


class TestOneShot:
    def test_create_insert_select_csv(self, runner, dbdir):
        run_ok(
            runner,
            [
                dbdir,
                "-c",
                "CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)",
                "-c",
                "INSERT INTO t VALUES (1, 'alice'), (2, NULL)",
            ],
        )
        result = run_ok(runner, [dbdir, "-c", "SELECT * FROM t ORDER BY id", "-f", "csv"])
        assert result.output == "id,name\n1,alice\n2,\\N\n"

    def test_table_format(self, runner, dbdir):
        run_ok(runner, [dbdir, "-c", "CREATE TABLE t (id INTEGER, name TEXT)"])
        run_ok(runner, [dbdir, "-c", "INSERT INTO t VALUES (1, 'alice')"])
        result = run_ok(runner, [dbdir, "-c", "SELECT * FROM t", "-f", "table"])
        lines = result.output.splitlines()
        assert lines[0] == "id | name"
        assert lines[2] == "1  | alice"

    def test_json_format(self, runner, dbdir):
        run_ok(runner, [dbdir, "-c", "CREATE TABLE t (id INTEGER, ok BOOLEAN)"])
        run_ok(runner, [dbdir, "-c", "INSERT INTO t VALUES (1, TRUE)"])
        result = run_ok(runner, [dbdir, "-c", "SELECT * FROM t", "-f", "json"])
        assert result.output == '{"id": 1, "ok": true}\n'

    def test_multiple_statements_in_one_command(self, runner, dbdir):
        result = run_ok(
            runner,
            [dbdir, "-c", "CREATE TABLE t (id INTEGER); INSERT INTO t VALUES (1); "
             "SELECT * FROM t", "-f", "csv"],
        )
        assert result.output.endswith("id\n1\n")

    def test_sql_error_exit_code(self, runner, dbdir):
        result = runner.invoke(main, [dbdir, "-c", "SELECT * FROM missing"])
        assert result.exit_code != 0
        assert "no such table" in result.output

    def test_explicit_shell_subcommand(self, runner, dbdir):
        result = run_ok(runner, ["shell", dbdir, "-c", "SELECT 1 AS one", "-f", "csv"])
        assert result.output == "one\n1\n"


class TestRepl:
    def test_repl_basic(self, runner, dbdir):
        result = run_ok(
            runner,
            [dbdir],
            )
        # CliRunner では stdin が空なので EOF で即終了する
        assert "iceql" in result.output

    def test_repl_executes_and_meta(self, runner, dbdir):
        stdin = (
            "CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT);\n"
            "INSERT INTO t VALUES (1, 'alice');\n"
            "\\d\n"
            "SELECT name FROM t;\n"
            "\\q\n"
        )
        result = runner.invoke(main, [dbdir, "-f", "csv"], input=stdin)
        assert result.exit_code == 0, result.output
        assert "List of tables\n  t\n" in result.output
        assert "name\nalice\n" in result.output

    def test_repl_describe_table(self, runner, dbdir):
        stdin = "CREATE TABLE t (id INTEGER PRIMARY KEY);\n\\d t\n\\q\n"
        result = runner.invoke(main, [dbdir, "-f", "csv"], input=stdin)
        assert result.exit_code == 0, result.output
        assert "table: t" in result.output

    def test_repl_multiline_statement(self, runner, dbdir):
        stdin = "CREATE TABLE t (id INTEGER);\nSELECT 1\nAS x;\n\\q\n"
        result = runner.invoke(main, [dbdir, "-f", "csv"], input=stdin)
        assert result.exit_code == 0, result.output
        assert "x\n1\n" in result.output

    def test_repl_error_continues(self, runner, dbdir):
        stdin = "SELECT * FROM missing;\nSELECT 2 AS y;\n\\q\n"
        result = runner.invoke(main, [dbdir, "-f", "csv"], input=stdin)
        assert result.exit_code == 0
        assert "no such table" in result.output
        assert "y\n2\n" in result.output

    def test_repl_subcommand(self, runner, dbdir):
        stdin = "SELECT 1 AS one;\n\\q\n"
        result = runner.invoke(main, ["repl", dbdir, "-f", "csv"], input=stdin)
        assert result.exit_code == 0, result.output
        assert "one\n1\n" in result.output

    def test_repl_exit_word(self, runner, dbdir):
        result = runner.invoke(main, [dbdir], input="exit\n")
        assert result.exit_code == 0

    def test_repl_expanded_display(self, runner, dbdir):
        stdin = (
            "CREATE TABLE t (id INTEGER, name TEXT);\n"
            "INSERT INTO t VALUES (1, 'alice');\n"
            "\\x\n"
            "SELECT * FROM t;\n"
            "\\q\n"
        )
        result = runner.invoke(main, [dbdir], input=stdin)
        assert result.exit_code == 0, result.output
        assert "Expanded display is on." in result.output
        assert "-[ RECORD 1 ]-" in result.output
        assert "id   | 1" in result.output
        assert "name | alice" in result.output

    def test_repl_pset_format(self, runner, dbdir):
        stdin = (
            "CREATE TABLE t (id INTEGER);\n"
            "INSERT INTO t VALUES (7);\n"
            "\\pset format json\n"
            "SELECT * FROM t;\n"
            "\\q\n"
        )
        result = runner.invoke(main, [dbdir], input=stdin)
        assert result.exit_code == 0, result.output
        assert "Output format is json." in result.output
        assert '{"id": 7}' in result.output

    def test_repl_invalid_backslash_command(self, runner, dbdir):
        result = runner.invoke(main, [dbdir], input="\\foo\n\\q\n")
        assert result.exit_code == 0
        assert "invalid command \\foo" in result.output

    def test_repl_psql_prompt(self, runner, tmp_path):
        dbdir = str(tmp_path / "mydb")
        result = runner.invoke(main, [dbdir], input="\\q\n")
        assert result.exit_code == 0
        assert "mydb=#" in result.output


class TestInitAndCheck:
    def test_init(self, runner, dbdir):
        result = run_ok(runner, ["init", dbdir])
        assert "initialized" in result.output

    def test_check_ok(self, runner, dbdir):
        run_ok(runner, [dbdir, "-c", "CREATE TABLE t (id INTEGER PRIMARY KEY)"])
        run_ok(runner, [dbdir, "-c", "INSERT INTO t VALUES (1), (2)"])
        result = run_ok(runner, ["check", dbdir])
        assert result.output == "ok\n"

    def test_check_detects_duplicate_pk(self, runner, dbdir, tmp_path):
        run_ok(runner, [dbdir, "-c", "CREATE TABLE t (id INTEGER PRIMARY KEY)"])
        (tmp_path / "db" / "t.csv").write_text("id\n1\n1\n", encoding="utf-8")
        result = runner.invoke(main, ["check", dbdir])
        assert result.exit_code == 1
        assert "duplicate primary key" in result.output

    def test_check_detects_type_error(self, runner, dbdir, tmp_path):
        run_ok(runner, [dbdir, "-c", "CREATE TABLE t (id INTEGER PRIMARY KEY)"])
        (tmp_path / "db" / "t.csv").write_text("id\nabc\n", encoding="utf-8")
        result = runner.invoke(main, ["check", dbdir])
        assert result.exit_code == 1
        assert "invalid integer" in result.output

    def test_check_warns_non_canonical(self, runner, dbdir, tmp_path):
        run_ok(runner, [dbdir, "-c", "CREATE TABLE t (id INTEGER PRIMARY KEY)"])
        (tmp_path / "db" / "t.csv").write_text("id\r\n1\r\n", encoding="utf-8")
        result = run_ok(runner, ["check", dbdir])
        assert "canonical" in result.output
        assert "warnings" in result.output

    def test_check_detects_not_null_violation(self, runner, dbdir, tmp_path):
        run_ok(runner, [dbdir, "-c", "CREATE TABLE t (id INTEGER PRIMARY KEY, n TEXT NOT NULL)"])
        (tmp_path / "db" / "t.csv").write_text("id,n\n1,\\N\n", encoding="utf-8")
        result = runner.invoke(main, ["check", dbdir])
        assert result.exit_code == 1
        assert "NOT NULL" in result.output
