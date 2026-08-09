"""CSV が変わらない限り sqlglot の Table を作り直さないことのテスト。

キャッシュが効いていること以上に、効きすぎて古い内容を返さないことが重要なので、
自プロセスの書き込み・他プロセスの書き込み・トランザクション・DDL のそれぞれで
最新の内容が見えることを確かめる。
"""

import iceql
from iceql import storage
from iceql.engine import executor


def count_reads(monkeypatch):
    """storage.read_rows の呼び出し回数を数えるカウンタを返す。"""
    calls = []
    original = storage.read_rows

    def counted(path, schema):
        calls.append(path.name)
        return original(path, schema)

    monkeypatch.setattr(storage, "read_rows", counted)
    return calls


class TestReuse:
    def test_same_table_object_across_queries(self, conn):
        first, _ = executor.load_tables(conn._catalog, {"users"})
        second, _ = executor.load_tables(conn._catalog, {"users"})
        assert second["users"] is first["users"]

    def test_csv_is_read_once_for_repeated_selects(self, conn, monkeypatch):
        calls = count_reads(monkeypatch)
        for _ in range(3):
            assert conn.execute("SELECT COUNT(*) FROM users").fetchall() == [(4,)]
        assert calls == ["users.csv"]

    def test_each_table_is_cached_separately(self, conn, monkeypatch):
        calls = count_reads(monkeypatch)
        conn.execute("SELECT * FROM users JOIN depts ON users.dept_id = depts.id")
        conn.execute("SELECT * FROM depts")
        assert calls == ["depts.csv", "users.csv"]


class TestInvalidation:
    def test_own_write_is_visible(self, conn):
        assert conn.execute("SELECT COUNT(*) FROM depts").fetchall() == [(2,)]
        conn.execute("INSERT INTO depts VALUES (3, 'ops')")
        assert conn.execute("SELECT COUNT(*) FROM depts").fetchall() == [(3,)]
        conn.execute("UPDATE depts SET dept = 'sre' WHERE id = 3")
        assert conn.execute("SELECT dept FROM depts WHERE id = 3").fetchall() == [
            ("sre",)
        ]
        conn.execute("DELETE FROM depts WHERE id = 3")
        assert conn.execute("SELECT COUNT(*) FROM depts").fetchall() == [(2,)]

    def test_another_connection_write_is_visible(self, conn):
        assert conn.execute("SELECT COUNT(*) FROM depts").fetchall() == [(2,)]
        other = iceql.connect(conn._catalog.root)
        other.execute("INSERT INTO depts VALUES (3, 'ops')")
        other.close()
        # 書き込みは一時ファイルの置換なので、同じ大きさ・同じ時刻でも実体が変わる
        assert conn.execute("SELECT COUNT(*) FROM depts").fetchall() == [(3,)]

    def test_ddl_is_visible(self, conn):
        conn.execute("SELECT * FROM depts")
        conn.execute("ALTER TABLE depts ADD COLUMN head TEXT")
        assert conn.execute("SELECT id, dept, head FROM depts").fetchall() == [
            (1, "eng", None),
            (2, "sales", None),
        ]
        conn.execute("ALTER TABLE depts DROP COLUMN head")
        assert conn.execute("SELECT * FROM depts").fetchall() == [
            (1, "eng"),
            (2, "sales"),
        ]

    def test_dropped_table_is_not_served_from_the_cache(self, conn):
        conn.execute("SELECT * FROM depts")
        conn.execute("DROP TABLE depts")
        conn.execute("CREATE TABLE depts (id INTEGER PRIMARY KEY, dept TEXT)")
        assert conn.execute("SELECT * FROM depts").fetchall() == []

    def test_renamed_table_is_not_served_from_the_cache(self, conn):
        conn.execute("SELECT * FROM depts")
        conn.execute("ALTER TABLE depts RENAME TO teams")
        assert conn.execute("SELECT * FROM teams").fetchall() == [
            (1, "eng"),
            (2, "sales"),
        ]

    def test_hand_edited_csv_is_reread(self, conn):
        assert conn.execute("SELECT COUNT(*) FROM depts").fetchall() == [(2,)]
        path = conn._catalog.csv_path("depts")
        path.write_text(path.read_text() + "3,ops\n", encoding="utf-8")
        assert conn.execute("SELECT COUNT(*) FROM depts").fetchall() == [(3,)]


class TestTransaction:
    def test_staged_rows_are_visible_and_not_cached(self, conn):
        conn.execute("BEGIN")
        conn.execute("INSERT INTO depts VALUES (3, 'ops')")
        assert conn.execute("SELECT COUNT(*) FROM depts").fetchall() == [(3,)]
        conn.execute("INSERT INTO depts VALUES (4, 'qa')")
        assert conn.execute("SELECT COUNT(*) FROM depts").fetchall() == [(4,)]
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM depts").fetchall() == [(4,)]

    def test_rollback_restores_the_disk_contents(self, conn):
        conn.execute("SELECT * FROM depts")  # ロールバック前にキャッシュを作る
        conn.execute("BEGIN")
        conn.execute("INSERT INTO depts VALUES (3, 'ops')")
        assert conn.execute("SELECT COUNT(*) FROM depts").fetchall() == [(3,)]
        conn.rollback()
        assert conn.execute("SELECT COUNT(*) FROM depts").fetchall() == [(2,)]

    def test_unstaged_table_still_uses_the_cache(self, conn, monkeypatch):
        conn.execute("SELECT * FROM users")
        calls = count_reads(monkeypatch)
        conn.execute("BEGIN")
        conn.execute("INSERT INTO depts VALUES (3, 'ops')")
        assert conn.execute(
            "SELECT COUNT(*) FROM users JOIN depts ON users.dept_id = depts.id"
        ).fetchall() == [(3,)]
        conn.commit()
        assert "users.csv" not in calls
