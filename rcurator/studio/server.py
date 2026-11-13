"""Local HTTP studio server and backend adapter for reddit-curator.

Provides a zero-external-dependency ``ThreadingHTTPServer`` bound strictly to
loopback addresses with DNS-rebinding guards, per-boot CSRF protection, path
traversal validation, in-memory upload caps, and workspace blocklists.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import socket
import urllib.parse
import urllib.request
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8420
DEFAULT_CDP_PORT = 9222
MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # 15 MiB hard cap
MAX_JSON_BODY_BYTES = 8 * 1024 * 1024

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
LOOPBACK_HOST_HEADERS = frozenset({"127.0.0.1", "localhost", "[::1]", "::1"})

SENSITIVE_SYSTEM_DIRS: tuple[str, ...] = (
    "/etc",
    "/proc",
    "/sys",
    "/dev",
    "/run",
    "/root",
    "/var",
    "/boot",
)

SENSITIVE_HOME_REL_DIRS: tuple[str, ...] = (
    ".ssh",
    ".gnupg",
    ".aws",
    ".kube",
    ".config/gcloud",
    ".mozilla",
    ".config/google-chrome",
    ".config/chromium",
    ".config/BraveSoftware",
    ".config/reddit-curator",
    ".local/share/keyrings",
    ".password-store",
)

MIME_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".txt": "text/plain; charset=utf-8",
}

_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_SECRET_KEYS = frozenset({"access_token", "cookies", "cookie", "token_v2", "oauth_token"})


class StudioHTTPError(Exception):
    """Structured HTTP error raised during request validation or dispatch."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = int(status)
        self.message = message


def validate_loopback_bind_host(host: str) -> str:
    """Validate that the bind address is strictly loopback."""
    normalized = (host or "").strip()
    if normalized not in LOOPBACK_HOSTS:
        raise ValueError(
            f"Refusing non-loopback bind address {host!r}; only 127.0.0.1, localhost, or ::1 are allowed."
        )
    return normalized


def validate_host_header(host_header: str | None) -> bool:
    """Verify the HTTP Host header names a loopback host with optional valid port."""
    if not host_header:
        return False
    raw = host_header.strip().lower()
    if not raw or any(ch.isspace() for ch in raw):
        return False

    if raw.startswith("["):
        end = raw.find("]")
        if end == -1:
            return False
        host_part = raw[: end + 1]
        rest = raw[end + 1 :]
        if host_part != "[::1]":
            return False
        if not rest:
            return True
        if not rest.startswith(":"):
            return False
        port_str = rest[1:]
        return port_str.isdigit() and 1 <= int(port_str) <= 65535

    if ":" in raw:
        parts = raw.split(":")
        if len(parts) != 2:
            return False
        host_part, port_str = parts
        if host_part not in ("127.0.0.1", "localhost"):
            return False
        return port_str.isdigit() and 1 <= int(port_str) <= 65535

    return raw in ("127.0.0.1", "localhost")


def validate_origin_header(origin_header: str | None) -> bool:
    """Verify Origin header (when provided) comes from a loopback HTTP(S) origin."""
    if origin_header is None:
        return True
    raw = origin_header.strip()
    if not raw or raw.lower() == "null":
        return False
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        return False
    hostname = (parsed.hostname or "").lower()
    return hostname in {"127.0.0.1", "localhost", "::1"}


def _is_relative_to_any(candidate: Path, bases: list[Path]) -> bool:
    for base in bases:
        try:
            if candidate == base or candidate.is_relative_to(base):
                return True
        except ValueError:
            continue
    return False


def validate_workspace_path(raw_path: str) -> Path:
    """Validate a user-supplied workspace directory against sensitive blocklists.

    Blocks system directories and sensitive credential/browser directories under
    ``$HOME``, and restricts scanning strictly to existing directories inside
    ``$HOME`` or ``/tmp``.
    """
    if not isinstance(raw_path, str) or not raw_path.strip() or "\x00" in raw_path:
        raise StudioHTTPError(HTTPStatus.BAD_REQUEST, "Workspace path must be a non-empty string.")

    expanded = os.path.expanduser(raw_path.strip())
    lexical = Path(os.path.abspath(expanded))
    try:
        resolved = Path(expanded).resolve()
    except OSError as exc:
        raise StudioHTTPError(HTTPStatus.BAD_REQUEST, f"Invalid workspace path: {exc}") from exc

    system_bases = [Path(d) for d in SENSITIVE_SYSTEM_DIRS]
    system_bases.extend(Path(d).resolve() for d in SENSITIVE_SYSTEM_DIRS if Path(d).exists())

    home_raw = Path(os.path.expanduser("~"))
    home_resolved = Path.home().resolve()
    sensitive_home_bases: list[Path] = []
    for rel in SENSITIVE_HOME_REL_DIRS:
        sensitive_home_bases.append(home_raw / rel)
        sensitive_home_bases.append(home_resolved / rel)
        candidate_res = (home_resolved / rel).resolve()
        sensitive_home_bases.append(candidate_res)

    blocked_bases = system_bases + sensitive_home_bases
    if _is_relative_to_any(lexical, blocked_bases) or _is_relative_to_any(resolved, blocked_bases):
        raise StudioHTTPError(
            HTTPStatus.FORBIDDEN,
            f"Access denied: {raw_path!r} is within a protected system or credential directory.",
        )

    tmp_raw = Path("/tmp")
    tmp_resolved = tmp_raw.resolve()
    allowed_roots = [home_raw, home_resolved, tmp_raw, tmp_resolved]
    if not (_is_relative_to_any(resolved, allowed_roots) and _is_relative_to_any(lexical, allowed_roots)):
        raise StudioHTTPError(
            HTTPStatus.FORBIDDEN,
            "Workspace path must reside under $HOME or /tmp.",
        )

    if not resolved.exists() or not resolved.is_dir():
        raise StudioHTTPError(
            HTTPStatus.BAD_REQUEST,
            f"Workspace directory does not exist: {raw_path!r}",
        )

    return resolved


