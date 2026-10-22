"""Per-package PRs in isolated worktrees; never push the default branch."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from .adapters import get_adapter
from .lua_edit import Invalid


def run(*args: str, cwd: Path, env=None) -> str:
    result = subprocess.run(args, cwd=cwd, env=env, text=True, encoding="utf-8", capture_output=True)
    if result.returncode:
        raise Invalid(f"{args[0]} {args[1]} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def propose(root, plan, github, *, base=None, draft=False, limit=3) -> list[dict]:
    if not github.token:
        raise Invalid("propose requires GH_TOKEN/GITHUB_TOKEN or --token")
    if run("git", "status", "--porcelain", "--untracked-files=no", cwd=root):
        raise Invalid("propose requires a clean tracked worktree")
    repo = json.loads(run("gh", "repo", "view", "--json", "nameWithOwner,defaultBranchRef", cwd=root))
    base = base or repo["defaultBranchRef"]["name"]
    base_sha = run("git", "rev-parse", base, cwd=root)
    if base_sha != plan["base_commit"]:
        raise Invalid("plan base commit differs from PR base")
    env = dict(os.environ, GH_TOKEN=github.token)
    outcomes = []
    for record in plan["packages"]:
        if record["status"] != "update-ready":
            continue
        identity = record["id"]
        slug = re.sub(r"[^a-zA-Z0-9._-]", "-", identity)
        branch = f"auto/bump-{slug}-{record['candidate']}"
        prs = json.loads(run("gh", "pr", "list", "--state", "open", "--limit", "1000",
                             "--json", "headRefName,state,url,files", cwd=root, env=env))
        same_package = [p for p in prs if p["headRefName"].startswith(f"auto/bump-{slug}-")
                        or any(f["path"] == record["path"] for f in p.get("files", []))]
        if same_package:
            outcomes.append({"id": identity, "status": "pending", "url": same_package[0]["url"]})
            continue
        history = json.loads(run("gh", "pr", "list", "--state", "all", "--head", branch,
                                 "--json", "headRefName,state,url", cwd=root, env=env))
        if history:
            outcomes.append({"id": identity, "status": "dismissed"})
            continue
        if sum(o["status"] == "created" for o in outcomes) >= limit:
            break
        with tempfile.TemporaryDirectory(prefix="xpkgindex-update-") as directory:
            work = (Path(directory) / "work").resolve()
            run("git", "worktree", "add", "--detach", str(work), base_sha, cwd=root)
            try:
                changed = get_adapter(plan.get("adapter", "mcpp-index")).append(work, record, github)
                if not changed:
                    continue
                remote = run("git", "ls-remote", "--heads", "origin", branch, cwd=work, env=env)
                if remote:
                    run("git", "fetch", "origin", f"refs/heads/{branch}", cwd=work, env=env)
                    expected = (work / record["path"]).read_text(encoding="utf-8").rstrip()
                    actual = run("git", "show", f"FETCH_HEAD:{record['path']}", cwd=work)
                    changed_paths = run("git", "diff", "--name-only", base_sha, "FETCH_HEAD", cwd=work).splitlines()
                    if actual != expected or changed_paths != [record["path"]]:
                        raise Invalid("existing update branch differs; refusing to overwrite")
                    # A previous push succeeded: reuse it and retry PR creation only.
                else:
                    run("git", "add", "--", record["path"], cwd=work)
                    run("git", "-c", "user.name=xpkgindex-update[bot]", "-c",
                        "user.email=xpkgindex-update@users.noreply.github.com", "commit", "-m",
                        f"chore: append {identity} {record['candidate']}", cwd=work)
                    # gh's credential helper uses GH_TOKEN; no token appears in URLs or logs.
                    run("git", "-c", "credential.helper=", "-c",
                        "credential.helper=!gh auth git-credential", "push", "origin",
                        f"HEAD:refs/heads/{branch}", cwd=work, env=env)
                body = Path(directory) / "body.md"
                body.write_text(
                    f"Append `{identity}@{record['candidate']}`. Existing versions are unchanged.\n\n"
                    f"Upstream: `{record['repo']}@{record['tag']}` (`{record['upstream_commit']}`).\n\n"
                    "Resources and computed SHA256:\n\n```json\n" +
                    json.dumps(record["assets"], indent=2) + "\n```\n\n"
                    "The index CI must build and resolve the candidate version before merge.\n")
                args = ["gh", "pr", "create", "--base", base, "--head", branch, "--title",
                        f"chore: append {identity} {record['candidate']}", "--body-file", str(body)]
                if draft:
                    args.append("--draft")
                url = run(*args, cwd=work, env=env)
                outcomes.append({"id": identity, "status": "created", "url": url})
            finally:
                run("git", "worktree", "remove", "--force", str(work), cwd=root)
    return outcomes
