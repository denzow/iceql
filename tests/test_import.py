import pytest
from click.testing import CliRunner

import iceql
from iceql import storage
from iceql.cli import main
from iceql.errors import DataError, Error, ProgrammingError
from iceql.importer import build_plan, import_csv, infer_column_type, parse_type_overrides


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def dbdir(tmp_path):
    return str(tmp_path / "db")


def write_csv(tmp_path, text, name="users.csv"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8", newline="")
    return path


def run_ok(runner, args):
    result = runner.invoke(main, args, catch_exceptions=False)
    assert result.exit_code == 0, result.output + result.stderr
    return result


def run_fail(runner, args):
    result = runner.invoke(main, args, catch_exceptions=False)
    assert result.exit_code != 0
    return result


class TestInferColumnType:
    @pytest.mark.parametrize(
        ("fields", "expected"),
        [
            (["5", "0"], "integer"),
            (["007"], "text"),
            (["-0"], "text"),
            (["+5"], "text"),
            ([" 5"], "text"),
            (["1.5"], "real"),
            (["1.10"], "text"),
            (["10.00"], "text"),
            (["1e3"], "text"),
            (["1", "2.5"], "real"),
            (["true", "false"], "boolean"),
            (["1", "0"], "integer"),
            (["2020-01-15"], "date"),
            (["2020-01-15 10:00:00"], "datetime"),
            (["2020-1-5"], "text"),
            (["nan"], "text"),
            (["inf"], "text"),
            (["abc"], "text"),
            ([""], "text"),
            ([None, None], "text"),
            (["1", None, "2"], "integer"),
            (["1", "abc"], "text"),
        ],
    )
    def test_inference(self, fields, expected):
        assert infer_column_type(fields) == expected


class TestBasicImport:
    def test_creates_table_and_passes_check(self, runner, tmp_path, dbdir):
        csv_path = write_csv(tmp_path, "id,name,age\n1,alice,30\n2,bob,\\N\n")
        run_ok(runner, ["import", dbdir, str(csv_path)])
        result = run_ok(runner, ["check", dbdir])
        assert result.output == "ok\n"
        conn = iceql.connect(dbdir)
        schema = conn._catalog.load_schema("users")
        assert [(c.name, c.type, c.nullable) for c in schema.columns] == [
            ("id", "integer", False),
            ("name", "text", False),
            ("age", "integer", True),
        ]
        assert conn.execute("SELECT * FROM users ORDER BY id").fetchall() == [
            (1, "alice", 30),
            (2, "bob", None),
        ]
        conn.close()

    def test_table_name_option(self, runner, tmp_path, dbdir):
        csv_path = write_csv(tmp_path, "id\n1\n")
        run_ok(runner, ["import", dbdir, str(csv_path), "--table", "people"])
        conn = iceql.connect(dbdir)
        assert conn._catalog.list_tables() == ["people"]
        conn.close()

    def test_no_data_rows_creates_empty_table(self, runner, tmp_path, dbdir):
        csv_path = write_csv(tmp_path, "id,name\n")
        run_ok(runner, ["import", dbdir, str(csv_path)])
        conn = iceql.connect(dbdir)
        assert conn.execute("SELECT * FROM users").fetchall() == []
        # 行が無い列は text になる
        assert [c.type for c in conn._catalog.load_schema("users").columns] == ["text", "text"]
        conn.close()

    def test_preview_goes_to_stderr(self, runner, tmp_path, dbdir):
        csv_path = write_csv(tmp_path, "id,name\n1,alice\n")
        result = run_ok(runner, ["import", dbdir, str(csv_path)])
        assert "1 rows, 2 columns" in result.stderr
        assert "imported 1 rows into users" in result.stderr

    def test_stdin(self, runner, dbdir):
        result = runner.invoke(
            main,
            ["import", dbdir, "-", "--table", "t"],
            input="id\n7\n",
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.stderr
        conn = iceql.connect(dbdir)
        assert conn.execute("SELECT * FROM t").fetchall() == [(7,)]
        conn.close()

    def test_stdin_requires_table(self, runner, dbdir):
        result = runner.invoke(main, ["import", dbdir, "-"], input="id\n7\n")
        assert result.exit_code != 0
        assert "--table is required" in result.stderr

    def test_bom_is_stripped(self, runner, tmp_path, dbdir):
        path = tmp_path / "users.csv"
        path.write_bytes("id,name\n1,alice\n".encode("utf-8-sig"))
        run_ok(runner, ["import", dbdir, str(path)])
        conn = iceql.connect(dbdir)
        assert conn._catalog.load_schema("users").column_names == ["id", "name"]
        conn.close()

    def test_encoding_option(self, runner, tmp_path, dbdir):
        path = tmp_path / "users.csv"
        path.write_bytes("id,name\n1,アリス\n".encode("cp932"))
        run_ok(runner, ["import", dbdir, str(path), "--encoding", "cp932"])
        conn = iceql.connect(dbdir)
        assert conn.execute("SELECT name FROM users").fetchall() == [("アリス",)]
        conn.close()

    def test_quoted_fields_and_embedded_newline(self, runner, tmp_path, dbdir):
        csv_path = write_csv(tmp_path, 'id,note\n1,"a,b"\n2,"line1\nline2"\n')
        run_ok(runner, ["import", dbdir, str(csv_path)])
        conn = iceql.connect(dbdir)
        assert conn.execute("SELECT note FROM users ORDER BY id").fetchall() == [
            ("a,b",),
            ("line1\nline2",),
        ]
        conn.close()


class TestRoundTrip:
    def test_select_reproduces_the_source_csv(self, runner, tmp_path, dbdir):
        source = (
            "id,name,age,zip,price,flag,joined\n"
            "1,alice,30,007,1.5,true,2020-01-15\n"
            "2,bob,\\N,012,2.5,false,2021-06-01\n"
        )
        csv_path = write_csv(tmp_path, source)
        run_ok(runner, ["import", dbdir, str(csv_path)])
        result = run_ok(runner, [dbdir, "-c", "SELECT * FROM users ORDER BY id", "-f", "csv"])
        assert result.output == source

    def test_null_marker_convention(self, runner, tmp_path, dbdir):
        csv_path = write_csv(tmp_path, "v\n\\N\n\\\\N\nplain\n")
        run_ok(runner, ["import", dbdir, str(csv_path)])
        conn = iceql.connect(dbdir)
        assert conn.execute("SELECT v FROM users").fetchall() == [
            (None,),
            ("\\N",),
            ("plain",),
        ]
        conn.close()


class TestNullMarker:
    def test_empty_field_as_null(self, runner, tmp_path, dbdir):
        csv_path = write_csv(tmp_path, "id,age\n1,30\n2,\n")
        run_ok(runner, ["import", dbdir, str(csv_path), "--null-marker", ""])
        conn = iceql.connect(dbdir)
        schema = conn._catalog.load_schema("users")
        assert [(c.type, c.nullable) for c in schema.columns] == [
            ("integer", False),
            ("integer", True),
        ]
        assert conn.execute("SELECT age FROM users ORDER BY id").fetchall() == [(30,), (None,)]
        conn.close()

    def test_custom_marker_disables_backslash_escape(self, tmp_path):
        csv_path = write_csv(tmp_path, "v\nNA\n\\N\n")
        plan = build_plan(csv_path, null_marker="NA")
        assert plan.rows == [(None,), ("\\N",)]

    def test_empty_field_note(self, runner, tmp_path, dbdir):
        csv_path = write_csv(tmp_path, "id,age\n1,30\n2,\n")
        result = run_ok(runner, ["import", dbdir, str(csv_path), "--dry-run"])
        assert "--null-marker" in result.stderr


class TestTypeOverrides:
    def test_override(self, runner, tmp_path, dbdir):
        csv_path = write_csv(tmp_path, "id,zip\n1,007\n")
        run_ok(runner, ["import", dbdir, str(csv_path), "--types", "id=real"])
        conn = iceql.connect(dbdir)
        assert [c.type for c in conn._catalog.load_schema("users").columns] == ["real", "text"]
        conn.close()

    def test_undecodable_value_reports_the_line(self, runner, tmp_path, dbdir):
        csv_path = write_csv(tmp_path, "v\n1\nabc\n")
        result = run_fail(runner, ["import", dbdir, str(csv_path), "--types", "v=integer"])
        assert "line 3" in result.stderr
        assert "'v'" in result.stderr

    def test_unknown_column(self, runner, tmp_path, dbdir):
        csv_path = write_csv(tmp_path, "v\n1\n")
        result = run_fail(runner, ["import", dbdir, str(csv_path), "--types", "nope=integer"])
        assert "no such column" in result.stderr

    def test_unknown_type(self, runner, tmp_path, dbdir):
        csv_path = write_csv(tmp_path, "v\n1\n")
        result = run_fail(runner, ["import", dbdir, str(csv_path), "--types", "v=money"])
        assert "unknown type" in result.stderr

    def test_malformed_option(self):
        with pytest.raises(DataError):
            parse_type_overrides(["idinteger"])
        with pytest.raises(DataError):
            parse_type_overrides(["v=integer", "v=text"])


class TestDryRun:
    def test_writes_nothing(self, runner, tmp_path, dbdir):
        csv_path = write_csv(tmp_path, "id\n1\n")
        result = run_ok(runner, ["import", dbdir, str(csv_path), "--dry-run"])
        assert "dry run: no changes written" in result.stderr
        assert not (tmp_path / "db").exists()


class TestErrors:
    def _fails(self, runner, dbdir, csv_path, message, extra=()):
        result = run_fail(runner, ["import", dbdir, str(csv_path), *extra])
        assert message in result.stderr
        assert not iceql.connect(dbdir)._catalog.list_tables()
        return result

    def test_invalid_column_name(self, runner, tmp_path, dbdir):
        csv_path = write_csv(tmp_path, "id,user name\n1,alice\n")
        self._fails(runner, dbdir, csv_path, "invalid column name")

    def test_duplicate_column_name(self, runner, tmp_path, dbdir):
        csv_path = write_csv(tmp_path, "id,id\n1,2\n")
        self._fails(runner, dbdir, csv_path, "duplicate column name")

    def test_empty_file(self, runner, tmp_path, dbdir):
        csv_path = write_csv(tmp_path, "")
        self._fails(runner, dbdir, csv_path, "missing header row")

    def test_field_count_mismatch(self, runner, tmp_path, dbdir):
        csv_path = write_csv(tmp_path, "a,b\n1,2\n3\n")
        self._fails(runner, dbdir, csv_path, "line 3: expected 2 fields, got 1")

    def test_table_name_from_file_name(self, runner, tmp_path, dbdir):
        csv_path = write_csv(tmp_path, "id\n1\n", name="my data.csv")
        self._fails(runner, dbdir, csv_path, "use --table")

    def test_missing_file(self, runner, tmp_path, dbdir):
        result = run_fail(runner, ["import", dbdir, str(tmp_path / "nope.csv")])
        assert "cannot read" in result.stderr

    def test_existing_table(self, runner, tmp_path, dbdir):
        csv_path = write_csv(tmp_path, "id\n1\n")
        run_ok(runner, ["import", dbdir, str(csv_path)])
        result = run_fail(runner, ["import", dbdir, str(csv_path)])
        assert "table already exists: users" in result.stderr
        conn = iceql.connect(dbdir)
        assert conn.execute("SELECT COUNT(*) FROM users").fetchall() == [(1,)]
        conn.close()

    def test_existing_table_via_api(self, tmp_path):
        csv_path = write_csv(tmp_path, "id\n1\n")
        import_csv(tmp_path / "db", csv_path)
        with pytest.raises(ProgrammingError):
            import_csv(tmp_path / "db", csv_path)


class TestSingleWrite:
    def test_large_csv_writes_each_file_once(self, tmp_path, monkeypatch):
        rows = "\n".join(f"{i},name{i}" for i in range(10000))
        csv_path = write_csv(tmp_path, f"id,name\n{rows}\n")
        calls = []
        original = storage.atomic_write
        monkeypatch.setattr(
            storage,
            "atomic_write",
            lambda path, content: (calls.append(path.name), original(path, content))[1],
        )
        plan = import_csv(tmp_path / "db", csv_path)
        assert len(plan.rows) == 10000
        # CSV とスキーマの 1 回ずつ。行ごとの INSERT を経由していれば増える
        assert calls == ["users.csv", "users.schema.yaml"]


class TestLibraryApi:
    def test_build_plan_does_not_touch_the_database(self, tmp_path):
        csv_path = write_csv(tmp_path, "id,name\n1,alice\n")
        plan = build_plan(csv_path, table="t")
        assert plan.table == "t"
        assert plan.rows == [(1, "alice")]
        assert not (tmp_path / "db").exists()

    def test_import_csv_returns_the_plan(self, tmp_path):
        csv_path = write_csv(tmp_path, "id\n1\n")
        plan = import_csv(tmp_path / "db", csv_path, table="t")
        assert plan.schema.column_names == ["id"]
        assert plan.rows == [(1,)]

    def test_errors_are_iceql_errors(self, tmp_path):
        csv_path = write_csv(tmp_path, "id id\n1\n")
        with pytest.raises(Error):
            build_plan(csv_path)
