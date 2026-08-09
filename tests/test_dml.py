import pytest

from iceql.errors import (
    DataError,
    IntegrityError,
    NotSupportedError,
    ProgrammingError,
)


def all_rows(conn, table):
    return conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()


class TestInsert:
    def test_insert_values(self, conn):
        cur = conn.execute(
            "INSERT INTO depts (id, dept) VALUES (3, 'hr'), (4, 'legal')"
        )
        assert cur.rowcount == 2
        assert all_rows(conn, "depts")[-1] == (4, "legal")

    def test_insert_without_column_list(self, conn):
        conn.execute("INSERT INTO depts VALUES (3, 'hr')")
        assert (3, "hr") in all_rows(conn, "depts")

    def test_insert_applies_default(self, conn):
        conn.execute(
            "INSERT INTO users (id, name, age, dept_id, joined) "
            "VALUES (10, 'zoe', 20, 1, '2024-01-01')"
        )
        row = conn.execute("SELECT active FROM users WHERE id = 10").fetchone()
        assert row == (True,)  # active の default true

    def test_insert_null_and_bool(self, conn):
        conn.execute(
            "INSERT INTO users (id, name, age, dept_id, joined, active) "
            "VALUES (11, 'nul', NULL, NULL, NULL, FALSE)"
        )
        row = conn.execute(
            "SELECT age, dept_id, joined, active FROM users WHERE id = 11"
        ).fetchone()
        assert row == (None, None, None, False)

    def test_insert_params(self, conn):
        conn.execute("INSERT INTO depts (id, dept) VALUES (?, ?)", (9, "qa"))
        assert (9, "qa") in all_rows(conn, "depts")

    def test_executemany(self, conn):
        cur = conn.executemany(
            "INSERT INTO depts (id, dept) VALUES (?, ?)", [(5, "a"), (6, "b")]
        )
        assert cur.rowcount == 2
        assert all_rows(conn, "depts")[-2:] == [(5, "a"), (6, "b")]

    def test_executemany_named_params(self, conn):
        conn.executemany(
            "INSERT INTO depts (id, dept) VALUES (:id, :dept)",
            [{"id": 5, "dept": "a"}, {"id": 6, "dept": "b"}],
        )
        assert all_rows(conn, "depts")[-2:] == [(5, "a"), (6, "b")]

    def test_executemany_multi_row_template(self, conn):
        cur = conn.executemany(
            "INSERT INTO depts (id, dept) VALUES (?, ?), (?, ?)",
            [(5, "a", 6, "b"), (7, "c", 8, "d")],
        )
        assert cur.rowcount == 4
        assert len(all_rows(conn, "depts")) == 6

    def test_executemany_writes_the_table_once(self, conn, monkeypatch):
        from iceql import storage

        original = storage.write_rows
        writes = []

        def counting_write_rows(path, rows, schema):
            writes.append(path)
            original(path, rows, schema)

        monkeypatch.setattr(storage, "write_rows", counting_write_rows)
        conn.executemany(
            "INSERT INTO depts (id, dept) VALUES (?, ?)",
            [(i, str(i)) for i in range(5, 15)],
        )
        assert len(writes) == 1

    def test_executemany_is_all_or_nothing(self, conn):
        with pytest.raises(IntegrityError):
            conn.executemany(
                "INSERT INTO depts (id, dept) VALUES (?, ?)", [(5, "a"), (1, "dup")]
            )
        assert all_rows(conn, "depts") == [(1, "eng"), (2, "sales")]

    def test_executemany_without_params(self, conn):
        cur = conn.executemany("INSERT INTO depts (id, dept) VALUES (?, ?)", [])
        assert cur.rowcount == 0
        assert len(all_rows(conn, "depts")) == 2

    def test_executemany_update(self, conn):
        cur = conn.executemany(
            "UPDATE depts SET dept = ? WHERE id = ?", [("a", 1), ("b", 2)]
        )
        assert cur.rowcount == 2
        assert all_rows(conn, "depts") == [(1, "a"), (2, "b")]

    def test_executemany_insert_select(self, conn):
        cur = conn.executemany(
            "INSERT INTO depts (id, dept) SELECT id + ?, name FROM users WHERE id = 1",
            [(100,), (200,)],
        )
        assert cur.rowcount == 2
        assert all_rows(conn, "depts")[-2:] == [(101, "alice"), (201, "alice")]

    def test_insert_select(self, conn):
        cur = conn.execute(
            "INSERT INTO depts (id, dept) SELECT id + 100, name FROM users WHERE age >= 30"
        )
        assert cur.rowcount == 2
        assert (101, "alice") in all_rows(conn, "depts")

    def test_pk_violation(self, conn):
        with pytest.raises(IntegrityError, match="UNIQUE"):
            conn.execute("INSERT INTO depts (id, dept) VALUES (1, 'dup')")

    def test_pk_violation_within_batch(self, conn):
        with pytest.raises(IntegrityError):
            conn.execute("INSERT INTO depts (id, dept) VALUES (7, 'x'), (7, 'y')")

    def test_not_null_violation(self, conn):
        with pytest.raises(IntegrityError, match="NOT NULL"):
            conn.execute("INSERT INTO depts (id, dept) VALUES (8, NULL)")

    def test_type_error(self, conn):
        with pytest.raises(DataError):
            conn.execute("INSERT INTO depts (id, dept) VALUES ('abc', 'x')")

    def test_wrong_value_count(self, conn):
        with pytest.raises(ProgrammingError, match="values for"):
            conn.execute("INSERT INTO depts (id, dept) VALUES (1)")

    def test_unknown_column(self, conn):
        with pytest.raises(DataError, match="no such column"):
            conn.execute("INSERT INTO depts (id, nope) VALUES (1, 'x')")

    def test_bad_date_rejected(self, conn):
        with pytest.raises(DataError, match="date"):
            conn.execute(
                "INSERT INTO users (id, name, joined) VALUES (12, 'x', 'not-a-date')"
            )

    def test_expression_in_values(self, conn):
        conn.execute("INSERT INTO depts (id, dept) VALUES (1 + 2, UPPER('hr'))")
        assert (3, "HR") in all_rows(conn, "depts")

    def test_current_date_in_values(self, conn):
        from datetime import date

        conn.execute(
            "INSERT INTO users (id, name, joined) VALUES (20, 'noa', CURRENT_DATE)"
        )
        row = conn.execute("SELECT joined FROM users WHERE id = 20").fetchone()
        assert row == (date.today().isoformat(),)


