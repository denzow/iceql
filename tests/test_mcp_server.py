import pytest

from iceql.errors import ProgrammingError
from iceql.mcp_server import (
    describe_table_tool,
    execute_tool,
    list_tables_tool,
    query_tool,
)


class TestToolFunctions:
    def test_query(self, conn):
        result = query_tool(conn, "SELECT id, dept FROM depts ORDER BY id")
        assert result == {"columns": ["id", "dept"], "rows": [[1, "eng"], [2, "sales"]]}

    def test_query_rejects_dml(self, conn):
        with pytest.raises(ProgrammingError, match="SELECT statements only"):
            query_tool(conn, "DELETE FROM depts")

    def test_execute(self, conn):
        result = execute_tool(conn, "INSERT INTO depts (id, dept) VALUES (3, 'hr')")
        assert result == {"rowcount": 1}

    def test_list_tables(self, conn):
        assert list_tables_tool(conn) == ["depts", "users"]

    def test_describe_table(self, conn):
        text = describe_table_tool(conn, "depts")
        assert "table: depts" in text
        with pytest.raises(ProgrammingError):
            describe_table_tool(conn, "missing")


class TestServer:
    def test_create_server_registers_tools(self, tmp_path):
        pytest.importorskip("mcp")
        from iceql.mcp_server import create_server

        server = create_server(tmp_path / "db")
        import anyio

        tools = anyio.run(server.list_tools)
        names = {t.name for t in tools}
        assert names == {"query", "execute", "list_tables", "describe_table"}

    def test_read_only_hides_execute(self, tmp_path):
        pytest.importorskip("mcp")
        from iceql.mcp_server import create_server

        server = create_server(tmp_path / "db", read_only=True)
        import anyio

        tools = anyio.run(server.list_tools)
        assert "execute" not in {t.name for t in tools}
