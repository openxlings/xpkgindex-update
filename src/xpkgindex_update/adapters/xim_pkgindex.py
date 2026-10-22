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
)
from ..manifest import (
    version_key as release_key,
)

FORMAT = 2
ARCHES = {'x86_64', 'aarch64', 'x86', 'armv7', 'riscv64'}


def version_key(version):
    # A numeric upstream build is part of the index identity (e.g. JDK +7).
    build = version.partition('+')[2]
    return (*release_key(version), int(build) if build.isdigit() else -1)


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
    if isinstance(value, dict) and value and set(value) <= {'GLOBAL', 'CN'}:
        return value
    raise Invalid('resource must be an HTTPS URL or a region map')


def config(info):
    """Normalize the public URL-based declaration; no legacy xim translation."""
    auto = info['autoupdate']
    if not isinstance(auto, dict) or not {'source', 'resources'} <= set(auto) or set(auto) - {'source', 'resources', 'tag', 'latest', 'verify'}:
        raise Invalid('xim autoupdate requires source and resources; optional tag, latest, verify')
    source = auto['source']
    match = re.fullmatch(r'https://github\.com/([\w.-]+/[\w.-]+)/(releases|tags)/?', source if isinstance(source, str) else '')
    if not match:
        raise Invalid('unsupported-source: no version parser for this source URL')
    targets = auto['resources']
    if not isinstance(targets, dict) or not targets or set(targets) - {'GLOBAL', 'CN'}:
        raise Invalid('resources must declare GLOBAL and/or CN')
    normalized = {}
    for region, value in targets.items():
        value = {'url': value} if isinstance(value, str) else value
        if not isinstance(value, dict) or 'url' not in value or set(value) - {'url', 'required'}:
            raise Invalid('resource target requires url; optional required')
        required = value.get('required', True)
        if not isinstance(required, bool):
            raise Invalid('required must be boolean')
        normalized[region] = {**value, 'required': required}
    if not any(v['required'] for v in normalized.values()):
        raise Invalid('at least one resource target must be required')
    return {**auto, 'upstream': match[1], 'discovery': match[2], 'latest': auto.get('latest', 'advance'), 'targets': normalized}


def release_version(auto, tag):
    if 'tag' in auto:
        return tag_version(auto['tag'], tag)
    return tag_version('v${version}' if tag.startswith('v') else '${version}', tag)


