"""Append explicit platform entries without rewriting existing source."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from ..github import GitHub
from ..lua_edit import Invalid
from ..manifest import (
    assert_preserved,
    expand,
    package_info,
    repo_for,
    tag_version,
    url_for,
    validate_config,
    version_key,
)


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
    if hashlib.sha256(text.encode()).hexdigest() != record["descriptor_sha256"]:
        raise Invalid("descriptor changed since check; regenerate plan")
    asset_map = {a["platform"]: a["resources"] for a in record["assets"]}
    if len(asset_map) != len(record["assets"]) or set(asset_map) != set(info["platforms"]):
        raise Invalid("plan platform matrix mismatch")
    if github.commit(record["repo"], record["tag"]) != record["upstream_commit"]:
        raise Invalid("upstream tag moved since check")
    edits = []
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
        node = platform["node"]
        close = node.end - 1
        line = text.rfind("\n", 0, close) + 1
        if text[line:close].strip():
            position, indent = close, "    "
            lead = "\n"
            tail = "\n"
        else:
            position = line
            indent = text[line:close] + "    "
            lead = tail = ""
        last = max(node.fields.values(), key=lambda n: n.end)
        between = text[last.end:close]
        # Separators belong to the platform table, not to the immutable old value.
        if not re.match(r"\s*[,;]", between):
            edits.append((last.end, ","))
        def quote(value):
            return json.dumps(value, ensure_ascii=True)
        if isinstance(spec, dict):
            url_line = "{ " + ", ".join(f"{r} = {quote(a['url'])}" for r, a in resources.items()) + " }"
        else:
            url_line = quote(resources["GLOBAL"]["url"])
        block = (f"{lead}{indent}[{quote(candidate)}] = {{\n"
                 f"{indent}    url = {url_line},\n"
                 f"{indent}    sha256 = {quote(next(iter(hashes)))},\n"
                 f"{indent}}},\n{tail}")
        edits.append((position, block))
    updated = text
    for position, value in sorted(edits, reverse=True):
        updated = updated[:position] + value + updated[position:]
    assert_preserved(text, updated)
    # Atomic replacement only after every platform and download has been checked.
    temporary = path.with_name(path.name + ".xpkgindex-update.tmp")
    try:
        temporary.write_bytes(updated.encode("utf-8"))
        temporary.chmod(path.stat().st_mode & 0o777)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return True
