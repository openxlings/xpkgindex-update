"""Sparkle discovery keeps the same required-matrix and fixed-plan guarantees."""
import copy
import io
import json

import pytest
from test_update import FakeGitHub
from test_xim import descriptor

from xpkgindex_update import appcast
from xpkgindex_update.adapters import xim_pkgindex as xim
from xpkgindex_update.github import BudgetExceeded, GitHub, WaitingAssets
from xpkgindex_update.lua_edit import Invalid

SOURCE = 'https://vendor.example/appcast.xml'


def item(version, **attrs):
    enclosure = {'url': f'https://vendor.example/{version}/foo.zip', 'length': '12', **attrs}
    return (f'<item><s:shortVersionString>{version}</s:shortVersionString>'
            f'<s:version>{version}00</s:version><enclosure ' +
            ' '.join(f'{k}="{v}"' for k, v in enclosure.items()) + '/></item>')


def feed(*items):
    return f'<rss xmlns:s="{appcast.SPARKLE[1:-1]}"><channel>' + ''.join(items) + '</channel></rss>'


class Client(FakeGitHub):
    def __init__(self, text=None, missing=()):
        super().__init__()
        self.feed = text or feed(item('1.3.0'), item('1.2.0'))
        self.missing = missing

    def text(self, url, **kwargs):
        assert url == SOURCE
        return self.feed

    def listing(self, *args):
        pytest.fail('appcast discovery must not call the GitHub API')

    def commit(self, *args):
        pytest.fail('appcast releases have no GitHub commit')

    def artifact(self, url):
        if any(v in url for v in self.missing):
            raise WaitingAssets(url)
        return {'url': url, 'sha256': 'b' * 64, 'size': 12}


def index(tmp_path):
    path = tmp_path / 'pkgs/f/foo.lua'
    path.parent.mkdir(parents=True)
    text = descriptor()
    node = xim.parse(text).fields['extensions'].fields['autoupdate']
    auto = {'source': SOURCE, 'resources': {'GLOBAL': {'url': {
        'linux': {'x86_64': 'https://vendor.example/${version}/foo.zip',
                  'aarch64': 'https://vendor.example/${version}/arm.zip'},
        'windows': 'https://vendor.example/${version}/win.zip',
    }}}}
    # JSON and Lua dictionary syntax differ; use the updater's literal writer.
    path.write_text(text[:node.start] + xim.literal(auto) + text[node.end:])
    return path


def check(tmp_path, path, client):
    return xim.check_package(xim.package_info(path, tmp_path), client)


def test_common_version_intersection_apply_and_verify_without_discovery(tmp_path):
    path = index(tmp_path)
    before = path.read_text()
    client = Client(missing=('1.3.0/arm',))
    record = check(tmp_path, path, client)
    assert record['candidate'] == '1.2.0', record
    assert record['attempts'][0]['version'] == '1.3.0'
    assert record['source'] == SOURCE and 'repo' not in record
    assert 'upstream_commit' not in record
    xim.append(tmp_path, record, client)
    xim.guard(before, path.read_text())
    client.text = lambda *a, **k: pytest.fail('diff verification must not rediscover versions')
    reports = xim.verify_candidates(xim.package_info(path, tmp_path), before, client)
    assert reports[0]['status'] == 'verified' and reports[0]['candidate'] == '1.2.0'


def test_new_feed_entries_do_not_change_pinned_plan(tmp_path):
    path, client = index(tmp_path), Client()
    record = check(tmp_path, path, client)
    client.feed = feed(item('1.4.0'), item('1.3.0'), item('1.2.0'))
    xim.append(tmp_path, record, client)
    assert 'ref = "1.3.0"' in path.read_text() and '["1.4.0"]' not in path.read_text()


@pytest.mark.parametrize('mutation', ['build', 'url', 'size', 'removed', 'plan', 'resource'])
def test_changed_release_or_tampered_plan_never_writes(tmp_path, mutation):
    path, client = index(tmp_path), Client()
    before = path.read_bytes()
    record = check(tmp_path, path, client)
    if mutation == 'build':
        client.feed = client.feed.replace('1.3.000', '1.3.001')
    elif mutation == 'url':
        client.feed = client.feed.replace('1.3.0/foo.zip', '1.3.0/replaced.zip')
    elif mutation == 'size':
        client.feed = client.feed.replace('length="12"', 'length="13"')
    elif mutation == 'removed':
        client.feed = feed(item('1.2.0'))
    elif mutation == 'plan':
        record['source_release_sha256'] = 'c' * 64
    else:
        record['assets'][0]['resources']['GLOBAL']['sha256'] = 'c' * 64
    with pytest.raises(Invalid):
        xim.append(tmp_path, record, client)
    assert path.read_bytes() == before


