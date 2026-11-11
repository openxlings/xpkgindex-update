"""A corrective repack of an existing version, through the guard.

The revision field of xpackage-v2 names a repack of an unchanged upstream
version. The updater's guard keeps existing version entries immutable so a
hand edit cannot fight the automatic one; the one change it admits is the
corrective repack itself -- a raised revision whose resources, when they
change, name the corrective asset. These tests state the channel: what it
accepts, and each way it is refused.
"""

import pytest

from xpkgindex_update.adapters import xim_pkgindex as xim
from xpkgindex_update.manifest import Invalid, assert_preserved

BASE = """\
package = {
    name = "pkg",
    xvm_enable = true,

    xpm = {
        linux = {
            ["latest"] = { ref = "1.0.0" },
            ["1.0.0"] = {
                revision = 1,
                x86_64 = {
                    url = {
                        GLOBAL = "https://example.com/pkg/releases/download/1.0.0/pkg-1.0.0-linux-x86_64.tar.gz",
                        CN = "https://mirror.example.com/pkg/releases/download/1.0.0/pkg-1.0.0-linux-x86_64.tar.gz",
                    },
                    sha256 = "aaa",
                },
            },
        },
        macosx = {
            ["latest"] = { ref = "1.0.0" },
            ["1.0.0"] = {
                url = "https://example.com/pkg/releases/download/1.0.0/pkg-1.0.0-macosx-arm64.tar.xz",
                sha256 = "bbb",
            },
        },
    }
}
"""

OLD_LINUX = """\
["1.0.0"] = {
                revision = 1,
                x86_64 = {
                    url = {
                        GLOBAL = "https://example.com/pkg/releases/download/1.0.0/pkg-1.0.0-linux-x86_64.tar.gz",
                        CN = "https://mirror.example.com/pkg/releases/download/1.0.0/pkg-1.0.0-linux-x86_64.tar.gz",
                    },
                    sha256 = "aaa",
                },
            },"""

OLD_MACOS = """\
["1.0.0"] = {
                url = "https://example.com/pkg/releases/download/1.0.0/pkg-1.0.0-macosx-arm64.tar.xz",
                sha256 = "bbb",
            },"""


def linux_entry(entry_revision, marker, digest):
    return f"""\
["1.0.0"] = {{
                revision = {entry_revision},
                x86_64 = {{
                    url = {{
                        GLOBAL = "https://example.com/pkg/releases/download/1.0.0/pkg-1.0.0{marker}-linux-x86_64.tar.gz",
                        CN = "https://mirror.example.com/pkg/releases/download/1.0.0/pkg-1.0.0{marker}-linux-x86_64.tar.gz",
                    }},
                    sha256 = "{digest}",
                }},
            }},"""


def macos_entry(entry_revision, marker, digest):
    revision = f"revision = {entry_revision},\n                " if entry_revision else ""
    return f"""\
["1.0.0"] = {{
                {revision}url = "https://example.com/pkg/releases/download/1.0.0/pkg-1.0.0{marker}-macosx-arm64.tar.xz",
                sha256 = "{digest}",
            }},"""

def corrective(linux=None, macos=None):
    if linux is None:
        linux = linux_entry(2, "-r2", "ccc")
    if macos is None:
        macos = macos_entry(2, "-r2", "ddd")
    after = BASE.replace(OLD_LINUX, linux)
    return after.replace(OLD_MACOS, macos)


def test_a_corrective_repack_passes():
    assert_preserved(BASE, corrective())


def test_a_corrective_repack_passes_the_descriptor_guard():
    xim.guard(BASE, corrective())


def test_comments_inside_a_corrected_entry_are_free():
    after = corrective().replace(
        '["1.0.0"] = {\n                revision = 2,',
        '["1.0.0"] = {  -- repacked: the archive missed a file\n                revision = 2,', 1)
    assert_preserved(BASE, after)


def test_revision_may_restate_without_new_assets():
    assert_preserved(BASE, BASE.replace("revision = 1,", "revision = 2,"))


def test_changed_assets_without_the_marker_are_refused():
    with pytest.raises(Invalid):
        assert_preserved(BASE, corrective(
            linux=linux_entry(2, "-other", "ccc"),
            macos=macos_entry(2, "-other", "ddd")))


def test_assets_changed_without_a_raised_revision_are_refused():
    with pytest.raises(Invalid):
        assert_preserved(BASE, corrective(
            linux=linux_entry(1, "-r2", "ccc"),
            macos=macos_entry(1, "-r2", "ddd")))


def test_revision_lowering_alone_is_refused():
    with pytest.raises(Invalid):
        assert_preserved(BASE, corrective(
            linux=linux_entry(0, "", "aaa"),
            macos=macos_entry(0, "", "bbb")))


def test_revision_may_not_carry_other_changes():
    after = corrective().replace('sha256 = "ccc"', 'sha256 = "ccc", extra = true')
    with pytest.raises(Invalid):
        assert_preserved(BASE, after)


def test_one_platform_alone_is_refused():
    with pytest.raises(Invalid, match="every platform"):
        xim.guard(BASE, corrective(macos=OLD_MACOS))


def test_removal_stays_refused():
    with pytest.raises(Invalid):
        assert_preserved(BASE, corrective(linux=""))


def test_floor_follows():
    after = corrective().replace(
        '["1.0.0"] = {\n                revision = 2,',
        '["1.0.0"] = {\n                revision = 2,\n                floor = 2,', 1)
    assert_preserved(BASE, after)
    runaway = corrective().replace(
        '["1.0.0"] = {\n                revision = 2,',
        '["1.0.0"] = {\n                revision = 2,\n                floor = 3,', 1)
    with pytest.raises(Invalid):
        assert_preserved(BASE, runaway)


def test_a_shared_template_entry_becomes_an_explicit_corrective_table():
    before = BASE.replace(OLD_MACOS, '["1.0.0"] = "XLINGS_RES",')
    after = corrective()
    assert_preserved(before, after)


def test_a_template_entry_becoming_a_table_without_the_marker_is_refused():
    before = BASE.replace(OLD_MACOS, '["1.0.0"] = "XLINGS_RES",')
    with pytest.raises(Invalid):
        assert_preserved(before, corrective(
            macos=macos_entry(2, "-other", "ddd")))


def test_a_platform_left_on_the_template_is_refused():
    before = BASE.replace(OLD_LINUX, '["1.0.0"] = "XLINGS_RES",')
    after = before.replace(OLD_MACOS, macos_entry(2, "-r2", "ddd"))
    with pytest.raises(Invalid, match="every platform"):
        xim.guard(before, after)
