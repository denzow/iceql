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
    catalog.write_rows(
        "users",
        [
            {"id": 1, "name": "alice", "age": 30, "dept_id": 1,
             "joined": "2020-01-15", "active": True},
            {"id": 2, "name": "bob", "age": None, "dept_id": 2,
             "joined": "2021-06-01", "active": True},
            {"id": 3, "name": "carol", "age": 25, "dept_id": None,
             "joined": None, "active": False},
            {"id": 4, "name": "dave", "age": 35, "dept_id": 1,
             "joined": "2019-11-30", "active": True},
        ],
        users,
    )
    catalog.write_rows(
        "depts",
        [{"id": 1, "dept": "eng"}, {"id": 2, "dept": "sales"}],
        depts,
    )
    yield connection
    connection.close()
