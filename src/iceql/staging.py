"""トランザクション用のステージング Catalog。

BEGIN 後の書き込みをメモリに溜め、COMMIT で一括してディスクへ書き出す。
読み取りはステージ済みテーブルを優先し、未変更テーブルはディスクから読む。

DDL もステージ上で表現する。CREATE / ALTER はステージにテーブルの内容を置き、
DROP と RENAME の旧名は削除済みを表す番兵を置く。テーブル名ごとに内容か番兵の
どちらか一方だけを持つので、同じ名前の作成と削除が衝突しない。
"""

from __future__ import annotations

from sqlglot.executor.table import Table

from iceql.catalog import Catalog
from iceql.errors import ProgrammingError
from iceql.schema import TableSchema
from iceql.storage import DatabaseLock, Row
from iceql.tablecache import sqlglot_table


class _Dropped:
    """ステージ上でテーブルが削除されたことを表す番兵。"""


DROPPED = _Dropped()

StagedEntry = tuple[TableSchema, list[Row]] | _Dropped


class StagedCatalog(Catalog):
    def __init__(self, base: Catalog) -> None:
        self._base = base
        self.root = base.root  # Catalog.__init__ のディレクトリ検査は済んでいる
        # 未ステージのテーブルは base と同じ CSV を読むので、キャッシュも共有する
        self._table_cache = base._table_cache
        self._staged: dict[str, StagedEntry] = {}

    def has_table(self, table: str) -> bool:
        entry = self._staged.get(table)
        if entry is None:
            return self._base.has_table(table)
        return not isinstance(entry, _Dropped)

    def list_tables(self) -> list[str]:
        names = set(self._base.list_tables())
        for name, entry in self._staged.items():
            if isinstance(entry, _Dropped):
                names.discard(name)
            else:
                names.add(name)
        return sorted(names)

    def load_schema(self, table: str) -> TableSchema:
        entry = self._staged.get(table)
        if isinstance(entry, _Dropped):
            raise ProgrammingError(f"no such table: {table}")
        if entry is not None:
            return entry[0]
        return self._base.load_schema(table)

    def read_rows(self, table: str) -> list[Row]:
        entry = self._staged.get(table)
        if isinstance(entry, _Dropped):
            raise ProgrammingError(f"no such table: {table}")
        if entry is not None:
            # DML は返されたリストを直接書き換えるため、ステージ本体と共有しない
            # (行そのものは tuple で不変なので、リストの複製だけで足りる)
            return list(entry[1])
        return self._base.read_rows(table)

    def query_table(self, table: str, schema: TableSchema) -> Table:
        entry = self._staged.get(table)
        if isinstance(entry, _Dropped):
            raise ProgrammingError(f"no such table: {table}")
        if entry is not None:
            # ステージ上の行はディスクに現れていないのでキャッシュに載せられない。
            # 行リストはステージ本体と共有する(sqlglot は読むだけなので安全)
            return sqlglot_table(schema.column_names, entry[1])
        return super().query_table(table, schema)

    def write_rows(self, table: str, rows: list[Row], schema: TableSchema) -> None:
        self._staged[table] = (schema, rows)

    def create_table(self, schema: TableSchema, *, if_not_exists: bool = False) -> None:
        if self.has_table(schema.table):
            if if_not_exists:
                return
            raise ProgrammingError(f"table already exists: {schema.table}")
        self._staged[schema.table] = (schema, [])

    def write_table(self, schema: TableSchema, rows: list[Row]) -> None:
        self._staged[schema.table] = (schema, rows)

    def drop_table(self, table: str, *, if_exists: bool = False) -> None:
        if not self.has_table(table):
            if if_exists:
                return
            raise ProgrammingError(f"no such table: {table}")
        self._staged[table] = DROPPED

    def rename_table(self, old: str, new: str) -> None:
        schema = self.load_schema(old)  # 無ければ ProgrammingError
        if self.has_table(new):
            raise ProgrammingError(f"table already exists: {new}")
        rows = self.read_rows(old)
        self._staged[new] = (TableSchema(table=new, columns=schema.columns), rows)
        self._staged[old] = DROPPED

    def flush(self, lock: DatabaseLock) -> None:
        """ステージ済みの変更をディスクへ書き出す(COMMIT)。

        先に全内容を redo ジャーナルとして原子的に置いてから各テーブルを
        置換・削除する。途中でクラッシュしても、次の接続がジャーナルを
        再適用してコミットを完成させる(journal.py 参照)。
        """
        from iceql import journal

        if self._staged:
            tables: dict[str, tuple[TableSchema, list[Row]]] = {}
            dropped: list[str] = []
            for name, entry in self._staged.items():
                if isinstance(entry, _Dropped):
                    dropped.append(name)
                else:
                    tables[name] = entry
            with lock.flush_commit():
                journal.write_journal(self._base.root, tables, dropped)
                # 置換を済ませてから削除する(改名は新名ができてから旧名が消える)
                for schema, rows in tables.values():
                    self._base.write_table(schema, rows)
                for name in dropped:
                    self._base.drop_table(name, if_exists=True)
                journal.clear_journal(self._base.root)
        self._staged.clear()
