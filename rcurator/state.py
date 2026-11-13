"""Pre-mutation snapshot persistence, live diffing, and rollback management."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from rcurator.reddit_api import RedditAPIClient

_VALID_SNAPSHOT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def _extract_manifest_subreddits(manifest: dict[str, Any]) -> list[str]:
    """Extract deduplicated desired subreddit names across anchors and domain priming."""
    raw_entries: list[Any] = []
    for key in ("anchor_subreddits", "domain_priming_subreddits", "tier1_systems_subreddits"):
        val = manifest.get(key)
        if isinstance(val, list):
            raw_entries.extend(val)

    desired: list[str] = []
    seen: set[str] = set()
    for entry in raw_entries:
        if isinstance(entry, dict):
            name = str(entry.get("name") or "").strip()
        else:
            name = str(entry or "").strip()
        if not name:
            continue
        low = name.lower()
        if low not in seen:
            seen.add(low)
            desired.append(name)
    return desired


def _normalize_subscription_list(subscriptions: list[str] | dict[str, Any]) -> list[str]:
    if isinstance(subscriptions, dict):
        out: list[str] = []
        for key, val in subscriptions.items():
            if isinstance(val, dict) and val.get("name"):
                out.append(str(val["name"]))
            else:
                out.append(str(key))
        return out
    return [str(s) for s in subscriptions if str(s).strip()]


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write JSON with strict 0600 file permissions."""
    tmp_path = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    serialized = json.dumps(payload, indent=2, sort_keys=False) + "\n"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(serialized)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, path)
    os.chmod(path, 0o600)


