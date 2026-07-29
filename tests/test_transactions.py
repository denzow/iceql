import pytest

from iceql.errors import NotSupportedError, ProgrammingError


def dept_count(conn):
    return conn.execute("SELECT COUNT(*) FROM depts").fetchone()[0]


class TestTransaction:
    def test_commit_persists(self, conn):
        conn.execute("BEGIN")
        conn.execute("INSERT INTO depts (id, dept) VALUES (3, 'hr')")
        conn.execute("COMMIT")
        assert dept_count(conn) == 3
        text = (conn._catalog.root / "depts.csv").read_text(encoding="utf-8")
        assert "3,hr" in text

    def test_rollback_discards(self, conn):
        conn.execute("BEGIN")
        conn.execute("INSERT INTO depts (id, dept) VALUES (3, 'hr')")
        conn.execute("ROLLBACK")
        assert dept_count(conn) == 2
        text = (conn._catalog.root / "depts.csv").read_text(encoding="utf-8")
        assert "3,hr" not in text

    def test_no_disk_write_before_commit(self, conn):
        conn.execute("BEGIN")
        conn.execute("DELETE FROM depts")
        text = (conn._catalog.root / "depts.csv").read_text(encoding="utf-8")
        assert "eng" in text  # ディスクは未変更
        conn.execute("COMMIT")
        text = (conn._catalog.root / "depts.csv").read_text(encoding="utf-8")
        assert "eng" not in text

    def test_select_sees_staged_changes(self, conn):
        conn.execute("BEGIN")
        conn.execute("UPDATE users SET age = 99 WHERE id = 1")
        assert conn.execute("SELECT age FROM users WHERE id = 1").fetchone() == (99,)
        conn.execute("ROLLBACK")
        assert conn.execute("SELECT age FROM users WHERE id = 1").fetchone() == (30,)

    def test_multiple_tables_in_one_transaction(self, conn):
        conn.execute("BEGIN")
        conn.execute("INSERT INTO depts (id, dept) VALUES (3, 'hr')")
        conn.execute("UPDATE users SET dept_id = 3 WHERE id = 3")
        conn.execute("COMMIT")
        row = conn.execute(
            "SELECT d.dept FROM users u JOIN depts d ON u.dept_id = d.id WHERE u.id = 3"
        ).fetchone()
        assert row == ("hr",)

    def test_commit_method(self, conn):
        conn.execute("BEGIN")
        conn.execute("INSERT INTO depts (id, dept) VALUES (3, 'hr')")
        assert conn.in_transaction
        conn.commit()
        assert not conn.in_transaction
        assert dept_count(conn) == 3

    def test_rollback_method(self, conn):
        conn.execute("BEGIN")
        conn.execute("INSERT INTO depts (id, dept) VALUES (3, 'hr')")
        conn.rollback()
        assert dept_count(conn) == 2

    def test_commit_without_transaction_is_noop_method(self, conn):
        conn.commit()
        conn.rollback()

    def test_commit_statement_without_transaction_raises(self, conn):
        with pytest.raises(ProgrammingError, match="no transaction"):
            conn.execute("COMMIT")
        with pytest.raises(ProgrammingError, match="no transaction"):
            conn.execute("ROLLBACK")

    def test_nested_begin_raises(self, conn):
        conn.execute("BEGIN")
        with pytest.raises(ProgrammingError, match="already active"):
            conn.execute("BEGIN")

    def test_ddl_in_transaction_rejected(self, conn):
        conn.execute("BEGIN")
        with pytest.raises(NotSupportedError, match="transaction"):
            conn.execute("CREATE TABLE t (id INTEGER)")

    def test_close_discards_staged(self, conn, tmp_path):
        conn.execute("BEGIN")
        conn.execute("INSERT INTO depts (id, dept) VALUES (3, 'hr')")
        conn.close()
        import iceql

        conn2 = iceql.connect(tmp_path / "db")
        assert dept_count(conn2) == 2
        conn2.close()

    def test_repeated_dml_on_same_table(self, conn):
        conn.execute("BEGIN")
        conn.execute("INSERT INTO depts (id, dept) VALUES (3, 'hr')")
        conn.execute("INSERT INTO depts (id, dept) VALUES (4, 'legal')")
        conn.execute("DELETE FROM depts WHERE id = 1")
        conn.execute("COMMIT")
        rows = conn.execute("SELECT id FROM depts ORDER BY id").fetchall()
        assert rows == [(2,), (3,), (4,)]


class TestTransactionDifferential:
    def test_matches_sqlite(self, tmp_path):
        import sqlite3

        import iceql

        lite = sqlite3.connect(":memory:")
        lite.isolation_level = None  # 明示的な BEGIN/COMMIT を使う
        ice = iceql.connect(tmp_path / "tx")
        script = [
            "CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)",
            "INSERT INTO t VALUES (1, 'a')",
            "BEGIN",
            "INSERT INTO t VALUES (2, 'b')",
            "UPDATE t SET v = 'z' WHERE id = 1",
            "ROLLBACK",
            "BEGIN",
            "INSERT INTO t VALUES (3, 'c')",
            "COMMIT",
        ]
        for sql in script:
            lite.execute(sql)
            ice.execute(sql)
        expected = lite.execute("SELECT * FROM t ORDER BY id").fetchall()
        actual = ice.execute("SELECT * FROM t ORDER BY id").fetchall()
        assert actual == expected == [(1, "a"), (3, "c")]
