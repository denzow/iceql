"""DB ディレクトリの管理: テーブルの一覧・作成・削除・リネーム。"""

from __future__ import annotations

from pathlib import Path

from iceql import storage
from iceql.errors import OperationalError, ProgrammingError
from iceql.schema import TableSchema, dump_schema, read_schema_file, validate_identifier
from iceql.storage import Row


class Catalog:
    def __init__(self, dbdir: Path | str) -> None:
        self.root = Path(dbdir)
        if not self.root.is_dir():
            raise OperationalError(f"database directory does not exist: {self.root}")

    def csv_path(self, table: str) -> Path:
        validate_identifier(table, "table name")
        return self.root / f"{table}.csv"

    def schema_path(self, table: str) -> Path:
        validate_identifier(table, "table name")
        return self.root / f"{table}.schema.yaml"

    def list_tables(self) -> list[str]:
        names = []
        for path in sorted(self.root.glob("*.schema.yaml")):
            name = path.name[: -len(".schema.yaml")]
            if name and not name.startswith("."):
                names.append(name)
        return names

    def has_table(self, table: str) -> bool:
        return self.schema_path(table).is_file()

    def load_schema(self, table: str) -> TableSchema:
        path = self.schema_path(table)
        if not path.is_file():
            raise ProgrammingError(f"no such table: {table}")
        schema = read_schema_file(path)
        if schema.table != table:
            raise OperationalError(
                f"{path}: table name in schema ({schema.table!r}) does not match "
                f"file name ({table!r})"
            )
        return schema

    def read_rows(self, table: str) -> list[Row]:
        return storage.read_rows(self.csv_path(table), self.load_schema(table))

    def write_rows(self, table: str, rows: list[Row], schema: TableSchema) -> None:
        storage.write_rows(self.csv_path(table), rows, schema)

    def create_table(self, schema: TableSchema, *, if_not_exists: bool = False) -> None:
        if self.has_table(schema.table):
            if if_not_exists:
                return
            raise ProgrammingError(f"table already exists: {schema.table}")
        # CSV → schema の順(schema が現れた時点で CSV も存在している状態を保つ)
        storage.write_rows(self.csv_path(schema.table), [], schema)
        storage.atomic_write(self.schema_path(schema.table), dump_schema(schema))

    def write_table(self, schema: TableSchema, rows: list[Row]) -> None:
        """CSV とスキーマの両方を書き換える(ALTER 用)。CSV → schema の順。"""
        storage.write_rows(self.csv_path(schema.table), rows, schema)
        storage.atomic_write(self.schema_path(schema.table), dump_schema(schema))

    def drop_table(self, table: str, *, if_exists: bool = False) -> None:
        if not self.has_table(table):
            if if_exists:
                return
            raise ProgrammingError(f"no such table: {table}")
        # schema → CSV の順(schema が存在する間は CSV も存在している状態を保つ)
        self.schema_path(table).unlink(missing_ok=True)
        self.csv_path(table).unlink(missing_ok=True)

    def rename_table(self, old: str, new: str) -> None:
        schema = self.load_schema(old)
        if self.has_table(new):
            raise ProgrammingError(f"table already exists: {new}")
        rows = storage.read_rows(self.csv_path(old), schema)
        renamed = TableSchema(table=new, columns=schema.columns)
        self.create_table(renamed)
        self.write_rows(new, rows, renamed)
        self.drop_table(old)


def init_database(dbdir: Path | str) -> Catalog:
    """空の DB ディレクトリを作成する(既存の空ディレクトリも受理)。"""
    root = Path(dbdir)
    root.mkdir(parents=True, exist_ok=True)
    (root / ".iceql").mkdir(exist_ok=True)
    return Catalog(root)