def validate_safe_identifier(raw_id: str, label: str = "identifier") -> str:
    """Reject traversal sequences, slashes, or invalid identifier characters."""
    if not isinstance(raw_id, str):
        raise StudioHTTPError(HTTPStatus.BAD_REQUEST, f"Invalid {label}.")
    decoded = urllib.parse.unquote(raw_id)
    if (
        not decoded
        or "\x00" in decoded
        or "/" in decoded
        or "\\" in decoded
        or ".." in decoded
        or decoded.startswith(".")
    ):
        raise StudioHTTPError(HTTPStatus.FORBIDDEN, f"Invalid or unsafe {label}: {raw_id!r}")
    if not _SAFE_NAME_RE.match(decoded):
        raise StudioHTTPError(HTTPStatus.BAD_REQUEST, f"Malformed {label}: {raw_id!r}")
    return decoded


def resolve_safe_child(base_dir: Path, rel_path: str, must_exist: bool = False) -> Path:
    """Resolve a relative path inside ``base_dir`` and enforce ``is_relative_to``."""
    if not isinstance(rel_path, str) or "\x00" in rel_path:
        raise StudioHTTPError(HTTPStatus.FORBIDDEN, "Invalid asset path.")
    decoded = urllib.parse.unquote(rel_path).lstrip("/")
    if not decoded or "\\" in decoded:
        raise StudioHTTPError(HTTPStatus.FORBIDDEN, "Invalid asset path.")
    parts = decoded.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise StudioHTTPError(HTTPStatus.FORBIDDEN, "Path traversal is forbidden.")

    base_resolved = base_dir.resolve()
    target = (base_resolved / decoded).resolve()
    if not target.is_relative_to(base_resolved) or target == base_resolved:
        raise StudioHTTPError(HTTPStatus.FORBIDDEN, "Path escapes allowed directory.")
    if must_exist and not target.is_file():
        raise StudioHTTPError(HTTPStatus.NOT_FOUND, f"Not found: {decoded}")
    return target


def sanitize_filename(raw_name: str | None, default: str = "resume.pdf") -> str:
    """Return a clean basename with control characters stripped."""
    if not raw_name:
        return default
    name = os.path.basename(urllib.parse.unquote(raw_name)).strip()
    name = re.sub(r"[\x00-\x1f\x7f/\\]+", "_", name)
    if not name or name in (".", ".."):
        return default
    return name[:160]


def parse_multipart_upload(content_type: str, body: bytes) -> tuple[str, bytes]:
    """Parse a ``multipart/form-data`` payload purely in-memory without ``cgi``."""
    match = re.search(r'boundary=(?:"([^"]+)"|([^;\s]+))', content_type, flags=re.IGNORECASE)
    if not match:
        raise StudioHTTPError(HTTPStatus.BAD_REQUEST, "Missing multipart boundary.")
    boundary_str = match.group(1) or match.group(2)
    delimiter = b"--" + boundary_str.encode("latin-1", errors="ignore")

    chunks = body.split(delimiter)
    for chunk in chunks[1:]:
        if chunk.startswith(b"--"):
            break
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        elif chunk.startswith(b"\n"):
            chunk = chunk[1:]

        sep = b"\r\n\r\n"
        sep_len = 4
        idx = chunk.find(sep)
        if idx == -1:
            sep = b"\n\n"
            sep_len = 2
            idx = chunk.find(sep)
        if idx == -1:
            continue

        header_bytes = chunk[:idx]
        part_data = chunk[idx + sep_len :]
        if part_data.endswith(b"\r\n"):
            part_data = part_data[:-2]
        elif part_data.endswith(b"\n"):
            part_data = part_data[:-1]

        header_text = header_bytes.decode("utf-8", errors="replace")
        disp_match = re.search(r'filename="([^"]*)"', header_text, flags=re.IGNORECASE)
        if disp_match is not None:
            filename = sanitize_filename(disp_match.group(1), default="resume.pdf")
            return filename, part_data

    raise StudioHTTPError(HTTPStatus.BAD_REQUEST, "No file part found in multipart upload.")


def _redact_secrets(obj: Any) -> Any:
    """Recursively strip any accidental secret fields from outgoing payloads."""
    if isinstance(obj, dict):
        cleaned: dict[str, Any] = {}
        for k, v in obj.items():
            if str(k).lower() in _SECRET_KEYS:
                continue
            cleaned[k] = _redact_secrets(v)
        return cleaned
    if isinstance(obj, list):
        return [_redact_secrets(item) for item in obj]
    return obj


def _normalize_ratio(raw_ratio: Any) -> tuple[int, int, int]:
    """Normalize a 3-element ratio sequence to positive integers summing to 100."""
    if not isinstance(raw_ratio, (list, tuple)) or len(raw_ratio) != 3:
        return (40, 35, 25)
    try:
        vals = [max(0.0, float(x)) for x in raw_ratio]
    except (TypeError, ValueError):
        return (40, 35, 25)
    total = sum(vals)
    if total <= 0:
        return (40, 35, 25)
    a = round(vals[0] * 100.0 / total)
    b = round(vals[1] * 100.0 / total)
    c = 100 - a - b
    if c < 0:
        b = max(0, b + c)
        c = 100 - a - b
    return (int(a), int(b), int(c))


