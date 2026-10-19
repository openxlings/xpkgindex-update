# xpkgindex-update

Discover releases. Verify resources. Open update PRs.

A Python CLI and GitHub Action for [mcpp-index](https://github.com/mcpplibs/mcpp-index).
New versions are appended; published versions and mirrors stay intact.
Identical cross-platform resources are declared once.

## Declare

```lua
extensions = {
    autoupdate = {
        tag = "v${version}",
        url = "https://github.com/acme/foo/archive/refs/tags/${tag}.tar.gz",
    },
},
```

## Automate

After checking out your index:

```yaml
- uses: openxlings/xpkgindex-update@main
  with:
    mode: check
    token: ${{ github.token }}
```

Use `mode: propose` with an App/PAT token to open update PRs.
Build verification and merging remain with the index maintainers.

[Usage & CLI](docs/usage.md) · [Contributing](CONTRIBUTING.md) · [MIT License](LICENSE)