class TestInsertConflict:
    @pytest.fixture
    def stock(self, conn):
        """在庫表。主キーのほかに UNIQUE と CHECK を 1 つずつ持つ。"""
        conn.execute(
            "CREATE TABLE stock (id INTEGER PRIMARY KEY, code TEXT UNIQUE, "
            "qty INTEGER NOT NULL CHECK (qty >= 0))"
        )
        conn.execute("INSERT INTO stock VALUES (1, 'a', 10), (2, 'b', 20)")
        return conn

    def test_or_ignore_skips_the_conflicting_row(self, stock):
        cur = stock.execute("INSERT OR IGNORE INTO stock VALUES (1, 'z', 1), (3, 'c', 5)")
        assert cur.rowcount == 1
        assert all_rows(stock, "stock") == [(1, "a", 10), (2, "b", 20), (3, "c", 5)]

    def test_or_ignore_skips_not_null_and_check_violations(self, stock):
        cur = stock.execute(
            "INSERT OR IGNORE INTO stock VALUES (3, 'c', NULL), (4, 'd', -1), (5, 'e', 5)"
        )
        assert cur.rowcount == 1
        assert all_rows(stock, "stock")[-1] == (5, "e", 5)

    def test_or_ignore_skips_a_unique_conflict(self, stock):
        cur = stock.execute("INSERT OR IGNORE INTO stock VALUES (3, 'a', 5)")
        assert cur.rowcount == 0
        assert len(all_rows(stock, "stock")) == 2

    def test_or_replace_overwrites_the_existing_row(self, stock):
        cur = stock.execute("INSERT OR REPLACE INTO stock VALUES (1, 'z', 99)")
        assert cur.rowcount == 1
        assert cur.lastrowid == 1
        assert all_rows(stock, "stock") == [(1, "z", 99), (2, "b", 20)]

    def test_or_replace_drops_every_conflicting_row(self, stock):
        # 主キーで 1 行、UNIQUE で別の 1 行に当たるので、どちらも消えて 1 行になる
        cur = stock.execute("INSERT OR REPLACE INTO stock VALUES (1, 'b', 7)")
        assert cur.rowcount == 1
        assert all_rows(stock, "stock") == [(1, "b", 7)]

    def test_or_replace_keeps_the_row_position(self, stock):
        stock.execute("INSERT OR REPLACE INTO stock VALUES (1, 'z', 99)")
        csv = (stock._catalog.root / "stock.csv").read_text()
        assert csv.splitlines()[1] == "1,z,99"

    def test_or_replace_still_fails_on_check(self, stock):
        with pytest.raises(IntegrityError, match="CHECK"):
            stock.execute("INSERT OR REPLACE INTO stock VALUES (1, 'a', -1)")

    def test_do_nothing_keeps_the_existing_row(self, stock):
        cur = stock.execute(
            "INSERT INTO stock VALUES (1, 'z', 99) ON CONFLICT(id) DO NOTHING"
        )
        assert cur.rowcount == 0
        assert all_rows(stock, "stock") == [(1, "a", 10), (2, "b", 20)]

    def test_do_nothing_without_a_target(self, stock):
        cur = stock.execute("INSERT INTO stock VALUES (3, 'a', 9) ON CONFLICT DO NOTHING")
        assert cur.rowcount == 0
        assert len(all_rows(stock, "stock")) == 2

    def test_do_nothing_reports_a_violation_outside_the_target(self, stock):
        # 主キーは空いているが、対象外の UNIQUE に当たるのでエラーになる
        with pytest.raises(IntegrityError, match="stock.code"):
            stock.execute(
                "INSERT INTO stock VALUES (3, 'a', 9) ON CONFLICT(id) DO NOTHING"
            )

    def test_do_update_uses_excluded(self, stock):
        cur = stock.execute(
            "INSERT INTO stock VALUES (1, 'a', 5) "
            "ON CONFLICT(id) DO UPDATE SET qty = stock.qty + excluded.qty"
        )
        assert cur.rowcount == 1
        assert all_rows(stock, "stock")[0] == (1, "a", 15)

    def test_do_update_defaults_to_the_target_row(self, stock):
        stock.execute(
            "INSERT INTO stock VALUES (1, 'a', 5) "
            "ON CONFLICT(id) DO UPDATE SET qty = qty * 2"
        )
        assert all_rows(stock, "stock")[0] == (1, "a", 20)

    def test_do_update_on_a_unique_target(self, stock):
        stock.execute(
            "INSERT INTO stock VALUES (9, 'b', 1) "
            "ON CONFLICT(code) DO UPDATE SET qty = excluded.qty"
        )
        assert all_rows(stock, "stock") == [(1, "a", 10), (2, "b", 1)]

    def test_do_update_where_false_changes_nothing(self, stock):
        cur = stock.execute(
            "INSERT INTO stock VALUES (1, 'a', 5) "
            "ON CONFLICT(id) DO UPDATE SET qty = 0 WHERE stock.qty < 5"
        )
        assert cur.rowcount == 0
        assert all_rows(stock, "stock")[0] == (1, "a", 10)

    def test_do_update_inserts_when_there_is_no_conflict(self, stock):
        cur = stock.execute(
            "INSERT INTO stock VALUES (3, 'c', 5) ON CONFLICT(id) DO UPDATE SET qty = 0"
        )
        assert cur.rowcount == 1
        assert all_rows(stock, "stock")[-1] == (3, "c", 5)

    def test_do_update_checks_the_updated_row(self, stock):
        with pytest.raises(IntegrityError, match="CHECK"):
            stock.execute(
                "INSERT INTO stock VALUES (1, 'a', 5) "
                "ON CONFLICT(id) DO UPDATE SET qty = -1"
            )

    def test_do_update_reports_a_new_unique_violation(self, stock):
        with pytest.raises(IntegrityError, match="stock.code"):
            stock.execute(
                "INSERT INTO stock VALUES (1, 'a', 5) "
                "ON CONFLICT(id) DO UPDATE SET code = 'b'"
            )

    def test_later_values_see_the_earlier_result(self, stock):
        cur = stock.execute(
            "INSERT INTO stock VALUES (3, 'c', 1), (3, 'c', 2) "
            "ON CONFLICT(id) DO UPDATE SET qty = stock.qty + excluded.qty"
        )
        assert cur.rowcount == 2
        assert all_rows(stock, "stock")[-1] == (3, "c", 3)

    def test_skipped_rows_do_not_consume_the_counter(self, stock):
        stock.execute("INSERT OR IGNORE INTO stock (code, qty) VALUES ('a', 1), ('d', 1)")
        assert all_rows(stock, "stock")[-1] == (3, "d", 1)

    def test_conflict_target_must_match_a_constraint(self, stock):
        with pytest.raises(ProgrammingError, match="ON CONFLICT clause"):
            stock.execute(
                "INSERT INTO stock VALUES (3, 'c', 1) ON CONFLICT(qty) DO NOTHING"
            )

    def test_composite_conflict_target_must_match_as_a_whole(self, stock):
        with pytest.raises(ProgrammingError, match="ON CONFLICT clause"):
            stock.execute(
                "INSERT INTO stock VALUES (3, 'c', 1) ON CONFLICT(id, code) DO NOTHING"
            )

    def test_do_update_without_set_is_rejected(self, stock):
        with pytest.raises(ProgrammingError, match="SET"):
            stock.execute("INSERT INTO stock VALUES (1, 'z', 1) ON CONFLICT(id) DO UPDATE")

    def test_or_rollback_is_rejected(self, stock):
        with pytest.raises(NotSupportedError, match="OR ROLLBACK"):
            stock.execute("INSERT OR ROLLBACK INTO stock VALUES (1, 'z', 1)")

    def test_or_abort_still_fails_on_a_conflict(self, stock):
        with pytest.raises(IntegrityError, match="UNIQUE"):
            stock.execute("INSERT OR ABORT INTO stock VALUES (1, 'z', 1)")

    def test_replace_into_is_rejected(self, stock):
        with pytest.raises(NotSupportedError):
            stock.execute("REPLACE INTO stock VALUES (1, 'z', 1)")

    def test_composite_unique_as_a_target(self, conn):
        conn.execute(
            "CREATE TABLE pairs (id INTEGER PRIMARY KEY, a INTEGER, b INTEGER, "
            "n INTEGER, UNIQUE (a, b))"
        )
        conn.execute("INSERT INTO pairs VALUES (1, 1, 1, 5)")
        conn.execute(
            "INSERT INTO pairs VALUES (2, 1, 1, 7) "
            "ON CONFLICT(b, a) DO UPDATE SET n = excluded.n"
        )
        assert all_rows(conn, "pairs") == [(1, 1, 1, 7)]

    def test_null_keys_never_conflict(self, conn):
        conn.execute("CREATE TABLE opt (id INTEGER PRIMARY KEY, code TEXT UNIQUE)")
        conn.execute("INSERT INTO opt VALUES (1, NULL)")
        cur = conn.execute("INSERT OR IGNORE INTO opt VALUES (2, NULL)")
        assert cur.rowcount == 1
        assert all_rows(conn, "opt") == [(1, None), (2, None)]

    def test_executemany_upsert(self, stock):
        cur = stock.executemany(
            "INSERT INTO stock VALUES (?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET qty = excluded.qty",
            [(1, "a", 1), (3, "c", 2)],
        )
        assert cur.rowcount == 2
        assert all_rows(stock, "stock") == [(1, "a", 1), (2, "b", 20), (3, "c", 2)]

    def test_insert_select_with_conflict(self, stock):
        cur = stock.execute(
            "INSERT OR IGNORE INTO stock SELECT id, code, qty FROM stock"
        )
        assert cur.rowcount == 0
        assert len(all_rows(stock, "stock")) == 2


