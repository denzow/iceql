import pytest

import iceql
from iceql.errors import IntegrityError, NotSupportedError, ProgrammingError


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
