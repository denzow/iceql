"""iceql: ストレージが平文(CSV + YAML)のローカル RDBMS。"""

from importlib.metadata import PackageNotFoundError, version

from iceql.api import Connection, Cursor, connect
from iceql.errors import (
    DatabaseError,
    DataError,
    Error,
    IntegrityError,
    InterfaceError,
    InternalError,
    NotSupportedError,
    OperationalError,
    ProgrammingError,
    Warning,
)

# バージョンは pyproject.toml が唯一の情報源
try:
    __version__ = version("iceql")
except PackageNotFoundError:  # 未インストールのソースツリーから import された場合
    __version__ = "0.0.0"

# DB-API 2.0 モジュール属性
apilevel = "2.0"
threadsafety = 1
paramstyle = "qmark"

__all__ = [
    "Connection",
    "Cursor",
    "DataError",
    "DatabaseError",
    "Error",
    "IntegrityError",
    "InterfaceError",
    "InternalError",
    "NotSupportedError",
    "OperationalError",
    "ProgrammingError",
    "Warning",
    "apilevel",
    "connect",
    "paramstyle",
    "threadsafety",
]