class TestAutoIncrement:
    def test_omitted_pk_is_assigned(self, conn):
        conn.execute("INSERT INTO depts (dept) VALUES ('hr')")
        assert (3, "hr") in all_rows(conn, "depts")

    def test_assigns_one_to_empty_table(self, conn):
        conn.execute("DELETE FROM depts")
        conn.execute("INSERT INTO depts (dept) VALUES ('hr')")
        assert all_rows(conn, "depts") == [(1, "hr")]

    def test_successive_values_in_one_statement(self, conn):
        conn.execute("INSERT INTO depts (dept) VALUES ('hr'), ('legal')")
        assert all_rows(conn, "depts")[-2:] == [(3, "hr"), (4, "legal")]

    def test_explicit_null_is_assigned(self, conn):
        conn.execute("INSERT INTO depts VALUES (NULL, 'hr')")
        assert (3, "hr") in all_rows(conn, "depts")

    def test_explicit_value_advances_the_counter(self, conn):
        conn.execute("INSERT INTO depts (id, dept) VALUES (100, 'hr')")
        conn.execute("INSERT INTO depts (dept) VALUES ('legal')")
        assert all_rows(conn, "depts")[-1] == (101, "legal")

    def test_explicit_value_advances_within_one_statement(self, conn):
        conn.execute("INSERT INTO depts VALUES (5, 'a'), (NULL, 'b')")
        assert all_rows(conn, "depts")[-1] == (6, "b")

    def test_executemany_assigns_successive_values(self, conn):
        cur = conn.executemany("INSERT INTO depts (dept) VALUES (?)", [("hr",), ("legal",)])
        assert all_rows(conn, "depts")[-2:] == [(3, "hr"), (4, "legal")]
        assert cur.lastrowid == 4

    def test_reuses_deleted_values(self, conn):
        conn.execute("INSERT INTO depts (dept) VALUES ('hr')")
        conn.execute("DELETE FROM depts WHERE id = 3")
        conn.execute("INSERT INTO depts (dept) VALUES ('legal')")
        assert (3, "legal") in all_rows(conn, "depts")

    def test_insert_select_is_assigned(self, conn):
        conn.execute("INSERT INTO depts (dept) SELECT name FROM users WHERE age >= 30")
        assert all_rows(conn, "depts")[-2:] == [(3, "alice"), (4, "dave")]

    def test_sees_rows_added_in_the_same_transaction(self, conn):
        conn.execute("BEGIN")
        conn.execute("INSERT INTO depts (dept) VALUES ('hr')")
        conn.execute("INSERT INTO depts (dept) VALUES ('legal')")
        conn.execute("COMMIT")
        assert all_rows(conn, "depts")[-2:] == [(3, "hr"), (4, "legal")]

    def test_composite_pk_is_not_assigned(self, conn):
        conn.execute("CREATE TABLE pairs (a INTEGER, b INTEGER, PRIMARY KEY (a, b))")
        with pytest.raises(IntegrityError, match="NOT NULL"):
            conn.execute("INSERT INTO pairs (b) VALUES (1)")

    def test_text_pk_is_not_assigned(self, conn):
        conn.execute("CREATE TABLE tags (k TEXT PRIMARY KEY, v TEXT)")
        with pytest.raises(IntegrityError, match="NOT NULL"):
            conn.execute("INSERT INTO tags (v) VALUES ('x')")

    def test_other_not_null_columns_still_fail(self, conn):
        with pytest.raises(IntegrityError, match="NOT NULL"):
            conn.execute("INSERT INTO depts (id) VALUES (3)")


