"""COMMIT の全か無かを守る redo ジャーナル。

COMMIT はまずステージ内容全体を 1 ファイル(<dbdir>/.iceql/journal)として
原子的に置く。これがコミットの成立点になる。その後に各テーブルを置換・削除し、
最後にジャーナルを削除する。途中でクラッシュしても、次にデータベースへ触れた
接続がジャーナルを再適用(redo)してコミットを完成させる。redo は同じ内容を
置換し直して同じテーブルを消すだけなので冪等。

ジャーナルは置換するテーブルの内容(tables)と削除するテーブル名(dropped)を
持つ。version 1 は tables しか持たないが、旧バージョンが残したジャーナルからも
回復できるよう読み込みは受理する。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from iceql.catalog import Catalog
from iceql.errors import OperationalError
from iceql.schema import TableSchema, dump_schema, load_schema
from iceql.storage import DatabaseLock, Row, atomic_write, encode_rows

JOURNAL_VERSION = 2
SUPPORTED_JOURNAL_VERSIONS = (1, 2)


def journal_path(root: Path) -> Path:
    return root / ".iceql" / "journal"


def has_journal(root: Path) -> bool:
    return journal_path(root).is_file()


def write_journal(
    root: Path,
    tables: dict[str, tuple[TableSchema, list[Row]]],
    dropped: Iterable[str] = (),
) -> None:
    """ステージ内容をジャーナルとして原子的に置く(コミット成立点)。"""
    payload = {
        "version": JOURNAL_VERSION,
        "tables": {
            name: {"schema": dump_schema(schema), "csv": encode_rows(rows, schema)}
            for name, (schema, rows) in tables.items()
        },
        "dropped": sorted(dropped),
    }
    atomic_write(journal_path(root), json.dumps(payload, ensure_ascii=False))


def clear_journal(root: Path) -> None:
    journal_path(root).unlink(missing_ok=True)


def apply_journal(catalog: Catalog) -> list[str]:
    """ジャーナルの内容を再適用(redo)し、対象になったテーブル名を返す。"""
    path = journal_path(catalog.root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OperationalError(f"corrupt commit journal: {path}: {exc}") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("version") not in SUPPORTED_JOURNAL_VERSIONS
    ):
        raise OperationalError(
            f"unsupported commit journal version in {path}: "
            f"{payload.get('version') if isinstance(payload, dict) else payload!r}"
        )
    tables = payload.get("tables")
    if not isinstance(tables, dict):
        raise OperationalError(f"corrupt commit journal: {path}: missing tables")
    dropped = payload.get("dropped", [])  # version 1 は削除を持たない
    if not isinstance(dropped, list) or not all(isinstance(n, str) for n in dropped):
        raise OperationalError(f"corrupt commit journal: {path}: invalid dropped list")
    for name, entry in tables.items():
        schema_text, csv_text = entry.get("schema"), entry.get("csv")
        if not isinstance(schema_text, str) or not isinstance(csv_text, str):
            raise OperationalError(f"corrupt commit journal: {path}: table {name!r}")
        load_schema(schema_text, source=f"{path} ({name})")  # 内容の検証のみ
        # ジャーナル内の CSV / YAML は正規形テキストなのでそのまま置換する
        atomic_write(catalog.csv_path(name), csv_text)
        atomic_write(catalog.schema_path(name), schema_text)
    # 置換を済ませてから削除する(改名の redo が新名の作成 → 旧名の削除の順になる)
    for name in dropped:
        # schema → CSV の順(schema が存在する間は CSV も存在している状態を保つ)
        catalog.schema_path(name).unlink(missing_ok=True)
        catalog.csv_path(name).unlink(missing_ok=True)
    clear_journal(catalog.root)
    return sorted(set(tables) | set(dropped))


def recover_if_needed(catalog: Catalog, lock: DatabaseLock) -> None:
    """ジャーナルが残っていれば、ロックを取って再適用する。"""
    if not has_journal(catalog.root):  # ロック無しの速い事前チェック
        return
    with lock.recovery():
        if has_journal(catalog.root):  # ロック獲得までに他の接続が回復した可能性
            apply_journal(catalog)
