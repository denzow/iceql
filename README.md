# iceql

[日本語版 README](README_ja.md)

A local RDBMS with plaintext storage (CSV + YAML).
Like SQLite, a database is self-contained and queryable with SQL — but the storage stays human-readable.

SQLite database files are binary, so you cannot hand one to an LLM and have it read the contents directly.
iceql stores tables as CSV and schemas as YAML, so both LLMs and humans can read the storage as-is.
Writes always produce a canonical form (LF newlines, minimal quoting, one record per line), which keeps git diffs clean and meaningful.

## Installation

```console
$ uv tool install iceql   # as a CLI
$ uv add iceql            # as a library
```

## Quick start

A database is just a directory.

```console
$ iceql mydb -c "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL, age INTEGER)"
$ iceql mydb -c "INSERT INTO users VALUES (1, 'alice', 30), (2, 'bob', NULL)"
$ iceql mydb -c "SELECT * FROM users WHERE age IS NULL"
id,name,age
2,bob,\N
```

The resulting files are readable as-is.

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

Running without arguments opens a psql-style REPL.

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

Meta commands: `\d [table]` (list / describe tables), `\x` (toggle expanded output), `\pset format table|csv|json`, `\?` (help), and `\q` (quit).

`-f table|csv|json` selects the output format (defaults to table on a TTY, csv when piped).
With `-f csv`, NULL is written as `\N`, the same convention as the stored CSV files. `--null-marker` changes that for tools that expect something else: `iceql mydb -c "SELECT * FROM users" -f csv --null-marker '' > users.csv`. An empty marker makes a NULL and an empty string look the same in the output; the distinction is gone and cannot be recovered by reading the file back.

`iceql check mydb` validates schema/CSV consistency (types, NOT NULL, duplicate primary keys, UNIQUE, CHECK, canonical form) and exits non-zero on errors.
This makes hand-edited CSV files verifiable in CI or a pre-commit hook.

## Importing CSV

`iceql import` turns an existing CSV file into a table. Column names come from the header row, and types are inferred by scanning every row. The inferred schema is printed before anything is written.

```console
$ iceql import mydb users.csv
users.csv: 2 rows, 3 columns -> table 'users'
  id    integer  not null
  name  text     not null
  age   integer  null
imported 2 rows into users
```

```console
$ iceql import mydb users.csv --table people    # table name (default: the file name)
$ iceql import mydb users.csv --types zip=text  # override the inferred type (repeatable)
$ iceql import mydb users.csv --dry-run         # show the inferred schema and stop
$ cat users.csv | iceql import mydb - --table users
```

A type is only inferred when reading a value and writing it back leaves the text unchanged, so a zero-padded `007` stays `text` instead of silently becoming `7`. A column holding both integers and decimals becomes `real`. Anything else falls back to `text`, which loses nothing; `--types` overrides the result.

NULL follows the same convention as everywhere else in iceql: an unquoted `\N` is NULL and an empty field is the empty string. For CSV files where an empty field means NULL, pass `--null-marker ''` (or `--null-marker NA`, and so on). `--encoding` (default `utf-8-sig`) reads files that are not UTF-8.

A column is nullable only when it actually contains a NULL, primary keys are not inferred, and importing into an existing table is an error. When the inference does not match your intent, edit the generated `<table>.schema.yaml`.

## Python API

A DB-API 2.0 style API, familiar to anyone who has used the sqlite3 module.

```python
import iceql

conn = iceql.connect("mydb")
conn.execute("INSERT INTO users VALUES (?, ?, ?)", (3, "carol", 25))
for row in conn.execute("SELECT name FROM users WHERE age > :min", {"min": 20}):
    print(row)
conn.close()
```

Every write rewrites the whole CSV, so running one INSERT per row costs time proportional to the square of the row count.
`executemany` builds all the rows in memory and writes the file once.

```python
conn.executemany("INSERT INTO users VALUES (?, ?, ?)", rows)
```

For anything `executemany` does not cover (mixed statements, UPDATE against many keys), wrap the statements in a transaction: they are staged in memory and written once at COMMIT.

## Supported SQL

