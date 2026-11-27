"""An entry keeps its form (xim-pkgindex K4): a new version is written the
way the platform's entries name their architectures; an existing entry may
gain an artifact for another architecture; alias keys beside `latest` pass.
"""
import pytest
from test_update import FakeGitHub

from xpkgindex_update.adapters import xim_pkgindex as xim
from xpkgindex_update.lua_edit import Invalid, parse
from xpkgindex_update.manifest import assert_preserved, gains_architectures

H = 'a' * 64


def descriptor(linux_url, digests, extra=''):
    return '''package = {
    name = "foo", spec = "2", archs = {"x86_64", "aarch64"},
    extensions = { autoupdate = {
        source = "https://github.com/acme/foo/releases", tag = "v${version}",
        resources = { GLOBAL = { url = { linux = %s } } },
    } },
    xpm = {
        linux = {
            ["latest"] = { ref = "1.0.0" },%s
            ["1.0.0"] = {
                url = "https://example.org/v1.0.0/foo.tar.gz",
                sha256 = %s,
            },
        },
    },
}
''' % (linux_url, extra, digests)


SHARED = '"https://example.org/${tag}/foo.tar.gz"'
X86 = '{ x86_64 = "https://example.org/${tag}/foo.tar.gz" }'


@pytest.mark.parametrize('url,digests,named', [
    (SHARED, '{ x86_64 = "%s" }' % H, ['x86_64']),
    (SHARED, '{ x86_64 = "%s", aarch64 = "%s" }' % (H, H), ['x86_64', 'aarch64']),
    (X86, '{ x86_64 = "%s" }' % H, ['x86_64']),
])
def test_a_new_version_names_the_architectures_its_platform_names(tmp_path, url, digests, named):
    path = tmp_path / 'pkgs/f/foo.lua'
    path.parent.mkdir(parents=True)
    path.write_text(descriptor(url, digests))
    before = path.read_text()
    client = FakeGitHub()
    record = xim.check_package(xim.package_info(path, tmp_path), client)
    assert record['status'] == 'update-ready', record
    assert xim.append(tmp_path, record, client)
    entry = parse(path.read_text()).fields['xpm'].fields['linux'].fields['1.1.0'].plain()
    assert entry == {'url': 'https://example.org/v1.1.0/foo.tar.gz', 'sha256': {a: 'b' * 64 for a in named}}
    assert list(entry['sha256']) == named
    xim.guard(before, path.read_text())
    [report] = xim.verify_candidates(xim.package_info(path, tmp_path), before, client)
    assert report['candidate'] == '1.1.0'


def test_a_candidate_that_names_other_architectures_is_refused(tmp_path):
    path = tmp_path / 'pkgs/f/foo.lua'
    path.parent.mkdir(parents=True)
    path.write_text(descriptor(SHARED, '{ x86_64 = "%s" }' % H))
    before = path.read_text()
    client = FakeGitHub()
    record = xim.check_package(xim.package_info(path, tmp_path), client)
    xim.append(tmp_path, record, client)
    path.write_text(path.read_text().replace(
        'x86_64 = "' + 'b' * 64 + '"', 'x86_64 = "' + 'b' * 64 + '", aarch64 = "' + 'b' * 64 + '"'))
    with pytest.raises(Invalid, match='other architectures'):
        xim.verify_candidates(xim.package_info(path, tmp_path), before, client)


def test_one_artifact_under_differing_digests_is_not_one_named_artifact(tmp_path):
    path = tmp_path / 'pkgs/f/foo.lua'
    path.parent.mkdir(parents=True)
    path.write_text(descriptor(SHARED, '{ x86_64 = "%s", aarch64 = "%s" }' % (H, 'c' * 64)))
    with pytest.raises(Invalid, match='architecture set changed'):
        xim.validate_config(xim.package_info(path, tmp_path))


