"""Hardened Chrome DevTools Protocol (CDP) client and Reddit session acquisition."""

from __future__ import annotations

import asyncio
import concurrent.futures
from dataclasses import dataclass
import itertools
import json
import time
from typing import Any, Coroutine, TypeVar
from urllib.parse import urlparse

import requests
import websockets
from websockets.exceptions import WebSocketException

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_USER_AGENT = "linux:reddit-curator:2.0.0 (by /u/reddit-curator)"
_REDDIT_ME_URL = "https://oauth.reddit.com/api/v1/me?raw_json=1"
_FOLLOWING_URL = "https://www.reddit.com/?feed=following"

_T = TypeVar("_T")


class CDPError(RuntimeError):
    """Raised when a Chrome DevTools Protocol RPC call fails or returns an error."""

    def __init__(self, message: str, error: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.error = error


class SSRFValidationError(CDPError, ValueError):
    """Raised when a WebSocket or target URL fails SSRF/hostname validation."""


@dataclass(frozen=True)
class RedditSessionData:
    """Validated Reddit OAuth session extracted from an authenticated Chrome tab."""

    username: str
    access_token: str
    target_id: str
    ws_url: str


def validate_cdp_port(cdp_port: int) -> int:
    """Validate that a CDP TCP port is within the valid 1..65535 range."""
    if isinstance(cdp_port, bool) or not isinstance(cdp_port, int) or not (1 <= cdp_port <= 65535):
        raise ValueError(f"Invalid CDP port: {cdp_port!r}")
    return cdp_port


def validate_loopback_ws_url(ws_url: str) -> str:
    """Ensure a CDP WebSocket URL uses ws:// on a loopback host without userinfo."""
    if not isinstance(ws_url, str) or not ws_url.strip():
        raise SSRFValidationError("WebSocket URL must be a non-empty string.")
    parsed = urlparse(ws_url.strip())
    if parsed.scheme != "ws":
        raise SSRFValidationError(f"Unsafe CDP WebSocket scheme {parsed.scheme!r}; only 'ws' is allowed.")
    if parsed.username is not None or parsed.password is not None:
        raise SSRFValidationError("CDP WebSocket URL must not contain userinfo credentials.")
    hostname = (parsed.hostname or "").lower()
    if hostname not in _LOOPBACK_HOSTS:
        raise SSRFValidationError(
            f"Refusing non-loopback CDP WebSocket host {hostname!r}; expected one of {sorted(_LOOPBACK_HOSTS)}."
        )
    return ws_url.strip()


def is_reddit_url(url: str) -> bool:
    """Strictly verify that a URL belongs to reddit.com or one of its subdomains."""
    if not isinstance(url, str) or not url:
        return False
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http"):
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    hostname = (parsed.hostname or "").lower()
    if not hostname or hostname.startswith(".") or ".." in hostname:
        return False
    return hostname == "reddit.com" or hostname.endswith(".reddit.com")


def _run_async(coro: Coroutine[Any, Any, _T]) -> _T:
    """Execute a coroutine synchronously, even if an event loop is already running."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


class CDPClient:
    """Async context-managed CDP JSON-RPC client with event filtering and timeouts."""

    def __init__(self, ws_url: str) -> None:
        self.ws_url = validate_loopback_ws_url(ws_url)
        self._id_counter = itertools.count(1)
        self._ws: Any = None

    async def __aenter__(self) -> CDPClient:
        try:
            self._ws = await websockets.connect(
                self.ws_url,
                origin=None,
                max_size=16_777_216,
                open_timeout=5.0,
                close_timeout=2.0,
            )
        except (WebSocketException, OSError, TimeoutError) as exc:
            raise CDPError(f"Failed to connect to CDP WebSocket {self.ws_url}: {exc}") from exc
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except (WebSocketException, OSError):
                pass
            finally:
                self._ws = None

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        """Send a CDP command and wait for its matching response frame."""
        if self._ws is None:
            raise CDPError("CDPClient is not connected; use 'async with CDPClient(...)'.")
        if timeout <= 0:
            raise ValueError(f"Timeout must be positive, got {timeout!r}")

        cmd_id = next(self._id_counter)
        payload = {"id": cmd_id, "method": method, "params": params or {}}
        try:
            await self._ws.send(json.dumps(payload))
        except (WebSocketException, OSError) as exc:
            raise CDPError(f"CDP WebSocket send failed for {method}: {exc}") from exc

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError(f"Timed out waiting for CDP response to {method} (id={cmd_id}).")
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=remaining)
            except (TimeoutError, asyncio.TimeoutError):
                raise
            except (WebSocketException, OSError) as exc:
                raise CDPError(f"CDP WebSocket receive failed for {method}: {exc}") from exc
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError) as exc:
                raise CDPError(f"Malformed JSON frame received from CDP for {method}: {exc}") from exc
            if not isinstance(msg, dict):
                continue
            if msg.get("id") != cmd_id:
                # Ignore unsolicited CDP event frames or responses for other IDs.
                continue
            if msg.get("error") is not None:
                err = msg["error"]
                raise CDPError(f"CDP command {method} failed: {err}", error=err if isinstance(err, dict) else None)
            result = msg.get("result")
            return result if isinstance(result, dict) else {}


def _cdp_http_get_json(cdp_port: int, path: str, timeout: float = 5.0) -> Any:
    url = f"http://127.0.0.1:{cdp_port}{path}"
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _find_reddit_page_target(targets: list[dict[str, Any]]) -> dict[str, Any] | None:
    for target in targets:
        if target.get("type") == "page" and is_reddit_url(str(target.get("url", ""))):
            return target
    return None


async def _open_reddit_target(cdp_port: int, targets: list[dict[str, Any]]) -> dict[str, Any]:
    """Open https://www.reddit.com/ via Target.createTarget when no Reddit tab is open."""
    control_ws_url: str | None = None
    try:
        version_info = _cdp_http_get_json(cdp_port, "/json/version")
        if isinstance(version_info, dict) and version_info.get("webSocketDebuggerUrl"):
            control_ws_url = str(version_info["webSocketDebuggerUrl"])
    except Exception:
        control_ws_url = None

    if not control_ws_url:
        for target in targets:
            if target.get("webSocketDebuggerUrl"):
                control_ws_url = str(target["webSocketDebuggerUrl"])
                break

    if not control_ws_url:
        raise CDPError(f"No CDP control WebSocket available on 127.0.0.1:{cdp_port}.")

    async with CDPClient(control_ws_url) as client:
        created = await client.send("Target.createTarget", {"url": "https://www.reddit.com/"})
    target_id = str(created.get("targetId") or "")
    if not target_id:
        raise CDPError("Target.createTarget did not return a targetId.")

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        refreshed = _cdp_http_get_json(cdp_port, "/json/list")
        if isinstance(refreshed, list):
            for target in refreshed:
                if target.get("id") == target_id and target.get("webSocketDebuggerUrl"):
                    return target
            found = _find_reddit_page_target(refreshed)
            if found is not None and found.get("webSocketDebuggerUrl"):
                return found
        await asyncio.sleep(0.2)

    return {
        "id": target_id,
        "url": "https://www.reddit.com/",
        "webSocketDebuggerUrl": f"ws://127.0.0.1:{cdp_port}/devtools/page/{target_id}",
    }


async def _eval_js(cdp: CDPClient, expression: str, timeout: float = 10.0) -> Any:
    res = await cdp.send(
        "Runtime.evaluate",
        {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": False,
        },
        timeout=timeout,
    )
    if res.get("exceptionDetails"):
        return None
    result_obj = res.get("result")
    if isinstance(result_obj, dict):
        return result_obj.get("value")
    return None


def _is_reddit_cookie_domain(domain: str) -> bool:
    clean = domain.strip().lower().lstrip(".")
    return clean == "reddit.com" or clean.endswith(".reddit.com")


async def _extract_token_candidates_from_tab(cdp: CDPClient) -> list[str]:
    """Extract ordered OAuth token candidates: (localStorage chat:access-token, token_v2 cookie)."""
    candidates: list[str] = []
    seen: set[str] = set()

    token_expr = (
        "(() => { try {"
        " const raw = localStorage.getItem('chat:access-token');"
        " if (!raw) return null;"
        " const parsed = JSON.parse(raw);"
        " return parsed && parsed.token ? String(parsed.token) : null;"
        "} catch (e) { return null; } })()"
    )
    token = await _eval_js(cdp, token_expr)
    if isinstance(token, str):
        clean_ls = token.strip()
        if clean_ls and clean_ls not in seen:
            seen.add(clean_ls)
            candidates.append(clean_ls)

    try:
        cookie_res = await cdp.send("Network.getCookies", {"urls": ["https://www.reddit.com"]})
    except CDPError:
        cookie_res = await cdp.send("Network.getCookies", {})

    cookies = cookie_res.get("cookies", [])
    if isinstance(cookies, list):
        for cookie in cookies:
            if not isinstance(cookie, dict):
                continue
            if cookie.get("name") == "token_v2" and _is_reddit_cookie_domain(str(cookie.get("domain", ""))):
                val = str(cookie.get("value") or "").strip()
                if val and val not in seen:
                    seen.add(val)
                    candidates.append(val)
    return candidates


async def _extract_token_from_tab(cdp: CDPClient) -> str | None:
    """Extract primary OAuth access token from localStorage chat:access-token or token_v2 cookie."""
    candidates = await _extract_token_candidates_from_tab(cdp)
    return candidates[0] if candidates else None


def _validate_reddit_token(access_token: str) -> str | None:
    """Validate token against /api/v1/me?raw_json=1 and return username if valid.

    Returns None on HTTP auth rejection (401/403 or other non-200 status), and raises
    CDPError on network transport failures so callers do not misdiagnose offline errors
    as missing Reddit login.
    """
    try:
        resp = requests.get(
            _REDDIT_ME_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "User-Agent": _USER_AGENT,
            },
            timeout=10.0,
        )
    except requests.RequestException as exc:
        raise CDPError(
            f"Network error contacting oauth.reddit.com to validate Reddit token: {exc}"
        ) from exc
    if resp.status_code in (401, 403) or resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    username = data.get("name") if isinstance(data, dict) else None
    return str(username) if isinstance(username, str) and username else None


