"""CREATE / DROP / ALTER TABLE の実行(AST の自前解釈)。"""

from __future__ import annotations

from sqlglot import exp

from iceql.catalog import Catalog
from iceql.engine import StatementResult
from iceql.engine.dml import _eval_constant
from iceql.errors import IntegrityError, NotSupportedError, ProgrammingError
from iceql.schema import Column, TableSchema

_DTYPE_MAP = {
    exp.DataType.Type.TINYINT: "integer",
    exp.DataType.Type.SMALLINT: "integer",
    exp.DataType.Type.MEDIUMINT: "integer",
    exp.DataType.Type.INT: "integer",
    exp.DataType.Type.BIGINT: "integer",
    exp.DataType.Type.FLOAT: "real",
    exp.DataType.Type.DOUBLE: "real",
    exp.DataType.Type.DECIMAL: "real",
    exp.DataType.Type.CHAR: "text",
    exp.DataType.Type.VARCHAR: "text",
    exp.DataType.Type.TEXT: "text",
    exp.DataType.Type.BOOLEAN: "boolean",
    exp.DataType.Type.DATE: "date",
    exp.DataType.Type.DATETIME: "datetime",
    exp.DataType.Type.TIMESTAMP: "datetime",
}


def _map_type(dtype: exp.DataType, column: str) -> str:
    mapped = _DTYPE_MAP.get(dtype.this)
    if mapped is None:
        raise NotSupportedError(
            f"unsupported column type for {column!r}: {dtype.sql(dialect='sqlite')}"
        )
    return mapped


def _build_column(coldef: exp.ColumnDef) -> Column:
    name = coldef.name
    if name == "_rowid_":
        raise ProgrammingError("column name '_rowid_' is reserved")
    dtype = coldef.args.get("kind")
    if dtype is None:
        raise ProgrammingError(f"column {name!r} must declare a type")
    nullable = True
    primary_key = False
    default = None
    for constraint in coldef.args.get("constraints") or []:
        kind = constraint.kind
        if isinstance(kind, exp.PrimaryKeyColumnConstraint):
            primary_key = True
        elif isinstance(kind, exp.NotNullColumnConstraint):
            nullable = bool(kind.args.get("allow_null"))
        elif isinstance(kind, exp.DefaultColumnConstraint):
            default = _eval_constant(kind.this)
        else:
            raise NotSupportedError(
                f"unsupported column constraint on {name!r}: "
                f"{constraint.sql(dialect='sqlite')}"
            )
    column = Column(
        name=name,
        type=_map_type(dtype, name),
        nullable=nullable,
        primary_key=primary_key,
        default=default,
    )
    if default is not None:
        # DEFAULT 値が型に合うか検証しておく
        codec_input = column.default
        from iceql.types import get_type

        get_type(column.type).encode(codec_input)
    return column


def run_create(catalog: Catalog, ast: exp.Create) -> StatementResult:
    if ast.kind != "TABLE":
        raise NotSupportedError(f"CREATE {ast.kind} is not supported")
    if ast.expression is not None:
        raise NotSupportedError("CREATE TABLE AS SELECT is not supported yet")
    schema_node = ast.this
    if not isinstance(schema_node, exp.Schema):
        raise ProgrammingError("CREATE TABLE requires a column list")
    table = schema_node.this.name

    columns: list[Column] = []
    table_pk: list[str] = []
    for item in schema_node.expressions:
        if isinstance(item, exp.ColumnDef):
            columns.append(_build_column(item))
        elif isinstance(item, exp.PrimaryKey):
            table_pk.extend(ident.name for ident in item.expressions)
        else:
            raise NotSupportedError(
                f"unsupported table constraint: {item.sql(dialect='sqlite')}"
            )
    if table_pk:
        by_name = {c.name: c for c in columns}
        for name in table_pk:
            if name not in by_name:
                raise ProgrammingError(f"PRIMARY KEY column not found: {name}")
            by_name[name].primary_key = True
            by_name[name].nullable = False

    schema = TableSchema(table=table, columns=columns)
    catalog.create_table(schema, if_not_exists=bool(ast.args.get("exists")))
    return StatementResult(rowcount=-1)


def run_drop(catalog: Catalog, ast: exp.Drop) -> StatementResult:
    if ast.kind != "TABLE":
        raise NotSupportedError(f"DROP {ast.kind} is not supported")
    catalog.drop_table(ast.this.name, if_exists=bool(ast.args.get("exists")))
    return StatementResult(rowcount=-1)


def _alter_add_column(
    catalog: Catalog, schema: TableSchema, coldef: exp.ColumnDef
) -> None:
    column = _build_column(coldef)
    if column.name in schema.column_names:
        raise ProgrammingError(f"column already exists: {schema.table}.{column.name}")
    if column.primary_key:
        raise NotSupportedError("cannot add a PRIMARY KEY column with ALTER TABLE")
    rows = catalog.read_rows(schema.table)
    if rows and not column.nullable and column.default is None:
        raise IntegrityError(
            f"cannot add NOT NULL column {column.name!r} without a DEFAULT "
            "to a non-empty table"
        )
    new_schema = TableSchema(table=schema.table, columns=[*schema.columns, column])
    for row in rows:
        row[column.name] = column.default
    catalog.write_table(new_schema, rows)


def _alter_drop_column(catalog: Catalog, schema: TableSchema, name: str) -> None:
    schema.column(name)  # 存在チェック
    remaining = [c for c in schema.columns if c.name != name]
    if not remaining:
        raise ProgrammingError(f"cannot drop the only column of {schema.table!r}")
    rows = catalog.read_rows(schema.table)
    new_schema = TableSchema(table=schema.table, columns=remaining)
    for row in rows:
        row.pop(name, None)
    catalog.write_table(new_schema, rows)


def _alter_rename_column(
    catalog: Catalog, schema: TableSchema, old: str, new: str
) -> None:
    schema.column(old)  # 存在チェック
    if new in schema.column_names:
        raise ProgrammingError(f"column already exists: {schema.table}.{new}")
    columns = [
        Column(
            name=new if c.name == old else c.name,
            type=c.type,
            nullable=c.nullable,
            primary_key=c.primary_key,
            default=c.default,
        )
        for c in schema.columns
    ]
    rows = catalog.read_rows(schema.table)
    new_schema = TableSchema(table=schema.table, columns=columns)
    for row in rows:
        row[new] = row.pop(old)
    catalog.write_table(new_schema, rows)


def run_alter(catalog: Catalog, ast: exp.Alter) -> StatementResult:
    if ast.args.get("kind") != "TABLE":
        raise NotSupportedError("only ALTER TABLE is supported")
    table = ast.this.name
    for action in ast.args.get("actions") or []:
        schema = catalog.load_schema(table)
        if isinstance(action, exp.ColumnDef):
            _alter_add_column(catalog, schema, action)
        elif isinstance(action, exp.Drop):
            _alter_drop_column(catalog, schema, action.this.name)
        elif isinstance(action, exp.RenameColumn):
            _alter_rename_column(
                catalog, schema, action.this.name, action.args["to"].name
            )
        elif isinstance(action, exp.AlterRename):
            catalog.rename_table(table, action.this.name)
            table = action.this.name
        else:
            raise NotSupportedError(
                f"unsupported ALTER TABLE action: {action.sql(dialect='sqlite')}"
            )
    return StatementResult(rowcount=-1)
