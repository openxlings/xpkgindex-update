import json
import subprocess

import pytest
from test_update import FakeGitHub

from xpkgindex_update.adapters import get_adapter
from xpkgindex_update.adapters import xim_pkgindex as xim
from xpkgindex_update.cli import main
from xpkgindex_update.github import BudgetExceeded, GitHub
from xpkgindex_update.lua_edit import Invalid, parse


def descriptor(policy='advance'):
    return '''package = {
    name = "foo", spec = "2",
    extensions = { autoupdate = {
        source = "https://github.com/acme/foo/releases", tag = "v${version}", latest = "%s",
        resources = { GLOBAL = { url = { linux = {
            x86_64 = "https://example.org/${tag}/foo-x86_64.tar.gz",
            aarch64 = "https://example.org/${tag}/foo-aarch64.tar.gz",
        }, windows = "https://example.org/${tag}/foo.zip" } } },
    } },
    xpm = {
        linux = {
            ["latest"] = { ref = "1.0.0" },
            ["0.9.0"] = { url = "historical-linux-only", sha256 = "immutable" },
            ["1.0.0"] = { sha256 = { x86_64 = "old-x86", aarch64 = "old-arm" } },
        },
        windows = { ["latest"] = { ref = "1.0.0" },
            ["1.0.0"] = { url = "old.zip", sha256 = "old-win" }, },
    },
}
function install() error("never execute me") end
''' % policy


@pytest.fixture
def index(tmp_path):
    path = tmp_path / 'pkgs/f/foo.lua'
    path.parent.mkdir(parents=True)
    path.write_text(descriptor())
    return tmp_path, path


def plan(index, client=None):
    root, path = index
    client = client or FakeGitHub()
    record = xim.check_package(xim.package_info(path, root), client)
    assert record['status'] == 'update-ready', record
    return record, client


def test_complete_matrix_advances_latest_and_preserves_linux_only_history(index):
    root, path = index
    before = path.read_text()
    record, client = plan(index)
    assert record['id'] == 'xim:foo'
    assert len(record['assets']) == 3
    assert xim.append(root, record, client)
    after = path.read_text()
    xim.guard(before, after)
    pkg = parse(after).fields['xpm'].fields
    assert pkg['linux'].fields['latest'].fields['ref'].string() == '1.1.0'
    assert set(pkg['linux'].fields['1.1.0'].fields) == {'x86_64', 'aarch64'}
    assert 'historical-linux-only' in after
    assert after.endswith('function install() error("never execute me") end\n')


def test_keep_does_not_move_latest(index):
    root, path = index
    path.write_text(descriptor('keep'))
    record, client = plan(index)
    xim.append(root, record, client)
    assert parse(path.read_text()).fields['xpm'].fields['linux'].fields['latest'].fields['ref'].string() == '1.0.0'


def test_missing_arch_does_not_write(index):
    root, path = index
    client = FakeGitHub()
    record, _ = plan(index, client)
    before = path.read_bytes()
    client.fail_url = 'aarch64'
    with pytest.raises(Exception):
        xim.append(root, record, client)
    assert path.read_bytes() == before
    assert xim.check_package(xim.package_info(path, root), client)['status'] == 'waiting-assets'


@pytest.mark.parametrize('mutation', ['matrix', 'tag', 'latest', 'url', 'hash', 'digest'])
def test_tampered_or_stale_plan_cannot_write(index, mutation):
    root, path = index
    record, client = plan(index)
    before = path.read_bytes()
    if mutation == 'matrix':
        record['assets'].pop()
    elif mutation == 'tag':
        client.sha = 'c' * 40
    elif mutation == 'latest':
        record['latest_after'] = '0.1.0'
    elif mutation == 'url':
        record['assets'][0]['resources']['GLOBAL']['url'] = 'https://evil.example/foo'
    elif mutation == 'hash':
        record['assets'][0]['resources']['GLOBAL']['sha256'] = 'c' * 64
    else:
        record['descriptor_sha256'] = 'd' * 64
    with pytest.raises(Invalid):
        xim.append(root, record, client)
    assert path.read_bytes() == before


