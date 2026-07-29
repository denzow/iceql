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
    {"id": 1, "note": "hello", "score": 1.5},
    {"id": 2, "note": None, "score": None},
    {"id": 3, "note": "", "score": -0.25},
    {"id": 4, "note": "a,b \"quoted\"\nmultiline", "score": 0.0},
    {"id": 5, "note": "\\N", "score": 2.0},
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
    assert back[1]["note"] is None
    assert back[2]["note"] == ""


def test_crlf_accepted(tmp_path):
    path = tmp_path / "t.csv"
    path.write_text("id,note,score\r\n1,x,1.0\r\n", encoding="utf-8")
    assert read_rows(path, SCHEMA) == [{"id": 1, "note": "x", "score": 1.0}]


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
