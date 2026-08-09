"""CREATE / DROP / ALTER TABLE の実行(AST の自前解釈)。"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlglot import exp

from iceql.catalog import Catalog
from iceql.engine import SQL_DIALECT, StatementResult, node_arg
from iceql.engine.dml import _eval_constant, check_expressions
from iceql.errors import IntegrityError, NotSupportedError, ProgrammingError
from iceql.schema import CheckConstraint, Column, TableSchema, UniqueConstraint

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


@dataclass
class _TableConstraints:
    """表レベルに正規化した制約。列に書かれた UNIQUE / CHECK もここへ集める。

    列レベルの CHECK は対象列が式から一意に決まらないので、表レベルと
    区別せずに持つ。
    """

    unique: list[UniqueConstraint] = field(default_factory=list)
    checks: list[CheckConstraint] = field(default_factory=list)


def _constraint_name(node: exp.Expression) -> str | None:
    """CONSTRAINT 句で付いた名前。無名なら None。"""
    name = node.args.get("this")
    return name.name if isinstance(name, exp.Identifier) else None


def _build_column(coldef: exp.ColumnDef, constraints: _TableConstraints) -> Column:
    name = coldef.name
    if name == "_rowid_":
        raise ProgrammingError("column name '_rowid_' is reserved")
    dtype = node_arg(coldef, "kind")
    if dtype is None:
        raise ProgrammingError(f"column {name!r} must declare a type")
    nullable = True
    primary_key = False
    default = None
    autoincrement = False
    for constraint in node_arg(coldef, "constraints") or []:
        kind = constraint.kind
        if isinstance(kind, exp.PrimaryKeyColumnConstraint):
            primary_key = True
        elif isinstance(kind, exp.NotNullColumnConstraint):
            nullable = bool(node_arg(kind, "allow_null"))
        elif isinstance(kind, exp.DefaultColumnConstraint):
            default = _eval_constant(kind.this)
        elif isinstance(kind, exp.AutoIncrementColumnConstraint):
            # INTEGER PRIMARY KEY は AUTOINCREMENT の有無によらず採番するので、
            # キーワードは受理するだけで何も記録しない(README に差分を明記)
            autoincrement = True
        elif isinstance(kind, exp.UniqueColumnConstraint):
            constraints.unique.append(
                UniqueConstraint(columns=[name], name=_constraint_name(constraint))
            )
        elif isinstance(kind, exp.CheckColumnConstraint):
            constraints.checks.append(
                CheckConstraint(
                    expr=kind.this.sql(dialect=SQL_DIALECT),
                    name=_constraint_name(constraint),
                )
            )
        elif isinstance(kind, exp.Reference):
            raise NotSupportedError(
                f"FOREIGN KEY is not supported: {constraint.sql(dialect=SQL_DIALECT)}"
            )
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
    if autoincrement and not (column.primary_key and column.type == "integer"):
        raise ProgrammingError(
            f"AUTOINCREMENT is only allowed on an INTEGER PRIMARY KEY: {name!r}"
        )
    if default is not None:
        # DEFAULT 値が型に合うか検証しておく
        codec_input = column.default
        from iceql.types import get_type

        get_type(column.type).encode(codec_input)
    return column


def _add_table_constraint(
    item: exp.Expression, constraints: _TableConstraints, name: str | None = None
) -> None:
    """表レベルの制約を読み取って _TableConstraints へ足す。"""
    if isinstance(item, exp.Constraint):
        # CONSTRAINT <名前> <制約> は名前つきの入れ物として現れる
        for inner in item.expressions:
            _add_table_constraint(inner, constraints, item.this.name)
        return
    if isinstance(item, exp.UniqueColumnConstraint):
        target = item.this
        if not isinstance(target, exp.Schema):
            raise NotSupportedError(
                f"unsupported table constraint: {item.sql(dialect=SQL_DIALECT)}"
            )
        constraints.unique.append(
            UniqueConstraint(
                columns=[ident.name for ident in target.expressions], name=name
            )
        )
        return
    if isinstance(item, exp.CheckColumnConstraint):
        constraints.checks.append(
            CheckConstraint(expr=item.this.sql(dialect=SQL_DIALECT), name=name)
        )
        return
    if isinstance(item, (exp.ForeignKey, exp.Reference)):
        raise NotSupportedError(
            f"FOREIGN KEY is not supported: {item.sql(dialect=SQL_DIALECT)}"
        )
    raise NotSupportedError(
        f"unsupported table constraint: {item.sql(dialect='sqlite')}"
    )


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
    constraints = _TableConstraints()
    for item in schema_node.expressions:
        if isinstance(item, exp.ColumnDef):
            columns.append(_build_column(item, constraints))
        elif isinstance(item, exp.PrimaryKey):
            table_pk.extend(ident.name for ident in item.expressions)
        else:
            _add_table_constraint(item, constraints)
    if table_pk:
        by_name = {c.name: c for c in columns}
        for name in table_pk:
            if name not in by_name:
                raise ProgrammingError(f"PRIMARY KEY column not found: {name}")
            by_name[name].primary_key = True
            by_name[name].nullable = False

    schema = TableSchema(
        table=table,
        columns=columns,
        unique=constraints.unique,
        checks=constraints.checks,
    )
    catalog.create_table(schema, if_not_exists=bool(node_arg(ast, "exists")))
    return StatementResult(rowcount=-1)


def run_drop(catalog: Catalog, ast: exp.Drop) -> StatementResult:
    if ast.kind != "TABLE":
        raise NotSupportedError(f"DROP {ast.kind} is not supported")
    catalog.drop_table(ast.this.name, if_exists=bool(node_arg(ast, "exists")))
    return StatementResult(rowcount=-1)


def _alter_add_column(
    catalog: Catalog, schema: TableSchema, coldef: exp.ColumnDef
) -> None:
    added = _TableConstraints()
    column = _build_column(coldef, added)
    if column.name in schema.column_names:
        raise ProgrammingError(f"column already exists: {schema.table}.{column.name}")
    if column.primary_key:
        raise NotSupportedError("cannot add a PRIMARY KEY column with ALTER TABLE")
    if added.unique:
        # 既存の行にはすべて同じ既定値が入るので、非空のテーブルでは必ず重複する。
        # sqlite も同じ理由で ADD COLUMN の UNIQUE を拒む
        raise NotSupportedError("cannot add a UNIQUE column with ALTER TABLE")
    rows = catalog.read_rows(schema.table)
    if rows and not column.nullable and column.default is None:
        raise IntegrityError(
            f"cannot add NOT NULL column {column.name!r} without a DEFAULT "
            "to a non-empty table"
        )
    new_schema = TableSchema(
        table=schema.table,
        columns=[*schema.columns, column],
        unique=schema.unique,
        checks=[*schema.checks, *added.checks],
    )
    new_rows = [(*row, column.default) for row in rows]
    check_expressions(new_schema, new_rows)
    catalog.write_table(new_schema, new_rows)


def _constraints_using(schema: TableSchema, name: str) -> list[str]:
    """列 name を参照している UNIQUE / CHECK 制約の表示名。"""
    using = [u.label for u in schema.unique if name in u.columns]
    using += [
        c.label
        for c in schema.checks
        if any(column.name == name for column in c.node.find_all(exp.Column))
    ]
    return using


def _alter_drop_column(catalog: Catalog, schema: TableSchema, name: str) -> None:
    index = schema.column_index(name)  # 存在チェックを兼ねる
    remaining = [c for c in schema.columns if c.name != name]
    if not remaining:
        raise ProgrammingError(f"cannot drop the only column of {schema.table!r}")
    using = _constraints_using(schema, name)
    if using:
        raise ProgrammingError(
            f"cannot drop column {schema.table}.{name}: "
            f"used by constraint {', '.join(using)}"
        )
    rows = catalog.read_rows(schema.table)
    new_schema = TableSchema(
        table=schema.table,
        columns=remaining,
        unique=schema.unique,
        checks=schema.checks,
    )
    catalog.write_table(new_schema, [row[:index] + row[index + 1 :] for row in rows])


def _rename_in_expression(node: exp.Expression, old: str, new: str) -> str:
    """CHECK 式の中の列参照を付け替えて SQL 文字列に戻す。"""
    renamed = node.copy()
    for column in renamed.find_all(exp.Column):
        if column.name == old:
            column.set("this", exp.to_identifier(new))
    return renamed.sql(dialect=SQL_DIALECT)


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
    unique = [
        UniqueConstraint(
            columns=[new if name == old else name for name in u.columns], name=u.name
        )
        for u in schema.unique
    ]
    checks = [
        CheckConstraint(expr=_rename_in_expression(c.node, old, new), name=c.name)
        for c in schema.checks
    ]
    # 行は列順に並んだ値なので、列名を変えても中身は動かない
    new_schema = TableSchema(
        table=schema.table, columns=columns, unique=unique, checks=checks
    )
    catalog.write_table(new_schema, catalog.read_rows(schema.table))


def run_alter(catalog: Catalog, ast: exp.Alter) -> StatementResult:
    if node_arg(ast, "kind") != "TABLE":
        raise NotSupportedError("only ALTER TABLE is supported")
    table = ast.this.name
    for action in node_arg(ast, "actions") or []:
        schema = catalog.load_schema(table)
        if isinstance(action, exp.ColumnDef):
            _alter_add_column(catalog, schema, action)
        elif isinstance(action, exp.Drop):
            _alter_drop_column(catalog, schema, action.this.name)
        elif isinstance(action, exp.RenameColumn):
            _alter_rename_column(
                catalog, schema, action.this.name, node_arg(action, "to").name
            )
        elif isinstance(action, exp.AlterRename):
            catalog.rename_table(table, action.this.name)
            table = action.this.name
        else:
            raise NotSupportedError(
                f"unsupported ALTER TABLE action: {action.sql(dialect='sqlite')}"
            )
    return StatementResult(rowcount=-1)
