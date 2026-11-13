"""Pooled Reddit OAuth API client with rate-limit throttling and retry backoff."""

from __future__ import annotations

import json
import random
import re
import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter

_BASE_URL = "https://oauth.reddit.com"
_FEED_NAME_RE = re.compile(r"^[a-z0-9_]{1,50}$")
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
_MAX_CUSTOM_FEED_SUBS = 100


def validate_feed_name(feed_name: str) -> str:
    """Validate that a custom feed slug matches Reddit's ^[a-z0-9_]{1,50}$ rule."""
    if not isinstance(feed_name, str) or not _FEED_NAME_RE.fullmatch(feed_name):
        raise ValueError(
            f"Invalid custom feed name {feed_name!r}; must match ^[a-z0-9_]{{1,50}}$."
        )
    return feed_name


def _partition_feed_subreddits(
    feed_name: str,
    display_name: str,
    subreddits: list[str],
) -> list[tuple[str, str, list[str]]]:
    """Split a custom feed exceeding 100 subreddits into valid <=100-item chunks."""
    validate_feed_name(feed_name)

    deduped: list[str] = []
    seen: set[str] = set()
    for sub in subreddits:
        clean = str(sub).strip()
        if not clean:
            continue
        low = clean.lower()
        if low not in seen:
            seen.add(low)
            deduped.append(clean)

    if len(deduped) <= _MAX_CUSTOM_FEED_SUBS:
        return [(feed_name, display_name or feed_name, deduped)]

    chunks: list[tuple[str, str, list[str]]] = []
    for idx, offset in enumerate(range(0, len(deduped), _MAX_CUSTOM_FEED_SUBS), start=1):
        sub_slice = deduped[offset : offset + _MAX_CUSTOM_FEED_SUBS]
        if idx == 1:
            chunk_name = feed_name
            chunk_display = display_name or feed_name
        else:
            suffix = f"_{idx}"
            base = feed_name[: 50 - len(suffix)]
            chunk_name = f"{base}{suffix}"
            base_display = display_name or feed_name
            chunk_display = f"{base_display} ({idx})"
        chunks.append((chunk_name, chunk_display, sub_slice))
    return chunks


def _normalize_subreddit_record(data: dict[str, Any]) -> dict[str, Any]:
    name = str(data.get("display_name") or data.get("name") or "")
    fullname = str(data.get("name") or "")
    if not fullname.startswith("t5_") and data.get("id"):
        fullname = f"t5_{data['id']}"
    return {
        "name": name,
        "fullname": fullname,
        "subscribers": int(data.get("subscribers") or 0),
        "over18": bool(data.get("over18", False)),
        "public_description": str(data.get("public_description") or ""),
        "url": str(data.get("url") or f"/r/{name}/"),
    }


