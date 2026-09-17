from http2opc.includes import OpenOPC


def test_type_check_list():
    tags, single, valid = OpenOPC.type_check(["a", "b"])
    assert tags == ["a", "b"]
    assert single is False
    assert valid is True


def test_type_check_single_string():
    tags, single, valid = OpenOPC.type_check("a")
    assert tags == ["a"]
    assert single is True
    assert valid is True


def test_type_check_none():
    tags, single, valid = OpenOPC.type_check(None)
    assert tags == []
    assert single is False
    assert valid is True


def test_type_check_invalid_type():
    tags, single, valid = OpenOPC.type_check(123)
    assert tags == [123]
    assert single is True
    assert valid is False


def test_wild2regex():
    assert OpenOPC.wild2regex("Root.*") == r"Root\..*"
    assert OpenOPC.wild2regex("A?B") == "A.B"
    assert OpenOPC.wild2regex("!X") == "^X"


def test_quality_str():
    assert OpenOPC.quality_str(0) == "Bad"
    assert OpenOPC.quality_str(0xC0) == "Good"


def test_tags2trace():
    assert OpenOPC.tags2trace(["ignored", "a", "b"]) == "a,b"


def test_exceptional_returns_alt_on_exception():
    def boom():
        raise ValueError("nope")

    wrapped = OpenOPC.exceptional(boom, alt_return="fallback")
    assert wrapped() == "fallback"


def test_exceptional_passes_through_return_value():
    wrapped = OpenOPC.exceptional(lambda: 42, alt_return="fallback")
    assert wrapped() == 42
