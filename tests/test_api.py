import pytest

import iceql
from iceql.errors import InterfaceError, ProgrammingError


class TestModuleAttributes:
    def test_dbapi_globals(self):
        assert iceql.apilevel == "2.0"
        assert iceql.paramstyle == "qmark"


class TestConnection:
    def test_connect_creates_directory(self, tmp_path):
        conn = iceql.connect(tmp_path / "newdb")
        assert (tmp_path / "newdb").is_dir()
        conn.close()

    def test_closed_connection_raises(self, conn):
        conn.close()
        with pytest.raises(InterfaceError):
            conn.execute("SELECT 1")

    def test_context_manager(self, tmp_path):
        with iceql.connect(tmp_path / "db2") as conn:
            assert conn.execute("SELECT 1").fetchall() == [(1,)]
        with pytest.raises(InterfaceError):
            conn.cursor()

    def test_commit_is_noop(self, conn):
        conn.commit()


class TestCursor:
    def test_description(self, conn):
        cur = conn.execute("SELECT id, name FROM users")
        assert [d[0] for d in cur.description] == ["id", "name"]
        assert all(len(d) == 7 for d in cur.description)

    def test_fetchone_exhausts(self, conn):
        cur = conn.execute("SELECT id FROM depts ORDER BY id")
        assert cur.fetchone() == (1,)
        assert cur.fetchone() == (2,)
        assert cur.fetchone() is None

    def test_fetchmany(self, conn):
        cur = conn.execute("SELECT id FROM users ORDER BY id")
        assert cur.fetchmany(3) == [(1,), (2,), (3,)]
        assert cur.fetchmany(3) == [(4,)]

    def test_iteration(self, conn):
        cur = conn.execute("SELECT id FROM depts ORDER BY id")
        assert [row for row in cur] == [(1,), (2,)]

    def test_closed_cursor(self, conn):
        cur = conn.cursor()
        cur.close()
        with pytest.raises(InterfaceError):
            cur.execute("SELECT 1")


class TestParameters:
    def test_qmark(self, conn):
        cur = conn.execute("SELECT name FROM users WHERE age > ? AND active = ?", (26, True))
        rows = cur.fetchall()
        assert rows == [("alice",), ("dave",)]

    def test_named(self, conn):
        rows = conn.execute("SELECT name FROM users WHERE id = :uid", {"uid": 3}).fetchall()
        assert rows == [("carol",)]

    def test_null_param(self, conn):
        rows = conn.execute("SELECT name FROM users WHERE ? IS NULL", (None,)).fetchall()
        assert len(rows) == 4

    def test_string_with_quote(self, conn):
        rows = conn.execute("SELECT ?", ("it's",)).fetchall()
        assert rows == [("it's",)]

    def test_param_count_mismatch(self, conn):
        with pytest.raises(ProgrammingError, match="expected 1 parameters"):
            conn.execute("SELECT name FROM users WHERE id = ?", (1, 2))

    def test_missing_named(self, conn):
        with pytest.raises(ProgrammingError, match="missing named parameter"):
            conn.execute("SELECT name FROM users WHERE id = :uid", {"other": 1})

    def test_unsupported_type(self, conn):
        with pytest.raises(ProgrammingError, match="unsupported parameter type"):
            conn.execute("SELECT ?", (object(),))
