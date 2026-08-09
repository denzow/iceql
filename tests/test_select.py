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

    def test_limit_in_from_subquery(self, conn):
        rows = q(conn, "SELECT id FROM (SELECT id FROM users ORDER BY id LIMIT 2) x")
        assert rows == [(1,), (2,)]

    def test_limit_in_cte(self, conn):
        rows = q(
            conn,
            "WITH top2 AS (SELECT id FROM users ORDER BY id DESC LIMIT 2) "
            "SELECT id FROM top2 ORDER BY id",
        )
        assert rows == [(3,), (4,)]

    def test_limit_in_scalar_subquery(self, conn):
        rows = q(
            conn,
            "SELECT name FROM users WHERE id = (SELECT id FROM users ORDER BY id LIMIT 1)",
        )
        assert rows == [("alice",)]

    def test_offset_in_from_subquery(self, conn):
        rows = q(conn, "SELECT id FROM (SELECT id FROM users ORDER BY id LIMIT 2 OFFSET 1) x")
        assert rows == [(2,), (3,)]

    def test_offset_in_cte(self, conn):
        rows = q(
            conn,
            "WITH t AS (SELECT id FROM users ORDER BY id LIMIT 2 OFFSET 1) "
            "SELECT id FROM t ORDER BY id",
        )
        assert rows == [(2,), (3,)]

    def test_offset_in_scalar_subquery(self, conn):
        rows = q(
            conn,
            "SELECT name FROM users "
            "WHERE id = (SELECT id FROM users ORDER BY id LIMIT 1 OFFSET 2)",
        )
        assert rows == [("carol",)]

    def test_limit_in_in_subquery(self, conn):
        rows = q(
            conn,
            "SELECT id FROM users WHERE id IN (SELECT id FROM users ORDER BY id LIMIT 2) "
            "ORDER BY id",
        )
        assert rows == [(1,), (2,)]

    def test_offset_in_in_subquery(self, conn):
        rows = q(
            conn,
            "SELECT id FROM users "
            "WHERE id IN (SELECT id FROM users ORDER BY id LIMIT 2 OFFSET 2) ORDER BY id",
        )
        assert rows == [(3,), (4,)]

    def test_limit_nested_under_in_subquery(self, conn):
        rows = q(
            conn,
            "SELECT id FROM users "
            "WHERE id IN (SELECT id FROM (SELECT id FROM users ORDER BY id LIMIT 2) z) "
            "ORDER BY id",
        )
        assert rows == [(1,), (2,)]

    def test_limit_in_exists_subquery(self, conn):
        rows = q(conn, "SELECT id FROM users WHERE EXISTS (SELECT 1 FROM depts LIMIT 1)")
        assert rows == [(1,), (2,), (3,), (4,)]

    def test_limit_in_exists_subquery_empty(self, conn):
        rows = q(
            conn,
            "SELECT id FROM users WHERE EXISTS (SELECT 1 FROM depts WHERE id > 99 LIMIT 1)",
        )
        assert rows == []

    def test_limit_in_not_exists_subquery(self, conn):
        rows = q(conn, "SELECT id FROM users WHERE NOT EXISTS (SELECT 1 FROM depts LIMIT 1)")
        assert rows == []

    def test_not_in_subquery_with_limit(self, conn):
        # 実体化してから畳むので、LIMIT 付きの NOT IN も評価できる
        rows = q(
            conn,
            "SELECT id FROM users "
            "WHERE id NOT IN (SELECT id FROM depts ORDER BY id LIMIT 1) ORDER BY id",
        )
        assert rows == [(2,), (3,), (4,)]

    def test_limit_in_subquery_over_cte(self, conn):
        # 実体化したサブクエリが外側の CTE を参照する
        rows = q(
            conn,
            "WITH t AS (SELECT id FROM users ORDER BY id LIMIT 3) "
            "SELECT id FROM (SELECT id FROM t ORDER BY id DESC LIMIT 2) y ORDER BY id",
        )
        assert rows == [(2,), (3,)]

    def test_limit_in_union_subquery(self, conn):
        rows = q(
            conn,
            "SELECT id FROM (SELECT id FROM users UNION SELECT id FROM depts "
            "ORDER BY id LIMIT 2 OFFSET 1) x ORDER BY id",
        )
        assert rows == [(2,), (3,)]

    def test_limit_in_union_subquery_over_cte(self, conn):
        # 集合演算は WITH 句を持てないので、切り離すときに派生表へ包み直す
        rows = q(
            conn,
            "WITH t AS (SELECT id FROM users WHERE id <= 3) "
            "SELECT id FROM (SELECT id FROM t UNION SELECT id FROM depts "
            "ORDER BY id LIMIT 2 OFFSET 1) x ORDER BY id",
        )
        assert rows == [(2,), (3,)]

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


