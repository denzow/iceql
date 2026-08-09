"""UPDATE / DELETE の高速パスが SELECT 還元と同じ結果になることを固定する。

高速パス(engine/dml.py)は、還元では sqlglot が評価していた WHERE と SET を
Python 側で評価する。ここでは同じ文を両経路で実行して、テーブルの状態・
rowcount・RETURNING の行・送出される例外を突き合わせる。還元を止めるには
_equality_terms を「乗らない」と答えさせる(高速パスの唯一の入口なので、
UPDATE の SET が受理される形でも還元へ落ちる)。

高速パスに乗る形と乗らない形の両方を並べてある。どちらに乗ったかは
_matching_positions が呼ばれたかで判定し、乗り方そのものも固定する。
"""

import pytest

import iceql
from iceql.engine import dml
from iceql.errors import IntegrityError

SETUP = [
    "CREATE TABLE t ("
    " id INTEGER PRIMARY KEY,"
    " name TEXT NOT NULL UNIQUE,"
    " score REAL,"
    " tag TEXT,"
    " flag BOOLEAN,"
    " day DATE,"
    " CHECK (score IS NULL OR score >= -10)"
    ")",
    "INSERT INTO t (id, name, score, tag, flag, day) VALUES "
    "(1, 'a', 1.5, 'x', TRUE, '2020-01-01'),"
    "(2, 'b', NULL, 'y', FALSE, NULL),"
    "(3, 'c', -0.5, NULL, TRUE, '2021-12-31'),"
    "(4, 'd', 2.0, 'x', NULL, '2020-01-01')",
]

# 高速パスに乗る文
FAST_CASES = [
    "UPDATE t SET score = 9.0 WHERE id = 2",
    "UPDATE t SET tag = 'z' WHERE tag = 'x'",
    "UPDATE t SET tag = 'z' WHERE tag = 'x' AND id = 1",
    "UPDATE t SET tag = 'z' WHERE tag = 'x' AND tag = 'y'",
    "UPDATE t SET tag = 'z' WHERE 2 = id",
    "UPDATE t SET tag = 'z' WHERE t.id = 2",
    "UPDATE t SET tag = 'z' WHERE (id) = ((2))",
    "UPDATE t SET score = NULL WHERE id = 1",
    "UPDATE t SET score = -1.5 WHERE id = 4",
    "UPDATE t SET tag = 'z' WHERE score = 1.5",
    "UPDATE t SET tag = 'z' WHERE score = 2",  # real 列を整数リテラルと比較
    # 型が食い違う比較。iceql は sqlite の型親和性による変換を行わないので
    # どちらの経路でも 1 行も一致しない
    "UPDATE t SET tag = 'z' WHERE id = '2'",
    "UPDATE t SET tag = 'z' WHERE name = 2",
    "UPDATE t SET tag = 'z' WHERE flag = FALSE",
    "UPDATE t SET tag = 'z' WHERE day = '2020-01-01'",
    "UPDATE t SET tag = 'z' WHERE tag = 'nope'",  # 1 行も一致しない
    "UPDATE t SET tag = 'q'",  # WHERE 無し
    "UPDATE t SET tag = name WHERE id = 3",  # 右辺が列参照
    "UPDATE t SET tag = name, name = tag WHERE id = 1",  # 右辺は更新前の行
    "UPDATE t SET id = 9 WHERE id = 1",  # 主キーを動かす
    "UPDATE t SET name = 'zz' WHERE id = 1",  # UNIQUE 列を動かす
    "UPDATE t SET tag = 'r' WHERE id = 1 RETURNING id, tag, name",
    "DELETE FROM t WHERE id = 3",
    "DELETE FROM t WHERE tag = 'x'",
    "DELETE FROM t WHERE tag = 'x' AND flag = TRUE",
    "DELETE FROM t WHERE score = 1.5",
    "DELETE FROM t WHERE id = 999",
    "DELETE FROM t",
    "DELETE FROM t WHERE tag = 'x' RETURNING id, name",
]

# 高速パスに乗らない文(還元のまま)
SLOW_CASES = [
    "UPDATE t SET score = score + 1 WHERE id = 1",
    "UPDATE t SET tag = UPPER(tag) WHERE id = 1",
    "UPDATE t SET tag = 'z' WHERE score IS NULL",
    "UPDATE t SET tag = 'z' WHERE tag = NULL",  # 常に UNKNOWN
    "UPDATE t SET tag = 'z' WHERE id > 2",
    "UPDATE t SET tag = 'z' WHERE id = 1 OR id = 2",
    "UPDATE t SET tag = 'z' WHERE id = 1 + 1",
    "UPDATE t SET tag = 'z' WHERE id = (SELECT MAX(id) FROM t)",
    "UPDATE t SET tag = 'z' WHERE NOT id = 1",
    "DELETE FROM t WHERE id IN (1, 2)",
    "DELETE FROM t WHERE tag = NULL",
    "DELETE FROM t WHERE tag IS NULL",
]

