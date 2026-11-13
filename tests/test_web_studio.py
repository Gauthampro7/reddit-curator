"""Comprehensive standalone test suite for Slice 4 Local Web Studio."""

from __future__ import annotations

import http.client
import json
import os
import re
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from rcurator.studio.server import (
    MAX_UPLOAD_BYTES,
    DefaultStudioBackend,
    StudioHTTPError,
    _validate_cdp_port,
    create_studio_server,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = REPO_ROOT / "rcurator" / "studio" / "static"


class MockStudioBackend(DefaultStudioBackend):
    """Test backend using real ontology/presets on disk and deterministic stubs for Slices 1-3."""

    def __init__(self) -> None:
        super().__init__(project_root=REPO_ROOT)
        self.uploaded_payloads: list[tuple[str, bytes]] = []
        self.snapshots: list[dict[str, Any]] = [
            {
                "snapshot_id": "snapshot_20261113T120000000000Z_tester",
                "created_at": "2026-11-13T12:00:00Z",
                "username": "tester",
                "subscriptions_count": 14,
                "custom_feeds_count": 1,
                "manifest_name": "Systems Engineering & Infrastructure",
                "path": ".curator_snapshots/snapshot_20261113T120000000000Z_tester.json",
            }
        ]

    def get_status(self, cdp_port: int | None = None) -> dict[str, Any]:
        target_port = _validate_cdp_port(cdp_port, self.cdp_port)
        ontology = self._load_ontology_raw()
        subs = ontology.get("subreddits", {})
        return {
            "ok": True,
            "version": "2.0.0",
            "cdp_port": target_port,
            "cdp_connected": True,
            "authenticated": True,
            "username": "tester",
            "access_token": "MUST_BE_REDACTED",
            "cookies": {"token_v2": "MUST_BE_REDACTED"},
            "ontology_subreddit_count": len(subs),
            "presets_count": len(list(self.presets_dir.glob("*.json"))),
            "browsers": [
                {
                    "browser_id": "com.brave.Browser",
                    "kind": "flatpak",
                    "executable": "/usr/bin/flatpak",
                    "name": "Brave Browser (Flatpak)",
                }
            ],
        }

    def parse_resume_bytes(self, data: bytes, filename: str = "resume.pdf") -> dict[str, Any]:
        self.uploaded_payloads.append((filename, data))
        return {
            "filename": filename,
            "size_bytes": len(data),
            "evidences_count": 2,
            "evidences": [
                {
                    "term": "rust",
                    "raw_term": "Rust",
                    "weight": 3.5,
                    "source_type": "resume",
                    "source_detail": "skills",
                    "signal_kind": "language",
                },
                {
                    "term": "linux",
                    "raw_term": "Linux",
                    "weight": 2.8,
                    "source_type": "resume",
                    "source_detail": "experience",
                    "signal_kind": "ecosystem",
                },
            ],
            "top_terms": [{"term": "rust", "weight": 3.5}, {"term": "linux", "weight": 2.8}],
        }

    def generate(self, payload: dict[str, Any]) -> dict[str, Any]:
        preset = self.get_preset("systems_engineering")
        preset["name"] = payload.get("name") or "Auto-Generated Curation"
        preset["curation_provenance"] = {
            "algorithm": "bm25f_tfidf_bipartite_ppr_velocity_gr_v2",
            "sources_used": ["query"],
            "top_extracted_terms": [{"term": "rust", "weight": 2.2, "sources": ["query"]}],
            "achieved_velocity_distribution_pct": {
                "aesthetic_leisure": 40.0,
                "cognitive_curiosity": 35.0,
                "domain_priming": 25.0,
            },
            "diet_health": self.audit_manifest(preset),
            "explanations": {
                "rust": {
                    "name": "rust",
                    "category": "domain_priming",
                    "tier": "tier1_following",
                    "final_relevance": 0.91,
                    "lexical_score": 0.88,
                    "pagerank_score": 0.82,
                    "signal_density": 0.89,
                    "estimated_daily_posts": 24.0,
                    "impression_share_pct": 6.2,
                    "matched_terms": [{"term": "rust", "contribution": 0.72}],
                    "source_breakdown": {"query": 1.0},
                    "source_artifacts": ["studio_query"],
                    "graph_bridges": ["systems", "compilers"],
                    "routing_reason": "Selected for Tier-1 domain priming.",
                }
            },
        }
        return {"manifest": preset, "evidences_count": 3, "sources_used": ["query"]}

    def mix_presets(
        self,
        weights: dict[str, float],
        target_ratio: tuple[int, int, int] = (40, 35, 25),
    ) -> dict[str, Any]:
        first = next(iter(weights.keys()))
        manifest = self.get_preset(first)
        manifest["name"] = f"Mixed: {' + '.join(weights.keys())}"
        return {"manifest": manifest}

    def import_manifest(
        self,
        manifest: dict[str, Any],
        filename: str | None = None,
    ) -> dict[str, Any]:
        return {
            "filename": filename or "custom_manifest.json",
            "manifest": manifest,
            "audit": self.audit_manifest(manifest),
        }

    def audit_manifest(self, manifest: dict[str, Any]) -> dict[str, Any]:
        return {
            "subreddit_count": 18,
            "total_daily_velocity": 320.0,
            "target_distribution_pct": {
                "aesthetic_leisure": 40.0,
                "cognitive_curiosity": 35.0,
                "domain_priming": 25.0,
            },
            "actual_velocity_distribution_pct": {
                "aesthetic_leisure": 40.5,
                "cognitive_curiosity": 34.5,
                "domain_priming": 25.0,
            },
            "pillar_counts": {"aesthetic_leisure": 4, "cognitive_curiosity": 4, "domain_priming": 10},
            "golden_ratio_tvd_drift": 0.005,
            "golden_ratio_fidelity_score": 99.5,
            "shannon_domain_entropy_norm": 0.91,
            "gini_velocity_concentration": 0.28,
            "max_subreddit_dominance_pct": 14.2,
            "dominant_subreddit": "unixporn",
            "mean_signal_density": 0.86,
            "composite_health_score": 94.0,
            "unrecognized_subreddits": [],
            "recommendations": [],
            "per_subreddit_shares": [
                {
                    "name": "unixporn",
                    "category": "aesthetic_leisure",
                    "estimated_daily_posts": 95.0,
                    "impression_share_pct": 14.2,
                    "signal_density": 0.86,
                }
            ],
        }

    def audit_live(self, target_ratio: tuple[int, int, int] = (40, 35, 25)) -> dict[str, Any]:
        return self.audit_manifest({})

    def compute_diff(
        self,
        manifest: dict[str, Any],
        prune: bool = False,
        keep: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "to_subscribe": ["ebpf", "kernel"],
            "already_subscribed": ["unixporn", "Fedora"],
            "to_unsubscribe": ["funny"] if prune and "funny" not in (keep or []) else [],
            "unmanaged_kept": ["funny"] if not prune else [],
            "custom_feeds": {
                "systems_core": {
                    "action": "create",
                    "added_subreddits": ["ebpf", "kernel"],
                    "removed_subreddits": [],
                    "final_subreddits": ["ebpf", "kernel"],
                }
            },
            "has_changes": True,
        }

    def sync_manifest(
        self,
        manifest: dict[str, Any],
        dry_run: bool = True,
        prune: bool = False,
        keep: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "dry_run": dry_run,
            "username": "tester",
            "snapshot_id": None if dry_run else self.snapshots[0]["snapshot_id"],
            "subscriptions": {
                "dry_run": dry_run,
                "already_subscribed": ["unixporn"],
                "subscribed": ["ebpf"],
                "unsubscribed": [],
                "kept_unmanaged": [],
                "failed": [],
            },
            "custom_feeds": {
                "systems_core": {
                    "status": "dry_run" if dry_run else "success",
                    "http_code": 200,
                    "endpoint": "/api/multi/user/tester/m/systems_core",
                    "path": "/user/tester/m/systems_core",
                    "subreddits": ["ebpf"],
                    "error": None,
                }
            },
            "following_feed": None if dry_run else {"success": True, "method_used": "url_and_shadow_dom"},
        }

    def list_snapshots(self) -> list[dict[str, Any]]:
        return list(self.snapshots)

    def rollback(self, snapshot_id: str, dry_run: bool = False) -> dict[str, Any]:
        return {
            "snapshot_id": snapshot_id,
            "username": "tester",
            "dry_run": dry_run,
            "restored_subscriptions": ["unixporn"],
            "removed_subscriptions": [],
            "restored_custom_feeds": {},
            "deleted_custom_feeds": [],
            "success": True,
        }

    def launch_browser(self, port: int | None = None, preferred: str | None = None) -> dict[str, Any]:
        target_port = _validate_cdp_port(port, self.cdp_port)
        if preferred == "missing-browser":
            raise StudioHTTPError(404, "Browser not found: missing-browser")
        return {
            "launched": True,
            "reused_existing": False,
            "browser_id": preferred or "com.brave.Browser",
            "command": ["flatpak", "run", "com.brave.Browser", f"--remote-debugging-port={target_port}"],
            "pid": 4242,
            "port": target_port,
            "cdp_http_url": f"http://127.0.0.1:{target_port}",
            "user_data_dir": "/home/tester/.config/reddit-curator/chrome-profile",
        }


class TestWebStudioServer(unittest.TestCase):
    """Tests for all 14 REST routes, security hardening, and frontend XSS safety."""

    server: Any
    thread: threading.Thread
    backend: MockStudioBackend
    port: int
    csrf_token: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.backend = MockStudioBackend()
        cls.server = create_studio_server(host="127.0.0.1", port=0, backend=cls.backend)
        cls.port = cls.server.server_port
        cls.csrf_token = cls.server.csrf_token
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def _request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        req_headers = {"Host": f"127.0.0.1:{self.port}"}
        if headers:
            req_headers.update(headers)
        conn.request(method, path, body=body, headers=req_headers)
        resp = conn.getresponse()
        data = resp.read()
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        conn.close()
        return resp.status, resp_headers, data

    def _post_json(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        csrf: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        headers = {
            "Content-Type": "application/json",
            "X-Curator-Token": self.csrf_token if csrf is None else csrf,
        }
        if extra_headers:
            headers.update(extra_headers)
        raw = json.dumps(payload).encode("utf-8")
        status, _, data = self._request("POST", path, body=raw, headers=headers)
        return status, json.loads(data.decode("utf-8"))

    # --- Bind & Security Hardening Tests ---

    def test_refuses_non_loopback_bind(self) -> None:
        for bad_host in ("0.0.0.0", "192.168.1.10", "example.com"):
            with self.assertRaises(ValueError):
                create_studio_server(host=bad_host, port=0, backend=self.backend)

    def test_forged_host_header_rejected(self) -> None:
        for bad_host in ("evil.com", "evil.com:8420", "127.0.0.1.evil.com", "192.168.1.2:8420"):
            status, _, _ = self._request("GET", "/api/status", headers={"Host": bad_host})
            self.assertEqual(status, 403, f"Expected 403 for Host={bad_host!r}")

    def test_forged_origin_header_rejected_on_post(self) -> None:
        for bad_origin in ("http://evil.com", "https://attacker.local:8420", "null"):
            status, body = self._post_json(
                "/api/audit",
                {"live": True},
                extra_headers={"Origin": bad_origin},
            )
            self.assertEqual(status, 403, f"Expected 403 for Origin={bad_origin!r}")
            self.assertIn("error", body)

    def test_csrf_missing_or_invalid_rejected(self) -> None:
        raw = json.dumps({"live": True}).encode("utf-8")
        status_missing, _, _ = self._request(
            "POST",
            "/api/audit",
            body=raw,
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status_missing, 403)

        status_bad, _ = self._post_json("/api/audit", {"live": True}, csrf="wrong-token")
        self.assertEqual(status_bad, 403)

    def test_static_and_preset_path_traversal_blocked(self) -> None:
        # Use raw socket so client does not collapse '..' before sending
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as sock:
            req = (
                f"GET /static/../server.py HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.port}\r\n"
                f"Connection: close\r\n\r\n"
            ).encode("ascii")
            sock.sendall(req)
            resp_raw = sock.recv(4096).decode("utf-8", errors="replace")
        self.assertTrue(resp_raw.startswith("HTTP/1.1 403"), resp_raw.splitlines()[0])

        status_enc, _, _ = self._request("GET", "/static/%2e%2e/server.py")
        self.assertEqual(status_enc, 403)

        status_preset, _, _ = self._request("GET", "/api/presets/..%2f..%2fREADME")
        self.assertIn(status_preset, (400, 403))

        status_rb, _ = self._post_json("/api/rollback", {"snapshot_id": "../secret"})
        self.assertIn(status_rb, (400, 403))

    def test_oversized_resume_upload_returns_413(self) -> None:
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as sock:
            req = (
                f"POST /api/upload-resume HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.port}\r\n"
                f"X-Curator-Token: {self.csrf_token}\r\n"
                f"Content-Type: application/pdf\r\n"
                f"X-Filename: huge.pdf\r\n"
                f"Content-Length: {MAX_UPLOAD_BYTES + 1}\r\n"
                f"Connection: close\r\n\r\n"
            ).encode("ascii")
            sock.sendall(req)
            resp_raw = sock.recv(4096).decode("utf-8", errors="replace")
        self.assertTrue(resp_raw.startswith("HTTP/1.1 413"), resp_raw.splitlines()[0])

    def test_sensitive_workspace_paths_blocked_and_tmp_allowed(self) -> None:
        home = Path.home()
        blocked_paths = [
            "/etc",
            "/proc",
            "/root",
            "/var",
            "/usr",
            str(home / ".ssh"),
            str(home / ".gnupg"),
            str(home / ".config" / "google-chrome"),
            str(home / ".config" / "reddit-curator"),
        ]
        for p in blocked_paths:
            status, body = self._post_json("/api/generate", {"workspace_path": p, "query": "rust"})
            self.assertEqual(status, 403, f"Expected 403 for workspace_path={p!r}, got {status}: {body}")

        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            status_ok, body_ok = self._post_json(
                "/api/generate",
                {"workspace_path": tmpdir, "query": "rust"},
            )
            self.assertEqual(status_ok, 200, body_ok)
            self.assertIn("manifest", body_ok)

    # --- All 14 REST & Static Routes ---

    def test_static_root_and_assets(self) -> None:
        status, headers, data = self._request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("content-type", ""))
        html = data.decode("utf-8")
        self.assertIn("/static/styles.css", html)
        self.assertIn("/static/app.js", html)

        status_css, headers_css, _ = self._request("GET", "/static/styles.css")
        self.assertEqual(status_css, 200)
        self.assertIn("text/css", headers_css.get("content-type", ""))

        status_js, headers_js, _ = self._request("GET", "/static/app.js")
        self.assertEqual(status_js, 200)
        self.assertIn("javascript", headers_js.get("content-type", ""))

    def test_status_redacts_secrets_and_reports_real_counts(self) -> None:
        status, _, raw = self._request("GET", "/api/status")
        self.assertEqual(status, 200)
        data = json.loads(raw.decode("utf-8"))
        self.assertEqual(data["csrf_token"], self.csrf_token)
        self.assertEqual(data["ontology_subreddit_count"], 292)
        self.assertEqual(data["presets_count"], 17)
        self.assertNotIn("access_token", data)
        self.assertNotIn("cookies", data)

    def test_presets_list_and_detail(self) -> None:
        status, _, raw = self._request("GET", "/api/presets")
        self.assertEqual(status, 200)
        presets = json.loads(raw.decode("utf-8"))["presets"]
        self.assertEqual(len(presets), 17)

        status_one, _, raw_one = self._request("GET", "/api/presets/systems_engineering")
        self.assertEqual(status_one, 200)
        detail = json.loads(raw_one.decode("utf-8"))
        self.assertEqual(detail["name"], "Systems Engineering & Infrastructure")
        self.assertGreaterEqual(len(detail["domain_priming_subreddits"]), 10)

        status_missing, _, _ = self._request("GET", "/api/presets/nonexistent_preset_xyz")
        self.assertEqual(status_missing, 404)

    def test_upload_resume_raw_and_multipart_in_memory(self) -> None:
        sample_pdf = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n"
        status_raw, _, data_raw = self._request(
            "POST",
            "/api/upload-resume",
            body=sample_pdf,
            headers={
                "Content-Type": "application/pdf",
                "X-Filename": "my_cv.pdf",
                "X-Curator-Token": self.csrf_token,
            },
        )
        self.assertEqual(status_raw, 200)
        parsed_raw = json.loads(data_raw.decode("utf-8"))
        self.assertEqual(parsed_raw["filename"], "my_cv.pdf")
        self.assertEqual(parsed_raw["size_bytes"], len(sample_pdf))

        boundary = "----StudioTestBoundary123"
        mp_body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="resume.txt"\r\n'
            f"Content-Type: text/plain\r\n\r\n"
            f"Skills: Rust, Linux, eBPF, PostgreSQL\r\n"
            f"--{boundary}--\r\n"
        ).encode("utf-8")
        status_mp, _, data_mp = self._request(
            "POST",
            "/api/upload-resume",
            body=mp_body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "X-Curator-Token": self.csrf_token,
            },
        )
        self.assertEqual(status_mp, 200)
        parsed_mp = json.loads(data_mp.decode("utf-8"))
        self.assertEqual(parsed_mp["filename"], "resume.txt")
        self.assertEqual(parsed_mp["evidences_count"], 2)

    def test_generate_mix_audit_diff_sync_snapshots_rollback_launch(self) -> None:
        st_gen, gen_res = self._post_json(
            "/api/generate",
            {"query": "rust linux ebpf", "target_ratio": [40, 35, 25], "tier1_count": 18},
        )
        self.assertEqual(st_gen, 200)
        manifest = gen_res["manifest"]

        st_mix, mix_res = self._post_json(
            "/api/mix",
            {"weights": {"systems_engineering": 0.6, "rust_and_compilers": 0.4}},
        )
        self.assertEqual(st_mix, 200)
        self.assertIn("manifest", mix_res)

        st_imp, imp_res = self._post_json(
            "/api/import-manifest",
            {"manifest": manifest, "filename": "my_custom.json"},
        )
        self.assertEqual(st_imp, 200)
        self.assertEqual(imp_res["filename"], "my_custom.json")
        self.assertEqual(imp_res["manifest"]["name"], manifest["name"])
        self.assertGreater(imp_res["audit"]["composite_health_score"], 0)

        st_imp_bad, _ = self._post_json("/api/import-manifest", {"foo": "bar"})
        self.assertEqual(st_imp_bad, 400)

        st_audit, audit_res = self._post_json("/api/audit", {"manifest": manifest})
        self.assertEqual(st_audit, 200)
        self.assertGreater(audit_res["audit"]["composite_health_score"], 0)

        st_diff, diff_res = self._post_json("/api/diff", {"manifest": manifest, "prune": False})
        self.assertEqual(st_diff, 200)
        self.assertTrue(diff_res["diff"]["has_changes"])

        st_sync, sync_res = self._post_json("/api/sync", {"manifest": manifest, "dry_run": True})
        self.assertEqual(st_sync, 200)
        self.assertTrue(sync_res["sync"]["dry_run"])

        st_snaps, _, raw_snaps = self._request("GET", "/api/snapshots")
        self.assertEqual(st_snaps, 200)
        snaps = json.loads(raw_snaps.decode("utf-8"))["snapshots"]
        self.assertEqual(len(snaps), 1)

        st_rb, rb_res = self._post_json("/api/rollback", {"snapshot_id": "latest", "dry_run": True})
        self.assertEqual(st_rb, 200)
        self.assertTrue(rb_res["rollback"]["success"])

        st_launch, launch_res = self._post_json("/api/launch-browser", {"port": 9222, "browser": "com.brave.Browser"})
        self.assertEqual(st_launch, 200)
        self.assertTrue(launch_res["launch"]["launched"])
        self.assertEqual(launch_res["launch"]["browser_id"], "com.brave.Browser")

    def test_cdp_port_validation_and_browser_errors(self) -> None:
        st_ok, _, raw_ok = self._request("GET", "/api/status?cdp_port=9333")
        self.assertEqual(st_ok, 200)
        self.assertEqual(json.loads(raw_ok.decode("utf-8"))["cdp_port"], 9333)

        for bad_q in ("80", "70000", "abc"):
            st_bad, _, _ = self._request("GET", f"/api/status?cdp_port={bad_q}")
            self.assertEqual(st_bad, 400, f"Expected 400 for cdp_port={bad_q!r}")

        for bad_port in (80, 70000, "not-a-port", True):
            st_lbad, _ = self._post_json("/api/launch-browser", {"port": bad_port})
            self.assertEqual(st_lbad, 400, f"Expected 400 for launch port={bad_port!r}")

        st_missing, _ = self._post_json("/api/launch-browser", {"port": 9222, "browser": "missing-browser"})
        self.assertEqual(st_missing, 404)

    # --- Frontend XSS & Offline Safety Tests ---

    def test_frontend_zero_xss_and_zero_external_cdns(self) -> None:
        app_js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        index_html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        styles_css = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")

        for unsafe_sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function("):
            self.assertNotIn(unsafe_sink, app_js, f"Unsafe DOM sink {unsafe_sink!r} found in app.js")

        self.assertIsNone(re.search(r"\bstyle\s*:", app_js), "Inline style object key found in app.js; violates style-src 'self'")
        self.assertNotIn('setAttribute("style"', app_js)
        self.assertNotIn("setAttribute('style'", app_js)
        self.assertIn("b.browser_id", app_js)
        self.assertIn("btn-import-manifest", index_html)
        self.assertIn("import-manifest-input", index_html)
        self.assertIn("/api/import-manifest", app_js)
        self.assertIn('class="mode-easy"', index_html)
        self.assertIn("btn-mode-easy", index_html)
        self.assertIn("btn-mode-advanced", index_html)
        self.assertIn("view-easy", index_html)
        self.assertIn("easy-btn-launch", index_html)
        self.assertIn("easy-btn-apply", index_html)
        self.assertIn("easy-btn-import-json", index_html)
        self.assertIn("easy-btn-about", index_html)
        self.assertIn("easy-preset-preview", index_html)
        self.assertIn("easy-btn-preview", index_html)
        self.assertIn("btn-about", index_html)
        self.assertIn("view-about", index_html)
        self.assertIn("btn-about-close", index_html)
        self.assertIn('role="tabpanel"', index_html)
        self.assertIn("setUiMode", app_js)
        self.assertIn("openAboutView", app_js)
        self.assertIn("closeAboutView", app_js)
        self.assertIn("isSafeHttpsUrl", app_js)
        self.assertIn("renderEasyPresetPreview", app_js)
        self.assertIn("handleEasyPreview", app_js)
        self.assertIn("/api/about", app_js)
        self.assertIn(":focus-visible", styles_css)
        self.assertIn("body.about-open #view-about", styles_css)

        for asset_name, text in (("index.html", index_html), ("styles.css", styles_css), ("app.js", app_js)):
            scrubbed = text.replace("http://www.w3.org/2000/svg", "")
            self.assertIsNone(
                re.search(r"https?://", scrubbed, flags=re.IGNORECASE),
                f"External URL found in {asset_name}; studio must be 100% offline-capable",
            )

    def test_about_endpoint_and_philosophy_citations(self) -> None:
        st_about, _, raw_about = self._request("GET", "/api/about")
        self.assertEqual(st_about, 200)
        about = json.loads(raw_about.decode("utf-8"))
        self.assertIn("Reverse Passive Marketing", about.get("title", ""))
        self.assertGreaterEqual(len(about.get("global_statistics", [])), 4)
        self.assertEqual(len(about.get("pillars", [])), 3)
        self.assertEqual(about.get("ontology_subreddit_count"), 292)
        self.assertEqual(about.get("presets_count"), 17)

        refs = about.get("references", [])
        self.assertEqual(len(refs), 17)
        themes = {r.get("theme") for r in refs}
        self.assertEqual(
            themes,
            {
                "global_scale",
                "passive_marketing",
                "third_person_effect",
                "priming_availability",
                "controlled_engagement",
                "context_interleaving",
            },
        )
        serialized = json.dumps(about)
        self.assertIn("Tversky, A., & Kahneman, D.", serialized)
        self.assertIn("von Lindern, E.", serialized)
        self.assertIn("Mobile Media & Communication", serialized)
        self.assertIn("Media Psychology", serialized)
        self.assertIn("Journal of Technology in Behavioral Science", serialized)
        self.assertIn("Motivation Science", serialized)
        self.assertNotIn("19h 18m", serialized)
        for expected_doi in (
            "10.1037/h0025848",
            "10.1086/209496",
            "10.1016/0010-0285(73)90033-9",
            "10.1177/20501579211028647",
            "10.1080/15213269.2019.1688171",
            "10.1007/s41347-020-00189-w",
            "10.1111/j.1467-9280.2008.02127.x",
        ):
            self.assertIn(expected_doi, serialized)

        # Ensure philosophy.json uses zero _SECRET_KEYS as dictionary keys
        raw_phil = json.loads((REPO_ROOT / "curator_data" / "philosophy.json").read_text(encoding="utf-8"))
        forbidden_keys = {"access_token", "cookies", "cookie", "token_v2", "oauth_token"}

        def _walk_keys(obj: Any) -> None:
            if isinstance(obj, dict):
                for k, v in obj.items():
                    self.assertNotIn(str(k).lower(), forbidden_keys)
                    _walk_keys(v)
            elif isinstance(obj, list):
                for item in obj:
                    _walk_keys(item)

        _walk_keys(raw_phil)

        # Missing philosophy.json falls back cleanly without crashing
        with tempfile.TemporaryDirectory() as tmpdir:
            fallback_backend = DefaultStudioBackend(
                project_root=REPO_ROOT,
                philosophy_path=Path(tmpdir) / "missing_philosophy.json",
            )
            fallback_about = fallback_backend.get_about()
            self.assertIn("motto", fallback_about)
            self.assertEqual(fallback_about["references"], [])

    def test_default_backend_offline_dry_run_sync_fallback(self) -> None:
        from rcurator.cdp_client import CDPError

        real_backend = DefaultStudioBackend(project_root=REPO_ROOT)
        manifest = real_backend.get_preset("systems_engineering")
        with patch("rcurator.cdp_client.acquire_reddit_session", side_effect=CDPError("CDP offline")):
            dry_res = real_backend.sync_manifest(manifest, dry_run=True, cdp_port=9222)
        self.assertTrue(dry_res["dry_run"])
        self.assertEqual(dry_res["mode"], "offline_dry_run")
        self.assertFalse(dry_res["cdp_reachable"])
        self.assertGreater(len(dry_res["subscriptions"]["subscribed"]), 0)
        self.assertIsInstance(dry_res["custom_feeds"], dict)
        self.assertIsNone(dry_res["following_feed"])
        self.assertGreater(dry_res["analytics"]["composite_health_score"], 0)

        with patch("rcurator.cdp_client.acquire_reddit_session", side_effect=CDPError("CDP offline")):
            with self.assertRaises(CDPError):
                real_backend.sync_manifest(manifest, dry_run=False, cdp_port=9222)

    def test_default_studio_backend_reads_real_ontology_and_presets_and_passive_cdp(self) -> None:
        real_backend = DefaultStudioBackend(project_root=REPO_ROOT)

        class _FakeResp:
            status = 200

            def __enter__(self) -> "_FakeResp":
                return self

            def __exit__(self, *args: Any) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps([{"type": "page", "url": "https://example.com/", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/1"}]).encode("utf-8")

        with patch("urllib.request.urlopen", return_value=_FakeResp()):
            status = real_backend.get_status(cdp_port=9222)
        self.assertTrue(status["ok"])
        self.assertTrue(status["cdp_connected"])
        self.assertFalse(status["authenticated"])
        self.assertEqual(status["ontology_subreddit_count"], 292)
        self.assertEqual(status["presets_count"], 17)
        presets = real_backend.list_presets()
        self.assertEqual(len(presets), 17)
        loaded = real_backend.get_preset("linux_ricing_opensource")
        self.assertEqual(loaded["preset_id"], "linux_ricing_opensource")
        self.assertIn("anchor_subreddits", loaded)

        example_raw = json.loads((REPO_ROOT / "manifest.example.json").read_text(encoding="utf-8"))
        imported_example = real_backend.import_manifest(example_raw, filename="../manifest.example.json")
        self.assertEqual(imported_example["filename"], "manifest.example.json")
        self.assertIn("domain_priming_subreddits", imported_example["manifest"])
        self.assertGreater(imported_example["audit"]["composite_health_score"], 0)

        legacy_v1_raw = {
            "version": "1.0.0",
            "name": "Legacy v1 Fixture",
            "metadata": {
                "cognitive_distribution": {
                    "aesthetic_leisure_target_pct": 40,
                    "cognitive_curiosity_target_pct": 35,
                    "domain_priming_target_pct": 25,
                }
            },
            "anchor_subreddits": [
                {"name": "unixporn", "category": "aesthetic_leisure"},
                {"name": "askscience", "category": "cognitive_curiosity"},
            ],
            "tier1_systems_subreddits": [
                {"name": "rust", "category": "high_signal_systems_priming"},
            ],
            "custom_feeds": [
                {
                    "feed_name": "systems_focus",
                    "display_name": "Systems Focus",
                    "subreddits": ["rust", "linux"],
                }
            ],
        }
        imported = real_backend.import_manifest(legacy_v1_raw, filename="../legacy_v1.json")
        self.assertEqual(imported["filename"], "legacy_v1.json")
        self.assertIn("domain_priming_subreddits", imported["manifest"])
        self.assertGreater(imported["audit"]["composite_health_score"], 0)
        with self.assertRaises(StudioHTTPError):
            real_backend.import_manifest({"name": "empty"})


if __name__ == "__main__":
    unittest.main()
