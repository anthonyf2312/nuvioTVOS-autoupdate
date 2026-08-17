"""Command-line entry point."""

from __future__ import annotations

import argparse
import hashlib
import logging
import queue
import sys
import threading
from dataclasses import dataclass, replace
from datetime import timedelta

from .atvloadly import AtvloadlyClient, AtvloadlyError
from .commands import Command, CommandListener
from .config import Config, ConfigError
from .github import GitHubError, NoMatchingAssetError, ReleaseWatcher
from .mcp_backend import McpInstallBackend, McpError
from .commands import menu_keyboard
from .notify import TelegramNotifier, build_client, build_notifier
from .state import StateStore
from .telegram import TelegramError
from .timeutil import format_local, utcnow
from .updater import Updater


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("mcp").setLevel(logging.WARNING)


def build_updater(config: Config) -> tuple[Updater, CommandListener | None]:
    """Assemble the updater, and a Telegram listener when buttons are usable."""
    client = build_client(
        config.telegram_bot_token, config.telegram_chat_id, timeout=config.http_timeout_seconds
    )
    notifier = build_notifier(client, dry_run=config.dry_run)

    command_queue: queue.Queue[Command] = queue.Queue()
    busy = threading.Event()

    updater = Updater(
        config,
        watcher=ReleaseWatcher(
            config.github_repo,
            asset_re=config.ipa_asset_re,
            asset_prefer_re=config.ipa_asset_prefer_re,
            token=config.github_token,
            timeout=config.http_timeout_seconds,
        ),
        atvloadly=AtvloadlyClient(config.atvloadly_url, timeout=config.http_timeout_seconds),
        backend=McpInstallBackend(
            config.mcp_url,
            device_id=config.device_id,
            account_id=config.account_id,
            remove_extensions=config.remove_extensions,
            connect_timeout=config.http_timeout_seconds,
        ),
        notifier=notifier,
        store=StateStore(config.state_path),
        command_queue=command_queue,
        busy=busy,
    )

    # Buttons need a live bot. In dry-run nothing is sent, so nothing can be pressed.
    listener = None
    if client is not None and not config.dry_run:
        listener = CommandListener(client, command_queue, busy=busy)

    return updater, listener


# ---------------------------------------------------------------- preflight

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"


@dataclass
class CheckLine:
    status: str
    name: str
    detail: str


