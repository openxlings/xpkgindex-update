"""Publication admission uses full required-target intersection, without editing history."""
import json

import pytest
from test_update import FakeGitHub
from test_xim import descriptor

from xpkgindex_update.adapters import xim_pkgindex as xim
from xpkgindex_update.github import BudgetExceeded, WaitingAssets
from xpkgindex_update.lua_edit import Invalid


def setup(tmp_path, resources=None, **extra):
    path = tmp_path / 'pkgs/f/foo.lua'
    path.parent.mkdir(parents=True)
    text = descriptor()
    node = xim.parse(text).fields['extensions'].fields['autoupdate']
    auto = {'source': 'https://github.com/acme/foo/releases', 'resources': resources or {
        'GLOBAL': 'https://global.example/${tag}/foo.zip',
        'CN': 'https://mirror.example/${tag}/foo.zip',
    }, **extra}
    # This fixture uses a shared archive on each platform, rather than per-arch binaries.
    text = text.replace('["1.0.0"] = { sha256 = { x86_64 = "old-x86", aarch64 = "old-arm" } }', '["1.0.0"] = { url = "old", sha256 = "old" }')
    node = xim.parse(text).fields['extensions'].fields['autoupdate']
    def literal(v):
        if isinstance(v, bool):
            return 'true' if v else 'false'
        if isinstance(v, str):
            return json.dumps(v)
        return '{' + ','.join(k + '=' + literal(x) for k, x in v.items()) + '}'
    path.write_text(text[:node.start] + literal(auto) + text[node.end:])
    return tmp_path, path


class Client(FakeGitHub):
    def __init__(self, missing=(), broken=None):
        super().__init__(releases=[{'tag_name': 'v' + v} for v in ['1.5.0', '1.4.0', '1.3.0', '1.2.0']])
        self.missing, self.broken = missing, broken
        self.downloads = []

    def artifact(self, url):
        self.downloads.append(url)
        for host, version in self.missing:
            if host in url and version in url:
                raise WaitingAssets(url)
        if self.broken:
            return self.broken(url)
        return super().artifact(url)


def check(index, client):
    root, path = index
    return xim.check_package(xim.package_info(path, root), client)


def test_true_intersection_with_gaps_and_immutable_history(tmp_path):
    index = setup(tmp_path)
    before = index[1].read_bytes()
    client = Client([('global', '1.5.0'), ('mirror', '1.4.0')])
    record = check(index, client)
    assert record['status'] == 'update-ready', record
    assert record['candidate'] == '1.3.0'  # min(latest GLOBAL, latest CN) would wrongly select 1.4
    xim.append(index[0], record, client)
    xim.guard(before.decode(), index[1].read_bytes().decode())
    client.listing = lambda *a: pytest.fail('verification must not rediscover')
    assert xim.verify_candidates(xim.package_info(index[1], index[0]), before.decode(), client)[0]['status'] == 'verified'


def test_optional_mirror_does_not_block_and_is_not_written(tmp_path):
    index = setup(tmp_path, {'GLOBAL': 'https://global.example/${tag}/foo.zip', 'CN': {'url': 'https://mirror.example/${tag}/foo.zip', 'required': False}})
    client = Client([('mirror', '1.5.0')])
    record = check(index, client)
    assert record['candidate'] == '1.5.0' and record['optional_unavailable']['CN']
    assert all(set(a['resources']) == {'GLOBAL'} for a in record['assets'])
    xim.append(index[0], record, client)
    assert 'mirror.example/v1.5.0' not in index[1].read_text()


def test_cn_required_global_optional_selects_cn_latest(tmp_path):
    index = setup(tmp_path, {'GLOBAL': {'url': 'https://global.example/${tag}/foo.zip', 'required': False}, 'CN': 'https://mirror.example/${tag}/foo.zip'})
    record = check(index, Client([('mirror', '1.5.0'), ('global', '1.4.0')]))
    assert record['candidate'] == '1.4.0'
    assert all(set(a['resources']) == {'CN'} for a in record['assets'])


