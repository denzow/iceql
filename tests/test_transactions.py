import pytest

from iceql.check import check_database
from iceql.errors import ProgrammingError


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


class TestTransactionDdl:
    """トランザクション内の DDL。COMMIT まではディスクに現れない。"""

    def files(self, conn, table):
        root = conn._catalog.root
        return (root / f"{table}.csv").exists(), (root / f"{table}.schema.yaml").exists()

    def test_create_and_insert_commit(self, conn):
        conn.execute("BEGIN")
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.execute("INSERT INTO t VALUES (1, 'a')")
        assert conn.execute("SELECT v FROM t").fetchone() == ("a",)
        assert self.files(conn, "t") == (False, False)  # COMMIT 前は書かれない
        conn.execute("COMMIT")
        assert self.files(conn, "t") == (True, True)
        assert conn.execute("SELECT v FROM t").fetchone() == ("a",)
        assert check_database(conn._catalog.root) == []

    def test_create_rollback_leaves_nothing(self, conn):
        conn.execute("BEGIN")
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        conn.execute("ROLLBACK")
        assert self.files(conn, "t") == (False, False)
        with pytest.raises(ProgrammingError, match="no such table"):
            conn.execute("SELECT * FROM t")

    def test_drop_commit_removes_files(self, conn):
        conn.execute("BEGIN")
        conn.execute("DROP TABLE depts")
        assert self.files(conn, "depts") == (True, True)  # COMMIT 前は残る
        conn.execute("COMMIT")
        assert self.files(conn, "depts") == (False, False)
        assert check_database(conn._catalog.root) == []

    def test_drop_rollback_keeps_table(self, conn):
        conn.execute("BEGIN")
        conn.execute("DROP TABLE depts")
        conn.execute("ROLLBACK")
        assert self.files(conn, "depts") == (True, True)
        assert conn.execute("SELECT COUNT(*) FROM depts").fetchone() == (2,)

    def test_dropped_table_is_invisible_in_transaction(self, conn):
        conn.execute("BEGIN")
        conn.execute("DROP TABLE depts")
        with pytest.raises(ProgrammingError, match="no such table"):
            conn.execute("SELECT * FROM depts")
        assert "depts" not in conn._active_catalog.list_tables()
        conn.execute("ROLLBACK")

    def test_drop_then_create_same_name(self, conn):
        conn.execute("BEGIN")
        conn.execute("DROP TABLE depts")
        conn.execute("CREATE TABLE depts (id INTEGER PRIMARY KEY, label TEXT)")
        conn.execute("INSERT INTO depts VALUES (9, 'ops')")
        conn.execute("COMMIT")
        assert conn.execute("SELECT label FROM depts").fetchall() == [("ops",)]
        assert check_database(conn._catalog.root) == []

    def test_create_then_drop_same_transaction(self, conn):
        conn.execute("BEGIN")
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        conn.execute("DROP TABLE t")
        conn.execute("COMMIT")
        assert self.files(conn, "t") == (False, False)

    def test_create_existing_table_raises(self, conn):
        conn.execute("BEGIN")
        with pytest.raises(ProgrammingError, match="already exists"):
            conn.execute("CREATE TABLE depts (id INTEGER)")
        conn.execute("CREATE TABLE t (id INTEGER)")
        with pytest.raises(ProgrammingError, match="already exists"):
            conn.execute("CREATE TABLE t (id INTEGER)")  # ステージ済みの名前も衝突する
        conn.execute("ROLLBACK")

    def test_if_exists_and_if_not_exists_are_noops(self, conn):
        conn.execute("BEGIN")
        conn.execute("DROP TABLE IF EXISTS nothing")
        conn.execute("CREATE TABLE IF NOT EXISTS depts (id INTEGER)")
        conn.execute("COMMIT")
        # depts は元のまま(IF NOT EXISTS で作り直されていない)
        assert conn.execute("SELECT dept FROM depts WHERE id = 1").fetchone() == ("eng",)

    def test_alter_add_column_then_backfill(self, conn):
        """issue #12 の動機となるケース: スキーマ変更とデータ移行を 1 単位にする。"""
        conn.execute("BEGIN")
        conn.execute("ALTER TABLE depts ADD COLUMN code TEXT")
        conn.execute("UPDATE depts SET code = 'E' WHERE id = 1")
        conn.execute("UPDATE depts SET code = 'S' WHERE id = 2")
        assert "code" not in (conn._catalog.root / "depts.csv").read_text(encoding="utf-8")
        conn.execute("COMMIT")
        rows = conn.execute("SELECT id, code FROM depts ORDER BY id").fetchall()
        assert rows == [(1, "E"), (2, "S")]
        assert check_database(conn._catalog.root) == []

    def test_alter_add_column_rollback(self, conn):
        conn.execute("BEGIN")
        conn.execute("ALTER TABLE depts ADD COLUMN code TEXT")
        conn.execute("ROLLBACK")
        with pytest.raises(ProgrammingError):
            conn.execute("SELECT code FROM depts")

    def test_alter_drop_and_rename_column(self, conn):
        conn.execute("BEGIN")
        conn.execute("ALTER TABLE depts RENAME COLUMN dept TO name")
        conn.execute("ALTER TABLE users DROP COLUMN joined")
        conn.execute("COMMIT")
        assert conn.execute("SELECT name FROM depts WHERE id = 1").fetchone() == ("eng",)
        assert "joined" not in [
            c.name for c in conn._catalog.load_schema("users").columns
        ]
        assert check_database(conn._catalog.root) == []

    def test_rename_table_commit(self, conn):
        conn.execute("BEGIN")
        conn.execute("ALTER TABLE depts RENAME TO teams")
        assert conn.execute("SELECT COUNT(*) FROM teams").fetchone() == (2,)
        with pytest.raises(ProgrammingError, match="no such table"):
            conn.execute("SELECT * FROM depts")
        conn.execute("COMMIT")
        assert self.files(conn, "depts") == (False, False)
        assert self.files(conn, "teams") == (True, True)
        assert conn.execute("SELECT dept FROM teams WHERE id = 2").fetchone() == ("sales",)
        assert check_database(conn._catalog.root) == []

    def test_rename_table_rollback(self, conn):
        conn.execute("BEGIN")
        conn.execute("ALTER TABLE depts RENAME TO teams")
        conn.execute("ROLLBACK")
        assert self.files(conn, "teams") == (False, False)
        assert conn.execute("SELECT COUNT(*) FROM depts").fetchone() == (2,)

    def test_rename_to_existing_name_raises(self, conn):
        conn.execute("BEGIN")
        with pytest.raises(ProgrammingError, match="already exists"):
            conn.execute("ALTER TABLE depts RENAME TO users")
        conn.execute("ROLLBACK")

    def test_drop_missing_table_raises(self, conn):
        conn.execute("BEGIN")
        with pytest.raises(ProgrammingError, match="no such table"):
            conn.execute("DROP TABLE nothing")
        conn.execute("ROLLBACK")

    def test_created_table_visible_to_list_tables(self, conn):
        conn.execute("BEGIN")
        conn.execute("CREATE TABLE t (id INTEGER)")
        assert conn._active_catalog.list_tables() == ["depts", "t", "users"]
        conn.execute("ROLLBACK")
        assert conn._active_catalog.list_tables() == ["depts", "users"]

    def test_other_connection_sees_nothing_until_commit(self, conn, tmp_path):
        import iceql

        other = iceql.connect(tmp_path / "db")
        conn.execute("BEGIN")
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        conn.execute("DROP TABLE depts")
        assert other.execute("SELECT COUNT(*) FROM depts").fetchone() == (2,)
        with pytest.raises(ProgrammingError, match="no such table"):
            other.execute("SELECT * FROM t")
        conn.execute("COMMIT")
        assert other.execute("SELECT COUNT(*) FROM t").fetchone() == (0,)
        with pytest.raises(ProgrammingError, match="no such table"):
            other.execute("SELECT * FROM depts")
        other.close()


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

    def test_ddl_in_transaction_matches_sqlite(self, tmp_path):
        import sqlite3

        import iceql

        lite = sqlite3.connect(":memory:")
        lite.isolation_level = None
        ice = iceql.connect(tmp_path / "ddltx")
        script = [
            "CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)",
            "INSERT INTO t VALUES (1, 'a'), (2, 'b')",
            # スキーマ変更とデータ移行を 1 トランザクションにまとめる
            "BEGIN",
            "ALTER TABLE t ADD COLUMN n INTEGER",
            "UPDATE t SET n = id * 10",
            "CREATE TABLE u (id INTEGER PRIMARY KEY)",
            "INSERT INTO u VALUES (1)",
            "COMMIT",
            # 作成と削除をまとめて捨てる
            "BEGIN",
            "DROP TABLE u",
            "CREATE TABLE w (id INTEGER PRIMARY KEY)",
            "ROLLBACK",
            # 改名を反映する
            "BEGIN",
            "ALTER TABLE t RENAME TO t2",
            "COMMIT",
        ]
        for sql in script:
            lite.execute(sql)
            ice.execute(sql)
        for query in ("SELECT * FROM t2 ORDER BY id", "SELECT * FROM u ORDER BY id"):
            assert ice.execute(query).fetchall() == lite.execute(query).fetchall()
        assert ice.execute("SELECT * FROM t2 ORDER BY id").fetchall() == [
            (1, "a", 10),
            (2, "b", 20),
        ]
        ice.close()
