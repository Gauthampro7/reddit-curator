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
import importlib.util
import json
import logging
import os
import platform
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import requests

from rcurator import __version__
from rcurator.analytics import FeedHealthAuditor
from rcurator.browser import BrowserLauncher
from rcurator.cdp_client import (
    CDPClient,
    CDPError,
    _extract_token_candidates_from_tab,
    _find_reddit_page_target,
    _validate_reddit_token,
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
from rcurator.engine.ontology import OntologyRepository, normalize_manifest, validate_manifest
from rcurator.engine.pipeline import AutoPresetGenerator
from rcurator.mixer import PresetMixer
from rcurator.nlp.tokenizer import TechnicalTokenizer
from rcurator.reddit_api import RedditAPIClient, _partition_feed_subreddits, validate_feed_name
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


def resolve_snapshots_dir(preferred: Path | None = None) -> Path:
    """Return preferred snapshots directory, falling back to ~/.config/reddit-curator/snapshots if read-only."""
    candidate = Path(preferred) if preferred is not None else SNAPSHOTS_DIR
    if preferred is not None:
        return candidate
    if candidate.exists():
        if os.access(candidate, os.W_OK | os.X_OK):
            return candidate
    elif candidate.parent.exists() and os.access(candidate.parent, os.W_OK | os.X_OK):
        return candidate
    return Path.home() / ".config" / "reddit-curator" / "snapshots"


def validate_studio_port(port: int) -> int:
    """Validate that a Web Studio TCP port is within 1..65535."""
    if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
        raise ValueError(f"Invalid studio port: {port!r}; must be in range 1..65535.")
    return port


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
        self.snapshots_dir = (
            Path(snapshots_dir) if snapshots_dir is not None else resolve_snapshots_dir()
        )
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
        """Verify custom feed existence and constituent subreddit membership (including >100-sub partitions)."""
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
            display_name = str(feed.get("display_name") or feed_name)
            expected_subs = [str(s) for s in feed.get("subreddits", [])]
            try:
                partitions = _partition_feed_subreddits(feed_name, display_name, expected_subs)
            except ValueError:
                partitions = [(feed_name, display_name, expected_subs)]

            for chunk_name, _chunk_display, chunk_subs in partitions:
                live_feed = live_by_name.get(chunk_name.lower())

                if live_feed is None:
                    results[chunk_name] = {
                        "exists": False,
                        "is_complete": False,
                        "missing_subreddits": chunk_subs,
                        "extra_subreddits": [],
                    }
                    continue

                present_subs = [str(s) for s in live_feed.get("subreddits", [])]
                present_lower = {s.lower() for s in present_subs}
                expected_lower = {s.lower() for s in chunk_subs}
                missing_subs = [s for s in chunk_subs if s.lower() not in present_lower]
                extra_subs = [s for s in present_subs if s.lower() not in expected_lower]

                results[chunk_name] = {
                    "exists": True,
                    "display_name": live_feed.get("display_name"),
                    "path": live_feed.get("path"),
                    "subreddits_count": len(present_subs),
                    "expected_count": len(chunk_subs),
                    "is_complete": len(missing_subs) == 0 and len(extra_subs) == 0,
                    "missing_subreddits": missing_subs,
                    "extra_subreddits": extra_subs,
                    "subreddits": present_subs,
                }

        return results

    def verify_all(
        self,
        session: RedditSession,
        reloaded_following: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Run comprehensive verification of anchors, priming subs, custom feeds, and Following lock."""
        subs = self.verify_subscriptions(session)
        feeds = self.verify_custom_feed(session)
        following_ok = True if reloaded_following is None else bool(reloaded_following)
        out: Dict[str, Any] = {
            "subscriptions": subs,
            "custom_feeds": feeds,
            "all_healthy": (
                len(subs.get("missing_anchors", [])) == 0
                and len(subs.get("missing_priming", [])) == 0
                and all(f.get("is_complete", False) for f in feeds.values())
                and following_ok
            ),
        }
        if reloaded_following is not None:
            out["reloaded_following"] = bool(reloaded_following)
        return out

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
        verification = self.verify_all(session, reloaded_following=reloaded)
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


def _build_parser() -> argparse.ArgumentParser:
    """Construct the grouped CLI argument parser."""
    epilog = (
        "Quickstart Examples:\n"
        "  python3 reddit_curator.py doctor\n"
        "  python3 reddit_curator.py --list-presets --query rust\n"
        "  python3 reddit_curator.py --preset systems_engineering --dry-run\n"
        "  python3 reddit_curator.py --preset systems_engineering --output out.json\n"
        "  python3 reddit_curator.py --compare-presets systems_engineering,rust_and_compilers\n"
        "  python3 reddit_curator.py wizard\n"
        "  python3 reddit_curator.py studio\n"
    )
    parser = argparse.ArgumentParser(
        description="Reddit Passive Cognitive Curator v2.0 via Chrome DevTools Protocol.",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=["sync", "studio", "ui", "wizard", "doctor"],
        default="sync",
        help="Optional positional command (sync, studio/ui, wizard, doctor).",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    grp_target = parser.add_argument_group("Target & Preset Inspection")
    grp_target.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Path to manifest JSON file.",
    )
    grp_target.add_argument(
        "--preset",
        type=str,
        default=None,
        help="Named preset from presets/ directory.",
    )
    grp_target.add_argument(
        "--list-presets",
        action="store_true",
        help="List available presets and exit (combine with --query to filter).",
    )
    grp_target.add_argument(
        "--preset-info",
        type=str,
        default=None,
        help="Show full subreddits, custom feeds, and diet audit for a preset.",
    )
    grp_target.add_argument(
        "--compare-presets",
        type=str,
        default=None,
        help="Compare two presets side-by-side (e.g. systems_engineering,rust_and_compilers).",
    )
    grp_target.add_argument(
        "--validate",
        action="store_true",
        help="Validate selected --manifest or --preset schema and Diet Health.",
    )

    grp_gen = parser.add_argument_group("Auto-Curation & Mixing")
    grp_gen.add_argument(
        "--from-workspace",
        type=Path,
        default=None,
        help="Mine signals deterministically from a local workspace directory.",
    )
    grp_gen.add_argument(
        "--from-github",
        type=str,
        default=None,
        help="Mine signals from a GitHub username's public profile, repos, and stars.",
    )
    grp_gen.add_argument(
        "--github-token",
        type=str,
        default=None,
        help="Optional GitHub PAT (or set GITHUB_TOKEN) to raise API rate limits.",
    )
    grp_gen.add_argument(
        "--from-resume",
        type=Path,
        default=None,
        help="Parse a local .pdf, .md, .txt, or .tex resume into weighted skill signals.",
    )
    grp_gen.add_argument(
        "--query",
        type=str,
        default=None,
        help="Free-text technical/research topic query (or filter for --list-presets).",
    )
    grp_gen.add_argument(
        "--mix",
        type=str,
        default=None,
        help="Blend presets with convex weights (e.g. systems_engineering:0.6,rust_and_compilers:0.4).",
    )
    grp_gen.add_argument(
        "--ratio",
        type=str,
        default="40,35,25",
        help="Target Golden Ratio percentages aesthetic,cognitive,domain (default: 40,35,25).",
    )
    grp_gen.add_argument(
        "--tier1-count",
        type=int,
        default=18,
        help="Target number of Tier-1 communities in the Following feed (>= 3, default: 18).",
    )
    grp_gen.add_argument(
        "--discover-online",
        action="store_true",
        help="Optionally enrich ontology candidates via Reddit /subreddits/search.",
    )
    grp_gen.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write generated, mixed, or normalized preset/manifest JSON to disk.",
    )

    grp_sync = parser.add_argument_group("Sync, Diff, Audit & Snapshot Operations")
    grp_sync.add_argument(
        "--one-shot",
        action="store_true",
        help="Execute full one-shot curation and feed locking.",
    )
    grp_sync.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview curation operations without executing mutations.",
    )
    grp_sync.add_argument(
        "--verify",
        action="store_true",
        help="Verify active subscriptions and custom feed without mutating.",
    )
    grp_sync.add_argument(
        "--audit",
        action="store_true",
        help="Run Diet Health audit on the selected or generated manifest.",
    )
    grp_sync.add_argument(
        "--audit-live",
        action="store_true",
        help="Fetch live Reddit subscriptions via CDP and run Diet Health audit.",
    )
    grp_sync.add_argument(
        "--diff",
        action="store_true",
        help="Compute and display subscription/custom-feed diff against live Reddit state.",
    )
    grp_sync.add_argument(
        "--prune",
        action="store_true",
        help="Opt-in removal of unmanaged subscriptions during sync.",
    )
    grp_sync.add_argument(
        "--keep",
        type=str,
        default="",
        help="Comma-separated subreddit allowlist preserved when --prune is enabled.",
    )
    grp_sync.add_argument(
        "--snapshot",
        action="store_true",
        help="Capture a timestamped backup snapshot in .curator_snapshots/ and exit.",
    )
    grp_sync.add_argument(
        "--list-snapshots",
        action="store_true",
        help="List saved backup snapshots from .curator_snapshots/.",
    )
    grp_sync.add_argument(
        "--rollback",
        type=str,
        default=None,
        help="Restore subscriptions and custom feeds from 'latest' or a specific snapshot ID.",
    )

    grp_runtime = parser.add_argument_group("Browser, Studio & Runtime")
    grp_runtime.add_argument(
        "--port",
        type=int,
        default=DEFAULT_CDP_PORT,
        help=f"CDP remote debugging port (default: {DEFAULT_CDP_PORT})",
    )
    grp_runtime.add_argument(
        "--launch-browser",
        action="store_true",
        help="Detect and launch Chrome/Chromium/Brave (RPM or Flatpak) with CDP enabled.",
    )
    grp_runtime.add_argument(
        "--browser",
        type=str,
        default=None,
        help="Preferred browser binary or Flatpak ID for --launch-browser.",
    )
    grp_runtime.add_argument(
        "--studio",
        action="store_true",
        help="Start the local Web Studio HTTP server.",
    )
    grp_runtime.add_argument(
        "--studio-port",
        type=int,
        default=DEFAULT_STUDIO_PORT,
        help=f"TCP port for the local Web Studio server (default: {DEFAULT_STUDIO_PORT}).",
    )
    grp_runtime.add_argument(
        "--no-open-browser",
        action="store_true",
        help="Do not auto-open the Web Studio URL in your browser on startup.",
    )
    grp_runtime.add_argument(
        "--agent",
        action="store_true",
        help="Agent mode: suppresses verbose logging and outputs clean JSON to stdout.",
    )
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse frozen v2.0 CLI subcommands and flags (#9 §4)."""
    return _build_parser().parse_args(argv)


def _format_quickstart_and_help() -> str:
    """Render safe zero-argument Quickstart banner and grouped CLI help."""
    banner = (
        f"reddit-curator v{__version__} — Deterministic Cognitive Information Diet Engine\n"
        + ("=" * 72)
        + "\n"
        "Safe Quickstart (no arguments passed — zero Reddit mutations performed):\n"
        "  1. Environment diagnostics : python3 reddit_curator.py doctor\n"
        "  2. Interactive wizard      : python3 reddit_curator.py wizard\n"
        "  3. Local Web Studio        : python3 reddit_curator.py studio\n"
        "  4. Offline preset preview  : python3 reddit_curator.py --preset systems_engineering --dry-run\n"
        "  5. Browse 17 domain packs  : python3 reddit_curator.py --list-presets\n\n"
    )
    return banner + _build_parser().format_help()


def _emit_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str))