@pytest.mark.parametrize('failure,status', [(OSError('network'), 'error'), (Invalid('bad archive'), 'error'), (BudgetExceeded('budget'), 'budget-deferred')])
def test_unknown_or_corrupt_required_asset_never_falls_back(tmp_path, failure, status):
    index = setup(tmp_path)
    def fail(url):
        raise failure
    record = check(index, Client(broken=fail))
    assert record['status'] == status
    assert not record.get('candidate')


def test_no_intersection_never_downgrades_or_writes(tmp_path):
    index = setup(tmp_path)
    before = index[1].read_bytes()
    record = check(index, Client([('mirror', '1.')]))
    assert record['status'] == 'waiting-assets'
    assert index[1].read_bytes() == before


def test_hash_disagreement_required_fails_optional_omits(tmp_path):
    def artifact(url):
        return {'url': url, 'size': 12, 'sha256': ('c' if 'mirror' in url else 'b') * 64}
    index = setup(tmp_path)
    assert check(index, Client(broken=artifact))['status'] == 'error'
    path = index[1]
    path.write_text(path.read_text().replace('CN="https://mirror.example/${tag}/foo.zip"', 'CN={url="https://mirror.example/${tag}/foo.zip",required=false}'))
    record = check(index, Client(broken=artifact))
    assert record['status'] == 'update-ready'
    assert all(set(a['resources']) == {'GLOBAL'} for a in record['assets'])


def test_mirror_redirect_to_github_is_not_mirror_support(tmp_path):
    index = setup(tmp_path)
    def artifact(url):
        return {'url': url, 'final_url': 'https://release-assets.githubusercontent.com/foo', 'size': 12, 'sha256': 'b' * 64}
    record = check(index, Client(broken=artifact))
    assert record['status'] == 'waiting-assets'
    assert any('redirects' in a['reason'] for a in record['attempts'])


@pytest.mark.parametrize('extra,resources,message', [
    ({'source': 'https://vendor.example/versions.json'}, None, 'unsupported-source'),
    ({}, {'GLOBAL': {'url': 'https://a/${version}', 'required': False}}, 'at least one'),
    ({}, {'GLOBAL': {'url': 'https://a/${version}', 'required': 'true'}}, 'boolean'),
])
def test_invalid_declarations_are_explicit(tmp_path, extra, resources, message):
    index = setup(tmp_path, resources, **extra)
    assert message in check(index, Client())['reason']


@pytest.mark.parametrize('tag', ['1.5.0', 'v1.5.0'])
def test_simple_source_infers_numeric_and_v_tags(tmp_path, tag):
    index = setup(tmp_path, {'GLOBAL': 'https://global.example/${tag}/foo.zip'})
    client = FakeGitHub(releases=[{'tag_name': tag}])
    assert check(index, client)['candidate'] == '1.5.0'


def test_numeric_upstream_builds_keep_their_identity_and_order(tmp_path):
    from xpkgindex_update.manifest import expand, tag_version
    assert tag_version('jdk-${version}', 'jdk-25.0.4+7') == '25.0.4+7'
    assert xim.version_key('25.0.4+8') > xim.version_key('25.0.4+7')
    assert xim.version_key('25.0.4+7') > xim.version_key('25.0.4')
    assert expand('${version:base}_${version:build}', '25.0.4+7', 'jdk-25.0.4+7') == '25.0.4_7'


def test_manual_newer_architecture_cannot_be_dropped(tmp_path):
    root, path = setup(tmp_path)
    text = path.read_text().replace('["0.9.0"] = { url = "historical-linux-only", sha256 = "immutable" }', '["1.2.0"] = { x86_64 = {url="manual",sha256="old"}, aarch64={url="manual-arm",sha256="old"} }')
    path.write_text(text)
    result = check((root, path), Client())
    assert result['status'] == 'error' and 'architecture set changed' in result['reason']