def matrix(info):
    result = {}
    for region, target in config(info)['targets'].items():
        urls = target['url']
        if isinstance(urls, dict) and set(urls) <= set(PLATFORMS):
            if set(urls) != set(info['platforms']):
                raise Invalid('URL platforms must equal the versioned platform set')
        else:
            urls = dict.fromkeys(info['platforms'], urls)
        for plat, value in urls.items():
            arches = value if isinstance(value, dict) and set(value) <= ARCHES else {'shared': value}
            if not arches:
                raise Invalid('empty architecture matrix')
            for arch, url in arches.items():
                if not isinstance(url, str):
                    raise Invalid('resource leaf must be an HTTPS URL template')
                result.setdefault(plat, {}).setdefault(arch, {})[region] = url
    for arches in result.values():
        if any(set(regions) != set(config(info)['targets']) for regions in arches.values()):
            raise Invalid('resource targets must declare identical platform/architecture matrices')
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
    auto = config(info)
    if auto['latest'] not in ('advance', 'keep'):
        raise Invalid('latest policy must be advance or keep')
    if auto.get('verify') not in (None, 'sha256-sidecar'):
        raise Invalid('unsupported verification policy')
    if 'tag' in auto:
        if not isinstance(auto['tag'], str):
            raise Invalid('tag must be a string')
        tag_version(auto['tag'], '')
    if not info['platforms']:
        raise Invalid('no versioned platforms')
    refs = latest_refs(info)
    for plat, arches in matrix(info).items():
        entries = info['platforms'][plat]['entries']
        newest = max((v for v in entries if ordered(v)), key=version_key)
        expected = entry_arches(entries[newest]) or entry_arches(entries[refs[plat]])
        if expected and set(arches) != expected:
            raise Invalid(f'architecture set changed for {plat}: {sorted(expected)} -> {sorted(arches)}')
        if not expected and len(arches) > 1:
            baseline = info['platforms'][plat]['entries'][refs[plat]]
            declared = info['package'].get('archs')
            declared = {n.string() for n in declared.fields.values()} if declared else set()
            declared = {'aarch64' if a == 'arm64' else a for a in declared}
            if baseline.kind != 'expr' or set(arches) != declared:
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
        auto = config(info)
        refs = latest_refs(info)
        current = next(iter(refs.values()))
        existing = set().union(*(p['entries'] for p in info['platforms'].values()))
        if any(not ordered(v) for v in existing):
            raise Invalid('policy-required: historical version has no release ordering')
        floor = max(existing, key=version_key)
        record.update(current=current, repo=auto['upstream'], latest_policy=auto['latest'],
                      latest_before=refs, descriptor_sha256=descriptor_digest(info['text']))
        releases = github.listing(auto['upstream'], 'releases') if auto['discovery'] == 'releases' else []
        dates = [r.get('published_at', '') for r in releases if release_version(auto, r['tag_name']) == current]
        current_date = max(dates, default='')
        excluded = {r['tag_name'] for r in releases if r.get('draft') or r.get('prerelease')}
        candidates = {}
        for r in releases:
            if r['tag_name'] in excluded or current_date and r.get('published_at', '') < current_date:
                continue
            v = release_version(auto, r['tag_name'])
            if v:
                candidates[r['tag_name']] = v
        record['discovery'] = 'releases'
        if not candidates:
            record['discovery'] = 'tags'
            for t in github.listing(auto['upstream'], 'tags'):
                v = release_version(auto, t['name'])
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
        # The first complete candidate is the newest true intersection.
        record['attempts'] = []
        for tag in sorted(ahead, key=lambda t: version_key(ahead[t]), reverse=True):
            candidate = ahead[tag]
            if sum(version_key(v) == version_key(candidate) for v in ahead.values()) != 1:
                raise Invalid('ambiguous version spellings or tags')
            try:
                assets, unavailable = collect_assets(info, github, candidate, tag)
            except WaitingAssets as exc:
                record['attempts'].append({'version': candidate, 'reason': str(exc)})
                continue
            record.update(candidate=candidate, tag=tag, upstream_commit=github.commit(auto['upstream'], tag),
                          latest_after=candidate if auto['latest'] == 'advance' else current,
                          status='update-ready', assets=assets, optional_unavailable=unavailable)
            return record
        record.update(status='waiting-assets', reason='no newer version has all required resources')
    except WaitingAssets as exc:
        record.update(status='waiting-assets', reason=str(exc))
    except BudgetExceeded as exc:
        record.update(status='budget-deferred', reason=str(exc))
    except (Invalid, OSError, ValueError, KeyError) as exc:
        reason = str(exc)
        status = next((s for s in ('policy-required', 'existing-version-conflict') if reason.startswith(s)), 'error')
        record.update(status=status, reason=reason)
    return record


def collect_assets(info, github, version, tag):
    """A target covers the complete matrix, or is omitted in its entirety."""
    auto, expected = config(info), matrix(info)
    checked, unavailable = {}, {}
    for region, target in auto['targets'].items():
        resources = {}
        try:
            for plat, arches in expected.items():
                for arch, templates in arches.items():
                    url = expand(templates[region], version, tag)
                    resource = verify_resource(github, url, auto.get('verify') == 'sha256-sidecar')
                    final_host = urlsplit(resource.get('final_url', url)).hostname or ''
                    if region == 'CN' and (final_host == 'github.com' or final_host.endswith('.githubusercontent.com')):
                        raise WaitingAssets('CN mirror redirects to GitHub; mirror asset is not available')
                    resources[plat, arch] = resource
        except BudgetExceeded:
            raise
        except (WaitingAssets, Invalid, OSError, ValueError) as exc:
            if target['required']:
                raise
            unavailable[region] = str(exc)
            continue
        checked[region] = resources
    assets = []
    for plat, arches in expected.items():
        for arch in arches:
            resources = {region: checks[plat, arch] for region, checks in checked.items()}
            required_hashes = {r['sha256'] for region, r in resources.items() if auto['targets'][region]['required']}
            if len(required_hashes) != 1:
                raise Invalid('required resource hashes differ')
            for region in list(resources):
                if resources[region]['sha256'] not in required_hashes:
                    unavailable[region] = 'optional mirror content differs from required resource'
            assets.append({'platform': plat, 'arch': arch, 'resources': resources})
    # Omit an optional target from all cells if any of its resources disagree.
    for asset in assets:
        asset['resources'] = {r: v for r, v in asset['resources'].items() if r not in unavailable}
    return assets, unavailable


