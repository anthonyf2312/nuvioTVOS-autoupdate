"""Environment-driven configuration."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .schedule import QuietWindow, parse_backoff

DEFAULTS: dict[str, str] = {
    "ATVLOADLY_URL": "http://192.168.1.180:5533",
    # Upstream renamed the bundle at 3.2.6 (com.nuvio.app.tv -> com.pyksel.nuviotvos),
    # which made atvloadly treat the update as a brand-new app. Track both.
    "NUVIO_BUNDLE_IDS": "com.pyksel.nuviotvos,com.nuvio.app.tv",
    # Free Apple IDs keep only 3 sideloaded apps active at once.
    "MAX_ACTIVE_APPS": "3",
    "GITHUB_REPO": "bobsupra/NuvioTVOS",
    "TAG_VERSION_RE": r"tvos-beta-(.+)",
    "IPA_ASSET_RE": r".*\.ipa$",
    "IPA_ASSET_PREFER_RE": r"unsigned-release\.ipa$",
    "POLL_INTERVAL_MINUTES": "30",
    "QUIET_WINDOW": "04:00-06:00",
    "TZ": "Europe/London",
    "MAX_ATTEMPTS": "3",
    "BACKOFF_MINUTES": "15,60,240",
    "INSTALL_TIMEOUT_MINUTES": "20",
    "INSTALL_POLL_SECONDS": "15",
    "HTTP_TIMEOUT_SECONDS": "30",
    # Report a gap this long between successful GitHub checks, once it ends. 0 disables.
    "OFFLINE_ALERT_HOURS": "6",
    "STATE_PATH": "/data/state.json",
    "DRY_RUN": "false",
    "REQUIRE_EXISTING_APP": "true",
    "REMOVE_EXTENSIONS": "false",
    "LOG_LEVEL": "INFO",
}

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class ConfigError(Exception):
    """Raised when the environment is missing or malformed."""


def _get(env: Mapping[str, str], key: str) -> str:
    return (env.get(key) or DEFAULTS.get(key, "")).strip()


def _get_bool(env: Mapping[str, str], key: str) -> bool:
    raw = _get(env, key).lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ConfigError(f"{key} must be a boolean, got {raw!r}")


def _get_int(env: Mapping[str, str], key: str, minimum: int = 1) -> int:
    raw = _get(env, key)
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{key} must be >= {minimum}, got {value}")
    return value


def _compile(env: Mapping[str, str], key: str) -> re.Pattern[str]:
    raw = _get(env, key)
    try:
        return re.compile(raw)
    except re.error as exc:
        raise ConfigError(f"{key} is not a valid regex ({raw!r}): {exc}") from exc


@dataclass(frozen=True)
class Config:
    atvloadly_url: str
    device_id: str
    account_id: str
    bundle_ids: tuple[str, ...]
    max_active_apps: int

    github_repo: str
    github_token: str | None
    tag_version_re: re.Pattern[str]
    ipa_asset_re: re.Pattern[str]
    ipa_asset_prefer_re: re.Pattern[str]

    poll_interval_minutes: int
    quiet_window: QuietWindow
    timezone: ZoneInfo
    max_attempts: int
    backoff_minutes: tuple[int, ...]
    install_timeout_minutes: int
    install_poll_seconds: int
    http_timeout_seconds: int
    offline_alert_hours: int

    telegram_bot_token: str | None
    telegram_chat_id: str | None

    state_path: str
    dry_run: bool
    require_existing_app: bool
    remove_extensions: bool
    log_level: str

    @property
    def mcp_url(self) -> str:
        return f"{self.atvloadly_url.rstrip('/')}/mcp"

    @property
    def bundle_id(self) -> str:
        """The bundle we expect to see most often."""
        return self.bundle_ids[0]

    def version_from_tag(self, tag: str) -> str | None:
        """Extract a comparable version string from a release tag."""
        match = self.tag_version_re.search(tag)
        if not match:
            return None
        return (match.group(1) if match.groups() else match.group(0)).strip()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        env = os.environ if env is None else env

        url = _get(env, "ATVLOADLY_URL").rstrip("/")
        if not url:
            raise ConfigError("ATVLOADLY_URL is required")
        if not url.startswith(("http://", "https://")):
            raise ConfigError(f"ATVLOADLY_URL must start with http:// or https://, got {url!r}")

        device_id = _get(env, "ATV_DEVICE_ID")
        account_id = _get(env, "ATV_ACCOUNT_ID")
        if not device_id:
            raise ConfigError("ATV_DEVICE_ID is required (see `--check` output for valid ids)")
        if not account_id:
            raise ConfigError("ATV_ACCOUNT_ID is required (md5 of the Apple ID email)")

        try:
            window = QuietWindow.parse(_get(env, "QUIET_WINDOW"))
        except ValueError as exc:
            raise ConfigError(f"QUIET_WINDOW is invalid: {exc}") from exc

        tz_name = _get(env, "TZ") or "UTC"
        try:
            tz = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigError(f"TZ is not a known timezone: {tz_name!r}") from exc

        try:
            backoff = parse_backoff(_get(env, "BACKOFF_MINUTES"))
        except ValueError as exc:
            raise ConfigError(f"BACKOFF_MINUTES is invalid: {exc}") from exc

        repo = _get(env, "GITHUB_REPO")
        if repo.count("/") != 1:
            raise ConfigError(f"GITHUB_REPO must be 'owner/name', got {repo!r}")

        # NUVIO_BUNDLE_ID is the pre-rename spelling; still honoured if set alone.
        # Read the environment directly so the built-in default does not mask it.
        raw_bundles = (
            (env.get("NUVIO_BUNDLE_IDS") or "").strip()
            or (env.get("NUVIO_BUNDLE_ID") or "").strip()
            or DEFAULTS["NUVIO_BUNDLE_IDS"]
        )
        bundle_ids = tuple(b.strip() for b in raw_bundles.split(",") if b.strip())
        if not bundle_ids:
            raise ConfigError("NUVIO_BUNDLE_IDS must list at least one bundle identifier")

        return cls(
            atvloadly_url=url,
            device_id=device_id,
            account_id=account_id,
            bundle_ids=bundle_ids,
            max_active_apps=_get_int(env, "MAX_ACTIVE_APPS"),
            github_repo=repo,
            github_token=_get(env, "GITHUB_TOKEN") or None,
            tag_version_re=_compile(env, "TAG_VERSION_RE"),
            ipa_asset_re=_compile(env, "IPA_ASSET_RE"),
            ipa_asset_prefer_re=_compile(env, "IPA_ASSET_PREFER_RE"),
            poll_interval_minutes=_get_int(env, "POLL_INTERVAL_MINUTES"),
            quiet_window=window,
            timezone=tz,
            max_attempts=_get_int(env, "MAX_ATTEMPTS"),
            backoff_minutes=backoff,
            install_timeout_minutes=_get_int(env, "INSTALL_TIMEOUT_MINUTES"),
            install_poll_seconds=_get_int(env, "INSTALL_POLL_SECONDS"),
            http_timeout_seconds=_get_int(env, "HTTP_TIMEOUT_SECONDS"),
            offline_alert_hours=_get_int(env, "OFFLINE_ALERT_HOURS", minimum=0),
            telegram_bot_token=_get(env, "TELEGRAM_BOT_TOKEN") or None,
            telegram_chat_id=_get(env, "TELEGRAM_CHAT_ID") or None,
            state_path=_get(env, "STATE_PATH"),
            dry_run=_get_bool(env, "DRY_RUN"),
            require_existing_app=_get_bool(env, "REQUIRE_EXISTING_APP"),
            remove_extensions=_get_bool(env, "REMOVE_EXTENSIONS"),
            log_level=_get(env, "LOG_LEVEL").upper(),
        )
