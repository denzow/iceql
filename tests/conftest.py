import pytest

import iceql
from iceql.schema import Column, TableSchema


@pytest.fixture
def conn(tmp_path):
    """users / depts の 2 テーブル入りの接続。"""
    connection = iceql.connect(tmp_path / "db")
    catalog = connection._catalog
    users = TableSchema(
        table="users",
        columns=[
            Column(name="id", type="integer", primary_key=True),
            Column(name="name", type="text", nullable=False),
            Column(name="age", type="integer"),
            Column(name="dept_id", type="integer"),
            Column(name="joined", type="date"),
            Column(name="active", type="boolean", nullable=False, default=True),
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
    # 行は users / depts の列順に並べた tuple
    catalog.write_rows(
        "users",
        [
            (1, "alice", 30, 1, "2020-01-15", True),
            (2, "bob", None, 2, "2021-06-01", True),
            (3, "carol", 25, None, None, False),
            (4, "dave", 35, 1, "2019-11-30", True),
        ],
        users,
    )
    catalog.write_rows("depts", [(1, "eng"), (2, "sales")], depts)
    yield connection
    connection.close()
