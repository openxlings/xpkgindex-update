# Contributing

Use a branch and a pull request. Run these checks before pushing:

```sh
python -m pip install -e '.[test]'
ruff check .
pytest -q
python -m build
```

Changes must preserve every published version block, including its mirrors,
hashes, comments and ordering. Cover parser and update behavior with fixtures;
use the mcpp-index draft integration for real upstream and build validation.

Keep package declarations small. Do not add implicit mirror publication,
arbitrary package hooks or automatic merges.
