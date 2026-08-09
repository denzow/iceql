"""sqlglot の AST を触る共通部分のテスト。"""

import pytest

from iceql.engine import node_arg, parse_statement
from iceql.errors import InternalError


class TestNodeArg:
    def test_returns_the_argument(self):
        ast = parse_statement("UPDATE t SET a = 1 WHERE id = 2")
        assert node_arg(ast, "where") is not None

    def test_returns_none_when_absent(self):
        ast = parse_statement("UPDATE t SET a = 1")
        assert node_arg(ast, "where") is None

    def test_unknown_key_raises(self):
        # 綴り違いを None として黙って流すと、句を見落として実行が続く
        ast = parse_statement("UPDATE t SET a = 1 FROM s")
        with pytest.raises(InternalError, match="whre"):
            node_arg(ast, "whre")

    def test_falls_back_to_the_other_spelling(self):
        # FROM 句のキーは sqlglot のバージョンで from / from_ のどちらかになる
        ast = parse_statement("UPDATE t SET a = 1 FROM s")
        assert node_arg(ast, "from", "from_") is not None

    @pytest.mark.parametrize(
        ("sql", "keys"),
        [
            ("UPDATE t SET a = 1 FROM s", ("from", "from_")),
            ("UPDATE t SET a = 1 WHERE id = 2", ("where",)),
            ("DELETE FROM t WHERE id = 2", ("where",)),
            ("SELECT a FROM t ORDER BY a LIMIT 1 OFFSET 2", ("order",)),
            ("SELECT a FROM t ORDER BY a LIMIT 1 OFFSET 2", ("limit",)),
            ("SELECT a FROM t ORDER BY a LIMIT 1 OFFSET 2", ("offset",)),
            ("SELECT DISTINCT a FROM t", ("distinct",)),
        ],
    )
    def test_key_is_present_in_this_sqlglot_version(self, sql, keys):
        """engine が依存する sqlglot のキー名が変わったら落とす。"""
        assert node_arg(parse_statement(sql), *keys) is not None
