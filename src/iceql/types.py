"""型システムと CSV フィールドの encode/decode。

CSV 上の表現規約:
- NULL は非クォートの ``\\N`` (PostgreSQL COPY 方式)。空フィールドは空文字列。
- 文字列がたまたま ``\\N``, ``\\\\N`` ... の形のときはバックスラッシュを 1 つ足して退避する。
- boolean は ``true`` / ``false``、date は ``YYYY-MM-DD``、datetime は ISO-8601。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from iceql.errors import DataError

NULL_MARKER = r"\N"

_NULL_LIKE = re.compile(r"^\\+N$")

Value = int | float | bool | str | None


def encode_null(field: str | None) -> str:
    """型エンコード済みフィールドに NULL マーカー規約を適用する。"""
    if field is None:
        return NULL_MARKER
    if _NULL_LIKE.match(field):
        return "\\" + field
    return field


def decode_null(field: str) -> str | None:
    """CSV フィールドから NULL マーカー規約を剥がす。"""
    if field == NULL_MARKER:
        return None
    if _NULL_LIKE.match(field):
        return field[1:]
    return field


def _decode_integer(field: str) -> int:
    try:
        return int(field, 10)
    except ValueError:
        raise DataError(f"invalid integer literal: {field!r}") from None


def _encode_integer(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DataError(f"expected integer, got {value!r}")
    return str(value)


def _decode_real(field: str) -> float:
    try:
        return float(field)
    except ValueError:
        raise DataError(f"invalid real literal: {field!r}") from None


def _encode_real(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DataError(f"expected real, got {value!r}")
    return repr(float(value))


def _decode_boolean(field: str) -> bool:
    if field == "true":
        return True
    if field == "false":
        return False
    raise DataError(f"invalid boolean literal: {field!r} (expected 'true' or 'false')")


def _encode_boolean(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    # SQL 経由では TRUE/FALSE が 1/0 で届く処理系も多いため 0/1 は受け付ける
    if isinstance(value, int) and value in (0, 1):
        return "true" if value else "false"
    raise DataError(f"expected boolean, got {value!r}")


def _decode_text(field: str) -> str:
    return field


def _encode_text(value: Any) -> str:
    if not isinstance(value, str):
        raise DataError(f"expected text, got {value!r}")
    # NUL は csv モジュールが扱えず「平文で読める」という目的にも反するため拒否
    # (PostgreSQL の text 型と同じ制約)
    if "\x00" in value:
        raise DataError("text value must not contain NUL (\\x00)")
    return value


def _decode_date(field: str) -> str:
    try:
        date.fromisoformat(field)
    except ValueError:
        raise DataError(f"invalid date literal: {field!r} (expected YYYY-MM-DD)") from None
    return field


def _encode_date(value: Any) -> str:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return _decode_date(value)
    raise DataError(f"expected date, got {value!r}")


def _decode_datetime(field: str) -> str:
    try:
        datetime.fromisoformat(field)
    except ValueError:
        raise DataError(f"invalid datetime literal: {field!r} (expected ISO-8601)") from None
    return field


def _encode_datetime(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, str):
        return _decode_datetime(value)
    raise DataError(f"expected datetime, got {value!r}")


class TypeCodec:
    def __init__(
        self,
        name: str,
        decode: Callable[[str], Value],
        encode: Callable[[Any], str],
    ) -> None:
        self.name = name
        self._decode = decode
        self._encode = encode

    def decode(self, field: str | None) -> Value:
        """NULL 処理済みのフィールドをエンジン内表現へ変換する。"""
        if field is None:
            return None
        return self._decode(field)

    def encode(self, value: Any) -> str | None:
        """エンジン内表現を CSV フィールド(NULL 処理前)へ変換する。"""
        if value is None:
            return None
        return self._encode(value)


TYPES: dict[str, TypeCodec] = {
    "integer": TypeCodec("integer", _decode_integer, _encode_integer),
    "real": TypeCodec("real", _decode_real, _encode_real),
    "boolean": TypeCodec("boolean", _decode_boolean, _encode_boolean),
    "text": TypeCodec("text", _decode_text, _encode_text),
    "date": TypeCodec("date", _decode_date, _encode_date),
    "datetime": TypeCodec("datetime", _decode_datetime, _encode_datetime),
}


def get_type(name: str) -> TypeCodec:
    try:
        return TYPES[name]
    except KeyError:
        raise DataError(f"unknown type: {name!r} (expected one of {sorted(TYPES)})") from None
