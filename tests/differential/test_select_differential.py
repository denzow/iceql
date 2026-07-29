"""同一クエリを iceql と sqlite3 に投げて結果を突き合わせる差分テスト。"""

import math
import sqlite3

import pytest

import iceql
from iceql.schema import Column, TableSchema

USERS_ROWS = [
    (1, "alice", 30, 1, "2020-01-15", 1),
    (2, "bob", None, 2, "2021-06-01", 1),
    (3, "carol", 25, None, None, 0),
    (4, "dave", 35, 1, "2019-11-30", 1),
    (5, "eve", 30, 2, "2022-03-10", 0),
]
DEPTS_ROWS = [(1, "eng"), (2, "sales"), (3, "hr")]


@pytest.fixture
def sqlite_conn():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, age INTEGER,
            dept_id INTEGER, joined TEXT, active INTEGER NOT NULL
        );
        CREATE TABLE depts (id INTEGER PRIMARY KEY, dept TEXT NOT NULL);
        """
    )
    conn.executemany("INSERT INTO users VALUES (?, ?, ?, ?, ?, ?)", USERS_ROWS)
    conn.executemany("INSERT INTO depts VALUES (?, ?)", DEPTS_ROWS)
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def iceql_conn(tmp_path):
    conn = iceql.connect(tmp_path / "db")
    catalog = conn._catalog
    users = TableSchema(
        table="users",
        columns=[
            Column(name="id", type="integer", primary_key=True),
            Column(name="name", type="text", nullable=False),
            Column(name="age", type="integer"),
            Column(name="dept_id", type="integer"),
            Column(name="joined", type="date"),
            Column(name="active", type="boolean", nullable=False),
        ],
    )
    depts = TableSchema(
        table="depts",
        columns=[
            Column(name="id", type="integer", primary_key=True),
            Column(name="dept", type="text", nullable=False),
        ],
    )
    catalog.create_table(users)
    catalog.create_table(depts)
    catalog.write_rows(
        "users",
        [
            {
                "id": r[0], "name": r[1], "age": r[2],
                "dept_id": r[3], "joined": r[4], "active": bool(r[5]),
            }
            for r in USERS_ROWS
        ],
        users,
    )
    catalog.write_rows(
        "depts", [{"id": r[0], "dept": r[1]} for r in DEPTS_ROWS], depts
    )
    yield conn
    conn.close()


def normalize_value(v):
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, float):
        return round(v, 9)
    return v


def normalize(rows, ordered):
    out = [tuple(normalize_value(v) for v in row) for row in rows]
    if not ordered:
        out = sorted(out, key=repr)
    return out


def assert_same(iceql_conn, sqlite_conn, sql, params=()):
    ordered = "ORDER BY" in sql.upper()
    expected = normalize(sqlite_conn.execute(sql, params).fetchall(), ordered)
    actual = normalize(iceql_conn.execute(sql, params).fetchall(), ordered)
    assert actual == expected, f"query: {sql}\niceql:  {actual}\nsqlite: {expected}"


SELECT_QUERIES = [
    "SELECT * FROM users",
    "SELECT id, name FROM users ORDER BY id",
    "SELECT name FROM users WHERE age > 26",
    "SELECT name FROM users WHERE age IS NULL",
    "SELECT name FROM users WHERE age IS NOT NULL AND active = 1",
    "SELECT name FROM users WHERE joined >= '2020-06-01'",
    "SELECT name, age FROM users ORDER BY age",
    "SELECT name, age FROM users ORDER BY age DESC",
    "SELECT name, age FROM users ORDER BY age NULLS LAST, name",
    "SELECT name, age FROM users ORDER BY age DESC NULLS FIRST, id",
    "SELECT name FROM users ORDER BY age DESC, name ASC",
    "SELECT id FROM users ORDER BY id LIMIT 2",
    "SELECT id FROM users ORDER BY id LIMIT 2 OFFSET 2",
    "SELECT u.name, d.dept FROM users u JOIN depts d ON u.dept_id = d.id ORDER BY u.id",
    "SELECT u.name, d.dept FROM users u LEFT JOIN depts d ON u.dept_id = d.id ORDER BY u.id",
    "SELECT d.dept, COUNT(*) AS c FROM users u JOIN depts d ON u.dept_id = d.id "
    "GROUP BY d.dept ORDER BY c DESC, d.dept",
    "SELECT dept_id, COUNT(*) FROM users GROUP BY dept_id",
    "SELECT dept_id, AVG(age) FROM users GROUP BY dept_id HAVING COUNT(*) > 1",
    "SELECT COUNT(age), SUM(age), MIN(age), MAX(age), AVG(age) FROM users",
    "SELECT COUNT(*) FROM (SELECT DISTINCT age FROM users WHERE age IS NOT NULL)",
    "SELECT DISTINCT age FROM users",
    "SELECT DISTINCT age FROM users ORDER BY age",
    "SELECT name FROM users WHERE dept_id IN (SELECT id FROM depts WHERE dept = 'eng')",
    "SELECT name FROM users WHERE dept_id NOT IN (SELECT id FROM depts WHERE dept = 'hr') "
    "AND dept_id IS NOT NULL",
    "WITH grown AS (SELECT * FROM users WHERE age >= 30) SELECT name FROM grown",
    "SELECT name FROM users UNION ALL SELECT dept FROM depts",
    "SELECT dept_id FROM users WHERE dept_id IS NOT NULL UNION SELECT id FROM depts",
    "SELECT UPPER(name), LOWER(name), LENGTH(name) FROM users",
    "SELECT name || '-' || COALESCE(age, 0) FROM users",
    "SELECT SUBSTR(name, 1, 2) FROM users",
    "SELECT REPLACE(name, 'a', 'A') FROM users",
    "SELECT ABS(-age) FROM users WHERE age IS NOT NULL",
    "SELECT name, CASE WHEN age >= 30 THEN 'senior' WHEN age IS NULL THEN 'unknown' "
    "ELSE 'junior' END FROM users",
    "SELECT COALESCE(age, -1), IFNULL(age, -1), NULLIF(age, 30) FROM users",
    "SELECT name FROM users WHERE name LIKE 'a%' OR name LIKE '%e'",
    "SELECT name FROM users WHERE age BETWEEN 25 AND 30",
    "SELECT id * 2 + age FROM users WHERE age IS NOT NULL",
    "SELECT age / 2, age % 7 FROM users WHERE age IS NOT NULL",
    "SELECT NOT active FROM users",
    "SELECT 1 + 1",
    "SELECT name, age FROM users ORDER BY 2 DESC, 1",
]


@pytest.mark.parametrize("sql", SELECT_QUERIES)
def test_select_matches_sqlite(iceql_conn, sqlite_conn, sql):
    assert_same(iceql_conn, sqlite_conn, sql)


def test_params_match_sqlite(iceql_conn, sqlite_conn):
    assert_same(
        iceql_conn, sqlite_conn, "SELECT name FROM users WHERE age > ? AND id < ?", (26, 5)
    )


def test_float_aggregate(iceql_conn, sqlite_conn):
    sql = "SELECT AVG(age) * 1.5 FROM users"
    expected = sqlite_conn.execute(sql).fetchall()[0][0]
    actual = iceql_conn.execute(sql).fetchall()[0][0]
    assert math.isclose(actual, expected)
