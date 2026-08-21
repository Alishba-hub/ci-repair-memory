# Issue

The slug helper crashes when the input value is `None`.

Expected behavior:

- `None` should return an empty string.
- Normal strings should still be trimmed, lowercased, and hyphenated.
