# Usage

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

`mode: propose` creates PRs and requires a token with repository write access.
Checkout should use `persist-credentials: false`. Plans are uploaded as Actions
artifacts. An open PR already editing the package also blocks duplicate proposals, including
manual package PRs. Closed candidate versions are not reopened.
Development end-to-end tests use `propose --draft --base <test-branch>`.

The mcpp adapter remains append-only. The xim adapter uses explicit latest policies
and architecture-aware format-2 plans, described below.

## CI authentication

Schedule the caller workflow with `schedule` and run proposals only on its
repository's default branch. Grant `contents: write` and `pull-requests: write`
only to the proposal job. Keep resource builds in separate read-only jobs.

A GitHub App installation token can create bot PRs that trigger ordinary PR CI.
With the built-in `GITHUB_TOKEN`, organization and repository settings must allow
Actions to create PRs, and the caller must explicitly dispatch CI workflows
(`actions: write`) to run unattended. The PR author is `github-actions[bot]`;
commit author metadata alone does not determine the PR author.

The [mcpp-index integration](https://github.com/mcpplibs/mcpp-index/blob/main/.github/workflows/xpkgindex-update.yml)
uses daily scanning, a fixed artifact plan, bot proposals and explicit CI dispatch.
It verifies the PR identity and head, avoids duplicate PRs and resumes missing
dispatches. CLI proposals can consume a previously checked plan; the composite
`mode: propose` performs its own check first. Neither path merges PRs.

## Shared resources

When every platform uses the same URL and SHA256, new resources are written
once before the package declaration:

```lua
local versions = {
    ["1.2.0"] = { url = "https://example.org/foo-1.2.0.tar.gz", sha256 = "…" },
}

package = {
    -- identity, update declaration and build settings
    xpm = {
        linux = { ["1.2.0"] = versions["1.2.0"] },
        macosx = { ["1.2.0"] = versions["1.2.0"] },
        windows = { ["1.2.0"] = versions["1.2.0"] },
    },
}
```

Future releases append to the same resource table. Existing inline versions
are left untouched. Different platform resources remain separate. Literal
local tables and their literal-key references are read statically; Lua is never
executed. Shared definitions are covered by the same preservation checks as
inline entries. Mutation, rebinding and dynamic references are rejected.

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

## xim-pkgindex adapter (plan format 2)

The xim adapter is selected with `adapter: xim-pkgindex`. It uses the same CLI,
PR recovery and static Lua parser; it does not read legacy update flags or templates.
Every xim declaration requires an explicit discovery repository and latest policy:

```lua
extensions = { autoupdate = {
    upstream = "acme/foo", tag = "v${version}", latest = "advance",
    url = {
        linux = {
            x86_64 = "https://github.com/acme/foo/releases/download/${tag}/foo-amd64.tar.gz",
            aarch64 = "https://github.com/acme/foo/releases/download/${tag}/foo-arm64.tar.gz",
        },
        windows = "https://github.com/acme/foo/releases/download/${tag}/foo.exe",
    },
} },
```

`latest = "keep"` appends without moving pointers. Current platform latest refs
must agree. Historical platform version sets may differ; new candidates must
cover every declared platform and architecture. Partial existing candidates are
conflicts, never repaired implicitly. Architecture additions/removals require an
explicit packaging change. Multi-architecture output uses literal per-arch
resource maps; historical entries and runtime hooks are preserved.

A URL leaf can also be a `{ GLOBAL = "https://...", CN = "https://..." }` pair.
Both mirrors must already exist and match. Optional `verify = "sha256-sidecar"`
requires each resource's `.sha256` sidecar to name the artifact and match its
independently computed digest. Resource production is separate from updating.

The fixed plan includes expected pointers, candidate, tag commit, descriptor digest
and every platform/architecture/region resource. `apply` and `propose` verify this
plan before writing. Format 1 and format 2 plans cannot be interchanged.

Central positive integer limits are `request_budget`, `resource_bytes`,
`download_bytes`, `max_packages`, plus the existing `max_prs`. Retry API requests
count toward the budget. Use `check --state-file <path>` to preserve a scan cursor
and waiting-resource timestamps; this cursor rotates through the inventory and
never throttles errors for multiple days. Resources unavailable for seven days
become errors. Archive recognition includes gzip, ZIP, XZ and bzip2; raw binaries
are also supported. A failed package does not authorize changes to another package.

Scheduled callers should run `check --output plan.json` once, upload the plan,
and run `propose --plan plan.json` in a separate write-permission job. Ready
records can be proposed even when another record reports an operational error;
the final workflow gate must still fail for errors and policy conflicts.

The xim-only `provider = "go"` option requires `upstream = "golang/go"` and
`tag = "go${version}"`. It discovers stable releases from the official
`https://go.dev/dl/?mode=json` feed and verifies downloaded resources against
its authoritative SHA256 values, again during apply. GitHub is used only to
bind the selected release tag to a commit. It never substitutes GitHub tag
heuristics for the official Go release feed.

URL templates can use `${version:compact}` for artifact names such as 7zip's
`7z2602`; tag templates also support `${version:underscore}`. Historical SemVer
prereleases are ordered below their stable version, while hexadecimal commit
snapshots remain pinned and never follow a release stream.

`runtime-diff --base <ref>` reports which descriptors change installation inputs.
It excludes only CI declarations, compares runtime hooks and resource metadata,
and is intended to select install tests without skipping new versions or hashes.

For xim candidate PRs, `xpkgindex-update verify --base <base-sha> --output
candidate-resources.json` validates only the added versions in the diff. It
checks all platform/architecture/region URLs against the declaration, downloads
and compares every resource SHA256 and configured sidecar, and checks the Go
provider when selected. It does not discover future releases. Use this read-only
check before native installation jobs; `guard` alone protects history but does
not download candidate resources.