class TestUpdate:
    def test_update_where(self, conn):
        cur = conn.execute("UPDATE users SET age = 31 WHERE name = 'alice'")
        assert cur.rowcount == 1
        assert conn.execute("SELECT age FROM users WHERE id = 1").fetchone() == (31,)

    def test_update_expression_uses_old_values(self, conn):
        conn.execute("UPDATE users SET age = age + 1 WHERE age IS NOT NULL")
        rows = conn.execute("SELECT id, age FROM users ORDER BY id").fetchall()
        assert rows == [(1, 31), (2, None), (3, 26), (4, 36)]

    def test_update_all_rows(self, conn):
        cur = conn.execute("UPDATE users SET active = TRUE")
        assert cur.rowcount == 4

    def test_update_multiple_columns(self, conn):
        conn.execute("UPDATE users SET age = 99, active = FALSE WHERE id = 3")
        assert conn.execute(
            "SELECT age, active FROM users WHERE id = 3"
        ).fetchone() == (99, False)

    def test_update_to_null(self, conn):
        conn.execute("UPDATE users SET joined = NULL WHERE id = 1")
        assert conn.execute("SELECT joined FROM users WHERE id = 1").fetchone() == (None,)

    def test_update_rowcount_zero(self, conn):
        assert conn.execute("UPDATE users SET age = 1 WHERE id = 999").rowcount == 0

    def test_update_pk_violation(self, conn):
        with pytest.raises(IntegrityError, match="UNIQUE"):
            conn.execute("UPDATE users SET id = 1 WHERE id = 2")

    def test_update_not_null_violation(self, conn):
        with pytest.raises(IntegrityError, match="NOT NULL"):
            conn.execute("UPDATE users SET name = NULL WHERE id = 1")

    def test_update_type_check(self, conn):
        with pytest.raises(DataError):
            conn.execute("UPDATE users SET age = 'abc' WHERE id = 1")

    def test_update_from_is_rejected(self, conn):
        # FROM 句を取りこぼすと WHERE ごと無視して全行を書き換えてしまう
        with pytest.raises(NotSupportedError, match="FROM"):
            conn.execute(
                "UPDATE users SET name = depts.dept "
                "FROM depts WHERE users.dept_id = depts.id"
            )
        assert all_rows(conn, "users")[0][1] == "alice"

    def test_update_from_subquery_is_rejected(self, conn):
        with pytest.raises(NotSupportedError, match="FROM"):
            conn.execute("UPDATE users SET name = 'x' FROM (SELECT 1) s")
        assert all_rows(conn, "users")[0][1] == "alice"

    def test_update_with_unnestable_correlated_exists_is_rejected(self, conn):
        with pytest.raises(NotSupportedError, match="correlated EXISTS"):
            conn.execute(
                "UPDATE users SET name = 'x' "
                "WHERE EXISTS (SELECT 1 FROM depts d WHERE d.id > users.dept_id)"
            )
        assert all_rows(conn, "users")[0][1] == "alice"