def _load_ontology() -> OntologyRepository:
    return (
        OntologyRepository.load(ONTOLOGY_PATH)
        if ONTOLOGY_PATH.is_file()
        else OntologyRepository.load()
    )


def _filter_presets_by_query(
    presets: list[dict[str, Any]], query: str | None
) -> list[dict[str, Any]]:
    """Filter preset summaries case-insensitively by id, name, description, or subreddit."""
    q = str(query or "").strip().lower()
    if not q:
        return presets
    matched: list[dict[str, Any]] = []
    for item in presets:
        haystack = " ".join(
            [
                str(item.get("id", "")),
                str(item.get("name", "")),
                str(item.get("description", "")),
            ]
        ).lower()
        if q in haystack:
            matched.append(item)
            continue
        ppath = Path(str(item.get("path", "")))
        if ppath.is_file():
            try:
                raw = json.loads(ppath.read_text(encoding="utf-8"))
                sub_names: list[str] = []
                for key in ("anchor_subreddits", "domain_priming_subreddits"):
                    for entry in raw.get(key) or []:
                        sub_names.append(
                            str(entry.get("name") if isinstance(entry, dict) else entry)
                        )
                for feed in raw.get("custom_feeds") or []:
                    if isinstance(feed, dict):
                        sub_names.extend(str(s) for s in (feed.get("subreddits") or []))
                if any(q in s.lower() for s in sub_names):
                    matched.append(item)
            except (OSError, ValueError):
                pass
    return matched


