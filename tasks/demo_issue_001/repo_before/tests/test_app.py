from app import normalize_title


def test_normalize_title_trims_and_lowercases():
    assert normalize_title("  Hello World  ") == "hello world"


def test_normalize_title_blank_input():
    assert normalize_title("") == ""
    assert normalize_title("   ") == ""
