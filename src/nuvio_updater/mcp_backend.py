"""Install side of atvloadly, driven over its MCP endpoint.

``POST /api/install`` only landed in atvloadly v0.4.7, but ``/mcp`` has exposed
an equivalent ``install_app`` tool since v0.4.0 -- so MCP is the path that works
across versions, including the v0.4.6 this was built against.

This is a deliberately small, synchronous JSON-RPC client rather than the
official MCP SDK. Three tool calls do not justify the SDK's dependency
footprint, its asyncio requirement, or its API churn across majors (1.x's
``streamablehttp_client`` became 2.x's ``streamable_http_client``, on a
different HTTP library). Everything here speaks plain ``httpx``, so it is also
testable with ``httpx.MockTransport``.

Transport notes (verified against atvloadly v0.4.6):
  * ``POST /mcp`` with ``Accept: application/json, text/event-stream``.
  * ``initialize`` replies as a one-shot SSE frame and returns ``Mcp-Session-Id``.
  * Subsequent calls echo that session id back.
  * We never open the long-lived ``GET`` stream, so nothing can time out
    underneath a 20-minute install.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

import httpx

log = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "nuvio-autoupdate", "version": "1.0.0"}


class McpError(Exception):
    """The MCP call failed or returned something unusable."""


class InstallConfigurationError(Exception):
    """atvloadly needs a device or account we did not supply.

    Retrying is pointless -- the configured ``ATV_DEVICE_ID`` / ``ATV_ACCOUNT_ID``
    is wrong, or the device is no longer paired.
    """


@dataclass(frozen=True)
class InstallResult:
    queued: bool
    completed: bool
    timed_out: bool
    waited_seconds: float
    message: str = ""
    device_name: str | None = None
    account_email: str | None = None


class InstallBackend(Protocol):
    """The write surface the updater depends on (faked in tests)."""

    def ping(self) -> str: ...

    def install_in_progress(self) -> bool: ...

    def install_and_wait(
        self,
        ipa_url: str,
        *,
        timeout_seconds: float,
        poll_seconds: float,
        on_progress: Callable[[float], None] | None = None,
    ) -> InstallResult: ...


# --------------------------------------------------------------- wire helpers


def parse_rpc_response(response: httpx.Response) -> dict[str, Any]:
    """Extract a single JSON-RPC message from a JSON or SSE response body."""
    content_type = response.headers.get("content-type", "")

    if "text/event-stream" in content_type:
        message = _first_sse_message(response.text)
        if message is None:
            raise McpError("SSE response contained no data frame")
        return message

    try:
        payload = response.json()
    except ValueError as exc:
        raise McpError(f"expected JSON, got: {response.text[:200]!r}") from exc
    if not isinstance(payload, dict):
        raise McpError(f"expected a JSON object, got {type(payload).__name__}")
    return payload


def _first_sse_message(body: str) -> dict[str, Any] | None:
    """Return the first well-formed ``data:`` payload in an SSE stream."""
    for block in body.replace("\r\n", "\n").split("\n\n"):
        data_lines = [
            line[len("data:") :].lstrip()
            for line in block.split("\n")
            if line.startswith("data:")
        ]
        if not data_lines:
            continue
        try:
            parsed = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def tool_payload(result: dict[str, Any]) -> dict[str, Any]:
    """Normalise an MCP ``CallToolResult`` into a plain dict."""
    if result.get("isError"):
        raise McpError(_result_text(result) or "MCP tool reported an error")

    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured

    text = _result_text(result)
    if text:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise McpError(f"tool returned non-JSON content: {text[:200]}") from exc
        if isinstance(parsed, dict):
            return parsed
    raise McpError("tool returned no usable content")


def _result_text(result: dict[str, Any]) -> str:
    chunks = [
        item["text"]
        for item in result.get("content") or []
        if isinstance(item, dict) and isinstance(item.get("text"), str)
    ]
    return "\n".join(chunks)


# ------------------------------------------------------------------- session


class McpSession:
    """One initialised MCP session over streamable HTTP."""

    def __init__(self, url: str, client: httpx.Client):
        self.url = url
        self._client = client
        self._session_id: str | None = None
        self._protocol_version = PROTOCOL_VERSION
        self._next_id = 0
        self.server_info: dict[str, Any] = {}

    # -- plumbing --

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
            headers["MCP-Protocol-Version"] = self._protocol_version
        return headers

    def _post(self, body: dict[str, Any]) -> httpx.Response:
        try:
            return self._client.post(self.url, json=body, headers=self._headers())
        except httpx.HTTPError as exc:
            raise McpError(f"POST {self.url} failed: {exc}") from exc

    def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        body: dict[str, Any] = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            body["params"] = params

        response = self._post(body)
        if response.status_code >= 400:
            raise McpError(
                f"{method} returned HTTP {response.status_code}: {response.text[:200]}"
            )

        message = parse_rpc_response(response)
        if "error" in message:
            error = message["error"] or {}
            raise McpError(f"{method} failed: {error.get('message', error)}")
        result = message.get("result")
        if not isinstance(result, dict):
            raise McpError(f"{method} returned no result object")
        return result

    def _notify(self, method: str) -> None:
        response = self._post({"jsonrpc": "2.0", "method": method})
        if response.status_code >= 400:
            raise McpError(
                f"{method} returned HTTP {response.status_code}: {response.text[:200]}"
            )

    # -- lifecycle --

    def open(self) -> dict[str, Any]:
        response = self._post(
            {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": CLIENT_INFO,
                },
            }
        )
        if response.status_code >= 400:
            raise McpError(
                f"initialize returned HTTP {response.status_code}: {response.text[:200]}"
            )

        message = parse_rpc_response(response)
        if "error" in message:
            raise McpError(f"initialize failed: {(message['error'] or {}).get('message')}")
        result = message.get("result")
        if not isinstance(result, dict):
            raise McpError("initialize returned no result object")

        self._session_id = response.headers.get("Mcp-Session-Id")
        self._protocol_version = result.get("protocolVersion") or PROTOCOL_VERSION
        self.server_info = result.get("serverInfo") or {}
        self._notify("notifications/initialized")
        return self.server_info

    def close(self) -> None:
        """Best-effort session teardown; servers may not implement DELETE."""
        if not self._session_id:
            return
        try:
            self._client.delete(self.url, headers=self._headers())
        except httpx.HTTPError:
            log.debug("MCP session teardown failed (harmless)", exc_info=True)
        finally:
            self._session_id = None

    def call(self, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        result = self._request("tools/call", {"name": tool, "arguments": arguments or {}})
        return tool_payload(result)

    def describe_server(self) -> str:
        return f"{self.server_info.get('name', 'unknown')} {self.server_info.get('version', '?')}"


# ------------------------------------------------------------------- backend


class McpInstallBackend:
    def __init__(
        self,
        mcp_url: str,
        *,
        device_id: str,
        account_id: str,
        remove_extensions: bool = False,
        connect_timeout: float = 30.0,
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.mcp_url = mcp_url
        self.device_id = device_id
        self.account_id = account_id
        self.remove_extensions = remove_extensions
        self.sleeper = sleeper
        self._client = client or httpx.Client(timeout=connect_timeout, follow_redirects=True)

    def _open(self) -> McpSession:
        session = McpSession(self.mcp_url, self._client)
        session.open()
        return session

    # -- public API --

    def ping(self) -> str:
        session = self._open()
        try:
            return session.describe_server()
        finally:
            session.close()

    def install_in_progress(self) -> bool:
        session = self._open()
        try:
            return bool(session.call("get_install_status").get("install_in_progress"))
        finally:
            session.close()

    def install_and_wait(
        self,
        ipa_url: str,
        *,
        timeout_seconds: float,
        poll_seconds: float,
        on_progress: Callable[[float], None] | None = None,
    ) -> InstallResult:
        """Queue an install and poll until atvloadly reports no active task.

        ``get_install_status`` is global rather than per-app, so a concurrent
        refresh of another app can keep it ``in_progress``. That only ever makes
        us wait longer; whether *our* install worked is settled separately by
        :func:`nuvio_updater.atvloadly.verify_install`.
        """
        session = self._open()
        try:
            arguments: dict[str, Any] = {
                "ipa_url": ipa_url,
                "device_id": self.device_id,
                "account_id": self.account_id,
            }
            if self.remove_extensions:
                arguments["remove_extensions"] = True

            payload = session.call("install_app", arguments)
            status = str(payload.get("status") or "")
            message = str(payload.get("message") or "")

            if status in ("require_device", "require_account"):
                raise InstallConfigurationError(
                    f"atvloadly rejected the install ({status}): {message} "
                    "-- check ATV_DEVICE_ID / ATV_ACCOUNT_ID against `--check`"
                )
            if status != "installing":
                raise McpError(f"unexpected install_app status {status!r}: {message}")

            device = payload.get("selected_device")
            account = payload.get("selected_account")
            device_name = device.get("name") if isinstance(device, dict) else None
            account_email = account.get("account_email") if isinstance(account, dict) else None

            started = time.monotonic()
            while True:
                self.sleeper(poll_seconds)
                elapsed = time.monotonic() - started

                in_progress = bool(
                    session.call("get_install_status").get("install_in_progress")
                )
                if on_progress is not None:
                    on_progress(elapsed)

                if not in_progress:
                    return InstallResult(
                        queued=True,
                        completed=True,
                        timed_out=False,
                        waited_seconds=elapsed,
                        message=message,
                        device_name=device_name,
                        account_email=account_email,
                    )

                if elapsed >= timeout_seconds:
                    return InstallResult(
                        queued=True,
                        completed=False,
                        timed_out=True,
                        waited_seconds=elapsed,
                        message=f"still in progress after {elapsed / 60:.1f} min",
                        device_name=device_name,
                        account_email=account_email,
                    )
        finally:
            session.close()

    def close(self) -> None:
        self._client.close()
