from __future__ import annotations

import json

import httpx
import pytest

from nuvio_updater.mcp_backend import (
    InstallConfigurationError,
    McpError,
    McpInstallBackend,
    McpSession,
    parse_rpc_response,
    tool_payload,
)

URL = "http://atv.test:5533/mcp"

INIT_RESULT = {
    "capabilities": {"tools": {"listChanged": True}},
    "protocolVersion": "2025-06-18",
    "serverInfo": {"name": "atvloadly-mcp", "version": "v0.4.6"},
}


def sse(payload: dict) -> httpx.Response:
    """Mimic atvloadly's one-shot SSE reply."""
    body = f"event: message\nid: ABC_0\ndata: {json.dumps(payload)}\n\n"
    return httpx.Response(
        200,
        text=body,
        headers={"Content-Type": "text/event-stream", "Mcp-Session-Id": "SESSION123"},
    )


class Server:
    """Scriptable MCP server: maps a tool name to a queue of result payloads."""

    def __init__(self, tool_results: dict[str, list[dict]] | None = None):
        self.tool_results = {k: list(v) for k, v in (tool_results or {}).items()}
        self.requests: list[dict] = []
        self.session_headers: list[str | None] = []
        self.deletes = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            self.deletes += 1
            return httpx.Response(200)

        body = json.loads(request.content)
        self.requests.append(body)
        self.session_headers.append(request.headers.get("Mcp-Session-Id"))
        method = body.get("method")

        if method == "initialize":
            return sse({"jsonrpc": "2.0", "id": body["id"], "result": INIT_RESULT})
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/call":
            name = body["params"]["name"]
            queue = self.tool_results.get(name)
            if not queue:
                return sse(
                    {
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "error": {"code": -32601, "message": f"no script for {name}"},
                    }
                )
            structured = queue.pop(0) if len(queue) > 1 else queue[0]
            return sse(
                {
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"structuredContent": structured, "content": []},
                }
            )
        return httpx.Response(400, text=f"unexpected method {method}")

    def tool_calls(self, name: str) -> list[dict]:
        return [
            r["params"]["arguments"]
            for r in self.requests
            if r.get("method") == "tools/call" and r["params"]["name"] == name
        ]


def backend_for(server: Server, **kwargs) -> McpInstallBackend:
    return McpInstallBackend(
        URL,
        device_id="device-abc",
        account_id="account-abc",
        client=httpx.Client(transport=httpx.MockTransport(server.handler)),
        sleeper=lambda _: None,
        **kwargs,
    )


class TestWireParsing:
    def test_parses_sse_frame(self):
        message = parse_rpc_response(sse({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}))
        assert message["result"] == {"ok": True}

    def test_parses_plain_json(self):
        response = httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 1, "result": {}},
            headers={"Content-Type": "application/json"},
        )
        assert parse_rpc_response(response)["id"] == 1

    def test_multiline_sse_data(self):
        body = 'event: message\ndata: {"jsonrpc":"2.0",\ndata: "id":1,"result":{"a":1}}\n\n'
        response = httpx.Response(
            200, text=body, headers={"Content-Type": "text/event-stream"}
        )
        assert parse_rpc_response(response)["result"] == {"a": 1}

    def test_sse_with_no_data_frame(self):
        response = httpx.Response(
            200, text="event: ping\n\n", headers={"Content-Type": "text/event-stream"}
        )
        with pytest.raises(McpError, match="no data frame"):
            parse_rpc_response(response)

    def test_non_json_body(self):
        response = httpx.Response(200, text="<html>oops</html>")
        with pytest.raises(McpError, match="expected JSON"):
            parse_rpc_response(response)


class TestToolPayload:
    def test_prefers_structured_content(self):
        assert tool_payload({"structuredContent": {"status": "installing"}}) == {
            "status": "installing"
        }

    def test_falls_back_to_json_in_text_content(self):
        result = {"content": [{"type": "text", "text": '{"status":"ok"}'}]}
        assert tool_payload(result) == {"status": "ok"}

    def test_is_error_raises_with_the_message(self):
        result = {"isError": True, "content": [{"type": "text", "text": "device not paired"}]}
        with pytest.raises(McpError, match="device not paired"):
            tool_payload(result)

    def test_non_json_text_raises(self):
        with pytest.raises(McpError, match="non-JSON"):
            tool_payload({"content": [{"type": "text", "text": "plain words"}]})

    def test_empty_result_raises(self):
        with pytest.raises(McpError, match="no usable content"):
            tool_payload({})


