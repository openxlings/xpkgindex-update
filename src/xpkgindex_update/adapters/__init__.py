"""One adapter dispatch for discovery, application, proposals and guards."""
from types import SimpleNamespace

from .. import manifest
from ..lua_edit import Invalid
from . import mcpp_index, xim_pkgindex


def get_adapter(name):
    if name == 'xim-pkgindex':
        return xim_pkgindex
    if name == 'mcpp-index':
        return SimpleNamespace(FORMAT=1, package_info=manifest.package_info,
                               validate_config=manifest.validate_config,
                               check_package=manifest.check_package,
                               append=mcpp_index.append, guard=manifest.assert_preserved)
    raise Invalid('unsupported adapter')
