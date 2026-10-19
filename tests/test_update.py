from __future__ import annotations

import hashlib
import json
import subprocess

import pytest

from xpkgindex_update.adapters.mcpp_index import append
from xpkgindex_update.cli import main
from xpkgindex_update.github import WaitingAssets
from xpkgindex_update.lua_edit import Invalid, parse
from xpkgindex_update.manifest import (
    assert_preserved,
    check_package,
    package_info,
    tag_version,
    validate_config,
    version_key,
)


def descriptor(old="1.0.0", extra="", mirror=False, compact=False):
    url = ('{ GLOBAL = "https://github.com/acme/foo/archive/refs/tags/v1.0.0.tar.gz", '
           'CN = "https://gitcode.com/res/foo/releases/download/1.0.0/foo.tar.gz" }') if mirror else (
               '"https://github.com/acme/foo/archive/refs/tags/v1.0.0.tar.gz"')
    block = f'["{old}"] = {{ url = {url}, sha256 = "old-hash" }}'
    return f'''-- {{ package = {{ fake = true }}
package = {{
    name = "foo", namespace = "compat", repo = "https://github.com/acme/foo",
    extensions = {{ other = {{ ignored = true }}, autoupdate = {{
        tag = "v${{version}}",
        url = "https://github.com/acme/foo/archive/refs/tags/${{tag}}.tar.gz",
    }} }},
    xpm = {{
        linux = {{ {block} }},
        macosx = {{
            -- untouched old release
            {block},
        }},
        windows = {{ {block}, }},
    }},
    mcpp = {{ generated_files = {{ ["x"] = [==[ {{ }} -- "quoted" ]==] }} }},
}}
{extra}
'''


class FakeGitHub:
    def __init__(self, releases=None, tags=None):
        self.releases = releases if releases is not None else [{"tag_name": "v1.1.0"}]
        self.tags = tags or []
        self.sha = "a" * 40
        self.fail_url = None
        self.calls = []

    def listing(self, repo, kind):
        self.calls.append((repo, kind))
        return self.releases if kind == "releases" else self.tags

    def commit(self, repo, tag):
        return self.sha

    def artifact(self, url):
        if self.fail_url and self.fail_url in url:
            raise WaitingAssets(url)
        return {"url": url, "size": 12, "sha256": "b" * 64}


@pytest.fixture
def index(tmp_path):
    path = tmp_path / "pkgs/f/foo.lua"
    path.parent.mkdir(parents=True)
    path.write_text(descriptor())
    return tmp_path, path


def test_static_parser_never_executes_hooks(tmp_path):
    sentinel = tmp_path / "executed"
    pkg = parse(descriptor(extra=f'os.execute("touch {sentinel}")'))
    assert pkg.fields["name"].string() == "foo"
    assert not sentinel.exists()


@pytest.mark.parametrize("source", [
    'package = { name="a", name="b" }',
    'package = { x = [=[ unterminated }',
    'package = { [x] = "dynamic key" }',
])
def test_invalid_lua(source):
    with pytest.raises(Invalid):
        parse(source)


def test_comments_escaped_strings_and_long_comments():
    source = '--[==[ } package={ } ]==]\npackage={name="a\\\"b", x=[=[},]=], y="}",}'
    p = parse(source)
    assert p.fields["name"].string() == 'a"b'
    assert p.fields["x"].string() == "},"


@pytest.mark.parametrize("left,right", [("1.9", "1.10"), ("1.0", "2"),
    ("2026.9.30.2", "2026.10.1.1"), ("b10069.2", "b10070"), ("3.0.28a", "3.0.29")])
def test_numeric_order(left, right):
    assert version_key(left) < version_key(right)


def test_equivalent_versions():
    assert version_key("1.2.0") == version_key("1.2")


@pytest.mark.parametrize("template,tag,expected", [
    ("v${version}", "v1.2.3", "1.2.3"),
    ("boost-${version}", "boost-1.92.0", "1.92.0"),
    ("VER-${version:dash}", "VER-2-13-3", "2.13.3"),
    ("${version}", "b10069.2", "b10069.2"),
    ("v${version}", "v1.2-rc1", None),
    ("v${version}", "v1.2.3extra", None),
])
def test_tag_templates(template, tag, expected):
    assert tag_version(template, tag) == expected


def test_append_preserves_old_versions_and_build_fields(index):
    root, path = index
    before = path.read_text()
    client = FakeGitHub()
    record = check_package(package_info(path, root), client)
    assert record["status"] == "update-ready"
    assert append(root, record, client)
    after = path.read_text()
    assert_preserved(before, after)
    assert after.count('["1.1.0"]') == 3
    assert 'generated_files = { ["x"] = [==[ { } -- "quoted" ]==] }' in after
    assert not append(root, record, client)
    assert after == path.read_text()


