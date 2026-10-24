import io
import socket
import urllib.error

import pytest

from xpkgindex_update.github import BudgetExceeded, GitHub, WaitingAssets, public_url
from xpkgindex_update.lua_edit import Invalid


@pytest.mark.parametrize("url", ["http://example.org/a", "https://user:password@example.org/a",
                                 "https://example.org:8443/a"])
def test_unsafe_urls_rejected(url):
    with pytest.raises(Invalid):
        public_url(url)


def test_private_dns_rejected(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(Invalid, match="private"):
        public_url("https://private.example/file")


def test_resources_are_cached_and_never_receive_api_token(monkeypatch):
    requests = []

    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            return io.BytesIO(b"PKfixture")

    monkeypatch.setattr("xpkgindex_update.github.public_url", lambda u: u)
    monkeypatch.setattr("urllib.request.build_opener", lambda *a: Opener())
    client = GitHub("secret-api-token")
    first = client.artifact("https://example.org/resource.zip")
    assert client.artifact("https://example.org/resource.zip") == first
    assert len(requests) == 1
    assert not requests[0].has_header("Authorization")


def test_artifact_dns_failure_is_retried(monkeypatch):
    client = GitHub()
    calls = []

    def once(url):
        calls.append(url)
        if len(calls) == 1:
            raise socket.gaierror("temporary DNS failure")
        return {"url": url}

    monkeypatch.setattr(client, "_artifact_once", once)
    monkeypatch.setattr("time.sleep", lambda _: None)
    assert client.artifact("https://example.org/a")["url"] == "https://example.org/a"
    assert len(calls) == 2


def test_missing_asset_is_distinct_from_server_failure(monkeypatch):
    class Opener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, None)

    monkeypatch.setattr("xpkgindex_update.github.public_url", lambda u: u)
    monkeypatch.setattr("urllib.request.build_opener", lambda *a: Opener())
    with pytest.raises(WaitingAssets):
        GitHub().artifact("https://example.org/a.zip")


def test_html_is_not_an_artifact(monkeypatch):
    class Opener:
        def open(self, request, timeout):
            return io.BytesIO(b"<!doctype html><title>not a download</title>")

    monkeypatch.setattr("xpkgindex_update.github.public_url", lambda u: u)
    monkeypatch.setattr("urllib.request.build_opener", lambda *a: Opener())
    with pytest.raises(Invalid, match="HTML"):
        GitHub().artifact("https://example.org/a.zip")


def test_ephemeral_redirect_queries_do_not_enter_fixed_plans(monkeypatch):
    class Response(io.BytesIO):
        def geturl(self):
            return 'https://release-assets.githubusercontent.com/archive?signature=ephemeral'
    class Opener:
        def open(self, request, timeout):
            return Response(b'PKfixture')
    monkeypatch.setattr('xpkgindex_update.github.public_url', lambda u: u)
    monkeypatch.setattr('urllib.request.build_opener', lambda *a: Opener())
    resource = GitHub().artifact('https://example.org/a.zip')
    assert resource['final_host'] == 'release-assets.githubusercontent.com'
    assert 'ephemeral' not in str(resource)


def test_absent_mirror_backtracking_consumes_the_request_budget(monkeypatch):
    class Opener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(request.full_url, 404, 'missing', {}, None)
    monkeypatch.setattr('xpkgindex_update.github.public_url', lambda u: u)
    monkeypatch.setattr('urllib.request.build_opener', lambda *a: Opener())
    client = GitHub(request_budget=2)
    for version in ['3', '2']:
        with pytest.raises(WaitingAssets):
            client.artifact('https://mirror.example/' + version + '.zip')
    with pytest.raises(BudgetExceeded):
        client.artifact('https://mirror.example/1.zip')
    assert client.requests == 2


def test_sidecar_and_artifact_share_the_run_request_budget(monkeypatch):
    class Opener:
        def open(self, request, timeout):
            return io.BytesIO(b'PKfixture')
    monkeypatch.setattr('xpkgindex_update.github.public_url', lambda u: u)
    monkeypatch.setattr('urllib.request.build_opener', lambda *a: Opener())
    client = GitHub(request_budget=1)
    client.artifact('https://mirror.example/a.zip')
    assert client.artifact('https://mirror.example/a.zip')  # cache hits are free
    with pytest.raises(BudgetExceeded):
        client.text('https://mirror.example/a.zip.sha256')


@pytest.mark.parametrize('payload,valid', [(b'!<arch>\nfixture', True), (b'error: unavailable', False)])
def test_debian_archive_magic(monkeypatch, payload, valid):
    class Opener:
        def open(self, request, timeout):
            return io.BytesIO(payload)
    monkeypatch.setattr('xpkgindex_update.github.public_url', lambda u: u)
    monkeypatch.setattr('urllib.request.build_opener', lambda *a: Opener())
    if valid:
        assert GitHub().artifact('https://vendor.example/app.deb')['size'] == len(payload)
    else:
        with pytest.raises(Invalid, match='Debian'):
            GitHub().artifact('https://vendor.example/app.deb')


def test_oversized_resource_does_not_exhaust_the_run_budget(monkeypatch):
    class Opener:
        def open(self, request, timeout):
            return io.BytesIO(b'PKoversized' if request.full_url.endswith('large.zip') else b'PKok')
    monkeypatch.setattr('xpkgindex_update.github.public_url', lambda u: u)
    monkeypatch.setattr('urllib.request.build_opener', lambda *a: Opener())
    client = GitHub(resource_bytes=5, download_bytes=100)
    with pytest.raises(Invalid, match='per-resource') as error:
        client.artifact('https://example.org/large.zip')
    assert not isinstance(error.value, BudgetExceeded)
    assert client.artifact('https://example.org/small.zip')['size'] == 4


def test_run_download_exhaustion_is_distinct_from_resource_limit(monkeypatch):
    class Opener:
        def open(self, request, timeout):
            return io.BytesIO(b'PKfixture')
    monkeypatch.setattr('xpkgindex_update.github.public_url', lambda u: u)
    monkeypatch.setattr('urllib.request.build_opener', lambda *a: Opener())
    with pytest.raises(BudgetExceeded, match='run download'):
        GitHub(resource_bytes=100, download_bytes=5).artifact('https://example.org/a.zip')
