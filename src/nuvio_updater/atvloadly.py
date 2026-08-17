"""Read-side client for atvloadly, plus install verification.

Reads go over the plain REST API rather than MCP: ``GET /api/apps`` returns the
full stored record (``ID``, ``version``, ``refreshed_result``, ``refreshed_date``),
whereas the MCP ``get_app_list`` tool returns a trimmed shape that cannot
distinguish a fresh install from a stale one.

Installing is a *write* and lives in :mod:`nuvio_updater.mcp_backend`, because
``POST /api/install`` only exists in atvloadly >= v0.4.7 while ``/mcp`` has been
available since v0.4.0.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from .timeutil import parse_timestamp

log = logging.getLogger(__name__)

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


class AtvloadlyError(Exception):
    """atvloadly was unreachable or returned something unusable."""


@dataclass(frozen=True)
class AppRecord:
    """One row of atvloadly's installed-app table."""

    id: int
    ipa_name: str
    bundle_identifier: str
    version: str
    udid: str
    account: str
    refreshed_result: bool
    refreshed_date: datetime | None
    expiration_date: datetime | None
    installed_date: datetime | None
    enabled: bool

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "AppRecord":
        return cls(
            id=int(data.get("ID") or 0),
            ipa_name=data.get("ipa_name") or "",
            bundle_identifier=data.get("bundle_identifier") or "",
            version=data.get("version") or "",
            udid=data.get("udid") or "",
            account=data.get("account") or "",
            refreshed_result=bool(data.get("refreshed_result")),
            refreshed_date=parse_timestamp(data.get("refreshed_date")),
            expiration_date=parse_timestamp(data.get("expiration_date")),
            installed_date=parse_timestamp(data.get("installed_date")),
            enabled=bool(data.get("enabled")),
        )


@dataclass(frozen=True)
class Verification:
    ok: bool
    reason: str
    record: AppRecord | None = None
    version_mismatch: bool = False
    # The app installed under an identifier we did not expect...
    bundle_changed: bool = False
    # ...and this is the superseded record, when one exists to clean up.
    # The two are distinct: a first-ever install can change bundle with nothing
    # left behind to remove.
    renamed_from: AppRecord | None = None


def find_changed_record(
    before: list[AppRecord],
    after: list[AppRecord],
    prefer_bundles: tuple[str, ...] = (),
) -> AppRecord | None:
    """The record this install created or refreshed.

    Deliberately identified by *what changed* rather than by a fixed bundle
    identifier. Upstream renamed Nuvio's bundle at 3.2.6, which makes atvloadly
    write a brand-new row instead of updating the old one -- a bundle-keyed
    lookup sees the stale row and wrongly reports failure.
    """
    previous = {r.id: r for r in before}
    changed: list[AppRecord] = []
    for record in after:
        prior = previous.get(record.id)
        if prior is None:
            changed.append(record)
        elif record.refreshed_date is not None and (
            prior.refreshed_date is None or record.refreshed_date > prior.refreshed_date
        ):
            changed.append(record)

    if not changed:
        return None

    # atvloadly's own refresh cron can touch another app at the same time, so
    # prefer a record we already recognise before falling back to the newest.
    known = [r for r in changed if r.bundle_identifier in prefer_bundles]
    pool = known or changed
    pool.sort(key=lambda r: (r.refreshed_date or _EPOCH), reverse=True)
    return pool[0]


def verify_install(
    before: list[AppRecord],
    after: list[AppRecord],
    expected_version: str | None = None,
    known_bundles: tuple[str, ...] = (),
) -> Verification:
    """Decide whether an install actually landed.

    This cannot be a simple ``refreshed_result`` check. When a *new* install
    fails, atvloadly persists nothing at all -- the previous row survives
    untouched with ``refreshed_result: true``, which would read as success. The
    only trustworthy signal is that a record was created, or had its
    ``refreshed_date`` moved forward, during this install.
    """
    record = find_changed_record(before, after, known_bundles)
    if record is None:
        return Verification(
            False,
            "no app record was created or updated -- the install never completed",
        )

    if not record.refreshed_result:
        return Verification(False, "atvloadly recorded the install as failed", record=record)

    renamed_from = None
    bundle_changed = bool(known_bundles) and record.bundle_identifier not in known_bundles
    if bundle_changed:
        # Installed under an identifier we did not expect: upstream renamed it,
        # so tvOS now treats this as a separate app from the previous version.
        stale = [r for r in before if r.bundle_identifier in known_bundles]
        stale.sort(key=lambda r: (r.refreshed_date or _EPOCH), reverse=True)
        renamed_from = stale[0] if stale else None
        log.warning(
            "Installed under a new bundle identifier %r (was %r)",
            record.bundle_identifier,
            renamed_from.bundle_identifier if renamed_from else "unknown",
        )

    mismatch = bool(expected_version) and record.version != expected_version
    if mismatch:
        # Not fatal: the Info.plist version can legitimately differ from the tag.
        log.warning(
            "Installed version %r does not match expected %r", record.version, expected_version
        )

    return Verification(
        True,
        "install verified",
        record=record,
        version_mismatch=mismatch,
        bundle_changed=bundle_changed,
        renamed_from=renamed_from,
    )