class TestSession:
    def test_handshake_captures_session_id_and_server_info(self):
        server = Server()
        session = McpSession(URL, httpx.Client(transport=httpx.MockTransport(server.handler)))
        info = session.open()

        assert info["version"] == "v0.4.6"
        assert session.describe_server() == "atvloadly-mcp v0.4.6"
        # initialize is unauthenticated; the notification carries the session id.
        assert server.session_headers[0] is None
        assert server.session_headers[1] == "SESSION123"
        assert server.requests[1]["method"] == "notifications/initialized"

    def test_jsonrpc_error_becomes_mcperror(self):
        server = Server()
        backend = backend_for(server)
        with pytest.raises(McpError, match="no script for"):
            backend.install_in_progress()

    def test_http_error_becomes_mcperror(self):
        def handler(request):
            return httpx.Response(500, text="boom")

        backend = McpInstallBackend(
            URL,
            device_id="d",
            account_id="a",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        with pytest.raises(McpError, match="HTTP 500"):
            backend.ping()

    def test_connection_error_becomes_mcperror(self):
        def handler(request):
            raise httpx.ConnectError("no route to host")

        backend = McpInstallBackend(
            URL,
            device_id="d",
            account_id="a",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        with pytest.raises(McpError, match="failed"):
            backend.ping()


class TestInstall:
    def test_queues_then_polls_to_completion(self):
        server = Server(
            {
                "install_app": [
                    {
                        "status": "installing",
                        "message": "Install task queued",
                        "selected_device": {"name": "Bedroom"},
                        "selected_account": {"account_email": "b***@example.com"},
                    }
                ],
                "get_install_status": [
                    {"install_in_progress": True, "install_state": "in_progress"},
                    {"install_in_progress": True, "install_state": "in_progress"},
                    {"install_in_progress": False, "install_state": "completed"},
                ],
            }
        )
        result = backend_for(server).install_and_wait(
            "https://example.test/NuvioTV.ipa", timeout_seconds=600, poll_seconds=0
        )

        assert result.completed is True
        assert result.timed_out is False
        assert result.device_name == "Bedroom"
        assert len(server.tool_calls("get_install_status")) == 3

    def test_sends_the_configured_device_and_account(self):
        server = Server(
            {
                "install_app": [{"status": "installing"}],
                "get_install_status": [{"install_in_progress": False}],
            }
        )
        backend_for(server).install_and_wait(
            "https://example.test/a.ipa", timeout_seconds=60, poll_seconds=0
        )
        args = server.tool_calls("install_app")[0]
        assert args["ipa_url"] == "https://example.test/a.ipa"
        assert args["device_id"] == "device-abc"
        assert args["account_id"] == "account-abc"
        assert "remove_extensions" not in args

    def test_remove_extensions_is_opt_in(self):
        server = Server(
            {
                "install_app": [{"status": "installing"}],
                "get_install_status": [{"install_in_progress": False}],
            }
        )
        backend_for(server, remove_extensions=True).install_and_wait(
            "https://example.test/a.ipa", timeout_seconds=60, poll_seconds=0
        )
        assert server.tool_calls("install_app")[0]["remove_extensions"] is True

    @pytest.mark.parametrize("status", ["require_device", "require_account"])
    def test_selection_prompts_are_configuration_errors(self, status):
        server = Server({"install_app": [{"status": status, "message": "pick one"}]})
        with pytest.raises(InstallConfigurationError, match=status):
            backend_for(server).install_and_wait(
                "https://example.test/a.ipa", timeout_seconds=60, poll_seconds=0
            )

    def test_unexpected_status_raises(self):
        server = Server({"install_app": [{"status": "exploded", "message": "?"}]})
        with pytest.raises(McpError, match="unexpected install_app status"):
            backend_for(server).install_and_wait(
                "https://example.test/a.ipa", timeout_seconds=60, poll_seconds=0
            )

    def test_timeout_is_reported_not_raised(self):
        server = Server(
            {
                "install_app": [{"status": "installing"}],
                "get_install_status": [{"install_in_progress": True}],
            }
        )
        result = backend_for(server).install_and_wait(
            "https://example.test/a.ipa", timeout_seconds=0, poll_seconds=0
        )
        assert result.timed_out is True
        assert result.completed is False

    def test_progress_callback_fires_each_poll(self):
        server = Server(
            {
                "install_app": [{"status": "installing"}],
                "get_install_status": [
                    {"install_in_progress": True},
                    {"install_in_progress": False},
                ],
            }
        )
        seen: list[float] = []
        backend_for(server).install_and_wait(
            "https://example.test/a.ipa",
            timeout_seconds=600,
            poll_seconds=0,
            on_progress=seen.append,
        )
        assert len(seen) == 2

    def test_session_is_torn_down_afterwards(self):
        server = Server(
            {
                "install_app": [{"status": "installing"}],
                "get_install_status": [{"install_in_progress": False}],
            }
        )
        backend_for(server).install_and_wait(
            "https://example.test/a.ipa", timeout_seconds=60, poll_seconds=0
        )
        assert server.deletes == 1

    def test_session_is_torn_down_even_on_failure(self):
        server = Server({"install_app": [{"status": "require_device"}]})
        with pytest.raises(InstallConfigurationError):
            backend_for(server).install_and_wait(
                "https://example.test/a.ipa", timeout_seconds=60, poll_seconds=0
            )
        assert server.deletes == 1


class TestStatusAndPing:
    def test_install_in_progress(self):
        server = Server({"get_install_status": [{"install_in_progress": True}]})
        assert backend_for(server).install_in_progress() is True

    def test_idle(self):
        server = Server({"get_install_status": [{"install_in_progress": False}]})
        assert backend_for(server).install_in_progress() is False

    def test_ping_returns_server_identity(self):
        assert backend_for(Server()).ping() == "atvloadly-mcp v0.4.6"
