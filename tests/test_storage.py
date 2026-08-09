import pytest

from iceql.errors import OperationalError
from iceql.schema import Column, TableSchema
from iceql.storage import encode_rows, read_rows, write_rows

SCHEMA = TableSchema(
    table="t",
    columns=[
        Column(name="id", type="integer", primary_key=True),
        Column(name="note", type="text"),
        Column(name="score", type="real"),
    ],
)

ROWS = [
    (1, "hello", 1.5),
    (2, None, None),
    (3, "", -0.25),
    (4, "a,b \"quoted\"\nmultiline", 0.0),
    (5, "\\N", 2.0),
]


def test_roundtrip(tmp_path):
    path = tmp_path / "t.csv"
    write_rows(path, ROWS, SCHEMA)
    assert read_rows(path, SCHEMA) == ROWS


def test_canonical_form():
    text = encode_rows(ROWS[:2], SCHEMA)
    assert text == 'id,note,score\n1,hello,1.5\n2,\\N,\\N\n'


def test_null_vs_empty_string_distinct(tmp_path):
    path = tmp_path / "t.csv"
    write_rows(path, ROWS, SCHEMA)
    back = read_rows(path, SCHEMA)
    assert back[1][1] is None
    assert back[2][1] == ""


def test_crlf_accepted(tmp_path):
    path = tmp_path / "t.csv"
    path.write_text("id,note,score\r\n1,x,1.0\r\n", encoding="utf-8")
    assert read_rows(path, SCHEMA) == [(1, "x", 1.0)]


def test_header_mismatch(tmp_path):
    path = tmp_path / "t.csv"
    path.write_text("id,wrong,score\n", encoding="utf-8")
    with pytest.raises(OperationalError, match="header"):
        read_rows(path, SCHEMA)


def test_field_count_mismatch(tmp_path):
    path = tmp_path / "t.csv"
    path.write_text("id,note,score\n1,x\n", encoding="utf-8")
    with pytest.raises(OperationalError, match="line 2"):
        read_rows(path, SCHEMA)


def test_bad_value_reports_line_and_column(tmp_path):
    path = tmp_path / "t.csv"
    path.write_text("id,note,score\nabc,x,1.0\n", encoding="utf-8")
    with pytest.raises(OperationalError, match="line 2.*'id'"):
        read_rows(path, SCHEMA)


def test_missing_header(tmp_path):
    path = tmp_path / "t.csv"
    path.write_text("", encoding="utf-8")
    with pytest.raises(OperationalError, match="header"):
        read_rows(path, SCHEMA)


def test_atomic_write_leaves_no_tmp(tmp_path):
    path = tmp_path / "t.csv"
    write_rows(path, ROWS, SCHEMA)
    assert [p.name for p in tmp_path.iterdir()] == ["t.csv"]
