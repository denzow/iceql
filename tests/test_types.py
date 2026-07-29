import pytest

from iceql.errors import DataError
from iceql.types import decode_null, encode_null, get_type


class TestNullCodec:
    def test_none_roundtrip(self):
        assert encode_null(None) == r"\N"
        assert decode_null(r"\N") is None

    def test_empty_string_is_not_null(self):
        assert encode_null("") == ""
        assert decode_null("") == ""

    @pytest.mark.parametrize(
        "raw",
        [r"\N", r"\\N", r"\\\N", "plain", "", "a,b", 'say "hi"', "line1\nline2"],
    )
    def test_roundtrip(self, raw):
        assert decode_null(encode_null(raw)) == raw

    def test_literal_backslash_n_is_escaped(self):
        assert encode_null(r"\N") == r"\\N"
        assert decode_null(r"\\N") == r"\N"

    def test_lookalikes_pass_through(self):
        # \N に似ているが規約対象外の文字列はそのまま
        for s in [r"\n", "N", r"\NN", r"x\N"]:
            assert encode_null(s) == s
            assert decode_null(s) == s


class TestInteger:
    def test_roundtrip(self):
        t = get_type("integer")
        assert t.decode("42") == 42
        assert t.encode(-7) == "-7"

    @pytest.mark.parametrize("bad", ["1.5", "1.0", "abc", "", "0x10"])
    def test_decode_rejects(self, bad):
        with pytest.raises(DataError):
            get_type("integer").decode(bad)

    def test_encode_rejects_bool_and_float(self):
        with pytest.raises(DataError):
            get_type("integer").encode(True)
        with pytest.raises(DataError):
            get_type("integer").encode(1.5)


class TestReal:
    def test_roundtrip(self):
        t = get_type("real")
        assert t.decode("1.5") == 1.5
        assert t.encode(1.5) == "1.5"
        assert t.encode(1) == "1.0"

    def test_shortest_repr(self):
        assert get_type("real").encode(0.1) == "0.1"


class TestBoolean:
    def test_roundtrip(self):
        t = get_type("boolean")
        assert t.decode("true") is True
        assert t.decode("false") is False
        assert t.encode(True) == "true"
        assert t.encode(0) == "false"

    @pytest.mark.parametrize("bad", ["True", "FALSE", "1", "yes", ""])
    def test_decode_rejects(self, bad):
        with pytest.raises(DataError):
            get_type("boolean").decode(bad)


class TestDateDatetime:
    def test_date(self):
        t = get_type("date")
        assert t.decode("2024-06-01") == "2024-06-01"
        assert t.encode("2024-06-01") == "2024-06-01"
        with pytest.raises(DataError):
            t.decode("2024/06/01")

    def test_datetime(self):
        t = get_type("datetime")
        assert t.decode("2024-06-01 12:34:56") == "2024-06-01 12:34:56"
        with pytest.raises(DataError):
            t.decode("not a datetime")


class TestNullHandling:
    def test_decode_none_is_none(self):
        for name in ["integer", "real", "boolean", "text", "date", "datetime"]:
            assert get_type(name).decode(None) is None
            assert get_type(name).encode(None) is None


def test_unknown_type():
    with pytest.raises(DataError):
        get_type("varchar")
