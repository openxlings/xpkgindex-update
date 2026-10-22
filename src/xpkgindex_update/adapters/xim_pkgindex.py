"""Explicit xim policy: complete resource matrices and atomic latest advancement."""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from ..github import BudgetExceeded, GitHub, WaitingAssets
from ..lua_edit import Invalid, parse
from ..manifest import (
    PLATFORMS,
    assert_preserved,
    descriptor_digest,
    expand,
    ordered,
    tag_version,
    version_key,
)

FORMAT = 2
ARCHES = {'x86_64', 'aarch64', 'x86', 'armv7', 'riscv64'}


def package_info(path: Path, root: Path) -> dict:
    root = root.resolve()
    original, path = path, path.resolve()
    if original.is_symlink() or not path.is_relative_to(root / 'pkgs'):
        raise Invalid('descriptor must be a regular file inside pkgs')
    text = path.read_bytes().decode('utf-8')
    pkg = parse(text).table()
    ns = pkg.get('namespace')
    ext = pkg.get('extensions')
    auto = ext.table().get('autoupdate') if ext else None
    platforms = {}
    xpm = pkg.get('xpm')
    if xpm:
        for plat, node in xpm.table().items():
            if plat in PLATFORMS and node.kind == 'table':
                entries = {v: n for v, n in node.fields.items() if re.match(r'^(?:[0-9]|b[0-9])', v)}
                if entries:
                    platforms[plat] = {'node': node, 'entries': entries}
    return {'id': (ns.string() if ns else 'xim') + ':' + pkg['name'].string(),
            'path': path.relative_to(root).as_posix(), 'text': text, 'package': pkg,
            'platforms': platforms, 'direct': [], 'autoupdate': auto.plain() if auto else None}


def regions(value):
    if isinstance(value, str):
        return {'GLOBAL': value}
    if isinstance(value, dict) and set(value) == {'GLOBAL', 'CN'}:
        return value
    raise Invalid('resource must be an HTTPS URL or a GLOBAL/CN pair')


def matrix(info):
    urls = info['autoupdate']['url']
    if isinstance(urls, dict) and set(urls) <= set(PLATFORMS):
        if set(urls) != set(info['platforms']):
            raise Invalid('URL platforms must equal the versioned platform set')
    else:
        urls = dict.fromkeys(info['platforms'], urls)
    result = {}
    for plat, value in urls.items():
        if isinstance(value, dict) and set(value) <= ARCHES:
            if not value:
                raise Invalid('empty architecture matrix')
            result[plat] = {a: regions(v) for a, v in value.items()}
        else:
            result[plat] = {'shared': regions(value)}
    return result


def latest_refs(info):
    refs = {}
    for plat, p in info['platforms'].items():
        latest = p['node'].fields.get('latest')
        if latest is None or set(latest.table()) != {'ref'}:
            raise Invalid('every versioned platform needs a literal latest.ref')
        ref = latest.fields['ref'].string()
        if ref not in p['entries'] or not ordered(ref):
            raise Invalid('latest must name an existing ordered version')
        refs[plat] = ref
    if len(set(refs.values())) != 1:
        raise Invalid('policy-required: current platform latest refs disagree')
    return refs


def entry_arches(entry):
    if entry.kind != 'table':
        return None
    arches = set(entry.fields) & ARCHES
    if arches:
        return arches
    sha = entry.fields.get('sha256')
    return set(sha.fields) if sha and sha.kind == 'table' else None


