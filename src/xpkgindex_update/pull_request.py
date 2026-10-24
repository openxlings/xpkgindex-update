"""Reusable per-package PR branches; ordinary fast-forward pushes only."""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from .adapters import get_adapter
from .lua_edit import Invalid

BOT_NAME = "xpkgindex-update[bot]"
BOT_EMAIL = "xpkgindex-update@users.noreply.github.com"
BOT_USERS = {"github-actions[bot]", BOT_NAME}
TRAILER = "Xpkgindex-Update: "


def run(*args: str, cwd: Path, env=None) -> str:
    result = subprocess.run(args, cwd=cwd, env=env, text=True, encoding="utf-8", capture_output=True)
    if result.returncode:
        raise Invalid(f"{args[0]} {args[1]} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def branch_for(identity: str) -> str:
    return "auto/bump-" + re.sub(r"[^a-zA-Z0-9._-]", "-", identity)


def owned(pr: dict) -> bool:
    author = pr.get("author", {})
    login = author.get("login", "")
    # gh's GraphQL author shape differs from REST's `github-actions[bot]`.
    if author.get("is_bot") is True and login.startswith("app/"):
        login = login.removeprefix("app/") + "[bot]"
    return login in BOT_USERS and not pr.get("isCrossRepository", False)


def verify_generated(work, sha, base_sha, record, adapter, *, legacy=False):
    """Refuse to overwrite a human/concurrent edit, even on a bot-owned PR."""
    ancestor = run("git", "merge-base", sha, base_sha, cwd=work)
    if ancestor == sha:
        return  # A previously merged branch can carry the next update.
    email = run("git", "show", "-s", "--format=%ae", sha, cwd=work)
    text = run("git", "show", "-s", "--format=%B", sha, cwd=work)
    metadata = next((line[len(TRAILER):] for line in text.splitlines() if line.startswith(TRAILER)), None)
    if email != BOT_EMAIL or (metadata is None and not legacy):
        raise Invalid("update branch was modified outside the updater; refusing to overwrite")
    if metadata:
        try:
            marker = json.loads(metadata)
        except ValueError as exc:
            raise Invalid("invalid update branch provenance") from exc
        if (marker.get("id") != record["id"] or marker.get("path") != record["path"]
                or marker.get("tree") != run("git", "rev-parse", sha + "^{tree}", cwd=work)):
            raise Invalid("update branch provenance differs; refusing to overwrite")
        ancestor = marker["base"]
        run("git", "merge-base", "--is-ancestor", ancestor, base_sha, cwd=work)
    paths = run("git", "diff", "--name-only", ancestor, sha, cwd=work).splitlines()
    if paths != [record["path"]]:
        raise Invalid("update branch includes unrelated files; refusing to overwrite")
    before = run("git", "show", f"{ancestor}:{record['path']}", cwd=work)
    after = run("git", "show", f"{sha}:{record['path']}", cwd=work)
    adapter.guard(before, after)


def proposal_body(plan, record, replaced):
    marker = json.dumps({"id": record["id"], "candidate": record["candidate"]}, separators=(",", ":"))
    source = (f"Source: `{record['source']}`; release `{record['candidate']}` "
              f"(metadata SHA256 `{record['source_release_sha256']}`).\n\n"
              if record.get("discovery") == "appcast" else
              f"Upstream: `{record['repo']}@{record['tag']}` (`{record['upstream_commit']}`).\n\n")
    return (f"<!-- xpkgindex-update: {marker} -->\n"
            f"Append `{record['id']}@{record['candidate']}`. Existing published versions are unchanged.\n\n" +
            source + f"Plan base: `{plan['base_commit']}`; adapter `{plan.get('adapter', 'mcpp-index')}`.\n\n" +
            ("Replaces legacy proposals: " + ", ".join(p["url"] for p in replaced) + ".\n\n" if replaced else "") +
            "Resources and computed SHA256:\n\n```json\n" + json.dumps(record["assets"], indent=2) + "\n```\n\n"
            "The index CI must install, verify and remove this exact candidate before merge.\n")


def propose(root, plan, github, *, base=None, draft=False, limit=3, session=None) -> list[dict]:
    if not github.token:
        raise Invalid("propose requires GH_TOKEN/GITHUB_TOKEN or --token")
    if run("git", "status", "--porcelain", "--untracked-files=no", cwd=root):
        raise Invalid("propose requires a clean tracked worktree")
    env = dict(os.environ, GH_TOKEN=github.token)
    session = session if session is not None else {}
    if "repo" not in session:
        session["repo"] = json.loads(run("gh", "repo", "view", "--json", "nameWithOwner,defaultBranchRef", cwd=root, env=env))
    repo = session["repo"]
    base = base or repo["defaultBranchRef"]["name"]
    base_sha = run("git", "rev-parse", base, cwd=root)
    if base_sha != plan["base_commit"]:
        raise Invalid("plan base commit differs from PR base")
    outcomes = []
    adapter = get_adapter(plan.get("adapter", "mcpp-index"))
    for record in plan["packages"]:
        if record["status"] != "update-ready":
            continue
        identity, branch = record["id"], branch_for(record["id"])
        if "prs" not in session:
            session["prs"] = json.loads(run("gh", "pr", "list", "--state", "open", "--limit", "1000", "--base", base,
                                           "--json", "headRefName,state,url,files,author,isCrossRepository", cwd=root, env=env))
        prs = session["prs"]
        related = [p for p in prs if p["headRefName"] == branch
                   or (p["headRefName"].startswith(branch + "-") and not p.get("files"))
                   or any(f["path"] == record["path"] for f in p.get("files", []))]
        manual = next((p for p in related if not owned(p)), None)
        if manual:
            outcomes.append({"id": identity, "status": "pending", "url": manual["url"]})
            continue
        current = next((p for p in related if p["headRefName"] == branch), None)
        legacy = [p for p in related if p["headRefName"] != branch]
        if current is None:
            history = json.loads(run("gh", "pr", "list", "--state", "closed", "--head", branch, "--base", base,
                                     "--json", "state,body,url", cwd=root, env=env))
            marker = json.dumps({"id": identity, "candidate": record["candidate"]}, separators=(",", ":"))
            if any(p["state"] == "CLOSED" and f"<!-- xpkgindex-update: {marker} -->" in p.get("body", "")
                   for p in history):
                outcomes.append({"id": identity, "status": "dismissed"})
                continue
        if sum(o["status"] in ("created", "updated") for o in outcomes) >= limit:
            break
        upstream = run("git", "ls-remote", "--heads", "origin", base, cwd=root, env=env)
        if not upstream or upstream.split()[0] != base_sha:
            raise Invalid("PR base advanced since discovery; regenerate the plan")
        with tempfile.TemporaryDirectory(prefix="xpkgindex-update-") as directory:
            work = (Path(directory) / "work").resolve()
            run("git", "worktree", "add", "--detach", str(work), base_sha, cwd=root)
            try:
                for previous in legacy:
                    run("git", "fetch", "origin", f"refs/heads/{previous['headRefName']}", cwd=work, env=env)
                    verify_generated(work, run("git", "rev-parse", "FETCH_HEAD", cwd=work), base_sha,
                                     record, adapter, legacy=True)
                if not adapter.append(work, record, github):
                    outcomes.append({"id": identity, "status": "existing"})
                    continue
                run("git", "add", "--", record["path"], cwd=work)
                tree = run("git", "write-tree", cwd=work)
                remote = run("git", "ls-remote", "--heads", "origin", branch, cwd=work, env=env)
                old_sha = None
                if remote:
                    old_sha = remote.split()[0]
                    run("git", "fetch", "origin", f"refs/heads/{branch}", cwd=work, env=env)
                    if run("git", "rev-parse", "FETCH_HEAD", cwd=work) != old_sha:
                        raise Invalid("update branch moved during proposal; retry from a fresh plan")
                    verify_generated(work, old_sha, base_sha, record, adapter)
                changed = (not old_sha or tree != run("git", "rev-parse", old_sha + "^{tree}", cwd=work)
                           or run("git", "merge-base", base_sha, old_sha, cwd=work) != base_sha)
                if changed:
                    metadata = {"id": identity, "path": record["path"], "candidate": record["candidate"],
                                "base": base_sha, "tree": tree}
                    parents = ["-p", base_sha]
                    if old_sha and old_sha != base_sha:
                        parents = ["-p", old_sha, "-p", base_sha]
                    commit_env = {**env, "GIT_AUTHOR_NAME": BOT_NAME, "GIT_AUTHOR_EMAIL": BOT_EMAIL,
                                  "GIT_COMMITTER_NAME": BOT_NAME, "GIT_COMMITTER_EMAIL": BOT_EMAIL}
                    sha = run("git", "commit-tree", tree, *parents, "-m",
                              f"chore: append {identity} {record['candidate']}\n\n{TRAILER}" + json.dumps(metadata),
                              cwd=work, env=commit_env)
                    run("git", "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential",
                        "push", "origin", f"{sha}:refs/heads/{branch}", cwd=work, env=env)
                body = Path(directory) / "body.md"
                body.write_text(proposal_body(plan, record, legacy), encoding="utf-8")
                title = f"chore: append {identity} {record['candidate']}"
                if current:
                    number = current["url"].rstrip("/").rsplit("/", 1)[-1]
                    payload = Path(directory) / "pr.json"
                    payload.write_text(json.dumps({"title": title, "body": body.read_text(encoding="utf-8")}), encoding="utf-8")
                    run("gh", "api", "--method", "PATCH", f"repos/{repo['nameWithOwner']}/pulls/{number}",
                        "--input", str(payload), cwd=work, env=env)
                    url, status = current["url"], "updated" if changed else "pending"
                else:
                    args = ["gh", "pr", "create", "--base", base, "--head", branch, "--title", title,
                            "--body-file", str(body)]
                    if draft:
                        args.append("--draft")
                    url, status = run(*args, cwd=work, env=env), "created"
                for previous in legacy:
                    run("gh", "pr", "close", previous["url"], cwd=work, env=env)
                outcomes.append({"id": identity, "status": status, "url": url})
            finally:
                run("git", "worktree", "remove", "--force", str(work), cwd=root)
    return outcomes