def test_latest_conflict_and_changed_arch_policy_visible(index):
    root, path = index
    path.write_text(descriptor().replace('windows = { ["latest"] = { ref = "1.0.0" }',
                                         'windows = { ["latest"] = { ref = "0.9.0" }'))
    assert xim.check_package(xim.package_info(path, root), FakeGitHub())['status'] in ('error', 'policy-required')
    path.write_text(descriptor().replace('aarch64 = "https://example.org/${tag}/foo-aarch64.tar.gz",', ''))
    assert 'architecture set changed' in xim.check_package(xim.package_info(path, root), FakeGitHub())['reason']


def test_sidecar_hash_and_filename_are_verified(index):
    root, path = index
    path.write_text(descriptor().replace('source =', 'verify = "sha256-sidecar", source ='))
    client = FakeGitHub()
    client.text = lambda url: 'b' * 64 + '  ' + url.rsplit('/', 1)[-1].removesuffix('.sha256')
    record, _ = plan(index, client)
    client.text = lambda url: 'b' * 64 + '  wrong.tar.gz'
    before = path.read_bytes()
    with pytest.raises(Invalid, match='sidecar'):
        xim.append(root, record, client)
    assert path.read_bytes() == before


def test_guard_rejects_downgrade_and_history_change(index):
    _, path = index
    before = path.read_text()
    for after in [before.replace('old-x86', 'replaced'), before.replace('ref = "1.0.0"', 'ref = "0.9.0"')]:
        with pytest.raises(Invalid):
            xim.guard(before, after)


def test_adapter_dispatch_is_real():
    assert get_adapter('xim-pkgindex').append == xim.append
    assert get_adapter('mcpp-index').FORMAT == 1


def test_budget_failure_is_not_up_to_date(index):
    root, path = index
    client = FakeGitHub()
    def fail(*args):
        raise BudgetExceeded('request budget')
    client.listing = fail
    assert xim.check_package(xim.package_info(path, root), client)['status'] == 'budget-deferred'


def test_cli_fixed_plan_and_state_cursor(index, monkeypatch):
    root, path = index
    subprocess.run(['git', 'init', '-b', 'main', str(root)], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=test', '-c', 'user.email=test@example.org',
                    'commit', '-m', 'fixture'], check=True, capture_output=True)
    config = root / '.github/xpkgindex-update.yml'
    config.parent.mkdir()
    config.write_text('version: 1\nadapter: xim-pkgindex\nmax_prs: 3\n')
    class Client(FakeGitHub):
        def __init__(self, *args, **kwargs):
            super().__init__()
            self.requests, self.downloaded = 0, 0
    monkeypatch.setattr('xpkgindex_update.cli.GitHub', Client)
    output = root / 'plan.json'
    state = root / 'state.json'
    assert main(['--workspace', str(root), 'check', '--output', str(output), '--state-file', str(state)]) == 0
    data = json.loads(output.read_text())
    assert data['format'] == 2 and data['adapter'] == 'xim-pkgindex'
    assert json.loads(state.read_text())['cursor'] == 'xim:foo'
    assert main(['--workspace', str(root), 'apply', '--plan', str(output)]) == 0


def test_api_retries_count_toward_budget(monkeypatch):
    import urllib.error
    def fail(*args, **kwargs):
        raise urllib.error.URLError('offline')
    monkeypatch.setattr('urllib.request.urlopen', fail)
    monkeypatch.setattr('time.sleep', lambda _: None)
    client = GitHub(request_budget=1)
    with pytest.raises(BudgetExceeded):
        client.api('repos/acme/foo')
    assert client.requests == 1


def test_missing_arch_in_candidate_is_a_conflict(index):
    root, path = index
    # Candidate exists only on Linux; never call this complete just because the union has it.
    path.write_text(descriptor().replace('["0.9.0"]', '["1.1.0"]'))
    result = xim.check_package(xim.package_info(path, root), FakeGitHub())
    assert result['status'] == 'existing-version-conflict'


def test_numeric_revision_metadata_is_literal():
    assert parse('package={revision=1,weight=1.5}').plain() == {'revision': 1, 'weight': 1.5}


