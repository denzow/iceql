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
    # sqlite3 は boolean を 0/1 で持つので、iceql 側だけ bool へ直す
    catalog.write_rows("users", [(*r[:5], bool(r[5])) for r in USERS_ROWS], users)
    catalog.write_rows("depts", list(DEPTS_ROWS), depts)
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
    # リテラルの並びに対する IN / NOT IN(NULL が値側・左辺のどちらにも来る)
    "SELECT id FROM users WHERE age IN (30, 35) ORDER BY id",
    "SELECT id FROM users WHERE age NOT IN (30, 35) ORDER BY id",
    "SELECT id FROM users WHERE age IN (30, NULL) ORDER BY id",
    "SELECT id FROM users WHERE age NOT IN (30, NULL)",
    "SELECT id FROM users WHERE NOT (age IN (30, 35)) ORDER BY id",
    "SELECT id FROM users WHERE dept_id NOT IN (1) ORDER BY id",
    "SELECT id FROM users WHERE age NOT IN (30) OR name = 'bob' ORDER BY id",
    "SELECT id FROM users WHERE age NOT IN (30) AND dept_id IN (1, 2) ORDER BY id",
    "SELECT COUNT(*) FROM users WHERE age NOT IN (30)",
    "SELECT id, age IN (30, 35), age NOT IN (30, 35) FROM users ORDER BY id",
    "SELECT id, CASE WHEN age NOT IN (30) THEN 'y' ELSE 'n' END FROM users ORDER BY id",
    "SELECT name FROM users WHERE name NOT IN ('alice', 'bob') ORDER BY name",
    "SELECT dept_id, COUNT(*) FROM users WHERE dept_id NOT IN (2) GROUP BY dept_id",
    "SELECT id FROM users WHERE NOT (joined LIKE '2020%') ORDER BY id",
    "SELECT id, NOT (joined LIKE '2020%') FROM users ORDER BY id",
    # NOT LIKE(sqlglot は Like ノードの negate フラグとして持つ)
    "SELECT id FROM users WHERE name NOT LIKE 'a%' ORDER BY id",
    "SELECT id FROM users WHERE joined NOT LIKE '2020%' ORDER BY id",
    "SELECT id, name NOT LIKE 'a%', joined NOT LIKE '2020%' FROM users ORDER BY id",
    "SELECT id FROM users WHERE NOT (name NOT LIKE 'a%') ORDER BY id",
    "SELECT id FROM users WHERE name NOT LIKE 'a%' AND age NOT IN (35) ORDER BY id",
    "SELECT id, CASE WHEN name NOT LIKE 'a%' THEN 'y' ELSE 'n' END FROM users ORDER BY id",
    "SELECT COUNT(*) FROM users WHERE joined NOT LIKE '2020%'",
    "SELECT name FROM users WHERE dept_id IN (SELECT id FROM depts WHERE dept = 'eng')",
    "SELECT name FROM users WHERE dept_id NOT IN (SELECT id FROM depts WHERE dept = 'hr') "
    "AND dept_id IS NOT NULL",
    # NOT IN / EXISTS(サブクエリの結果に該当する行があるデータで突き合わせる)
    "SELECT name FROM users WHERE dept_id NOT IN (SELECT id FROM depts WHERE dept = 'hr')",
    "SELECT id FROM users WHERE id NOT IN (SELECT id FROM depts) ORDER BY id",
    "SELECT id FROM users WHERE NOT (id IN (SELECT id FROM depts)) ORDER BY id",
    # サブクエリの値に NULL が混じる / 左辺が NULL になる
    "SELECT id FROM users WHERE id NOT IN (SELECT dept_id FROM users)",
    "SELECT id FROM users WHERE dept_id NOT IN (SELECT id FROM depts WHERE id = 1) "
    "ORDER BY id",
    "SELECT id FROM users WHERE id NOT IN (SELECT id FROM depts WHERE id > 99) ORDER BY id",
    "SELECT id FROM users WHERE id NOT IN (SELECT id FROM depts) OR name = 'alice' "
    "ORDER BY id",
    "SELECT COUNT(*) FROM users WHERE id NOT IN (SELECT id FROM depts)",
    "SELECT id FROM users WHERE id NOT IN (SELECT id FROM depts ORDER BY id LIMIT 2) "
    "ORDER BY id",
    # NULL と偽が別の結果になる位置に置いた NOT IN サブクエリ
    "SELECT id, CASE WHEN id NOT IN (SELECT id FROM depts) THEN 1 ELSE 0 END "
    "FROM users ORDER BY id",
    "SELECT id, CASE WHEN id NOT IN (SELECT dept_id FROM users) THEN 1 ELSE 0 END "
    "FROM users ORDER BY id",
    "SELECT id FROM users WHERE NOT (id NOT IN (SELECT id FROM depts)) ORDER BY id",
    "SELECT id FROM users WHERE NOT (id NOT IN (SELECT dept_id FROM users)) ORDER BY id",
    "SELECT id, id NOT IN (SELECT id FROM depts) FROM users ORDER BY id",
    "SELECT id, id NOT IN (SELECT dept_id FROM users) FROM users ORDER BY id",
    "SELECT id, dept_id NOT IN (SELECT id FROM depts WHERE id = 1) FROM users ORDER BY id",
    "SELECT id, id NOT IN (SELECT id FROM depts WHERE id > 99) FROM users ORDER BY id",
    "SELECT id FROM users WHERE (id NOT IN (SELECT dept_id FROM users)) IS NULL ORDER BY id",
    "SELECT id FROM users WHERE EXISTS (SELECT 1 FROM depts) ORDER BY id",
    "SELECT id FROM users WHERE EXISTS (SELECT 1 FROM depts WHERE id > 99)",
    "SELECT id FROM users WHERE NOT EXISTS (SELECT 1 FROM depts)",
    "SELECT id FROM users WHERE NOT EXISTS (SELECT 1 FROM depts WHERE id > 99) ORDER BY id",
    "SELECT id FROM users WHERE EXISTS (SELECT 1 FROM depts) AND age > 26 ORDER BY id",
    "SELECT u.id FROM users u "
    "WHERE EXISTS (SELECT 1 FROM depts d WHERE d.id = u.dept_id) ORDER BY u.id",
    "SELECT u.id FROM users u "
    "WHERE NOT EXISTS (SELECT 1 FROM depts d WHERE d.id = u.dept_id) ORDER BY u.id",
    # 相関条件に等値が 1 つあれば、等値でない条件が混ざっていても join へ書き換わる
    "SELECT u.id FROM users u WHERE EXISTS "
    "(SELECT 1 FROM depts d WHERE d.id = u.dept_id AND d.dept > 'a') ORDER BY u.id",
    "SELECT u.id FROM users u WHERE NOT EXISTS "
    "(SELECT 1 FROM depts d WHERE d.id = u.dept_id AND d.dept > 'e') ORDER BY u.id",
    # 相関 EXISTS のうち、decorrelate に渡す前に均す形
    "SELECT u.id FROM users u WHERE EXISTS "
    "(SELECT 1 FROM users x WHERE x.dept_id = u.dept_id GROUP BY x.id) ORDER BY u.id",
    "SELECT u.id FROM users u WHERE NOT EXISTS "
    "(SELECT 1 FROM users x WHERE x.dept_id = u.dept_id GROUP BY x.id) ORDER BY u.id",
    "SELECT u.id FROM users u WHERE EXISTS "
    "(SELECT x.age FROM users x WHERE x.dept_id = u.dept_id GROUP BY x.age) ORDER BY u.id",
    "SELECT u.id FROM users u WHERE EXISTS "
    "(SELECT COUNT(*) FROM depts d WHERE d.id = u.dept_id) ORDER BY u.id",
    "SELECT u.id FROM users u WHERE NOT EXISTS "
    "(SELECT MAX(d.id) FROM depts d WHERE d.id = u.dept_id) ORDER BY u.id",
    "SELECT u.id FROM users u WHERE EXISTS "
    "(SELECT 1 FROM depts d WHERE d.id = u.dept_id ORDER BY d.dept) ORDER BY u.id",
    "SELECT u.id FROM users u WHERE EXISTS "
    "(SELECT 1 FROM depts d WHERE u.age IS NOT NULL AND d.id = u.dept_id) ORDER BY u.id",
    "SELECT u.id FROM users u WHERE NOT EXISTS "
    "(SELECT 1 FROM depts d WHERE u.age IS NOT NULL AND d.id = u.dept_id) ORDER BY u.id",
    "SELECT u.id FROM users u WHERE EXISTS "
    "(SELECT 1 FROM depts d WHERE NOT (u.age > 30) AND d.id = u.dept_id) ORDER BY u.id",
    "SELECT u.id FROM users u WHERE EXISTS "
    "(SELECT 1 FROM depts d WHERE u.age BETWEEN 26 AND 40 AND d.id = u.dept_id) ORDER BY u.id",
    "SELECT u.id FROM users u WHERE EXISTS "
    "(SELECT 1 FROM depts d WHERE u.age IN (30, 35) AND d.id = u.dept_id) ORDER BY u.id",
    "SELECT u.id FROM users u WHERE EXISTS "
    "(SELECT 1 FROM depts d WHERE u.age IS NOT NULL) ORDER BY u.id",
    "SELECT u.id FROM users u WHERE EXISTS (SELECT 1 FROM depts d "
    "WHERE d.id = u.dept_id AND EXISTS "
    "(SELECT 1 FROM users x WHERE x.dept_id = d.id AND u.age IS NOT NULL)) ORDER BY u.id",
    "SELECT COUNT(*) FROM users u WHERE EXISTS "
    "(SELECT 1 FROM users x WHERE x.dept_id = u.dept_id GROUP BY x.id)",
    # EXISTS を join の ON 句に置ける形(相関していない、押し出しで相関が消える)
    "SELECT u.id FROM users u JOIN depts d ON d.id = u.dept_id "
    "AND EXISTS (SELECT 1 FROM depts e WHERE e.id = 1) ORDER BY u.id",
    "SELECT u.id FROM users u JOIN depts d ON d.id = u.dept_id "
    "AND EXISTS (SELECT 1 FROM depts e WHERE u.age IS NOT NULL) ORDER BY u.id",
    "SELECT u.id FROM users u JOIN depts d ON d.id = u.dept_id "
    "WHERE EXISTS (SELECT 1 FROM depts e WHERE e.id = u.dept_id) ORDER BY u.id",
    "WITH grown AS (SELECT * FROM users WHERE age >= 30) SELECT name FROM grown",
    "SELECT id FROM (SELECT id FROM users ORDER BY id LIMIT 2) x ORDER BY id",
    "SELECT id, name FROM (SELECT id, name FROM users ORDER BY id DESC LIMIT 3) x "
    "ORDER BY id",
    "SELECT x.name, d.dept FROM (SELECT id, name, dept_id FROM users ORDER BY id LIMIT 3) x "
    "JOIN depts d ON d.id = x.dept_id ORDER BY x.id",
    "WITH top2 AS (SELECT id, name FROM users ORDER BY id DESC LIMIT 2) "
    "SELECT id, name FROM top2 ORDER BY id",
    "SELECT name FROM users WHERE id = (SELECT id FROM users ORDER BY id DESC LIMIT 1)",
    # サブクエリ内の LIMIT / OFFSET(実体化して評価する)
    "SELECT id FROM (SELECT id FROM users ORDER BY id LIMIT 2 OFFSET 1) x ORDER BY id",
    "WITH t AS (SELECT id, name FROM users ORDER BY id DESC LIMIT 3 OFFSET 1) "
    "SELECT id, name FROM t ORDER BY id",
    "SELECT name FROM users WHERE id = (SELECT id FROM users ORDER BY id LIMIT 1 OFFSET 2)",
    "SELECT id FROM users WHERE id IN (SELECT id FROM users ORDER BY id LIMIT 2) ORDER BY id",
    "SELECT id FROM users "
    "WHERE id IN (SELECT id FROM users ORDER BY id DESC LIMIT 2 OFFSET 1) ORDER BY id",
    "SELECT name FROM users "
    "WHERE dept_id IN (SELECT id FROM depts ORDER BY id LIMIT 1) ORDER BY name",
    "SELECT id FROM users "
    "WHERE id IN (SELECT id FROM (SELECT id FROM users ORDER BY id LIMIT 2) z) ORDER BY id",
    "SELECT id FROM users WHERE EXISTS (SELECT 1 FROM depts LIMIT 1) ORDER BY id",
    "SELECT id FROM users WHERE EXISTS (SELECT 1 FROM depts WHERE id > 99 LIMIT 1)",
    "SELECT id FROM users WHERE NOT EXISTS (SELECT 1 FROM depts LIMIT 1)",
    "WITH t AS (SELECT id FROM users ORDER BY id LIMIT 3) "
    "SELECT id FROM (SELECT id FROM t ORDER BY id DESC LIMIT 2) y ORDER BY id",
    "SELECT COUNT(*) FROM (SELECT id FROM users ORDER BY id LIMIT 2 OFFSET 1) x",
    "SELECT x.name, d.dept FROM (SELECT id, name, dept_id FROM users ORDER BY id LIMIT 3 "
    "OFFSET 1) x JOIN depts d ON d.id = x.dept_id ORDER BY x.id",
    "SELECT id FROM (SELECT id FROM users UNION SELECT id FROM depts ORDER BY id "
    "LIMIT 3 OFFSET 1) x ORDER BY id",
    "WITH t AS (SELECT id FROM users WHERE id <= 4) "
    "SELECT id FROM (SELECT id FROM t UNION SELECT id FROM depts ORDER BY id "
    "LIMIT 3 OFFSET 1) x ORDER BY id",
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
