"""行を tuple で保持し、sqlglot へ変換なしで渡すことの回帰テスト。

sqlglot の ensure_tables は Table インスタンスを素通しするため、iceql が読んだ
行リストがそのまま executor 側の Table になる。素通しされる分、列名の正規化と
行リストの不変性は iceql 側で守る必要がある。
"""

import iceql
from iceql.engine import executor, parse_statement
from iceql.storage import read_rows


class TestRowsAreTuples:
    def test_read_rows_returns_tuples(self, conn):
        rows = conn._catalog.read_rows("depts")
        assert rows == [(1, "eng"), (2, "sales")]
        assert all(isinstance(row, tuple) for row in rows)

    def test_insert_update_delete_keep_tuples(self, tmp_path):
        conn = iceql.connect(tmp_path / "db")
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT, n INTEGER)")
        conn.execute("INSERT INTO t (n, id) VALUES (7, 1), (8, 2)")
        schema = conn._catalog.load_schema("t")
        csv_path = conn._catalog.csv_path("t")
        assert read_rows(csv_path, schema) == [(1, None, 7), (2, None, 8)]
        conn.execute("UPDATE t SET v = 'x' WHERE id = 2")
        conn.execute("DELETE FROM t WHERE id = 1")
        assert read_rows(csv_path, schema) == [(2, "x", 8)]
        conn.close()


class TestSqlglotHandover:
    def test_table_shares_the_row_list(self):
        rows = [(1, "a"), (2, "b")]
        assert executor.sqlglot_table(["id", "name"], rows).rows is rows

    def test_column_names_are_normalized(self):
        # 自前で作る Table は sqlglot 側で正規化されないので、渡す前に揃える
        assert executor.sqlglot_table(["Id", "NAME"], []).columns == ("id", "name")

    def test_evaluate_does_not_mutate_the_source_rows(self, conn):
        tables, annotations = executor.load_tables(conn._catalog, {"users", "depts"})
        source = tables["users"].rows
        snapshot = list(source)
        ast = parse_statement(
            "SELECT d.dept, COUNT(*) AS n FROM users u "
            "JOIN depts d ON u.dept_id = d.id GROUP BY d.dept"
        )
        _, rows = executor.evaluate(ast, tables, annotations)
        assert sorted(rows) == [("eng", 2), ("sales", 1)]
        assert tables["users"].rows is source
        assert source == snapshot


class TestCaseFolding:
    """大文字を含む列名の扱い。

    Table を素通しさせると列名が正規化されないので、SELECT が引けなくなる。
    テーブル名は CSV のファイル名なので、従来どおり大小を区別する。
    """

    def test_uppercase_column_names(self, tmp_path):
        conn = iceql.connect(tmp_path / "db")
        conn.execute("CREATE TABLE Users (Id INTEGER PRIMARY KEY, FullName TEXT)")
        conn.execute("INSERT INTO Users VALUES (1, 'alice'), (2, 'bob')")
        assert conn.execute("SELECT FullName FROM Users WHERE Id = 2").fetchall() == [
            ("bob",)
        ]
        assert conn.execute("SELECT fullname FROM Users WHERE id = 1").fetchall() == [
            ("alice",)
        ]
        conn.execute("UPDATE Users SET FullName = 'carol' WHERE id = 1")
        conn.execute("DELETE FROM Users WHERE ID = 2")
        assert conn.execute("SELECT Id, FullName FROM Users").fetchall() == [
            (1, "carol")
        ]
        conn.close()