def validate_config(info):
    auto = info['autoupdate']
    required = {'upstream', 'tag', 'latest', 'url'}
    if not isinstance(auto, dict) or not required <= set(auto) or set(auto) - required - {'verify'}:
        raise Invalid('xim autoupdate requires upstream, tag, latest, url; optional verify')
    if not isinstance(auto['upstream'], str) or not re.fullmatch(r'[\w.-]+/[\w.-]+', auto['upstream']):
        raise Invalid('upstream must be owner/repository')
    if auto['latest'] not in ('advance', 'keep'):
        raise Invalid('latest policy must be advance or keep')
    if auto.get('verify') not in (None, 'sha256-sidecar'):
        raise Invalid('unsupported verification policy')
    if not isinstance(auto['tag'], str):
        raise Invalid('tag must be a string')
    tag_version(auto['tag'], '')
    if not info['platforms']:
        raise Invalid('no versioned platforms')
    refs = latest_refs(info)
    for plat, arches in matrix(info).items():
        expected = entry_arches(info['platforms'][plat]['entries'][refs[plat]])
        if expected and set(arches) != expected:
            raise Invalid(f'architecture set changed for {plat}: {sorted(expected)} -> {sorted(arches)}')
        if not expected and len(arches) > 1:
            raise Invalid(f'policy-required: {plat} has no verified multi-architecture baseline')
        for resources in arches.values():
            for url in resources.values():
                if not isinstance(url, str) or not url.startswith('https://'):
                    raise Invalid('URL template must use HTTPS')
                if '${version}' not in url and '${tag}' not in url:
                    raise Invalid('resource URL must be versioned')
                expand(url, '0.0', 'v0.0')


def verify_resource(github, url, sidecar):
    resource = github.artifact(url)
    if sidecar:
        match = re.fullmatch(r'([0-9a-fA-F]{64})\s+\*?(.+?)\s*', github.text(url + '.sha256'))
        filename = unquote(urlsplit(url).path.rsplit('/', 1)[-1])
        if not match or match[2] != filename or match[1].lower() != resource['sha256']:
            raise Invalid(f'sidecar filename/hash mismatch: {url}')
        resource = {**resource, 'sidecar_sha256': match[1].lower()}
    return resource


def check_package(info, github: GitHub):
    record = {'id': info['id'], 'path': info['path'], 'status': 'error'}
    try:
        validate_config(info)
        auto = info['autoupdate']
        refs = latest_refs(info)
        current = next(iter(refs.values()))
        existing = set().union(*(p['entries'] for p in info['platforms'].values()))
        if any(not ordered(v) for v in existing):
            raise Invalid('policy-required: historical version has no release ordering')
        floor = max(existing, key=version_key)
        record.update(current=current, repo=auto['upstream'], latest_policy=auto['latest'],
                      latest_before=refs, descriptor_sha256=descriptor_digest(info['text']))
        releases = github.listing(auto['upstream'], 'releases')
        dates = [r.get('published_at', '') for r in releases if tag_version(auto['tag'], r['tag_name']) == current]
        current_date = max(dates, default='')
        excluded = {r['tag_name'] for r in releases if r.get('draft') or r.get('prerelease')}
        candidates = {}
        for r in releases:
            if r['tag_name'] in excluded or current_date and r.get('published_at', '') < current_date:
                continue
            v = tag_version(auto['tag'], r['tag_name'])
            if v:
                candidates[r['tag_name']] = v
        record['discovery'] = 'releases'
        if not candidates:
            record['discovery'] = 'tags'
            for t in github.listing(auto['upstream'], 'tags'):
                v = tag_version(auto['tag'], t['name'])
                if v and t['name'] not in excluded:
                    candidates[t['name']] = v
        if not candidates:
            raise Invalid('policy-required: no matching stable release or numeric tag')
        newest = max(candidates.values(), key=version_key)
        presence = [newest in p['entries'] for p in info['platforms'].values()]
        if any(presence) and not all(presence):
            raise Invalid('existing-version-conflict: candidate exists on only some platforms')
        ahead = {t: v for t, v in candidates.items() if version_key(v)[0] == version_key(floor)[0] and version_key(v) > version_key(floor)}
        if not ahead:
            record['status'] = 'up-to-date'
            return record
        tag = max(ahead, key=lambda t: version_key(ahead[t]))
        candidate = ahead[tag]
        if sum(version_key(v) == version_key(candidate) for v in ahead.values()) != 1:
            raise Invalid('ambiguous version spellings or tags')
        record.update(candidate=candidate, tag=tag, upstream_commit=github.commit(auto['upstream'], tag),
                      latest_after=candidate if auto['latest'] == 'advance' else current)
        assets = []
        for plat, arches in matrix(info).items():
            for arch, templates in arches.items():
                resources = {region: verify_resource(github, expand(url, candidate, tag), auto.get('verify') == 'sha256-sidecar') for region, url in templates.items()}
                if len({r['sha256'] for r in resources.values()}) != 1:
                    raise Invalid('GLOBAL/CN hashes differ')
                assets.append({'platform': plat, 'arch': arch, 'resources': resources})
        record.update(status='update-ready', assets=assets)
    except WaitingAssets as exc:
        record.update(status='waiting-assets', reason=str(exc))
    except BudgetExceeded as exc:
        record.update(status='budget-deferred', reason=str(exc))
    except (Invalid, OSError, ValueError, KeyError) as exc:
        reason = str(exc)
        status = next((s for s in ('policy-required', 'existing-version-conflict') if reason.startswith(s)), 'error')
        record.update(status=status, reason=reason)
    return record


