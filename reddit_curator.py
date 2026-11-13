#!/usr/bin/env python3
"""
reddit_curator.py - Automated Cognitive Feed Curator for Reddit via Chrome DevTools Protocol (CDP)

v2.0 integrates:
1. Hardened CDP WebSocket JSON-RPC & pooled Reddit OAuth runtime (`rcurator.cdp_client`, `rcurator.reddit_api`)
2. Multi-source deterministic signal miners (`WorkspaceScanner`, `GitHubMiner`, `ResumeParser`, `TechnicalTokenizer`)
3. BM25F/TF-IDF + Bipartite PPR + Velocity-Weighted Golden Ratio Optimizer (`AutoPresetGenerator`, `PresetMixer`, `FeedHealthAuditor`)
4. Pre-mutation snapshots, diff previews, opt-in pruning, and rollback (`SnapshotManager`)
5. Multi-browser RPM/Flatpak CDP launcher (`BrowserLauncher`)
6. Local Web Studio (`run_studio`) and interactive CLI Wizard (`run_wizard`)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import requests

from rcurator.analytics import FeedHealthAuditor
from rcurator.browser import BrowserLauncher
from rcurator.cdp_client import (
    CDPError,
    acquire_reddit_session,
    activate_following_feed,
    validate_cdp_port,
)
from rcurator.cli_wizard import (
    format_audit_summary,
    format_diff_summary,
    format_manifest_summary,
    format_presets_table,
    format_rollback_summary,
    format_run_summary,
    format_snapshots_table,
    run_wizard,
)
from rcurator.engine.ontology import OntologyRepository, normalize_manifest
from rcurator.engine.pipeline import AutoPresetGenerator
from rcurator.mixer import PresetMixer
from rcurator.nlp.tokenizer import TechnicalTokenizer
from rcurator.reddit_api import RedditAPIClient
from rcurator.sources.github import GitHubMiner
from rcurator.sources.resume import ResumeParser
from rcurator.sources.types import SignalEvidence
from rcurator.sources.workspace import WorkspaceScanner
from rcurator.state import SnapshotManager
from rcurator.studio.server import run_studio

DEFAULT_CDP_PORT = 9222
DEFAULT_STUDIO_PORT = 8420
REPO_ROOT = Path(__file__).resolve().parent
PRESETS_DIR = REPO_ROOT / "presets"
ONTOLOGY_PATH = REPO_ROOT / "curator_data" / "subreddit_ontology.json"
SNAPSHOTS_DIR = REPO_ROOT / ".curator_snapshots"

CHROME_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"
)

logger = logging.getLogger("reddit_curator")


def resolve_manifest_path(
    manifest_arg: Optional[Path] = None, preset_arg: Optional[str] = None
) -> Path:
    """Resolve the manifest file path from CLI flags, presets, or local defaults."""
    if preset_arg:
        raw = str(preset_arg).strip()
        if not raw or "/" in raw or "\\" in raw or ".." in raw:
            raise ValueError(f"Invalid preset name: {preset_arg!r}")
        base_presets = PRESETS_DIR.resolve()
        preset_file = (base_presets / f"{raw}.json").resolve()
        if not preset_file.is_file():
            preset_file = (base_presets / raw).resolve()
        if not preset_file.is_relative_to(base_presets):
            raise ValueError(f"Preset path escapes presets directory: {preset_arg!r}")
        if not preset_file.is_file():
            available = sorted(p.stem for p in PRESETS_DIR.glob("*.json"))
            raise FileNotFoundError(
                f"Preset '{preset_arg}' not found. Available presets: {', '.join(available)}"
            )
        return preset_file

    if manifest_arg is not None:
        resolved = Path(manifest_arg).expanduser()
        if not resolved.is_file():
            raise FileNotFoundError(f"Manifest file not found: {resolved}")
        return resolved

    candidates = [
        REPO_ROOT / "curation_manifest.json",
        REPO_ROOT / "manifest.local.json",
        REPO_ROOT / "manifest.json",
        REPO_ROOT / "manifest.example.json",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate

    raise FileNotFoundError(
        "No manifest found. Please provide --manifest or --preset, or copy manifest.example.json to manifest.json."
    )


def parse_ratio(raw: str) -> tuple[int, int, int]:
    """Parse 'aesthetic,cognitive,domain' percentage string into ints summing to 100."""
    text = str(raw or "").strip()
    if not text:
        return (40, 35, 25)
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if len(parts) != 3:
        raise ValueError(
            f"Invalid --ratio {raw!r}; expected 3 comma-separated numbers like '40,35,25'."
        )
    try:
        vals = [float(p) for p in parts]
    except ValueError as exc:
        raise ValueError(f"Invalid numeric values in --ratio {raw!r}.") from exc
    if any(v < 0 for v in vals) or sum(vals) <= 0:
        raise ValueError(f"--ratio values must be non-negative with positive sum: {raw!r}")
    total = sum(vals)
    a = round(vals[0] * 100.0 / total)
    c = round(vals[1] * 100.0 / total)
    d = 100 - a - c
    if d < 0:
        c = max(0, c + d)
        d = 100 - a - c
    return (int(a), int(c), int(d))


def parse_mix_spec(raw: str) -> dict[str, float]:
    """Parse '--mix preset1:0.6,preset2:0.4' into positive weights dict."""
    text = str(raw or "").strip()
    if not text:
        raise ValueError("--mix specification cannot be empty.")
    weights: dict[str, float] = {}
    for chunk in text.split(","):
        part = chunk.strip()
        if not part:
            continue
        if ":" not in part:
            raise ValueError(
                f"Invalid --mix entry {part!r}; expected 'preset_name:weight'."
            )
        name, raw_w = part.split(":", 1)
        clean_name = name.strip()
        if not clean_name:
            raise ValueError(f"Missing preset name in --mix entry {part!r}.")
        try:
            w = float(raw_w.strip())
        except ValueError as exc:
            raise ValueError(f"Invalid numeric weight in --mix entry {part!r}.") from exc
        if w <= 0:
            raise ValueError(f"Weight for preset {clean_name!r} must be > 0.")
        weights[clean_name] = weights.get(clean_name, 0.0) + w
    if not weights:
        raise ValueError("--mix must specify at least one preset with positive weight.")
    return weights


def parse_keep_csv(raw: str) -> set[str]:
    """Parse '--keep sub1,sub2' into a set of subreddit names."""
    return {item.strip() for item in str(raw or "").split(",") if item.strip()}


def validate_tier1_count(count: int) -> int:
    """Validate --tier1-count >= 3."""
    if isinstance(count, bool) or not isinstance(count, int) or count < 3:
        raise ValueError(f"--tier1-count must be an integer >= 3, got {count!r}.")
    return count


def discover_online_subreddits(
    query: str,
    *,
    cdp_port: int = DEFAULT_CDP_PORT,
    limit: int = 15,
) -> list[dict[str, Any]]:
    """Optionally enrich candidate pool via authenticated CDP search or public Reddit search.

    Gracefully returns [] on any network/CDP failure so offline curation never crashes.
    """
    clean_q = str(query or "").strip()
    if not clean_q:
        return []

    try:
        session = acquire_reddit_session(cdp_port)
        api = RedditAPIClient(session.access_token, session.username)
        results = api.search_subreddits(clean_q, limit=limit)
        if results:
            return results
    except Exception:
        pass

    try:
        resp = requests.get(
            "https://www.reddit.com/subreddits/search.json",
            params={"q": clean_q, "limit": max(1, min(int(limit), 50)), "raw_json": 1},
            headers={"User-Agent": CHROME_USER_AGENT},
            timeout=5.0,
        )
        if resp.status_code != 200:
            return []
        payload = resp.json()
        children = payload.get("data", {}).get("children", []) if isinstance(payload, dict) else []
        candidates: list[dict[str, Any]] = []
        for child in children:
            if not isinstance(child, dict):
                continue
            sub_data = child.get("data", {})
            if not isinstance(sub_data, dict):
                continue
            name = str(sub_data.get("display_name") or sub_data.get("name") or "").strip()
            if not name:
                continue
            candidates.append(
                {
                    "name": name,
                    "title": str(sub_data.get("title") or ""),
                    "public_description": str(sub_data.get("public_description") or ""),
                    "subscribers": int(sub_data.get("subscribers") or 0),
                    "over18": bool(sub_data.get("over18", False)),
                }
            )
        return candidates
    except Exception:
        return []


def list_presets_metadata(presets_dir: Path = PRESETS_DIR) -> list[dict[str, Any]]:
    """Load metadata for all built-in presets sorted by preset key."""
    items: list[dict[str, Any]] = []
    if not presets_dir.is_dir():
        return items
    for path in sorted(presets_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        items.append(
            {
                "id": path.stem,
                "name": data.get("name", path.stem),
                "description": data.get("description", ""),
                "anchor_count": len(data.get("anchor_subreddits", [])),
                "domain_priming_count": len(data.get("domain_priming_subreddits", [])),
                "custom_feeds_count": len(data.get("custom_feeds", [])),
                "golden_ratio_distribution": data.get("golden_ratio_distribution", {}),
                "path": str(path),
            }
        )
    return items


@dataclass
class RedditSession:
    username: str
    access_token: str
    target_id: str
    ws_url: str


class RedditCurator:
    """High-level curation orchestrator preserving v1 API and integrating v2 runtime."""

    def __init__(
        self,
        cdp_port: int = DEFAULT_CDP_PORT,
        manifest_path: Optional[Path] = None,
        preset_name: Optional[str] = None,
        dry_run: bool = False,
        manifest: Optional[Dict[str, Any]] = None,
        snapshots_dir: Optional[Path] = None,
        ontology: Optional[OntologyRepository] = None,
        prune: bool = False,
        keep: Optional[Iterable[str]] = None,
    ) -> None:
        self.cdp_port = validate_cdp_port(cdp_port)
        self.dry_run = bool(dry_run)
        self.prune = bool(prune)
        self.keep: set[str] = {str(x).strip() for x in (keep or ()) if str(x).strip()}
        self.snapshots_dir = Path(snapshots_dir) if snapshots_dir is not None else SNAPSHOTS_DIR
        self.ontology = ontology or (
            OntologyRepository.load(ONTOLOGY_PATH)
            if ONTOLOGY_PATH.is_file()
            else OntologyRepository.load()
        )

        if manifest is not None:
            self.manifest_path: Optional[Path] = manifest_path
            self.manifest: Dict[str, Any] = normalize_manifest(manifest, self.ontology)
        else:
            self.manifest_path = resolve_manifest_path(manifest_path, preset_name)
            self.manifest = self._load_manifest()

        self._api_clients: Dict[str, RedditAPIClient] = {}

    def _load_manifest(self) -> Dict[str, Any]:
        """Load, parse, and normalize the curation manifest JSON."""
        assert self.manifest_path is not None
        with open(self.manifest_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        logger.info("Loaded curation manifest from: %s", self.manifest_path)
        return normalize_manifest(raw, self.ontology)

    def _get_api_client(self, session: RedditSession) -> RedditAPIClient:
        key = f"{session.username}:{session.access_token}"
        if key not in self._api_clients:
            self._api_clients[key] = RedditAPIClient(session.access_token, session.username)
        return self._api_clients[key]

    def get_priming_subreddits(self) -> List[str]:
        """Extract list of domain priming subreddits from manifest across schema versions."""
        items = self.manifest.get("domain_priming_subreddits") or self.manifest.get(
            "tier1_systems_subreddits", []
        )
        sub_names: List[str] = []
        for item in items:
            if isinstance(item, str):
                sub_names.append(item)
            elif isinstance(item, dict) and "name" in item:
                sub_names.append(str(item["name"]))
        return sub_names

    def get_anchor_subreddits(self) -> List[str]:
        """Extract list of aesthetic and cognitive anchor subreddits from manifest."""
        items = self.manifest.get("anchor_subreddits", [])
        anchor_names: List[str] = []
        for item in items:
            if isinstance(item, str):
                anchor_names.append(item)
            elif isinstance(item, dict) and "name" in item:
                anchor_names.append(str(item["name"]))
        return anchor_names

    def get_desired_subreddits(self) -> List[str]:
        """Return deduplicated union of anchor and domain-priming subreddits."""
        desired: List[str] = []
        seen: set[str] = set()
        for name in self.get_anchor_subreddits() + self.get_priming_subreddits():
            clean = str(name).strip()
            if clean and clean.lower() not in seen:
                seen.add(clean.lower())
                desired.append(clean)
        return desired

    async def acquire_session(self) -> RedditSession:
        """Discover active Reddit page tab and extract OAuth bearer token via hardened CDP."""
        session_data = await asyncio.to_thread(acquire_reddit_session, self.cdp_port)
        logger.info("Authenticated successfully as: u/%s", session_data.username)
        return RedditSession(
            username=session_data.username,
            access_token=session_data.access_token,
            target_id=session_data.target_id,
            ws_url=session_data.ws_url,
        )

    def subscribe_priming_subreddits(self, session: RedditSession) -> List[str]:
        """Subscribe active user to domain priming subreddits via pooled delta sync."""
        priming = self.get_priming_subreddits()
        api = self._get_api_client(session)
        sync_res = api.sync_subscriptions(priming, dry_run=self.dry_run)
        active_lower = {
            s.lower() for s in (sync_res["already_subscribed"] + sync_res["subscribed"])
        }
        return [s for s in priming if s.lower() in active_lower]

    def subscribe_manifest_subreddits(
        self,
        session: RedditSession,
        prune: Optional[bool] = None,
        keep: Optional[Iterable[str]] = None,
    ) -> Dict[str, Any]:
        """Delta-sync BOTH anchor_subreddits and domain_priming_subreddits."""
        anchors = self.get_anchor_subreddits()
        priming = self.get_priming_subreddits()
        desired = self.get_desired_subreddits()
        use_prune = self.prune if prune is None else bool(prune)
        use_keep = self.keep if keep is None else {str(x).strip() for x in keep if str(x).strip()}

        api = self._get_api_client(session)
        sync_res = api.sync_subscriptions(
            desired,
            dry_run=self.dry_run,
            prune=use_prune,
            keep=use_keep,
        )
        active_lower = {
            s.lower() for s in (sync_res["already_subscribed"] + sync_res["subscribed"])
        }
        return {
            **sync_res,
            "anchors_subscribed": [s for s in anchors if s.lower() in active_lower],
            "priming_subscribed": [s for s in priming if s.lower() in active_lower],
        }

    def sync_custom_feed(self, session: RedditSession) -> Dict[str, Any]:
        """Create or update custom feed(s) with 100-subreddit partitioning and validation."""
        custom_feeds = list(self.manifest.get("custom_feeds") or [])
        api = self._get_api_client(session)
        return api.sync_custom_feeds(custom_feeds, dry_run=self.dry_run)

    async def reload_to_following_feed(self, session: RedditSession) -> bool:
        """Navigate browser tab to Reddit Following feed across open Shadow DOMs."""
        if self.dry_run:
            logger.info("[DRY-RUN] Would navigate and activate 'Following' feed tab.")
            return True
        res = await asyncio.to_thread(activate_following_feed, session.ws_url)
        logger.info("Feed selection response: %s", res.get("message"))
        return bool(res.get("success", False))

    def verify_subscriptions(self, session: RedditSession) -> Dict[str, Any]:
        """Verify BOTH anchor and domain-priming subscriptions via paginated Reddit API."""
        api = self._get_api_client(session)
        live_subs = api.fetch_all_subscriptions()
        current_subs = {low: entry["name"] for low, entry in live_subs.items()}

        anchor_list = self.get_anchor_subreddits()
        priming_list = self.get_priming_subreddits()

        anchors_present = [s for s in anchor_list if s.lower() in current_subs]
        missing_anchors = [s for s in anchor_list if s.lower() not in current_subs]
        priming_present = [s for s in priming_list if s.lower() in current_subs]
        missing_priming = [s for s in priming_list if s.lower() not in current_subs]

        return {
            "total_subscribed": len(current_subs),
            "anchors_present": len(anchors_present),
            "anchors_total": len(anchor_list),
            "missing_anchors": missing_anchors,
            "priming_present": len(priming_present),
            "priming_total": len(priming_list),
            "missing_priming": missing_priming,
            "all_subreddits": sorted(current_subs.values()),
        }

    def verify_custom_feed(self, session: RedditSession) -> Dict[str, Any]:
        """Verify custom feed existence and constituent subreddit membership."""
        api = self._get_api_client(session)
        live_feeds = api.fetch_custom_feeds()
        live_by_name = {
            str(feed.get("feed_name") or "").lower(): feed
            for feed in live_feeds
            if isinstance(feed, dict) and feed.get("feed_name")
        }

        custom_feeds = self.manifest.get("custom_feeds", [])
        results: Dict[str, Any] = {}
        for feed in custom_feeds:
            feed_name = str(feed["feed_name"])
            expected_subs = [str(s) for s in feed.get("subreddits", [])]
            live_feed = live_by_name.get(feed_name.lower())

            if live_feed is None:
                results[feed_name] = {
                    "exists": False,
                    "is_complete": False,
                    "missing_subreddits": expected_subs,
                }
                continue

            present_subs = [str(s) for s in live_feed.get("subreddits", [])]
            present_lower = {s.lower() for s in present_subs}
            missing_subs = [s for s in expected_subs if s.lower() not in present_lower]

            results[feed_name] = {
                "exists": True,
                "display_name": live_feed.get("display_name"),
                "path": live_feed.get("path"),
                "subreddits_count": len(present_subs),
                "expected_count": len(expected_subs),
                "is_complete": len(missing_subs) == 0,
                "missing_subreddits": missing_subs,
                "subreddits": present_subs,
            }

        return results

    def verify_all(self, session: RedditSession) -> Dict[str, Any]:
        """Run comprehensive verification of anchors, priming subs, and custom feeds."""
        subs = self.verify_subscriptions(session)
        feeds = self.verify_custom_feed(session)
        return {
            "subscriptions": subs,
            "custom_feeds": feeds,
            "all_healthy": (
                len(subs.get("missing_anchors", [])) == 0
                and len(subs.get("missing_priming", [])) == 0
                and all(f.get("is_complete", False) for f in feeds.values())
            ),
        }

    def build_offline_dry_run_preview(self, reason: str | None = None) -> Dict[str, Any]:
        """Build a 100% offline dry-run preview when Chrome CDP is not reachable."""
        analytics = FeedHealthAuditor(self.ontology).audit_manifest(self.manifest)
        preview: Dict[str, Any] = {
            "mode": "offline_dry_run",
            "dry_run": True,
            "cdp_reachable": False,
            "manifest_name": self.manifest.get("name"),
            "feed_mode": self.manifest.get("feed_mode", "following"),
            "desired_anchors": self.get_anchor_subreddits(),
            "desired_priming": self.get_priming_subreddits(),
            "custom_feeds": self.manifest.get("custom_feeds", []),
            "analytics": analytics,
        }
        if reason:
            preview["cdp_error"] = reason
        return preview

    async def run(self, verify_only: bool = False) -> Dict[str, Any]:
        """Execute full curation flow with automatic pre-mutation snapshot."""
        session = await self.acquire_session()

        if verify_only:
            logger.info("Running verification only...")
            verification = self.verify_all(session)
            return {"session": session.username, "verification": verification}

        api = self._get_api_client(session)
        snapshot_id: Optional[str] = None
        if not self.dry_run:
            live_subs_before = api.fetch_all_subscriptions()
            live_feeds_before = api.fetch_custom_feeds()
            snap = SnapshotManager(self.snapshots_dir).create_snapshot(
                username=session.username,
                subscriptions=live_subs_before,
                custom_feeds=live_feeds_before,
                manifest=self.manifest,
            )
            snapshot_id = snap["snapshot_id"]
            logger.info("Recorded pre-mutation snapshot: %s", snapshot_id)

        # 1. Subscribe to both anchor and domain-priming subreddits via delta sync
        sub_sync = self.subscribe_manifest_subreddits(session)

        # 2. Sync Custom Feed(s)
        feed_result = self.sync_custom_feed(session)

        # 3. Activate Following feed tab
        reloaded = await self.reload_to_following_feed(session)

        # 4. Verify resulting state
        verification = self.verify_all(session)
        analytics = FeedHealthAuditor(self.ontology).audit_manifest(self.manifest)

        return {
            "username": session.username,
            "snapshot_id": snapshot_id,
            "priming_subscribed": sub_sync["priming_subscribed"],
            "anchors_subscribed": sub_sync["anchors_subscribed"],
            "subscription_sync": sub_sync,
            "custom_feed": feed_result,
            "reloaded_following": reloaded,
            "verification": verification,
            "analytics": analytics,
        }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse frozen v2.0 CLI subcommands and flags (#9 §4)."""
    parser = argparse.ArgumentParser(
        description="Reddit Passive Cognitive Curator v2.0 via Chrome DevTools Protocol."
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=["sync", "studio", "ui", "wizard"],
        default="sync",
        help="Optional positional command (default: sync).",
    )
    # Legacy 8 flags
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
        help="Named preset from presets/ directory.",
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
    # New 23 frozen v2.0 flags
    parser.add_argument(
        "--from-workspace",
        type=Path,
        default=None,
        help="Mine signals deterministically from a local workspace directory.",
    )
    parser.add_argument(
        "--from-github",
        type=str,
        default=None,
        help="Mine signals from a GitHub username's public profile, repos, and stars.",
    )
    parser.add_argument(
        "--github-token",
        type=str,
        default=None,
        help="Optional GitHub PAT (or set GITHUB_TOKEN) to raise API rate limits.",
    )
    parser.add_argument(
        "--from-resume",
        type=Path,
        default=None,
        help="Parse a local .pdf, .md, .txt, or .tex resume into weighted skill signals.",
    )
    parser.add_argument(
        "--query",
        type=str,
        default=None,
        help="Free-text technical/research topic query for deterministic curation.",
    )
    parser.add_argument(
        "--mix",
        type=str,
        default=None,
        help="Blend presets with convex weights (e.g. systems_engineering:0.6,rust_and_compilers:0.4).",
    )
    parser.add_argument(
        "--ratio",
        type=str,
        default="40,35,25",
        help="Target Golden Ratio percentages aesthetic,cognitive,domain (default: 40,35,25).",
    )
    parser.add_argument(
        "--tier1-count",
        type=int,
        default=18,
        help="Target number of Tier-1 communities in the Following feed (>= 3, default: 18).",
    )
    parser.add_argument(
        "--discover-online",
        action="store_true",
        help="Optionally enrich ontology candidates via Reddit /subreddits/search.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write generated or mixed manifest JSON to disk.",
    )
    parser.add_argument(
        "--audit",
        action="store_true",
        help="Run Diet Health audit on the selected or generated manifest.",
    )
    parser.add_argument(
        "--audit-live",
        action="store_true",
        help="Fetch live Reddit subscriptions via CDP and run Diet Health audit.",
    )
    parser.add_argument(
        "--diff",
        action="store_true",
        help="Compute and display subscription/custom-feed diff against live Reddit state.",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="Opt-in removal of unmanaged subscriptions during sync.",
    )
    parser.add_argument(
        "--keep",
        type=str,
        default="",
        help="Comma-separated subreddit allowlist preserved when --prune is enabled.",
    )
    parser.add_argument(
        "--snapshot",
        action="store_true",
        help="Capture a timestamped backup snapshot in .curator_snapshots/ and exit.",
    )
    parser.add_argument(
        "--list-snapshots",
        action="store_true",
        help="List saved backup snapshots from .curator_snapshots/.",
    )
    parser.add_argument(
        "--rollback",
        type=str,
        default=None,
        help="Restore subscriptions and custom feeds from 'latest' or a specific snapshot ID.",
    )
    parser.add_argument(
        "--launch-browser",
        action="store_true",
        help="Detect and launch Chrome/Chromium/Brave (RPM or Flatpak) with CDP enabled.",
    )
    parser.add_argument(
        "--browser",
        type=str,
        default=None,
        help="Preferred browser binary or Flatpak ID for --launch-browser.",
    )
    parser.add_argument(
        "--studio",
        action="store_true",
        help="Start the local Web Studio HTTP server.",
    )
    parser.add_argument(
        "--studio-port",
        type=int,
        default=DEFAULT_STUDIO_PORT,
        help=f"TCP port for the local Web Studio server (default: {DEFAULT_STUDIO_PORT}).",
    )
    parser.add_argument(
        "--no-open-browser",
        action="store_true",
        help="Do not auto-open the Web Studio URL in your browser on startup.",
    )
    return parser.parse_args(argv)


