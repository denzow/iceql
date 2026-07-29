"""hypothesis による write→read round-trip と正規形の冪等性テスト。"""

import math

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from iceql.schema import Column, TableSchema
from iceql.storage import encode_rows, read_rows

TYPE_VALUE_STRATEGIES = {
    "integer": st.integers(),
    "real": st.floats(allow_nan=False, allow_infinity=False),
    "boolean": st.booleans(),
    # NUL は text 型が拒否する仕様(types._encode_text 参照)なので生成しない
    "text": st.text(alphabet=st.characters(codec="utf-8", exclude_characters="\x00")),
    "date": st.dates().map(lambda d: d.isoformat()),
    "datetime": st.datetimes().map(lambda d: d.isoformat(sep=" ")),
}

column_names = st.from_regex(r"[A-Za-z_][A-Za-z0-9_]{0,10}", fullmatch=True)


@st.composite
def schema_and_rows(draw):
    n_cols = draw(st.integers(min_value=1, max_value=5))
    names = draw(
        st.lists(column_names, min_size=n_cols, max_size=n_cols, unique=True)
    )
    columns = [
        Column(name=name, type=draw(st.sampled_from(sorted(TYPE_VALUE_STRATEGIES))))
        for name in names
    ]
    schema = TableSchema(table="t", columns=columns)
    row_strategy = st.fixed_dictionaries(
        {c.name: st.none() | TYPE_VALUE_STRATEGIES[c.type] for c in columns}
    )
    rows = draw(st.lists(row_strategy, max_size=20))
    return schema, rows


def rows_equal(a, b):
    if len(a) != len(b):
        return False
    for ra, rb in zip(a, b, strict=False):
        for k in ra:
            va, vb = ra[k], rb[k]
            if isinstance(va, float) and isinstance(vb, float):
                if not (math.isclose(va, vb) or (va == 0.0 and vb == 0.0)):
                    return False
            elif va != vb:
                return False
    return True


PROPERTY_SETTINGS = settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


@pytest.fixture
def csv_path(tmp_path):
    # hypothesis は example ごとに同じ fixture を再利用するが、毎回上書きするので問題ない
    return tmp_path / "t.csv"


@given(schema_and_rows())
@PROPERTY_SETTINGS
def test_write_read_roundtrip(csv_path, data):
    schema, rows = data
    csv_path.write_text(encode_rows(rows, schema), encoding="utf-8", newline="")
    assert rows_equal(read_rows(csv_path, schema), rows)


@given(schema_and_rows())
@PROPERTY_SETTINGS
def test_canonical_form_is_idempotent(csv_path, data):
    """一度書き出したものを読み直して再度書き出すとバイト同一(diff 友好性)。"""
    schema, rows = data
    first = encode_rows(rows, schema)
    csv_path.write_text(first, encoding="utf-8", newline="")
    assert encode_rows(read_rows(csv_path, schema), schema) == first