def literal(value, indent=''):
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=True)
    if not isinstance(value, dict):
        raise Invalid('only literal resource tables can be generated')
    def key(k):
        return k if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", k) else "[" + literal(k) + "]"
    return '{\n' + ''.join(f'{indent}    {key(k)} = {literal(v, indent + "    ")},\n' for k, v in value.items()) + indent + '}' 


def append(root: Path, record: dict, github: GitHub):
    if record.get('status') != 'update-ready':
        raise Invalid('only update-ready records can be applied')
    info = package_info(root / record['path'], root)
    validate_config(info)
    auto = info['autoupdate']
    if info['id'] != record['id'] or auto['upstream'] != record['repo']:
        raise Invalid('plan identity/repository mismatch')
    if descriptor_digest(info['text']) != record['descriptor_sha256']:
        raise Invalid('descriptor changed since check; regenerate plan')
    refs = latest_refs(info)
    if refs != record['latest_before'] or auto['latest'] != record['latest_policy']:
        raise Invalid('plan latest policy/baseline mismatch')
    candidate = record['candidate']
    if tag_version(auto['tag'], record['tag']) != candidate:
        raise Invalid('plan tag/version mismatch')
    for p in info['platforms'].values():
        if any(not ordered(v) or version_key(v) >= version_key(candidate) for v in p['entries']):
            raise Invalid('candidate is not newer than every existing version')
    target = candidate if auto['latest'] == 'advance' else next(iter(refs.values()))
    if record['latest_after'] != target:
        raise Invalid('plan latest target mismatch')
    if github.commit(record['repo'], record['tag']) != record['upstream_commit']:
        raise Invalid('upstream tag moved since check')
    assets = {(a['platform'], a['arch']): a['resources'] for a in record['assets']}
    expected = matrix(info)
    if set(assets) != {(p, a) for p, arches in expected.items() for a in arches} or len(assets) != len(record['assets']):
        raise Invalid('plan platform/architecture matrix mismatch')
    versions = {}
    for plat, arches in expected.items():
        values = {}
        for arch, templates in arches.items():
            resources = assets[plat, arch]
            if set(resources) != set(templates):
                raise Invalid('plan URL regions mismatch')
            for region, template in templates.items():
                url = expand(template, candidate, record['tag'])
                if verify_resource(github, url, auto.get('verify') == 'sha256-sidecar') != resources[region]:
                    raise Invalid('artifact changed since check or plan URL was altered')
            if len({r['sha256'] for r in resources.values()}) != 1:
                raise Invalid('mirror content conflict')
            urls = {region: r['url'] for region, r in resources.items()}
            values[arch] = {'url': urls['GLOBAL'] if set(urls) == {'GLOBAL'} else urls,
                            'sha256': next(iter(resources.values()))['sha256']}
        versions[plat] = values['shared'] if set(values) == {'shared'} else values
    text = info['text']
    edits = []
    seen_nodes = set()
    for plat, value in versions.items():
        node = info['platforms'][plat]['node'].definition()
        if node.start in seen_nodes:
            raise Invalid('shared mutable platform tables need an explicit xim policy')
        seen_nodes.add(node.start)
        close = node.end - 1
        line = text.rfind('\n', 0, close) + 1
        if text[line:close].strip():
            position, indent, lead, tail = close, '    ', '\n', '\n'
        else:
            position, indent, lead, tail = line, text[line:close] + '    ', '', ''
        last = max(node.fields.values(), key=lambda n: n.end)
        if not re.match(r'\s*[,;]', text[last.end:close]):
            edits.append((last.end, last.end, ','))
        edits.append((position, position, lead + indent + f'[{literal(candidate)}] = ' + literal(value, indent) + ',\n' + tail))
        if auto['latest'] == 'advance':
            ref = node.fields['latest'].fields['ref']
            edits.append((ref.start, ref.end, literal(candidate)))
    updated = text
    for start, end, replacement in sorted(edits, reverse=True):
        updated = updated[:start] + replacement + updated[end:]
    guard(text, updated)
    parsed = parse(updated).fields['xpm'].fields
    for plat, value in versions.items():
        if parsed[plat].fields[candidate].plain() != value:
            raise Invalid('generated resource does not match verified plan')
    path = root / info['path']
    temporary = path.with_name(path.name + '.xpkgindex-update.tmp')
    try:
        temporary.write_bytes(updated.encode('utf-8'))
        temporary.chmod(path.stat().st_mode & 0o777)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def protected_metadata(text):
    root = parse(text)
    def value(node, path):
        if node.kind != "table":
            return (node.kind, text[node.start:node.end]) if node.kind == "expr" else (node.kind, node.value)
        fields = {}
        for key, child in node.fields.items():
            if path == ("extensions",) and key == "autoupdate":
                continue
            if path == ("ci",) and key == "update":
                continue
            if len(path) == 2 and path[0] == "xpm" and path[1] in PLATFORMS:
                if ordered(key) or key in ("latest", "url_template", "res_versioned"):
                    continue
            item = value(child, (*path, key))
            if path == () and key in ("extensions", "ci") and item == ("table", {}):
                continue
            fields[key] = item
        return "table", fields
    return value(root, ())