@pytest.mark.parametrize('failure,status', [
    (WaitingAssets('not published'), 'waiting-assets'),
    (BudgetExceeded('budget'), 'budget-deferred'),
    (OSError('network failure'), 'error'),
])
def test_resource_failures_are_distinct(tmp_path, failure, status):
    path, client = index(tmp_path), Client()
    def fail(url):
        raise copy.copy(failure)
    client.artifact = fail
    record = check(tmp_path, path, client)
    assert record['status'] == status and 'candidate' not in record


def test_enclosure_must_match_required_resource_and_size(tmp_path):
    path = index(tmp_path)
    for attrs in [{'url': 'https://vendor.example/elsewhere.zip'}, {'length': '13'}]:
        record = check(tmp_path, path, Client(feed(item('1.3.0', **attrs))))
        assert record['status'] == 'error' and 'appcast' in record['reason']


def test_keep_and_up_to_date(tmp_path):
    path, client = index(tmp_path), Client()
    path.write_text(path.read_text().replace('source =', 'latest = "keep", source ='))
    record = check(tmp_path, path, client)
    xim.append(tmp_path, record, client)
    assert 'ref = "1.0.0"' in path.read_text()
    assert check(tmp_path, path, client)['status'] == 'up-to-date'


def test_delta_and_beta_entries_are_not_candidates():
    stable = item('1.2.0').replace('</item>', '<s:deltas><enclosure url="https://vendor.example/delta" /></s:deltas></item>')
    beta = item('1.4.0').replace('<item>', '<item><s:channel>beta</s:channel>')
    releases = appcast.releases(Client(feed(stable, beta, item('1.5.0-beta'))), SOURCE)
    assert set(releases) == {'1.2.0'}


def test_legacy_enclosure_version_attributes():
    text = feed('<item><enclosure url="https://vendor.example/1.2.zip" length="12" '
                's:shortVersionString="1.2" s:version="120" /></item>')
    assert appcast.releases(Client(text), SOURCE)['1.2']['build'] == '120'


@pytest.mark.parametrize('text', [
    '<html/>', '<rss>', '<rss><channel/></rss>',
    '<!DOCTYPE rss [<!ENTITY x "payload">]><rss><channel/></rss>',
    feed(item('1.2.0'), item('1.2.0')),
    feed(item('1.2.0'), item('1.2')),
    feed(item('1.2.0', url='http://vendor.example/a')),
    feed(item('1.2.0', url='https://user:secret@vendor.example/a')),
    feed(item('1.2.0', url='https://vendor.example/a?signature=ephemeral')),
    feed(item('1.2.0', length='0')),
])
def test_malformed_ambiguous_or_unsafe_feed_fails_closed(text):
    with pytest.raises(Invalid):
        appcast.releases(Client(text), SOURCE)


def test_metadata_download_is_bounded_and_credential_free(monkeypatch):
    requests = []
    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            return io.BytesIO(b'x' * (1024 * 1024 + 1))
    monkeypatch.setattr('xpkgindex_update.github.public_url', lambda u: u)
    monkeypatch.setattr('urllib.request.build_opener', lambda *a: Opener())
    client = GitHub('must-not-leak')
    with pytest.raises(Invalid, match='exceeds'):
        appcast.releases(client, SOURCE)
    assert client.requests == 1 and client.downloaded == 1024 * 1024 + 1
    assert not requests[0].has_header('Authorization')


def test_appcast_does_not_accept_a_github_tag_template(tmp_path):
    path = index(tmp_path)
    path.write_text(path.read_text().replace('source =', 'tag = "v${version}", source ='))
    assert 'tag templates' in check(tmp_path, path, Client())['reason']


def test_plan_roundtrips_through_json(tmp_path):
    path, client = index(tmp_path), Client()
    record = json.loads(json.dumps(check(tmp_path, path, client)))
    assert xim.append(tmp_path, record, client)
