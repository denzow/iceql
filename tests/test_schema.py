import pytest

from iceql.errors import DataError, IntegrityError, OperationalError
from iceql.schema import (
    CheckConstraint,
    Column,
    TableSchema,
    UniqueConstraint,
    dump_schema,
    load_schema,
)


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

    def test_autoincrement_column_is_single_integer_pk(self):
        column = make_schema().autoincrement_column
        assert column is not None and column.name == "id"

    def test_no_autoincrement_for_text_pk(self):
        schema = TableSchema(
            table="t",
            columns=[Column(name="k", type="text", primary_key=True)],
        )
        assert schema.autoincrement_column is None

    def test_no_autoincrement_for_composite_pk(self):
        schema = TableSchema(
            table="t",
            columns=[
                Column(name="a", type="integer", primary_key=True),
                Column(name="b", type="integer", primary_key=True),
            ],
        )
        assert schema.autoincrement_column is None

    def test_no_autoincrement_without_pk(self):
        schema = TableSchema(table="t", columns=[Column(name="a", type="integer")])
        assert schema.autoincrement_column is None


class TestValidateValues:
    def test_normalizes_values(self):
        assert make_schema().validate_values([1, "alice", None, True]) == (
            1,
            "alice",
            None,
            True,
        )

    def test_length_mismatch(self):
        with pytest.raises(DataError, match="4 columns"):
            make_schema().validate_values([1, "alice"])

    def test_not_null_violation(self):
        with pytest.raises(IntegrityError):
            make_schema().validate_values([1, None, None, True])

    def test_type_check(self):
        with pytest.raises(DataError):
            make_schema().validate_values(["abc", "a", None, True])


class TestBuildRow:
    def test_applies_default_and_order(self):
        row = make_schema().build_row(["name", "id"], ["alice", 1])
        assert row == (1, "alice", None, True)

    def test_unknown_column(self):
        with pytest.raises(DataError, match="no such column"):
            make_schema().build_row(["id", "name", "nope"], [1, "a", 1])

    def test_duplicate_column(self):
        with pytest.raises(DataError, match="more than once"):
            make_schema().build_row(["id", "id", "name"], [1, 2, "a"])

    def test_column_index(self):
        assert make_schema().column_index("bio") == 2

    def test_arrange_row_skips_validation(self):
        # 採番を挟めるよう、NOT NULL の主キーが NULL のままでも通る
        assert make_schema().arrange_row(["name"], ["alice"]) == [
            None,
            "alice",
            None,
            True,
        ]


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


CONSTRAINED = TableSchema(
    table="u",
    columns=[
        Column(name="id", type="integer", primary_key=True),
        Column(name="e", type="text"),
        Column(name="n", type="integer"),
    ],
    unique=[UniqueConstraint(columns=["e"]), UniqueConstraint(columns=["e", "n"], name="uq")],
    checks=[CheckConstraint(expr="n > 0"), CheckConstraint(expr="n < 100", name="ck")],
)


class TestConstraints:
    def test_roundtrip(self):
        loaded = load_schema(dump_schema(CONSTRAINED))
        assert loaded == CONSTRAINED

    def test_omitted_when_empty(self):
        text = dump_schema(make_schema())
        assert "unique:" not in text
        assert "checks:" not in text

    def test_schema_without_constraints_still_loads(self):
        # 既存のスキーマファイル(unique / checks なし)がそのまま読める
        loaded = load_schema(dump_schema(make_schema()))
        assert loaded.unique == [] and loaded.checks == []

    def test_unique_rejects_unknown_column(self):
        with pytest.raises(DataError, match="no such column"):
            TableSchema(
                table="u",
                columns=[Column(name="a", type="integer")],
                unique=[UniqueConstraint(columns=["b"])],
            )

    def test_check_rejects_unknown_column(self):
        with pytest.raises(DataError, match="no such column"):
            TableSchema(
                table="u",
                columns=[Column(name="a", type="integer")],
                checks=[CheckConstraint(expr="b > 0")],
            )

    def test_load_rejects_unknown_constraint_key(self):
        text = (
            "version: 1\ntable: u\ncolumns:\n  - {name: a, type: integer}\n"
            "unique:\n  - {columns: [a], oops: 1}\n"
        )
        with pytest.raises(OperationalError, match="unknown keys"):
            load_schema(text)

    def test_load_rejects_broken_check_expression(self):
        text = (
            "version: 1\ntable: u\ncolumns:\n  - {name: a, type: integer}\n"
            "checks:\n  - {expr: 'a >'}\n"
        )
        with pytest.raises(OperationalError):
            load_schema(text)

    def test_renamed_keeps_constraints(self):
        renamed = CONSTRAINED.renamed("v")
        assert renamed.table == "v"
        assert renamed.unique == CONSTRAINED.unique
        assert renamed.checks == CONSTRAINED.checks