async def _passive_probe_reddit_auth(ws_url: str) -> tuple[bool, str | None, str]:
    """Passively inspect an open Reddit tab for OAuth session without reloading or opening tabs."""
    try:
        async with CDPClient(ws_url) as cdp:
            candidates = await _extract_token_candidates_from_tab(cdp)
    except Exception:
        return False, None, "cdp_probe_failed"

    if not candidates:
        return False, None, "unauthenticated"

    for token in candidates:
        try:
            username = await asyncio.to_thread(_validate_reddit_token, token)
        except CDPError:
            return False, None, "network_error"
        if username:
            return True, username, "authenticated"
    return False, None, "unauthenticated"


def run_doctor(cdp_port: int = DEFAULT_CDP_PORT) -> dict[str, Any]:
    """Run read-only environment, ontology, browser, CDP, auth, and PDF tool diagnostics."""
    port = validate_cdp_port(cdp_port)
    hints: list[str] = []

    py_ver = platform.python_version()
    py_ok = sys.version_info >= (3, 10)
    if not py_ok:
        hints.append("Upgrade Python to 3.10+ (Fedora: sudo dnf install python3).")

    try:
        import websockets as _ws_mod

        ws_ver = str(getattr(_ws_mod, "__version__", "installed"))
    except Exception:
        ws_ver = "missing"
        hints.append("Install websockets>=11.0 (pip install -r requirements.txt).")

    req_ver = str(getattr(requests, "__version__", "installed"))

    ontology_loaded = False
    ontology_count = 0
    ontology_error: str | None = None
    try:
        ont = _load_ontology()
        ontology_loaded = True
        ontology_count = len(ont)
    except Exception as exc:
        ontology_error = str(exc)
        hints.append("Ensure curator_data/subreddit_ontology.json is present or reinstall package.")

    presets = list_presets_metadata(PRESETS_DIR)
    presets_loaded = len(presets) > 0
    if not presets_loaded:
        hints.append(f"No preset JSON files found in {PRESETS_DIR}.")

    browsers = BrowserLauncher.detect_browsers()
    if not browsers:
        hints.append(
            "No Chromium/Chrome/Brave browser detected. Install on Fedora via "
            "'sudo dnf install chromium' or 'flatpak install flathub com.google.Chrome'."
        )

    cdp_reachable = False
    browser_version: str | None = None
    reddit_tab_open = False
    reddit_ws_url: str | None = None
    try:
        ver_resp = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=1.5)
        if ver_resp.status_code == 200:
            ver_data = ver_resp.json()
            if isinstance(ver_data, dict):
                cdp_reachable = True
                browser_version = str(ver_data.get("Browser") or "") or None
    except (requests.RequestException, ValueError):
        cdp_reachable = False

    if cdp_reachable:
        try:
            list_resp = requests.get(f"http://127.0.0.1:{port}/json/list", timeout=1.5)
            if list_resp.status_code == 200:
                targets = list_resp.json()
                if isinstance(targets, list):
                    reddit_target = _find_reddit_page_target(
                        [t for t in targets if isinstance(t, dict)]
                    )
                    if reddit_target is not None:
                        reddit_tab_open = True
                        raw_ws = reddit_target.get("webSocketDebuggerUrl")
                        if isinstance(raw_ws, str) and raw_ws.strip():
                            reddit_ws_url = raw_ws.strip()
        except (requests.RequestException, ValueError):
            pass
    else:
        hints.append(
            f"CDP port {port} is not reachable. Launch a debugging browser with: "
            f"python3 reddit_curator.py --launch-browser --port {port}"
        )

    reddit_authenticated = False
    reddit_username: str | None = None
    auth_status = "cdp_unreachable"
    if cdp_reachable:
        if not reddit_tab_open or not reddit_ws_url:
            auth_status = "no_reddit_tab"
            hints.append(
                "Open https://www.reddit.com in the debugging browser and sign in."
            )
        else:
            reddit_authenticated, reddit_username, auth_status = asyncio.run(
                _passive_probe_reddit_auth(reddit_ws_url)
            )
            if auth_status == "unauthenticated":
                hints.append(
                    "Sign in to https://www.reddit.com in the debugging browser."
                )
            elif auth_status == "network_error":
                hints.append(
                    "Reddit tab found, but oauth.reddit.com could not be reached to validate session."
                )

    pdftotext_path = shutil.which("pdftotext")
    pypdf_available = importlib.util.find_spec("pypdf") is not None
    if pypdf_available:
        pdf_backend = "pypdf"
    elif pdftotext_path:
        pdf_backend = "pdftotext"
    else:
        pdf_backend = "builtin_pure_python"
        hints.append(
            "Optional PDF enhancement: install Poppler on Fedora ('sudo dnf install poppler-utils') "
            "or 'pip install pypdf' (built-in pure-Python PDF parser remains active)."
        )

    snaps_dir = resolve_snapshots_dir()
    snaps_writable = False
    if snaps_dir.exists():
        snaps_writable = os.access(snaps_dir, os.W_OK | os.X_OK)
    else:
        parent = snaps_dir.parent
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        snaps_writable = os.access(parent, os.W_OK | os.X_OK)

    return {
        "python": {"version": py_ver, "ok": py_ok},
        "dependencies": {"requests": req_ver, "websockets": ws_ver},
        "ontology": {
            "loaded": ontology_loaded,
            "count": ontology_count,
            "path": str(ONTOLOGY_PATH),
            "error": ontology_error,
        },
        "presets": {
            "loaded": presets_loaded,
            "count": len(presets),
            "dir": str(PRESETS_DIR),
        },
        "browsers": browsers,
        "cdp": {
            "port": port,
            "reachable": cdp_reachable,
            "browser_version": browser_version,
            "reddit_tab_open": reddit_tab_open,
        },
        "reddit_auth": {
            "authenticated": reddit_authenticated,
            "username": reddit_username,
            "status": auth_status,
        },
        "pdf_tools": {
            "pdftotext_path": pdftotext_path,
            "pypdf_available": pypdf_available,
            "active_backend": pdf_backend,
        },
        "snapshots": {
            "dir": str(snaps_dir),
            "writable": snaps_writable,
        },
        "hints": hints,
    }


