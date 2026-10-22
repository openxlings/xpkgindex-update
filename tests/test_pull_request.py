import json
import subprocess

import pytest
from test_update import FakeGitHub, descriptor

from xpkgindex_update import pull_request
from xpkgindex_update.lua_edit import Invalid
from xpkgindex_update.manifest import check_package, package_info


def test_push_then_failed_pr_is_recovered_without_overwriting(tmp_path, monkeypatch):
    root = tmp_path / "index"
    root.mkdir()
    path = root / "pkgs/f/foo.lua"
    path.parent.mkdir(parents=True)
    path.write_text(descriptor())

    def git(*args, cwd=root):
        return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()

    git("init", "-b", "main")
    git("config", "core.autocrlf", "false")
    git("add", ".")
    git("-c", "user.name=ci", "-c", "user.email=ci@example.org", "commit", "-m", "fixture")
    origin = tmp_path / "origin.git"
    git("init", "--bare", str(origin))
    git("remote", "add", "origin", str(origin))
    git("push", "origin", "main")
    client = FakeGitHub()
    client.token = "fixture-token-never-sent"
    record = check_package(package_info(path, root), client)
    plan = {"base_commit": git("rev-parse", "HEAD"), "packages": [record]}
    real_run = pull_request.run
    create_attempts = []

    def run(*args, cwd, env=None):
        if args[:3] == ("gh", "repo", "view"):
            return json.dumps({"nameWithOwner": "acme/index", "defaultBranchRef": {"name": "main"}})
        if args[:3] == ("gh", "pr", "list"):
            # An earlier closed validation PR must not dismiss a production proposal.
            if "all" in args and "--base" not in args:
                return json.dumps([{"state": "CLOSED", "url": "https://github.com/acme/index/pull/old-validation"}])
            return "[]"
        if args[:3] == ("gh", "pr", "create"):
            create_attempts.append(args)
            assert "--draft" in args
            if len(create_attempts) == 1:
                raise Invalid("simulated PR API outage after successful push")
            return "https://github.com/acme/index/pull/1"
        return real_run(*args, cwd=cwd, env=env)

    monkeypatch.setattr(pull_request, "run", run)
    before = path.read_bytes()
    with pytest.raises(Invalid, match="simulated PR API outage"):
        pull_request.propose(root, plan, client, draft=True)
    branch = "auto/bump-compat-foo-1.1.0"
    first = git("ls-remote", "origin", branch)
    assert first
    result = pull_request.propose(root, plan, client, draft=True)
    assert result[0]["status"] == "created"
    assert git("ls-remote", "origin", branch) == first
    assert path.read_bytes() == before
    assert git("rev-parse", "HEAD") == plan["base_commit"]


@pytest.mark.parametrize("branch,files", [
    ("auto/bump-compat-foo-1.1.0", []),
    ("feat/manual-foo-upgrade", [{"path": "pkgs/f/foo.lua"}]),
])
def test_existing_bot_or_manual_package_pr_prevents_duplicate(tmp_path, monkeypatch, branch, files):
    client = FakeGitHub()
    client.token = "fixture-token-never-sent"
    url = "https://github.com/acme/index/pull/9"

    def run(*args, cwd, env=None):
        if args[:2] == ("git", "status"):
            return ""
        if args[:3] == ("gh", "repo", "view"):
            return json.dumps({"defaultBranchRef": {"name": "main"}})
        if args[:2] == ("git", "rev-parse"):
            return "base"
        if args[:3] == ("gh", "pr", "list"):
            assert "headRefName,state,url,files" in args
            return json.dumps([{"headRefName": branch, "state": "OPEN", "url": url, "files": files}])
        pytest.fail(f"an existing package PR must prevent mutation: {args}")

    monkeypatch.setattr(pull_request, "run", run)
    plan = {"base_commit": "base", "packages": [{
        "status": "update-ready", "id": "compat:foo", "path": "pkgs/f/foo.lua", "candidate": "1.1.0",
    }]}
    assert pull_request.propose(tmp_path, plan, client) == [{"id": "compat:foo", "status": "pending", "url": url}]
