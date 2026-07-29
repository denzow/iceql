"""DML を両エンジンで実行し、実行後の SELECT * で状態を突き合わせる。"""

import sqlite3

import pytest

import iceql

SETUP = [
    "CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT NOT NULL, score REAL, tag TEXT)",
    "INSERT INTO t (id, name, score, tag) VALUES "
    "(1, 'a', 1.5, 'x'), (2, 'b', NULL, 'y'), (3, 'c', -0.5, NULL), (4, 'd', 2.0, 'x')",
]

DML_CASES = [
    ["INSERT INTO t (id, name, score, tag) VALUES (5, 'e', 9.5, 'z')"],
    ["INSERT INTO t (id, name) VALUES (6, 'f')"],
    ["UPDATE t SET score = 0.0 WHERE score IS NULL"],
    ["UPDATE t SET score = score * 2 WHERE score IS NOT NULL"],
    ["UPDATE t SET tag = 'w', score = NULL WHERE id IN (1, 3)"],
    ["UPDATE t SET name = name || '!' WHERE tag = 'x'"],
    ["DELETE FROM t WHERE tag IS NULL"],
    ["DELETE FROM t WHERE score < 1.0"],
    ["DELETE FROM t"],
    [
        "INSERT INTO t (id, name, tag) VALUES (10, 'j', 'q')",
        "UPDATE t SET score = 5.0 WHERE id = 10",
        "DELETE FROM t WHERE id <= 2",
    ],
]


def normalize_rows(rows):
    return [
        tuple(round(v, 9) if isinstance(v, float) else v for v in row) for row in rows
    ]


@pytest.mark.parametrize("statements", DML_CASES)
def test_dml_state_matches_sqlite(tmp_path, statements):
    lite = sqlite3.connect(":memory:")
    ice = iceql.connect(tmp_path / "db")
    for sql in SETUP + statements:
        expected_count = lite.execute(sql).rowcount
        actual_count = ice.execute(sql).rowcount
        if sql.startswith(("UPDATE", "DELETE", "INSERT")):
            assert actual_count == expected_count, f"rowcount mismatch for: {sql}"
    expected = normalize_rows(lite.execute("SELECT * FROM t ORDER BY id").fetchall())
    actual = normalize_rows(ice.execute("SELECT * FROM t ORDER BY id").fetchall())
    assert actual == expected
    lite.close()
    ice.close()