- SELECT: WHERE, JOIN (INNER / LEFT), GROUP BY, aggregate functions, HAVING, ORDER BY (with NULLS FIRST / LAST), LIMIT / OFFSET, DISTINCT, `IN` / `NOT IN` / `EXISTS` / `NOT EXISTS` subqueries, CTE (WITH), UNION / UNION ALL
- DML: INSERT (VALUES / SELECT), UPDATE, DELETE
- RETURNING: INSERT / UPDATE / DELETE hand back the rows they wrote through the same interface as SELECT (`description` and `fetchall()`). INSERT and UPDATE return the row as written, DELETE the row as it was before removal. Aggregate functions and subqueries are not allowed inside RETURNING
- Conflict resolution on INSERT: `INSERT OR IGNORE`, `INSERT OR REPLACE`, `ON CONFLICT ... DO NOTHING`, and `ON CONFLICT ... DO UPDATE` (including `excluded.<column>` and a `WHERE` clause). The conflict target must name the primary key or a UNIQUE constraint. `OR IGNORE` skips a row on any constraint violation, `DO NOTHING` only on a key collision — the same split SQLite makes
- DDL: CREATE TABLE, DROP TABLE, ALTER TABLE (ADD / DROP / RENAME COLUMN, RENAME TO)
- Constraints: PRIMARY KEY, NOT NULL, DEFAULT, UNIQUE, CHECK. UNIQUE and CHECK are recorded in the schema YAML and enforced on INSERT / UPDATE; `iceql check` applies the same two checks to hand-edited CSV. NULL follows SQLite: keys containing a NULL never collide, and a CHECK that evaluates to NULL passes
- Transactions: BEGIN / COMMIT / ROLLBACK (changes are staged in memory and flushed on COMMIT). DDL is allowed inside a transaction, so a schema change and the data migration that goes with it commit as one unit. Writers are serialized: BEGIN takes a database-wide write lock held until COMMIT / ROLLBACK, and other writers wait for it (up to `connect(timeout=...)` seconds, then `OperationalError`). SELECTs are never blocked by an open transaction — they only wait during the brief COMMIT flush. COMMITs are crash-safe: staged changes are first written to a redo journal, and an interrupted COMMIT is completed automatically the next time the database is opened
- Placeholders: `?` (qmark) and `:name` (named)