async def _await_document_complete(cdp: CDPClient, timeout: float = 10.0, poll_interval: float = 0.25) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            ready = await _eval_js(cdp, "document.readyState", timeout=5.0)
            if ready == "complete":
                return True
        except CDPError:
            pass
        await asyncio.sleep(poll_interval)
    return False


async def _acquire_reddit_session_async(cdp_port: int) -> RedditSessionData:
    validate_cdp_port(cdp_port)
    targets_raw = _cdp_http_get_json(cdp_port, "/json/list")
    targets = targets_raw if isinstance(targets_raw, list) else []

    target = _find_reddit_page_target(targets)
    created_new = False
    if target is None:
        target = await _open_reddit_target(cdp_port, targets)
        created_new = True

    target_id = str(target.get("id") or "")
    ws_url = validate_loopback_ws_url(str(target.get("webSocketDebuggerUrl") or ""))

    async with CDPClient(ws_url) as cdp:
        if created_new:
            await _await_document_complete(cdp, timeout=10.0, poll_interval=0.4)

        for token in await _extract_token_candidates_from_tab(cdp):
            username = await asyncio.to_thread(_validate_reddit_token, token)
            if username:
                return RedditSessionData(
                    username=username,
                    access_token=token,
                    target_id=target_id,
                    ws_url=ws_url,
                )

        # Missing or expired token: reload page and poll readiness + token candidates every 400ms up to 10s.
        await cdp.send("Page.reload", {"ignoreCache": False})
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            await asyncio.sleep(0.4)
            try:
                ready_state = await _eval_js(cdp, "document.readyState", timeout=5.0)
            except CDPError:
                ready_state = None
            if ready_state != "complete":
                continue

            for token in await _extract_token_candidates_from_tab(cdp):
                username = await asyncio.to_thread(_validate_reddit_token, token)
                if username:
                    return RedditSessionData(
                        username=username,
                        access_token=token,
                        target_id=target_id,
                        ws_url=ws_url,
                    )

    raise CDPError(
        "Could not acquire a valid Reddit OAuth session from Chrome CDP within 10s. "
        "Ensure you are logged into https://www.reddit.com in the debugging browser."
    )


