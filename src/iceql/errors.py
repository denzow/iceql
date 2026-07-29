"""PEP 249 準拠の例外階層。"""

from __future__ import annotations


class Warning(Exception):  # noqa: A001 - PEP 249 で規定された名前
    """重要な警告(データ切り捨て等)。"""


class Error(Exception):
    """iceql の全エラーの基底クラス。"""


class InterfaceError(Error):
    """データベースではなくインターフェースの誤用に起因するエラー。"""


class DatabaseError(Error):
    """データベースに関連するエラーの基底クラス。"""


class DataError(DatabaseError):
    """値の型不一致・範囲外などデータ処理に起因するエラー。"""


class OperationalError(DatabaseError):
    """ファイル破損・ロック失敗などデータベースの操作に起因するエラー。"""


class IntegrityError(DatabaseError):
    """PK 重複・NOT NULL 違反などリレーショナル整合性のエラー。"""


class InternalError(DatabaseError):
    """iceql 内部の不整合。"""


class ProgrammingError(DatabaseError):
    """SQL 構文エラー・存在しないテーブル参照などプログラミング上のエラー。"""


class NotSupportedError(DatabaseError):
    """iceql が対応していない SQL 機能が使われた。"""
