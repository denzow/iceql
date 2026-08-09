"""SELECT へ渡す sqlglot テーブルの組み立てと、その使い回し。

sqlglot.executor へは行を dict のリストではなく Table として渡す。
ensure_tables は Table インスタンスを素通しするので、行ごと・列ごとの
列名正規化と別表現への複製が起きない(sqlglot_table 参照)。

Table 自体もクエリごとに作り直すと、CSV の読み直しと型デコードが毎回かかる。
そこで Catalog(= Connection)ごとに直前の Table を持ち、CSV とスキーマの
実体が変わっていなければそのまま使い回す。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from sqlglot.executor.table import Table
from sqlglot.schema import normalize_name

from iceql.storage import Row

# ファイルの実体が変わったかを判定する識別子: (inode, mtime_ns, size) の連結。
# iceql の書き込みは一時ファイルの os.replace なので、内容が変われば inode が変わる。
# 手で書き換えられた場合に備えて mtime と size も見る(mtime の分解能が粗い
# ファイルシステムでは、同一秒内の同サイズの上書きだけ検出できない)。
Stamp = tuple[int, ...]


def file_stamp(*paths: Path) -> Stamp | None:
    """複数ファイルのスタンプを連結して返す。1 つでも stat できなければ None。"""
    values: list[int] = []
    for path in paths:
        try:
            st = path.stat()
        except OSError:
            return None
        values += [st.st_ino, st.st_mtime_ns, st.st_size]
    return tuple(values)


def sqlglot_table(columns: Sequence[str], rows: list[Row]) -> Table:
    """行リストを共有したまま sqlglot の Table を組む。

    execute() が通す ensure_tables は Table インスタンスを素通しするので、
    行ごとの dict 変換とその複製が起きない。ただし素通しされる分、列名は
    sqlglot 側で正規化されないため、ここで normalize_name を通しておく。
    sqlglot は渡した行リストを読むだけで、走査結果は別の Table に溜める。
    """
    return Table(columns=tuple(normalize_name(c).name for c in columns), rows=rows)


class TableCache:
    """テーブル名 → (スタンプ, Table)。Catalog ごとに 1 つ持つ。

    保持するのは最後に読んだテーブルごとに 1 つだけなので、常駐量は
    その DB のテーブル数で頭打ちになる。

    スタンプの取得は行を読む前に行う。SELECT は flush ロックを共有で取ってから
    ここへ来るので、スタンプを見てから読み終えるまでの間に他プロセスが
    CSV を置き換えることはない。
    """

    def __init__(self) -> None:
        self._entries: dict[str, tuple[Stamp, Table]] = {}

    def get(self, table: str, stamp: Stamp | None, build: Callable[[], Table]) -> Table:
        """スタンプが前回と同じなら前回の Table を、違えば build() の結果を返す。

        stamp が None(ファイルが読めない)なら、キャッシュに載せずに毎回作る。
        """
        entry = self._entries.get(table)
        if stamp is not None and entry is not None and entry[0] == stamp:
            return entry[1]
        built = build()
        if stamp is None:
            self._entries.pop(table, None)
        else:
            self._entries[table] = (stamp, built)
        return built

    def discard(self, table: str) -> None:
        """キャッシュを捨てる。スタンプでも無効化されるが、書き込み・削除の
        直後に呼んで、使わない行を抱え続けないようにする。"""
        self._entries.pop(table, None)