class TestDelete:
    def test_delete_where(self, conn):
        cur = conn.execute("DELETE FROM users WHERE age IS NULL")
        assert cur.rowcount == 1
        assert [r[0] for r in all_rows(conn, "users")] == [1, 3, 4]

    def test_delete_all(self, conn):
        assert conn.execute("DELETE FROM users").rowcount == 4
        assert all_rows(conn, "users") == []

    def test_delete_none_matched(self, conn):
        assert conn.execute("DELETE FROM users WHERE id = 999").rowcount == 0

    def test_delete_with_subquery(self, conn):
        conn.execute(
            "DELETE FROM users WHERE dept_id IN (SELECT id FROM depts WHERE dept = 'eng')"
        )
        assert [r[0] for r in all_rows(conn, "users")] == [2, 3]

    def test_delete_with_correlated_exists(self, conn):
        conn.execute(
            "DELETE FROM users WHERE EXISTS (SELECT 1 FROM depts d WHERE d.id = users.dept_id)"
        )
        assert [r[0] for r in all_rows(conn, "users")] == [3]

    def test_delete_with_unnestable_correlated_exists_is_rejected(self, conn):
        # SELECT と同じ経路を通るので、書き換えられない相関 EXISTS は DML でも拒否される
        with pytest.raises(NotSupportedError, match="correlated EXISTS"):
            conn.execute(
                "DELETE FROM users "
                "WHERE EXISTS (SELECT 1 FROM depts d WHERE d.id > users.dept_id)"
            )
        assert len(all_rows(conn, "users")) == 4


