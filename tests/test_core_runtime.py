"""Unit tests for Slice 1 core runtime: CDP, Reddit API, Browser, and State."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import requests
from websockets.exceptions import ConnectionClosed, WebSocketException
from websockets.frames import Close

import rcurator
from rcurator.browser import BrowserLauncher
from rcurator.cdp_client import (
    CDPClient,
    CDPError,
    RedditSessionData,
    SSRFValidationError,
    acquire_reddit_session,
    activate_following_feed,
    is_reddit_url,
    validate_loopback_ws_url,
)
from rcurator.reddit_api import RedditAPIClient, validate_feed_name
from rcurator.state import SnapshotManager


class _FakeWebSocket:
    """In-memory fake WebSocket supporting scripted RPC responses & event injection."""

    def __init__(self, handler) -> None:
        self._handler = handler
        self._outgoing: asyncio.Queue[str] = asyncio.Queue()
        self.sent_frames: list[dict] = []
        self.closed = False

    async def send(self, raw: str) -> None:
        msg = json.loads(raw)
        self.sent_frames.append(msg)
        for frame in self._handler(msg):
            await self._outgoing.put(json.dumps(frame))

    async def recv(self) -> str:
        return await self._outgoing.get()

    async def close(self) -> None:
        self.closed = True


def _mock_response(
    status_code: int = 200,
    json_data=None,
    headers: dict[str, str] | None = None,
    text: str = "",
) -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.text = text if text else (json.dumps(json_data) if json_data is not None else "")
    if json_data is not None:
        resp.json.return_value = json_data
    else:
        resp.json.side_effect = ValueError("No JSON")
    if status_code >= 400:
        err = requests.HTTPError(f"HTTP {status_code}", response=resp)
        resp.raise_for_status.side_effect = err
    else:
        resp.raise_for_status.return_value = None
    return resp


class TestPackageMetadata(unittest.TestCase):
    def test_version_metadata(self) -> None:
        self.assertEqual(rcurator.__version__, "2.0.0")


class TestCDPClient(unittest.TestCase):
    def test_ssrf_and_hostname_guards(self) -> None:
        for valid_ws in (
            "ws://127.0.0.1:9222/devtools/page/ABC",
            "ws://localhost:9222/devtools/browser/1",
            "ws://[::1]:9222/devtools/page/XYZ",
        ):
            self.assertEqual(validate_loopback_ws_url(valid_ws), valid_ws)

        for bad_ws in (
            "",
            "http://127.0.0.1:9222/devtools/page/1",
            "wss://127.0.0.1:9222/devtools/page/1",
            "file:///etc/passwd",
            "ws://evil.com:9222/devtools/page/1",
            "ws://169.254.169.254/latest/meta-data",
            "ws://127.0.0.1@evil.com:9222/devtools/page/1",
            "ws://0.0.0.0:9222/devtools/page/1",
        ):
            with self.assertRaises((SSRFValidationError, ValueError)):
                CDPClient(bad_ws)

        for good_url in (
            "https://www.reddit.com/",
            "https://old.reddit.com/r/linux",
            "https://reddit.com/prefs",
            "http://sh.reddit.com/",
        ):
            self.assertTrue(is_reddit_url(good_url), msg=f"Expected valid: {good_url}")

        for spoofed_url in (
            "",
            "https://evil-reddit.com/",
            "https://evil.com/?next=https://www.reddit.com",
            "https://reddit.com.evil.com/",
            "https://reddit.com@evil.com/",
            "javascript:alert('reddit.com')",
            "file:///tmp/reddit.com.html",
        ):
            self.assertFalse(is_reddit_url(spoofed_url), msg=f"Expected rejected: {spoofed_url}")

    def test_unsolicited_events_and_monotonic_ids(self) -> None:
        def handler(req: dict) -> list[dict]:
            cid = req["id"]
            if req["method"] == "Runtime.evaluate":
                return [
                    {"method": "Network.requestWillBeSent", "params": {"url": "https://www.reddit.com"}},
                    {"method": "Page.frameNavigated", "params": {}},
                    {"id": cid + 99, "result": {"stale": True}},
                    {"id": cid, "result": {"result": {"value": "ok"}}},
                ]
            return [{"id": cid, "result": {"done": True}}]

        fake_ws = _FakeWebSocket(handler)

        async def run() -> tuple[dict, dict]:
            with patch("rcurator.cdp_client.websockets.connect", new=AsyncMock(return_value=fake_ws)) as mock_conn:
                async with CDPClient("ws://127.0.0.1:9222/devtools/page/1") as client:
                    r1 = await client.send("Runtime.evaluate", {"expression": "'ok'"})
                    r2 = await client.send("Page.reload", {})
                mock_conn.assert_awaited_once_with(
                    "ws://127.0.0.1:9222/devtools/page/1",
                    origin=None,
                    max_size=16_777_216,
                    open_timeout=5.0,
                    close_timeout=2.0,
                )
                return r1, r2

        res1, res2 = asyncio.run(run())
        self.assertEqual(res1, {"result": {"value": "ok"}})
        self.assertEqual(res2, {"done": True})
        self.assertEqual([f["id"] for f in fake_ws.sent_frames], [1, 2])
        self.assertTrue(fake_ws.closed)

    def test_rpc_error_raises_cdp_error(self) -> None:
        fake_ws = _FakeWebSocket(
            lambda req: [{"id": req["id"], "error": {"code": -32000, "message": "Context destroyed"}}]
        )

        async def run() -> None:
            with patch("rcurator.cdp_client.websockets.connect", new=AsyncMock(return_value=fake_ws)):
                async with CDPClient("ws://127.0.0.1:9222/devtools/page/1") as client:
                    await client.send("Runtime.evaluate", {"expression": "1+1"})

        with self.assertRaises(CDPError) as ctx:
            asyncio.run(run())
        self.assertEqual(ctx.exception.error["code"], -32000)

    def test_send_timeout_ignores_events_then_raises(self) -> None:
        class _SilentWebSocket:
            async def send(self, raw: str) -> None:
                return None

            async def recv(self) -> str:
                await asyncio.sleep(1.0)
                return json.dumps({"method": "Page.loadEventFired"})

            async def close(self) -> None:
                return None

        async def run() -> None:
            with patch("rcurator.cdp_client.websockets.connect", new=AsyncMock(return_value=_SilentWebSocket())):
                async with CDPClient("ws://localhost:9222/devtools/page/1") as client:
                    await client.send("Runtime.evaluate", {"expression": "1"}, timeout=0.05)

        with self.assertRaises((asyncio.TimeoutError, TimeoutError)):
            asyncio.run(run())

    def test_acquire_session_creates_tab_and_falls_back_to_cookie_after_reload(self) -> None:
        list_calls = {"n": 0}

        def fake_get(url: str, headers=None, timeout=None):
            if url.endswith("/json/list"):
                list_calls["n"] += 1
                if list_calls["n"] == 1:
                    return _mock_response(200, [{"id": "BLANK1", "type": "page", "url": "about:blank", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/BLANK1"}])
                return _mock_response(
                    200,
                    [
                        {
                            "id": "REDDIT1",
                            "type": "page",
                            "url": "https://www.reddit.com/",
                            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/REDDIT1",
                        }
                    ],
                )
            if url.endswith("/json/version"):
                return _mock_response(200, {"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/ROOT"})
            if "oauth.reddit.com/api/v1/me" in url:
                self.assertEqual(headers["Authorization"], "Bearer cookie-token-xyz")
                return _mock_response(200, {"name": "FedoraDev"})
            raise AssertionError(f"Unexpected GET {url}")

        state = {"reloaded": False}

        def ws_handler(req: dict) -> list[dict]:
            cid = req["id"]
            method = req["method"]
            if method == "Target.createTarget":
                return [{"id": cid, "result": {"targetId": "REDDIT1"}}]
            if method == "Page.reload":
                state["reloaded"] = True
                return [{"id": cid, "result": {}}]
            if method == "Runtime.evaluate":
                expr = req["params"]["expression"]
                if expr == "document.readyState":
                    return [{"id": cid, "result": {"result": {"value": "complete"}}}]
                return [{"id": cid, "result": {"result": {"value": None}}}]
            if method == "Network.getCookies":
                if not state["reloaded"]:
                    return [{"id": cid, "result": {"cookies": []}}]
                return [
                    {
                        "id": cid,
                        "result": {
                            "cookies": [
                                {"name": "token_v2", "domain": ".reddit.com", "value": "cookie-token-xyz"}
                            ]
                        },
                    }
                ]
            return [{"id": cid, "result": {}}]

        async def fast_sleep(_delay: float) -> None:
            return None

        with (
            patch("rcurator.cdp_client.requests.get", side_effect=fake_get),
            patch(
                "rcurator.cdp_client.websockets.connect",
                side_effect=lambda *a, **kw: AsyncMock(return_value=_FakeWebSocket(ws_handler))(),
            ),
            patch("rcurator.cdp_client.asyncio.sleep", side_effect=fast_sleep),
        ):
            session = acquire_reddit_session(cdp_port=9222)

        self.assertEqual(
            session,
            RedditSessionData(
                username="FedoraDev",
                access_token="cookie-token-xyz",
                target_id="REDDIT1",
                ws_url="ws://127.0.0.1:9222/devtools/page/REDDIT1",
            ),
        )
        self.assertTrue(state["reloaded"])

    def test_acquire_session_localstorage_fast_path(self) -> None:
        def fake_get(url: str, headers=None, timeout=None):
            if url.endswith("/json/list"):
                return _mock_response(
                    200,
                    [
                        {
                            "id": "RED99",
                            "type": "page",
                            "url": "https://old.reddit.com/",
                            "webSocketDebuggerUrl": "ws://localhost:9222/devtools/page/RED99",
                        }
                    ],
                )
            if "oauth.reddit.com/api/v1/me" in url:
                self.assertEqual(headers["Authorization"], "Bearer ls-token-abc")
                return _mock_response(200, {"name": "FastUser"})
            raise AssertionError(f"Unexpected GET {url}")

        def ws_handler(req: dict) -> list[dict]:
            cid = req["id"]
            if req["method"] == "Page.reload":
                raise AssertionError("Should not reload when localStorage token is already valid")
            return [{"id": cid, "result": {"result": {"value": "ls-token-abc"}}}]

        with (
            patch("rcurator.cdp_client.requests.get", side_effect=fake_get),
            patch(
                "rcurator.cdp_client.websockets.connect",
                side_effect=lambda *a, **kw: AsyncMock(return_value=_FakeWebSocket(ws_handler))(),
            ),
        ):
            session = acquire_reddit_session(cdp_port=9222)

        self.assertEqual(session.username, "FastUser")
        self.assertEqual(session.access_token, "ls-token-abc")
        self.assertEqual(session.target_id, "RED99")

    def test_activate_following_feed_shadow_dom(self) -> None:
        def ws_handler(req: dict) -> list[dict]:
            cid = req["id"]
            method = req["method"]
            if method == "Page.navigate":
                self.assertEqual(req["params"]["url"], "https://www.reddit.com/?feed=following")
                return [{"id": cid, "result": {"frameId": "F1"}}]
            if method == "Runtime.evaluate":
                if req["params"]["expression"] == "document.readyState":
                    return [{"id": cid, "result": {"result": {"value": "complete"}}}]
                return [
                    {
                        "id": cid,
                        "result": {
                            "result": {
                                "value": {
                                    "matched": True,
                                    "method": "shadow_dom_#feed-tab-2",
                                    "already_selected": False,
                                }
                            }
                        },
                    }
                ]
            return [{"id": cid, "result": {}}]

        fake_ws = _FakeWebSocket(ws_handler)
        with patch("rcurator.cdp_client.websockets.connect", new=AsyncMock(return_value=fake_ws)):
            result = activate_following_feed("ws://127.0.0.1:9222/devtools/page/REDDIT1")

        self.assertEqual(set(result.keys()), {"success", "method_used", "message", "elapsed_ms"})
        self.assertTrue(result["success"])
        self.assertEqual(result["method_used"], "shadow_dom_#feed-tab-2")
        self.assertIsInstance(result["elapsed_ms"], int)

    def test_expired_localstorage_falls_back_to_token_v2_cookie(self) -> None:
        seen_tokens: list[str] = []

        def fake_get(url: str, headers=None, timeout=None):
            if url.endswith("/json/list"):
                return _mock_response(
                    200,
                    [
                        {
                            "id": "RED1",
                            "type": "page",
                            "url": "https://www.reddit.com/",
                            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/RED1",
                        }
                    ],
                )
            if "oauth.reddit.com/api/v1/me" in url:
                auth_header = headers["Authorization"]
                seen_tokens.append(auth_header)
                if auth_header == "Bearer expired-ls-tok":
                    return _mock_response(401, {"message": "Unauthorized"})
                if auth_header == "Bearer fresh-cookie-v2":
                    return _mock_response(200, {"name": "CookieFallbackUser"})
            raise AssertionError(f"Unexpected GET {url}")

        def ws_handler(req: dict) -> list[dict]:
            cid = req["id"]
            if req["method"] == "Page.reload":
                raise AssertionError("Should not reload when token_v2 cookie already validates")
            if req["method"] == "Runtime.evaluate":
                return [{"id": cid, "result": {"result": {"value": "expired-ls-tok"}}}]
            if req["method"] == "Network.getCookies":
                return [
                    {
                        "id": cid,
                        "result": {
                            "cookies": [
                                {"name": "token_v2", "domain": ".reddit.com", "value": "fresh-cookie-v2"}
                            ]
                        },
                    }
                ]
            return [{"id": cid, "result": {}}]

        with (
            patch("rcurator.cdp_client.requests.get", side_effect=fake_get),
            patch(
                "rcurator.cdp_client.websockets.connect",
                side_effect=lambda *a, **kw: AsyncMock(return_value=_FakeWebSocket(ws_handler))(),
            ),
        ):
            session = acquire_reddit_session(cdp_port=9222)

        self.assertEqual(session.username, "CookieFallbackUser")
        self.assertEqual(session.access_token, "fresh-cookie-v2")
        self.assertEqual(seen_tokens, ["Bearer expired-ls-tok", "Bearer fresh-cookie-v2"])

    def test_validate_reddit_token_network_error_vs_401(self) -> None:
        from rcurator.cdp_client import _validate_reddit_token

        with patch("rcurator.cdp_client.requests.get", return_value=_mock_response(401, {"error": "invalid"})):
            self.assertIsNone(_validate_reddit_token("bad-tok"))
        with patch("rcurator.cdp_client.requests.get", return_value=_mock_response(403, {"error": "forbidden"})):
            self.assertIsNone(_validate_reddit_token("bad-tok"))
        with patch(
            "rcurator.cdp_client.requests.get",
            side_effect=requests.ConnectionError("DNS unreachable"),
        ):
            with self.assertRaises(CDPError) as ctx:
                _validate_reddit_token("some-tok")
            self.assertIn("oauth.reddit.com", str(ctx.exception))

    def test_websocket_exception_and_malformed_json_wrapped_in_cdp_error(self) -> None:
        async def run_connect_fail() -> None:
            with patch(
                "rcurator.cdp_client.websockets.connect",
                side_effect=WebSocketException("handshake refused"),
            ):
                async with CDPClient("ws://127.0.0.1:9222/devtools/page/1"):
                    pass

        with self.assertRaises(CDPError):
            asyncio.run(run_connect_fail())

        class _BrokenWS:
            def __init__(self, mode: str) -> None:
                self.mode = mode

            async def send(self, raw: str) -> None:
                if self.mode == "send_closed":
                    raise ConnectionClosed(Close(1006, "dropped"), None)

            async def recv(self) -> str:
                if self.mode == "bad_json":
                    return "{not-valid-json"
                raise ConnectionClosed(Close(1006, "dropped"), None)

            async def close(self) -> None:
                return None

        for mode in ("send_closed", "bad_json"):
            async def run_send_fail(m: str = mode) -> None:
                with patch(
                    "rcurator.cdp_client.websockets.connect",
                    new=AsyncMock(return_value=_BrokenWS(m)),
                ):
                    async with CDPClient("ws://127.0.0.1:9222/devtools/page/1") as client:
                        await client.send("Runtime.evaluate", {"expression": "1"})

            with self.assertRaises(CDPError):
                asyncio.run(run_send_fail())

        with patch(
            "rcurator.cdp_client.websockets.connect",
            new=AsyncMock(return_value=_BrokenWS("send_closed")),
        ):
            res = activate_following_feed("ws://127.0.0.1:9222/devtools/page/1")
            self.assertFalse(res["success"])
            self.assertEqual(res["method_used"], "error")

    def test_activate_following_feed_polls_shreddit_hydration(self) -> None:
        walker_calls = {"n": 0}

        def ws_handler(req: dict) -> list[dict]:
            cid = req["id"]
            method = req["method"]
            if method == "Page.navigate":
                return [{"id": cid, "result": {"frameId": "F1"}}]
            if method == "Runtime.evaluate":
                if req["params"]["expression"] == "document.readyState":
                    return [{"id": cid, "result": {"result": {"value": "complete"}}}]
                walker_calls["n"] += 1
                if walker_calls["n"] < 3:
                    return [{"id": cid, "result": {"result": {"value": {"matched": False, "method": "not_found"}}}}]
                return [
                    {
                        "id": cid,
                        "result": {
                            "result": {
                                "value": {"matched": True, "method": "shadow_dom_#feed-tab-2"}
                            }
                        },
                    }
                ]
            return [{"id": cid, "result": {}}]

        async def fast_sleep(_d: float) -> None:
            return None

        fake_ws = _FakeWebSocket(ws_handler)
        with (
            patch("rcurator.cdp_client.websockets.connect", new=AsyncMock(return_value=fake_ws)),
            patch("rcurator.cdp_client.asyncio.sleep", side_effect=fast_sleep),
        ):
            res = activate_following_feed("ws://127.0.0.1:9222/devtools/page/REDDIT1")

        self.assertTrue(res["success"])
        self.assertEqual(walker_calls["n"], 3)
        self.assertEqual(res["method_used"], "shadow_dom_#feed-tab-2")


class TestRedditAPIClient(unittest.TestCase):
    def test_pagination_over_250_subs_and_raw_json(self) -> None:
        session = MagicMock(spec=requests.Session)
        session.headers = {}
        seen_params: list[dict] = []

        def make_children(start: int, count: int) -> list[dict]:
            return [
                {
                    "data": {
                        "display_name": f"Sub_{i}",
                        "name": f"t5_{i}",
                        "subscribers": 1000 + i,
                        "over18": False,
                        "public_description": f"Desc {i}",
                        "url": f"/r/Sub_{i}/",
                    }
                }
                for i in range(start, start + count)
            ]

        pages = [
            _mock_response(200, {"data": {"children": make_children(0, 100), "after": "cursor_1"}}),
            _mock_response(200, {"data": {"children": make_children(100, 100), "after": "cursor_2"}}),
            _mock_response(200, {"data": {"children": make_children(200, 55), "after": None}}),
        ]

        def fake_request(method, url, params=None, data=None, timeout=None):
            seen_params.append(dict(params or {}))
            return pages.pop(0)

        session.request.side_effect = fake_request
        client = RedditAPIClient("tok123", "Tester", session=session)
        subs = client.fetch_all_subscriptions()

        self.assertEqual(len(subs), 255)
        self.assertIn("sub_0", subs)
        self.assertIn("sub_254", subs)
        self.assertEqual(subs["sub_42"]["name"], "Sub_42")
        self.assertTrue(all(p.get("raw_json") == 1 for p in seen_params))
        self.assertEqual(seen_params[1]["after"], "cursor_1")
        self.assertEqual(seen_params[2]["after"], "cursor_2")

    def test_pagination_cursor_cycle_guard(self) -> None:
        session = MagicMock(spec=requests.Session)
        session.headers = {}
        responses = [
            _mock_response(200, {"data": {"children": [{"data": {"display_name": "rust"}}], "after": "loop_cur"}}),
            _mock_response(200, {"data": {"children": [{"data": {"display_name": "linux"}}], "after": "loop_cur"}}),
        ]
        session.request.side_effect = responses

        client = RedditAPIClient("tok123", "Tester", session=session)
        subs = client.fetch_all_subscriptions()
        self.assertEqual(sorted(subs.keys()), ["linux", "rust"])
        self.assertEqual(session.request.call_count, 2)

    def test_429_retry_after_and_proactive_ratelimit(self) -> None:
        session = MagicMock(spec=requests.Session)
        session.headers = {}
        session.request.side_effect = [
            _mock_response(429, {"error": "rate_limited"}, headers={"Retry-After": "2.5"}),
            _mock_response(503, {"error": "unavailable"}),
            _mock_response(
                200,
                {"data": {"children": [{"data": {"display_name": "Python"}}]}},
                headers={"x-ratelimit-remaining": "1.0", "x-ratelimit-reset": "4.0"},
            ),
        ]

        client = RedditAPIClient("tok123", "Tester", max_retries=4, session=session)
        with patch("rcurator.reddit_api.time.sleep") as mock_sleep:
            results = client.search_subreddits("python", limit=5)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["name"], "Python")
        sleep_args = [call.args[0] for call in mock_sleep.call_args_list]
        self.assertIn(2.5, sleep_args)
        self.assertIn(4.0, sleep_args)
        self.assertEqual(len(sleep_args), 3)

    def test_sync_subscriptions_delta_and_opt_in_prune(self) -> None:
        client = RedditAPIClient("tok123", "Tester")
        with (
            patch.object(
                client,
                "fetch_all_subscriptions",
                return_value={
                    "linux": {"name": "linux"},
                    "oldsub": {"name": "OldSub"},
                    "keepsub": {"name": "KeepSub"},
                },
            ),
            patch.object(client, "_mutate_subscription", return_value=(True, 200, "")) as mock_mut,
        ):
            res_no_prune = client.sync_subscriptions(["Linux", "rust"], dry_run=False, prune=False)
            self.assertEqual(res_no_prune["already_subscribed"], ["linux"])
            self.assertEqual(res_no_prune["subscribed"], ["rust"])
            self.assertEqual(res_no_prune["unsubscribed"], [])
            self.assertEqual(sorted(res_no_prune["kept_unmanaged"]), ["KeepSub", "OldSub"])
            mock_mut.assert_called_once_with("sub", "rust")

            mock_mut.reset_mock()
            res_prune = client.sync_subscriptions(
                ["Linux", "rust"], dry_run=False, prune=True, keep={"keepsub"}
            )
            self.assertEqual(res_prune["unsubscribed"], ["OldSub"])
            self.assertEqual(res_prune["kept_unmanaged"], ["KeepSub"])

    def test_custom_feed_validation_splitting_and_put_post_fallback(self) -> None:
        for bad in ("", "Bad-Name", "../escape", "a" * 51, "has space"):
            with self.assertRaises(ValueError):
                validate_feed_name(bad)

        session = MagicMock(spec=requests.Session)
        session.headers = {}
        calls: list[tuple[str, str, int]] = []

        def fake_request(method, url, params=None, data=None, timeout=None):
            model = json.loads(data["model"])
            count = len(model["subreddits"])
            calls.append((method, url, count))
            if method == "PUT":
                return _mock_response(404, {"error": "Not Found"})
            return _mock_response(200, {"data": {"path": url.split("/api/multi", 1)[1]}})

        session.request.side_effect = fake_request
        client = RedditAPIClient("tok123", "GauthamUser", session=session)
        subs_230 = [f"sub_{i}" for i in range(230)]
        res = client.sync_custom_feeds(
            [{"feed_name": "systems_core", "display_name": "Systems Core", "subreddits": subs_230}]
        )

        self.assertEqual(list(res.keys()), ["systems_core", "systems_core_2", "systems_core_3"])
        self.assertEqual(len(res["systems_core"]["subreddits"]), 100)
        self.assertEqual(len(res["systems_core_2"]["subreddits"]), 100)
        self.assertEqual(len(res["systems_core_3"]["subreddits"]), 30)
        self.assertEqual(res["systems_core"]["path"], "/user/gauthamuser/m/systems_core")
        self.assertTrue(all(v["status"] == "success" for v in res.values()))
        self.assertEqual(calls[0][:2], ("PUT", "https://oauth.reddit.com/api/multi/user/gauthamuser/m/systems_core"))
        self.assertEqual(calls[1][:2], ("POST", "https://oauth.reddit.com/api/multi/user/gauthamuser/m/systems_core"))

    def test_fetch_custom_feeds_and_dry_run_sync(self) -> None:
        session = MagicMock(spec=requests.Session)
        session.headers = {}
        session.request.return_value = _mock_response(
            200,
            [
                {
                    "kind": "LabeledMulti",
                    "data": {
                        "name": "systems_core",
                        "display_name": "Systems Core",
                        "path": "/user/tester/m/systems_core",
                        "description_md": "Core systems",
                        "icon_name": "programming",
                        "visibility": "private",
                        "subreddits": [{"name": "rust"}, {"name": "linux"}],
                    },
                }
            ],
        )
        client = RedditAPIClient("tok123", "Tester", session=session)
        feeds = client.fetch_custom_feeds()
        self.assertEqual(len(feeds), 1)
        self.assertEqual(feeds[0]["feed_name"], "systems_core")
        self.assertEqual(feeds[0]["subreddits"], ["rust", "linux"])

        dry = client.sync_custom_feeds(
            [{"feed_name": "systems_core", "subreddits": ["rust"]}], dry_run=True
        )
        self.assertEqual(dry["systems_core"]["status"], "dry_run")
        self.assertIsNone(dry["systems_core"]["http_code"])
        self.assertEqual(session.request.call_count, 1)

    def test_sync_custom_feeds_skips_post_on_400_401_403_and_invalid_feed_name(self) -> None:
        for code in (400, 401, 403):
            session = MagicMock(spec=requests.Session)
            session.headers = {}
            methods: list[str] = []

            def fake_req(method, url, params=None, data=None, timeout=None, c=code):
                methods.append(method)
                return _mock_response(c, {"error": f"http_{c}"}, text=f"client error {c}")

            session.request.side_effect = fake_req
            client = RedditAPIClient("tok123", "Tester", max_retries=0, session=session)
            res = client.sync_custom_feeds(
                [
                    {"feed_name": "Bad-Name!", "subreddits": ["rust"]},
                    {"feed_name": "valid_feed", "subreddits": ["linux"]},
                ]
            )
            self.assertEqual(res["Bad-Name!"]["status"], "failed")
            self.assertIn("Invalid custom feed name", res["Bad-Name!"]["error"])
            self.assertEqual(res["valid_feed"]["status"], "failed")
            self.assertEqual(res["valid_feed"]["http_code"], code)
            self.assertEqual(methods, ["PUT"])

    def test_proactive_ratelimit_logs_sleep(self) -> None:
        session = MagicMock(spec=requests.Session)
        session.headers = {}
        session.request.return_value = _mock_response(
            200,
            {"data": {"children": []}},
            headers={"x-ratelimit-remaining": "1.0", "x-ratelimit-reset": "3.0"},
        )
        client = RedditAPIClient("tok123", "Tester", session=session)
        with (
            patch("rcurator.reddit_api.time.sleep") as mock_sleep,
            self.assertLogs("rcurator.reddit_api", level="INFO") as logs,
        ):
            client.search_subreddits("rust")
        mock_sleep.assert_called_once_with(3.0)
        self.assertTrue(any("proactive rate-limit" in line.lower() for line in logs.output))


class TestBrowserLauncher(unittest.TestCase):
    def test_detect_and_flatpak_command_with_wayland(self) -> None:
        def fake_which(name: str):
            if name == "flatpak":
                return "/usr/bin/flatpak"
            if name == "google-chrome":
                return "/usr/bin/google-chrome"
            return None

        def fake_run(cmd, **_kwargs):
            proc = MagicMock()
            proc.returncode = 0 if cmd[2] == "com.brave.Browser" else 1
            return proc

        with (
            patch("rcurator.browser.shutil.which", side_effect=fake_which),
            patch("rcurator.browser.subprocess.run", side_effect=fake_run),
        ):
            browsers = BrowserLauncher.detect_browsers()

        self.assertEqual(
            [b["browser_id"] for b in browsers],
            ["google-chrome", "com.brave.Browser"],
        )

        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "chrome-profile"
            mock_proc = MagicMock()
            mock_proc.pid = 4242
            mock_proc.poll.return_value = None

            with (
                patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-0"}, clear=False),
                patch.object(BrowserLauncher, "detect_browsers", return_value=browsers),
                patch("rcurator.browser._probe_cdp_ready", side_effect=[False, True]),
                patch("rcurator.browser.subprocess.Popen", return_value=mock_proc) as mock_popen,
            ):
                launch_res = BrowserLauncher.launch(
                    port=9333,
                    preferred="com.brave.Browser",
                    user_data_dir=profile,
                )

            self.assertEqual(
                set(launch_res.keys()),
                {
                    "launched",
                    "reused_existing",
                    "browser_id",
                    "command",
                    "pid",
                    "port",
                    "cdp_http_url",
                    "user_data_dir",
                },
            )
            self.assertTrue(launch_res["launched"])
            self.assertFalse(launch_res["reused_existing"])
            self.assertEqual(launch_res["pid"], 4242)
            cmd = launch_res["command"]
            self.assertEqual(cmd[:4], ["/usr/bin/flatpak", "run", f"--filesystem={profile.resolve()}:create", "com.brave.Browser"])
            self.assertIn("--remote-debugging-port=9333", cmd)
            self.assertIn("--remote-debugging-address=127.0.0.1", cmd)
            self.assertIn("--remote-allow-origins=*", cmd)
            self.assertIn("--ozone-platform-hint=auto", cmd)
            mock_popen.assert_called_once()
            self.assertEqual(stat.S_IMODE(profile.stat().st_mode), 0o700)

    def test_reuse_existing_cdp_instance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "profile"
            with (
                patch("rcurator.browser._probe_cdp_ready", return_value=True),
                patch("rcurator.browser.subprocess.Popen") as mock_popen,
            ):
                res = BrowserLauncher.launch(port=9222, user_data_dir=profile)
            self.assertFalse(res["launched"])
            self.assertTrue(res["reused_existing"])
            self.assertEqual(res["browser_id"], "existing")
            self.assertEqual(res["command"], [])
            mock_popen.assert_not_called()

    def test_immediate_exit_captures_bounded_stderr_and_timeout_terminates(self) -> None:
        browsers = [{"browser_id": "chromium", "kind": "native", "executable": "/usr/bin/chromium", "name": "Chromium"}]
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "prof"

            def fake_popen_crash(cmd, stdout=None, stderr=None, stdin=None, start_new_session=None):
                stderr.write(b"FATAL:zygote_host_impl_linux.cc: sandbox error\n")
                stderr.flush()
                proc = MagicMock()
                proc.poll.return_value = 1
                return proc

            with (
                patch.object(BrowserLauncher, "detect_browsers", return_value=browsers),
                patch("rcurator.browser._probe_cdp_ready", return_value=False),
                patch("rcurator.browser.subprocess.Popen", side_effect=fake_popen_crash),
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    BrowserLauncher.launch(port=9222, user_data_dir=profile, wait_timeout=1.0)
                self.assertIn("sandbox error", str(ctx.exception))

            mock_proc = MagicMock()
            mock_proc.poll.return_value = None
            with (
                patch.object(BrowserLauncher, "detect_browsers", return_value=browsers),
                patch("rcurator.browser._probe_cdp_ready", return_value=False),
                patch("rcurator.browser.subprocess.Popen", return_value=mock_proc),
                patch("rcurator.browser.time.sleep"),
            ):
                with self.assertRaises(TimeoutError):
                    BrowserLauncher.launch(port=9222, user_data_dir=profile, wait_timeout=0.5)
            mock_proc.terminate.assert_called_once()
            mock_proc.wait.assert_called_once()


class TestSnapshotManager(unittest.TestCase):
    def test_snapshot_permissions_listing_latest_and_traversal_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "snaps"
            mgr = SnapshotManager(root)

            s1 = mgr.create_snapshot("Gautham", ["linux", "rust"], [], {"name": "First"})
            s2 = mgr.create_snapshot("Gautham", ["linux", "rust", "golang"], [{"feed_name": "core", "subreddits": ["rust"]}], {"name": "Second"})

            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(Path(s1["path"]).stat().st_mode), 0o600)

            listed = mgr.list_snapshots()
            self.assertEqual([x["snapshot_id"] for x in listed], [s2["snapshot_id"], s1["snapshot_id"]])

            latest = mgr.load_snapshot("latest")
            self.assertEqual(latest["snapshot_id"], s2["snapshot_id"])
            self.assertEqual(latest["subscriptions"], ["linux", "rust", "golang"])

            for bad_id in ("../etc/passwd", "..", "nested/snap", "a\\b", "snap\x00bad", ".hidden"):
                with self.assertRaises(ValueError):
                    mgr.load_snapshot(bad_id)

            with self.assertRaises(FileNotFoundError):
                mgr.load_snapshot("snapshot_nonexistent_123")

    def test_compute_diff_and_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = SnapshotManager(Path(tmp) / "snaps")
            snap = mgr.create_snapshot(
                "Gautham",
                ["linux", "unixporn"],
                [{"feed_name": "kept_feed", "display_name": "Kept", "subreddits": ["linux"]}],
                {"name": "Baseline"},
            )

            manifest = {
                "name": "Target",
                "anchor_subreddits": [{"name": "linux"}, {"name": "AskHistorians"}],
                "domain_priming_subreddits": [{"name": "rust"}],
                "custom_feeds": [
                    {"feed_name": "kept_feed", "subreddits": ["linux", "rust"]},
                    {"feed_name": "new_feed", "subreddits": ["rust"]},
                ],
            }
            diff = mgr.compute_diff(
                manifest,
                live_subs=["Linux", "ExtraSub", "KeepMe"],
                live_feeds=[{"feed_name": "kept_feed", "subreddits": ["linux", "oldsub"]}],
                prune=True,
                keep={"keepme"},
            )
            self.assertTrue(diff["has_changes"])
            self.assertEqual(diff["already_subscribed"], ["Linux"])
            self.assertEqual(diff["to_subscribe"], ["AskHistorians", "rust"])
            self.assertEqual(diff["to_unsubscribe"], ["ExtraSub"])
            self.assertEqual(diff["unmanaged_kept"], ["KeepMe"])
            self.assertEqual(diff["custom_feeds"]["kept_feed"]["action"], "update")
            self.assertEqual(diff["custom_feeds"]["new_feed"]["action"], "create")

            mock_api = MagicMock()
            mock_api.username = "Gautham"
            mock_api.fetch_custom_feeds.return_value = [
                {"feed_name": "kept_feed", "path": "/user/gautham/m/kept_feed"},
                {"feed_name": "extra_feed", "path": "/user/gautham/m/extra_feed"},
            ]
            mock_api.sync_subscriptions.return_value = {
                "dry_run": False,
                "already_subscribed": ["linux"],
                "subscribed": ["unixporn"],
                "unsubscribed": ["rust"],
                "kept_unmanaged": [],
                "failed": [],
            }
            mock_api.sync_custom_feeds.return_value = {
                "kept_feed": {"status": "success", "http_code": 200, "endpoint": "/api/multi/user/gautham/m/kept_feed", "path": "/user/gautham/m/kept_feed", "subreddits": ["linux"], "error": None}
            }
            mock_api.delete_custom_feed.return_value = True

            rb = mgr.rollback("latest", mock_api, dry_run=False)
            self.assertEqual(rb["snapshot_id"], snap["snapshot_id"])
            self.assertTrue(rb["success"])
            self.assertEqual(rb["restored_subscriptions"], ["unixporn"])
            self.assertEqual(rb["removed_subscriptions"], ["rust"])
            self.assertEqual(rb["deleted_custom_feeds"], ["extra_feed"])
            mock_api.sync_subscriptions.assert_called_once_with(
                desired=["linux", "unixporn"], dry_run=False, prune=True
            )
            mock_api.delete_custom_feed.assert_called_once_with("/user/gautham/m/extra_feed", dry_run=False)

            mock_api.delete_custom_feed.reset_mock()
            rb_dry = mgr.rollback("latest", mock_api, dry_run=True)
            self.assertTrue(rb_dry["dry_run"])
            self.assertEqual(rb_dry["deleted_custom_feeds"], ["extra_feed"])
            mock_api.delete_custom_feed.assert_not_called()

    def test_rollback_cross_account_guard_and_partitioned_case_insensitive_feeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = SnapshotManager(Path(tmp) / "snaps")
            subs_125 = [f"sub_{i}" for i in range(125)]
            mgr.create_snapshot(
                "Alice",
                ["linux"],
                [{"feed_name": "big_feed", "display_name": "Big Feed", "subreddits": subs_125}],
                {"name": "Snap"},
            )

            other_api = MagicMock()
            other_api.username = "Bob"
            with self.assertRaises(ValueError) as ctx:
                mgr.rollback("latest", other_api)
            self.assertIn("Account mismatch", str(ctx.exception))
            other_api.sync_subscriptions.assert_not_called()

            # Case-insensitive matching + >100-sub _2 feed preservation + unmanaged_custom_feeds
            manifest = {
                "name": "Target",
                "anchor_subreddits": [{"name": "linux"}],
                "domain_priming_subreddits": [],
                "custom_feeds": [{"feed_name": "big_feed", "subreddits": subs_125}],
            }
            diff = mgr.compute_diff(
                manifest,
                live_subs=["linux"],
                live_feeds=[
                    {"feed_name": "BIG_FEED", "subreddits": subs_125[:100]},
                    {"feed_name": "big_feed_2", "subreddits": subs_125[100:]},
                    {"feed_name": "side_feed", "subreddits": ["golang"]},
                ],
            )
            self.assertEqual(diff["custom_feeds"]["big_feed"]["action"], "unchanged")
            self.assertEqual(diff["custom_feeds"]["big_feed_2"]["action"], "unchanged")
            self.assertEqual(diff["unmanaged_custom_feeds"], ["side_feed"])

            alice_api = MagicMock()
            alice_api.username = "alice"
            alice_api.fetch_custom_feeds.return_value = [
                {"feed_name": "BIG_FEED", "path": "/user/alice/m/BIG_FEED"},
                {"feed_name": "big_feed_2", "path": "/user/alice/m/big_feed_2"},
                {"feed_name": "extra_feed", "path": "/user/alice/m/extra_feed"},
            ]
            alice_api.sync_subscriptions.return_value = {
                "dry_run": False,
                "already_subscribed": ["linux"],
                "subscribed": [],
                "unsubscribed": [],
                "kept_unmanaged": [],
                "failed": [],
            }
            alice_api.sync_custom_feeds.return_value = {
                "big_feed": {"status": "success"},
                "big_feed_2": {"status": "success"},
            }
            alice_api.delete_custom_feed.return_value = True

            rb = mgr.rollback("latest", alice_api, dry_run=False)
            self.assertTrue(rb["success"])
            self.assertEqual(rb["deleted_custom_feeds"], ["extra_feed"])
            alice_api.delete_custom_feed.assert_called_once_with("/user/alice/m/extra_feed", dry_run=False)


if __name__ == "__main__":
    unittest.main()
