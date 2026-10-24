"""Bounded GitHub discovery and public HTTPS artifact verification."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from .lua_edit import Invalid


class BudgetExceeded(Invalid):
    pass


class WaitingAssets(Exception):
    pass


def public_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise Invalid("resources must be public HTTPS URLs without credentials")
    if parsed.port not in (None, 443):
        raise Invalid("nonstandard resource port")
    for address in socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM):
        if not ipaddress.ip_address(address[4][0]).is_global:
            raise Invalid("private resource address")
    return url


class PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class GitHub:
    def __init__(self, token: str | None = None, *, request_budget=500, resource_bytes=512 * 1024**2, download_bytes=4 * 1024**3):
        self.token = token or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        self.request_budget = request_budget
        self.resource_bytes = resource_bytes
        self.download_bytes = download_bytes
        self.cache = {}
        self.digests = {}
        self.requests = 0
        self.downloaded = 0

    def charge_request(self):
        if self.requests >= self.request_budget:
            raise BudgetExceeded("run request budget exhausted")
        self.requests += 1

    def api(self, path: str):
        if path in self.cache:
            return self.cache[path]
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "xpkgindex-update"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request("https://api.github.com/" + path, headers=headers)
        for attempt in range(3):
            self.charge_request()
            try:
                with urllib.request.urlopen(req, timeout=45) as response:
                    result = json.load(response)
                self.cache[path] = result
                return result
            except urllib.error.HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise
                time.sleep(2 ** attempt)
            except (urllib.error.URLError, TimeoutError):
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)

    def listing(self, repo: str, kind: str) -> list[dict]:
        result = []
        for page in range(1, self.request_budget + 1):
            batch = self.api(f"repos/{repo}/{kind}?per_page=100&page={page}")
            result.extend(batch)
            if len(batch) < 100:
                return result
        raise BudgetExceeded(f"discovery-incomplete: {repo}/{kind} exceeded the request budget")

    def commit(self, repo: str, tag: str) -> str:
        return self.api(f"repos/{repo}/commits/{urllib.parse.quote(tag, safe='')}")["sha"]

    def artifact(self, url: str) -> dict:
        for attempt in range(3):
            try:
                return self._artifact_once(url)
            except urllib.error.HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise
            except (urllib.error.URLError, TimeoutError, socket.gaierror):
                if attempt == 2:
                    raise
            time.sleep(2 ** attempt)
        raise Invalid("artifact retry limit exceeded")

    def _artifact_once(self, url: str) -> dict:
        if url in self.digests:
            return self.digests[url]
        if self.downloaded >= self.download_bytes:
            raise BudgetExceeded("run download budget exhausted")
        self.charge_request()
        public_url(url)
        request = urllib.request.Request(url, headers={"User-Agent": "xpkgindex-update"})
        digest = hashlib.sha256()
        size = 0
        opener = urllib.request.build_opener(PublicRedirect())
        try:
            with opener.open(request, timeout=180) as response:
                final_url = response.geturl() if hasattr(response, "geturl") else url
                head = b""
                while chunk := response.read(1024 * 1024):
                    if not head:
                        head = chunk[:512]
                    size += len(chunk)
                    self.downloaded += len(chunk)
                    if self.downloaded > self.download_bytes:
                        raise BudgetExceeded("run download budget exhausted")
                    if size > self.resource_bytes:
                        raise Invalid("policy-required: resource exceeds per-resource byte limit")
                    digest.update(chunk)
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 410):
                raise WaitingAssets(url) from exc
            raise
        if not size or head.lstrip().lower().startswith((b"<!doctype html", b"<html")):
            raise Invalid(f"empty or HTML artifact: {url}")
        if url.endswith((".tar.gz", ".tgz")) and not head.startswith(b"\x1f\x8b"):
            raise Invalid(f"not a gzip archive: {url}")
        if url.endswith(".tar.xz") and not head.startswith(b"\xfd7zXZ\x00"):
            raise Invalid(f"not an XZ archive: {url}")
        if url.endswith(".tar.bz2") and not head.startswith(b"BZh"):
            raise Invalid(f"not a bzip2 archive: {url}")
        if url.endswith(".zip") and not head.startswith(b"PK"):
            raise Invalid(f"not a ZIP archive: {url}")
        if url.endswith(".deb") and not head.startswith(b"!<arch>\n"):
            raise Invalid(f"not a Debian archive: {url}")
        result = {"url": url, "final_host": urllib.parse.urlsplit(final_url).hostname, "size": size, "sha256": digest.hexdigest()}
        self.digests[url] = result
        return result

    def text(self, url: str, *, max_bytes=65536) -> str:
        """Fetch bounded public metadata, never send API credentials."""
        public_url(url)
        request = urllib.request.Request(url, headers={"User-Agent": "xpkgindex-update"})
        for attempt in range(3):
            self.charge_request()
            try:
                with urllib.request.build_opener(PublicRedirect()).open(request, timeout=45) as response:
                    payload = response.read(max_bytes + 1)
                self.downloaded += len(payload)
                if len(payload) > max_bytes:
                    raise Invalid(f"public metadata exceeds {max_bytes} bytes")
                if self.downloaded > self.download_bytes:
                    raise BudgetExceeded("run download budget exhausted")
                return payload.decode("utf-8")
            except urllib.error.HTTPError as exc:
                if exc.code in (404, 410):
                    raise WaitingAssets(url) from exc
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise
            except (urllib.error.URLError, TimeoutError):
                if attempt == 2:
                    raise
            time.sleep(2 ** attempt)
        raise Invalid("sidecar retry limit exceeded")
