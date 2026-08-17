from __future__ import annotations

import dataclasses
import re

import httpx
import pytest

from nuvio_updater.github import (
    GitHubError,
    NoMatchingAssetError,
    Release,
    ReleaseWatcher,
    select_asset,
)

ASSET_RE = re.compile(r".*\.ipa$")
PREFER_RE = re.compile(r"unsigned-release\.ipa$")


def asset(name, url="https://example.test/a.ipa", state="uploaded", **extra):
    return {"name": name, "browser_download_url": url, "state": state, **extra}


def release_payload(tag="tvos-beta-3.2.5", assets=None, release_id=365637729):
    return {
        "id": release_id,
        "tag_name": tag,
        "name": "Beta 3.2.5",
        "published_at": "2026-08-05T15:43:29Z",
        "html_url": f"https://github.com/bobsupra/NuvioTVOS/releases/tag/{tag}",
        "assets": assets
        if assets is not None
        else [
            asset(
                "NuvioTV-3.2.5-unsigned-release.ipa",
                "https://github.com/bobsupra/NuvioTVOS/releases/download/x/NuvioTV.ipa",
                size=22824182,
                digest="sha256:c80429a67eb23d2db0a3a4148d4810526a55bca8265693d2855c5105161cf3cc",
            )
        ],
    }


def watcher_with(handler, **kwargs):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return ReleaseWatcher(
        "bobsupra/NuvioTVOS", asset_re=ASSET_RE, asset_prefer_re=PREFER_RE, client=client, **kwargs
    )


class TestSelectAsset:
    def test_single_match(self):
        chosen = select_asset([asset("NuvioTV.ipa")], ASSET_RE, PREFER_RE)
        assert chosen["name"] == "NuvioTV.ipa"

    def test_ignores_non_ipa(self):
        chosen = select_asset(
            [asset("notes.txt"), asset("NuvioTV.ipa"), asset("checksums.sha256")],
            ASSET_RE,
            PREFER_RE,
        )
        assert chosen["name"] == "NuvioTV.ipa"

    def test_prefers_unsigned_release(self):
        chosen = select_asset(
            [asset("AAA-debug.ipa"), asset("NuvioTV-3.2.5-unsigned-release.ipa")],
            ASSET_RE,
            PREFER_RE,
        )
        assert chosen["name"] == "NuvioTV-3.2.5-unsigned-release.ipa"

    def test_deterministic_when_several_match_equally(self):
        chosen = select_asset([asset("b.ipa"), asset("a.ipa")], ASSET_RE, None)
        assert chosen["name"] == "a.ipa"

    def test_skips_assets_still_uploading(self):
        with pytest.raises(NoMatchingAssetError):
            select_asset([asset("NuvioTV.ipa", state="starter")], ASSET_RE, PREFER_RE)

    def test_no_assets_at_all(self):
        with pytest.raises(NoMatchingAssetError):
            select_asset([], ASSET_RE, PREFER_RE)

    def test_error_names_what_it_saw(self):
        with pytest.raises(NoMatchingAssetError, match="notes.txt"):
            select_asset([asset("notes.txt")], ASSET_RE, PREFER_RE)


