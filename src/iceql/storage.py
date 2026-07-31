"""CSV ファイルの読み書き(正規形)・アトミック置換・プロセス間ロック。

CSV 正規形:
- 改行は LF(読み込みは CRLF も受理)、QUOTE_MINIMAL、1 行目はヘッダ、末尾改行あり。
- 書き込みは常にこの正規形で行い、git diff のノイズを排除する。
"""

from __future__ import annotations

import csv
import fcntl
import os
import time
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


class _FileLock:
    """1 ファイルへの flock。Windows(fcntl 非対応)は現時点でスコープ外。"""

    def __init__(self, path: Path) -> None:
        self._path = path

    def _open(self) -> IO[bytes]:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        return self._path.open("ab")

    @contextmanager
    def locked(self, flags: int) -> Iterator[None]:
        f = self._open()
        try:
            fcntl.flock(f.fileno(), flags)
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            f.close()

    def acquire(self, flags: int, timeout: float) -> IO[bytes]:
        """fd を保持したままロックを取得する(トランザクション用)。"""
        f = self._open()
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(f.fileno(), flags | fcntl.LOCK_NB)
                return f
            except OSError:
                if time.monotonic() >= deadline:
                    f.close()
                    raise OperationalError(
                        "database is locked (another write transaction is active)"
                    ) from None
                time.sleep(0.01)

    @staticmethod
    def release(f: IO[bytes]) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()


class DatabaseLock:
    """DB ディレクトリ単位の 2 段ロック。

    - write ロック(<dbdir>/.iceql/lock.write): 書き手同士の直列化。
      autocommit の書き込み文は文の間、トランザクションは BEGIN〜COMMIT で保持する。
    - flush ロック(<dbdir>/.iceql/lock.flush): ディスク I/O の整合。
      SELECT のロードは共有、書き出しは排他で取り、複数テーブルの置換途中を読ませない。

    取得順は常に write → flush(逆順を作らないことでデッドロックを防ぐ)。
    これにより、トランザクションが長く開いていても SELECT は COMMIT の
    書き出し中(ミリ秒)しか待たない。
    """

    def __init__(self, dbdir: Path, timeout: float = 5.0) -> None:
        base = dbdir / ".iceql"
        self._write = _FileLock(base / "lock.write")
        self._flush = _FileLock(base / "lock.flush")
        self._timeout = timeout
        self._tx: IO[bytes] | None = None

    @property
    def in_transaction(self) -> bool:
        return self._tx is not None

    def begin(self) -> None:
        """write ロックを取得し、COMMIT / ROLLBACK まで保持する。"""
        assert self._tx is None
        self._tx = self._write.acquire(fcntl.LOCK_EX, self._timeout)

    def end(self) -> None:
        if self._tx is not None:
            _FileLock.release(self._tx)
            self._tx = None

    @contextmanager
    def read(self) -> Iterator[None]:
        """SELECT 用: フラッシュ中でなければ待たない。"""
        with self._flush.locked(fcntl.LOCK_SH):
            yield

    @contextmanager
    def write_statement(self) -> Iterator[None]:
        """書き込み文用。トランザクション外なら write + flush 排他、
        トランザクション中は write 保持済みで書き込み先はメモリなので
        ディスク読みの整合(flush 共有)だけ守る。"""
        if self._tx is not None:
            with self._flush.locked(fcntl.LOCK_SH):
                yield
        else:
            handle = self._write.acquire(fcntl.LOCK_EX, self._timeout)
            try:
                with self._flush.locked(fcntl.LOCK_EX):
                    yield
            finally:
                _FileLock.release(handle)

    @contextmanager
    def flush_commit(self) -> Iterator[None]:
        """COMMIT の書き出し用(write ロックは保持済みの前提)。"""
        with self._flush.locked(fcntl.LOCK_EX):
            yield
