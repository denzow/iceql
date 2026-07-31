"""複数接続間のロック挙動(2 段ロック方式)のテスト。"""

import threading
import time

import pytest

import iceql
from iceql.errors import OperationalError


@pytest.fixture
def db(tmp_path):
    conn = iceql.connect(tmp_path / "db")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER)")
    conn.execute("INSERT INTO t VALUES (1, 100)")
    yield tmp_path / "db", conn
    conn.close()


def test_select_not_blocked_by_open_transaction(db):
    path, a = db
    b = iceql.connect(path)
    a.execute("BEGIN")
    a.execute("UPDATE t SET v = 1 WHERE id = 1")
    # A のトランザクションが開いたままでも B の SELECT は待たず、コミット前の値を見る
    assert b.execute("SELECT v FROM t").fetchone() == (100,)
    a.execute("COMMIT")
    assert b.execute("SELECT v FROM t").fetchone() == (1,)
    b.close()


def test_writer_waits_for_commit_and_no_lost_update(db):
    path, a = db
    b = iceql.connect(path)
    a.execute("BEGIN")
    a.execute("UPDATE t SET v = v + 1 WHERE id = 1")

    done = threading.Event()

    def run_b():
        b.execute("UPDATE t SET v = v + 50 WHERE id = 1")
        done.set()

    thread = threading.Thread(target=run_b)
    thread.start()
    time.sleep(0.3)
    assert not done.is_set(), "B の書き込みは A の COMMIT まで待機するはず"

    a.execute("COMMIT")
    assert done.wait(timeout=5), "A の COMMIT 後に B の書き込みが完了するはず"
    thread.join()
    # 直列化されて両方の加算が反映される(以前は last-writer-wins で 101 になっていた)
    assert a.execute("SELECT v FROM t").fetchone() == (151,)
    b.close()


def test_concurrent_transaction_times_out(db):
    path, a = db
    b = iceql.connect(path, timeout=0.2)
    a.execute("BEGIN")
    with pytest.raises(OperationalError, match="locked"):
        b.execute("BEGIN")
    a.execute("ROLLBACK")
    # 解放後は取得できる
    b.execute("BEGIN")
    b.execute("COMMIT")
    b.close()


def test_autocommit_write_times_out_against_transaction(db):
    path, a = db
    b = iceql.connect(path, timeout=0.2)
    a.execute("BEGIN")
    with pytest.raises(OperationalError, match="locked"):
        b.execute("UPDATE t SET v = 0")
    a.execute("ROLLBACK")
    b.close()


def test_close_releases_write_lock(db):
    path, a = db
    a.execute("BEGIN")
    a.execute("UPDATE t SET v = 999")
    a.close()  # ROLLBACK 相当でロックも解放
    b = iceql.connect(path, timeout=0.5)
    b.execute("BEGIN")
    assert b.execute("SELECT v FROM t").fetchone() == (100,)
    b.execute("COMMIT")
    b.close()


def test_sequential_transactions_on_same_connection(db):
    _, a = db
    for _ in range(3):
        a.execute("BEGIN")
        a.execute("UPDATE t SET v = v + 1 WHERE id = 1")
        a.execute("COMMIT")
    assert a.execute("SELECT v FROM t").fetchone() == (103,)


def test_select_during_own_transaction(db):
    _, a = db
    a.execute("BEGIN")
    a.execute("UPDATE t SET v = 7 WHERE id = 1")
    assert a.execute("SELECT v FROM t").fetchone() == (7,)
    a.execute("ROLLBACK")
