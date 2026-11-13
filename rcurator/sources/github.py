"""Rate-limit-aware GitHub profile, repository, and README signal miner."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from rcurator.nlp.tokenizer import (
    TechnicalTokenizer,
    canonicalize_token,
    load_ontology_phrases,
)
from rcurator.sources.types import SignalEvidence
from rcurator.sources.workspace import scrub_url_userinfo

GITHUB_API_BASE = "https://api.github.com"
MAX_GET_BUDGET = 6
MAX_README_REPOS = 3
README_MIN_RATE_REMAINING = 10
MAX_README_CHARS = 65536
_USERNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")


def compute_repo_weight(
    repo: dict[str, Any],
    *,
    now: datetime | None = None,
    starred: bool = False,
) -> float:
    """Compute recency-, star-, and fork-adjusted repository weight omega(r)."""
    ref_now = now or datetime.now(timezone.utc)
    is_fork = bool(repo.get("fork", False))
    fork_factor = 0.25 if is_fork else 1.0

    days_old = 0.0
    pushed_at = repo.get("pushed_at") or repo.get("updated_at")
    if isinstance(pushed_at, str) and pushed_at:
        try:
            dt = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            days_old = max(0.0, (ref_now - dt).total_seconds() / 86400.0)
        except ValueError:
            days_old = 0.0

    recency_decay = math.exp(-(math.log(2.0) / 365.0) * days_old)
    stars = max(0, int(repo.get("stargazers_count", 0) or 0))
    star_factor = 1.0 + 0.35 * math.log1p(stars)
    weight = fork_factor * recency_decay * star_factor
    if starred:
        weight *= 0.35
    return weight


class GitHubMiner:
    """ETag-cached GitHub profile miner respecting the 6-request unauthenticated budget."""

    def __init__(
        self,
        token: str | None = None,
        cache_dir: Path | str | None = None,
        known_phrases: Iterable[str] | None = None,
        session: requests.Session | None = None,
        timeout: float = 10.0,
    ) -> None:
        raw_token = token if token is not None else os.environ.get("GITHUB_TOKEN")
        self.token: str | None = raw_token.strip() if isinstance(raw_token, str) and raw_token.strip() else None
        self.cache_dir = Path(cache_dir) if cache_dir is not None else (Path.cwd() / ".curator_cache")
        phrases = list(known_phrases) if known_phrases is not None else load_ontology_phrases()
        self._tokenizer = TechnicalTokenizer(phrases)
        self._session = session or requests.Session()
        self._timeout = float(timeout)
        self.last_request_count = 0
        self.last_warnings: list[str] = []

    def _ensure_cache_dir(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.cache_dir, 0o700)
        except OSError:
            pass

    def _cache_path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.cache_dir / f"gh_{digest}.json"

    def _read_cache(self, url: str) -> dict[str, Any] | None:
        cpath = self._cache_path(url)
        if not cpath.is_file():
            return None
        try:
            data = json.loads(cpath.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, json.JSONDecodeError):
            return None

    def _write_cache(self, url: str, etag: str | None, payload: Any) -> None:
        self._ensure_cache_dir()
        cpath = self._cache_path(url)
        record = {
            "url": url,
            "etag": etag,
            "cached_at": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        }
        tmp = cpath.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(record, fh)
        except Exception:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, cpath)
        try:
            os.chmod(cpath, 0o600)
        except OSError:
            pass

    def _cached_get(
        self,
        url: str,
        state: dict[str, Any],
        *,
        required: bool = False,
    ) -> Any:
        if state["requests_made"] >= MAX_GET_BUDGET:
            cached = self._read_cache(url)
            return cached.get("payload") if cached else None

        cached = self._read_cache(url)
        headers: dict[str, str] = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "reddit-curator/2.0",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if cached and cached.get("etag"):
            headers["If-None-Match"] = str(cached["etag"])

        state["requests_made"] += 1
        try:
            resp = self._session.get(url, headers=headers, timeout=self._timeout)
        except requests.RequestException as exc:
            if cached is not None:
                self.last_warnings.append(f"GitHub request failed for {url} ({exc}); using stale cache.")
                return cached.get("payload")
            msg = f"GitHub request failed for {url}: {exc}"
            self.last_warnings.append(msg)
            if required:
                raise RuntimeError(msg) from exc
            return None

        rem_hdr = resp.headers.get("X-RateLimit-Remaining")
        if rem_hdr is not None:
            try:
                state["rate_remaining"] = int(rem_hdr)
            except ValueError:
                pass

        if resp.status_code == 304 and cached is not None:
            return cached.get("payload")

        if resp.status_code == 200:
            payload = resp.json()
            etag = resp.headers.get("ETag")
            self._write_cache(url, etag, payload)
            return payload

        if resp.status_code in (403, 429):
            if cached is not None:
                self.last_warnings.append(
                    f"GitHub HTTP {resp.status_code} for {url}; using stale cache."
                )
                return cached.get("payload")
            msg = f"GitHub rate limited / forbidden ({resp.status_code}) with no cache for {url}"
            self.last_warnings.append(msg)
            if required:
                raise RuntimeError(msg)
            return None

        if resp.status_code == 404:
            if cached is None:
                msg = f"GitHub resource not found (404): {url}"
                self.last_warnings.append(msg)
                if required:
                    raise RuntimeError(msg)
                return None
            self.last_warnings.append(f"GitHub HTTP 404 for {url}; using stale cache.")
            return cached.get("payload")

        if cached is not None:
            self.last_warnings.append(f"GitHub HTTP {resp.status_code} for {url}; using stale cache.")
            return cached.get("payload")
        msg = f"GitHub HTTP {resp.status_code} for {url}"
        self.last_warnings.append(msg)
        if required:
            raise RuntimeError(msg)
        return None

    def mine(self, username: str) -> list[SignalEvidence]:
        """Mine public profile, repos, starred repos, and top READMEs for username."""
        uname = username.strip().lstrip("@")
        if not _USERNAME_RE.match(uname):
            raise ValueError(f"Invalid GitHub username: {username!r}")

        self.last_warnings = []
        state: dict[str, Any] = {
            "requests_made": 0,
            "rate_remaining": 5000 if self.token else 60,
        }
        evidences: list[SignalEvidence] = []
        now = datetime.now(timezone.utc)

        try:
            profile_url = f"{GITHUB_API_BASE}/users/{uname}"
            repos_url = f"{GITHUB_API_BASE}/users/{uname}/repos?per_page=100&sort=pushed"
            starred_url = f"{GITHUB_API_BASE}/users/{uname}/starred?per_page=100"

            profile = self._cached_get(profile_url, state, required=True)
            if isinstance(profile, dict):
                self._extract_profile(uname, profile, evidences)

            repos_data = self._cached_get(repos_url, state, required=False)
            repos = repos_data if isinstance(repos_data, list) else []

            starred_data = self._cached_get(starred_url, state, required=False)
            starred = starred_data if isinstance(starred_data, list) else []

            weighted_owned: list[tuple[float, dict[str, Any]]] = []
            for repo in repos:
                if not isinstance(repo, dict):
                    continue
                omega = compute_repo_weight(repo, now=now, starred=False)
                self._extract_repo(uname, repo, omega, evidences, starred=False)
                if not repo.get("fork", False) and repo.get("name"):
                    weighted_owned.append((omega, repo))

            for srepo in starred:
                if not isinstance(srepo, dict):
                    continue
                omega = compute_repo_weight(srepo, now=now, starred=True)
                self._extract_repo(uname, srepo, omega, evidences, starred=True)

            # Top K <= 3 non-fork READMEs ONLY when X-RateLimit-Remaining > 10 (or from disk cache if 0 quota)
            weighted_owned.sort(key=lambda pair: pair[0], reverse=True)
            for omega, repo in weighted_owned[:MAX_README_REPOS]:
                rname = str(repo["name"])
                readme_url = f"{GITHUB_API_BASE}/repos/{uname}/{rname}/readme"
                if state["rate_remaining"] <= README_MIN_RATE_REMAINING:
                    stale_readme = self._read_cache(readme_url)
                    if stale_readme is not None and isinstance(stale_readme.get("payload"), dict):
                        self._extract_readme(uname, rname, stale_readme["payload"], omega, evidences)
                        continue
                    self.last_warnings.append(
                        f"GitHub rate limit low ({state['rate_remaining']} remaining); skipped README fetch."
                    )
                    break
                if state["requests_made"] >= MAX_GET_BUDGET:
                    break
                readme_obj = self._cached_get(readme_url, state, required=False)
                if isinstance(readme_obj, dict):
                    self._extract_readme(uname, rname, readme_obj, omega, evidences)
        finally:
            self.last_request_count = state["requests_made"]

        return evidences

    def _emit_tokenized(
        self,
        text: str,
        base_weight: float,
        detail: str,
        evidences: list[SignalEvidence],
        kind: str = "keyword",
    ) -> None:
        cleaned = scrub_url_userinfo(text)
        term_max: dict[str, float] = {}
        for term, tok_w in self._tokenizer.tokenize(cleaned):
            w = base_weight * tok_w
            if w > term_max.get(term, 0.0):
                term_max[term] = w
        for term, w in term_max.items():
            evidences.append(
                SignalEvidence(
                    term=term,
                    raw_term=term,
                    weight=round(w, 4),
                    source_type="github",
                    source_detail=detail,
                    signal_kind=kind,
                )
            )

    def _extract_profile(
        self, uname: str, profile: dict[str, Any], evidences: list[SignalEvidence]
    ) -> None:
        detail = f"github:{uname}/profile"
        parts = [str(profile.get(k) or "") for k in ("bio", "company")]
        combined = " ".join(p for p in parts if p).strip()
        if combined:
            self._emit_tokenized(combined, 2.0, detail, evidences, kind="keyword")

    def _extract_repo(
        self,
        uname: str,
        repo: dict[str, Any],
        omega: float,
        evidences: list[SignalEvidence],
        *,
        starred: bool,
    ) -> None:
        full_name = str(repo.get("full_name") or f"{uname}/{repo.get('name', 'unknown')}")
        prefix = "starred" if starred else "repo"
        detail = f"github:{prefix}:{full_name}"

        lang = repo.get("language")
        if isinstance(lang, str) and lang.strip():
            canon_lang = canonicalize_token(lang)
            if canon_lang:
                evidences.append(
                    SignalEvidence(
                        term=canon_lang,
                        raw_term=lang,
                        weight=round(2.5 * omega, 4),
                        source_type="github",
                        source_detail=detail,
                        signal_kind="language",
                    )
                )

        topics = repo.get("topics") or []
        if isinstance(topics, list):
            for raw_topic in topics:
                canon_topic = canonicalize_token(str(raw_topic))
                if canon_topic:
                    evidences.append(
                        SignalEvidence(
                            term=canon_topic,
                            raw_term=str(raw_topic),
                            weight=round(3.0 * omega, 4),
                            source_type="github",
                            source_detail=detail,
                            signal_kind="ecosystem",
                        )
                    )

        desc_parts = [str(repo.get("name") or ""), str(repo.get("description") or "")]
        desc = " ".join(p for p in desc_parts if p).strip()
        if desc:
            self._emit_tokenized(desc, 1.8 * omega, detail, evidences, kind="keyword")

    def _extract_readme(
        self,
        uname: str,
        rname: str,
        readme_obj: dict[str, Any],
        omega: float,
        evidences: list[SignalEvidence],
    ) -> None:
        raw_content = readme_obj.get("content")
        if not isinstance(raw_content, str) or not raw_content:
            return
        encoding = str(readme_obj.get("encoding") or "").lower()
        if encoding == "base64":
            try:
                decoded = base64.b64decode(raw_content, validate=False).decode("utf-8", errors="replace")
            except (ValueError, UnicodeDecodeError):
                return
        else:
            decoded = raw_content
        decoded = decoded[:MAX_README_CHARS]
        detail = f"github:readme:{uname}/{rname}"
        self._emit_tokenized(decoded, 1.4 * omega, detail, evidences, kind="keyword")