def guard(before, after):
    assert_preserved(before, after)
    if protected_metadata(before) != protected_metadata(after):
        raise Invalid("automatic updates must not change dependencies or other runtime metadata")
    old, new = parse(before).fields.get('xpm'), parse(after).fields.get('xpm')
    if not old:
        return
    ext = parse(after).fields.get('extensions')
    auto = ext.fields.get('autoupdate') if ext else None
    for plat in PLATFORMS:
        previous = old.fields.get(plat)
        current = new.fields.get(plat) if new else None
        ref = previous.fields.get('latest') if previous else None
        if ref is None:
            continue
        target = current.fields.get('latest') if current else None
        if target is None:
            raise Invalid('latest was removed')
        if ref.plain() == target.plain():
            continue
        if auto is None or auto.fields.get('latest') is None or auto.fields['latest'].string() != 'advance':
            raise Invalid('latest movement requires the advance policy')
        old_v, new_v = ref.table()['ref'].string(), target.table()['ref'].string()
        if version_key(new_v) <= version_key(old_v) or new_v in previous.fields:
            raise Invalid('latest must advance to a newly appended version')
        if new_v not in current.fields:
            raise Invalid('latest target does not exist')

    moved = any(old.fields.get(p) and old.fields[p].fields.get("latest")
                and new.fields.get(p) and new.fields[p].fields.get("latest")
                and old.fields[p].fields["latest"].plain() != new.fields[p].fields["latest"].plain()
                for p in PLATFORMS)
    if moved:
        targets = [new.fields[p].fields["latest"].fields["ref"].string()
                   for p in PLATFORMS if p in old.fields and "latest" in old.fields[p].fields]
        if len(set(targets)) != 1:
            raise Invalid("latest advancement must cover every platform")
    if parse(before).end and before[parse(before).end:] != after[parse(after).end:]:
        raise Invalid("automatic updates must not change runtime hooks")
