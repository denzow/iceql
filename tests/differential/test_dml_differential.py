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
    # リテラルの並びに対する IN / NOT IN(NULL 混じりの行を巻き込む形)
    ["DELETE FROM t WHERE tag NOT IN ('x')"],
    ["DELETE FROM t WHERE tag IN ('x', NULL)"],
    ["DELETE FROM t WHERE score NOT IN (1.5, NULL)"],
    ["UPDATE t SET name = name || '!' WHERE tag NOT IN ('x', 'y')"],
    ["UPDATE t SET tag = 'z' WHERE NOT (score IN (1.5))"],
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
    # UPDATE / DELETE の高速パス(WHERE が「列 = 定数」の AND だけの形)
    ["UPDATE t SET score = 9.5 WHERE id = 2"],
    ["UPDATE t SET tag = 'z' WHERE tag = 'x'"],
    ["UPDATE t SET tag = 'z' WHERE tag = 'x' AND id = 4"],
    ["UPDATE t SET score = NULL WHERE score = 1.5"],
    ["UPDATE t SET tag = 'z' WHERE score = 2"],
    ["UPDATE t SET tag = 'z' WHERE tag = NULL"],
    ["UPDATE t SET tag = name WHERE id = 3"],
    ["UPDATE t SET name = tag, tag = name WHERE id = 1"],
    ["UPDATE t SET tag = 'z'"],
    ["DELETE FROM t WHERE id = 3"],
    ["DELETE FROM t WHERE tag = 'x'"],
    ["DELETE FROM t WHERE tag = NULL"],
    ["DELETE FROM t WHERE score = -0.5"],
    [
        "UPDATE t SET tag = 'z' WHERE id = 1",
        "DELETE FROM t WHERE tag = 'z'",
        "UPDATE t SET score = 0.0 WHERE tag = 'y'",
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


SUBQUERY_SETUP = [
    "CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT NOT NULL, tag TEXT)",
    "CREATE TABLE u (id INTEGER, tag TEXT)",
    "INSERT INTO t (id, name, tag) VALUES "
    "(1, 'a', 'x'), (2, 'b', 'y'), (3, 'c', NULL), (4, 'd', 'x')",
    "INSERT INTO u (id, tag) VALUES (2, 'x'), (3, NULL), (9, 'y')",
]

# WHERE の中のサブクエリ。素通しすると DELETE が表を空にするなど、
# 誤答が読み取りより重い形で出る
SUBQUERY_DML_CASES = [
    ["DELETE FROM t WHERE id IN (SELECT id FROM u)"],
    ["DELETE FROM t WHERE id NOT IN (SELECT id FROM u)"],
    ["DELETE FROM t WHERE id NOT IN (SELECT id FROM u WHERE id > 100)"],
    # サブクエリの値に NULL が混じる / 左辺が NULL になる
    ["DELETE FROM t WHERE tag NOT IN (SELECT tag FROM u)"],
    ["DELETE FROM t WHERE tag NOT IN (SELECT tag FROM u WHERE tag IS NOT NULL)"],
    ["DELETE FROM t WHERE id IN (SELECT id FROM u ORDER BY id LIMIT 1)"],
    ["DELETE FROM t WHERE EXISTS (SELECT 1 FROM u)"],
    ["DELETE FROM t WHERE EXISTS (SELECT 1 FROM u WHERE id > 100)"],
    ["DELETE FROM t WHERE NOT EXISTS (SELECT 1 FROM u WHERE id > 100)"],
    ["DELETE FROM t WHERE NOT EXISTS (SELECT 1 FROM u WHERE u.id = t.id)"],
    ["UPDATE t SET tag = 'z' WHERE id NOT IN (SELECT id FROM u)"],
    ["UPDATE t SET tag = 'z' WHERE EXISTS (SELECT 1 FROM u WHERE u.id = t.id)"],
    ["UPDATE t SET name = name || '!' WHERE id NOT IN (SELECT id FROM u) AND tag = 'x'"],
    [
        "DELETE FROM t WHERE id NOT IN (SELECT id FROM u)",
        "UPDATE t SET tag = 'z' WHERE EXISTS (SELECT 1 FROM u)",
    ],
    # decorrelate に渡す前に均す形。rowcount にも重複や取りこぼしが出る
    ["DELETE FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.id = t.id GROUP BY u.tag)"],
    ["UPDATE t SET tag = 'z' WHERE EXISTS (SELECT 1 FROM u WHERE u.id = t.id GROUP BY u.tag)"],
    ["DELETE FROM t WHERE EXISTS (SELECT 1 FROM u WHERE t.tag IS NOT NULL AND u.id = t.id)"],
    ["DELETE FROM t WHERE NOT EXISTS (SELECT 1 FROM u WHERE t.tag IS NOT NULL AND u.id = t.id)"],
    ["UPDATE t SET tag = 'z' WHERE EXISTS (SELECT COUNT(*) FROM u WHERE u.id = t.id)"],
]


@pytest.mark.parametrize("statements", SUBQUERY_DML_CASES)
def test_subquery_dml_state_matches_sqlite(tmp_path, statements):
    lite = sqlite3.connect(":memory:")
    ice = iceql.connect(tmp_path / "db")
    for sql in SUBQUERY_SETUP + statements:
        expected_count = lite.execute(sql).rowcount
        actual_count = ice.execute(sql).rowcount
        if sql.startswith(("UPDATE", "DELETE")):
            assert actual_count == expected_count, f"rowcount mismatch for: {sql}"
    for table in ("t", "u"):
        expected = lite.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
        actual = ice.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
        assert actual == expected, f"table {table} after: {statements}"
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


CONFLICT_CASES = [
    # 競合しない行は競合解決の指定があっても普通に入る
    "INSERT OR IGNORE INTO c VALUES (4, 'z', 1, 2, 2)",
    "INSERT OR REPLACE INTO c VALUES (4, 'z', 1, 2, 2)",
    "INSERT INTO c VALUES (4, 'z', 1, 2, 2) ON CONFLICT(id) DO UPDATE SET n = 9",
    # 主キーの競合
    "INSERT OR IGNORE INTO c VALUES (1, 'z', 9, 2, 2)",
    "INSERT OR REPLACE INTO c VALUES (1, 'z', 9, 2, 2)",
    "INSERT INTO c VALUES (1, 'z', 9, 2, 2) ON CONFLICT(id) DO NOTHING",
    "INSERT INTO c VALUES (1, 'z', 9, 2, 2) ON CONFLICT DO NOTHING",
    "INSERT INTO c VALUES (1, 'z', 9, 2, 2) ON CONFLICT(id) DO UPDATE SET n = excluded.n",
    "INSERT INTO c VALUES (1, 'z', 9, 2, 2) ON CONFLICT(id) DO UPDATE SET n = n + excluded.n",
    "INSERT INTO c VALUES (1, 'z', 9, 2, 2) ON CONFLICT(id) DO UPDATE SET n = 9 WHERE c.n > 1",
    "INSERT INTO c VALUES (1, 'z', 9, 2, 2) ON CONFLICT(id) DO UPDATE SET n = 9 WHERE c.n = 1",
    # UNIQUE 列と複合 UNIQUE の競合
    "INSERT OR IGNORE INTO c VALUES (4, 'x', 9, 2, 2)",
    "INSERT OR REPLACE INTO c VALUES (4, 'x', 9, 2, 2)",
    "INSERT INTO c VALUES (4, 'x', 9, 2, 2) ON CONFLICT(e) DO UPDATE SET n = excluded.n",
    "INSERT INTO c VALUES (4, 'z', 9, 1, 1) ON CONFLICT(a, b) DO UPDATE SET n = excluded.n",
    # 対象外の制約に当たる / 主キーと UNIQUE の両方に当たる
    "INSERT INTO c VALUES (4, 'x', 9, 2, 2) ON CONFLICT(id) DO NOTHING",
    "INSERT OR REPLACE INTO c VALUES (1, 'y', 9, 2, 2)",
    "INSERT INTO c VALUES (1, 'z', 9, 2, 2) ON CONFLICT(id) DO UPDATE SET e = 'y'",
    # CHECK 違反は OR IGNORE だけが飛ばす
    "INSERT OR IGNORE INTO c VALUES (4, 'z', 0, 2, 2)",
    "INSERT INTO c VALUES (1, 'z', 0, 2, 2) ON CONFLICT(id) DO NOTHING",
    "INSERT INTO c VALUES (1, 'z', 9, 2, 2) ON CONFLICT(id) DO UPDATE SET n = 0",
    # 複数行、同じ文の中での競合、自動採番との組み合わせ
    "INSERT OR IGNORE INTO c VALUES (1, 'z', 9, 2, 2), (4, 'w', 9, 3, 3)",
    "INSERT OR REPLACE INTO c VALUES (4, 'x', 9, 2, 2), (4, 'x', 8, 2, 2)",
    "INSERT INTO c VALUES (4, 'w', 9, 3, 3), (4, 'v', 8, 4, 4) "
    "ON CONFLICT(id) DO UPDATE SET n = n + excluded.n",
    "INSERT OR IGNORE INTO c (e, n) VALUES ('x', 1), ('w', 1)",
    "INSERT INTO c (e, n) VALUES ('x', 1) ON CONFLICT(e) DO UPDATE SET n = 5",
]


RETURNING_SETUP = [
    "CREATE TABLE r (id INTEGER PRIMARY KEY, name TEXT NOT NULL, score REAL)",
    "INSERT INTO r VALUES (1, 'a', 1.5), (2, 'b', NULL), (3, 'c', -0.5)",
]

RETURNING_CASES = [
    "INSERT INTO r VALUES (4, 'd', 2.0) RETURNING id, name",
    "INSERT INTO r VALUES (4, 'd', 2.0), (5, 'e', NULL) RETURNING *",
    "INSERT INTO r (name) VALUES ('d') RETURNING id AS k, name",
    "INSERT INTO r (id, name) SELECT id + 10, name FROM r WHERE id < 3 RETURNING *",
    "UPDATE r SET score = score * 2 WHERE score IS NOT NULL RETURNING id, score",
    "UPDATE r SET name = name || '!' RETURNING id AS k, name AS n",
    "UPDATE r SET score = 1.0 WHERE id = 99 RETURNING *",
    "DELETE FROM r WHERE score IS NULL RETURNING *",
    "DELETE FROM r RETURNING id, name",
    "DELETE FROM r WHERE id = 99 RETURNING id",
    "INSERT INTO r VALUES (1, 'z', 9.0) ON CONFLICT(id) "
    "DO UPDATE SET score = excluded.score RETURNING *",
    "INSERT OR IGNORE INTO r VALUES (1, 'z', 9.0), (6, 'f', 3.0) RETURNING *",
    "INSERT OR REPLACE INTO r VALUES (1, 'z', 9.0) RETURNING *",
]


@pytest.mark.skipif(
    sqlite3.sqlite_version_info < (3, 35),
    reason="RETURNING requires SQLite 3.35 or later",
)
@pytest.mark.parametrize("sql", RETURNING_CASES)
def test_returning_matches_sqlite(tmp_path, sql):
    lite = sqlite3.connect(":memory:")
    ice = iceql.connect(tmp_path / "db")
    for setup in RETURNING_SETUP:
        lite.execute(setup)
        ice.execute(setup)

    expected_cursor = lite.execute(sql)
    expected = normalize_rows(expected_cursor.fetchall())
    expected_names = [d[0] for d in expected_cursor.description]
    actual_cursor = ice.execute(sql)
    actual = normalize_rows(actual_cursor.fetchall())
    actual_names = [d[0] for d in actual_cursor.description]

    assert actual_names == expected_names, sql
    # sqlite は RETURNING が返す行の順序を規定していないため、順序は問わない
    # (iceql 側の順序は tests/test_dml.py の TestReturning で押さえている)
    assert sorted(actual, key=repr) == sorted(expected, key=repr), sql

    expected_state = normalize_rows(lite.execute("SELECT * FROM r ORDER BY id").fetchall())
    actual_state = normalize_rows(ice.execute("SELECT * FROM r ORDER BY id").fetchall())
    assert actual_state == expected_state, sql
    lite.close()
    ice.close()


@pytest.mark.parametrize("sql", CONFLICT_CASES)
def test_insert_conflict_matches_sqlite(tmp_path, sql):
    lite = sqlite3.connect(":memory:")
    ice = iceql.connect(tmp_path / "db")
    for setup in CONSTRAINT_SETUP:
        lite.execute(setup)
        ice.execute(setup)

    try:
        expected_count = lite.execute(sql).rowcount
    except sqlite3.IntegrityError:
        expected_count = None
    try:
        actual_count = ice.execute(sql).rowcount
    except IntegrityError:
        actual_count = None
    assert actual_count == expected_count, sql

    expected = lite.execute("SELECT * FROM c ORDER BY id").fetchall()
    actual = ice.execute("SELECT * FROM c ORDER BY id").fetchall()
    assert actual == expected
    lite.close()
    ice.close()
