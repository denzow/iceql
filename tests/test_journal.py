"""redo ジャーナルによる COMMIT のクラッシュ耐性テスト。"""

import pytest

import iceql
from iceql.catalog import Catalog
from iceql.check import check_database
from iceql.errors import OperationalError


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "db"
    conn = iceql.connect(path)
    conn.execute("CREATE TABLE t1 (id INTEGER PRIMARY KEY, v INTEGER)")
    conn.execute("CREATE TABLE t2 (id INTEGER PRIMARY KEY, v INTEGER)")
    conn.execute("INSERT INTO t1 VALUES (1, 10)")
    conn.execute("INSERT INTO t2 VALUES (1, 20)")
    yield path, conn
    conn.close()


def journal_file(path):
    return path / ".iceql" / "journal"


class CrashError(Exception):
    pass


def crash_on_second_write(monkeypatch):
    """Catalog.write_rows の 2 回目の呼び出しで「クラッシュ」させる。"""
    original = Catalog.write_rows
    calls = {"n": 0}

    def crashing(self, table, rows, schema):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise CrashError("simulated crash between replaces")
        original(self, table, rows, schema)

    monkeypatch.setattr(Catalog, "write_rows", crashing)


class TestCommitJournal:
    def test_successful_commit_leaves_no_journal(self, db):
        path, conn = db
        conn.execute("BEGIN")
        conn.execute("UPDATE t1 SET v = 11")
        conn.execute("COMMIT")
        assert not journal_file(path).exists()

    def test_empty_commit_writes_no_journal(self, db):
        path, conn = db
        conn.execute("BEGIN")
        conn.execute("COMMIT")
        assert not journal_file(path).exists()

    def test_crash_between_replaces_recovers_on_next_connection(self, db, monkeypatch):
        path, conn = db
        conn.execute("BEGIN")
        conn.execute("UPDATE t1 SET v = 111")
        conn.execute("UPDATE t2 SET v = 222")
        crash_on_second_write(monkeypatch)
        with pytest.raises(CrashError):
            conn.execute("COMMIT")
        monkeypatch.undo()

        # ジャーナルが残り、ディスクは片方だけ新しい「途中状態」
        assert journal_file(path).exists()

        # 新しい接続がジャーナルを再適用してコミットを完成させる
        conn2 = iceql.connect(path)
        assert conn2.execute("SELECT v FROM t1").fetchone() == (111,)
        assert conn2.execute("SELECT v FROM t2").fetchone() == (222,)
        assert not journal_file(path).exists()
        conn2.close()

    def test_crash_recovers_via_existing_connection(self, db, monkeypatch):
        path, conn = db
        other = iceql.connect(path)  # クラッシュ前から開いている接続
        conn.execute("BEGIN")
        conn.execute("UPDATE t1 SET v = 111")
        conn.execute("UPDATE t2 SET v = 222")
        crash_on_second_write(monkeypatch)
        with pytest.raises(CrashError):
            conn.execute("COMMIT")
        monkeypatch.undo()

        # 既存接続の次の文でも回復し、途中状態の JOIN を見ない
        rows = other.execute(
            "SELECT a.v, b.v FROM t1 a JOIN t2 b ON a.id = b.id"
        ).fetchall()
        assert rows == [(111, 222)]
        assert not journal_file(path).exists()
        other.close()

    def test_crash_before_journal_means_rollback(self, db, monkeypatch):
        path, conn = db
        conn.execute("BEGIN")
        conn.execute("UPDATE t1 SET v = 111")
        monkeypatch.setattr(
            "iceql.journal.write_journal",
            lambda root, staged: (_ for _ in ()).throw(CrashError("crash")),
        )
        with pytest.raises(CrashError):
            conn.execute("COMMIT")
        monkeypatch.undo()

        # ジャーナルが置かれる前のクラッシュはコミット不成立(全て旧状態)
        assert not journal_file(path).exists()
        conn2 = iceql.connect(path)
        assert conn2.execute("SELECT v FROM t1").fetchone() == (10,)
        conn2.close()

    def test_redo_is_idempotent(self, db):
        path, conn = db
        conn.execute("BEGIN")
        conn.execute("UPDATE t1 SET v = 99")
        conn.execute("COMMIT")
        # コミット済みの状態でジャーナルだけ「残っていた」状況を再現しても
        # 再適用は同じ内容の置換なので状態は変わらない
        from iceql import journal
        from iceql.schema import Column, TableSchema

        schema = TableSchema(
            table="t1",
            columns=[
                Column(name="id", type="integer", primary_key=True),
                Column(name="v", type="integer"),
            ],
        )
        journal.write_journal(path, {"t1": (schema, [{"id": 1, "v": 99}])})
        conn2 = iceql.connect(path)
        assert conn2.execute("SELECT v FROM t1").fetchone() == (99,)
        assert not journal_file(path).exists()
        conn2.close()

    def test_check_warns_on_leftover_journal(self, db):
        path, conn = db
        journal_file(path).write_text('{"version": 1, "tables": {}}', encoding="utf-8")
        issues = check_database(path)
        assert any("journal" in issue.message for issue in issues)
        assert all(issue.severity == "warning" for issue in issues)

    def test_corrupt_journal_raises(self, db):
        path, conn = db
        journal_file(path).write_text("not json", encoding="utf-8")
        with pytest.raises(OperationalError, match="corrupt"):
            iceql.connect(path)