def _validate_cdp_port(raw_port: Any, default: int = DEFAULT_CDP_PORT) -> int:
    """Validate an optional CDP port number in the non-privileged user range."""
    if raw_port is None or raw_port == "":
        return int(default)
    if isinstance(raw_port, bool):
        raise StudioHTTPError(HTTPStatus.BAD_REQUEST, "CDP port must be an integer between 1024 and 65535.")
    if isinstance(raw_port, float) and not raw_port.is_integer():
        raise StudioHTTPError(HTTPStatus.BAD_REQUEST, "CDP port must be an integer between 1024 and 65535.")
    try:
        port_val = int(raw_port)
    except (TypeError, ValueError) as exc:
        raise StudioHTTPError(
            HTTPStatus.BAD_REQUEST,
            "CDP port must be an integer between 1024 and 65535.",
        ) from exc
    if not (1024 <= port_val <= 65535):
        raise StudioHTTPError(
            HTTPStatus.BAD_REQUEST,
            "CDP port must be between 1024 and 65535.",
        )
    return port_val


def _is_reddit_url(url: str) -> bool:
    """Strictly verify a browser tab URL belongs to reddit.com."""
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    hostname = (parsed.hostname or "").lower()
    return hostname == "reddit.com" or hostname.endswith(".reddit.com")


def _extract_manifest(payload: dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload.get("manifest"), dict):
        return payload["manifest"]
    if any(
        k in payload
        for k in ("anchor_subreddits", "domain_priming_subreddits", "tier1_systems_subreddits", "custom_feeds")
    ):
        return payload
    raise StudioHTTPError(HTTPStatus.BAD_REQUEST, "Request must include a valid 'manifest' object.")