class TestReleaseWatcher:
    def test_parses_release_and_digest(self):
        watcher = watcher_with(
            lambda request: httpx.Response(
                200, json=release_payload(), headers={"ETag": 'W/"abc"'}
            )
        )
        result = watcher.latest()
        assert result.not_modified is False
        assert result.etag == 'W/"abc"'
        release = result.release
        assert release is not None
        assert release.tag == "tvos-beta-3.2.5"
        assert release.ipa_name == "NuvioTV-3.2.5-unsigned-release.ipa"
        assert release.sha256 and release.sha256.startswith("c80429a6")
        assert release.ipa_size == 22824182
        assert release.release_id == 365637729

    def test_sends_conditional_header_and_handles_304(self):
        seen = {}

        def handler(request):
            seen["if_none_match"] = request.headers.get("If-None-Match")
            return httpx.Response(304)

        result = watcher_with(handler).latest('W/"abc"')
        assert seen["if_none_match"] == 'W/"abc"'
        assert result.not_modified is True
        assert result.release is None
        assert result.etag == 'W/"abc"'  # caller's etag is preserved

    def test_token_is_sent_when_configured(self):
        seen = {}

        def handler(request):
            seen["auth"] = request.headers.get("Authorization")
            return httpx.Response(200, json=release_payload())

        watcher_with(handler, token="ghp_secret").latest()
        assert seen["auth"] == "Bearer ghp_secret"

    def test_no_auth_header_without_token(self):
        seen = {}

        def handler(request):
            seen["auth"] = request.headers.get("Authorization")
            return httpx.Response(200, json=release_payload())

        watcher_with(handler).latest()
        assert seen["auth"] is None

    def test_rate_limit_is_reported_clearly(self):
        handler = lambda request: httpx.Response(  # noqa: E731
            403, json={}, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "123"}
        )
        with pytest.raises(GitHubError, match="rate limit"):
            watcher_with(handler).latest()

    def test_server_error_raises(self):
        with pytest.raises(GitHubError, match="HTTP 500"):
            watcher_with(lambda request: httpx.Response(500)).latest()

    def test_transport_error_raises_githuberror(self):
        def handler(request):
            raise httpx.ConnectError("boom")

        with pytest.raises(GitHubError, match="request failed"):
            watcher_with(handler).latest()

    def test_missing_asset_surfaces_as_config_error(self):
        handler = lambda request: httpx.Response(  # noqa: E731
            200, json=release_payload(assets=[asset("readme.txt")])
        )
        with pytest.raises(NoMatchingAssetError):
            watcher_with(handler).latest()


def make_cacheable_release():
    return Release(
        tag="tvos-beta-3.2.6",
        name="Beta 3.2.6",
        published_at="2026-08-06T10:00:00Z",
        html_url="https://example.test/r",
        ipa_url="https://example.test/a.ipa",
        ipa_name="a.ipa",
        sha256="c80429a6",
        release_id=367178201,
    )


class TestReleaseCache:
    def test_roundtrip(self):
        original = make_cacheable_release()
        assert Release.from_cache(original.to_cache()) == original

    @pytest.mark.parametrize("bad", [None, {}, {"tag": "x"}, "not a dict", {"ipa_url": "u"}])
    def test_rejects_unusable_cache(self, bad):
        assert Release.from_cache(bad) is None

    def test_fingerprint_survives_the_cache_roundtrip(self):
        # The 304 path serves a Release rebuilt from cache. If that fingerprinted
        # differently from a freshly fetched one, every other tick would look new.
        original = make_cacheable_release()
        restored = Release.from_cache(original.to_cache())
        assert restored is not None
        assert restored.fingerprint == original.fingerprint

    @pytest.mark.parametrize(
        "field, value",
        [
            ("release_id", 999),
            ("published_at", "2026-08-15T18:48:53Z"),
            ("sha256", "beef"),
            ("ipa_url", "https://example.test/other.ipa"),
        ],
    )
    def test_fingerprint_moves_when_the_publication_does(self, field, value):
        original = make_cacheable_release()
        replaced = dataclasses.replace(original, **{field: value})
        assert replaced.tag == original.tag  # same version, different publication
        assert replaced.fingerprint != original.fingerprint

    def test_fingerprint_ignores_cosmetic_edits(self):
        original = make_cacheable_release()
        retitled = dataclasses.replace(original, name="Beta 3.2.6 (hotfix notes)")
        assert retitled.fingerprint == original.fingerprint

    def test_tolerates_extra_keys_from_a_future_version(self):
        payload = {
            "tag": "t",
            "name": "n",
            "published_at": "",
            "html_url": "",
            "ipa_url": "u",
            "ipa_name": "a.ipa",
            "some_new_field": 1,
        }
        restored = Release.from_cache(payload)
        assert restored is not None and restored.tag == "t"