def acquire_reddit_session(cdp_port: int = 9222) -> RedditSessionData:
    """Acquire and validate a Reddit OAuth session from an open Chrome CDP instance."""
    return _run_async(_acquire_reddit_session_async(cdp_port))


_SHADOW_WALKER_JS = r"""
(() => {
  const collectRoots = (root, out = []) => {
    out.push(root);
    const elements = root.querySelectorAll ? root.querySelectorAll('*') : [];
    for (const el of elements) {
      if (el.shadowRoot) {
        collectRoots(el.shadowRoot, out);
      }
    }
    return out;
  };

  const roots = collectRoots(document);

  const activate = (el, method) => {
    const selected = el.getAttribute && (
      el.getAttribute('aria-selected') === 'true' || el.hasAttribute('selected')
    );
    if (!selected && typeof el.click === 'function') {
      el.click();
    }
    return {
      matched: true,
      method,
      already_selected: Boolean(selected),
      tag: el.tagName ? el.tagName.toLowerCase() : 'unknown',
    };
  };

  for (const root of roots) {
    if (!root.querySelector) continue;
    const byId = root.querySelector('#feed-tab-2');
    if (byId) return activate(byId, 'shadow_dom_#feed-tab-2');
  }

  for (const root of roots) {
    if (!root.querySelector) continue;
    const byHref = root.querySelector('a[href*="feed=following"]');
    if (byHref) return activate(byHref, 'shadow_dom_href');
  }

  for (const root of roots) {
    if (!root.querySelectorAll) continue;
    const candidates = root.querySelectorAll('[role="tab"], faceplate-tab');
    for (const el of candidates) {
      const label = (el.textContent || '').trim().toLowerCase();
      if (label === 'following') {
        return activate(el, 'shadow_dom_tab_text');
      }
    }
  }

  return {
    matched: false,
    method: window.location.href.includes('feed=following') ? 'url_navigation' : 'not_found',
    href: window.location.href,
  };
})()
"""