class DefaultStudioBackend:
    """Default backend reading local presets/ontology and lazily importing rcurator modules."""

    def __init__(
        self,
        project_root: Path | None = None,
        ontology_path: Path | None = None,
        presets_dir: Path | None = None,
        snapshots_dir: Path | None = None,
        cdp_port: int = DEFAULT_CDP_PORT,
    ) -> None:
        self.project_root = (project_root or Path(__file__).resolve().parents[2]).resolve()
        self.ontology_path = (ontology_path or (self.project_root / "curator_data" / "subreddit_ontology.json")).resolve()
        self.presets_dir = (presets_dir or (self.project_root / "presets")).resolve()
        self.snapshots_dir = (snapshots_dir or (self.project_root / ".curator_snapshots")).resolve()
        self.cdp_port = int(cdp_port)
        self._ontology_cache: dict[str, Any] | None = None

    def _load_ontology_raw(self) -> dict[str, Any]:
        if self._ontology_cache is not None:
            return self._ontology_cache
        if not self.ontology_path.is_file():
            return {"subreddits": {}, "domains": []}
        with self.ontology_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        self._ontology_cache = data
        return data

    def _probe_cdp_passive(self, target_port: int) -> tuple[bool, bool, str | None]:
        """Passively probe CDP /json/list and existing Reddit tabs without mutating browser state."""
        cdp_connected = False
        authenticated = False
        username: str | None = None

        targets: list[dict[str, Any]] = []
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{target_port}/json/list")
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                if resp.status == 200:
                    parsed = json.loads(resp.read().decode("utf-8", errors="replace"))
                    if isinstance(parsed, list):
                        cdp_connected = True
                        targets = [t for t in parsed if isinstance(t, dict)]
        except Exception:
            return False, False, None

        reddit_ws_url: str | None = None
        for tab in targets:
            if tab.get("type") == "page" and _is_reddit_url(str(tab.get("url") or "")):
                ws_url = tab.get("webSocketDebuggerUrl")
                if isinstance(ws_url, str) and ws_url.startswith("ws://"):
                    reddit_ws_url = ws_url
                    break

        if not reddit_ws_url:
            return cdp_connected, False, None

        token: str | None = None
        try:
            from rcurator.cdp_client import CDPClient

            async def _read_token() -> str | None:
                async with CDPClient(reddit_ws_url) as client:
                    res = await client.send(
                        "Runtime.evaluate",
                        {
                            "expression": "(() => { try { return localStorage.getItem('chat:access-token'); } catch (e) { return null; } })()",
                            "returnByValue": True,
                        },
                        timeout=1.5,
                    )
                    raw_val = ((res.get("result") or {}).get("value")) if isinstance(res, dict) else None
                    if isinstance(raw_val, str) and raw_val:
                        try:
                            tok_obj = json.loads(raw_val)
                            if isinstance(tok_obj, dict) and tok_obj.get("token"):
                                return str(tok_obj["token"])
                        except json.JSONDecodeError:
                            pass
                    cookies_res = await client.send(
                        "Network.getCookies",
                        {"urls": ["https://www.reddit.com", "https://oauth.reddit.com"]},
                        timeout=1.5,
                    )
                    for cookie in (cookies_res.get("cookies") or []):
                        if isinstance(cookie, dict) and cookie.get("name") == "token_v2" and cookie.get("value"):
                            return str(cookie["value"])
                    return None

            token = asyncio.run(asyncio.wait_for(_read_token(), timeout=2.0))
        except Exception:
            token = None

        if token:
            try:
                me_req = urllib.request.Request(
                    "https://oauth.reddit.com/api/v1/me?raw_json=1",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "User-Agent": "reddit-curator/2.0 (local studio passive probe)",
                    },
                )
                with urllib.request.urlopen(me_req, timeout=2.0) as me_resp:
                    if me_resp.status == 200:
                        me_data = json.loads(me_resp.read().decode("utf-8", errors="replace"))
                        if isinstance(me_data, dict) and me_data.get("name"):
                            authenticated = True
                            username = str(me_data["name"])
            except Exception:
                authenticated = False
                username = None

        return cdp_connected, authenticated, username

    def get_status(self, cdp_port: int | None = None) -> dict[str, Any]:
        target_port = _validate_cdp_port(cdp_port, self.cdp_port)
        ontology = self._load_ontology_raw()
        subs = ontology.get("subreddits", {})
        ontology_count = len(subs) if isinstance(subs, (dict, list)) else 0
        presets_count = len(list(self.presets_dir.glob("*.json"))) if self.presets_dir.is_dir() else 0

        browsers: list[dict[str, Any]] = []
        try:
            from rcurator.browser import BrowserLauncher

            browsers = BrowserLauncher.detect_browsers()
        except Exception:
            browsers = []

        cdp_connected, authenticated, username = self._probe_cdp_passive(target_port)

        return {
            "ok": True,
            "version": "2.0.0",
            "cdp_port": target_port,
            "cdp_connected": cdp_connected,
            "authenticated": authenticated,
            "username": username,
            "ontology_subreddit_count": ontology_count,
            "presets_count": presets_count,
            "browsers": browsers,
        }

    def list_presets(self) -> list[dict[str, Any]]:
        if not self.presets_dir.is_dir():
            return []
        items: list[dict[str, Any]] = []
        for path in sorted(self.presets_dir.glob("*.json")):
            try:
                with path.open("r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue
            anchors = data.get("anchor_subreddits", [])
            priming = data.get("domain_priming_subreddits", [])
            feeds = data.get("custom_feeds", [])
            preview = [
                entry.get("name", "")
                for entry in (anchors[:3] + priming[:4])
                if isinstance(entry, dict) and entry.get("name")
            ]
            items.append(
                {
                    "id": path.stem,
                    "name": data.get("name", path.stem),
                    "description": data.get("description", ""),
                    "anchor_count": len(anchors),
                    "domain_priming_count": len(priming),
                    "custom_feeds_count": len(feeds),
                    "golden_ratio_distribution": data.get("golden_ratio_distribution", {}),
                    "preview_subreddits": preview,
                }
            )
        return items

    def get_preset(self, name: str) -> dict[str, Any]:
        safe_name = validate_safe_identifier(name, label="preset name")
        if safe_name.endswith(".json"):
            safe_name = safe_name[:-5]
        target = resolve_safe_child(self.presets_dir, f"{safe_name}.json", must_exist=True)
        with target.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            data.setdefault("preset_id", safe_name)
        return data

    def parse_resume_bytes(self, data: bytes, filename: str = "resume.pdf") -> dict[str, Any]:
        try:
            from rcurator.sources.resume import ResumeParser
        except ImportError as exc:
            raise StudioHTTPError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                f"Resume parser module unavailable: {exc}",
            ) from exc

        try:
            evidences = ResumeParser().parse_bytes(data, filename=filename)
        except ValueError as exc:
            raise StudioHTTPError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
        except Exception as exc:
            raise StudioHTTPError(HTTPStatus.BAD_REQUEST, f"Failed to parse resume: {exc}") from exc

        serialized = [
            ev.to_dict() if hasattr(ev, "to_dict") else dict(ev) for ev in evidences
        ]
        term_totals: dict[str, float] = {}
        for item in serialized:
            term = str(item.get("term", ""))
            weight = float(item.get("weight", 0.0))
            if term:
                term_totals[term] = term_totals.get(term, 0.0) + weight
        top_terms = [
            {"term": k, "weight": round(v, 4)}
            for k, v in sorted(term_totals.items(), key=lambda kv: (-kv[1], kv[0]))[:25]
        ]
        return {
            "filename": filename,
            "size_bytes": len(data),
            "evidences_count": len(serialized),
            "evidences": serialized,
            "top_terms": top_terms,
        }

    def generate(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            from rcurator.engine.ontology import OntologyRepository
            from rcurator.engine.pipeline import AutoPresetGenerator
            from rcurator.nlp.tokenizer import TechnicalTokenizer
            from rcurator.sources.types import SignalEvidence
        except ImportError as exc:
            raise StudioHTTPError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                f"Curation engine or NLP modules unavailable: {exc}",
            ) from exc

        evidences: list[Any] = []
        sources_used: list[str] = []

        workspace_raw = str(payload.get("workspace_path") or "").strip()
        if workspace_raw:
            validated_ws = validate_workspace_path(workspace_raw)
            from rcurator.sources.workspace import WorkspaceScanner

            ws_evidences = WorkspaceScanner().scan(validated_ws)
            evidences.extend(ws_evidences)
            sources_used.append("workspace")

        github_user = str(payload.get("github_username") or "").strip()
        if github_user:
            from rcurator.sources.github import GitHubMiner

            gh_token = str(payload.get("github_token") or "").strip() or None
            gh_evidences = GitHubMiner(token=gh_token).mine(github_user)
            evidences.extend(gh_evidences)
            sources_used.append("github")

        resume_items = payload.get("resume_evidences") or []
        if isinstance(resume_items, list) and resume_items:
            for item in resume_items:
                if isinstance(item, dict) and item.get("term"):
                    evidences.append(
                        SignalEvidence(
                            term=str(item["term"]),
                            raw_term=str(item.get("raw_term", item["term"])),
                            weight=float(item.get("weight", 1.0)),
                            source_type="resume",
                            source_detail=str(item.get("source_detail", "uploaded_resume")),
                            signal_kind=str(item.get("signal_kind", "keyword")),
                        )
                    )
            sources_used.append("resume")

        ontology = OntologyRepository.load(self.ontology_path)
        query_text = str(payload.get("query") or "").strip()
        if query_text:
            known_phrases = getattr(ontology, "known_phrases", ()) or ()
            if not known_phrases:
                try:
                    from rcurator.nlp.tokenizer import load_ontology_phrases

                    known_phrases = load_ontology_phrases(self.ontology_path)
                except Exception:
                    known_phrases = ()
            tokenizer = TechnicalTokenizer(known_phrases=known_phrases)
            for tok, w in tokenizer.tokenize(query_text):
                evidences.append(
                    SignalEvidence(
                        term=tok,
                        raw_term=tok,
                        weight=float(w) * 2.2,
                        source_type="query",
                        source_detail="studio_query",
                        signal_kind="keyword",
                    )
                )
            sources_used.append("query")

        target_cdp_port = _validate_cdp_port(payload.get("cdp_port"), self.cdp_port)
        online_candidates: list[dict[str, Any]] | None = None
        if payload.get("discover_online") and query_text:
            try:
                from rcurator.cdp_client import acquire_reddit_session
                from rcurator.reddit_api import RedditAPIClient

                session = acquire_reddit_session(target_cdp_port)
                if session is not None:
                    api = RedditAPIClient(session.access_token, session.username)
                    online_candidates = api.search_subreddits(query_text, limit=15)
            except Exception:
                online_candidates = None

        target_ratio = _normalize_ratio(payload.get("target_ratio"))
        try:
            tier1_count = max(3, min(30, int(payload.get("tier1_count", 18))))
        except (TypeError, ValueError):
            tier1_count = 18
        name = str(payload.get("name") or "Auto-Generated Curation").strip() or "Auto-Generated Curation"

        manifest = AutoPresetGenerator(ontology).generate(
            evidences,
            name=name,
            target_ratio=target_ratio,
            tier1_count=tier1_count,
            online_candidates=online_candidates,
        )
        return {
            "manifest": manifest,
            "evidences_count": len(evidences),
            "sources_used": sources_used,
        }

    def mix_presets(
        self,
        weights: dict[str, float],
        target_ratio: tuple[int, int, int] = (40, 35, 25),
    ) -> dict[str, Any]:
        validated_weights: dict[str, float] = {}
        for preset_name, raw_w in weights.items():
            safe_name = validate_safe_identifier(preset_name, label="preset name")
            if safe_name.endswith(".json"):
                safe_name = safe_name[:-5]
            resolve_safe_child(self.presets_dir, f"{safe_name}.json", must_exist=True)
            try:
                w = float(raw_w)
            except (TypeError, ValueError) as exc:
                raise StudioHTTPError(HTTPStatus.BAD_REQUEST, f"Invalid weight for {safe_name!r}.") from exc
            if w > 0:
                validated_weights[safe_name] = w

        if not validated_weights:
            raise StudioHTTPError(HTTPStatus.BAD_REQUEST, "At least one preset with positive weight is required.")

        try:
            from rcurator.engine.ontology import OntologyRepository
            from rcurator.mixer import PresetMixer
        except ImportError as exc:
            raise StudioHTTPError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                f"Preset mixer unavailable: {exc}",
            ) from exc

        ontology = OntologyRepository.load(self.ontology_path)
        manifest = PresetMixer(ontology, self.presets_dir).mix(
            validated_weights,
            target_ratio=_normalize_ratio(target_ratio),
        )
        return {"manifest": manifest}

    def audit_manifest(self, manifest: dict[str, Any]) -> dict[str, Any]:
        try:
            from rcurator.analytics import FeedHealthAuditor
            from rcurator.engine.ontology import OntologyRepository, normalize_manifest
        except ImportError as exc:
            raise StudioHTTPError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                f"Analytics engine unavailable: {exc}",
            ) from exc

        ontology = OntologyRepository.load(self.ontology_path)
        normalized = normalize_manifest(manifest, ontology=ontology)
        return FeedHealthAuditor(ontology).audit_manifest(normalized)

    def audit_live(
        self,
        target_ratio: tuple[int, int, int] = (40, 35, 25),
        cdp_port: int | None = None,
    ) -> dict[str, Any]:
        target_port = _validate_cdp_port(cdp_port, self.cdp_port)
        try:
            from rcurator.analytics import FeedHealthAuditor
            from rcurator.cdp_client import acquire_reddit_session
            from rcurator.engine.ontology import OntologyRepository
            from rcurator.reddit_api import RedditAPIClient
        except ImportError as exc:
            raise StudioHTTPError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                f"Live audit dependencies unavailable: {exc}",
            ) from exc

        session = acquire_reddit_session(target_port)
        api = RedditAPIClient(session.access_token, session.username)
        live_subs = api.fetch_all_subscriptions()
        sub_names = [entry.get("name", key) for key, entry in live_subs.items()]
        ontology = OntologyRepository.load(self.ontology_path)
        return FeedHealthAuditor(ontology).audit_live(sub_names, target_ratio=_normalize_ratio(target_ratio))

    def compute_diff(
        self,
        manifest: dict[str, Any],
        prune: bool = False,
        keep: list[str] | None = None,
        cdp_port: int | None = None,
    ) -> dict[str, Any]:
        target_port = _validate_cdp_port(cdp_port, self.cdp_port)
        try:
            from rcurator.cdp_client import acquire_reddit_session
            from rcurator.reddit_api import RedditAPIClient
            from rcurator.state import SnapshotManager
        except ImportError as exc:
            raise StudioHTTPError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                f"Runtime sync dependencies unavailable: {exc}",
            ) from exc

        session = acquire_reddit_session(target_port)
        api = RedditAPIClient(session.access_token, session.username)
        live_subs = api.fetch_all_subscriptions()
        live_feeds = api.fetch_custom_feeds()
        sub_names = [entry.get("name", key) for key, entry in live_subs.items()]
        return SnapshotManager(self.snapshots_dir).compute_diff(
            manifest,
            live_subs=sub_names,
            live_feeds=live_feeds,
            prune=bool(prune),
            keep=set(keep or []),
        )

    def sync_manifest(
        self,
        manifest: dict[str, Any],
        dry_run: bool = True,
        prune: bool = False,
        keep: list[str] | None = None,
        cdp_port: int | None = None,
    ) -> dict[str, Any]:
        target_port = _validate_cdp_port(cdp_port, self.cdp_port)
        try:
            from rcurator.cdp_client import acquire_reddit_session, activate_following_feed
            from rcurator.reddit_api import RedditAPIClient
            from rcurator.state import SnapshotManager
        except ImportError as exc:
            raise StudioHTTPError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                f"Runtime sync dependencies unavailable: {exc}",
            ) from exc

        session = acquire_reddit_session(target_port)
        api = RedditAPIClient(session.access_token, session.username)
        snapshot_id: str | None = None
        if not dry_run:
            live_subs = api.fetch_all_subscriptions()
            live_feeds = api.fetch_custom_feeds()
            snap = SnapshotManager(self.snapshots_dir).create_snapshot(
                session.username,
                live_subs,
                live_feeds,
                manifest,
            )
            snapshot_id = snap.get("snapshot_id")

        desired: list[str] = []
        seen: set[str] = set()
        priming = manifest.get("domain_priming_subreddits") or manifest.get("tier1_systems_subreddits") or []
        for entry in list(manifest.get("anchor_subreddits") or []) + list(priming):
            name = entry.get("name") if isinstance(entry, dict) else str(entry)
            if name and name.lower() not in seen:
                seen.add(name.lower())
                desired.append(name)

        subs_result = api.sync_subscriptions(
            desired,
            dry_run=bool(dry_run),
            prune=bool(prune),
            keep=set(keep or []),
        )
        feeds_result = api.sync_custom_feeds(
            list(manifest.get("custom_feeds") or []),
            dry_run=bool(dry_run),
        )
        following_result: dict[str, Any] | None = None
        if not dry_run and manifest.get("feed_mode", "following") == "following":
            following_result = activate_following_feed(session.ws_url)

        return {
            "dry_run": bool(dry_run),
            "username": session.username,
            "snapshot_id": snapshot_id,
            "subscriptions": subs_result,
            "custom_feeds": feeds_result,
            "following_feed": following_result,
        }

    def list_snapshots(self) -> list[dict[str, Any]]:
        try:
            from rcurator.state import SnapshotManager
        except ImportError:
            return []
        return SnapshotManager(self.snapshots_dir).list_snapshots()

    def rollback(
        self,
        snapshot_id: str,
        dry_run: bool = False,
        cdp_port: int | None = None,
    ) -> dict[str, Any]:
        target_port = _validate_cdp_port(cdp_port, self.cdp_port)
        if snapshot_id != "latest":
            validate_safe_identifier(snapshot_id, label="snapshot_id")
            if self.snapshots_dir.exists():
                resolve_safe_child(self.snapshots_dir, snapshot_id, must_exist=False)
        try:
            from rcurator.cdp_client import acquire_reddit_session
            from rcurator.reddit_api import RedditAPIClient
            from rcurator.state import SnapshotManager
        except ImportError as exc:
            raise StudioHTTPError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                f"Rollback dependencies unavailable: {exc}",
            ) from exc

        session = acquire_reddit_session(target_port)
        api = RedditAPIClient(session.access_token, session.username)
        return SnapshotManager(self.snapshots_dir).rollback(snapshot_id, api, dry_run=bool(dry_run))

    def launch_browser(self, port: int | None = None, preferred: str | None = None) -> dict[str, Any]:
        target_port = _validate_cdp_port(port, self.cdp_port)
        try:
            from rcurator.browser import BrowserLauncher
        except ImportError as exc:
            raise StudioHTTPError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                f"Browser launcher unavailable: {exc}",
            ) from exc

        try:
            return BrowserLauncher().launch(port=target_port, preferred=preferred)
        except FileNotFoundError as exc:
            raise StudioHTTPError(HTTPStatus.NOT_FOUND, str(exc)) from exc
        except ValueError as exc:
            raise StudioHTTPError(HTTPStatus.BAD_REQUEST, str(exc)) from exc