def test_runtime_signature_only_excludes_ci_declarations():
    from xpkgindex_update.manifest import runtime_signature
    base = 'package={name="a",xpm={linux={["1"]={url="https://a",sha256="old"}}}}\nfunction install() return true end'
    metadata = base.replace('name="a",', 'name="a",ci={update=true},extensions={autoupdate={tag="v${version}",url="https://a"}},')
    assert runtime_signature(base) == runtime_signature(metadata)
    assert runtime_signature(base) != runtime_signature(base.replace('sha256="old"', 'sha256="new"'))
    assert runtime_signature(base) != runtime_signature(base.replace('return true', 'return false'))


def test_guard_rejects_only_one_platform_advancing(index):
    root, path = index
    before = path.read_text()
    record, client = plan(index)
    xim.append(root, record, client)
    after = path.read_text()
    # Keep the Windows pointer behind while Linux advances.
    offset = after.index('windows = { ["latest"]')
    altered = after[:offset] + after[offset:].replace('ref = "1.1.0"', 'ref = "1.0.0"', 1)
    with pytest.raises(Invalid, match='every platform'):
        xim.guard(before, altered)


def test_runtime_diff_preserves_git_source_trailing_newline(index, capsys):
    root, path = index
    subprocess.run(['git', 'init', '-b', 'main', str(root)], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=test', '-c', 'user.email=test@example.org',
                    'commit', '-m', 'fixture'], check=True, capture_output=True)
    config = root / '.github/xpkgindex-update.yml'
    config.parent.mkdir()
    config.write_text('version: 1\nadapter: xim-pkgindex\n')
    assert main(['--workspace', str(root), 'guard', '--base', 'HEAD']) == 0
    capsys.readouterr()
    assert main(['--workspace', str(root), 'runtime-diff', '--base', 'HEAD']) == 0
    assert json.loads(capsys.readouterr().out)['packages'] == []


def test_historical_prereleases_do_not_block_a_stable_candidate(index):
    root, path = index
    path.write_text(descriptor().replace('["0.9.0"]', '["1.0.0-beta.2"]'))
    record, client = plan(index)
    assert record['candidate'] == '1.1.0'
    xim.append(root, record, client)
    assert '["1.0.0-beta.2"]' in path.read_text()


def test_semver_prerelease_ordering_and_build_equivalence():
    from xpkgindex_update.manifest import version_key
    versions = ['1.0.0-beta.2', '1.0.0-beta.11', '1.0.0-rc.1', '1.0.0']
    assert sorted(versions, key=version_key) == versions
    assert version_key('1.0.0+build.2') == version_key('1.0.0')


def test_guard_rejects_dependency_and_platform_source_changes(index):
    _, path = index
    before = path.read_text().replace('linux = {\n            ["latest"]', 'linux = {\n            deps = {"xim:old"},\n            ["latest"]')
    with pytest.raises(Invalid, match='dependencies'):
        xim.guard(before, before.replace('xim:old', 'xim:new'))


@pytest.mark.parametrize("crlf", [False, True])
def test_candidate_verification_downloads_exact_diff_without_discovery(index, crlf):
    root, path = index
    before = path.read_text()
    record, client = plan(index)
    xim.append(root, record, client)
    if crlf:
        path.write_bytes(path.read_bytes().replace(b'\r\n', b'\n').replace(b'\n', b'\r\n'))
    client.listing = lambda *args: pytest.fail('candidate CI must never discover releases')
    results = xim.verify_candidates(xim.package_info(path, root), before, client)
    assert [r['candidate'] for r in results] == ['1.1.0']
    assert len(results[0]['assets']) == 3


@pytest.mark.parametrize('change', ['hash', 'missing-platform', 'url'])
def test_candidate_verification_rejects_incomplete_or_tampered_diff(index, change):
    root, path = index
    before = path.read_text()
    record, client = plan(index)
    xim.append(root, record, client)
    text = path.read_text()
    xpm = parse(text).fields['xpm'].fields
    node = xpm['windows'].fields['1.1.0']
    if change == 'hash':
        target = node.fields['sha256']
        text = text[:target.start] + '"tampered"' + text[target.end:]
    elif change == 'url':
        target = node.fields['url']
        text = text[:target.start] + '"https://example.org/wrong.zip"' + text[target.end:]
    else:
        text = text[:node.start] + '{}' + text[node.end:]
    path.write_text(text)
    with pytest.raises(Invalid):
        xim.verify_candidates(xim.package_info(path, root), before, client)
