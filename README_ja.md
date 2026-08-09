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

引数なしで起動すると psql 風の REPL になる。

```console
$ iceql mydb
iceql (0.1.1)
Type "\?" for help.
mydb=# SELECT COUNT(*) FROM users;
_col_0
------
2
mydb=# \d
List of tables
  users
mydb=# \x
Expanded display is on.
mydb=# SELECT * FROM users;
-[ RECORD 1 ]-
id   | 1
name | alice
age  | 30
-[ RECORD 2 ]-
id   | 2
name | bob
age  |
mydb=# \q
```

メタコマンドは `\d [table]`（テーブル一覧・スキーマ表示）、`\x`（拡張表示の切り替え）、`\pset format table|csv|json`、`\?`（ヘルプ）、`\q`（終了）。

`-f table|csv|json` で出力形式を選べる（既定は TTY なら table、パイプなら csv）。
`iceql check mydb` はスキーマと CSV の整合性（型、NOT NULL、主キー重複、正規形）を検証し、問題があれば非ゼロで終了する。
手編集した CSV の検証を CI や pre-commit に組み込める。

## CSV の取り込み

`iceql import` は手元の CSV をテーブルにする。
列名はヘッダ行から取り、型は全行を走査して推論する。
推論結果は書き込みの前に表示される。

```console
$ iceql import mydb users.csv
users.csv: 2 rows, 3 columns -> table 'users'
  id    integer  not null
  name  text     not null
  age   integer  null
imported 2 rows into users
```

```console
$ iceql import mydb users.csv --table people    # テーブル名（既定はファイル名）
$ iceql import mydb users.csv --types zip=text  # 推論の上書き（繰り返し可）
$ iceql import mydb users.csv --dry-run         # 推論結果だけ表示して終了
$ cat users.csv | iceql import mydb - --table users
```

型は「読んで書き戻したときに表記が変わらない」場合にだけ推論する。
ゼロ埋めの `007` が黙って `7` にならず `text` のまま残るのはこのためである。
整数と小数が混ざった列は `real` になり、どれにも当てはまらない列は情報を落とさない `text` になる。
推論が意図と違うときは `--types` で上書きする。

NULL の規約は iceql の他の入出力と同じで、クォートされていない `\N` が NULL、空フィールドは空文字列である。
空フィールドを NULL とする CSV では `--null-marker ''` を渡す（`--null-marker NA` のような指定もできる）。
UTF-8 でないファイルは `--encoding`（既定は `utf-8-sig`）で読む。

nullable になるのは実際に NULL を含む列だけで、主キーは推論しない。
既存のテーブルへの取り込みはエラーになる。
推論結果が意図と違えば、生成された `<table>.schema.yaml` を手で直せばよい。

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

書き込みは毎回 CSV 全体を書き直すため、1 行につき 1 文の INSERT を実行すると総コストが行数の二乗になる。
`executemany` は全パラメータ分の行をメモリ上で組み立ててから 1 回だけ書き出す。

```python
conn.executemany("INSERT INTO users VALUES (?, ?, ?)", rows)
```

`executemany` で書けない場合（種類の違う文が混ざる、多数のキーに対する UPDATE など）は、トランザクションで囲む。
変更はメモリに溜まり、COMMIT で 1 回だけ書き出される。

## 対応する SQL

- SELECT：WHERE、JOIN（INNER / LEFT）、GROUP BY、集約関数、HAVING、ORDER BY（NULLS FIRST / LAST 対応）、LIMIT / OFFSET、DISTINCT、IN サブクエリ、CTE（WITH）、UNION / UNION ALL
- DML：INSERT（VALUES / SELECT）、UPDATE、DELETE
- DDL：CREATE TABLE、DROP TABLE、ALTER TABLE（ADD / DROP / RENAME COLUMN、RENAME TO）
- トランザクション：BEGIN / COMMIT / ROLLBACK（変更はメモリに溜まり、COMMIT で一括書き出し）。
  DDL もトランザクション内で使えるため、スキーマ変更とそれに伴うデータ移行を 1 つの単位でコミットできる。
  書き手同士は直列化される。BEGIN は DB 全体の書き込みロックを COMMIT / ROLLBACK まで保持し、後発の書き手はその解放を待つ（`connect(timeout=...)` 秒を超えると `OperationalError`）。
  SELECT は開いているトランザクションにブロックされず、COMMIT の書き出し中だけ瞬間的に待つ。
  COMMIT はクラッシュ耐性を持つ。ステージ内容を先に redo ジャーナルへ書いてからテーブルを置換するため、途中でクラッシュしても次にデータベースを開いたときにコミットが自動で完成する
- プレースホルダ：`?`（qmark）と `:name`（named）

SQL のパースと SELECT の実行には [sqlglot](https://github.com/tobymao/sqlglot) を使っている。
NULL の順序は SQLite と同じ既定（NULL 最小：ASC で先頭、DESC で末尾）に揃えている。

## 主キーの自動採番

主キーが単一の `integer` 列であるテーブルは、INSERT でその値を省略するか NULL を渡すと自分で採番する。
入る値は既存の最大値 + 1 で、行が無ければ `1` である。
採番された値は `Cursor.lastrowid` から読める（この形の主キーを持たないテーブルでは `None`）。

```console
$ iceql mydb -c "INSERT INTO users (name, age) VALUES ('dave', 41)"
$ iceql mydb -c "SELECT * FROM users WHERE name = 'dave'"
id,name,age
4,dave,41
```

`AUTOINCREMENT` を付けても意味は同じである（SQLite と同様、それ以外の列に付けるとエラーになる）。
ただし SQLite の `AUTOINCREMENT` が持つ「一度使った値を二度と使わない」保証はなく、最大値の行を削除すればその値は再び採番される。
再利用しないためには採番済みの値を CSV の外に覚える必要があり、CSV を手で書き換えたときにその記録だけが取り残される。

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

- ウィンドウ関数、集約内の DISTINCT（`COUNT(DISTINCT x)` など）、SELECT 句のスカラサブクエリ、`UPDATE ... FROM` は未対応（明確なエラーになる）
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