class TestNotInAndExists:
    """NOT IN と非相関 EXISTS。どちらも sqlglot の unnest が扱わない形。"""

    def test_not_in_subquery(self, conn):
        rows = q(
            conn, "SELECT id FROM users WHERE id NOT IN (SELECT id FROM depts) ORDER BY id"
        )
        assert rows == [(3,), (4,)]

    def test_not_in_subquery_parenthesized(self, conn):
        # NOT (x IN ...) は sqlglot の見送り条件を素通りして anti join になる形
        rows = q(
            conn,
            "SELECT id FROM users WHERE NOT (id IN (SELECT id FROM depts)) ORDER BY id",
        )
        assert rows == [(3,), (4,)]

    def test_not_in_subquery_with_null(self, conn):
        # サブクエリの値に NULL があると NOT IN はどの行でも真にならない
        rows = q(conn, "SELECT id FROM users WHERE id NOT IN (SELECT dept_id FROM users)")
        assert rows == []

    def test_not_in_null_on_the_left(self, conn):
        # 左辺が NULL の行は NOT IN が NULL になるので WHERE を通らない(carol)
        rows = q(
            conn,
            "SELECT id FROM users "
            "WHERE dept_id NOT IN (SELECT id FROM depts WHERE id = 1) ORDER BY id",
        )
        assert rows == [(2,)]

    def test_not_in_empty_subquery(self, conn):
        rows = q(
            conn,
            "SELECT id FROM users "
            "WHERE id NOT IN (SELECT id FROM depts WHERE id > 99) ORDER BY id",
        )
        assert rows == [(1,), (2,), (3,), (4,)]

    def test_not_in_under_or(self, conn):
        rows = q(
            conn,
            "SELECT id FROM users "
            "WHERE id NOT IN (SELECT id FROM depts) OR name = 'alice' ORDER BY id",
        )
        assert rows == [(1,), (3,), (4,)]

    def test_not_in_in_aggregate_query(self, conn):
        assert q(conn, "SELECT COUNT(*) FROM users WHERE id NOT IN (SELECT id FROM depts)") == [
            (2,)
        ]

    def test_not_in_inside_case(self, conn):
        rows = q(
            conn,
            "SELECT id, CASE WHEN id NOT IN (SELECT id FROM depts) THEN 1 ELSE 0 END "
            "FROM users ORDER BY id",
        )
        assert rows == [(1, 0), (2, 0), (3, 1), (4, 1)]

    def test_not_in_inside_case_with_null_in_the_values(self, conn):
        # 値に NULL があると NOT IN は NULL になり、CASE の ELSE に落ちる
        rows = q(
            conn,
            "SELECT id, CASE WHEN id NOT IN (SELECT dept_id FROM users) THEN 1 ELSE 0 END "
            "FROM users ORDER BY id",
        )
        assert rows == [(1, 0), (2, 0), (3, 0), (4, 0)]

    def test_double_negated_not_in_subquery(self, conn):
        # NOT (x NOT IN ...) は NULL を伝播するので、二重の否定でも IN と一致する
        rows = q(
            conn,
            "SELECT id FROM users WHERE NOT (id NOT IN (SELECT id FROM depts)) ORDER BY id",
        )
        assert rows == [(1,), (2,)]

    def test_not_in_in_the_projection(self, conn):
        rows = q(conn, "SELECT id, id NOT IN (SELECT id FROM depts) FROM users ORDER BY id")
        assert rows == [(1, False), (2, False), (3, True), (4, True)]

    def test_not_in_in_the_projection_with_null(self, conn):
        # 値の NULL(carol の dept_id)も左辺の NULL も NULL として現れる
        rows = q(
            conn,
            "SELECT id, id NOT IN (SELECT dept_id FROM users), "
            "dept_id NOT IN (SELECT id FROM depts WHERE id = 1) FROM users ORDER BY id",
        )
        assert rows == [
            (1, False, False),
            (2, False, True),
            (3, None, None),
            (4, None, False),
        ]

    def test_exists_uncorrelated(self, conn):
        rows = q(conn, "SELECT id FROM users WHERE EXISTS (SELECT 1 FROM depts) ORDER BY id")
        assert rows == [(1,), (2,), (3,), (4,)]

    def test_exists_uncorrelated_empty(self, conn):
        rows = q(conn, "SELECT id FROM users WHERE EXISTS (SELECT 1 FROM depts WHERE id > 99)")
        assert rows == []

    def test_not_exists_uncorrelated(self, conn):
        assert q(conn, "SELECT id FROM users WHERE NOT EXISTS (SELECT 1 FROM depts)") == []

    def test_not_exists_uncorrelated_empty(self, conn):
        rows = q(
            conn,
            "SELECT id FROM users "
            "WHERE NOT EXISTS (SELECT 1 FROM depts WHERE id > 99) ORDER BY id",
        )
        assert rows == [(1,), (2,), (3,), (4,)]

    def test_exists_combined_with_other_predicates(self, conn):
        rows = q(
            conn,
            "SELECT id FROM users WHERE EXISTS (SELECT 1 FROM depts) AND id > 2 ORDER BY id",
        )
        assert rows == [(3,), (4,)]