class StudioHTTPServer(ThreadingHTTPServer):
    """Loopback-only ThreadingHTTPServer holding CSRF state and studio backend."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        RequestHandlerClass: type[BaseHTTPRequestHandler],
        *,
        backend: Any,
        static_dir: Path,
        csrf_token: str,
        cdp_port: int = DEFAULT_CDP_PORT,
    ) -> None:
        if server_address[0] == "::1":
            self.address_family = socket.AF_INET6
        super().__init__(server_address, RequestHandlerClass)
        self.backend = backend
        self.static_dir = static_dir.resolve()
        self.csrf_token = csrf_token
        self.cdp_port = int(cdp_port)


class StudioRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler implementing all 14 Studio REST and static routes."""

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    @property
    def studio_server(self) -> StudioHTTPServer:
        return self.server  # type: ignore[return-value]

    def _send_json(self, status: int, payload: Any) -> None:
        safe_payload = _redact_secrets(payload)
        body = json.dumps(safe_payload, ensure_ascii=False).encode("utf-8")
        if int(status) >= 400:
            self.close_connection = True
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if int(status) >= 400:
            self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        data = path.read_bytes()
        mime = MIME_TYPES.get(path.suffix.lower(), "application/octet-stream")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'",
        )
        self.end_headers()
        self.wfile.write(data)

    def _enforce_security(self, *, require_csrf: bool) -> bool:
        if not validate_host_header(self.headers.get("Host")):
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "Forbidden: invalid Host header."})
            return False

        origin = self.headers.get("Origin")
        if require_csrf and not validate_origin_header(origin):
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "Forbidden: invalid Origin header."})
            return False

        if require_csrf:
            token = self.headers.get("X-Curator-Token") or ""
            if not token or not secrets.compare_digest(token, self.studio_server.csrf_token):
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "Forbidden: missing or invalid X-Curator-Token."})
                return False

        return True

    def _read_raw_body(self, max_bytes: int) -> bytes:
        raw_len = self.headers.get("Content-Length")
        if raw_len is not None:
            try:
                content_length = int(raw_len)
            except ValueError as exc:
                raise StudioHTTPError(HTTPStatus.BAD_REQUEST, "Invalid Content-Length header.") from exc
            if content_length < 0:
                raise StudioHTTPError(HTTPStatus.BAD_REQUEST, "Negative Content-Length.")
            if content_length > max_bytes:
                raise StudioHTTPError(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    f"Payload exceeds maximum limit of {max_bytes} bytes.",
                )
            data = self.rfile.read(content_length)
            if len(data) > max_bytes:
                raise StudioHTTPError(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    f"Payload exceeds maximum limit of {max_bytes} bytes.",
                )
            return data

        transfer_enc = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in transfer_enc:
            raise StudioHTTPError(HTTPStatus.LENGTH_REQUIRED, "Content-Length header is required.")
        return b""

    def _read_json_body(self) -> dict[str, Any]:
        raw = self._read_raw_body(MAX_JSON_BODY_BYTES)
        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StudioHTTPError(HTTPStatus.BAD_REQUEST, f"Invalid JSON body: {exc}") from exc
        if not isinstance(parsed, dict):
            raise StudioHTTPError(HTTPStatus.BAD_REQUEST, "JSON request body must be an object.")
        return parsed

    def do_GET(self) -> None:
        if not self._enforce_security(require_csrf=False):
            return

        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path

        try:
            if path == "/":
                index_file = resolve_safe_child(self.studio_server.static_dir, "index.html", must_exist=True)
                self._send_file(index_file)
                return

            if path.startswith("/static/"):
                rel_asset = path[len("/static/") :]
                asset_file = resolve_safe_child(self.studio_server.static_dir, rel_asset, must_exist=True)
                self._send_file(asset_file)
                return

            if path == "/api/status":
                query_params = urllib.parse.parse_qs(parsed.query)
                raw_cdp_port = query_params.get("cdp_port", [None])[0]
                if raw_cdp_port is not None:
                    req_cdp_port = _validate_cdp_port(raw_cdp_port, self.studio_server.cdp_port)
                    status_data = dict(self.studio_server.backend.get_status(cdp_port=req_cdp_port))
                else:
                    status_data = dict(self.studio_server.backend.get_status())
                status_data["csrf_token"] = self.studio_server.csrf_token
                status_data["host"] = self.studio_server.server_address[0]
                status_data["port"] = self.studio_server.server_port
                status_data.setdefault("cdp_port", self.studio_server.cdp_port)
                self._send_json(HTTPStatus.OK, status_data)
                return

            if path == "/api/presets":
                presets = self.studio_server.backend.list_presets()
                self._send_json(HTTPStatus.OK, {"presets": presets})
                return

            if path.startswith("/api/presets/"):
                raw_name = path[len("/api/presets/") :]
                try:
                    preset = self.studio_server.backend.get_preset(raw_name)
                except FileNotFoundError as exc:
                    raise StudioHTTPError(HTTPStatus.NOT_FOUND, f"Preset not found: {raw_name}") from exc
                self._send_json(HTTPStatus.OK, preset)
                return

            if path == "/api/snapshots":
                snapshots = self.studio_server.backend.list_snapshots()
                self._send_json(HTTPStatus.OK, {"snapshots": snapshots})
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Unknown endpoint: {path}"})
        except StudioHTTPError as exc:
            self._send_json(exc.status, {"error": exc.message})
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def do_POST(self) -> None:
        if not self._enforce_security(require_csrf=True):
            return

        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path

        try:
            if path == "/api/upload-resume":
                raw_body = self._read_raw_body(MAX_UPLOAD_BYTES)
                if not raw_body:
                    raise StudioHTTPError(HTTPStatus.BAD_REQUEST, "Uploaded resume payload is empty.")
                content_type = self.headers.get("Content-Type") or "application/octet-stream"
                if "multipart/form-data" in content_type.lower():
                    filename, file_bytes = parse_multipart_upload(content_type, raw_body)
                else:
                    default_name = "resume.txt" if "text/" in content_type.lower() else "resume.pdf"
                    filename = sanitize_filename(self.headers.get("X-Filename"), default=default_name)
                    file_bytes = raw_body
                if not file_bytes:
                    raise StudioHTTPError(HTTPStatus.BAD_REQUEST, "Uploaded file is empty.")
                result = self.studio_server.backend.parse_resume_bytes(file_bytes, filename=filename)
                self._send_json(HTTPStatus.OK, result)
                return

            payload = self._read_json_body()

            if path == "/api/generate":
                workspace_raw = str(payload.get("workspace_path") or "").strip()
                if workspace_raw:
                    validate_workspace_path(workspace_raw)
                if "cdp_port" in payload and payload.get("cdp_port") is not None:
                    payload = dict(payload)
                    payload["cdp_port"] = _validate_cdp_port(payload.get("cdp_port"), self.studio_server.cdp_port)
                result = self.studio_server.backend.generate(payload)
                self._send_json(HTTPStatus.OK, result)
                return

            if path == "/api/mix":
                weights = payload.get("weights")
                if not isinstance(weights, dict) or not weights:
                    raise StudioHTTPError(HTTPStatus.BAD_REQUEST, "'weights' must be a non-empty object.")
                for preset_key in weights:
                    validate_safe_identifier(str(preset_key), label="preset name")
                target_ratio = _normalize_ratio(payload.get("target_ratio"))
                result = self.studio_server.backend.mix_presets(weights, target_ratio=target_ratio)
                self._send_json(HTTPStatus.OK, result)
                return

            if path == "/api/audit":
                if payload.get("live"):
                    target_ratio = _normalize_ratio(payload.get("target_ratio"))
                    if "cdp_port" in payload and payload.get("cdp_port") is not None:
                        req_port = _validate_cdp_port(payload.get("cdp_port"), self.studio_server.cdp_port)
                        audit_res = self.studio_server.backend.audit_live(
                            target_ratio=target_ratio, cdp_port=req_port
                        )
                    else:
                        audit_res = self.studio_server.backend.audit_live(target_ratio=target_ratio)
                else:
                    manifest = _extract_manifest(payload)
                    audit_res = self.studio_server.backend.audit_manifest(manifest)
                self._send_json(HTTPStatus.OK, {"audit": audit_res})
                return

            if path == "/api/diff":
                manifest = _extract_manifest(payload)
                prune = bool(payload.get("prune", False))
                keep = [str(x) for x in (payload.get("keep") or []) if str(x).strip()]
                if "cdp_port" in payload and payload.get("cdp_port") is not None:
                    req_port = _validate_cdp_port(payload.get("cdp_port"), self.studio_server.cdp_port)
                    diff_res = self.studio_server.backend.compute_diff(
                        manifest, prune=prune, keep=keep, cdp_port=req_port
                    )
                else:
                    diff_res = self.studio_server.backend.compute_diff(manifest, prune=prune, keep=keep)
                self._send_json(HTTPStatus.OK, {"diff": diff_res})
                return

            if path == "/api/sync":
                manifest = _extract_manifest(payload)
                dry_run = bool(payload.get("dry_run", True))
                prune = bool(payload.get("prune", False))
                keep = [str(x) for x in (payload.get("keep") or []) if str(x).strip()]
                if "cdp_port" in payload and payload.get("cdp_port") is not None:
                    req_port = _validate_cdp_port(payload.get("cdp_port"), self.studio_server.cdp_port)
                    sync_res = self.studio_server.backend.sync_manifest(
                        manifest,
                        dry_run=dry_run,
                        prune=prune,
                        keep=keep,
                        cdp_port=req_port,
                    )
                else:
                    sync_res = self.studio_server.backend.sync_manifest(
                        manifest,
                        dry_run=dry_run,
                        prune=prune,
                        keep=keep,
                    )
                self._send_json(HTTPStatus.OK, {"sync": sync_res})
                return

            if path == "/api/rollback":
                raw_id = str(payload.get("snapshot_id") or "latest").strip()
                if raw_id != "latest":
                    validate_safe_identifier(raw_id, label="snapshot_id")
                dry_run = bool(payload.get("dry_run", False))
                if "cdp_port" in payload and payload.get("cdp_port") is not None:
                    req_port = _validate_cdp_port(payload.get("cdp_port"), self.studio_server.cdp_port)
                    rb_res = self.studio_server.backend.rollback(raw_id, dry_run=dry_run, cdp_port=req_port)
                else:
                    rb_res = self.studio_server.backend.rollback(raw_id, dry_run=dry_run)
                self._send_json(HTTPStatus.OK, {"rollback": rb_res})
                return

            if path == "/api/launch-browser":
                port_raw = payload.get("port")
                port_val = (
                    _validate_cdp_port(port_raw, self.studio_server.cdp_port)
                    if port_raw is not None and port_raw != ""
                    else None
                )
                preferred = str(payload["browser"]) if payload.get("browser") else None
                launch_res = self.studio_server.backend.launch_browser(port=port_val, preferred=preferred)
                self._send_json(HTTPStatus.OK, {"launch": launch_res})
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Unknown endpoint: {path}"})
        except StudioHTTPError as exc:
            self._send_json(exc.status, {"error": exc.message})
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})


def create_studio_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    backend: Any = None,
    cdp_port: int = DEFAULT_CDP_PORT,
) -> StudioHTTPServer:
    """Create and bind a hardened loopback-only StudioHTTPServer."""
    bind_host = validate_loopback_bind_host(host)
    static_dir = Path(__file__).resolve().parent / "static"
    studio_backend = backend if backend is not None else DefaultStudioBackend(cdp_port=cdp_port)
    csrf_token = secrets.token_urlsafe(24)
    return StudioHTTPServer(
        (bind_host, int(port)),
        StudioRequestHandler,
        backend=studio_backend,
        static_dir=static_dir,
        csrf_token=csrf_token,
        cdp_port=cdp_port,
    )


def run_studio(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    cdp_port: int = DEFAULT_CDP_PORT,
    open_browser: bool = True,
) -> None:
    """Start the local Web Studio server and optionally open it in a browser."""
    server = create_studio_server(host=host, port=port, cdp_port=cdp_port)
    bound_host, bound_port = server.server_address[:2]
    url_host = f"[{bound_host}]" if ":" in bound_host else bound_host
    url = f"http://{url_host}:{bound_port}/"
    print(f"reddit-curator Web Studio listening at {url} (Ctrl+C to stop)")

    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