def test_old_mirror_is_preserved_when_new_version_is_direct(index):
    root, path = index
    path.write_text(descriptor(mirror=True))
    before = path.read_text()
    client = FakeGitHub()
    record = check_package(package_info(path, root), client)
    assert append(root, record, client)
    assert_preserved(before, path.read_text())
    assert path.read_text().count("gitcode.com/") == 3


def test_partial_download_failure_does_not_write(index):
    root, path = index
    before = path.read_text()
    client = FakeGitHub()
    record = check_package(package_info(path, root), client)
    client.fail_url = "v1.1.0"
    with pytest.raises(WaitingAssets):
        append(root, record, client)
    assert before == path.read_text()


def test_apply_rejects_changed_base_and_moved_tag(index):
    root, path = index
    client = FakeGitHub()
    record = check_package(package_info(path, root), client)
    client.sha = "c" * 40
    with pytest.raises(Invalid, match="tag moved"):
        append(root, record, client)
    client.sha = record["upstream_commit"]
    path.write_text(path.read_text() + "\n-- edited\n")
    with pytest.raises(Invalid, match="descriptor changed"):
        append(root, record, client)


def test_path_traversal_rejected(index):
    root, path = index
    client = FakeGitHub()
    record = check_package(package_info(path, root), client)
    record["path"] = "../outside.lua"
    with pytest.raises(Invalid, match="inside pkgs"):
        append(root, record, client)


def test_plan_url_tampering_rejected(index):
    root, path = index
    client = FakeGitHub()
    record = check_package(package_info(path, root), client)
    record["assets"][0]["resources"]["GLOBAL"]["url"] = "https://evil.example/file"
    with pytest.raises(Invalid, match="differs from declaration"):
        append(root, record, client)


def test_release_filter_and_tag_fallback(index):
    root, path = index
    client = FakeGitHub(releases=[{"tag_name": "v99.0", "prerelease": True}],
                        tags=[{"name": "v99.0"}, {"name": "v1.2.0"}])
    record = check_package(package_info(path, root), client)
    assert record["candidate"] == "1.2.0"
    assert record["discovery"] == "tags"


def test_no_downgrade(index):
    root, path = index
    record = check_package(package_info(path, root), FakeGitHub(releases=[{"tag_name": "v0.9"}]))
    assert record["status"] == "up-to-date"


def test_historical_numbering_scheme_does_not_downgrade(index):
    root, path = index
    path.write_text(descriptor().replace("1.0.0", "3.0.28a"))
    client = FakeGitHub(releases=[
        {"tag_name": "v3.0.28a", "published_at": "2023-05-01T00:00:00Z"},
        {"tag_name": "v3.24", "published_at": "2020-06-24T00:00:00Z"},
    ])
    record = check_package(package_info(path, root), client)
    assert record["status"] == "up-to-date"


def test_ambiguous_tags_fail(index):
    root, path = index
    record = check_package(package_info(path, root), FakeGitHub(releases=[
        {"tag_name": "v1.1"}, {"tag_name": "v1.1.0"}]))
    assert record["status"] == "error"


def test_snapshot_is_visible_and_never_follows_branch(index):
    root, path = index
    path.write_text(descriptor(old="0.0.0-abc1234"))
    client = FakeGitHub()
    record = check_package(package_info(path, root), client)
    assert record["status"] == "pinned-snapshot"
    assert not client.calls


def test_platform_divergence_is_not_repaired(index):
    root, path = index
    text = path.read_text().replace('["1.0.0"]', '["0.9.0"]', 1)
    path.write_text(text)
    record = check_package(package_info(path, root), FakeGitHub())
    assert record["status"] == "error"
    assert path.read_text() == text


def test_missing_platform_template_is_invalid(index):
    root, path = index
    info = package_info(path, root)
    info["autoupdate"]["url"] = {"linux": "https://example.org/${version}"}
    with pytest.raises(Invalid):
        validate_config(info)


def test_guard_rejects_changed_hash_removed_version_and_reordering():
    original = descriptor()
    for changed in (original.replace("old-hash", "new-hash", 1),
                    original.replace('["1.0.0"]', '["0.9.0"]', 1)):
        with pytest.raises(Invalid):
            assert_preserved(original, changed)


def test_cli_check_unconfigured_package_does_no_network(index, monkeypatch, capsys):
    root, path = index
    path.write_text('package = { name="foo", xpm={} }')
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=ci", "-c", "user.email=ci@example.org",
                    "commit", "--allow-empty", "-m", "fixture"], check=True, capture_output=True)
    assert main(["--workspace", str(root), "check"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["requests"] == 0
    assert report["packages"] == []


def test_literal_digest_covers_the_entire_descriptor(index):
    root, path = index
    record = check_package(package_info(path, root), FakeGitHub())
    assert record["descriptor_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_crlf_old_blocks_are_preserved_byte_for_byte(index):
    root, path = index
    path.write_bytes(descriptor().replace("\n", "\r\n").encode())
    before = path.read_bytes().decode()
    client = FakeGitHub()
    record = check_package(package_info(path, root), client)
    assert append(root, record, client)
    assert_preserved(before, path.read_bytes().decode())
