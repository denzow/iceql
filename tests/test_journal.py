"""redo ジャーナルによる COMMIT のクラッシュ耐性テスト。"""

import json

import pytest

import iceql
from iceql.catalog import Catalog
from iceql.check import check_database
from iceql.errors import OperationalError
from iceql.schema import dump_schema
from iceql.storage import encode_rows


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
    """Catalog.write_table の 2 回目の呼び出しで「クラッシュ」させる。"""
    original = Catalog.write_table
    calls = {"n": 0}

    def crashing(self, schema, rows):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise CrashError("simulated crash between replaces")
        original(self, schema, rows)

    monkeypatch.setattr(Catalog, "write_table", crashing)


def crash_on_drop(monkeypatch):
    """Catalog.drop_table の呼び出しで「クラッシュ」させる(置換の後、削除の前)。"""

    def crashing(self, table, *, if_exists=False):
        raise CrashError("simulated crash before drop")

    monkeypatch.setattr(Catalog, "drop_table", crashing)


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
            lambda *args, **kwargs: (_ for _ in ()).throw(CrashError("crash")),
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
        journal.write_journal(path, {"t1": (schema, [(1, 99)])})
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


class TestJournalWithDdl:
    """DDL を含むトランザクションのジャーナル(version 2)。"""

    def test_dropped_tables_are_recorded_and_recovered(self, db, monkeypatch):
        path, conn = db
        conn.execute("BEGIN")
        conn.execute("INSERT INTO t1 VALUES (2, 12)")
        conn.execute("DROP TABLE t2")
        crash_on_drop(monkeypatch)
        with pytest.raises(CrashError):
            conn.execute("COMMIT")
        monkeypatch.undo()

        # 置換だけ済み、削除が残った途中状態
        payload = json.loads(journal_file(path).read_text(encoding="utf-8"))
        assert payload["version"] == 2
        assert payload["dropped"] == ["t2"]
        assert (path / "t2.csv").exists()

        conn2 = iceql.connect(path)
        assert conn2.execute("SELECT COUNT(*) FROM t1").fetchone() == (2,)
        assert not (path / "t2.csv").exists()
        assert not (path / "t2.schema.yaml").exists()
        assert not journal_file(path).exists()
        assert check_database(path) == []
        conn2.close()

    def test_rename_crash_recovers(self, db, monkeypatch):
        path, conn = db
        conn.execute("BEGIN")
        conn.execute("ALTER TABLE t2 RENAME TO t3")
        crash_on_drop(monkeypatch)
        with pytest.raises(CrashError):
            conn.execute("COMMIT")
        monkeypatch.undo()

        # 新名は置かれ、旧名の削除だけが残っている
        assert (path / "t3.csv").exists()
        assert (path / "t2.csv").exists()

        conn2 = iceql.connect(path)
        assert conn2.execute("SELECT v FROM t3").fetchone() == (20,)
        assert not (path / "t2.csv").exists()
        assert not (path / "t2.schema.yaml").exists()
        assert check_database(path) == []
        conn2.close()

    def test_created_table_survives_crash(self, db, monkeypatch):
        path, conn = db
        conn.execute("BEGIN")
        conn.execute("CREATE TABLE t3 (id INTEGER PRIMARY KEY, v INTEGER)")
        conn.execute("INSERT INTO t3 VALUES (1, 30)")
        conn.execute("UPDATE t1 SET v = 111")
        crash_on_second_write(monkeypatch)
        with pytest.raises(CrashError):
            conn.execute("COMMIT")
        monkeypatch.undo()
        assert journal_file(path).exists()

        conn2 = iceql.connect(path)
        assert conn2.execute("SELECT v FROM t3").fetchone() == (30,)
        assert conn2.execute("SELECT v FROM t1").fetchone() == (111,)
        assert check_database(path) == []
        conn2.close()

    def test_crash_between_csv_and_schema_recovers(self, db, monkeypatch):
        """issue #12 の動機となるケース: ALTER とデータ移行の途中でクラッシュする。"""
        from iceql import storage

        path, conn = db
        conn.execute("BEGIN")
        conn.execute("ALTER TABLE t1 ADD COLUMN w INTEGER")
        conn.execute("UPDATE t1 SET w = v * 2")

        original = storage.atomic_write

        def crashing(target, content):
            if target.name.endswith(".schema.yaml"):
                raise CrashError("simulated crash between CSV and schema")
            original(target, content)

        monkeypatch.setattr("iceql.storage.atomic_write", crashing)
        with pytest.raises(CrashError):
            conn.execute("COMMIT")
        monkeypatch.undo()

        # CSV だけ新しい列を持ち、スキーマは古いままの食い違った状態
        assert any(issue.severity == "error" for issue in check_database(path))

        conn2 = iceql.connect(path)  # 接続時にジャーナルを再適用して完成させる
        assert conn2.execute("SELECT v, w FROM t1").fetchone() == (10, 20)
        assert check_database(path) == []
        conn2.close()

    def test_version_1_journal_is_applied(self, db):
        path, conn = db
        schema = conn._catalog.load_schema("t1")
        payload = {
            "version": 1,  # 削除を持たない旧形式
            "tables": {
                "t1": {
                    "schema": dump_schema(schema),
                    "csv": encode_rows([(1, 77)], schema),
                }
            },
        }
        journal_file(path).write_text(json.dumps(payload), encoding="utf-8")
        conn2 = iceql.connect(path)
        assert conn2.execute("SELECT v FROM t1").fetchone() == (77,)
        assert not journal_file(path).exists()
        conn2.close()

    def test_unknown_version_raises(self, db):
        path, conn = db
        journal_file(path).write_text('{"version": 99, "tables": {}}', encoding="utf-8")
        with pytest.raises(OperationalError, match="unsupported commit journal version"):
            iceql.connect(path)

    def test_invalid_dropped_list_raises(self, db):
        path, conn = db
        journal_file(path).write_text(
            '{"version": 2, "tables": {}, "dropped": [1]}', encoding="utf-8"
        )
        with pytest.raises(OperationalError, match="dropped"):
            iceql.connect(path)