def _emit_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str))


def _generate_or_mix_manifest(
    args: argparse.Namespace,
    ontology: OntologyRepository,
) -> dict[str, Any]:
    """Fuse multi-source signals and/or convex preset mix into a v2 manifest."""
    ratio = parse_ratio(args.ratio)
    tier1_count = validate_tier1_count(args.tier1_count)
    has_sources = any(
        [args.from_workspace, args.from_github, args.from_resume, args.query]
    )
    has_mix = bool(args.mix)

    evidences: list[SignalEvidence] = []
    if args.from_workspace:
        evidences.extend(WorkspaceScanner().scan(args.from_workspace))
    if args.from_github:
        evidences.extend(GitHubMiner(token=args.github_token).mine(args.from_github))
    if args.from_resume:
        evidences.extend(ResumeParser().parse_file(args.from_resume))
    if args.query:
        tokenizer = TechnicalTokenizer(known_phrases=ontology.known_phrases)
        for tok, w in tokenizer.tokenize(args.query):
            evidences.append(
                SignalEvidence(
                    term=tok,
                    raw_term=tok,
                    weight=float(w) * 2.2,
                    source_type="query",
                    source_detail="cli_query",
                    signal_kind="keyword",
                )
            )

    if has_mix and not has_sources:
        weights = parse_mix_spec(args.mix)
        manifest = PresetMixer(ontology, PRESETS_DIR).mix(
            weights, target_ratio=ratio, tier1_count=tier1_count
        )
    elif has_mix and has_sources:
        weights = parse_mix_spec(args.mix)
        mixed = PresetMixer(ontology, PRESETS_DIR).mix(
            weights, target_ratio=ratio, tier1_count=tier1_count
        )
        for item in mixed.get("anchor_subreddits", []) + mixed.get("domain_priming_subreddits", []):
            sname = item["name"] if isinstance(item, dict) else str(item)
            rec = ontology.get(sname)
            if rec is not None:
                for term in (*rec.ecosystems[:3], *rec.keywords[:3]):
                    evidences.append(
                        SignalEvidence(
                            term=term,
                            raw_term=term,
                            weight=1.4,
                            source_type="query",
                            source_detail=f"mix:{sname}",
                            signal_kind="keyword",
                        )
                    )
        online_candidates: list[dict[str, Any]] | None = None
        if args.discover_online:
            search_q = args.query or " ".join(
                dict.fromkeys(ev.term for ev in evidences[:5])
            )
            online_candidates = discover_online_subreddits(search_q, cdp_port=args.port)
        manifest = AutoPresetGenerator(ontology).generate(
            evidences,
            name="Blended & Auto-Generated Curation",
            target_ratio=ratio,
            tier1_count=tier1_count,
            online_candidates=online_candidates,
        )
    else:
        online_candidates = None
        if args.discover_online:
            if args.query:
                search_q = args.query
            else:
                sorted_terms = sorted(evidences, key=lambda e: -e.weight)
                search_q = " ".join(dict.fromkeys(ev.term for ev in sorted_terms[:4]))
            online_candidates = discover_online_subreddits(search_q, cdp_port=args.port)
        manifest = AutoPresetGenerator(ontology).generate(
            evidences,
            name="Auto-Generated Curation",
            target_ratio=ratio,
            tier1_count=tier1_count,
            online_candidates=online_candidates,
        )

    if args.output is not None:
        out_path = Path(args.output).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        logger.info("Wrote manifest to: %s", out_path)

    return manifest