class TestCorrelatedExists:
    """相関 EXISTS。sqlglot の decorrelate が join へ書き換えられる形だけ通る。"""

    def test_correlated_exists(self, conn):
        rows = q(
            conn,
            "SELECT id FROM users u "
            "WHERE EXISTS (SELECT 1 FROM depts d WHERE d.id = u.dept_id) ORDER BY id",
        )
        assert rows == [(1,), (2,), (4,)]

    def test_equality_with_another_comparison(self, conn):
        # 等値が 1 つあれば、等値でない条件が混ざっていても書き換えられる
        rows = q(
            conn,
            "SELECT id FROM users u WHERE EXISTS "
            "(SELECT 1 FROM depts d WHERE d.id = u.dept_id AND d.dept > 'a') ORDER BY id",
        )
        assert rows == [(1,), (2,), (4,)]

    def test_not_exists_with_another_comparison(self, conn):
        rows = q(
            conn,
            "SELECT id FROM users u WHERE NOT EXISTS "
            "(SELECT 1 FROM depts d WHERE d.id = u.dept_id AND d.dept > 'e') ORDER BY id",
        )
        assert rows == [(3,)]

    def test_correlation_without_equality_is_rejected(self, conn):
        with pytest.raises(NotSupportedError, match="correlated EXISTS"):
            q(
                conn,
                "SELECT id FROM users u "
                "WHERE EXISTS (SELECT 1 FROM depts d WHERE d.id > u.dept_id)",
            )

    def test_not_exists_without_equality_is_rejected(self, conn):
        with pytest.raises(NotSupportedError, match="correlated EXISTS"):
            q(
                conn,
                "SELECT id FROM users u "
                "WHERE NOT EXISTS (SELECT 1 FROM depts d WHERE d.id <> u.dept_id)",
            )

    def test_or_in_the_correlation_is_rejected(self, conn):
        with pytest.raises(NotSupportedError, match="correlated EXISTS"):
            q(
                conn,
                "SELECT id FROM users u WHERE EXISTS "
                "(SELECT 1 FROM depts d WHERE d.id = u.dept_id OR d.dept = u.name)",
            )

    def test_or_beside_the_correlation_is_rejected(self, conn):
        # 相関していない OR でも decorrelate は書き換えをあきらめる
        with pytest.raises(NotSupportedError, match="correlated EXISTS"):
            q(
                conn,
                "SELECT id FROM users u WHERE EXISTS (SELECT 1 FROM depts d "
                "WHERE d.id = u.dept_id AND (d.dept = 'eng' OR d.dept = 'sales'))",
            )

    def test_between_on_the_outer_column_is_rejected(self, conn):
        with pytest.raises(NotSupportedError, match="correlated EXISTS"):
            q(
                conn,
                "SELECT id FROM users u "
                "WHERE EXISTS (SELECT 1 FROM depts d WHERE d.id BETWEEN u.dept_id AND 99)",
            )

    def test_outer_column_in_the_projection_is_rejected(self, conn):
        with pytest.raises(NotSupportedError, match="correlated EXISTS"):
            q(conn, "SELECT id FROM users u WHERE EXISTS (SELECT u.id FROM depts d)")

    def test_outer_column_in_having_is_rejected(self, conn):
        with pytest.raises(NotSupportedError, match="correlated EXISTS"):
            q(
                conn,
                "SELECT id FROM users u WHERE EXISTS "
                "(SELECT 1 FROM depts d GROUP BY d.id HAVING COUNT(*) > u.id)",
            )

    def test_rejected_when_only_one_of_two_is_unnestable(self, conn):
        with pytest.raises(NotSupportedError, match="correlated EXISTS"):
            q(
                conn,
                "SELECT id FROM users u "
                "WHERE EXISTS (SELECT 1 FROM depts d WHERE d.id = u.dept_id) "
                "AND EXISTS (SELECT 1 FROM depts e WHERE e.id > u.id)",
            )

    def test_the_error_names_the_subquery_and_a_rewrite(self, conn):
        with pytest.raises(NotSupportedError) as excinfo:
            q(
                conn,
                "SELECT id FROM users u "
                "WHERE EXISTS (SELECT 1 FROM depts d WHERE d.id > u.dept_id)",
            )
        message = str(excinfo.value)
        assert "depts" in message
        assert "MAX" in message

    def test_correlated_exists_in_the_join_on_clause_is_rejected(self, conn):
        with pytest.raises(NotSupportedError, match="correlated EXISTS"):
            q(
                conn,
                "SELECT u.id FROM users u JOIN depts d ON d.id = u.dept_id "
                "AND EXISTS (SELECT 1 FROM depts e WHERE e.id > u.id)",
            )


