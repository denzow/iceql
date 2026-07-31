"""COMMIT の全か無かを守る redo ジャーナル。

COMMIT はまずステージ内容全体を 1 ファイル(<dbdir>/.iceql/journal)として
原子的に置く。これがコミットの成立点になる。その後に各テーブルを置換し、
最後にジャーナルを削除する。テーブル置換の途中でクラッシュしても、
次にデータベースへ触れた接続がジャーナルを再適用(redo)してコミットを
完成させる。redo は同じ内容を置換し直すだけなので冪等。
"""

from __future__ import annotations

import json
from pathlib import Path

from iceql.catalog import Catalog
from iceql.errors import OperationalError
from iceql.schema import TableSchema, dump_schema, load_schema
from iceql.storage import DatabaseLock, Row, atomic_write, encode_rows

JOURNAL_VERSION = 1


def journal_path(root: Path) -> Path:
    return root / ".iceql" / "journal"


def has_journal(root: Path) -> bool:
    return journal_path(root).is_file()


def write_journal(root: Path, staged: dict[str, tuple[TableSchema, list[Row]]]) -> None:
    """ステージ内容をジャーナルとして原子的に置く(コミット成立点)。"""
    payload = {
        "version": JOURNAL_VERSION,
        "tables": {
            name: {"schema": dump_schema(schema), "csv": encode_rows(rows, schema)}
            for name, (schema, rows) in staged.items()
        },
    }
    atomic_write(journal_path(root), json.dumps(payload, ensure_ascii=False))


def clear_journal(root: Path) -> None:
    journal_path(root).unlink(missing_ok=True)


def apply_journal(catalog: Catalog) -> list[str]:
    """ジャーナルの内容を再適用(redo)し、適用したテーブル名を返す。"""
    path = journal_path(catalog.root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OperationalError(f"corrupt commit journal: {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("version") != JOURNAL_VERSION:
        raise OperationalError(
            f"unsupported commit journal version in {path}: "
            f"{payload.get('version') if isinstance(payload, dict) else payload!r}"
        )
    tables = payload.get("tables")
    if not isinstance(tables, dict):
        raise OperationalError(f"corrupt commit journal: {path}: missing tables")
    for name, entry in tables.items():
        schema_text, csv_text = entry.get("schema"), entry.get("csv")
        if not isinstance(schema_text, str) or not isinstance(csv_text, str):
            raise OperationalError(f"corrupt commit journal: {path}: table {name!r}")
        load_schema(schema_text, source=f"{path} ({name})")  # 内容の検証のみ
        # ジャーナル内の CSV / YAML は正規形テキストなのでそのまま置換する
        atomic_write(catalog.csv_path(name), csv_text)
        atomic_write(catalog.schema_path(name), schema_text)
    clear_journal(catalog.root)
    return sorted(tables)


def recover_if_needed(catalog: Catalog, lock: DatabaseLock) -> None:
    """ジャーナルが残っていれば、ロックを取って再適用する。"""
    if not has_journal(catalog.root):  # ロック無しの速い事前チェック
        return
    with lock.recovery():
        if has_journal(catalog.root):  # ロック獲得までに他の接続が回復した可能性
            apply_journal(catalog)
