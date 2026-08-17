# nuvio-autoupdate

Watches [`bobsupra/NuvioTVOS`](https://github.com/bobsupra/NuvioTVOS) for new releases and
sideloads them onto an Apple TV through a self-hosted
[atvloadly](https://github.com/bitxeno/atvloadly), then tells you on Telegram whether it worked.

## How it works

```
GitHub releases/latest ──(ETag poll, 30 min)──▶ updater ──(MCP install_app)──▶ atvloadly ──▶ Apple TV
                                                   │                              │
                                                   └──── verify via /api/apps ◀────┘
                                                   │
                                                   └──▶ Telegram
```

1. Polls `releases/latest` with an `If-None-Match` header. A `304` costs nothing against
   GitHub's 60-requests-per-hour unauthenticated budget.
2. On a new tag it notifies immediately, then waits for the quiet window before touching
   anything — a sideload replaces the running app, which is unwelcome mid-episode.
3. Hands the asset's download URL to atvloadly's MCP `install_app` tool. **atvloadly downloads
   and signs the IPA itself**; this container never handles the binary.
4. Polls `get_install_status` until the queue drains, then verifies against `/api/apps`.
5. Reports success, or retries with backoff and reports a single give-up message with the log.

### Why MCP rather than `POST /api/install`

The `/mcp` endpoint has offered `install_app` since **v0.4.0**, so one code path covers every
version this is likely to meet. The REST install endpoint is newer and its exact arrival version
varies, so relying on it would tie the container to a minimum atvloadly release for no gain.

The MCP client here is ~250 lines of synchronous JSON-RPC over `httpx` rather than the official
MCP SDK. Three tool calls do not justify the SDK's dependency footprint or its asyncio
requirement, and its client API changed shape between 1.x and 2.x. **`httpx` is the only runtime
dependency.**

### Why success is not simply `refreshed_result`

When a *new* install fails, atvloadly persists nothing at all — the previous row survives
untouched with `refreshed_result: true`. Reading that field alone would report a failed update as
a success. The updater snapshots the record before installing and requires `refreshed_date` to
have actually moved forward.

## Setup

### 1. Telegram

Message [@BotFather](https://t.me/BotFather), send `/newbot`, and copy the token. Then send your
new bot any message and read your chat id from:

```
https://api.telegram.org/bot<TOKEN>/getUpdates      # look for result[].message.chat.id
```

### 2. Configure

```bash
cp .env.example .env
$EDITOR .env          # add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
```

Everything else in `.env.example` is already filled in from the live atvloadly instance
(device id, account id, bundle id). Re-derive them any time with `--check`.

### 3. Verify before running

```bash
docker compose run --rm nuvio-autoupdate --check --send-test
```

Confirms atvloadly is reachable, the MCP handshake works, the device is paired, the Apple ID is
valid, the release asset resolves, and a Telegram message actually arrives.

### 4. Start

```bash
docker compose up -d --build
docker compose logs -f
```

The first tick establishes a baseline: if the installed version already matches the newest
release it records that and does nothing. It never re-installs what is already there.

## Operating it

```bash
docker compose logs -f                                  # what it is doing
docker compose run --rm nuvio-autoupdate --check        # re-run preflight
docker compose run --rm nuvio-autoupdate --once --dry-run   # one cycle, no writes
docker exec nuvio-autoupdate python -m nuvio_updater --healthcheck
docker run --rm -v nuvio-autoupdate_nuvio-state:/data busybox cat /data/state.json
```

### Forcing an update now

Clear the recorded tag and open the window for a single run:

```bash
docker compose run --rm -e QUIET_WINDOW=always nuvio-autoupdate --once
```

(That still skips a version already recorded as installed. To genuinely re-sideload the current
version, blank `last_installed_tag` and `last_installed_release` in `/data/state.json` first —
or just press **⚡ Update now**, which ignores the recorded install when the release on GitHub is
not the one that was installed.)

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `ATVLOADLY_URL` | `http://192.168.1.180:5533` | Base URL, no trailing slash needed |
| `ATV_DEVICE_ID` | — | Required. From `--check` |
| `ATV_ACCOUNT_ID` | — | Required. md5 of the Apple ID email |
| `NUVIO_BUNDLE_IDS` | `com.pyksel.nuviotvos,com.nuvio.app.tv` | Identifiers counted as Nuvio |
| `MAX_ACTIVE_APPS` | `3` | Free Apple ID limit; exceeding it triggers a warning |
| `GITHUB_REPO` | `bobsupra/NuvioTVOS` | |
| `GITHUB_TOKEN` | *(empty)* | Optional; raises the rate limit |
| `TAG_VERSION_RE` | `tvos-beta-(.+)` | Turns a tag into a comparable version |
| `IPA_ASSET_RE` | `.*\.ipa$` | Which asset to install |
| `POLL_INTERVAL_MINUTES` | `30` | |
| `QUIET_WINDOW` | `04:00-06:00` | Local time. `always` disables gating. Wraps midnight fine |
| `TZ` | `Europe/London` | Interprets the window; handles BST/GMT |
| `MAX_ATTEMPTS` | `3` | Per release |
| `BACKOFF_MINUTES` | `15,60,240` | Between attempts |
| `OFFLINE_ALERT_HOURS` | `6` | Report a gap this long between successful checks, once it ends. `0` disables |
| `INSTALL_TIMEOUT_MINUTES` | `20` | |
| `REQUIRE_EXISTING_APP` | `true` | See below |
| `REMOVE_EXTENSIONS` | `false` | Passed through to atvloadly |
| `DRY_RUN` | `false` | Never writes to atvloadly |

### `REQUIRE_EXISTING_APP`

A free Apple ID can keep only **3 apps active at once**, and installing a fourth silently breaks
one of the others. atvloadly upserts on `(udid, bundle_identifier, account)`, so *updating* Nuvio
reuses its existing slot — but installing it when no record exists would claim a new one. The
default refuses that and notifies instead. Sideload Nuvio once by hand and updates resume.

## Telegram controls

Every release notification carries buttons, and `/menu`, `/status`, `/update` and `/schedule`
work as typed commands at any time.

| Control | Effect |
|---|---|
| ⚡ Update now | Installs the pending release immediately, ignoring the quiet window. Also clears a spent retry budget, so it works after a run of failures. |
| ⏭ Skip this one | Marks that version skipped. The next release is offered as normal. |
| 🕒 Schedule | Twelve 2-hour blocks plus "Immediately". The choice is stored in `/data/state.json` and **overrides `QUIET_WINDOW`** from `.env`. |

Presses are received by long-polling `getUpdates` from a daemon thread, so no inbound port,
public hostname or TLS certificate is needed. Only the configured `TELEGRAM_CHAT_ID` is obeyed —
messages from any other chat are logged and dropped, since the bot can trigger installs.

Presses that arrive while the container is stopped are **discarded** on startup rather than
replayed; an "Update now" from three days ago firing on boot would be surprising.

## When upstream renames the bundle ID

Nuvio changed from `com.nuvio.app.tv` to `com.pyksel.nuviotvos` at 3.2.6. tvOS keys apps by
bundle ID, so this is not an update — it installs as a **separate app**, alongside the old one
and with empty settings.

The updater identifies its install by *which record changed*, not by a fixed bundle, so a rename
is still recognised as success. It then deletes the superseded atvloadly record (otherwise
atvloadly keeps re-signing an app you no longer use, spending one of the free account's three
slots every week), remembers the new identifier in `state.json`, and tells you on Telegram.

**Removing the stale icon is manual.** atvloadly v0.4.6 has no uninstall API — `POST
/apps/:id/delete` only drops its database row — and `ideviceinstaller` is not in the container.
Nothing here can remove an app from the Apple TV.

## Failure modes

| Symptom | Cause | Action |
|---|---|---|
| "update failed" after 3 tries | Apple TV asleep or off the network | Wake it; the next release retries |
| Repeated auth failures in the log | Apple 2FA expired, or the ID needs re-auth | Log in again in the atvloadly UI |
| "nuvio-autoupdate is misconfigured" | Device unpaired or id changed | Re-pair, then `--check` |
| "can't find an IPA" | Release asset renamed | Adjust `IPA_ASSET_RE` |
| Healthcheck failing | Loop stalled | `docker compose logs`; restarts automatically |
| Two Nuvio icons on the Apple TV | Upstream renamed the bundle ID | Delete the old icon by hand; the record is cleaned up automatically |
| Buttons do nothing | Listener died, or a second instance is polling | `docker compose logs`; only one process may poll a bot token |

`GET /healthcheck` on atvloadly returns **503 whenever any installed app has expired** — including
Spotify or Kodi — so it is a signal about the whole instance, not just Nuvio.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest
```

302 tests, no network access required — GitHub, atvloadly, MCP and Telegram are all exercised
through `httpx.MockTransport`.

## Notes

- Each install resets Nuvio's 7-day signing clock. atvloadly's own refresh cron continues to run
  and the two coexist; the updater defers whenever atvloadly reports an install already running.
- atvloadly's API has **no authentication**, so anything on the LAN can drive it. That is a
  property of atvloadly, not of this container.
