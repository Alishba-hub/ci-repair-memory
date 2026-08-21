from slug import slugify


def test_slugify_regular_value():
    assert slugify("Hello World") == "hello-world"


def test_slugify_none_value():
    assert slugify(None) == ""
