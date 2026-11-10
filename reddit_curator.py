#!/usr/bin/env python3
"""
reddit_curator.py - Automated Cognitive Feed Curator for Reddit via Chrome DevTools Protocol (CDP)

This utility engineers your Reddit information diet using passive cognitive priming.
It allows one-shot or agent-driven curation of your Reddit subscriptions, custom feeds,
and feed display modes via an active Chrome session without needing Reddit API credentials.

Core Capabilities:
1. Connects to an active Chrome instance via CDP (default port 9222).
2. Extracts OAuth session token securely from localStorage without hardcoding credentials.
3. Automatically syncs domain-specific priming subreddits from a declarative manifest or preset.
4. Creates/upserts custom feeds (multireddits) with automatic lowercased URL routing.
5. Locks the home view to the 'Following' tab to bypass algorithmic regional clickbait.
6. Provides verification, dry-run, and machine-readable output for AI agents.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
import websockets

DEFAULT_CDP_PORT = 9222
REPO_ROOT = Path(__file__).resolve().parent
PRESETS_DIR = REPO_ROOT / "presets"

CHROME_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"
)

logger = logging.getLogger("reddit_curator")


def resolve_manifest_path(
    manifest_arg: Optional[Path] = None, preset_arg: Optional[str] = None
) -> Path:
    """Resolves the manifest file path from CLI flags, presets, or local defaults."""
    if preset_arg:
        preset_file = PRESETS_DIR / f"{preset_arg}.json"
        if not preset_file.is_file():
            preset_file = PRESETS_DIR / preset_arg
        if not preset_file.is_file():
            available = [p.stem for p in PRESETS_DIR.glob("*.json")]
            raise FileNotFoundError(
                f"Preset '{preset_arg}' not found. Available presets: {', '.join(available)}"
            )
        return preset_file

    if manifest_arg and manifest_arg.is_file():
        return manifest_arg

    candidates = [
        REPO_ROOT / "curation_manifest.json",
        REPO_ROOT / "manifest.local.json",
        REPO_ROOT / "manifest.json",
        REPO_ROOT / "manifest.example.json",
    ]
    for c in candidates:
        if c.is_file():
            return c

    raise FileNotFoundError(
        "No manifest found. Please provide --manifest or --preset, or copy manifest.example.json to manifest.json."
    )


@dataclass
class RedditSession:
    username: str
    access_token: str
    target_id: str
    ws_url: str


class RedditCurator:
    def __init__(
        self,
        cdp_port: int = DEFAULT_CDP_PORT,
        manifest_path: Optional[Path] = None,
        preset_name: Optional[str] = None,
        dry_run: bool = False,
    ) -> None:
        self.cdp_port = cdp_port
        self.manifest_path = resolve_manifest_path(manifest_path, preset_name)
        self.dry_run = dry_run
        self.manifest: Dict[str, Any] = self._load_manifest()

    def _load_manifest(self) -> Dict[str, Any]:
        """Loads and parses the curation manifest JSON."""
        with open(self.manifest_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        logger.info("Loaded curation manifest from: %s", self.manifest_path)
        return data

    def get_priming_subreddits(self) -> List[str]:
        """Extracts list of domain priming subreddits from manifest across schema versions."""
        items = self.manifest.get("domain_priming_subreddits") or self.manifest.get(
            "tier1_systems_subreddits", []
        )
        sub_names: List[str] = []
        for item in items:
            if isinstance(item, str):
                sub_names.append(item)
            elif isinstance(item, dict) and "name" in item:
                sub_names.append(item["name"])
        return sub_names

    def get_anchor_subreddits(self) -> List[str]:
        """Extracts list of aesthetic and cognitive anchor subreddits from manifest."""
        items = self.manifest.get("anchor_subreddits", [])
        anchor_names: List[str] = []
        for item in items:
            if isinstance(item, str):
                anchor_names.append(item)
            elif isinstance(item, dict) and "name" in item:
                anchor_names.append(item["name"])
        return anchor_names

    def _get_cdp_targets(self) -> List[Dict[str, Any]]:
        """Queries Chrome DevTools Protocol /json/list endpoint."""
        url = f"http://127.0.0.1:{self.cdp_port}/json/list"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "reddit_curator"})
            with urllib.request.urlopen(req, timeout=5) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as e:
            logger.error("Failed to connect to Chrome CDP at %s: %s", url, e)
            logger.error(
                "Ensure Chrome is running with remote debugging enabled: "
                "--remote-debugging-port=%d",
                self.cdp_port,
            )
            raise

    async def _send_cdp_command(
        self, ws_url: str, method: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Sends a JSON-RPC command over WebSocket to a CDP target."""
        async with websockets.connect(ws_url, max_size=10_000_000) as ws:
            cmd_id = 1
            payload = {"id": cmd_id, "method": method, "params": params or {}}
            await ws.send(json.dumps(payload))
            raw_res = await ws.recv()
            return json.loads(raw_res)

    async def acquire_session(self) -> RedditSession:
        """Discovers active Reddit page tab and extracts OAuth bearer token."""
        targets = self._get_cdp_targets()
        reddit_targets = [
            t
            for t in targets
            if "reddit.com" in t.get("url", "") and t.get("type") == "page"
        ]

        if not reddit_targets:
            raise RuntimeError(
                f"No active Reddit page found in Chrome on port {self.cdp_port}."
            )

        target = reddit_targets[0]
        for t in reddit_targets:
            if t.get("url") in ("https://www.reddit.com/", "https://www.reddit.com"):
                target = t
                break

        ws_url = target.get("webSocketDebuggerUrl")
        target_id = target.get("id", "")
        logger.info("Found Reddit tab [%s]: %s", target_id, target.get("url"))

        async def _extract_token_from_tab() -> Optional[str]:
            js_expr = 'localStorage.getItem("chat:access-token")'
            eval_res = await self._send_cdp_command(
                ws_url,
                "Runtime.evaluate",
                {"expression": js_expr, "returnByValue": True},
            )
            val = eval_res.get("result", {}).get("result", {}).get("value")
            if not val:
                return None
            try:
                token_data = json.loads(val)
                return token_data.get("token")
            except Exception:
                return None

        access_token = await _extract_token_from_tab()

        def _check_me(tok: str) -> Optional[str]:
            headers = {
                "Authorization": f"Bearer {tok}",
                "User-Agent": CHROME_USER_AGENT,
            }
            try:
                resp = requests.get(
                    "https://oauth.reddit.com/api/v1/me", headers=headers, timeout=10
                )
                if resp.status_code == 200:
                    return resp.json().get("name")
            except Exception:
                pass
            return None

        username: Optional[str] = None
        if access_token:
            username = _check_me(access_token)

        if not username:
            logger.warning(
                "Token absent or expired. Reloading Reddit tab to rehydrate session..."
            )
            await self._send_cdp_command(ws_url, "Page.reload", {})
            await asyncio.sleep(3.0)
            access_token = await _extract_token_from_tab()
            if access_token:
                username = _check_me(access_token)

        if not access_token or not username:
            raise RuntimeError(
                "Failed to authenticate session via Reddit OAuth. Ensure user is logged in."
            )

        logger.info("Authenticated successfully as: u/%s", username)

        return RedditSession(
            username=username,
            access_token=access_token,
            target_id=target_id,
            ws_url=ws_url,
        )

    def subscribe_priming_subreddits(self, session: RedditSession) -> List[str]:
        """Subscribes the active user to all domain priming subreddits."""
        sub_names = self.get_priming_subreddits()
        headers = {
            "Authorization": f"Bearer {session.access_token}",
            "User-Agent": CHROME_USER_AGENT,
        }

        successful_subs: List[str] = []
        logger.info("Subscribing to %d domain priming subreddits...", len(sub_names))

        for sub_name in sub_names:
            if self.dry_run:
                logger.info("[DRY-RUN] Would subscribe to: r/%s", sub_name)
                successful_subs.append(sub_name)
                continue

            try:
                resp = requests.post(
                    "https://oauth.reddit.com/api/subscribe",
                    headers=headers,
                    data={"action": "sub", "sr_name": sub_name},
                    timeout=10,
                )
                if resp.status_code == 200:
                    logger.info("  [✓] Subscribed: r/%-22s (status: 200)", sub_name)
                    successful_subs.append(sub_name)
                else:
                    logger.warning(
                        "  [✗] Failed to subscribe to r/%s: %s %s",
                        sub_name,
                        resp.status_code,
                        resp.text,
                    )
            except Exception as e:
                logger.error("  [✗] Error subscribing to r/%s: %s", sub_name, e)

        return successful_subs

    def sync_custom_feed(self, session: RedditSession) -> Dict[str, Any]:
        """
        Creates or updates custom feed(s) from the manifest.
        Uses lowercased username in the REST path to avoid Reddit's casing 404 bug,
        and uses PUT for idempotent upserting.
        """
        custom_feeds = self.manifest.get("custom_feeds", [])
        headers = {
            "Authorization": f"Bearer {session.access_token}",
            "User-Agent": CHROME_USER_AGENT,
        }

        results: Dict[str, Any] = {}
        for feed in custom_feeds:
            feed_name = feed["feed_name"]
            display_name = feed.get("display_name", feed_name)
            description_md = feed.get("description_md", "")
            icon_name = feed.get("icon_name", "programming")
            visibility = feed.get("visibility", "private")
            sub_names = feed.get("subreddits", [])

            lowercased_user = session.username.lower()
            endpoint = (
                f"https://oauth.reddit.com/api/multi/user/{lowercased_user}/m/{feed_name}"
            )

            model_data = {
                "display_name": display_name,
                "description_md": description_md,
                "icon_name": icon_name,
                "visibility": visibility,
                "subreddits": [{"name": s} for s in sub_names],
            }

            if self.dry_run:
                logger.info(
                    "[DRY-RUN] Would create/update custom feed '%s' with %d subreddits at: %s",
                    feed_name,
                    len(sub_names),
                    endpoint,
                )
                results[feed_name] = {"status": "dry_run", "endpoint": endpoint}
                continue

            logger.info("Creating/updating custom feed '%s' at: %s", feed_name, endpoint)
            resp = requests.put(
                endpoint,
                headers=headers,
                data={"model": json.dumps(model_data)},
                timeout=15,
            )

            if resp.status_code not in (200, 201):
                logger.info("  PUT returned %s, falling back to POST...", resp.status_code)
                resp = requests.post(
                    endpoint,
                    headers=headers,
                    data={"model": json.dumps(model_data)},
                    timeout=15,
                )

            if resp.status_code in (200, 201):
                res_json = resp.json()
                multi_path = res_json.get("data", {}).get("path")
                active_subs = [
                    s["name"] for s in res_json.get("data", {}).get("subreddits", [])
                ]
                logger.info(
                    "  [✓] Custom Feed '%s' successfully updated at: %s (%d subreddits)",
                    feed_name,
                    multi_path,
                    len(active_subs),
                )
                results[feed_name] = {
                    "status": "success",
                    "http_code": resp.status_code,
                    "path": multi_path,
                    "subreddits": active_subs,
                }
            else:
                logger.error(
                    "  [✗] Failed to update custom feed '%s' (%s): %s",
                    feed_name,
                    resp.status_code,
                    resp.text,
                )
                results[feed_name] = {
                    "status": "failed",
                    "http_code": resp.status_code,
                    "error": resp.text,
                }

        return results

    async def reload_to_following_feed(self, session: RedditSession) -> bool:
        """
        Reloads the browser page and ensures the 'Following' feed tab is activated.
        Modern Reddit interface has feed tabs:
          - feed-tab-1: 'For You' (algorithmic)
          - feed-tab-2: 'Following' (curated reverse-chronological anchor feed)
        """
        if self.dry_run:
            logger.info("[DRY-RUN] Would navigate and activate 'Following' feed tab.")
            return True

        logger.info("Navigating active tab to Reddit home and selecting Following feed...")

        nav_js = "window.location.href = 'https://www.reddit.com/';"
        await self._send_cdp_command(
            session.ws_url,
            "Runtime.evaluate",
            {"expression": nav_js},
        )

        poll_and_activate_js = """
        new Promise((resolve) => {
            const start = Date.now();
            const interval = setInterval(() => {
                const followingTab = document.querySelector('#feed-tab-2');
                if (followingTab) {
                    const isSelected = followingTab.classList.contains('tab-selected') || 
                                       followingTab.getAttribute('aria-selected') === 'true';
                    if (!isSelected) {
                        followingTab.click();
                    }
                    clearInterval(interval);
                    resolve({ 
                        success: true, 
                        message: isSelected ? 'feed-tab-2 already active' : 'Activated feed-tab-2 (Following)',
                        elapsed_ms: Date.now() - start
                    });
                } else if (Date.now() - start > 10000) {
                    clearInterval(interval);
                    resolve({ success: false, message: 'feed-tab-2 not located in DOM within 10s' });
                }
            }, 300);
        })
        """
        res = await self._send_cdp_command(
            session.ws_url,
            "Runtime.evaluate",
            {"expression": poll_and_activate_js, "awaitPromise": True, "returnByValue": True},
        )

        tab_res = res.get("result", {}).get("result", {}).get("value", {})
        logger.info("Feed selection response: %s", tab_res.get("message"))
        return bool(tab_res.get("success", False))

    def verify_subscriptions(self, session: RedditSession) -> Dict[str, Any]:
        """Verifies current Reddit subscriptions against the manifest."""
        headers = {
            "Authorization": f"Bearer {session.access_token}",
            "User-Agent": CHROME_USER_AGENT,
        }
        resp = requests.get(
            "https://oauth.reddit.com/subreddits/mine/subscriber?limit=100",
            headers=headers,
            timeout=10,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to fetch subscriptions: {resp.status_code}")

        items = resp.json().get("data", {}).get("children", [])
        current_subs = {
            item["data"]["display_name"].lower(): item["data"]["display_name"]
            for item in items
        }

        anchor_list = self.get_anchor_subreddits()
        priming_list = self.get_priming_subreddits()

        anchors_present = [s for s in anchor_list if s.lower() in current_subs]
        priming_present = [s for s in priming_list if s.lower() in current_subs]

        return {
            "total_subscribed": len(current_subs),
            "anchors_present": len(anchors_present),
            "anchors_total": len(anchor_list),
            "priming_present": len(priming_present),
            "priming_total": len(priming_list),
            "missing_priming": [s for s in priming_list if s.lower() not in current_subs],
            "all_subreddits": sorted(list(current_subs.values())),
        }

    def verify_custom_feed(self, session: RedditSession) -> Dict[str, Any]:
        """Verifies custom feed existence and constituent subreddit membership."""
        headers = {
            "Authorization": f"Bearer {session.access_token}",
            "User-Agent": CHROME_USER_AGENT,
        }
        custom_feeds = self.manifest.get("custom_feeds", [])
        lowercased_user = session.username.lower()
        results: Dict[str, Any] = {}

        for feed in custom_feeds:
            feed_name = feed["feed_name"]
            expected_subs = [s.lower() for s in feed.get("subreddits", [])]
            endpoint = (
                f"https://oauth.reddit.com/api/multi/user/{lowercased_user}/m/{feed_name}"
            )

            resp = requests.get(endpoint, headers=headers, timeout=10)
            if resp.status_code != 200:
                results[feed_name] = {
                    "exists": False,
                    "http_code": resp.status_code,
                    "error": resp.text,
                }
                continue

            feed_data = resp.json().get("data", {})
            present_subs = [s["name"] for s in feed_data.get("subreddits", [])]
            present_subs_lower = [s.lower() for s in present_subs]
            missing_subs = [
                s for s in feed.get("subreddits", []) if s.lower() not in present_subs_lower
            ]

            results[feed_name] = {
                "exists": True,
                "display_name": feed_data.get("display_name"),
                "path": feed_data.get("path"),
                "subreddits_count": len(present_subs),
                "expected_count": len(expected_subs),
                "is_complete": len(missing_subs) == 0,
                "missing_subreddits": missing_subs,
                "subreddits": present_subs,
            }

        return results

    def verify_all(self, session: RedditSession) -> Dict[str, Any]:
        """Runs comprehensive verification of both subscriptions and custom feeds."""
        subs = self.verify_subscriptions(session)
        feeds = self.verify_custom_feed(session)
        return {
            "subscriptions": subs,
            "custom_feeds": feeds,
            "all_healthy": (
                len(subs.get("missing_priming", [])) == 0
                and all(f.get("is_complete", False) for f in feeds.values())
            ),
        }

    async def run(self, verify_only: bool = False) -> Dict[str, Any]:
        """Executes full curation flow."""
        session = await self.acquire_session()

        if verify_only:
            logger.info("Running verification only...")
            verification = self.verify_all(session)
            return {"session": session.username, "verification": verification}

        # 1. Subscribe to domain priming subreddits
        subs_result = self.subscribe_priming_subreddits(session)

        # 2. Sync Custom Feed
        feed_result = self.sync_custom_feed(session)

        # 3. Reload browser tab to Following feed
        reloaded = await self.reload_to_following_feed(session)

        # 4. Verify resulting state
        verification = self.verify_all(session)

        custom_feed_healthy = all(
            f.get("is_complete", False)
            for f in verification.get("custom_feeds", {}).values()
        )
        logger.info("=" * 60)
        logger.info("CURATION COMPLETED SUCCESSFULLY")
        logger.info("Active User: u/%s", session.username)
        logger.info(
            "Total Subscriptions: %d",
            verification["subscriptions"]["total_subscribed"],
        )
        logger.info(
            "Priming Subscribed: %d / %d",
            verification["subscriptions"]["priming_present"],
            verification["subscriptions"]["priming_total"],
        )
        logger.info("Custom Feeds Verified & Complete: %s", custom_feed_healthy)
        logger.info("Following Feed Active: %s", reloaded)
        logger.info("=" * 60)

        return {
            "username": session.username,
            "priming_subscribed": subs_result,
            "custom_feed": feed_result,
            "reloaded_following": reloaded,
            "verification": verification,
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reddit Passive Cognitive Curator via Chrome DevTools Protocol."
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_CDP_PORT,
        help=f"CDP remote debugging port (default: {DEFAULT_CDP_PORT})",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Path to manifest JSON file.",
    )
    parser.add_argument(
        "--preset",
        type=str,
        default=None,
        help="Named preset from presets/ directory (e.g., systems_engineering, machine_learning, cognitive_science).",
    )
    parser.add_argument(
        "--list-presets",
        action="store_true",
        help="List available presets and exit.",
    )
    parser.add_argument(
        "--one-shot",
        action="store_true",
        help="Execute full one-shot curation and feed locking.",
    )
    parser.add_argument(
        "--agent",
        action="store_true",
        help="Agent mode: suppresses verbose logging and outputs clean JSON to stdout.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview curation operations without executing mutations.",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify active subscriptions and custom feed without mutating.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.list_presets:
        presets = [p.stem for p in PRESETS_DIR.glob("*.json")]
        print("Available presets:")
        for p in sorted(presets):
            print(f"  - {p}")
        sys.exit(0)

    log_level = logging.WARNING if args.agent else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        curator = RedditCurator(
            cdp_port=args.port,
            manifest_path=args.manifest,
            preset_name=args.preset,
            dry_run=args.dry_run,
        )
        result = asyncio.run(curator.run(verify_only=args.verify))
        print(json.dumps(result, indent=2, default=str))
    except Exception as e:
        if args.agent:
            print(json.dumps({"error": str(e), "success": False}))
        else:
            logger.error("Curator execution failed: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
