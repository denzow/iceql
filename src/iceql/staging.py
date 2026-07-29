"""トランザクション用のステージング Catalog。

BEGIN 後の書き込みをメモリに溜め、COMMIT で一括してディスクへ書き出す。
読み取りはステージ済みテーブルを優先し、未変更テーブルはディスクから読む。
DDL はトランザクション内では未対応。
"""

from __future__ import annotations

from iceql.catalog import Catalog
from iceql.errors import NotSupportedError
from iceql.schema import TableSchema
from iceql.storage import DatabaseLock, Row


class StagedCatalog(Catalog):
    def __init__(self, base: Catalog) -> None:
        self._base = base
        self.root = base.root  # Catalog.__init__ のディレクトリ検査は済んでいる
        self._staged: dict[str, tuple[TableSchema, list[Row]]] = {}

    def load_schema(self, table: str) -> TableSchema:
        if table in self._staged:
            return self._staged[table][0]
        return self._base.load_schema(table)

    def read_rows(self, table: str) -> list[Row]:
        if table in self._staged:
            # DML は返された行を直接書き換えるため、ステージ本体と共有しない
            return [dict(row) for row in self._staged[table][1]]
        return self._base.read_rows(table)

    def write_rows(self, table: str, rows: list[Row], schema: TableSchema) -> None:
        self._staged[table] = (schema, rows)

    def create_table(self, schema: TableSchema, *, if_not_exists: bool = False) -> None:
        raise NotSupportedError("DDL is not supported inside a transaction")

    def write_table(self, schema: TableSchema, rows: list[Row]) -> None:
        raise NotSupportedError("DDL is not supported inside a transaction")

    def drop_table(self, table: str, *, if_exists: bool = False) -> None:
        raise NotSupportedError("DDL is not supported inside a transaction")

    def rename_table(self, old: str, new: str) -> None:
        raise NotSupportedError("DDL is not supported inside a transaction")

    def flush(self, lock: DatabaseLock) -> None:
        """ステージ済みテーブルをディスクへ書き出す(COMMIT)。"""
        with lock.exclusive():
            for table, (schema, rows) in self._staged.items():
                self._base.write_rows(table, rows, schema)
        self._staged.clear()
