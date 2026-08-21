# Issue

The CI workflow does not install the project in editable mode, so the test job cannot import the package reliably.

Expected behavior:

- Install the project before running tests.
- Keep the workflow minimal and readable.