def format_doctor_report(report: dict[str, Any]) -> str:
    """Format `doctor` diagnostics into a concise terminal checklist."""
    py = report.get("python", {})
    deps = report.get("dependencies", {})
    ont = report.get("ontology", {})
    pre = report.get("presets", {})
    browsers = report.get("browsers", [])
    cdp = report.get("cdp", {})
    auth = report.get("reddit_auth", {})
    pdf = report.get("pdf_tools", {})
    snaps = report.get("snapshots", {})

    browser_str = (
        ", ".join(f"{b['name']} ({b['kind']})" for b in browsers)
        if browsers
        else "none detected"
    )
    cdp_state = (
        f"reachable ({cdp.get('browser_version') or 'CDP'})"
        if cdp.get("reachable")
        else "unreachable"
    )
    auth_str = (
        f"signed in as u/{auth['username']}"
        if auth.get("authenticated")
        else str(auth.get("status") or "unauthenticated")
    )
    lines = [
        f"reddit-curator v{__version__} Doctor Diagnostics",
        "=" * 64,
        f"  Python             : {py.get('version')} ({'OK' if py.get('ok') else 'OUTDATED'})",
        f"  Dependencies       : requests={deps.get('requests')}, websockets={deps.get('websockets')}",
        f"  Ontology           : {'OK' if ont.get('loaded') else 'FAIL'} ({ont.get('count', 0)} subreddits)",
        f"  Presets            : {'OK' if pre.get('loaded') else 'MISSING'} ({pre.get('count', 0)} presets)",
        f"  Browsers           : {browser_str}",
        f"  CDP (127.0.0.1:{cdp.get('port')}) : {cdp_state} (reddit_tab={cdp.get('reddit_tab_open')})",
        f"  Reddit Auth        : {auth_str}",
        f"  PDF Tools          : pypdf={pdf.get('pypdf_available')}, "
        f"pdftotext={pdf.get('pdftotext_path') or 'missing'} (backend={pdf.get('active_backend')})",
        f"  Snapshots Dir      : {snaps.get('dir')} ({'writable' if snaps.get('writable') else 'read-only'})",
    ]
    hints = report.get("hints") or []
    if hints:
        lines.append("  Fedora / Next-Step Hints:")
        for hint in hints:
            lines.append(f"    * {hint}")
    return "\n".join(lines)


