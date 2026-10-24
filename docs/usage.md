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
artifacts. Manual package PRs block automatic proposals. Bot-owned PRs on fixed
`auto/bump-<namespace>-<package>` branches are refreshed in place when a newer
candidate is verified. Explicitly closed candidates are not reopened.
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

## New xim packages

Copy the xim declaration below into the `package` table, merging its
`extensions.autoupdate` field with any existing extensions. Replace the example owner/project and asset paths with verified values. We recommend
upstream GitHub repositories with **at least 50 stars**; this is guidance, not
an eligibility check in the updater. Popularity does not establish artifact integrity.

Use a string when every platform consumes the same artifact; otherwise declare
an explicit matrix matching every supported platform/architecture. `${tag}` keeps
the original tag and `${version}` is the extracted version. Do not assume runtime
`xpm.source` placeholders such as `${os}`, `${arch_alias}` or `${ext}` expand here.
Start with GLOBAL alone if no verified mirror exists. Add CN only after checking
actual paths, complete assets and matching SHA256s. Every target defaults to
required; an explicitly optional target needs `required = false`. Keep at least one
required target. Use `latest = "keep"` for a pinned/LTS default.

In xim-pkgindex, install the workflow's pinned updater, run `inspect` and
`check --package xim:<name> --output plan.json` and review the plan. The recipe is
the configuration source of truth; index CI generates `coverage.json` as an Actions
artifact, without a checked-in audit snapshot. Keep unsupported sources manual,
with a reason in the recipe comments or PR description.
The index schedules scans every three days; cadence and mirror admission are index
policy, not global CLI defaults. Candidate PRs still need resource checks and native
installation, functionality and removal checks before review; the tool does not merge.
See the [index contributor guide](https://github.com/openxlings/xim-pkgindex/blob/main/docs/10-adding-a-package.md)
and [update mechanism](https://github.com/openxlings/xim-pkgindex/blob/main/docs/13-ci-mirror-and-update.md).

## xim-pkgindex adapter (plan format 2)

The xim adapter is selected with `adapter: xim-pkgindex`. It uses the same CLI,
PR recovery and static Lua parser; it does not read legacy update flags or templates.
The public model is a source URL plus resource URL templates:

```lua
extensions = { autoupdate = {
    source = "https://github.com/acme/foo/releases",
    resources = {
        GLOBAL = "https://github.com/acme/foo/releases/download/${tag}/foo-${version}.zip",
    },
} },
```

`latest = "advance"` and each target's `required = true` are defaults. Ordinary
numeric tags and `v`-prefixed tags need no tag declaration. An explicit `tag`
template supports project-specific spellings. Source URLs currently recognize
GitHub `/releases`, `/tags`, and public HTTPS Sparkle RSS appcasts ending in `.xml`.
Unknown sources fail explicitly. Resource templates support any public
HTTPS host independently of discovery.

For a vendor's official Sparkle feed, set `source = "https://vendor.example/appcast.xml"`
and use `${version}` in the same resource templates. No GitHub repository or `tag`
declaration is needed. The version is Sparkle's numeric `shortVersionString`, not
its internal build number. Channel-marked releases, prereleases and nested delta
enclosures are excluded; duplicate versions/builds/architectures fail explicitly.
Feeds are bounded to 1 MiB and fetched without GitHub credentials.

At least one required resource must match the feed's full enclosure URL and length.
Other required platforms are verified through their fixed versioned templates;
the newest version available across the complete matrix wins. A macOS release
whose Linux resource returns 404 does not advance either platform. Independent
per-platform version advancement is not implemented. Existing platform-specific
history and helper-based entries remain unchanged; new entries are literal.
For example, ChatGPT uses its official appcast plus Linux DEB/macOS ZIP templates.
This does not require inventing a GitHub mirror or star count for official vendors.

An appcast plan pins the selected version/build/enclosure metadata digest, not the
entire changing feed. Apply/propose rejects a removed or changed selected item,
while allowing newly published unrelated versions. Downloaded SHA256 is still
rechecked. Sparkle's signature is recorded in the metadata digest but **not
cryptographically verified** by this tool; native package validation must retain
vendor signature/identity checks (ChatGPT checks macOS codesign and bundle version).

Expand a target when it needs platform/architecture URLs or optional availability:

```lua
resources = {
    GLOBAL = { required = true, url = {
        linux = {
            x86_64 = "https://github.com/acme/foo/releases/download/${tag}/foo-amd64.tar.gz",
            aarch64 = "https://github.com/acme/foo/releases/download/${tag}/foo-arm64.tar.gz",
        },
        windows = "https://github.com/acme/foo/releases/download/${tag}/foo.exe",
    } },
    CN = { required = false, url = {
        linux = {
            x86_64 = "https://verified-mirror.example/foo/${tag}/foo-amd64.tar.gz",
            aarch64 = "https://verified-mirror.example/foo/${tag}/foo-arm64.tar.gz",
        },
        windows = "https://verified-mirror.example/foo/${tag}/foo.exe",
    } },
}
```

Each target covers the entire declared platform/architecture matrix. At least one
target must be required. Check candidates in descending order, above every
existing version. A missing required asset (404/410, or a CN endpoint redirecting
to GitHub) tries the next candidate: selection is the true intersection, not the
minimum of each target's newest version. Network failures, corrupt artifacts,
required hash conflicts and exhausted budgets never authorize fallback to a
supposedly older supported version. Optional target failures omit the entire
target, with a diagnostic. Only downloaded, matching resources enter the plan.

`latest = "keep"` appends without moving pointers, useful for LTS/pinned aliases.
Current platform latest refs must agree. Historical platform version sets may
differ; new candidates cover every declared platform and architecture. Existing
partial candidates are conflicts. Literal multi-architecture output leaves every
historical version, manual entry, comment and runtime hook intact. An opaque
historical helper is never executed; an explicit matrix matching the package's
complete declared architectures is required for such a baseline.

Optional `verify = "sha256-sidecar"` requires each artifact's `.sha256` sidecar to
name the artifact and match its independently computed digest. Resource production
is separate: consume existing xlings-res mirrors; never replace repackaged assets
with similarly named upstream archives or publish automatically.

The fixed plan includes expected pointers, candidate, tag commit (or appcast release digest), descriptor digest
and every platform/architecture/region resource. `apply` and `propose` verify this
plan before writing. Format 1 and format 2 plans cannot be interchanged.

Central positive integer limits are `request_budget`, `resource_bytes`,
`download_bytes`, `max_packages`, `package_requests`, `package_download_bytes`,
plus `max_prs`. Retry API requests
count toward the budget. Use `check --state-file <path>` to preserve a scan cursor
and waiting-resource timestamps; this cursor rotates through the inventory and
never throttles errors for multiple days. Resources unavailable for seven days
become errors. Archive recognition includes gzip, ZIP, XZ and bzip2; raw binaries
are also supported. A failed package does not authorize changes to another package.

Scheduled callers should run `check --output plan.json` once, upload the plan,
and run `propose --plan plan.json` in a separate write-permission job. Ready
records can be proposed even when another record reports an operational error;
the final workflow gate must still fail for errors and policy conflicts.

URL templates can use `${version:compact}` for artifact names such as 7zip's
`7z2602`; tag templates also support `${version:underscore}`. Historical SemVer
prereleases are ordered below their stable version, while hexadecimal commit
snapshots remain pinned and never follow a release stream.

`runtime-diff --base <ref>` reports which descriptors change installation inputs.
It excludes only CI declarations, compares runtime hooks and resource metadata,
and is intended to select install tests without skipping new versions or hashes.

Initial package descriptors use the index's normal package CI; updater verification
applies to added versions in existing descriptors.

For xim candidate PRs, `xpkgindex-update verify --base <base-sha> --output
candidate-resources.json` validates only the added versions in the diff. It
checks all platform/architecture/region URLs against the declaration, downloads
and compares every resource SHA256 and configured sidecar. It does not discover future releases. Use this read-only
check before native installation jobs; `guard` alone protects history but does
not download candidate resources.

### Scan batches, fairness and reusable PRs

`max_packages` is the scan **batch size**, not the number of upgrades or PRs.
`check --max-batches 20 --state-file state.json --output plan.json` processes up to
20 sequential batches in one run (default: one). Each eligible package is visited
at most once. All batches share one request/download budget and one fixed plan.
The `scan` report identifies batch, request and download exhaustion. Global
exhaustion keeps the unfinished package for the next run; a package-local quota
(`package_requests`, default 50; `package_download_bytes`, default 8 GiB) instead
marks that package `package-deferred`, advances the cursor and lets others run.
The cursor and candidate queue are checkpointed after each completed batch.

Verified candidates enter a persistent FIFO in the same state file. A newer
candidate replaces its record without losing its place. Unvisited candidates
remain available in the plan's `candidates` list; descriptor changes invalidate
cached records. `propose --plan plan.json --state-file state.json` rechecks source
identity and resource bytes before writing and attempts at most `max_prs`
candidates per invocation (20 in the index integration). Surplus candidates stay
queued. Failures move to the back with 1-hour to 24-hour exponential backoff.
An existing PR takes ownership of CI/review; later scans can enqueue its next
candidate. All state-file writes are atomic; callers must persist the post-proposal
state, not just the scan checkpoint. Corrupt state is rejected rather than reset.

Branches are fixed per package, for example `auto/bump-xim-nvim`. Verified newer
candidates refresh the same PR, replacing only unpublished candidate changes.
Commits include the previous update head and current production base as parents;
pushes are ordinary fast-forward pushes, never forced. Provenance and exact tree
checks reject manual edits, unrelated files and concurrent changes. The PR title
and resource evidence are updated too; CI must validate the new head. Bot-owned
legacy version branches migrate only after their replacement PR is created.
Manual PRs are never overwritten. A moved production base requires a fresh plan.
Updates are never merged automatically.