async def _activate_following_feed_async(ws_url: str) -> dict[str, Any]:
    validated_ws = validate_loopback_ws_url(ws_url)
    start = time.monotonic()

    try:
        async with CDPClient(validated_ws) as cdp:
            nav_res = await cdp.send("Page.navigate", {"url": _FOLLOWING_URL})
            if nav_res.get("errorText"):
                elapsed_ms = int((time.monotonic() - start) * 1000)
                return {
                    "success": False,
                    "method_used": "navigate_error",
                    "message": f"Navigation failed: {nav_res['errorText']}",
                    "elapsed_ms": elapsed_ms,
                }

            ready = await _await_document_complete(cdp, timeout=10.0, poll_interval=0.25)
            walker_res: Any = None
            hydration_deadline = time.monotonic() + 3.0
            while True:
                walker_res = await _eval_js(cdp, _SHADOW_WALKER_JS, timeout=10.0)
                if isinstance(walker_res, dict) and walker_res.get("matched"):
                    break
                if time.monotonic() >= hydration_deadline:
                    break
                await asyncio.sleep(0.25)
            elapsed_ms = int((time.monotonic() - start) * 1000)

            if isinstance(walker_res, dict) and walker_res.get("matched"):
                method_used = str(walker_res.get("method") or "shadow_dom")
                return {
                    "success": True,
                    "method_used": method_used,
                    "message": f"Activated Reddit Following feed via {method_used}.",
                    "elapsed_ms": elapsed_ms,
                }

            if isinstance(walker_res, dict) and walker_res.get("method") == "url_navigation" and ready:
                return {
                    "success": True,
                    "method_used": "url_navigation",
                    "message": "Navigated directly to https://www.reddit.com/?feed=following.",
                    "elapsed_ms": elapsed_ms,
                }

            return {
                "success": False,
                "method_used": str((walker_res or {}).get("method") if isinstance(walker_res, dict) else "unknown"),
                "message": "Following feed tab was not located and URL navigation did not settle.",
                "elapsed_ms": elapsed_ms,
            }
    except (CDPError, TimeoutError, asyncio.TimeoutError, OSError, WebSocketException) as exc:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return {
            "success": False,
            "method_used": "error",
            "message": str(exc),
            "elapsed_ms": elapsed_ms,
        }


def activate_following_feed(ws_url: str) -> dict[str, Any]:
    """Navigate to Reddit's Following feed and activate the tab across open Shadow DOMs."""
    return _run_async(_activate_following_feed_async(ws_url))