class RedditAPIClient:
    """Pooled Reddit OAuth API client with proactive rate-limit awareness."""

    def __init__(
        self,
        access_token: str,
        username: str,
        *,
        max_retries: int = 4,
        timeout: float = 15.0,
        session: requests.Session | None = None,
    ) -> None:
        if not access_token:
            raise ValueError("access_token must be non-empty.")
        if not username:
            raise ValueError("username must be non-empty.")

        self.access_token = access_token
        self.username = username
        self.max_retries = max(0, int(max_retries))
        self.timeout = float(timeout)

        if session is not None:
            self.session = session
        else:
            self.session = requests.Session()
            adapter = HTTPAdapter(pool_connections=10, pool_maxsize=10)
            self.session.mount("https://", adapter)
            self.session.mount("http://", adapter)

        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.access_token}",
                "User-Agent": f"linux:reddit-curator:2.0.0 (by /u/{self.username})",
            }
        )

    def _compute_retry_delay(self, attempt: int, response: requests.Response | None = None) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After") or response.headers.get("retry-after")
            if retry_after is not None:
                try:
                    parsed = float(retry_after)
                    if parsed >= 0:
                        return min(parsed, 60.0)
                except ValueError:
                    pass
        base = min(30.0, 1.0 * (2**attempt))
        return base * random.uniform(0.5, 1.0)

    def _apply_proactive_ratelimit(self, response: requests.Response) -> None:
        remaining_raw = response.headers.get("x-ratelimit-remaining")
        if remaining_raw is None:
            return
        try:
            remaining = float(remaining_raw)
        except (TypeError, ValueError):
            return
        if remaining < 2.0:
            reset_raw = response.headers.get("x-ratelimit-reset")
            sleep_for = 1.0
            if reset_raw is not None:
                try:
                    sleep_for = max(0.1, min(float(reset_raw), 60.0))
                except (TypeError, ValueError):
                    sleep_for = 1.0
            time.sleep(sleep_for)

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> requests.Response:
        url = endpoint if endpoint.startswith("http") else f"{_BASE_URL}{endpoint}"
        merged_params = dict(params or {})
        merged_params["raw_json"] = 1

        last_exc: requests.RequestException | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.session.request(
                    method=method.upper(),
                    url=url,
                    params=merged_params,
                    data=data,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    time.sleep(self._compute_retry_delay(attempt))
                    continue
                raise

            if resp.status_code in _RETRYABLE_STATUS_CODES and attempt < self.max_retries:
                time.sleep(self._compute_retry_delay(attempt, resp))
                continue

            self._apply_proactive_ratelimit(resp)
            return resp

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Unreachable retry loop exit")

    def fetch_all_subscriptions(self, max_pages: int = 50) -> dict[str, dict[str, Any]]:
        """Paginate all user subreddit subscriptions with cycle protection."""
        subscriptions: dict[str, dict[str, Any]] = {}
        seen_cursors: set[str] = set()
        after: str | None = None

        for _ in range(max_pages):
            params: dict[str, Any] = {"limit": 100}
            if after is not None:
                params["after"] = after

            resp = self._request("GET", "/subreddits/mine/subscriber", params=params)
            resp.raise_for_status()
            payload = resp.json()
            listing = payload.get("data", {}) if isinstance(payload, dict) else {}

            for child in listing.get("children", []):
                if not isinstance(child, dict):
                    continue
                sub_data = child.get("data", {})
                if not isinstance(sub_data, dict):
                    continue
                record = _normalize_subreddit_record(sub_data)
                if record["name"]:
                    subscriptions[record["name"].lower()] = record

            next_after = listing.get("after")
            if not next_after:
                break
            cursor = str(next_after)
            if cursor in seen_cursors:
                # Cycle guard against repeated pagination cursors.
                break
            seen_cursors.add(cursor)
            after = cursor

        return subscriptions

    def fetch_custom_feeds(self) -> list[dict[str, Any]]:
        """Fetch all user custom feeds (multis) via /api/multi/mine."""
        resp = self._request("GET", "/api/multi/mine")
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, list):
            return []

        feeds: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            data = item.get("data", item)
            if not isinstance(data, dict):
                continue
            raw_subs = data.get("subreddits", [])
            sub_names: list[str] = []
            if isinstance(raw_subs, list):
                for entry in raw_subs:
                    if isinstance(entry, dict) and entry.get("name"):
                        sub_names.append(str(entry["name"]))
                    elif isinstance(entry, str) and entry:
                        sub_names.append(entry)

            feeds.append(
                {
                    "feed_name": str(data.get("name") or ""),
                    "display_name": str(data.get("display_name") or data.get("name") or ""),
                    "path": str(data.get("path") or ""),
                    "description_md": str(data.get("description_md") or ""),
                    "icon_name": str(data.get("icon_name") or ""),
                    "visibility": str(data.get("visibility") or "private"),
                    "subreddits": sub_names,
                }
            )
        return feeds

    def _mutate_subscription(self, action: str, sr_name: str) -> tuple[bool, int | None, str]:
        try:
            resp = self._request(
                "POST",
                "/api/subscribe",
                data={"action": action, "sr_name": sr_name, "api_type": "json"},
            )
        except requests.RequestException as exc:
            return False, None, str(exc)

        if resp.status_code not in (200, 201, 204):
            return False, resp.status_code, resp.text[:300]

        try:
            body = resp.json() if resp.text else {}
        except ValueError:
            body = {}
        errors = body.get("json", {}).get("errors", []) if isinstance(body, dict) else []
        if errors:
            return False, resp.status_code, str(errors)
        return True, resp.status_code, ""

    def sync_subscriptions(
        self,
        desired: list[str],
        dry_run: bool = False,
        prune: bool = False,
        keep: set[str] | None = None,
    ) -> dict[str, Any]:
        """Delta-sync subscriptions against live account state, with opt-in pruning."""
        live = self.fetch_all_subscriptions()

        desired_map: dict[str, str] = {}
        for sub in desired:
            clean = str(sub).strip()
            if clean and clean.lower() not in desired_map:
                desired_map[clean.lower()] = clean

        already_subscribed = [live[k]["name"] for k in desired_map if k in live]
        to_subscribe = [v for k, v in desired_map.items() if k not in live]

        keep_lower = {str(x).strip().lower() for x in (keep or set()) if str(x).strip()}
        to_unsubscribe: list[str] = []
        kept_unmanaged: list[str] = []
        for low, record in live.items():
            if low not in desired_map:
                if prune and low not in keep_lower:
                    to_unsubscribe.append(record["name"])
                else:
                    kept_unmanaged.append(record["name"])

        if dry_run:
            return {
                "dry_run": True,
                "already_subscribed": already_subscribed,
                "subscribed": to_subscribe,
                "unsubscribed": to_unsubscribe,
                "kept_unmanaged": kept_unmanaged,
                "failed": [],
            }

        subscribed: list[str] = []
        unsubscribed: list[str] = []
        failed: list[dict[str, Any]] = []

        for sub in to_subscribe:
            ok, status_code, err = self._mutate_subscription("sub", sub)
            if ok:
                subscribed.append(sub)
            else:
                failed.append({"name": sub, "status_code": status_code, "error": err})

        for sub in to_unsubscribe:
            ok, status_code, err = self._mutate_subscription("unsub", sub)
            if ok:
                unsubscribed.append(sub)
            else:
                failed.append({"name": sub, "status_code": status_code, "error": err})

        return {
            "dry_run": False,
            "already_subscribed": already_subscribed,
            "subscribed": subscribed,
            "unsubscribed": unsubscribed,
            "kept_unmanaged": kept_unmanaged,
            "failed": failed,
        }

    def sync_custom_feeds(
        self,
        feeds: list[dict[str, Any]],
        dry_run: bool = False,
    ) -> dict[str, dict[str, Any]]:
        """Create or update custom feeds with validation and >100-subreddit splitting."""
        results: dict[str, dict[str, Any]] = {}
        user_lower = self.username.lower()

        for feed in feeds:
            raw_name = str(feed.get("feed_name") or "")
            display_name = str(feed.get("display_name") or raw_name)
            description_md = str(feed.get("description_md") or "")
            icon_name = str(feed.get("icon_name") or "")
            visibility = str(feed.get("visibility") or "private")
            subreddits = [str(s) for s in feed.get("subreddits", [])]

            for chunk_name, chunk_display, chunk_subs in _partition_feed_subreddits(
                raw_name, display_name, subreddits
            ):
                multipath = f"/user/{user_lower}/m/{chunk_name}"
                endpoint = f"/api/multi{multipath}"

                if dry_run:
                    results[chunk_name] = {
                        "status": "dry_run",
                        "http_code": None,
                        "endpoint": endpoint,
                        "path": multipath,
                        "subreddits": chunk_subs,
                        "error": None,
                    }
                    continue

                model_payload = {
                    "display_name": chunk_display,
                    "description_md": description_md,
                    "icon_name": icon_name,
                    "key_color": "#CEE3F8",
                    "visibility": visibility,
                    "weighting_scheme": "classic",
                    "subreddits": [{"name": s} for s in chunk_subs],
                }
                form_data = {"model": json.dumps(model_payload)}

                try:
                    resp = self._request("PUT", endpoint, data=form_data)
                    if resp.status_code not in (200, 201):
                        resp = self._request("POST", endpoint, data=form_data)
                except requests.RequestException as exc:
                    results[chunk_name] = {
                        "status": "failed",
                        "http_code": None,
                        "endpoint": endpoint,
                        "path": multipath,
                        "subreddits": chunk_subs,
                        "error": str(exc),
                    }
                    continue

                if resp.status_code in (200, 201):
                    resp_path = multipath
                    try:
                        body = resp.json()
                        if isinstance(body, dict):
                            resp_path = str(body.get("data", {}).get("path") or multipath)
                    except ValueError:
                        pass
                    results[chunk_name] = {
                        "status": "success",
                        "http_code": resp.status_code,
                        "endpoint": endpoint,
                        "path": resp_path,
                        "subreddits": chunk_subs,
                        "error": None,
                    }
                else:
                    results[chunk_name] = {
                        "status": "failed",
                        "http_code": resp.status_code,
                        "endpoint": endpoint,
                        "path": multipath,
                        "subreddits": chunk_subs,
                        "error": resp.text[:300],
                    }

        return results

    def delete_custom_feed(self, path_or_name: str, dry_run: bool = False) -> bool:
        """Delete a custom feed by path (/user/.../m/...) or validated feed_name."""
        if path_or_name.startswith("/user/"):
            endpoint = f"/api/multi{path_or_name}"
        else:
            validate_feed_name(path_or_name)
            endpoint = f"/api/multi/user/{self.username.lower()}/m/{path_or_name}"
        if dry_run:
            return True
        try:
            resp = self._request("DELETE", endpoint)
        except requests.RequestException:
            return False
        return resp.status_code in (200, 204)

    def search_subreddits(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Search Reddit communities via /subreddits/search?raw_json=1."""
        if not query or not query.strip():
            return []
        bounded_limit = max(1, min(int(limit), 100))
        resp = self._request(
            "GET",
            "/subreddits/search",
            params={"q": query.strip(), "limit": bounded_limit},
        )
        resp.raise_for_status()
        payload = resp.json()
        listing = payload.get("data", {}) if isinstance(payload, dict) else {}

        results: list[dict[str, Any]] = []
        for child in listing.get("children", []):
            if not isinstance(child, dict):
                continue
            sub_data = child.get("data", {})
            if isinstance(sub_data, dict):
                rec = _normalize_subreddit_record(sub_data)
                if rec["name"]:
                    results.append(rec)
        return results