def _dispatch(args: argparse.Namespace) -> int:
    """Dispatch CLI request according to offline-first v2.0 routing."""
    if args.command in ("studio", "ui") or args.studio:
        run_studio(
            host="127.0.0.1",
            port=args.studio_port,
            cdp_port=args.port,
            open_browser=not args.no_open_browser,
        )
        return 0

    if args.command == "wizard":
        run_wizard(
            presets_dir=PRESETS_DIR,
            ontology_path=ONTOLOGY_PATH,
            snapshots_dir=SNAPSHOTS_DIR,
            cdp_port=args.port,
            github_token=args.github_token,
        )
        return 0

    if args.list_presets:
        presets = list_presets_metadata(PRESETS_DIR)
        if args.agent:
            _emit_json({"presets": presets, "count": len(presets)})
        else:
            print(format_presets_table(presets))
        return 0

    if args.list_snapshots:
        snaps = SnapshotManager(SNAPSHOTS_DIR).list_snapshots()
        if args.agent:
            _emit_json({"snapshots": snaps, "count": len(snaps)})
        else:
            print(format_snapshots_table(snaps))
        return 0

    if args.launch_browser:
        res = BrowserLauncher.launch(port=args.port, preferred=args.browser)
        follow_up = any(
            [
                args.one_shot,
                args.verify,
                args.diff,
                args.audit_live,
                args.snapshot,
                args.rollback,
                args.dry_run,
            ]
        )
        if not follow_up:
            if args.agent:
                _emit_json(res)
            else:
                status = "Reused existing" if res.get("reused_existing") else "Launched"
                print(
                    f"{status} {res.get('browser_id')} on {res.get('cdp_http_url')} "
                    f"(profile: {res.get('user_data_dir')})"
                )
            return 0

    ontology = (
        OntologyRepository.load(ONTOLOGY_PATH)
        if ONTOLOGY_PATH.is_file()
        else OntologyRepository.load()
    )

    if args.snapshot:
        session = acquire_reddit_session(args.port)
        api = RedditAPIClient(session.access_token, session.username)
        live_subs = api.fetch_all_subscriptions()
        live_feeds = api.fetch_custom_feeds()
        snap = SnapshotManager(SNAPSHOTS_DIR).create_snapshot(
            username=session.username,
            subscriptions=live_subs,
            custom_feeds=live_feeds,
        )
        if args.agent:
            _emit_json(snap)
        else:
            print(
                f"Snapshot created: {snap['snapshot_id']} "
                f"(u/{snap['username']}, {snap['subscriptions_count']} subs, "
                f"{snap['custom_feeds_count']} custom feeds)\nPath: {snap['path']}"
            )
        return 0

    if args.rollback:
        session = acquire_reddit_session(args.port)
        api = RedditAPIClient(session.access_token, session.username)
        res = SnapshotManager(SNAPSHOTS_DIR).rollback(
            args.rollback, api, dry_run=bool(args.dry_run)
        )
        if args.agent:
            _emit_json(res)
        else:
            print(format_rollback_summary(res))
        return 0

    if args.audit_live:
        ratio = parse_ratio(args.ratio)
        session = acquire_reddit_session(args.port)
        api = RedditAPIClient(session.access_token, session.username)
        live_subs = api.fetch_all_subscriptions()
        sub_names = [entry.get("name", key) for key, entry in live_subs.items()]
        report = FeedHealthAuditor(ontology).audit_live(sub_names, target_ratio=ratio)
        payload = {"username": session.username, "audit": report}
        if args.agent:
            _emit_json(payload)
        else:
            print(format_audit_summary(report, title=f"Live Diet Health Audit (u/{session.username})"))
        return 0

    has_generation = any(
        [args.from_workspace, args.from_github, args.from_resume, args.query, args.mix]
    )
    generated_manifest: Optional[dict[str, Any]] = None
    if has_generation:
        generated_manifest = _generate_or_mix_manifest(args, ontology)

    needs_live_or_dry = any([args.one_shot, args.verify, args.diff, args.dry_run])

    # Pure offline generation/mixing or standalone offline manifest audit
    if has_generation and not needs_live_or_dry:
        assert generated_manifest is not None
        audit_report = (
            FeedHealthAuditor(ontology).audit_manifest(generated_manifest)
            if args.audit
            else generated_manifest.get("curation_provenance", {}).get("diet_health")
        )
        if args.agent:
            out_payload: dict[str, Any] = {
                "manifest": generated_manifest,
                "output_path": str(args.output) if args.output else None,
            }
            if args.audit:
                out_payload["audit"] = audit_report
            _emit_json(out_payload)
        else:
            print(format_manifest_summary(generated_manifest, saved_path=args.output))
        return 0

    if args.audit and not has_generation and not needs_live_or_dry:
        mpath = resolve_manifest_path(args.manifest, args.preset)
        raw = json.loads(mpath.read_text(encoding="utf-8"))
        norm = normalize_manifest(raw, ontology)
        report = FeedHealthAuditor(ontology).audit_manifest(norm)
        if args.agent:
            _emit_json({"manifest_name": norm.get("name"), "manifest_path": str(mpath), "audit": report})
        else:
            print(format_audit_summary(report, title=f"Manifest Diet Health Audit: {norm.get('name')}"))
        return 0

    keep_set = parse_keep_csv(args.keep)
    curator = RedditCurator(
        cdp_port=args.port,
        manifest_path=args.manifest if generated_manifest is None else None,
        preset_name=args.preset if generated_manifest is None else None,
        dry_run=args.dry_run,
        manifest=generated_manifest,
        snapshots_dir=SNAPSHOTS_DIR,
        ontology=ontology,
        prune=args.prune,
        keep=keep_set,
    )

    if args.diff:
        session = asyncio.run(curator.acquire_session())
        api = curator._get_api_client(session)
        live_subs = api.fetch_all_subscriptions()
        live_feeds = api.fetch_custom_feeds()
        sub_names = [entry.get("name", key) for key, entry in live_subs.items()]
        diff = SnapshotManager(SNAPSHOTS_DIR).compute_diff(
            curator.manifest,
            live_subs=sub_names,
            live_feeds=live_feeds,
            prune=args.prune,
            keep=keep_set,
        )
        if args.agent:
            _emit_json({"username": session.username, "diff": diff})
        else:
            print(format_diff_summary(diff))
        return 0

    if args.dry_run:
        try:
            result = asyncio.run(curator.run(verify_only=args.verify))
        except Exception as exc:
            result = curator.build_offline_dry_run_preview(reason=str(exc))
        if args.agent:
            _emit_json(result)
        else:
            print(format_run_summary(result))
        return 0

    result = asyncio.run(curator.run(verify_only=args.verify))
    if args.audit and "analytics" not in result:
        result["analytics"] = FeedHealthAuditor(ontology).audit_manifest(curator.manifest)

    if args.agent:
        _emit_json(result)
    else:
        print(format_run_summary(result))
    return 0


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)

    log_level = logging.ERROR if args.agent else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
        force=True,
    )

    try:
        exit_code = _dispatch(args)
        if exit_code:
            sys.exit(exit_code)
    except Exception as exc:
        if args.agent:
            _emit_json({"error": str(exc), "success": False})
        else:
            logger.error("Curator execution failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
