"""Append explicit platform entries without rewriting existing source."""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..github import GitHub
from ..lua_edit import Invalid, parse, tokens
from ..manifest import (
    assert_preserved,
    descriptor_digest,
    expand,
    package_info,
    repo_for,
    tag_version,
    url_for,
    validate_config,
    version_key,
)


def ordered_resource(key, node):
    try:
        version_key(key)
        return node.kind == "table" and set(node.fields) == {"url", "sha256"}
    except Invalid:
        return False


def append(root: Path, record: dict, github: GitHub) -> bool:
    root = root.resolve()
    if record.get("status") != "update-ready":
        raise Invalid("only update-ready records can be applied")
    original = root / record["path"]
    path = original.resolve()
    if not path.is_relative_to((root / "pkgs").resolve()) or original.is_symlink():
        raise Invalid("descriptor must be a regular file inside pkgs")
    info = package_info(path, root)
    validate_config(info)
    if info["id"] != record["id"] or repo_for(info) != record["repo"]:
        raise Invalid("plan package identity/repository mismatch")
    text = info["text"]
    candidate = record["candidate"]
    if tag_version(info["autoupdate"]["tag"], record["tag"]) != candidate:
        raise Invalid("plan tag/version mismatch")
    present = [candidate in p["entries"] for p in info["platforms"].values()]
    if any(present):
        if all(present):
            return False  # Existing versions are never rewritten or refreshed.
        raise Invalid("existing-version-conflict")
    if descriptor_digest(text) != record["descriptor_sha256"]:
        raise Invalid("descriptor changed since check; regenerate plan")
    asset_map = {a["platform"]: a["resources"] for a in record["assets"]}
    if len(asset_map) != len(record["assets"]) or set(asset_map) != set(info["platforms"]):
        raise Invalid("plan platform matrix mismatch")
    if github.commit(record["repo"], record["tag"]) != record["upstream_commit"]:
        raise Invalid("upstream tag moved since check")
    validated = []
    for plat, platform in info["platforms"].items():
        if "latest" in platform["node"].fields:
            raise Invalid("latest.ref update policy is not enabled; version blocks remain append-only")
        for existing in platform["entries"]:
            if version_key(existing)[0] != version_key(candidate)[0] or version_key(existing) >= version_key(candidate):
                raise Invalid("candidate is not newer than every existing version")
        spec = url_for(info["autoupdate"], plat)
        templates = spec if isinstance(spec, dict) else {"GLOBAL": spec}
        resources = asset_map[plat]
        if set(templates) != set(resources):
            raise Invalid("plan URL regions mismatch")
        for region, template in templates.items():
            expected_url = expand(template, candidate, record["tag"])
            if resources[region]["url"] != expected_url:
                raise Invalid("plan URL differs from declaration")
            if github.artifact(expected_url) != resources[region]:
                raise Invalid("artifact changed since check")
        hashes = {r["sha256"] for r in resources.values()}
        if len(hashes) != 1:
            raise Invalid("mirror content conflict")
        validated.append((platform["node"].definition(), resources))

    edits = []

    def quote(value):
        return json.dumps(value, ensure_ascii=True)

    def insert(node, value):
        close = node.end - 1
        line = text.rfind("\n", 0, close) + 1
        if text[line:close].strip():
            position, indent, lead, tail = close, "    ", "\n", "\n"
        else:
            position, indent, lead, tail = line, text[line:close] + "    ", "", ""
        if node.fields:
            last = max(node.fields.values(), key=lambda n: n.end)
            if not re.match(r"\s*[,;]", text[last.end:close]):
                edits.append((last.end, ","))
        edits.append((position, lead + value(indent) + "\n" + tail))

    def resource_value(resources, indent):
        urls = {region: resource["url"] for region, resource in resources.items()}
        url = quote(urls["GLOBAL"]) if set(urls) == {"GLOBAL"} else (
            "{ " + ", ".join(f"{key} = {quote(value)}" for key, value in urls.items()) + " }")
        digest = next(iter(resources.values()))["sha256"]
        return (f"{{\n{indent}    url = {url},\n"
                f"{indent}    sha256 = {quote(digest)},\n{indent}}}")

    # A shared whole-platform table is edited once. Different assets cannot
    # be represented by that table, so refuse instead of changing old aliases.
    unique = {}
    for node, resources in validated:
        if node.start in unique and unique[node.start][1] != resources:
            raise Invalid("shared platform table requires identical resources")
        unique[node.start] = (node, resources)
    same = len(validated) > 1 and all(resources == validated[0][1] for _, resources in validated)
    if same and len(unique) > 1:
        # Reuse an existing resource-only local table already referenced by a
        # version entry. Otherwise add one without rewriting historical rows.
        shared = None
        for name, table in info["bindings"].items():
            if (table.fields and all(ordered_resource(key, value) for key, value in table.fields.items())
                    and any(entry.reference is value for value in table.fields.values()
                            for platform in info["platforms"].values()
                            for entry in platform["entries"].values())):
                shared = (name, table)
                break
        resources = validated[0][1]
        if shared:
            name, table = shared
            if candidate in table.fields:
                raise Invalid("candidate already exists in the shared resource table")
            insert(table, lambda indent: f"{indent}[{quote(candidate)}] = "
                   + resource_value(resources, indent) + ",")
        else:
            names = {token.value for token in tokens(text) if token.kind == "word"}
            name = "versions"
            while name in names:
                name = "xpkg_" + name
            definition = (f"local {name} = {{\n    [{quote(candidate)}] = "
                          + resource_value(resources, "    ") + ",\n}\n\n")
            edits.append((0, definition))
        for node, _ in unique.values():
            insert(node, lambda indent: f"{indent}[{quote(candidate)}] = {name}[{quote(candidate)}],")
    else:
        for node, resources in unique.values():
            insert(node, lambda indent, resources=resources:
                   f"{indent}[{quote(candidate)}] = " + resource_value(resources, indent) + ",")
    updated = text
    for position, value in sorted(edits, reverse=True):
        updated = updated[:position] + value + updated[position:]
    assert_preserved(text, updated)
    platforms = parse(updated).table()["xpm"].table()
    for plat, resources in asset_map.items():
        urls = {key: resource["url"] for key, resource in resources.items()}
        expected = {"url": urls["GLOBAL"] if set(urls) == {"GLOBAL"} else urls,
                    "sha256": next(iter(resources.values()))["sha256"]}
        entry = platforms[plat].table().get(candidate)
        if entry is None or entry.plain() != expected:
            raise Invalid("generated version does not resolve to the verified resource")
    # Atomic replacement only after every platform and download has been checked.
    temporary = path.with_name(path.name + ".xpkgindex-update.tmp")
    try:
        temporary.write_bytes(updated.encode("utf-8"))
        temporary.chmod(path.stat().st_mode & 0o777)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return True
