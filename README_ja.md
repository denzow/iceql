# iceql

[English README](README.md)

ストレージに平文（CSV + YAML）を使うローカル RDBMS。
SQLite のように 1 ディレクトリで完結し、SQL で読み書きできる。

SQLite のデータベースファイルはバイナリなので、LLM にそのまま渡して内容を読ませることができない。
iceql はテーブルを CSV、スキーマを YAML で保存するため、LLM も人間もストレージを直接読める。
書き込みは常に正規形（LF、最小クォート、1 行 1 レコード）で行われるので、git diff で変更履歴を追える。

## インストール

```console
$ uv tool install iceql   # CLI として使う場合
$ uv add iceql            # ライブラリとして使う場合
```

## クイックスタート

データベースはただのディレクトリである。

```console
$ iceql mydb -c "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL, age INTEGER)"
$ iceql mydb -c "INSERT INTO users VALUES (1, 'alice', 30), (2, 'bob', NULL)"
$ iceql mydb -c "SELECT * FROM users WHERE age IS NULL"
id,name,age
2,bob,\N
```

作られたファイルはそのまま読める。

```console
$ cat mydb/users.csv
id,name,age
1,alice,30
2,bob,\N
$ cat mydb/users.schema.yaml
version: 1
table: users
columns:
- name: id
  type: integer
  nullable: false
  primary_key: true
- name: name
  type: text
  nullable: false
- name: age
  type: integer
  nullable: true
null_marker: \N
```

引数なしで起動すると REPL になる。

```console
$ iceql mydb
iceql> SELECT COUNT(*) FROM users;
_col_0
------
2
iceql> .tables
users
iceql> .quit
```

`-f table|csv|json` で出力形式を選べる（既定は TTY なら table、パイプなら csv）。
`iceql check mydb` はスキーマと CSV の整合性（型、NOT NULL、主キー重複、正規形）を検証し、問題があれば非ゼロで終了する。
手編集した CSV の検証を CI や pre-commit に組み込める。

## Python API

sqlite3 モジュールと同じ感覚で使える DB-API 2.0 ライクな API を持つ。

```python
import iceql

conn = iceql.connect("mydb")
conn.execute("INSERT INTO users VALUES (?, ?, ?)", (3, "carol", 25))
for row in conn.execute("SELECT name FROM users WHERE age > :min", {"min": 20}):
    print(row)
conn.close()
```

## 対応する SQL

- SELECT：WHERE、JOIN（INNER / LEFT）、GROUP BY、集約関数、HAVING、ORDER BY（NULLS FIRST / LAST 対応）、LIMIT / OFFSET、DISTINCT、IN サブクエリ、CTE（WITH）、UNION / UNION ALL
- DML：INSERT（VALUES / SELECT）、UPDATE、DELETE
- DDL：CREATE TABLE、DROP TABLE、ALTER TABLE（ADD / DROP / RENAME COLUMN、RENAME TO）
- トランザクション：BEGIN / COMMIT / ROLLBACK（変更はメモリに溜まり、COMMIT で一括書き出し。DDL はトランザクション内では使えない）
- プレースホルダ：`?`（qmark）と `:name`（named）

SQL のパースと SELECT の実行には [sqlglot](https://github.com/tobymao/sqlglot) を使っている。
NULL の順序は SQLite と同じ既定（NULL 最小：ASC で先頭、DESC で末尾）に揃えている。

## 型

| 型 | CSV 上の表現 |
|---|---|
| integer | 10 進整数 |
| real | 浮動小数点数（最短表現） |
| boolean | `true` / `false` |
| text | 文字列（`,` `"` 改行を含む場合のみクォート） |
| date | `YYYY-MM-DD` |
| datetime | ISO-8601 |

NULL は非クォートの `\N` で表す（PostgreSQL の COPY と同じ規約）。
空文字列は空フィールドなので、NULL と空文字列を区別できる。
文字列としての `\N` は `\\N` にエスケープされる。

## MCP サーバー

LLM エージェントから DB を直接読み書きするための MCP サーバーを内蔵している。

```console
$ uv tool install 'iceql[mcp]'
$ iceql mcp mydb --read-only   # --read-only を外すと書き込み系ツールも有効になる
```

ツールは query（SELECT のみ）、execute（DML / DDL）、list_tables、describe_table の 4 つ。

## 制限事項

- ウィンドウ関数、集約内の DISTINCT（`COUNT(DISTINCT x)` など）、SELECT 句のスカラサブクエリは未対応（明確なエラーになる）
- トランザクションは 1 接続内で完結し、複数接続の分離はない。COMMIT の複数テーブル書き出しはアトミックではない
- テーブルは実行時に全件メモリに載る。想定スコープは「LLM がそのまま読めるサイズ」（数万行規模）のデータベースである
- プロセス間ロックに fcntl を使うため、Windows は未対応

## 開発

```console
$ uv sync --all-groups
$ uv run pytest
$ uv run ruff check src tests
$ uv run mypy
```

テストには、同一のクエリを iceql と sqlite3 の両方に投げて結果を突き合わせる差分テストを含む。
