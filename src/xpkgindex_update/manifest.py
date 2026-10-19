"""Version discovery and fixed update plans."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .github import GitHub, WaitingAssets
from .lua_edit import Invalid, Node, parse

PLATFORMS = ("linux", "macosx", "windows")


def version_key(version: str) -> tuple:
    match = re.fullmatch(r"(b?)([0-9]+(?:\.[0-9]+)*)([a-z]?)", version)
    if not match:
        raise Invalid(f"not an ordered release version: {version}")
    parts = [int(x) for x in match[2].split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return match[1], tuple(parts), match[3]


def ordered(version: str) -> bool:
    try:
        version_key(version)
        return True
    except Invalid:
        return False


def expand(template: str, version: str, tag: str) -> str:
    value = template.replace("${version}", version).replace("${tag}", tag)
    if "${" in value:
        raise Invalid(f"unknown URL placeholder: {template}")
    return value


def tag_version(template: str, tag: str) -> str | None:
    slot = "${version:dash}" if "${version:dash}" in template else "${version}"
    if template.count(slot) != 1 or "${" in template.replace(slot, ""):
        raise Invalid("tag must have exactly one ${version} or ${version:dash}")
    left, right = template.split(slot)
    grammar = r"([0-9]+(?:-[0-9]+)*)" if slot.endswith(":dash}") else r"(b?[0-9]+(?:\.[0-9]+)*[a-z]?)"
    match = re.fullmatch(re.escape(left) + grammar + re.escape(right), tag)
    if not match:
        return None
    return match[1].replace("-", ".") if slot.endswith(":dash}") else match[1]


def package_info(path: Path, root: Path) -> dict:
    root = root.resolve()
    path = path.resolve()
    if not path.is_relative_to(root / "pkgs"):
        raise Invalid("descriptor is outside pkgs")
    text = path.read_bytes().decode("utf-8")
    pkg = parse(text).table()
    namespace = pkg.get("namespace")
    identity = (namespace.string() if namespace else "mcpplibs") + ":" + pkg["name"].string()
    extension = pkg.get("extensions")
    auto = extension.table().get("autoupdate") if extension else None
    platforms = {}
    source = pkg["xpm"].table().get("source")
    for plat in PLATFORMS:
        node = pkg["xpm"].table().get(plat)
        if node:
            entries = {k: v for k, v in node.table().items() if not k.startswith("#") and k not in
                       ("latest", "source", "url_template", "res_versioned", "deps", "inherits")}
            # Only table entries with a URL/hash or historical sentinels are versions.
            entries = {k: v for k, v in entries.items() if v.kind == "table" and
                       ("url" in v.fields or "sha256" in v.fields) or v.kind == "string"}
            if entries:
                platforms[plat] = {"node": node, "entries": entries,
                                   "source": node.fields.get("source", source)}
    direct = []
    for plat, info in platforms.items():
        for version, entry in info["entries"].items():
            url = entry.fields.get("url") if entry.kind == "table" else None
            if url and url.kind == "string" and url.string().startswith("https://github.com/"):
                direct.append({"platform": plat, "version": version, "url": url.string()})
    return {"id": identity, "path": path.relative_to(root).as_posix(), "text": text,
            "package": pkg, "platforms": platforms, "direct": direct,
            "autoupdate": auto.plain() if auto else None}


def url_for(config: dict, plat: str):
    urls = config["url"]
    if isinstance(urls, str) or isinstance(urls, dict) and set(urls) == {"GLOBAL", "CN"}:
        return urls
    if isinstance(urls, dict) and set(urls) <= set(PLATFORMS) and plat in urls:
        return urls[plat]
    raise Invalid(f"incomplete or mixed URL mapping for {plat}")


def validate_config(info: dict) -> None:
    config = info["autoupdate"]
    if not isinstance(config, dict) or set(config) != {"tag", "url"}:
        raise Invalid("autoupdate must contain exactly tag and url")
    if not isinstance(config["tag"], str):
        raise Invalid("tag must be a string")
    tag_version(config["tag"], "")
    if not info["platforms"]:
        raise Invalid("no versioned platforms")
    urls = config["url"]
    if isinstance(urls, dict) and set(urls) <= set(PLATFORMS) and set(urls) != set(info["platforms"]):
        raise Invalid("URL platforms must equal the versioned platform set")
    for plat in info["platforms"]:
        spec = url_for(config, plat)
        if isinstance(spec, dict):
            if set(spec) != {"GLOBAL", "CN"}:
                raise Invalid("mirror requires GLOBAL and CN")
            values = spec.values()
        else:
            values = [spec]
        for value in values:
            if not isinstance(value, str) or not value.startswith("https://"):
                raise Invalid("URL template must use HTTPS")
            expand(value, "0.0", "v0.0")


def repo_for(info: dict) -> str:
    # Explicit download templates identify forks/mirrors more precisely than homepage repo.
    repos = set()
    for plat in info["platforms"]:
        spec = url_for(info["autoupdate"], plat)
        url = spec["GLOBAL"] if isinstance(spec, dict) else spec
        match = re.match(r"https://github\.com/([^/]+/[^/]+)/", url)
        if match:
            repos.add(match[1])
    if len(repos) > 1:
        raise Invalid("platforms refer to different GitHub repositories")
    if repos:
        return repos.pop()
    repo = info["package"].get("repo")
    match = re.fullmatch(r"https://github\.com/([^/]+/[^/]+?)(?:\.git)?/?", repo.string() if repo else "")
    if not match:
        raise Invalid("no GitHub upstream repository")
    return match[1]


def check_package(info: dict, github: GitHub) -> dict:
    record = {"id": info["id"], "path": info["path"], "status": "error"}
    try:
        validate_config(info)
        sets = [set(p["entries"]) for p in info["platforms"].values()]
        if any(s != sets[0] for s in sets):
            raise Invalid("existing platform versions diverge; no automatic repair")
        versions = sets[0]
        numeric = [v for v in versions if ordered(v)]
        record["existing_versions"] = sorted(versions)
        if not numeric:
            record.update(status="pinned-snapshot", reason="commit snapshot has no release ordering; never follows a branch")
            return record
        current = max(numeric, key=version_key)
        record["current"] = current
        current_urls = []
        for platform in info["platforms"].values():
            entry = platform["entries"][current]
            url = entry.fields.get("url")
            if url and url.kind == "string":
                current_urls.append(url.string())
            elif url and url.kind == "table" and "GLOBAL" in url.fields:
                current_urls.append(url.fields["GLOBAL"].string())
        if current_urls and all(re.search(r"/archive/[0-9a-f]{40}\.tar", u) for u in current_urls):
            record.update(status="pinned-snapshot", reason="current payload is a commit snapshot; never follows a branch")
            return record
        # A package's own release counter may differ from its vendored source version.
        for plat, platform in info["platforms"].items():
            entry = platform["entries"][current]
            url = entry.fields.get("url")
            actual = url.string() if url and url.kind == "string" else (
                url.fields["GLOBAL"].string() if url and "GLOBAL" in url.fields else None)
            if actual:
                spec = url_for(info["autoupdate"], plat)
                template = spec["GLOBAL"] if isinstance(spec, dict) else spec
                tag_template = info["autoupdate"]["tag"]
                expected_tag = tag_template.replace("${version:dash}", current.replace(".", "-")).replace("${version}", current)
                if expand(template, current, expected_tag) != actual:
                    record.update(status="pinned-version-map", reason="index version does not map to current source URL; explicit release policy required")
                    return record
        repo = repo_for(info)
        record["repo"] = repo
        releases = github.listing(repo, "releases")
        candidates = {}
        current_dates = [r.get("published_at", "") for r in releases
                         if tag_version(info["autoupdate"]["tag"], r["tag_name"]) == current]
        current_date = max(current_dates, default="")
        for release in releases:
            if release.get("draft") or release.get("prerelease"):
                continue
            # Some projects changed numbering schemes (SafeInt 3.24 -> 3.0.28a).
            # A larger-looking historical spelling must not propose older source.
            if current_date and release.get("published_at", "") < current_date:
                continue
            tag = release["tag_name"]
            version = tag_version(info["autoupdate"]["tag"], tag)
            if version:
                candidates[tag] = version
        record["discovery"] = "releases"
        if not candidates:
            record["discovery"] = "tags"
            # Don't re-admit release tags explicitly marked as draft/prerelease.
            excluded = {r["tag_name"] for r in releases if r.get("draft") or r.get("prerelease")}
            for tag in github.listing(repo, "tags"):
                version = tag_version(info["autoupdate"]["tag"], tag["name"])
                if version and tag["name"] not in excluded:
                    candidates[tag["name"]] = version
        if not candidates:
            raise Invalid("no matching stable release or numeric tag")
        matching = {tag: v for tag, v in candidates.items() if version_key(v)[0] == version_key(current)[0]}
        ahead = {t: v for t, v in matching.items() if version_key(v) > version_key(current)}
        if not ahead:
            record["status"] = "up-to-date"
            return record
        tag = max(ahead, key=lambda t: version_key(ahead[t]))
        candidate = ahead[tag]
        if sum(version_key(v) == version_key(candidate) for v in ahead.values()) != 1:
            raise Invalid("ambiguous version spellings or tags")
        commit = github.commit(repo, tag)
        record.update(candidate=candidate, tag=tag, upstream_commit=commit,
                      descriptor_sha256=hashlib.sha256(info["text"].encode()).hexdigest())
        assets = []
        for plat in info["platforms"]:
            spec = url_for(info["autoupdate"], plat)
            urls = spec if isinstance(spec, dict) else {"GLOBAL": spec}
            checks = {region: github.artifact(expand(url, candidate, tag)) for region, url in urls.items()}
            if len({c["sha256"] for c in checks.values()}) != 1:
                raise Invalid("GLOBAL/CN hashes differ")
            assets.append({"platform": plat, "resources": checks})
        record.update(status="update-ready", assets=assets)
    except WaitingAssets as exc:
        record.update(status="waiting-assets", reason=str(exc))
    except (Invalid, OSError, ValueError, KeyError) as exc:
        record.update(status="error", reason=str(exc))
    return record


def inventory(root: Path) -> list[dict]:
    return [package_info(p, root) for p in sorted((root / "pkgs").glob("*/*.lua"))]


def version_blocks(text: str) -> dict[str, dict[str, str]]:
    pkg = parse(text).table()
    out = {}
    for plat in PLATFORMS:
        node: Node | None = pkg["xpm"].fields.get(plat)
        if node:
            out[plat] = {key: text[value.start:value.end] for key, value in node.fields.items()
                         if ordered(key) or re.match(r"^(?:\d|b\d)", key)}
    return out


def assert_preserved(before: str, after: str) -> None:
    old = version_blocks(before)
    new = version_blocks(after)
    for plat, entries in old.items():
        if not set(entries) <= set(new.get(plat, {})):
            raise Invalid("existing version was removed")
        if list(entries) != [k for k in new[plat] if k in entries]:
            raise Invalid("existing version order changed")
        for version, raw in entries.items():
            if new[plat][version] != raw:
                raise Invalid(f"existing version changed: {plat}/{version}")