def _compare_presets(
    preset_spec: str, ontology: OntologyRepository
) -> dict[str, Any]:
    """Load, normalize, audit, and compare two presets 'A,B'."""
    parts = [p.strip() for p in str(preset_spec or "").split(",") if p.strip()]
    if len(parts) != 2:
        raise ValueError(
            f"--compare-presets requires exactly two comma-separated preset names (e.g. A,B); got {preset_spec!r}."
        )
    id_a, id_b = parts
    path_a = resolve_manifest_path(preset_arg=id_a)
    path_b = resolve_manifest_path(preset_arg=id_b)
    norm_a = normalize_manifest(json.loads(path_a.read_text(encoding="utf-8")), ontology)
    norm_b = normalize_manifest(json.loads(path_b.read_text(encoding="utf-8")), ontology)

    auditor = FeedHealthAuditor(ontology)
    audit_a = auditor.audit_manifest(norm_a)
    audit_b = auditor.audit_manifest(norm_b)

    def _tier1_names(m: dict[str, Any]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for key in ("anchor_subreddits", "domain_priming_subreddits"):
            for item in m.get(key) or []:
                name = str(item["name"] if isinstance(item, dict) else item).strip()
                if name and name.lower() not in seen:
                    seen.add(name.lower())
                    out.append(name)
        return out

    subs_a = _tier1_names(norm_a)
    subs_b = _tier1_names(norm_b)
    map_a = {s.lower(): s for s in subs_a}
    map_b = {s.lower(): s for s in subs_b}

    shared = [map_a[k] for k in map_a if k in map_b]
    only_a = [v for k, v in map_a.items() if k not in map_b]
    only_b = [v for k, v in map_b.items() if k not in map_a]

    return {
        "preset_a": {
            "id": id_a,
            "name": norm_a.get("name"),
            "tier1_count": len(subs_a),
            "custom_feeds_count": len(norm_a.get("custom_feeds") or []),
            "audit": audit_a,
        },
        "preset_b": {
            "id": id_b,
            "name": norm_b.get("name"),
            "tier1_count": len(subs_b),
            "custom_feeds_count": len(norm_b.get("custom_feeds") or []),
            "audit": audit_b,
        },
        "shared_subreddits": shared,
        "only_in_a": only_a,
        "only_in_b": only_b,
    }


def _format_preset_comparison(comp: dict[str, Any]) -> str:
    """Format side-by-side preset comparison for terminal output."""
    pa = comp["preset_a"]
    pb = comp["preset_b"]
    aa = pa["audit"]
    ab = pb["audit"]
    act_a = aa.get("actual_velocity_distribution_pct", {})
    act_b = ab.get("actual_velocity_distribution_pct", {})
    lines = [
        f"Preset Comparison: {pa['id']} vs {pb['id']}",
        "=" * 72,
        f"  {'Metric':<24} | {pa['id']:<21} | {pb['id']:<21}",
        "-" * 72,
        f"  {'Title':<24} | {str(pa['name'])[:21]:<21} | {str(pb['name'])[:21]:<21}",
        f"  {'Tier-1 Subreddits':<24} | {pa['tier1_count']:<21} | {pb['tier1_count']:<21}",
        f"  {'Custom Feeds':<24} | {pa['custom_feeds_count']:<21} | {pb['custom_feeds_count']:<21}",
        f"  {'Composite Health':<24} | {aa.get('composite_health_score', 0.0):<21.2f} | {ab.get('composite_health_score', 0.0):<21.2f}",
        f"  {'Golden Ratio Fidelity':<24} | {aa.get('golden_ratio_fidelity_score', 0.0):<21.2f} | {ab.get('golden_ratio_fidelity_score', 0.0):<21.2f}",
        f"  {'Daily Velocity':<24} | {aa.get('total_daily_velocity', 0.0):<21.1f} | {ab.get('total_daily_velocity', 0.0):<21.1f}",
        f"  {'Aes/Cog/Dom (%)':<24} | "
        f"{act_a.get('aesthetic_leisure', 0):.0f}/{act_a.get('cognitive_curiosity', 0):.0f}/{act_a.get('domain_priming', 0):.0f}"
        f"{'':<13} | "
        f"{act_b.get('aesthetic_leisure', 0):.0f}/{act_b.get('cognitive_curiosity', 0):.0f}/{act_b.get('domain_priming', 0):.0f}",
        "-" * 72,
        f"  Shared Tier-1 ({len(comp['shared_subreddits'])}): {', '.join(f'r/{s}' for s in comp['shared_subreddits']) or 'none'}",
        f"  Only in {pa['id']} ({len(comp['only_in_a'])}): {', '.join(f'r/{s}' for s in comp['only_in_a'][:12])}{'...' if len(comp['only_in_a']) > 12 else ''}",
        f"  Only in {pb['id']} ({len(comp['only_in_b'])}): {', '.join(f'r/{s}' for s in comp['only_in_b'][:12])}{'...' if len(comp['only_in_b']) > 12 else ''}",
    ]
    return "\n".join(lines)


def _validate_manifest_payload(
    raw: Any, ontology: OntologyRepository
) -> tuple[dict[str, Any] | None, list[str], list[str], dict[str, Any] | None]:
    """Validate manifest schema, types, feed names, and Diet Health audit."""
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(raw, dict):
        return None, ["Manifest root must be a JSON object."], warnings, None

    if not str(raw.get("name") or "").strip():
        warnings.append("Manifest is missing a non-empty 'name' field.")

    for key in (
        "anchor_subreddits",
        "domain_priming_subreddits",
        "tier1_systems_subreddits",
        "custom_feeds",
    ):
        val = raw.get(key)
        if val is not None and not isinstance(val, list):
            errors.append(f"Field '{key}' must be a list when provided.")

    raw_feeds = raw.get("custom_feeds")
    if isinstance(raw_feeds, list):
        for idx, feed in enumerate(raw_feeds):
            if not isinstance(feed, dict):
                errors.append(f"custom_feeds[{idx}] must be an object.")
                continue
            fname = str(feed.get("feed_name") or "").strip()
            if not fname:
                errors.append(f"custom_feeds[{idx}] is missing 'feed_name'.")
            else:
                try:
                    validate_feed_name(fname)
                except ValueError as exc:
                    errors.append(str(exc))

    if errors:
        return None, errors, warnings, None

    base_check = validate_manifest(raw, ontology)
    for err in base_check.get("errors") or []:
        if err not in errors:
            errors.append(str(err))
    for warn in base_check.get("warnings") or []:
        if warn not in warnings:
            warnings.append(str(warn))
    if errors:
        return None, errors, warnings, None

    norm = base_check.get("normalized") or normalize_manifest(raw, ontology)
    anchors = norm.get("anchor_subreddits") or []
    priming = norm.get("domain_priming_subreddits") or []
    if not anchors and not priming:
        errors.append(
            "Manifest must contain at least one subreddit in anchor_subreddits or domain_priming_subreddits."
        )

    audit = FeedHealthAuditor(ontology).audit_manifest(norm)
    unrec = audit.get("unrecognized_subreddits") or []
    if unrec:
        warnings.append(f"Unrecognized subreddits in ontology: {', '.join(unrec[:10])}")
    for rec in audit.get("recommendations") or []:
        rec_str = str(rec)
        if not rec_str.startswith("Information diet is well-balanced") and rec_str not in warnings:
            warnings.append(rec_str)

    return norm, errors, warnings, audit


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


def _write_manifest_file(path: Path, manifest: dict[str, Any]) -> Path:
    out_path = Path(path).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    logger.info("Wrote manifest to: %s", out_path)
    return out_path


def _dispatch(args: argparse.Namespace) -> int:
    """Dispatch CLI request according to offline-first v2.0 routing."""
    validate_cdp_port(args.port)
    validate_studio_port(args.studio_port)

    if args.preset and args.manifest:
        raise ValueError("Conflicting flags: cannot specify both --preset and --manifest.")
    if args.mix and (args.preset or args.manifest):
        raise ValueError("Conflicting flags: cannot combine --mix with --preset or --manifest.")

    if args.command == "doctor":
        report = run_doctor(cdp_port=args.port)
        if args.agent:
            _emit_json(report)
        else:
            print(format_doctor_report(report))
        return 0

    if args.command in ("studio", "ui") or args.studio:
        run_studio(
            host="127.0.0.1",
            port=args.studio_port,
            cdp_port=args.port,
            open_browser=not args.no_open_browser,
        )
        return 0

    snapshots_dir = resolve_snapshots_dir()

    if args.command == "wizard":
        run_wizard(
            presets_dir=PRESETS_DIR,
            ontology_path=ONTOLOGY_PATH,
            snapshots_dir=snapshots_dir,
            cdp_port=args.port,
            github_token=args.github_token,
        )
        return 0

    if args.list_presets:
        presets = _filter_presets_by_query(list_presets_metadata(PRESETS_DIR), args.query)
        if args.agent:
            payload: dict[str, Any] = {"presets": presets, "count": len(presets)}
            if args.query:
                payload["query"] = args.query
            _emit_json(payload)
        else:
            print(format_presets_table(presets))
        return 0

    if args.list_snapshots:
        snaps = SnapshotManager(snapshots_dir).list_snapshots()
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

    ontology = _load_ontology()

    if args.preset_info:
        mpath = resolve_manifest_path(preset_arg=args.preset_info)
        raw = json.loads(mpath.read_text(encoding="utf-8"))
        norm = normalize_manifest(raw, ontology)
        audit = FeedHealthAuditor(ontology).audit_manifest(norm)
        if args.agent:
            _emit_json(
                {
                    "preset_id": args.preset_info,
                    "manifest_path": str(mpath),
                    "manifest": norm,
                    "audit": audit,
                }
            )
        else:
            desc = str(norm.get("description") or "").strip()
            print(format_manifest_summary(norm))
            if desc:
                print(f"  Description        : {desc}")
            print()
            print(format_audit_summary(audit, title=f"Preset Diet Health: {norm.get('name')}"))
        return 0

    if args.compare_presets:
        comp = _compare_presets(args.compare_presets, ontology)
        if args.agent:
            _emit_json(comp)
        else:
            print(_format_preset_comparison(comp))
        return 0

    if args.validate:
        mpath = resolve_manifest_path(args.manifest, args.preset)
        try:
            raw = json.loads(mpath.read_text(encoding="utf-8"))
        except ValueError as exc:
            err_msg = f"Invalid JSON in {mpath}: {exc}"
            if args.agent:
                _emit_json(
                    {
                        "valid": False,
                        "manifest_path": str(mpath),
                        "errors": [err_msg],
                        "warnings": [],
                        "audit": None,
                    }
                )
            else:
                print(f"Validation FAILED ({mpath}):\n  * {err_msg}")
            return 1

        norm, errors, warnings, audit = _validate_manifest_payload(raw, ontology)
        valid = len(errors) == 0
        if args.agent:
            _emit_json(
                {
                    "valid": valid,
                    "manifest_name": norm.get("name") if isinstance(norm, dict) else None,
                    "manifest_path": str(mpath),
                    "errors": errors,
                    "warnings": warnings,
                    "audit": audit,
                }
            )
        else:
            status_label = "VALID" if valid else "INVALID"
            print(f"Manifest Validation [{status_label}]: {mpath}")
            for err in errors:
                print(f"  ERROR: {err}")
            for warn in warnings:
                print(f"  WARN : {warn}")
            if isinstance(audit, dict):
                print()
                print(format_audit_summary(audit, title=f"Diet Health: {norm.get('name') if norm else mpath.name}"))
        return 0 if valid else 1

    if args.snapshot:
        session = acquire_reddit_session(args.port)
        api = RedditAPIClient(session.access_token, session.username)
        live_subs = api.fetch_all_subscriptions()
        live_feeds = api.fetch_custom_feeds()
        snap = SnapshotManager(snapshots_dir).create_snapshot(
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
        res = SnapshotManager(snapshots_dir).rollback(
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

    exported_manifest: Optional[dict[str, Any]] = None
    if not has_generation and args.output is not None:
        mpath = resolve_manifest_path(args.manifest, args.preset)
        raw = json.loads(mpath.read_text(encoding="utf-8"))
        exported_manifest = normalize_manifest(raw, ontology)
        out_written = _write_manifest_file(args.output, exported_manifest)
        if not needs_live_or_dry:
            audit_report = (
                FeedHealthAuditor(ontology).audit_manifest(exported_manifest)
                if args.audit
                else None
            )
            if args.agent:
                out_payload = {
                    "manifest": exported_manifest,
                    "output_path": str(out_written),
                }
                if args.audit:
                    out_payload["audit"] = audit_report
                _emit_json(out_payload)
            else:
                print(format_manifest_summary(exported_manifest, saved_path=out_written))
                if isinstance(audit_report, dict):
                    print()
                    print(
                        format_audit_summary(
                            audit_report,
                            title=f"Manifest Diet Health Audit: {exported_manifest.get('name')}",
                        )
                    )
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
        snapshots_dir=snapshots_dir,
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
        diff = SnapshotManager(snapshots_dir).compute_diff(
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


def _is_cdp_connection_error(exc: BaseException) -> bool:
    """Return True if an exception indicates a Chrome CDP connection or tab/session failure."""
    if isinstance(exc, (CDPError, ConnectionError, requests.ConnectionError)):
        return True
    if isinstance(exc, RuntimeError):
        msg = str(exc).lower()
        return any(
            kw in msg
            for kw in ("cdp", "port", "tab", "connection refused", "/json/list", "remote debugging")
        )
    return False


def _format_cdp_evaluator_hint(port: int) -> str:
    """Build an actionable human-mode hint when Chrome CDP is unreachable."""
    return (
        "\nHint: Chrome DevTools Protocol (CDP) is unreachable or no logged-in Reddit tab was found.\n"
        f"  - Launch a CDP browser and log into Reddit: python3 reddit_curator.py --launch-browser --port {port}\n"
        "  - Or try an offline preview / audit (zero Reddit login needed):\n"
        "      python3 reddit_curator.py --preset systems_engineering --dry-run\n"
        "      python3 reddit_curator.py --preset systems_engineering --audit\n"
        "      python3 reddit_curator.py studio"
    )


def main(argv: Sequence[str] | None = None) -> None:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if len(raw_argv) == 0:
        print(_format_quickstart_and_help())
        return

    args = parse_args(raw_argv)

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
            if _is_cdp_connection_error(exc):
                print(_format_cdp_evaluator_hint(args.port), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
