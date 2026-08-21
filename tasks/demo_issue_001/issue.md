# Issue

The title normalization helper fails on blank input.

Expected behavior:

- Empty strings should return an empty string.
- Whitespace-only strings should return an empty string.
- Normal titles should still be lowercased and trimmed.
