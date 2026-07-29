import pytest

from iceql.catalog import Catalog, init_database
from iceql.errors import OperationalError, ProgrammingError
from iceql.schema import Column, TableSchema


@pytest.fixture
def db(tmp_path):
    return init_database(tmp_path / "db")


def users_schema() -> TableSchema:
    return TableSchema(
        table="users",
        columns=[
            Column(name="id", type="integer", primary_key=True),
            Column(name="name", type="text", nullable=False),
        ],
    )


def test_missing_dir_raises(tmp_path):
    with pytest.raises(OperationalError):
        Catalog(tmp_path / "nope")


def test_create_and_list(db):
    db.create_table(users_schema())
    assert db.list_tables() == ["users"]
    assert db.read_rows("users") == []
    assert (db.root / "users.csv").read_text() == "id,name\n"


def test_create_duplicate(db):
    db.create_table(users_schema())
    with pytest.raises(ProgrammingError):
        db.create_table(users_schema())
    db.create_table(users_schema(), if_not_exists=True)  # エラーにならない


def test_write_and_read_rows(db):
    schema = users_schema()
    db.create_table(schema)
    rows = [{"id": 1, "name": "alice"}, {"id": 2, "name": "bob"}]
    db.write_rows("users", rows, schema)
    assert db.read_rows("users") == rows


def test_load_schema_no_such_table(db):
    with pytest.raises(ProgrammingError, match="no such table"):
        db.load_schema("nope")


def test_schema_table_name_mismatch(db):
    db.create_table(users_schema())
    (db.root / "users.schema.yaml").write_text(
        (db.root / "users.schema.yaml").read_text().replace("table: users", "table: other")
    )
    with pytest.raises(OperationalError, match="does not match"):
        db.load_schema("users")


def test_drop_table(db):
    db.create_table(users_schema())
    db.drop_table("users")
    assert db.list_tables() == []
    with pytest.raises(ProgrammingError):
        db.drop_table("users")
    db.drop_table("users", if_exists=True)


def test_rename_table(db):
    schema = users_schema()
    db.create_table(schema)
    db.write_rows("users", [{"id": 1, "name": "alice"}], schema)
    db.rename_table("users", "members")
    assert db.list_tables() == ["members"]
    assert db.read_rows("members") == [{"id": 1, "name": "alice"}]


def test_path_traversal_rejected(db):
    from iceql.errors import DataError

    with pytest.raises(DataError):
        db.csv_path("../evil")