class Preflight:
    def __init__(self, config: Config, send_test: bool = False):
        self.config = config
        self.send_test = send_test
        self.lines: list[CheckLine] = []

    def record(self, status: str, name: str, detail: str = "") -> None:
        self.lines.append(CheckLine(status, name, detail))

    def run(self) -> bool:
        atv = AtvloadlyClient(self.config.atvloadly_url, timeout=self.config.http_timeout_seconds)

        self._check_atvloadly(atv)
        self._check_mcp()
        self._check_device(atv)
        self._check_account(atv)
        self._check_app(atv)
        self._check_github()
        self._check_state()
        self._check_telegram()

        width = max(len(line.name) for line in self.lines)
        print()
        for line in self.lines:
            print(f"  [{line.status:<4}] {line.name.ljust(width)}  {line.detail}")
        print()

        failures = sum(1 for line in self.lines if line.status == FAIL)
        warnings = sum(1 for line in self.lines if line.status == WARN)
        if failures:
            print(f"{failures} check(s) failed, {warnings} warning(s).")
        else:
            print(f"All checks passed ({warnings} warning(s)).")
        return failures == 0

    def _check_atvloadly(self, atv: AtvloadlyClient) -> None:
        try:
            version = atv.server_version()
        except AtvloadlyError as exc:
            self.record(FAIL, "atvloadly reachable", str(exc))
            return
        self.record(PASS, "atvloadly reachable", f"{self.config.atvloadly_url} -- {version}")
        healthy = atv.healthy()
        self.record(
            PASS if healthy else WARN,
            "atvloadly healthcheck",
            "200 OK" if healthy else "503 -- an installed app has expired",
        )

    def _check_mcp(self) -> None:
        backend = McpInstallBackend(
            self.config.mcp_url,
            device_id=self.config.device_id,
            account_id=self.config.account_id,
            connect_timeout=self.config.http_timeout_seconds,
        )
        try:
            self.record(PASS, "MCP handshake", backend.ping())
        except McpError as exc:
            self.record(FAIL, "MCP handshake", f"{self.config.mcp_url} -- {exc}")

    def _check_device(self, atv: AtvloadlyClient) -> None:
        try:
            devices = atv.devices()
        except AtvloadlyError as exc:
            self.record(FAIL, "target device", str(exc))
            return

        match = next((d for d in devices if d.get("id") == self.config.device_id), None)
        if match is None:
            known = ", ".join(f"{d.get('name')}={d.get('id')}" for d in devices) or "none"
            self.record(FAIL, "target device", f"id not found. Available: {known}")
            return

        status = match.get("status")
        detail = f"{match.get('name')} ({match.get('device_class')}) status={status}"
        self.record(PASS if status == "paired" else WARN, "target device", detail)

    def _check_account(self, atv: AtvloadlyClient) -> None:
        try:
            accounts = atv.accounts()
        except AtvloadlyError as exc:
            self.record(FAIL, "Apple account", str(exc))
            return

        for email, info in accounts.items():
            digest = hashlib.md5(email.encode()).hexdigest()
            if digest == self.config.account_id:
                status = (info or {}).get("status", "unknown")
                masked = email[:3] + "***" + email[email.find("@") :] if "@" in email else email
                self.record(
                    PASS if status == "valid" else WARN,
                    "Apple account",
                    f"{masked} status={status}",
                )
                return

        expected = {
            hashlib.md5(e.encode()).hexdigest(): e for e in accounts
        }
        hint = ", ".join(f"{v} -> {k}" for k, v in expected.items()) or "no accounts configured"
        self.record(FAIL, "Apple account", f"ATV_ACCOUNT_ID matched nothing. Known: {hint}")

    def _check_app(self, atv: AtvloadlyClient) -> None:
        try:
            record = atv.app_record(self.config.bundle_ids)
        except AtvloadlyError as exc:
            self.record(FAIL, "Nuvio app record", str(exc))
            return
        if record is None:
            self.record(
                WARN,
                "Nuvio app record",
                f"no app matching {', '.join(self.config.bundle_ids)}"
                " -- sideload it once by hand",
            )
            return
        self.record(
            PASS,
            "Nuvio app record",
            f"id={record.id} v{record.version} bundle={record.bundle_identifier} "
            f"expires {format_local(record.expiration_date, self.config.timezone)}",
        )

    def _check_github(self) -> None:
        watcher = ReleaseWatcher(
            self.config.github_repo,
            asset_re=self.config.ipa_asset_re,
            asset_prefer_re=self.config.ipa_asset_prefer_re,
            token=self.config.github_token,
            timeout=self.config.http_timeout_seconds,
        )
        try:
            result = watcher.latest(None)
        except (GitHubError, NoMatchingAssetError) as exc:
            self.record(FAIL, "GitHub latest release", str(exc))
            return
        release = result.release
        assert release is not None
        version = self.config.version_from_tag(release.tag)
        self.record(
            PASS if version else WARN,
            "GitHub latest release",
            f"{release.tag} -> version={version or 'UNPARSED (check TAG_VERSION_RE)'} "
            f"asset={release.ipa_name}",
        )

    def _check_state(self) -> None:
        store = StateStore(self.config.state_path)
        try:
            state = store.load()
            store.save(state)
        except OSError as exc:
            self.record(FAIL, "state file writable", f"{self.config.state_path}: {exc}")
            return
        self.record(
            PASS,
            "state file writable",
            f"{self.config.state_path} (installed_tag={state.last_installed_tag})",
        )

    def _check_telegram(self) -> None:
        client = build_client(
            self.config.telegram_bot_token,
            self.config.telegram_chat_id,
            timeout=self.config.http_timeout_seconds,
        )
        if client is None:
            self.record(WARN, "Telegram", "not configured -- notifications will only be logged")
            self.record(WARN, "Telegram buttons", "unavailable without bot credentials")
            return

        try:
            who = client.get_me()
        except Exception as exc:  # noqa: BLE001 - report any credential problem
            self.record(FAIL, "Telegram", str(exc))
            return

        if self.send_test:
            ok = TelegramNotifier(client).send(
                "🧪 <b>nuvio-autoupdate</b>\nPreflight test message.",
                menu_keyboard(),
            )
            self.record(
                PASS if ok else FAIL,
                "Telegram",
                f"{who} -- test message {'delivered' if ok else 'FAILED, check TELEGRAM_CHAT_ID'}",
            )
        else:
            self.record(PASS, "Telegram", f"{who} -- rerun with --send-test to post a message")

        # Buttons are useless if we cannot read presses back.
        try:
            client.get_updates(offset=-1, long_poll_seconds=0)
        except TelegramError as exc:
            self.record(FAIL, "Telegram buttons", f"getUpdates failed: {exc}")
            return
        self.record(PASS, "Telegram buttons", "getUpdates reachable (long-polling will work)")


# ------------------------------------------------------------------- health


def healthcheck(config: Config) -> int:
    state = StateStore(config.state_path).load()
    beat = state.heartbeat
    if beat is None:
        print("no heartbeat recorded yet")
        return 1
    age = utcnow() - beat
    limit = timedelta(minutes=config.poll_interval_minutes * 2 + 5)
    if age > limit:
        print(f"heartbeat is stale: {age} old (limit {limit})")
        return 1
    print(f"ok, heartbeat {int(age.total_seconds())}s old")
    return 0


# --------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nuvio-updater",
        description="Keep a sideloaded Nuvio TV build in sync with its GitHub releases.",
    )
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="never write to atvloadly; log the install that would happen",
    )
    parser.add_argument("--check", action="store_true", help="run preflight checks and exit")
    parser.add_argument(
        "--send-test", action="store_true", help="with --check, post a Telegram test message"
    )
    parser.add_argument(
        "--healthcheck", action="store_true", help="exit non-zero if the loop has stalled"
    )
    args = parser.parse_args(argv)

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        config = replace(config, dry_run=True)

    configure_logging(config.log_level)

    if args.healthcheck:
        return healthcheck(config)

    if args.check:
        return 0 if Preflight(config, send_test=args.send_test).run() else 1

    updater, listener = build_updater(config)

    if args.once:
        result = updater.tick()
        logging.getLogger(__name__).info(
            "Result: %s%s", result.outcome, f" -- {result.detail}" if result.detail else ""
        )
        return 0

    if listener is not None:
        listener.start()
    try:
        updater.run_forever()
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Interrupted, shutting down")
    finally:
        if listener is not None:
            listener.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
