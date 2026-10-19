# xpkgindex-update

Append new package versions. Keep published versions untouched.

A Python CLI and GitHub Action for `mcpp-index` and `xim-pkgindex`.
No mirror uploads, package builds, or automatic merges.

## Declare

```lua
extensions = {
    autoupdate = {
        tag = "v${version}",
        url = "https://github.com/acme/foo/archive/refs/tags/${tag}.tar.gz",
    },
},
```

Only declared packages are scanned. URLs may be shared, mapped by platform,
or existing `GLOBAL`/`CN` mirrors. Every versioned platform must be covered.
The download template identifies the GitHub repository; `package.repo` is the fallback.

Stable GitHub releases are preferred. Tag-only repositories fall back to
numeric tags. `${version:dash}` in a tag template maps `2-13-3` to `2.13.3`.
Numeric, date, `b12345` and single-letter release suffixes are ordered numerically.
Commit snapshots and independently numbered package/source versions are reported
as pinned; they never follow a branch or silently change version policy.

## Run

Python 3.11+ and Git are required; `propose` also uses GitHub CLI.

```sh
python -m pip install .
xpkgindex-update inspect
xpkgindex-update check --output plan.json
xpkgindex-update apply --plan plan.json --package compat:foo
xpkgindex-update propose --plan plan.json
```

`check` downloads and hashes candidate resources. `apply` revalidates the
fixed plan and appends version blocks atomically. Existing version entries,
including mirrors, hashes and comments, are never replaced. Index CI must
resolve and build the candidate before merge.

```yaml
# .github/xpkgindex-update.yml in the index repository
version: 1
adapter: mcpp-index
max_prs: 3
```

```yaml
# After checkout; pin a reviewed commit during development.
- uses: openxlings/xpkgindex-update@<commit>
  with:
    mode: check
    token: ${{ github.token }}
```

`mode: propose` creates PRs and requires an App/PAT token with repository write
access. Checkout should use `persist-credentials: false`. Plans are uploaded as
Actions artifacts. One open update PR per package; closed versions are not reopened.
Development end-to-end tests use `propose --draft --base <test-branch>`.

The first integration is mcpp-index. The xim adapter boundary is included,
but moving `latest.ref` is intentionally disabled pending an explicit policy.

## Develop

```sh
python -m pip install -e '.[test]'
ruff check .
pytest -q
python -m build
```

Source is in `src/xpkgindex_update`, adapter code in `adapters`, tests in `tests`.
CI tests Python 3.11/3.13 on Linux, macOS and Windows, plus the composite Action.

MIT.