SQL parsing and SELECT execution are powered by [sqlglot](https://github.com/tobymao/sqlglot).
NULL ordering follows the SQLite default (NULL sorts smallest: first in ASC, last in DESC).

## Primary key auto-assignment

A table whose primary key is a single `integer` column assigns the value itself when an INSERT leaves it out or passes NULL.
The assigned value is the largest existing value plus one, or `1` when the table is empty.
`Cursor.lastrowid` holds the value given to the last inserted row (`None` for tables without such a primary key).

```console
$ iceql mydb -c "INSERT INTO users (name, age) VALUES ('dave', 41)"
$ iceql mydb -c "SELECT * FROM users WHERE name = 'dave'"
id,name,age
4,dave,41
```

`AUTOINCREMENT` is accepted on such a column and means exactly the same thing (it is an error anywhere else, as in SQLite).
It does not carry SQLite's guarantee that a value is never reused: deleting the row with the largest value makes that value available again.
Never reusing a value would require a counter stored outside the CSV, and hand-editing the CSV would then leave the counter behind.

## Types

| Type | CSV representation |
|---|---|
| integer | decimal integer |
| real | floating point (shortest round-trip form) |
| boolean | `true` / `false` |
| text | string (quoted only when it contains `,` `"` or newlines) |
| date | `YYYY-MM-DD` |
| datetime | ISO-8601 |

NULL is represented as an unquoted `\N` (the same convention as PostgreSQL COPY).
An empty string is an empty field, so NULL and the empty string are distinguishable.
A literal string `\N` is escaped as `\\N`.

## MCP server

A built-in MCP server lets LLM agents read and write the database directly.

```console
$ uv tool install 'iceql[mcp]'
$ iceql mcp mydb --read-only   # drop --read-only to enable write tools
```

Four tools are exposed: query (SELECT only), execute (DML / DDL), list_tables, and describe_table.

## Limitations

- Window functions, DISTINCT inside aggregates (e.g. `COUNT(DISTINCT x)`), scalar subqueries in the SELECT list, and `UPDATE ... FROM` are not supported (they fail with a clear error)
- A subquery with LIMIT / OFFSET is evaluated on its own before the outer query and replaced by its result, so LIMIT / OFFSET works in a FROM-clause subquery, a CTE, a scalar subquery, and an `IN` / `EXISTS` subquery. The evaluation is not short-circuited: the subquery is read in full even under `EXISTS`
- LIMIT / OFFSET in a correlated subquery (one that references a column of the outer query) is not supported, because a single evaluation cannot determine its result. It fails with a clear error
- A correlated `EXISTS` / `NOT EXISTS` is evaluated by rewriting it as a join. That rewrite applies when the correlation condition is a conjunction of comparisons placed directly in the subquery's WHERE, at least one of them an equality. Shapes without an equality (e.g. `EXISTS (SELECT 1 FROM u WHERE u.id > s.id)`), with an `OR` in the condition, or with a non-comparison predicate such as a `BETWEEN` over columns from both sides are not supported (they fail with a clear error); rewrite them as a comparison against an aggregate (`s.id < (SELECT MAX(u.id) FROM u)`) or as a join with `DISTINCT`
- Before that rewrite, the subquery of a correlated `EXISTS` is normalized in ways that cannot change whether it has rows: GROUP BY and ORDER BY are dropped, an aggregate without GROUP BY folds to true (it always returns one row), and a predicate determined by outer columns alone is moved out of the `EXISTS`. Without this the rewrite silently changes the result (GROUP BY makes the join key non-unique and multiplies outer rows, and a predicate on outer columns under a `NOT` loses its negation). A subquery with both GROUP BY and HAVING (HAVING changes whether rows exist, so GROUP BY cannot be dropped) and a `NOT` over a predicate that mixes inner and outer columns cannot be normalized and are not supported (they fail with a clear error)
- A correlated `EXISTS` / `NOT EXISTS` has to sit in a WHERE clause. The rewrite appends the subquery as a LEFT JOIN at the end of the FROM clause and leaves the test where it was, so the same `EXISTS` in a join's ON clause or in HAVING would read a column that is not there yet; both are not supported (they fail with a clear error). In an inner join's ON clause, move it to WHERE; in an outer join's ON clause, where moving it would change the result, move the subquery into the FROM clause and join against it; in HAVING, move it to WHERE if it only references grouped columns. An uncorrelated `EXISTS` folds to a boolean, so its position does not matter
- A `NOT IN` subquery is evaluated on its own and folded into an equivalent expression that keeps SQL's three-valued logic, so a NULL on either side behaves as it does in SQLite. The folded expression holds in any position, including one where NULL and false differ (inside CASE, under a second NOT, in the SELECT list). This needs the value set to be known up front, so a correlated `NOT IN` and a multi-column `NOT IN` are not supported; rewrite them as `NOT EXISTS`. Both fail with a clear error
- Tables are fully loaded into memory at query time; the intended scope is databases small enough for an LLM to read directly (tens of thousands of rows)
- UPDATE and DELETE take a fast path when the WHERE clause is absent or is only `column = constant` joined by AND, and (for UPDATE) every SET right-hand side is a constant or a bare column of the same table. Any other shape is evaluated by running the statement as a SELECT over the whole table, so its cost is proportional to the table size rather than to the number of rows changed
- UPDATE re-checks PRIMARY KEY and UNIQUE only when the SET clause assigns to a column of one of those keys. Key values in the other rows do not move, so an update cannot introduce a duplicate; a duplicate that was already in a hand-edited CSV is not reported. CHECK is likewise only evaluated on the rows the statement changed
- A connection keeps the tables it has read and reloads them when the inode, mtime, or size of the CSV or schema file changes. Writes from iceql always replace the file, so they are always picked up; on a filesystem with one-second mtime resolution, an external overwrite of the same size within the same second can be missed
- FOREIGN KEY is not supported; a `REFERENCES` clause in CREATE TABLE fails with a clear error rather than being silently dropped
- `INSERT OR ROLLBACK` and `REPLACE INTO` are not supported (both fail with a clear error); use `INSERT OR REPLACE` for the latter
- Windows is not supported (inter-process locking uses fcntl)

## Development

```console
$ uv sync --all-groups
$ uv run pytest
$ uv run ruff check src tests
$ uv run mypy
```

The test suite includes differential tests that run the same queries against both iceql and sqlite3 and compare the results.