class TestReturning:
    def names(self, cur):
        return [d[0] for d in cur.description]

    def test_insert_returns_the_inserted_rows(self, conn):
        cur = conn.execute(
            "INSERT INTO depts (id, dept) VALUES (3, 'hr'), (4, 'legal') "
            "RETURNING id, dept"
        )
        assert self.names(cur) == ["id", "dept"]
        assert cur.fetchall() == [(3, "hr"), (4, "legal")]
        assert cur.rowcount == 2

    def test_insert_star_returns_the_assigned_key(self, conn):
        cur = conn.execute("INSERT INTO depts (dept) VALUES ('hr') RETURNING *")
        assert self.names(cur) == ["id", "dept"]
        assert cur.fetchall() == [(3, "hr")]
        assert cur.lastrowid == 3

    def test_insert_returns_the_default_value(self, conn):
        cur = conn.execute(
            "INSERT INTO users (id, name) VALUES (10, 'zoe') RETURNING id, active"
        )
        assert cur.fetchall() == [(10, True)]

    def test_insert_select_returns_the_inserted_rows(self, conn):
        cur = conn.execute(
            "INSERT INTO depts (id, dept) SELECT id + 10, name FROM users "
            "WHERE id <= 2 RETURNING id, dept"
        )
        assert cur.fetchall() == [(11, "alice"), (12, "bob")]

    def test_returning_expressions_and_aliases(self, conn):
        cur = conn.execute(
            "INSERT INTO depts (id, dept) VALUES (3, 'hr') "
            "RETURNING id AS key, dept || '!' AS shouted"
        )
        assert self.names(cur) == ["key", "shouted"]
        assert cur.fetchall() == [(3, "hr!")]

    def test_update_returns_the_new_values(self, conn):
        cur = conn.execute(
            "UPDATE users SET age = age + 1 WHERE dept_id = 1 RETURNING id, age"
        )
        assert cur.fetchall() == [(1, 31), (4, 36)]
        assert cur.rowcount == 2

    def test_delete_returns_the_removed_rows(self, conn):
        cur = conn.execute("DELETE FROM users WHERE age IS NULL RETURNING id, name")
        assert cur.fetchall() == [(2, "bob")]
        assert [r[0] for r in all_rows(conn, "users")] == [1, 3, 4]

    def test_returning_without_matching_rows(self, conn):
        cur = conn.execute("DELETE FROM users WHERE id = 999 RETURNING id, name")
        assert self.names(cur) == ["id", "name"]
        assert cur.fetchall() == []
        assert cur.rowcount == 0

    def test_do_update_returns_the_updated_row(self, conn):
        conn.execute("CREATE TABLE stock (id INTEGER PRIMARY KEY, qty INTEGER)")
        conn.execute("INSERT INTO stock VALUES (1, 10)")
        cur = conn.execute(
            "INSERT INTO stock VALUES (1, 5) ON CONFLICT (id) "
            "DO UPDATE SET qty = stock.qty + excluded.qty RETURNING id, qty"
        )
        assert cur.fetchall() == [(1, 15)]

    def test_or_ignore_omits_the_skipped_row(self, conn):
        conn.execute("CREATE TABLE stock (id INTEGER PRIMARY KEY, qty INTEGER)")
        conn.execute("INSERT INTO stock VALUES (1, 10)")
        cur = conn.execute(
            "INSERT OR IGNORE INTO stock VALUES (1, 5), (2, 7) RETURNING id, qty"
        )
        assert cur.fetchall() == [(2, 7)]
        assert cur.rowcount == 1

    def test_or_replace_returns_the_new_row(self, conn):
        conn.execute("CREATE TABLE stock (id INTEGER PRIMARY KEY, qty INTEGER)")
        conn.execute("INSERT INTO stock VALUES (1, 10)")
        cur = conn.execute(
            "INSERT OR REPLACE INTO stock VALUES (1, 99) RETURNING id, qty"
        )
        assert cur.fetchall() == [(1, 99)]

    def test_returning_in_a_transaction(self, conn):
        conn.execute("BEGIN")
        cur = conn.execute("INSERT INTO depts (id, dept) VALUES (3, 'hr') RETURNING *")
        assert cur.fetchall() == [(3, "hr")]
        conn.execute("COMMIT")
        assert (3, "hr") in all_rows(conn, "depts")

    def test_aggregate_is_rejected(self, conn):
        with pytest.raises(NotSupportedError, match="aggregate"):
            conn.execute("DELETE FROM users RETURNING COUNT(*)")
        assert len(all_rows(conn, "users")) == 4

    def test_subquery_is_rejected(self, conn):
        with pytest.raises(NotSupportedError, match="subquer"):
            conn.execute(
                "DELETE FROM users RETURNING (SELECT COUNT(*) FROM depts)"
            )
        assert len(all_rows(conn, "users")) == 4

    def test_unknown_column_is_rejected(self, conn):
        with pytest.raises(ProgrammingError):
            conn.execute("DELETE FROM users WHERE id = 1 RETURNING nope")
        assert len(all_rows(conn, "users")) == 4


class TestPersistence:
    def test_dml_persists_to_disk(self, conn):
        conn.execute("INSERT INTO depts (id, dept) VALUES (3, 'hr')")
        text = (conn._catalog.root / "depts.csv").read_text(encoding="utf-8")
        assert text == "id,dept\n1,eng\n2,sales\n3,hr\n"

    def test_null_marker_on_disk(self, conn):
        conn.execute("UPDATE users SET joined = NULL WHERE id = 1")
        text = (conn._catalog.root / "users.csv").read_text(encoding="utf-8")
        assert "1,alice,30,1,\\N,true\n" in text