# 制約違反。どちらの経路でも同じ例外になる必要がある
VIOLATION_CASES = [
    "UPDATE t SET score = -100 WHERE id = 1",  # CHECK
    "UPDATE t SET name = 'a' WHERE id = 2",  # UNIQUE
    "UPDATE t SET id = 2 WHERE id = 1",  # 主キー重複
    "UPDATE t SET name = NULL WHERE id = 1",  # NOT NULL
]


def _run(dbdir, sql, *, fast, monkeypatch):
    """1 文を実行して (rowcount, RETURNING の行, 実行後の全行) を返す。

    ``fast`` が偽なら高速パスの入口を塞いで還元だけを使わせる。実際に
    どちらを通ったかは _matching_positions の呼び出し回数で確かめる。
    """
    original = dml._matching_positions
    used = []

    def spy(rows, terms):
        used.append(terms)
        return original(rows, terms)

    connection = iceql.connect(dbdir)
    try:
        for statement in SETUP:
            connection.execute(statement)
        with monkeypatch.context() as patch:
            patch.setattr(dml, "_matching_positions", spy)
            if not fast:
                patch.setattr(dml, "_equality_terms", lambda *args: None)
            error = None
            try:
                cur = connection.execute(sql)
                outcome = (cur.rowcount, cur.fetchall())
            except IntegrityError as exc:
                error = str(exc)
                outcome = None
        state = connection.execute("SELECT * FROM t ORDER BY id").fetchall()
    finally:
        connection.close()
    return (outcome, error, state), bool(used)


@pytest.mark.parametrize("sql", FAST_CASES + SLOW_CASES + VIOLATION_CASES)
def test_fast_path_matches_reduction(tmp_path, monkeypatch, sql):
    fast, took_fast = _run(tmp_path / "fast", sql, fast=True, monkeypatch=monkeypatch)
    slow, took_slow = _run(tmp_path / "slow", sql, fast=False, monkeypatch=monkeypatch)
    assert not took_slow, "還元だけを使わせたはずが高速パスが動いた"
    assert fast == slow
    if sql in SLOW_CASES:
        assert not took_fast, f"高速パスに乗らないはずの文が乗った: {sql}"
    else:
        assert took_fast, f"高速パスに乗るはずの文が乗らなかった: {sql}"


def test_unknown_column_still_reports_error(tmp_path):
    """高速パスが受け取れない列名は、還元と同じエラーになる。"""
    connection = iceql.connect(tmp_path / "db")
    try:
        for statement in SETUP:
            connection.execute(statement)
        with pytest.raises(Exception, match="nosuch"):
            connection.execute("UPDATE t SET tag = 'z' WHERE nosuch = 1")
        with pytest.raises(Exception, match="nosuch"):
            connection.execute("DELETE FROM t WHERE nosuch = 1")
    finally:
        connection.close()


def test_fast_path_does_not_scan_with_sqlglot(tmp_path, monkeypatch):
    """高速パスは sqlglot の評価を 1 度も呼ばない。

    還元は 1 文ごとにテーブル全行を executor に通す。高速パスの狙いは
    その走査を無くすことなので、時間ではなく呼び出しの有無で固定する。
    """
    connection = iceql.connect(tmp_path / "db")
    try:
        connection.execute("CREATE TABLE u (id INTEGER PRIMARY KEY, v INTEGER)")
        connection.execute("INSERT INTO u (id, v) VALUES (1, 10), (2, 20), (3, 30)")
        calls = []
        original = dml.executor.evaluate
        monkeypatch.setattr(
            dml.executor,
            "evaluate",
            lambda *args: calls.append(args) or original(*args),
        )
        connection.execute("UPDATE u SET v = 99 WHERE id = 2")
        connection.execute("DELETE FROM u WHERE id = 3")
        assert calls == []
        connection.execute("UPDATE u SET v = v + 1 WHERE id = 1")
        assert len(calls) == 1  # 還元に落ちる形では呼ばれる
    finally:
        connection.close()


def test_update_without_key_columns_skips_key_scan(tmp_path, monkeypatch):
    """一意キーに触れない UPDATE は、キー全体の重複検査を省く。

    省けるのは、キーの値がどの行でも変わらないため。CHECK を変更行だけで
    検査しているのと同じ扱いで、高速パスと還元のどちらでも同じにする。
    """
    connection = iceql.connect(tmp_path / "db")
    try:
        for statement in SETUP:
            connection.execute(statement)
        calls = []
        monkeypatch.setattr(
            dml, "_check_primary_key", lambda *args, **kwargs: calls.append(args)
        )
        connection.execute("UPDATE t SET tag = 'z' WHERE id = 1")
        assert calls == []
        connection.execute("UPDATE t SET id = 7 WHERE id = 1")
        assert len(calls) == 1
    finally:
        connection.close()