class SnapshotManager:
    """Manage 0700/0600 account state snapshots, manifest diffs, and rollbacks."""

    def __init__(self, root_dir: Path | str = Path(".curator_snapshots")) -> None:
        self.root_dir = Path(root_dir).expanduser().resolve()

    def _ensure_root_dir(self) -> None:
        self.root_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root_dir, 0o700)

    def _resolve_snapshot_path(self, snapshot_id: str) -> Path:
        if not isinstance(snapshot_id, str) or not snapshot_id:
            raise ValueError("Invalid snapshot_id: must be a non-empty string.")
        if "\x00" in snapshot_id or "/" in snapshot_id or "\\" in snapshot_id or ".." in snapshot_id:
            raise ValueError(f"Invalid snapshot_id {snapshot_id!r}: path traversal is forbidden.")

        stem = snapshot_id[:-5] if snapshot_id.endswith(".json") else snapshot_id
        if not _VALID_SNAPSHOT_ID_RE.fullmatch(stem):
            raise ValueError(
                f"Invalid snapshot_id {snapshot_id!r}: must match ^[A-Za-z0-9][A-Za-z0-9_-]{{0,127}}$."
            )

        candidate = (self.root_dir / f"{stem}.json").resolve()
        if not candidate.is_relative_to(self.root_dir) or candidate.parent != self.root_dir:
            raise ValueError(f"Invalid snapshot_id {snapshot_id!r}: resolved outside snapshot root.")
        return candidate

    def create_snapshot(
        self,
        username: str,
        subscriptions: list[str] | dict[str, Any],
        custom_feeds: list[dict[str, Any]],
        manifest: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist a pre-mutation snapshot of subscriptions, custom feeds, and manifest."""
        self._ensure_root_dir()

        now = datetime.now(timezone.utc)
        ts_slug = now.strftime("%Y%m%dT%H%M%S%fZ")
        user_slug = re.sub(r"[^a-zA-Z0-9_-]", "_", (username or "unknown").strip())[:40] or "unknown"
        base_id = f"snapshot_{ts_slug}_{user_slug}"

        snapshot_id = base_id
        target_path = self._resolve_snapshot_path(snapshot_id)
        suffix = 1
        while target_path.exists():
            snapshot_id = f"{base_id}_{suffix}"
            target_path = self._resolve_snapshot_path(snapshot_id)
            suffix += 1

        sub_list = _normalize_subscription_list(subscriptions)
        feeds_list = list(custom_feeds or [])
        manifest_name = str(manifest["name"]) if isinstance(manifest, dict) and manifest.get("name") else None
        created_at = now.isoformat()

        stored = {
            "snapshot_id": snapshot_id,
            "created_at": created_at,
            "username": username,
            "subscriptions_count": len(sub_list),
            "custom_feeds_count": len(feeds_list),
            "subscriptions": sub_list,
            "custom_feeds": feeds_list,
            "manifest_name": manifest_name,
            "manifest": manifest,
        }
        _write_private_json(target_path, stored)

        return {
            "snapshot_id": snapshot_id,
            "created_at": created_at,
            "username": username,
            "path": str(target_path),
            "subscriptions_count": len(sub_list),
            "custom_feeds_count": len(feeds_list),
            "subscriptions": sub_list,
            "custom_feeds": feeds_list,
            "manifest_name": manifest_name,
        }

    def list_snapshots(self) -> list[dict[str, Any]]:
        """List available snapshots sorted newest-first."""
        if not self.root_dir.exists():
            return []

        summaries: list[dict[str, Any]] = []
        for entry in self.root_dir.iterdir():
            if entry.is_symlink() or not entry.is_file() or entry.suffix != ".json":
                continue
            resolved = entry.resolve()
            if not resolved.is_relative_to(self.root_dir):
                continue
            try:
                data = json.loads(resolved.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict) or not data.get("snapshot_id"):
                continue

            subs = data.get("subscriptions", [])
            feeds = data.get("custom_feeds", [])
            summaries.append(
                {
                    "snapshot_id": str(data["snapshot_id"]),
                    "created_at": str(data.get("created_at") or ""),
                    "username": str(data.get("username") or ""),
                    "subscriptions_count": int(data.get("subscriptions_count", len(subs))),
                    "custom_feeds_count": int(data.get("custom_feeds_count", len(feeds))),
                    "manifest_name": data.get("manifest_name"),
                    "path": str(resolved),
                }
            )

        summaries.sort(key=lambda item: (item["created_at"], item["snapshot_id"]), reverse=True)
        return summaries

    def load_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        """Load a snapshot by exact ID or 'latest', enforcing strict path traversal checks."""
        if snapshot_id == "latest":
            available = self.list_snapshots()
            if not available:
                raise FileNotFoundError(f"No snapshots found in {self.root_dir}.")
            snapshot_id = available[0]["snapshot_id"]

        target_path = self._resolve_snapshot_path(snapshot_id)
        if not target_path.exists() or target_path.is_symlink():
            raise FileNotFoundError(f"Snapshot {snapshot_id!r} not found.")

        data = json.loads(target_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"Corrupt snapshot file: {target_path}")
        data["path"] = str(target_path)
        return data

    def compute_diff(
        self,
        manifest: dict[str, Any],
        live_subs: list[str],
        live_feeds: list[dict[str, Any]],
        prune: bool = False,
        keep: set[str] | None = None,
    ) -> dict[str, Any]:
        """Compute exact subscription and custom-feed deltas between manifest and live state."""
        desired_subs = _extract_manifest_subreddits(manifest)
        desired_map = {s.lower(): s for s in desired_subs}

        live_map: dict[str, str] = {}
        for sub in live_subs:
            clean = str(sub).strip()
            if clean and clean.lower() not in live_map:
                live_map[clean.lower()] = clean

        to_subscribe = [v for k, v in desired_map.items() if k not in live_map]
        already_subscribed = [live_map[k] for k in desired_map if k in live_map]

        keep_lower = {str(x).strip().lower() for x in (keep or set()) if str(x).strip()}
        to_unsubscribe: list[str] = []
        unmanaged_kept: list[str] = []
        for low, orig in live_map.items():
            if low not in desired_map:
                if prune and low not in keep_lower:
                    to_unsubscribe.append(orig)
                else:
                    unmanaged_kept.append(orig)

        live_feeds_map = {
            str(f["feed_name"]): f
            for f in live_feeds
            if isinstance(f, dict) and f.get("feed_name")
        }

        custom_feeds_diff: dict[str, dict[str, Any]] = {}
        for desired_feed in manifest.get("custom_feeds", []):
            if not isinstance(desired_feed, dict) or not desired_feed.get("feed_name"):
                continue
            feed_name = str(desired_feed["feed_name"])
            raw_subs = desired_feed.get("subreddits", [])
            final_subs: list[str] = []
            seen_f: set[str] = set()
            for sub in raw_subs:
                clean = str(sub).strip()
                if clean and clean.lower() not in seen_f:
                    seen_f.add(clean.lower())
                    final_subs.append(clean)

            if feed_name not in live_feeds_map:
                custom_feeds_diff[feed_name] = {
                    "action": "create",
                    "added_subreddits": final_subs,
                    "removed_subreddits": [],
                    "final_subreddits": final_subs,
                }
            else:
                existing_subs = [str(s) for s in live_feeds_map[feed_name].get("subreddits", [])]
                existing_low = {s.lower(): s for s in existing_subs}
                desired_low = {s.lower(): s for s in final_subs}
                added = [s for k, s in desired_low.items() if k not in existing_low]
                removed = [s for k, s in existing_low.items() if k not in desired_low]
                action = "unchanged" if (not added and not removed) else "update"
                custom_feeds_diff[feed_name] = {
                    "action": action,
                    "added_subreddits": added,
                    "removed_subreddits": removed,
                    "final_subreddits": final_subs,
                }

        has_changes = bool(
            to_subscribe
            or to_unsubscribe
            or any(entry["action"] != "unchanged" for entry in custom_feeds_diff.values())
        )
        return {
            "to_subscribe": to_subscribe,
            "already_subscribed": already_subscribed,
            "to_unsubscribe": to_unsubscribe,
            "unmanaged_kept": unmanaged_kept,
            "custom_feeds": custom_feeds_diff,
            "has_changes": has_changes,
        }

    def rollback(
        self,
        snapshot_id: str,
        api_client: RedditAPIClient,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Restore subscriptions and custom feeds to the exact state stored in snapshot_id."""
        snapshot = self.load_snapshot(snapshot_id)
        resolved_id = str(snapshot["snapshot_id"])
        target_subs = [str(s) for s in snapshot.get("subscriptions", [])]
        target_feeds = [f for f in snapshot.get("custom_feeds", []) if isinstance(f, dict)]

        live_feeds = api_client.fetch_custom_feeds()
        sub_sync = api_client.sync_subscriptions(desired=target_subs, dry_run=dry_run, prune=True)
        restored_feeds = api_client.sync_custom_feeds(target_feeds, dry_run=dry_run) if target_feeds else {}

        target_feed_names = {str(f.get("feed_name") or "") for f in target_feeds}
        deleted_feeds: list[str] = []
        delete_failed = False
        for live_feed in live_feeds:
            feed_name = str(live_feed.get("feed_name") or "")
            if not feed_name or feed_name in target_feed_names:
                continue
            if dry_run:
                deleted_feeds.append(feed_name)
            else:
                target_ref = str(live_feed.get("path") or feed_name)
                if api_client.delete_custom_feed(target_ref, dry_run=False):
                    deleted_feeds.append(feed_name)
                else:
                    delete_failed = True

        feeds_ok = all(item.get("status") != "failed" for item in restored_feeds.values())
        subs_ok = len(sub_sync.get("failed", [])) == 0
        return {
            "snapshot_id": resolved_id,
            "username": str(snapshot.get("username") or api_client.username),
            "dry_run": dry_run,
            "restored_subscriptions": list(sub_sync["subscribed"]),
            "removed_subscriptions": list(sub_sync["unsubscribed"]),
            "restored_custom_feeds": restored_feeds,
            "deleted_custom_feeds": deleted_feeds,
            "success": bool(subs_ok and feeds_ok and not delete_failed),
        }
