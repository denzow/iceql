import pytest

from iceql.errors import (
    DataError,
    IntegrityError,
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


class TestPersistence:
    def test_dml_persists_to_disk(self, conn):
        conn.execute("INSERT INTO depts (id, dept) VALUES (3, 'hr')")
        text = (conn._catalog.root / "depts.csv").read_text(encoding="utf-8")
        assert text == "id,dept\n1,eng\n2,sales\n3,hr\n"

    def test_null_marker_on_disk(self, conn):
        conn.execute("UPDATE users SET joined = NULL WHERE id = 1")
        text = (conn._catalog.root / "users.csv").read_text(encoding="utf-8")
        assert "1,alice,30,1,\\N,true\n" in text