class TestCorrelatedExistsNormalization:
    """decorrelate が書き換えたうえで結果を間違える形を、渡す前に均す。"""

    def test_group_by_does_not_duplicate_outer_rows(self, conn):
        # dept_id 1 は 2 行あるので、GROUP BY x.id のまま渡すと結合キーが
        # 一意にならず、alice と dave が 2 度ずつ返る
        rows = q(
            conn,
            "SELECT u.id FROM users u WHERE EXISTS "
            "(SELECT 1 FROM users x WHERE x.dept_id = u.dept_id GROUP BY x.id) ORDER BY u.id",
        )
        assert rows == [(1,), (2,), (4,)]

    def test_group_by_under_not_exists(self, conn):
        rows = q(
            conn,
            "SELECT u.id FROM users u WHERE NOT EXISTS "
            "(SELECT 1 FROM users x WHERE x.dept_id = u.dept_id GROUP BY x.id) ORDER BY u.id",
        )
        assert rows == [(3,)]

    def test_group_by_on_the_correlation_key(self, conn):
        rows = q(
            conn,
            "SELECT id FROM users u WHERE EXISTS "
            "(SELECT 1 FROM depts d WHERE d.id = u.dept_id GROUP BY d.id) ORDER BY id",
        )
        assert rows == [(1,), (2,), (4,)]

    def test_aggregate_without_group_by_is_always_true(self, conn):
        # 集約は行が無くても 1 行返すので、EXISTS は dept_id によらず真になる
        rows = q(
            conn,
            "SELECT id FROM users u WHERE EXISTS "
            "(SELECT COUNT(*) FROM depts d WHERE d.id = u.dept_id) ORDER BY id",
        )
        assert rows == [(1,), (2,), (3,), (4,)]

    def test_aggregate_without_group_by_under_not_exists(self, conn):
        rows = q(
            conn,
            "SELECT id FROM users u WHERE NOT EXISTS "
            "(SELECT MAX(d.id) FROM depts d WHERE d.id = u.dept_id) ORDER BY id",
        )
        assert rows == []

    def test_order_by_in_the_subquery(self, conn):
        # EXISTS は行の有無しか見ないので ORDER BY は結果を変えない
        rows = q(
            conn,
            "SELECT id FROM users u WHERE EXISTS "
            "(SELECT 1 FROM depts d WHERE d.id = u.dept_id ORDER BY d.dept) ORDER BY id",
        )
        assert rows == [(1,), (2,), (4,)]

    def test_predicate_on_outer_columns_only(self, conn):
        rows = q(
            conn,
            "SELECT id FROM users u WHERE EXISTS "
            "(SELECT 1 FROM depts d WHERE u.age IS NOT NULL AND d.id = u.dept_id) ORDER BY id",
        )
        assert rows == [(1,), (4,)]

    def test_predicate_on_outer_columns_only_under_not_exists(self, conn):
        # 押し出した述語が NULL の行(age が NULL の bob)は、NOT EXISTS で真になる
        rows = q(
            conn,
            "SELECT id FROM users u WHERE NOT EXISTS "
            "(SELECT 1 FROM depts d WHERE u.age IS NOT NULL AND d.id = u.dept_id) ORDER BY id",
        )
        assert rows == [(2,), (3,)]

    def test_negated_predicate_on_outer_columns_only(self, conn):
        rows = q(
            conn,
            "SELECT id FROM users u WHERE EXISTS "
            "(SELECT 1 FROM depts d WHERE NOT (u.age > 30) AND d.id = u.dept_id) ORDER BY id",
        )
        assert rows == [(1,)]

    def test_between_on_outer_columns_only(self, conn):
        # 押し出すと decorrelate が BETWEEN を相関条件として見ないので通る
        rows = q(
            conn,
            "SELECT id FROM users u WHERE EXISTS "
            "(SELECT 1 FROM depts d WHERE u.age BETWEEN 26 AND 40 AND d.id = u.dept_id) "
            "ORDER BY id",
        )
        assert rows == [(1,), (4,)]

    def test_in_on_outer_columns_only(self, conn):
        rows = q(
            conn,
            "SELECT id FROM users u WHERE EXISTS "
            "(SELECT 1 FROM depts d WHERE u.age IN (30, 35) AND d.id = u.dept_id) ORDER BY id",
        )
        assert rows == [(1,), (4,)]

    def test_subquery_left_uncorrelated_by_the_push_out(self, conn):
        # 相関する述語が全部出ていくと、残りは非相関 EXISTS として畳まれる
        rows = q(
            conn,
            "SELECT id FROM users u WHERE EXISTS "
            "(SELECT 1 FROM depts d WHERE u.age IS NOT NULL) ORDER BY id",
        )
        assert rows == [(1,), (3,), (4,)]

    def test_push_out_from_a_nested_correlated_subquery(self, conn):
        # 押し出し先がさらに相関サブクエリなので、スコープを組み直して繰り返す
        rows = q(
            conn,
            "SELECT id FROM users u WHERE EXISTS (SELECT 1 FROM depts d "
            "WHERE d.id = u.dept_id AND EXISTS "
            "(SELECT 1 FROM users x WHERE x.dept_id = d.id AND u.age IS NOT NULL)) ORDER BY id",
        )
        assert rows == [(1,), (4,)]

    def test_group_by_with_having_is_rejected(self, conn):
        with pytest.raises(NotSupportedError, match="GROUP BY ... HAVING"):
            q(
                conn,
                "SELECT u.id FROM users u WHERE EXISTS (SELECT 1 FROM users x "
                "WHERE x.dept_id = u.dept_id GROUP BY x.id HAVING COUNT(*) > 0)",
            )

    def test_negated_predicate_across_both_sides_is_rejected(self, conn):
        with pytest.raises(NotSupportedError, match="negated predicate"):
            q(
                conn,
                "SELECT id FROM users u WHERE EXISTS "
                "(SELECT 1 FROM depts d WHERE NOT (d.dept = u.name) AND d.id = u.dept_id)",
            )

    def test_the_negation_error_suggests_a_rewrite(self, conn):
        with pytest.raises(NotSupportedError) as excinfo:
            q(
                conn,
                "SELECT id FROM users u WHERE EXISTS "
                "(SELECT 1 FROM depts d WHERE NOT (d.dept = u.name) AND d.id = u.dept_id)",
            )
        message = str(excinfo.value)
        assert "u.k <> s.k" in message

    def test_unrewritable_shape_reports_the_correlation_first(self, conn):
        # OR があると decorrelate はそもそも書き換えない。NOT より OR が原因なので、
        # 書き換えられない形としてのメッセージを出す
        with pytest.raises(NotSupportedError, match="cannot be rewritten as a join"):
            q(
                conn,
                "SELECT id FROM users u WHERE EXISTS "
                "(SELECT 1 FROM depts d WHERE d.id = u.dept_id OR u.age IS NOT NULL)",
            )