def test_an_alias_beside_latest_passes_the_guard_and_the_update(tmp_path):
    path = tmp_path / 'pkgs/f/foo.lua'
    path.parent.mkdir(parents=True)
    path.write_text(descriptor(SHARED, '{ x86_64 = "%s" }' % H, extra='\n            ["lts"] = { ref = "1.0.0" },'))
    before = path.read_text()
    client = FakeGitHub()
    record = xim.check_package(xim.package_info(path, tmp_path), client)
    assert record['status'] == 'update-ready', record
    xim.append(tmp_path, record, client)
    after = path.read_text()
    xim.guard(before, after)
    linux = parse(after).fields['xpm'].fields['linux'].fields
    assert linux['lts'].plain() == {'ref': '1.0.0'} and linux['latest'].plain() == {'ref': '1.1.0'}
    # An automatic change does not move the alias; a reviewed one may.
    moved = after.replace('["lts"] = { ref = "1.0.0" }', '["lts"] = { ref = "1.1.0" }')
    with pytest.raises(Invalid):
        xim.guard(before, moved)
    xim.guard(before, moved, reviewed=True)


OLD = {'url': {'GLOBAL': 'https://e.org/foo-x86_64.tar.gz', 'CN': 'https://m.cn/foo-x86_64.tar.gz'}, 'sha256': H}


@pytest.mark.parametrize('new,ok', [
    ({'x86_64': OLD, 'aarch64': {'url': 'https://e.org/foo-aarch64.tar.gz', 'sha256': 'c' * 64}, 'revision': 1}, True),
    ({'x86_64': OLD, 'aarch64': {'url': 'https://e.org/foo-aarch64.tar.gz', 'sha256': 'c' * 64}}, True),
    ({'x86_64': {**OLD, 'sha256': 'd' * 64}, 'aarch64': {'url': 'https://e.org/a', 'sha256': 'c' * 64}}, False),
    ({'x86_64': OLD}, False),
    ({'x86_64': OLD, 'aarch64': {'url': 'https://e.org/a', 'sha256': 'c' * 64}, 'deps': ['x']}, False),
])
def test_an_entry_may_gain_an_architecture_and_keep_what_it_had(new, ok):
    assert gains_architectures(OLD, new) is ok


def test_assert_preserved_accepts_an_added_architecture():
    before = 'package = { xpm = { linux = { ["1.0"] = { url = "https://e.org/f.tar.gz", sha256 = "%s" } } } }' % H
    after = ('package = { xpm = { linux = { ["1.0"] = { revision = 1,'
             ' x86_64 = { url = "https://e.org/f.tar.gz", sha256 = "%s" },'
             ' aarch64 = { url = "https://e.org/g.tar.gz", sha256 = "%s" } } } } }' % (H, 'c' * 64))
    assert_preserved(before, after)
    with pytest.raises(Invalid):
        assert_preserved(before, after.replace('f.tar.gz', 'h.tar.gz'))


def test_a_gained_architecture_is_one_platform_s_change():
    text = ('package = { xpm = {'
            ' linux = { ["1.0"] = { url = "https://e.org/f.tar.gz", sha256 = "%s" } },'
            ' windows = { ["1.0"] = { url = "https://e.org/f.zip", sha256 = "%s" } } } }' % (H, H))
    after = text.replace('linux = { ["1.0"] = { url = "https://e.org/f.tar.gz", sha256 = "%s" } }' % H,
                         'linux = { ["1.0"] = { revision = 1,'
                         ' x86_64 = { url = "https://e.org/f.tar.gz", sha256 = "%s" },'
                         ' aarch64 = { url = "https://e.org/g.tar.gz", sha256 = "%s" } } }' % (H, 'c' * 64))
    assert after != text
    xim.guard(text, after, reviewed=True)


def test_an_entry_through_source_names_its_architectures_too(tmp_path):
    text = descriptor(SHARED, '{ x86_64 = "%s", aarch64 = "%s" }' % (H, H))
    text = text.replace('                url = "https://example.org/v1.0.0/foo.tar.gz",\n', '')
    text = text.replace('    xpm = {\n', '    xpm = {\n        source = "https://example.org/v${version}/foo.tar.gz",\n')
    path = tmp_path / 'pkgs/f/foo.lua'
    path.parent.mkdir(parents=True)
    path.write_text(text)
    xim.validate_config(xim.package_info(path, tmp_path))
    client = FakeGitHub()
    record = xim.check_package(xim.package_info(path, tmp_path), client)
    assert xim.append(tmp_path, record, client)
    entry = parse(path.read_text()).fields['xpm'].fields['linux'].fields['1.1.0'].plain()
    assert entry['sha256'] == {'x86_64': 'b' * 64, 'aarch64': 'b' * 64}