class AtvloadlyClient:
    """Read-only REST access to atvloadly."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=True)

    # ---- plumbing --------------------------------------------------------

    def _get(self, path: str) -> httpx.Response:
        url = f"{self.base_url}{path}"
        try:
            return self._client.get(url)
        except httpx.HTTPError as exc:
            raise AtvloadlyError(f"GET {url} failed: {exc}") from exc

    def _get_data(self, path: str) -> Any:
        """Unwrap atvloadly's ``{"code": 200, "msg": ..., "data": ...}`` envelope."""
        response = self._get(path)
        if response.status_code != 200:
            raise AtvloadlyError(f"GET {path} returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise AtvloadlyError(f"GET {path} returned invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise AtvloadlyError(f"GET {path} returned unexpected payload type")
        if payload.get("code") != 200:
            raise AtvloadlyError(f"GET {path} failed: {payload.get('msg') or payload}")
        return payload.get("data")

    # ---- reads -----------------------------------------------------------

    def server_version(self) -> str:
        data = self._get_data("/api/version")
        if isinstance(data, dict):
            return str(data.get("version") or "unknown")
        return str(data)

    def healthy(self) -> bool:
        """``/healthcheck`` reports 503 when any installed app has expired."""
        try:
            return self._get("/healthcheck").status_code == 200
        except AtvloadlyError:
            return False

    def apps(self) -> list[AppRecord]:
        data = self._get_data("/api/apps") or []
        if not isinstance(data, list):
            raise AtvloadlyError("/api/apps did not return a list")
        return [AppRecord.from_api(item) for item in data if isinstance(item, dict)]

    def app_record(self, bundle_ids: str | tuple[str, ...]) -> AppRecord | None:
        """The most recently refreshed record matching any of ``bundle_ids``."""
        wanted = (bundle_ids,) if isinstance(bundle_ids, str) else tuple(bundle_ids)
        matches = [a for a in self.apps() if a.bundle_identifier in wanted]
        if not matches:
            return None
        matches.sort(key=lambda a: (a.refreshed_date or _EPOCH, a.id), reverse=True)
        return matches[0]

    def delete_app(self, app_id: int) -> bool:
        """Remove a record from atvloadly.

        This stops atvloadly refreshing the app; it does **not** uninstall it
        from the Apple TV -- v0.4.6 has no API for that.
        """
        url = f"{self.base_url}/api/apps/{app_id}/delete"
        try:
            response = self._client.post(url)
        except httpx.HTTPError as exc:
            raise AtvloadlyError(f"POST {url} failed: {exc}") from exc
        if response.status_code != 200:
            raise AtvloadlyError(f"deleting app {app_id} returned HTTP {response.status_code}")
        log.info("Deleted atvloadly record %d", app_id)
        return True

    def devices(self) -> list[dict[str, Any]]:
        data = self._get_data("/api/devices") or []
        return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []

    def accounts(self) -> dict[str, Any]:
        data = self._get_data("/api/accounts") or {}
        return data if isinstance(data, dict) else {}

    def task_log(self, app_id: int, tail_chars: int = 1500) -> str:
        """Fetch an install task log.

        A *failed* new install is logged under id 0, because atvloadly writes the
        log before it has a database row to attach it to.
        """
        try:
            response = self._get(f"/apps/{app_id}/log")
        except AtvloadlyError as exc:
            return f"<log unavailable: {exc}>"
        if response.status_code != 200:
            return f"<no log (HTTP {response.status_code})>"
        text = response.text.strip()
        if len(text) > tail_chars:
            return "..." + text[-tail_chars:]
        return text or "<empty log>"

    def close(self) -> None:
        self._client.close()