class TestLiteralInThreeValuedLogic:
    """リテラルの並びに対する IN / NOT IN の NULL の扱い。"""

    def test_not_in_skips_null_on_the_left(self, conn):
        # age が NULL の bob は NOT IN が NULL になるので WHERE を通らない
        rows = q(conn, "SELECT id FROM users WHERE age NOT IN (30, 35) ORDER BY id")
        assert rows == [(3,)]

    def test_not_in_with_null_in_the_values(self, conn):
        assert q(conn, "SELECT id FROM users WHERE age NOT IN (30, NULL)") == []

    def test_in_with_null_in_the_values(self, conn):
        rows = q(conn, "SELECT id FROM users WHERE age IN (25, NULL) ORDER BY id")
        assert rows == [(3,)]

    def test_null_on_the_left_projects_null(self, conn):
        rows = q(conn, "SELECT id, age IN (30, 35), age NOT IN (30, 35) FROM users ORDER BY id")
        assert rows == [
            (1, True, False),
            (2, None, None),
            (3, False, True),
            (4, True, False),
        ]

    def test_parenthesized_not_in(self, conn):
        rows = q(conn, "SELECT id FROM users WHERE NOT (age IN (30, 35)) ORDER BY id")
        assert rows == [(3,)]

    def test_not_in_under_or(self, conn):
        rows = q(conn, "SELECT id FROM users WHERE age NOT IN (30) OR name = 'bob' ORDER BY id")
        assert rows == [(2,), (3,), (4,)]

    def test_not_in_inside_case(self, conn):
        rows = q(
            conn,
            "SELECT id, CASE WHEN age NOT IN (30) THEN 'y' ELSE 'n' END FROM users ORDER BY id",
        )
        assert rows == [(1, "n"), (2, "n"), (3, "y"), (4, "y")]

    def test_not_propagates_null_from_other_predicates(self, conn):
        # NOT は IN 以外でも NULL を伝播する(joined が NULL の carol は通らない)
        rows = q(conn, "SELECT id FROM users WHERE NOT (joined LIKE '2020%') ORDER BY id")
        assert rows == [(2,), (4,)]


