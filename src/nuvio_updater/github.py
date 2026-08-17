"""Watches a GitHub repository for new releases carrying an IPA asset."""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

API_ROOT = "https://api.github.com"


class GitHubError(Exception):
    """A transient or unexpected failure talking to the GitHub API."""


class NoMatchingAssetError(Exception):
    """The release exists but carries no asset matching the configured pattern.

    This is a configuration problem rather than a transient one -- retrying will
    not help until either the release or the pattern changes.
    """


@dataclass(frozen=True)
class Release:
    tag: str
    name: str
    published_at: str
    html_url: str
    ipa_url: str
    ipa_name: str
    ipa_size: int | None = None
    sha256: str | None = None
    release_id: int | None = None

    @property
    def fingerprint(self) -> str:
        """Identity of this *publication*, not just of the version it names.

        Upstream can delete a release and publish a fresh one under the same tag
        -- 3.2.8 was pulled and re-cut that way. The replacement carries a new
        id, a new publish time and a re-uploaded asset but an identical ``tag``,
        so keying "seen this already?" on the tag alone makes the second
        publication invisible. Fold in the parts that actually move.

        Every component is stable under benign edits (retitling, rewriting the
        release notes), so this does not churn.
        """
        return "|".join(
            (
                self.tag,
                str(self.release_id or ""),
                self.published_at or "",
                self.ipa_url,
                self.sha256 or "",
            )
        )

    def to_cache(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_cache(cls, data: dict[str, Any] | None) -> "Release | None":
        if not isinstance(data, dict):
            return None
        known = {f for f in cls.__dataclass_fields__}
        missing = {"tag", "ipa_url"} - set(data)
        if missing:
            return None
        try:
            return cls(**{k: v for k, v in data.items() if k in known})
        except TypeError:
            return None


@dataclass(frozen=True)
class LatestResult:
    release: Release | None
    etag: str | None
    not_modified: bool


def select_asset(
    assets: list[dict[str, Any]],
    match_re: re.Pattern[str],
    prefer_re: re.Pattern[str] | None = None,
) -> dict[str, Any]:
    """Choose the IPA asset from a release's asset list.

    Only assets that finished uploading are considered. When several match, the
    ``prefer_re`` pattern breaks the tie; failing that we sort by name so the
    choice is at least deterministic rather than dependent on API ordering.
    """
    usable = [
        a
        for a in assets
        if isinstance(a, dict)
        and a.get("name")
        and a.get("browser_download_url")
        and a.get("state", "uploaded") == "uploaded"
        and match_re.search(a["name"])
    ]
    if not usable:
        raise NoMatchingAssetError(
            f"no asset matched {match_re.pattern!r} "
            f"(saw: {', '.join(a.get('name', '?') for a in assets) or 'none'})"
        )

    if prefer_re is not None:
        preferred = [a for a in usable if prefer_re.search(a["name"])]
        if preferred:
            usable = preferred

    usable.sort(key=lambda a: a["name"])
    if len(usable) > 1:
        log.warning(
            "Multiple IPA assets matched; picking %s from %s",
            usable[0]["name"],
            ", ".join(a["name"] for a in usable),
        )
    return usable[0]


class ReleaseWatcher:
    """Fetches the latest release, using ETags so quiet polls are free.

    GitHub allows 60 unauthenticated requests per hour per IP; a 304 response
    does not count against that budget, so a 30-minute poll is comfortable even
    without a token.
    """

    def __init__(
        self,
        repo: str,
        *,
        asset_re: re.Pattern[str],
        asset_prefer_re: re.Pattern[str] | None = None,
        token: str | None = None,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ):
        self.repo = repo
        self.asset_re = asset_re
        self.asset_prefer_re = asset_prefer_re
        self.token = token
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=True)

    @property
    def latest_url(self) -> str:
        return f"{API_ROOT}/repos/{self.repo}/releases/latest"

    def _headers(self, etag: str | None) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "nuvio-autoupdate",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if etag:
            headers["If-None-Match"] = etag
        return headers

    def latest(self, etag: str | None = None) -> LatestResult:
        try:
            response = self._client.get(self.latest_url, headers=self._headers(etag))
        except httpx.HTTPError as exc:
            raise GitHubError(f"GitHub request failed: {exc}") from exc

        if response.status_code == 304:
            log.debug("GitHub returned 304 -- no new release")
            return LatestResult(release=None, etag=etag, not_modified=True)

        if response.status_code == 403 and response.headers.get("X-RateLimit-Remaining") == "0":
            reset = response.headers.get("X-RateLimit-Reset", "?")
            raise GitHubError(f"GitHub rate limit exhausted (resets at epoch {reset})")

        if response.status_code != 200:
            raise GitHubError(
                f"GitHub returned HTTP {response.status_code} for {self.latest_url}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise GitHubError(f"GitHub returned invalid JSON: {exc}") from exc

        release = self._to_release(payload)
        return LatestResult(release=release, etag=response.headers.get("ETag"), not_modified=False)

    def _to_release(self, payload: dict[str, Any]) -> Release:
        tag = payload.get("tag_name")
        if not tag:
            raise GitHubError("release payload has no tag_name")

        asset = select_asset(
            payload.get("assets") or [],
            self.asset_re,
            self.asset_prefer_re,
        )

        digest = asset.get("digest") or ""
        sha256 = digest.split("sha256:", 1)[1] if digest.startswith("sha256:") else None

        return Release(
            tag=tag,
            name=payload.get("name") or tag,
            published_at=payload.get("published_at") or "",
            html_url=payload.get("html_url") or "",
            ipa_url=asset["browser_download_url"],
            ipa_name=asset["name"],
            ipa_size=asset.get("size"),
            sha256=sha256,
            release_id=payload.get("id"),
        )

    def close(self) -> None:
        self._client.close()
