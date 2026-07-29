"""CSV ファイルの読み書き(正規形)・アトミック置換・プロセス間ロック。

CSV 正規形:
- 改行は LF(読み込みは CRLF も受理)、QUOTE_MINIMAL、1 行目はヘッダ、末尾改行あり。
- 書き込みは常にこの正規形で行い、git diff のノイズを排除する。
"""

from __future__ import annotations

import csv
import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

from iceql.errors import OperationalError
from iceql.schema import TableSchema
from iceql.types import Value, decode_null, encode_null, get_type

Row = dict[str, Value]


def read_rows(path: Path, schema: TableSchema) -> list[Row]:
    """CSV ファイルを読み、型デコード済みの行リストを返す。"""
    try:
        f = path.open("r", encoding="utf-8", newline="")
    except OSError as exc:
        raise OperationalError(f"cannot read table file {path}: {exc}") from exc
    with f:
        reader = csv.reader(f)
        try:
            try:
                header = next(reader)
            except StopIteration:
                raise OperationalError(f"{path}: missing header row") from None
            if header != schema.column_names:
                raise OperationalError(
                    f"{path}: header {header!r} does not match schema columns "
                    f"{schema.column_names!r}"
                )
            rows: list[Row] = []
            codecs = [get_type(c.type) for c in schema.columns]
            for lineno, record in enumerate(reader, start=2):
                if len(record) != len(schema.columns):
                    raise OperationalError(
                        f"{path}: line {lineno}: expected {len(schema.columns)} fields, "
                        f"got {len(record)}"
                    )
                row: Row = {}
                for col, codec, field in zip(schema.columns, codecs, record, strict=False):
                    try:
                        row[col.name] = codec.decode(decode_null(field))
                    except Exception as exc:
                        raise OperationalError(
                            f"{path}: line {lineno}: column {col.name!r}: {exc}"
                        ) from exc
                rows.append(row)
        except csv.Error as exc:
            raise OperationalError(f"{path}: invalid CSV: {exc}") from exc
    return rows


def _format_field(field: str) -> str:
    # csv.writer は lineterminator="\n" のとき \r を含むフィールドをクォートせず
    # CSV を壊すため、正規形のクォート判定は自前で行う
    if any(c in field for c in ',"\n\r'):
        return '"' + field.replace('"', '""') + '"'
    return field


def _format_record(fields: list[str]) -> str:
    # 単一列で値が空文字列だと空行になり、csv.reader が 0 フィールドと
    # 解釈してしまうため、この場合だけ明示的にクォートする
    if len(fields) == 1 and fields[0] == "":
        return '""\n'
    return ",".join(_format_field(f) for f in fields) + "\n"


def encode_rows(rows: list[Row], schema: TableSchema) -> str:
    """行リストを CSV 正規形の文字列にエンコードする。"""
    parts = [_format_record(schema.column_names)]
    codecs = [get_type(c.type) for c in schema.columns]
    for row in rows:
        record = []
        for col, codec in zip(schema.columns, codecs, strict=False):
            record.append(encode_null(codec.encode(row.get(col.name))))
        parts.append(_format_record(record))
    return "".join(parts)


def atomic_write(path: Path, content: str) -> None:
    """temp ファイル + fsync + os.replace によるアトミック書き込み。"""
    tmp = path.parent / f".{path.name}.tmp.{os.getpid()}"
    try:
        with tmp.open("w", encoding="utf-8", newline="") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        raise OperationalError(f"cannot write {path}: {exc}") from exc
    finally:
        tmp.unlink(missing_ok=True)


def write_rows(path: Path, rows: list[Row], schema: TableSchema) -> None:
    atomic_write(path, encode_rows(rows, schema))


class DatabaseLock:
    """DB ディレクトリ単位のプロセス間ロック(<dbdir>/.iceql/lock に flock)。

    読み取りは共有ロック、書き込み文は排他ロックを取る。
    Windows(fcntl 非対応)は現時点でスコープ外。
    """

    def __init__(self, dbdir: Path) -> None:
        self._lock_path = dbdir / ".iceql" / "lock"

    def _open(self) -> IO[bytes]:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        return self._lock_path.open("ab")

    @contextmanager
    def _locked(self, flags: int) -> Iterator[None]:
        f = self._open()
        try:
            fcntl.flock(f.fileno(), flags)
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            f.close()

    @contextmanager
    def shared(self) -> Iterator[None]:
        with self._locked(fcntl.LOCK_SH):
            yield

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        with self._locked(fcntl.LOCK_EX):
            yield