class TestNotLike:
    """NOT LIKE。sqlglot は Like ノードの negate フラグとして持つ。"""

    def test_not_like_filters(self, conn):
        rows = q(conn, "SELECT id FROM users WHERE name NOT LIKE 'a%' ORDER BY id")
        assert rows == [(2,), (3,), (4,)]

    def test_not_like_projects_negated_value(self, conn):
        rows = q(conn, "SELECT id, name NOT LIKE 'a%' FROM users ORDER BY id")
        assert rows == [(1, False), (2, True), (3, True), (4, True)]

    def test_not_like_propagates_null(self, conn):
        # joined が NULL の carol は NOT LIKE が NULL になるので WHERE を通らない
        rows = q(conn, "SELECT id FROM users WHERE joined NOT LIKE '2020%' ORDER BY id")
        assert rows == [(2,), (4,)]

    def test_not_like_projects_null(self, conn):
        rows = q(conn, "SELECT id, joined NOT LIKE '2020%' FROM users ORDER BY id")
        assert rows == [(1, False), (2, True), (3, None), (4, True)]

    def test_parenthesized_not_like(self, conn):
        rows = q(conn, "SELECT id FROM users WHERE NOT (name NOT LIKE 'a%') ORDER BY id")
        assert rows == [(1,)]

    def test_not_like_under_or(self, conn):
        rows = q(
            conn,
            "SELECT id FROM users WHERE name NOT LIKE 'a%' OR name LIKE 'a%' ORDER BY id",
        )
        assert rows == [(1,), (2,), (3,), (4,)]

    def test_not_like_inside_case(self, conn):
        rows = q(
            conn,
            "SELECT id, CASE WHEN name NOT LIKE 'a%' THEN 'y' ELSE 'n' END FROM users "
            "ORDER BY id",
        )
        assert rows == [(1, "n"), (2, "y"), (3, "y"), (4, "y")]


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

    def test_current_date_and_now(self, conn):
        from datetime import date, datetime

        (today,) = q(conn, "SELECT CURRENT_DATE")[0]
        assert str(today) == date.today().isoformat()
        (now,) = q(conn, "SELECT NOW()")[0]
        assert isinstance(now, (datetime, str))

    def test_strftime_on_column(self, conn):
        rows = q(
            conn,
            "SELECT STRFTIME('%Y', joined) FROM users WHERE joined IS NOT NULL ORDER BY id",
        )
        assert rows == [("2020",), ("2021",), ("2019",)]

    def test_cast(self, conn):
        assert q(conn, "SELECT CAST('42' AS INTEGER) + 1") == [(43,)]
        assert q(conn, "SELECT CAST(age AS TEXT) FROM users WHERE id = 1") == [("30",)]


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

    def test_not_in_correlated_subquery_clear_error(self, conn):
        # 外側の行ごとに値の集合が変わるので、一度の評価では畳めない
        with pytest.raises(NotSupportedError, match="NOT IN with a correlated subquery"):
            conn.execute(
                "SELECT id FROM users u "
                "WHERE id NOT IN (SELECT id FROM depts d WHERE d.id = u.dept_id)"
            )

    def test_not_in_multi_column_subquery_clear_error(self, conn):
        with pytest.raises(NotSupportedError, match="multi-column subquery"):
            conn.execute(
                "SELECT id FROM users WHERE (id, name) NOT IN (SELECT id, dept FROM depts)"
            )

    def test_limit_in_correlated_subquery_clear_error(self, conn):
        # 外側の行ごとに結果が変わるので、一度の評価では実体化できない
        with pytest.raises(NotSupportedError, match="correlated subquery"):
            conn.execute(
                "SELECT id FROM users u "
                "WHERE id IN (SELECT id FROM depts d WHERE d.id = u.dept_id LIMIT 1)"
            )

    def test_limit_in_correlated_subquery_unqualified_clear_error(self, conn):
        # 修飾子なしで外側を参照する形も qualify を通してから判定する
        with pytest.raises(NotSupportedError, match="correlated subquery"):
            conn.execute(
                "SELECT id FROM users "
                "WHERE dept_id IN (SELECT id FROM depts WHERE id = dept_id LIMIT 1)"
            )

    def test_non_literal_limit_in_subquery_clear_error(self, conn):
        # sqlglot の planner が ValueError で落ちるため、トップレベルと同じ扱いにする
        with pytest.raises(NotSupportedError, match="LIMIT must be an integer literal"):
            conn.execute("SELECT id FROM (SELECT id FROM users ORDER BY id LIMIT -1) x")

    def test_execute_error_wrapped(self, conn):
        with pytest.raises((OperationalError, ProgrammingError)):
            conn.execute("SELECT nonexistent_column FROM users")
