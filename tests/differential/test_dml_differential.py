"""DML を両エンジンで実行し、実行後の SELECT * で状態を突き合わせる。"""

import sqlite3

import pytest

import iceql
from iceql.errors import IntegrityError

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
    # INTEGER PRIMARY KEY の自動採番(sqlite の rowid 別名と同じ規則)
    ["INSERT INTO t (name) VALUES ('e')"],
    ["INSERT INTO t (name) VALUES ('e'), ('f'), ('g')"],
    ["INSERT INTO t VALUES (NULL, 'e', NULL, NULL)"],
    [
        "INSERT INTO t (id, name) VALUES (100, 'e')",
        "INSERT INTO t (name) VALUES ('f')",
    ],
    ["INSERT INTO t VALUES (7, 'e', NULL, NULL), (NULL, 'f', NULL, NULL)"],
    [
        "DELETE FROM t",
        "INSERT INTO t (name) VALUES ('e')",
    ],
    [
        "DELETE FROM t WHERE id = 4",
        "INSERT INTO t (name) VALUES ('e')",
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


CONSTRAINT_SETUP = [
    "CREATE TABLE c (id INTEGER PRIMARY KEY, e TEXT UNIQUE, n INTEGER CHECK (n > 0), "
    "a INTEGER, b INTEGER, UNIQUE (a, b))",
    "INSERT INTO c VALUES (1, 'x', 1, 1, 1), (2, 'y', 2, 1, 2), (3, NULL, NULL, NULL, NULL)",
]

CONSTRAINT_CASES = [
    "INSERT INTO c VALUES (4, 'x', 1, 2, 1)",
    "INSERT INTO c VALUES (4, 'z', 0, 2, 1)",
    "INSERT INTO c VALUES (4, 'z', 1, 1, 2)",
    # NULL を含むキーは重複とみなさず、NULL の CHECK も通る
    "INSERT INTO c VALUES (4, NULL, NULL, NULL, NULL)",
    "INSERT INTO c VALUES (4, 'z', 1, 1, NULL)",
    "UPDATE c SET e = 'y' WHERE id = 1",
    "UPDATE c SET n = 0 WHERE id = 1",
    "UPDATE c SET n = n + 1",
    # 同じ値で上書きする更新は重複にならない
    "UPDATE c SET e = e WHERE id = 1",
    "DELETE FROM c WHERE id = 1",
]


@pytest.mark.parametrize("sql", CONSTRAINT_CASES)
def test_constraint_violations_match_sqlite(tmp_path, sql):
    lite = sqlite3.connect(":memory:")
    ice = iceql.connect(tmp_path / "db")
    for setup in CONSTRAINT_SETUP:
        lite.execute(setup)
        ice.execute(setup)

    expected_error = None
    try:
        lite.execute(sql)
    except sqlite3.IntegrityError as exc:
        expected_error = str(exc)
    actual_error = None
    try:
        ice.execute(sql)
    except IntegrityError as exc:
        actual_error = str(exc)
    assert actual_error == expected_error, sql

    expected = lite.execute("SELECT * FROM c ORDER BY id").fetchall()
    actual = ice.execute("SELECT * FROM c ORDER BY id").fetchall()
    assert actual == expected
    lite.close()
    ice.close()
