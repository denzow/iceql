import pytest

from iceql.errors import DataError, IntegrityError, OperationalError
from iceql.schema import Column, TableSchema, dump_schema, load_schema


def make_schema() -> TableSchema:
    return TableSchema(
        table="users",
        columns=[
            Column(name="id", type="integer", primary_key=True),
            Column(name="name", type="text", nullable=False),
            Column(name="bio", type="text"),
            Column(name="active", type="boolean", nullable=False, default=True),
        ],
    )


class TestTableSchema:
    def test_pk_implies_not_null(self):
        col = Column(name="id", type="integer", primary_key=True)
        assert col.nullable is False

    def test_rejects_bad_table_name(self):
        with pytest.raises(DataError):
            TableSchema(table="../evil", columns=[Column(name="a", type="text")])

    def test_rejects_duplicate_columns(self):
        with pytest.raises(DataError):
            TableSchema(
                table="t",
                columns=[Column(name="a", type="text"), Column(name="a", type="integer")],
            )

    def test_rejects_empty_columns(self):
        with pytest.raises(DataError):
            TableSchema(table="t", columns=[])

    def test_primary_key_property(self):
        assert make_schema().primary_key == ["id"]


class TestValidateRow:
    def test_applies_default_and_order(self):
        row = make_schema().validate_row({"name": "alice", "id": 1})
        assert list(row) == ["id", "name", "bio", "active"]
        assert row == {"id": 1, "name": "alice", "bio": None, "active": True}

    def test_not_null_violation(self):
        with pytest.raises(IntegrityError):
            make_schema().validate_row({"id": 1, "name": None})

    def test_unknown_column(self):
        with pytest.raises(DataError):
            make_schema().validate_row({"id": 1, "name": "a", "nope": 1})

    def test_type_check(self):
        with pytest.raises(DataError):
            make_schema().validate_row({"id": "abc", "name": "a"})


class TestYamlRoundtrip:
    def test_roundtrip(self):
        schema = make_schema()
        text = dump_schema(schema)
        loaded = load_schema(text)
        assert loaded == schema

    def test_dump_preserves_column_order(self):
        text = dump_schema(make_schema())
        assert text.index("name: id") < text.index("name: name") < text.index("name: bio")

    def test_load_rejects_bad_version(self):
        with pytest.raises(OperationalError):
            load_schema("version: 99\ntable: t\ncolumns: []\n")

    def test_load_rejects_unknown_column_key(self):
        text = (
            "version: 1\ntable: t\ncolumns:\n"
            "  - {name: a, type: text, oops: 1}\n"
        )
        with pytest.raises(OperationalError):
            load_schema(text)

    def test_load_rejects_invalid_yaml(self):
        with pytest.raises(OperationalError):
            load_schema("{: :")