def candidate_tag(info, version, assets):
    auto, expected = config(info), matrix(info)
    if 'tag' in auto:
        choices = [auto['tag'].replace('${version:dash}', version.replace('.', '-')).replace('${version:underscore}', version.replace('.', '_')).replace('${version}', version)]
    else:
        choices = [version, 'v' + version]
    matches = [tag for tag in choices if all(
        resource['url'] == expand(expected[a['platform']][a['arch']][region], version, tag)
        for a in assets for region, resource in a['resources'].items())]
    if not matches:
        raise Invalid('candidate URLs differ from the declared matrix')
    # When no URL uses ${tag}, either spelling yields the same artifacts. Resolve
    # against the upstream commit in append; candidate verification checks bytes.
    return matches[0]


def verify_candidates(info, before, github):
    """Verify only versions added in this diff; never discover future releases."""
    guard(before.replace('\r\n', '\n'), info['text'].replace('\r\n', '\n'))
    validate_config(info)
    old = parse(before).fields['xpm'].fields
    additions = {p: set(data['entries']) - set(old[p].fields) for p, data in info['platforms'].items()}
    versions = set().union(*additions.values())
    if any(added != versions for added in additions.values()):
        raise Invalid('candidate additions must cover every platform')
    auto, expected = config(info), matrix(info)
    reports = []
    for version in sorted(versions, key=version_key):
        assets = []
        for plat, arches in expected.items():
            entry = info['platforms'][plat]['entries'][version].plain()
            if set(arches) != {'shared'} and set(entry) != set(arches):
                raise Invalid('candidate resource architecture matrix differs from declaration')
            for arch in arches:
                resource = entry if arch == 'shared' else entry[arch]
                if set(resource) != {'url', 'sha256'}:
                    raise Invalid('candidate must contain explicit URL and SHA256 resources')
                resources = {region: {'url': url, 'sha256': resource['sha256']}
                             for region, url in regions(resource['url']).items()}
                if set(resources) - set(auto['targets']) or any(t['required'] and r not in resources for r, t in auto['targets'].items()):
                    raise Invalid('candidate omits a required resource or adds an undeclared region')
                assets.append({'platform': plat, 'arch': arch, 'resources': resources})
        if len({tuple(sorted(a['resources'])) for a in assets}) != 1:
            raise Invalid('candidate optional targets must cover the full matrix')
        tag = candidate_tag(info, version, assets)
        for asset in assets:
            for region, resource in asset['resources'].items():
                checked = verify_resource(github, resource['url'], auto.get('verify') == 'sha256-sidecar')
                if region == 'CN' and urlsplit(checked.get('final_url', resource['url'])).hostname in ('github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'):
                    raise Invalid('CN mirror redirects to GitHub')
                if checked['sha256'] != resource['sha256']:
                    raise Invalid('candidate resource SHA256 mismatch')
        reports.append({'id': info['id'], 'candidate': version, 'status': 'verified', 'tag': tag, 'assets': assets})
    return reports


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
    auto = config(info)
    if info['id'] != record['id'] or auto['upstream'] != record['repo']:
        raise Invalid('plan identity/repository mismatch')
    if descriptor_digest(info['text']) != record['descriptor_sha256']:
        raise Invalid('descriptor changed since check; regenerate plan')
    refs = latest_refs(info)
    if refs != record['latest_before'] or auto['latest'] != record['latest_policy']:
        raise Invalid('plan latest policy/baseline mismatch')
    candidate = record['candidate']
    if release_version(auto, record['tag']) != candidate:
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
    if len({tuple(sorted(resources)) for resources in assets.values()}) != 1:
        raise Invalid('plan optional targets must cover the full matrix')
    versions = {}
    for plat, arches in expected.items():
        values = {}
        for arch, templates in arches.items():
            resources = assets[plat, arch]
            if set(resources) - set(templates) or any(t['required'] and r not in resources for r, t in auto['targets'].items()):
                raise Invalid('plan URL regions mismatch')
            for region in resources:
                template = templates[region]
                url = expand(template, candidate, record['tag'])
                checked = verify_resource(github, url, auto.get('verify') == 'sha256-sidecar')
                if region == 'CN' and (urlsplit(checked.get('final_url', url)).hostname or '').endswith(('github.com', 'githubusercontent.com')):
                    raise Invalid('CN mirror redirects to GitHub')
                if checked != resources[region]:
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
        if auto is None or auto.plain().get('latest', 'advance') != 'advance':
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
