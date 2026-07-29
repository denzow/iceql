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

Running without arguments opens a REPL.

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

`-f table|csv|json` selects the output format (defaults to table on a TTY, csv when piped).
`iceql check mydb` validates schema/CSV consistency (types, NOT NULL, duplicate primary keys, canonical form) and exits non-zero on errors.
This makes hand-edited CSV files verifiable in CI or a pre-commit hook.

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

## Supported SQL

- SELECT: WHERE, JOIN (INNER / LEFT), GROUP BY, aggregate functions, HAVING, ORDER BY (with NULLS FIRST / LAST), LIMIT / OFFSET, DISTINCT, IN subqueries, CTE (WITH), UNION / UNION ALL
- DML: INSERT (VALUES / SELECT), UPDATE, DELETE
- DDL: CREATE TABLE, DROP TABLE, ALTER TABLE (ADD / DROP / RENAME COLUMN, RENAME TO)
- Transactions: BEGIN / COMMIT / ROLLBACK (changes are staged in memory and flushed on COMMIT; DDL is not allowed inside a transaction)
- Placeholders: `?` (qmark) and `:name` (named)

SQL parsing and SELECT execution are powered by [sqlglot](https://github.com/tobymao/sqlglot).
NULL ordering follows the SQLite default (NULL sorts smallest: first in ASC, last in DESC).

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

- Window functions, DISTINCT inside aggregates (e.g. `COUNT(DISTINCT x)`), and scalar subqueries in the SELECT list are not supported (they fail with a clear error)
- Transactions are per-connection with no isolation between connections; a COMMIT touching multiple tables is not atomic
- Tables are fully loaded into memory at query time; the intended scope is databases small enough for an LLM to read directly (tens of thousands of rows)
- Windows is not supported (inter-process locking uses fcntl)

## Development

```console
$ uv sync --all-groups
$ uv run pytest
$ uv run ruff check src tests
$ uv run mypy
```

The test suite includes differential tests that run the same queries against both iceql and sqlite3 and compare the results.
