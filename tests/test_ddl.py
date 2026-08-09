import pytest

import iceql
from iceql.errors import (
    DataError,
    IntegrityError,
    NotSupportedError,
    ProgrammingError,
)


@pytest.fixture
def db(tmp_path):
    conn = iceql.connect(tmp_path / "db")
    yield conn
    conn.close()


CREATE_ITEMS = """
CREATE TABLE items (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    price REAL DEFAULT 0.0,
    ok BOOLEAN,
    added DATE,
    ts DATETIME
)
"""


class TestCreate:
    def test_create_and_use(self, db):
        db.execute(CREATE_ITEMS)
        db.execute("INSERT INTO items (id, name) VALUES (1, 'pen')")
        row = db.execute("SELECT id, name, price, ok FROM items").fetchone()
        assert row == (1, "pen", 0.0, None)

    def test_schema_file_contents(self, db):
        db.execute(CREATE_ITEMS)
        text = (db._catalog.root / "items.schema.yaml").read_text(encoding="utf-8")
        assert "table: items" in text
        assert "primary_key: true" in text
        assert "type: real" in text

    def test_create_duplicate(self, db):
        db.execute(CREATE_ITEMS)
        with pytest.raises(ProgrammingError, match="already exists"):
            db.execute(CREATE_ITEMS)
        db.execute(CREATE_ITEMS.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS"))

    def test_composite_pk(self, db):
        db.execute("CREATE TABLE m (a INTEGER, b INTEGER, v TEXT, PRIMARY KEY (a, b))")
        db.execute("INSERT INTO m VALUES (1, 1, 'x'), (1, 2, 'y')")
        with pytest.raises(IntegrityError):
            db.execute("INSERT INTO m VALUES (1, 2, 'dup')")

    def test_varchar_maps_to_text(self, db):
        db.execute("CREATE TABLE v (s VARCHAR(10))")
        schema = db._catalog.load_schema("v")
        assert schema.columns[0].type == "text"

    def test_unsupported_type(self, db):
        with pytest.raises(NotSupportedError, match="column type"):
            db.execute("CREATE TABLE b (data BLOB)")

    def test_create_index_unsupported(self, db):
        db.execute("CREATE TABLE t (a INTEGER)")
        with pytest.raises(NotSupportedError, match="INDEX"):
            db.execute("CREATE INDEX idx ON t (a)")

    def test_rowid_reserved(self, db):
        with pytest.raises(ProgrammingError, match="reserved"):
            db.execute("CREATE TABLE t (_rowid_ INTEGER)")

    def test_autoincrement_accepted(self, db):
        db.execute("CREATE TABLE u (id INTEGER PRIMARY KEY AUTOINCREMENT, n TEXT)")
        db.execute("INSERT INTO u (n) VALUES ('a'), ('b')")
        assert db.execute("SELECT * FROM u ORDER BY id").fetchall() == [
            (1, "a"),
            (2, "b"),
        ]

    def test_autoincrement_is_not_stored_in_the_schema(self, db):
        # 採番の有無は「単一の integer 主キーか」で決まるので、記録する必要がない
        db.execute("CREATE TABLE u (id INTEGER PRIMARY KEY AUTOINCREMENT, n TEXT)")
        text = (db._catalog.root / "u.schema.yaml").read_text(encoding="utf-8")
        assert "autoincrement" not in text

    def test_autoincrement_on_text_pk_rejected(self, db):
        with pytest.raises(ProgrammingError, match="AUTOINCREMENT"):
            db.execute("CREATE TABLE u (id TEXT PRIMARY KEY AUTOINCREMENT)")

    def test_autoincrement_without_pk_rejected(self, db):
        with pytest.raises(ProgrammingError, match="AUTOINCREMENT"):
            db.execute("CREATE TABLE u (id INTEGER AUTOINCREMENT, n TEXT)")


class TestDrop:
    def test_drop(self, db):
        db.execute("CREATE TABLE t (a INTEGER)")
        db.execute("DROP TABLE t")
        with pytest.raises(ProgrammingError, match="no such table"):
            db.execute("SELECT * FROM t")

    def test_drop_missing(self, db):
        with pytest.raises(ProgrammingError, match="no such table"):
            db.execute("DROP TABLE nope")
        db.execute("DROP TABLE IF EXISTS nope")


class TestAlter:
    @pytest.fixture(autouse=True)
    def _items(self, db):
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        db.execute("INSERT INTO t VALUES (1, 'a'), (2, 'b')")

    def test_add_column_with_default(self, db):
        db.execute("ALTER TABLE t ADD COLUMN score REAL DEFAULT 1.5")
        assert db.execute("SELECT score FROM t WHERE id = 1").fetchone() == (1.5,)

    def test_add_nullable_column(self, db):
        db.execute("ALTER TABLE t ADD COLUMN note TEXT")
        assert db.execute("SELECT note FROM t WHERE id = 1").fetchone() == (None,)

    def test_add_not_null_without_default_rejected(self, db):
        with pytest.raises(IntegrityError, match="DEFAULT"):
            db.execute("ALTER TABLE t ADD COLUMN x INTEGER NOT NULL")

    def test_drop_column(self, db):
        db.execute("ALTER TABLE t ADD COLUMN tmp TEXT")
        db.execute("ALTER TABLE t DROP COLUMN tmp")
        assert db._catalog.load_schema("t").column_names == ["id", "name"]

    def test_rename_column(self, db):
        db.execute("ALTER TABLE t RENAME COLUMN name TO title")
        assert db.execute("SELECT title FROM t WHERE id = 1").fetchone() == ("a",)

    def test_rename_table(self, db):
        db.execute("ALTER TABLE t RENAME TO s")
        assert db.execute("SELECT COUNT(*) FROM s").fetchone() == (2,)
        with pytest.raises(ProgrammingError):
            db.execute("SELECT * FROM t")

    def test_add_duplicate_column(self, db):
        with pytest.raises(ProgrammingError, match="already exists"):
            db.execute("ALTER TABLE t ADD COLUMN name TEXT")

    def test_csv_rewritten_on_alter(self, db):
        db.execute("ALTER TABLE t ADD COLUMN score REAL DEFAULT 2.0")
        text = (db._catalog.root / "t.csv").read_text(encoding="utf-8")
        assert text == "id,name,score\n1,a,2.0\n2,b,2.0\n"


class TestUniqueAndCheck:
    def test_column_unique(self, db):
        db.execute("CREATE TABLE u (id INTEGER PRIMARY KEY, e TEXT UNIQUE)")
        db.execute("INSERT INTO u VALUES (1, 'a')")
        with pytest.raises(IntegrityError, match=r"UNIQUE constraint failed: u\.e"):
            db.execute("INSERT INTO u VALUES (2, 'a')")

    def test_table_unique(self, db):
        db.execute("CREATE TABLE u (a INTEGER, b INTEGER, UNIQUE (a, b))")
        db.execute("INSERT INTO u VALUES (1, 1), (1, 2)")
        with pytest.raises(IntegrityError, match=r"u\.a, u\.b"):
            db.execute("INSERT INTO u VALUES (1, 2)")

    def test_unique_ignores_null_keys(self, db):
        # sqlite と同じで、NULL どうしは重複とみなさない
        db.execute("CREATE TABLE u (a INTEGER, b INTEGER, UNIQUE (a, b))")
        db.execute("INSERT INTO u VALUES (NULL, NULL), (NULL, NULL), (1, NULL), (1, NULL)")
        assert db.execute("SELECT COUNT(*) FROM u").fetchone() == (4,)

    def test_column_check(self, db):
        db.execute("CREATE TABLE u (id INTEGER PRIMARY KEY, n INTEGER CHECK (n > 0))")
        db.execute("INSERT INTO u VALUES (1, 5)")
        with pytest.raises(IntegrityError, match=r"CHECK constraint failed: n > 0"):
            db.execute("INSERT INTO u VALUES (2, 0)")

    def test_check_passes_null(self, db):
        db.execute("CREATE TABLE u (id INTEGER PRIMARY KEY, n INTEGER CHECK (n > 0))")
        db.execute("INSERT INTO u VALUES (1, NULL)")
        assert db.execute("SELECT n FROM u").fetchall() == [(None,)]

    def test_table_check_over_two_columns(self, db):
        db.execute("CREATE TABLE u (lo INTEGER, hi INTEGER, CHECK (lo <= hi))")
        db.execute("INSERT INTO u VALUES (1, 2)")
        with pytest.raises(IntegrityError, match="CHECK"):
            db.execute("INSERT INTO u VALUES (3, 2)")

    def test_named_constraints_are_reported_by_name(self, db):
        db.execute(
            "CREATE TABLE u (id INTEGER PRIMARY KEY, n INTEGER, "
            "CONSTRAINT ck_positive CHECK (n > 0))"
        )
        with pytest.raises(IntegrityError, match="ck_positive"):
            db.execute("INSERT INTO u VALUES (1, -1)")

    def test_constraints_are_stored_in_the_schema(self, db):
        db.execute(
            "CREATE TABLE u (id INTEGER PRIMARY KEY, e TEXT UNIQUE, n INTEGER, "
            "CONSTRAINT ck CHECK (n > 0))"
        )
        schema = db._catalog.load_schema("u")
        assert [u.columns for u in schema.unique] == [["e"]]
        assert [(c.name, c.expr) for c in schema.checks] == [("ck", "n > 0")]

    def test_constraints_survive_reconnect(self, db, tmp_path):
        db.execute("CREATE TABLE u (id INTEGER PRIMARY KEY, n INTEGER CHECK (n > 0))")
        db.close()
        again = iceql.connect(tmp_path / "db")
        with pytest.raises(IntegrityError, match="CHECK"):
            again.execute("INSERT INTO u VALUES (1, -1)")
        again.close()

    def test_update_is_checked(self, db):
        db.execute(
            "CREATE TABLE u (id INTEGER PRIMARY KEY, e TEXT UNIQUE, n INTEGER CHECK (n > 0))"
        )
        db.execute("INSERT INTO u VALUES (1, 'a', 1), (2, 'b', 2)")
        with pytest.raises(IntegrityError, match="UNIQUE"):
            db.execute("UPDATE u SET e = 'a' WHERE id = 2")
        with pytest.raises(IntegrityError, match="CHECK"):
            db.execute("UPDATE u SET n = 0 WHERE id = 2")
        assert db.execute("SELECT e, n FROM u WHERE id = 2").fetchone() == ("b", 2)

    def test_update_of_other_rows_ignores_existing_violations(self, db, tmp_path):
        # 手編集で違反した行があっても、無関係な UPDATE は通す(sqlite と同じ)
        db.execute("CREATE TABLE u (id INTEGER PRIMARY KEY, n INTEGER CHECK (n > 0))")
        db.execute("INSERT INTO u VALUES (1, 1), (2, 2)")
        (tmp_path / "db" / "u.csv").write_text("id,n\n1,-1\n2,2\n", encoding="utf-8")
        db.execute("UPDATE u SET n = 5 WHERE id = 2")
        assert db.execute("SELECT n FROM u ORDER BY id").fetchall() == [(-1,), (5,)]

    def test_check_cannot_reference_another_table(self, db):
        db.execute("CREATE TABLE other (id INTEGER PRIMARY KEY)")
        with pytest.raises(NotSupportedError, match="another table"):
            db.execute("CREATE TABLE u (n INTEGER CHECK (other.id > 0))")

    def test_check_rejects_unknown_column(self, db):
        with pytest.raises(DataError, match="no such column"):
            db.execute("CREATE TABLE u (n INTEGER CHECK (m > 0))")

    def test_check_rejects_subquery(self, db):
        with pytest.raises(NotSupportedError, match="subquer"):
            db.execute("CREATE TABLE u (n INTEGER CHECK (n IN (SELECT 1)))")

    def test_check_rejects_aggregate(self, db):
        with pytest.raises(NotSupportedError, match="aggregate"):
            db.execute("CREATE TABLE u (n INTEGER CHECK (COUNT(n) > 0))")

    def test_foreign_key_is_rejected_with_a_clear_error(self, db):
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        with pytest.raises(NotSupportedError, match="FOREIGN KEY"):
            db.execute("CREATE TABLE u (t_id INTEGER REFERENCES t(id))")
        with pytest.raises(NotSupportedError, match="FOREIGN KEY"):
            db.execute("CREATE TABLE u (t_id INTEGER, FOREIGN KEY (t_id) REFERENCES t(id))")

    def test_add_column_with_check(self, db):
        db.execute("CREATE TABLE u (id INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO u VALUES (1)")
        db.execute("ALTER TABLE u ADD COLUMN n INTEGER CHECK (n > 0)")
        with pytest.raises(IntegrityError, match="CHECK"):
            db.execute("UPDATE u SET n = -1 WHERE id = 1")

    def test_add_column_with_check_rejects_violating_default(self, db):
        db.execute("CREATE TABLE u (id INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO u VALUES (1)")
        with pytest.raises(IntegrityError, match="CHECK"):
            db.execute("ALTER TABLE u ADD COLUMN n INTEGER DEFAULT 0 CHECK (n > 0)")

    def test_add_unique_column_rejected(self, db):
        db.execute("CREATE TABLE u (id INTEGER PRIMARY KEY)")
        with pytest.raises(NotSupportedError, match="UNIQUE"):
            db.execute("ALTER TABLE u ADD COLUMN e TEXT UNIQUE")

    def test_drop_column_used_by_a_constraint_rejected(self, db):
        db.execute(
            "CREATE TABLE u (id INTEGER PRIMARY KEY, e TEXT UNIQUE, n INTEGER CHECK (n > 0))"
        )
        with pytest.raises(ProgrammingError, match="used by constraint"):
            db.execute("ALTER TABLE u DROP COLUMN e")
        with pytest.raises(ProgrammingError, match="used by constraint"):
            db.execute("ALTER TABLE u DROP COLUMN n")

    def test_rename_column_follows_constraints(self, db):
        db.execute(
            "CREATE TABLE u (id INTEGER PRIMARY KEY, e TEXT UNIQUE, n INTEGER CHECK (n > 0))"
        )
        db.execute("ALTER TABLE u RENAME COLUMN e TO mail")
        db.execute("ALTER TABLE u RENAME COLUMN n TO num")
        schema = db._catalog.load_schema("u")
        assert [u.columns for u in schema.unique] == [["mail"]]
        assert [c.expr for c in schema.checks] == ["num > 0"]
        db.execute("INSERT INTO u VALUES (1, 'a', 1)")
        with pytest.raises(IntegrityError, match=r"u\.mail"):
            db.execute("INSERT INTO u VALUES (2, 'a', 1)")
        with pytest.raises(IntegrityError, match="CHECK"):
            db.execute("INSERT INTO u VALUES (3, 'b', 0)")

    def test_rename_table_keeps_constraints(self, db):
        db.execute("CREATE TABLE u (id INTEGER PRIMARY KEY, n INTEGER CHECK (n > 0))")
        db.execute("ALTER TABLE u RENAME TO v")
        with pytest.raises(IntegrityError, match="CHECK"):
            db.execute("INSERT INTO v VALUES (1, -1)")

    def test_qualified_check_survives_a_table_rename(self, db):
        db.execute("CREATE TABLE u (id INTEGER PRIMARY KEY, n INTEGER CHECK (u.n > 0))")
        assert db._catalog.load_schema("u").checks[0].expr == "n > 0"
        db.execute("ALTER TABLE u RENAME TO v")
        with pytest.raises(IntegrityError, match="CHECK"):
            db.execute("INSERT INTO v VALUES (1, -1)")


class TestEndToEnd:
    def test_full_lifecycle(self, db):
        db.execute(
            "CREATE TABLE logs (id INTEGER PRIMARY KEY, msg TEXT, level TEXT DEFAULT 'info')"
        )
        db.execute("INSERT INTO logs (id, msg) VALUES (1, 'boot'), (2, 'ready')")
        db.execute("UPDATE logs SET level = 'warn' WHERE id = 2")
        db.execute("ALTER TABLE logs ADD COLUMN seen BOOLEAN DEFAULT FALSE")
        db.execute("DELETE FROM logs WHERE id = 1")
        rows = db.execute("SELECT id, msg, level, seen FROM logs").fetchall()
        assert rows == [(2, "ready", "warn", False)]
        db.execute("ALTER TABLE logs RENAME TO history")
        db.execute("DROP TABLE history")
        assert db._catalog.list_tables() == []
