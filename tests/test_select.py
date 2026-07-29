import pytest

from iceql.errors import NotSupportedError, OperationalError, ProgrammingError


def q(conn, sql, params=None):
    return conn.execute(sql, params).fetchall()


class TestBasicSelect:
    def test_select_all(self, conn):
        rows = q(conn, "SELECT id, name FROM users ORDER BY id")
        assert rows == [(1, "alice"), (2, "bob"), (3, "carol"), (4, "dave")]

    def test_where(self, conn):
        assert q(conn, "SELECT name FROM users WHERE age > 26 ORDER BY name") == [
            ("alice",),
            ("dave",),
        ]

    def test_star(self, conn):
        rows = q(conn, "SELECT * FROM depts ORDER BY id")
        assert rows == [(1, "eng"), (2, "sales")]

    def test_is_null(self, conn):
        assert q(conn, "SELECT name FROM users WHERE age IS NULL") == [("bob",)]

    def test_boolean_predicate(self, conn):
        assert q(conn, "SELECT name FROM users WHERE active = FALSE") == [("carol",)]

    def test_date_comparison(self, conn):
        rows = q(conn, "SELECT name FROM users WHERE joined >= '2020-06-01'")
        assert rows == [("bob",)]

    def test_limit_offset(self, conn):
        assert q(conn, "SELECT id FROM users ORDER BY id LIMIT 2 OFFSET 1") == [(2,), (3,)]

    def test_expression_no_table(self, conn):
        assert q(conn, "SELECT 1 + 1") == [(2,)]

    def test_empty_table(self, conn):
        conn._catalog.write_rows("depts", [], conn._catalog.load_schema("depts"))
        assert q(conn, "SELECT id, dept FROM depts") == []


class TestJoinAggregate:
    def test_join(self, conn):
        rows = q(
            conn,
            "SELECT u.name, d.dept FROM users u JOIN depts d ON u.dept_id = d.id "
            "ORDER BY u.id",
        )
        assert rows == [("alice", "eng"), ("bob", "sales"), ("dave", "eng")]

    def test_left_join_null(self, conn):
        rows = q(
            conn,
            "SELECT u.name, d.dept FROM users u LEFT JOIN depts d ON u.dept_id = d.id "
            "WHERE d.id IS NULL",
        )
        assert rows == [("carol", None)]

    def test_group_by(self, conn):
        rows = q(
            conn,
            "SELECT d.dept, COUNT(*) AS c, MAX(u.age) AS m FROM users u "
            "JOIN depts d ON u.dept_id = d.id GROUP BY d.dept ORDER BY d.dept",
        )
        assert rows == [("eng", 2, 35), ("sales", 1, None)]

    def test_aggregates_ignore_null(self, conn):
        assert q(conn, "SELECT COUNT(age), SUM(age), AVG(age) FROM users") == [(3, 90, 30.0)]

    def test_cte(self, conn):
        rows = q(
            conn,
            "WITH grown AS (SELECT * FROM users WHERE age >= 30) "
            "SELECT name FROM grown ORDER BY name",
        )
        assert rows == [("alice",), ("dave",)]

    def test_in_subquery(self, conn):
        rows = q(
            conn,
            "SELECT name FROM users WHERE dept_id IN (SELECT id FROM depts WHERE dept = 'eng') "
            "ORDER BY id",
        )
        assert rows == [("alice",), ("dave",)]

    def test_union_all(self, conn):
        rows = q(conn, "SELECT name FROM users WHERE id = 1 UNION ALL SELECT dept FROM depts")
        assert rows == [("alice",), ("eng",), ("sales",)]


class TestOrderByNulls:
    def test_asc_nulls_first(self, conn):
        # SQLite と同じ既定: ASC は NULL が先頭
        rows = q(conn, "SELECT name, age FROM users ORDER BY age")
        assert rows == [("bob", None), ("carol", 25), ("alice", 30), ("dave", 35)]

    def test_desc_nulls_last(self, conn):
        rows = q(conn, "SELECT name, age FROM users ORDER BY age DESC")
        assert rows == [("dave", 35), ("alice", 30), ("carol", 25), ("bob", None)]

    def test_explicit_nulls_last(self, conn):
        rows = q(conn, "SELECT name, age FROM users ORDER BY age NULLS LAST")
        assert rows == [("carol", 25), ("alice", 30), ("dave", 35), ("bob", None)]

    def test_ordinal(self, conn):
        rows = q(conn, "SELECT name, age FROM users WHERE age IS NOT NULL ORDER BY 2 DESC")
        assert rows == [("dave", 35), ("alice", 30), ("carol", 25)]

    def test_order_by_alias(self, conn):
        rows = q(
            conn,
            "SELECT dept_id, COUNT(*) AS c FROM users GROUP BY dept_id ORDER BY c DESC, dept_id",
        )
        assert rows[0] == (1, 2)


class TestFunctions:
    def test_string_functions(self, conn):
        assert q(conn, "SELECT UPPER(name), LENGTH(name) FROM users WHERE id = 1") == [
            ("ALICE", 5)
        ]

    def test_concat_operator(self, conn):
        assert q(conn, "SELECT name || '!' FROM users WHERE id = 2") == [("bob!",)]

    def test_case(self, conn):
        rows = q(
            conn,
            "SELECT name, CASE WHEN age >= 30 THEN 'senior' ELSE 'junior' END "
            "FROM users WHERE age IS NOT NULL ORDER BY id",
        )
        assert rows == [("alice", "senior"), ("carol", "junior"), ("dave", "senior")]

    def test_coalesce(self, conn):
        assert q(conn, "SELECT COALESCE(age, -1) FROM users WHERE id = 2") == [(-1,)]

    def test_like(self, conn):
        assert q(conn, "SELECT name FROM users WHERE name LIKE 'a%'") == [("alice",)]


class TestErrors:
    def test_no_such_table(self, conn):
        with pytest.raises(ProgrammingError, match="no such table"):
            conn.execute("SELECT * FROM missing")

    def test_syntax_error(self, conn):
        with pytest.raises(ProgrammingError, match="syntax"):
            conn.execute("SELEC * FROM users")

    def test_multiple_statements(self, conn):
        with pytest.raises(ProgrammingError, match="one statement"):
            conn.execute("SELECT 1; SELECT 2")

    def test_window_function_clear_error(self, conn):
        with pytest.raises(NotSupportedError, match="window"):
            conn.execute("SELECT name, ROW_NUMBER() OVER (ORDER BY id) FROM users")

    def test_count_distinct_clear_error(self, conn):
        # sqlglot が誤答を返すため、黙って間違うのではなく明確に拒否する
        with pytest.raises(NotSupportedError, match="DISTINCT inside aggregate"):
            conn.execute("SELECT COUNT(DISTINCT age) FROM users")

    def test_scalar_subquery_clear_error(self, conn):
        with pytest.raises(NotSupportedError, match="scalar subquer"):
            conn.execute("SELECT name, (SELECT MAX(id) FROM depts) FROM users")

    def test_execute_error_wrapped(self, conn):
        with pytest.raises((OperationalError, ProgrammingError)):
            conn.execute("SELECT nonexistent_column FROM users")
