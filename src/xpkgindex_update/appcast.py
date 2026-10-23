"""Stable full releases from public Sparkle RSS appcasts; no vendor-specific DSL."""
from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from .lua_edit import Invalid
from .manifest import ordered, version_key

SPARKLE = '{http://www.andymatuschak.org/xml-namespaces/sparkle}'


def is_appcast(source):
    if not isinstance(source, str):
        return False
    url = urlsplit(source)
    return (url.scheme == 'https' and bool(url.hostname) and not url.username
            and not url.password and url.port in (None, 443)
            and url.path.endswith('.xml') and not url.query and not url.fragment)


def releases(client, source):
    text = client.text(source, max_bytes=1024 * 1024)
    # ElementTree does not fetch external entities. Reject DTD/entity declarations
    # as well, so even a small feed cannot expand an internal entity payload.
    if re.search(r'<!\s*(?:DOCTYPE|ENTITY)\b', text, re.I):
        raise Invalid('appcast DTD/entities are not supported')
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise Invalid('invalid Sparkle appcast XML') from exc
    if root.tag != 'rss' or root.find('channel') is None:
        raise Invalid('source XML is not a Sparkle RSS appcast')
    result, seen = {}, set()
    for item in root.findall('./channel/item'):
        # A channel is an opt-in release track (e.g. beta); never promote it.
        if item.find(SPARKLE + 'channel') is not None:
            continue
        enclosures = item.findall('enclosure')  # never sparkle:deltas/enclosure
        if len(enclosures) != 1:
            raise Invalid('appcast item requires one full enclosure')
        enclosure = enclosures[0]
        def field(name):
            return (item.findtext(SPARKLE + name) or enclosure.get(SPARKLE + name) or '').strip()
        version, build = field('shortVersionString'), field('version')
        if not re.fullmatch(r'[0-9]+(?:\.[0-9]+)*', version) or not ordered(version):
            continue  # prereleases / unknown ordering are not stable candidates
        url, length = enclosure.get('url', ''), enclosure.get('length', '')
        parsed = urlsplit(url)
        if (not parsed.hostname or parsed.scheme != 'https' or parsed.username or parsed.password
                or parsed.port not in (None, 443) or parsed.fragment or parsed.query
                or not length.isdigit() or int(length) <= 0 or not build):
            raise Invalid('appcast requires a fixed HTTPS full enclosure, build and positive length')
        release = {'version': version, 'build': build, 'url': url, 'size': int(length),
                   'signature': enclosure.get(SPARKLE + 'edSignature', ''),
                   'minimum_system': field('minimumSystemVersion'),
                   'hardware': field('hardwareRequirements')}
        if version_key(version) in seen:
            raise Invalid('ambiguous appcast version/build or architecture')
        seen.add(version_key(version))
        result[version] = release
    if not result:
        raise Invalid('appcast has no supported stable full releases')
    return result


def digest(release):
    return hashlib.sha256(json.dumps(release, sort_keys=True).encode()).hexdigest()


def validate_assets(release, assets, targets):
    """The discovery enclosure must actually be one of the required resources."""
    matched = [r for a in assets for region, r in a['resources'].items()
               if targets[region]['required'] and r['url'] == release['url']]
    if not matched:
        raise Invalid('appcast full enclosure is absent from required resource templates')
    if any(r['size'] != release['size'] for r in matched):
        raise Invalid('appcast enclosure length differs from downloaded resource')
