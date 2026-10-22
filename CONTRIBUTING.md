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

When changing xim onboarding, keep README.md and docs/usage.md aligned with the
xim-pkgindex contributor guide, copyable package template and xpkg-creater skill.
Recommend upstream GitHub repositories with at least 50 stars as contributor
guidance; do not turn this recommendation into a parser or CI requirement.
Document source support, required/optional targets and validation steps accurately.

Keep package declarations small. Do not add implicit mirror publication,
arbitrary package hooks or automatic merges.
