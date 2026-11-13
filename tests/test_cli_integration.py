"""End-to-end integration tests for reddit_curator v2.0 CLI, wizard, and pyproject."""

from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

import requests

import reddit_curator
from rcurator.cdp_client import RedditSessionData
from rcurator.cli_wizard import run_wizard


def _make_minimal_pdf(text: str) -> bytes:
    """Build a valid single-page uncompressed PDF containing the given ASCII text."""
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream_body = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET\n".encode("latin-1")
    objects = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        (
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n"
        ),
        (
            f"4 0 obj\n<< /Length {len(stream_body)} >>\nstream\n".encode("ascii")
            + stream_body
            + b"endstream\nendobj\n"
        ),
        b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
    ]
    buf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objects:
        offsets.append(len(buf))
        buf.extend(obj)
    xref_pos = len(buf)
    buf.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    buf.extend(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        buf.extend(f"{off:010d} 00000 n \n".encode("ascii"))
    buf.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(buf)


def _find_closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _FakeRedditState:
    """In-memory stateful fake for Reddit OAuth endpoints used during E2E CLI tests."""

    def __init__(self, initial_subs: list[str] | None = None) -> None:
        self.username = "test_curator"
        self.subs: dict[str, dict[str, object]] = {}
        for name in initial_subs or []:
            self.subs[name.lower()] = {
                "name": name,
                "fullname": f"t5_{name.lower()}",
                "subscribers": 50000,
                "over18": False,
                "public_description": "",
                "url": f"/r/{name}/",
            }
        self.feeds: dict[str, dict[str, object]] = {}
        self.subscribe_calls: list[tuple[str, str]] = []
        self.following_activated = False

    def make_api_client(self, access_token: str, username: str) -> mock.MagicMock:
        state = self
        client = mock.MagicMock()
        client.access_token = access_token
        client.username = username

        def fetch_all_subscriptions() -> dict[str, dict[str, object]]:
            return dict(state.subs)

        def fetch_custom_feeds() -> list[dict[str, object]]:
            return [dict(v) for v in state.feeds.values()]

        def sync_subscriptions(
            desired: list[str],
            dry_run: bool = False,
            prune: bool = False,
            keep: set[str] | None = None,
        ) -> dict[str, object]:
            desired_map = {s.lower(): s for s in desired}
            already = [state.subs[k]["name"] for k in desired_map if k in state.subs]
            to_sub = [v for k, v in desired_map.items() if k not in state.subs]
            keep_low = {k.lower() for k in (keep or set())}
            to_unsub = [
                rec["name"]
                for low, rec in state.subs.items()
                if low not in desired_map and prune and low not in keep_low
            ]
            kept = [
                rec["name"]
                for low, rec in state.subs.items()
                if low not in desired_map and (not prune or low in keep_low)
            ]
            if dry_run:
                return {
                    "dry_run": True,
                    "already_subscribed": already,
                    "subscribed": to_sub,
                    "unsubscribed": to_unsub,
                    "kept_unmanaged": kept,
                    "failed": [],
                }
            for s in to_sub:
                state.subscribe_calls.append(("sub", s))
                state.subs[s.lower()] = {
                    "name": s,
                    "fullname": f"t5_{s.lower()}",
                    "subscribers": 25000,
                    "over18": False,
                    "public_description": "",
                    "url": f"/r/{s}/",
                }
            for s in to_unsub:
                state.subscribe_calls.append(("unsub", str(s)))
                state.subs.pop(str(s).lower(), None)
            return {
                "dry_run": False,
                "already_subscribed": already,
                "subscribed": to_sub,
                "unsubscribed": to_unsub,
                "kept_unmanaged": kept,
                "failed": [],
            }

        def sync_custom_feeds(
            feeds: list[dict[str, object]], dry_run: bool = False
        ) -> dict[str, dict[str, object]]:
            out: dict[str, dict[str, object]] = {}
            for feed in feeds:
                fname = str(feed["feed_name"])
                subs = [str(x) for x in feed.get("subreddits", [])]
                path = f"/user/{username.lower()}/m/{fname}"
                if not dry_run:
                    state.feeds[fname] = {
                        "feed_name": fname,
                        "display_name": feed.get("display_name", fname),
                        "path": path,
                        "description_md": feed.get("description_md", ""),
                        "icon_name": feed.get("icon_name", "programming"),
                        "visibility": feed.get("visibility", "private"),
                        "subreddits": subs,
                    }
                out[fname] = {
                    "status": "dry_run" if dry_run else "success",
                    "http_code": None if dry_run else 200,
                    "endpoint": f"/api/multi{path}",
                    "path": path,
                    "subreddits": subs,
                    "error": None,
                }
            return out

        def delete_custom_feed(path_or_name: str, dry_run: bool = False) -> bool:
            if dry_run:
                return True
            fname = path_or_name.rsplit("/", 1)[-1]
            return state.feeds.pop(fname, None) is not None

        client.fetch_all_subscriptions.side_effect = fetch_all_subscriptions
        client.fetch_custom_feeds.side_effect = fetch_custom_feeds
        client.sync_subscriptions.side_effect = sync_subscriptions
        client.sync_custom_feeds.side_effect = sync_custom_feeds
        client.delete_custom_feed.side_effect = delete_custom_feed
        return client


class TestPyprojectAndParser(unittest.TestCase):
    """Validate PEP 621 pyproject.toml and all 4 positional commands + 31 flags."""

    def test_pyproject_metadata_and_scripts(self) -> None:
        pyproject_path = reddit_curator.REPO_ROOT / "pyproject.toml"
        self.assertTrue(pyproject_path.is_file())
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        project = data["project"]
        self.assertEqual(project["name"], "reddit-curator")
        self.assertEqual(project["version"], "2.0.0")
        self.assertEqual(project["requires-python"], ">=3.10")
        self.assertEqual(
            project["dependencies"], ["requests>=2.28.0", "websockets>=11.0"]
        )
        self.assertEqual(project["scripts"]["reddit-curator"], "reddit_curator:main")
        self.assertIn("reddit_curator", data["tool"]["setuptools"]["py-modules"])
        self.assertIn("rcurator*", data["tool"]["setuptools"]["packages"]["find"]["include"])

    def test_parse_args_commands_and_all_31_flags(self) -> None:
        # Default positional command preserves legacy flag-only invocation
        legacy = reddit_curator.parse_args(
            ["--preset", "systems_engineering", "--one-shot", "--port", "9333"]
        )
        self.assertEqual(legacy.command, "sync")
        self.assertEqual(legacy.preset, "systems_engineering")
        self.assertTrue(legacy.one_shot)
        self.assertEqual(legacy.port, 9333)

        for cmd in ("sync", "studio", "ui", "wizard"):
            ns = reddit_curator.parse_args([cmd])
            self.assertEqual(ns.command, cmd)

        full = reddit_curator.parse_args(
            [
                "sync",
                "--port",
                "9223",
                "--manifest",
                "manifest.example.json",
                "--preset",
                "machine_learning",
                "--list-presets",
                "--one-shot",
                "--agent",
                "--dry-run",
                "--verify",
                "--from-workspace",
                ".",
                "--from-github",
                "octocat",
                "--github-token",
                "ghp_test",
                "--from-resume",
                "resume.pdf",
                "--query",
                "ebpf rust",
                "--mix",
                "systems_engineering:0.6,rust_and_compilers:0.4",
                "--ratio",
                "40,35,25",
                "--tier1-count",
                "16",
                "--discover-online",
                "--output",
                "out.json",
                "--audit",
                "--audit-live",
                "--diff",
                "--prune",
                "--keep",
                "Fedora,unixporn",
                "--snapshot",
                "--list-snapshots",
                "--rollback",
                "latest",
                "--launch-browser",
                "--browser",
                "chromium",
                "--studio",
                "--studio-port",
                "8450",
                "--no-open-browser",
            ]
        )
        self.assertEqual(full.port, 9223)
        self.assertEqual(full.manifest, Path("manifest.example.json"))
        self.assertEqual(full.preset, "machine_learning")
        self.assertTrue(full.list_presets)
        self.assertTrue(full.one_shot)
        self.assertTrue(full.agent)
        self.assertTrue(full.dry_run)
        self.assertTrue(full.verify)
        self.assertEqual(full.from_workspace, Path("."))
        self.assertEqual(full.from_github, "octocat")
        self.assertEqual(full.github_token, "ghp_test")
        self.assertEqual(full.from_resume, Path("resume.pdf"))
        self.assertEqual(full.query, "ebpf rust")
        self.assertEqual(full.mix, "systems_engineering:0.6,rust_and_compilers:0.4")
        self.assertEqual(full.ratio, "40,35,25")
        self.assertEqual(full.tier1_count, 16)
        self.assertTrue(full.discover_online)
        self.assertEqual(full.output, Path("out.json"))
        self.assertTrue(full.audit)
        self.assertTrue(full.audit_live)
        self.assertTrue(full.diff)
        self.assertTrue(full.prune)
        self.assertEqual(full.keep, "Fedora,unixporn")
        self.assertTrue(full.snapshot)
        self.assertTrue(full.list_snapshots)
        self.assertEqual(full.rollback, "latest")
        self.assertTrue(full.launch_browser)
        self.assertEqual(full.browser, "chromium")
        self.assertTrue(full.studio)
        self.assertEqual(full.studio_port, 8450)
        self.assertTrue(full.no_open_browser)

    def test_cli_value_parsers(self) -> None:
        self.assertEqual(reddit_curator.parse_ratio("40,35,25"), (40, 35, 25))
        self.assertEqual(reddit_curator.parse_ratio("80,70,50"), (40, 35, 25))
        with self.assertRaises(ValueError):
            reddit_curator.parse_ratio("40,60")
        self.assertEqual(
            reddit_curator.parse_mix_spec("systems_engineering:0.6,rust_and_compilers:0.4"),
            {"systems_engineering": 0.6, "rust_and_compilers": 0.4},
        )
        with self.assertRaises(ValueError):
            reddit_curator.parse_mix_spec("systems_engineering:0")
        self.assertEqual(reddit_curator.validate_tier1_count(3), 3)
        with self.assertRaises(ValueError):
            reddit_curator.validate_tier1_count(2)


class TestOfflineCLIModes(unittest.TestCase):
    """Test offline-first CLI modes: list-presets, audit, multi-source, mix, offline dry-run."""

    def _run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        exit_code = 0
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                reddit_curator.main(argv)
            except SystemExit as exc:
                exit_code = int(exc.code) if isinstance(exc.code, int) else 1
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_list_presets_human_and_agent(self) -> None:
        code, out, _ = self._run_cli(["--list-presets"])
        self.assertEqual(code, 0)
        self.assertIn("Available Curated Presets (17)", out)
        self.assertIn("systems_engineering", out)
        self.assertIn("rust_and_compilers", out)

        code, out_json, _ = self._run_cli(["--list-presets", "--agent"])
        self.assertEqual(code, 0)
        payload = json.loads(out_json)
        self.assertEqual(payload["count"], 17)
        ids = {p["id"] for p in payload["presets"]}
        self.assertIn("systems_engineering", ids)
        self.assertIn("machine_learning", ids)

    def test_offline_audit_manifest_and_preset(self) -> None:
        code, out, _ = self._run_cli(
            ["--manifest", str(reddit_curator.REPO_ROOT / "manifest.example.json"), "--audit"]
        )
        self.assertEqual(code, 0)
        self.assertIn("Manifest Diet Health Audit", out)
        self.assertIn("Golden Ratio Score", out)

        code, out_json, _ = self._run_cli(["--preset", "systems_engineering", "--audit", "--agent"])
        self.assertEqual(code, 0)
        payload = json.loads(out_json)
        self.assertEqual(payload["manifest_name"], "Systems Engineering & Infrastructure")
        self.assertIn("golden_ratio_fidelity_score", payload["audit"])
        self.assertGreater(payload["audit"]["composite_health_score"], 50.0)

    def test_multi_source_fusion_workspace_resume_query_to_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            ws_dir = tmp / "project"
            ws_dir.mkdir()
            (ws_dir / "pyproject.toml").write_text(
                '[project]\nname = "ebpf-kv"\ndependencies = ["duckdb", "fastapi"]\n',
                encoding="utf-8",
            )
            (ws_dir / "main.py").write_text(
                "import duckdb\nimport fastapi\n", encoding="utf-8"
            )
            (ws_dir / "Containerfile").write_text("FROM fedora:41\n", encoding="utf-8")

            resume_path = tmp / "resume.pdf"
            resume_path.write_bytes(
                _make_minimal_pdf("Skills: Rust, Linux Kernel, eBPF, PostgreSQL, Kubernetes")
            )
            output_path = tmp / "generated_manifest.json"

            code, out_json, _ = self._run_cli(
                [
                    "--from-workspace",
                    str(ws_dir),
                    "--from-resume",
                    str(resume_path),
                    "--query",
                    "systems programming distributed databases",
                    "--ratio",
                    "40,35,25",
                    "--tier1-count",
                    "16",
                    "--output",
                    str(output_path),
                    "--audit",
                    "--agent",
                ]
            )
            self.assertEqual(code, 0)
            payload = json.loads(out_json)
            self.assertEqual(payload["output_path"], str(output_path))
            self.assertTrue(output_path.is_file())

            saved = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["version"], "2.0.0")
            self.assertGreater(len(saved["anchor_subreddits"]), 0)
            self.assertGreater(len(saved["domain_priming_subreddits"]), 0)
            prov = saved["curation_provenance"]
            self.assertEqual(set(prov["sources_used"]), {"workspace", "resume", "query"})
            self.assertIn("diet_health", prov)
            self.assertIn("audit", payload)

    def test_mix_and_blended_mix_with_query(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            out_file = Path(tmpdir) / "mixed.json"
            code, out_json, _ = self._run_cli(
                [
                    "--mix",
                    "systems_engineering:0.6,rust_and_compilers:0.4",
                    "--ratio",
                    "40,35,25",
                    "--audit",
                    "--output",
                    str(out_file),
                    "--agent",
                ]
            )
            self.assertEqual(code, 0)
            payload = json.loads(out_json)
            self.assertTrue(out_file.is_file())
            manifest = payload["manifest"]
            self.assertIn("Mixed Diet:", manifest["name"])
            self.assertIn("preset_mixer", manifest["curation_provenance"]["sources_used"])

            # Blending --mix + --query together works cleanly
            code2, out2, _ = self._run_cli(
                [
                    "--mix",
                    "systems_engineering:0.5,machine_learning:0.5",
                    "--query",
                    "inference engine",
                    "--agent",
                ]
            )
            self.assertEqual(code2, 0)
            p2 = json.loads(out2)
            self.assertEqual(p2["manifest"]["name"], "Blended & Auto-Generated Curation")

    def test_discover_online_public_fallback_and_offline_resilience(self) -> None:
        with mock.patch(
            "reddit_curator.acquire_reddit_session", side_effect=RuntimeError("CDP down")
        ), mock.patch(
            "reddit_curator.requests.get",
            side_effect=requests.ConnectionError("Offline"),
        ):
            # Must not crash when offline
            code, out_json, _ = self._run_cli(
                ["--query", "rust compilers", "--discover-online", "--agent"]
            )
            self.assertEqual(code, 0)
            payload = json.loads(out_json)
            self.assertIn("manifest", payload)

        # Public search enrichment works when online JSON returns candidates
        fake_resp = mock.MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {
            "data": {
                "children": [
                    {
                        "data": {
                            "display_name": "CustomKernelDev",
                            "title": "Kernel Development",
                            "public_description": "Linux kernel and eBPF engineering",
                            "subscribers": 15000,
                            "over18": False,
                        }
                    }
                ]
            }
        }
        with mock.patch(
            "reddit_curator.acquire_reddit_session", side_effect=RuntimeError("CDP down")
        ), mock.patch("reddit_curator.requests.get", return_value=fake_resp):
            cands = reddit_curator.discover_online_subreddits("kernel")
            self.assertEqual(len(cands), 1)
            self.assertEqual(cands[0]["name"], "CustomKernelDev")

    def test_offline_dry_run_when_cdp_port_closed(self) -> None:
        closed_port = _find_closed_port()
        code, out_json, _ = self._run_cli(
            [
                "--preset",
                "systems_engineering",
                "--dry-run",
                "--port",
                str(closed_port),
                "--agent",
            ]
        )
        self.assertEqual(code, 0)
        payload = json.loads(out_json)
        self.assertEqual(payload["mode"], "offline_dry_run")
        self.assertTrue(payload["dry_run"])
        self.assertFalse(payload["cdp_reachable"])
        self.assertGreater(len(payload["desired_anchors"]), 0)
        self.assertGreater(len(payload["desired_priming"]), 0)
        self.assertIn("analytics", payload)

    def test_agent_error_emits_valid_json_and_nonzero_exit(self) -> None:
        code, out_json, _ = self._run_cli(
            ["--preset", "nonexistent_preset_xyz", "--agent"]
        )
        self.assertEqual(code, 1)
        payload = json.loads(out_json)
        self.assertFalse(payload["success"])
        self.assertIn("error", payload)


class TestMockedCDPEndToEnd(unittest.TestCase):
    """End-to-end mocked CDP + Reddit OAuth tests covering sync, diff, audit-live, snapshot, rollback, verify."""

    def _run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        exit_code = 0
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                reddit_curator.main(argv)
            except SystemExit as exc:
                exit_code = int(exc.code) if isinstance(exc.code, int) else 1
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_one_shot_sync_anchors_priming_snapshot_diff_audit_live_verify_rollback(self) -> None:
        state = _FakeRedditState(initial_subs=["Fedora", "KeepMeSub", "RandomLegacySub"])
        session = RedditSessionData(
            username=state.username,
            access_token="tok_secret_test",
            target_id="tab_1",
            ws_url="ws://127.0.0.1:9222/devtools/page/tab_1",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            snaps_dir = Path(tmpdir) / ".curator_snapshots"
            with mock.patch.object(reddit_curator, "SNAPSHOTS_DIR", snaps_dir), mock.patch(
                "reddit_curator.acquire_reddit_session", return_value=session
            ), mock.patch(
                "reddit_curator.activate_following_feed",
                return_value={"success": True, "method_used": "shadow_dom_#feed-tab-2", "message": "ok", "elapsed_ms": 12},
            ), mock.patch(
                "reddit_curator.RedditAPIClient", side_effect=state.make_api_client
            ):
                # 1. --diff before sync
                code, diff_out, _ = self._run_cli(
                    ["--preset", "systems_engineering", "--diff", "--prune", "--keep", "KeepMeSub", "--agent"]
                )
                self.assertEqual(code, 0)
                diff_data = json.loads(diff_out)["diff"]
                self.assertIn("unixporn", diff_data["to_subscribe"])
                self.assertIn("Fedora", diff_data["already_subscribed"])
                self.assertIn("RandomLegacySub", diff_data["to_unsubscribe"])
                self.assertIn("KeepMeSub", diff_data["unmanaged_kept"])

                # 2. --one-shot sync (WITHOUT prune first) -> verifies BOTH anchors + priming subscribed & snapshot created
                code, sync_out, _ = self._run_cli(
                    ["--preset", "systems_engineering", "--one-shot", "--agent"]
                )
                self.assertEqual(code, 0)
                sync_data = json.loads(sync_out)
                self.assertEqual(sync_data["username"], "test_curator")
                self.assertIsNotNone(sync_data["snapshot_id"])
                self.assertGreater(len(sync_data["anchors_subscribed"]), 0)
                self.assertGreater(len(sync_data["priming_subscribed"]), 0)
                self.assertTrue(sync_data["reloaded_following"])
                self.assertTrue(sync_data["verification"]["all_healthy"])
                self.assertEqual(sync_data["verification"]["subscriptions"]["missing_anchors"], [])
                self.assertEqual(sync_data["verification"]["subscriptions"]["missing_priming"], [])
                self.assertIn("RandomLegacySub", [v["name"] for v in state.subs.values()])

                # 3. Verify anchor verification bug is truly fixed: drop an anchor and run --verify
                state.subs.pop("unixporn")
                code, ver_out, _ = self._run_cli(
                    ["--preset", "systems_engineering", "--verify", "--agent"]
                )
                self.assertEqual(code, 0)
                ver_data = json.loads(ver_out)
                self.assertFalse(ver_data["verification"]["all_healthy"])
                self.assertIn("unixporn", ver_data["verification"]["subscriptions"]["missing_anchors"])

                # 4. --one-shot with --prune --keep KeepMeSub preserves unmanaged allowlist
                code, prune_out, _ = self._run_cli(
                    [
                        "--preset",
                        "systems_engineering",
                        "--one-shot",
                        "--prune",
                        "--keep",
                        "KeepMeSub",
                        "--agent",
                    ]
                )
                self.assertEqual(code, 0)
                prune_data = json.loads(prune_out)
                self.assertIn("RandomLegacySub", prune_data["subscription_sync"]["unsubscribed"])
                self.assertIn("keepmesub", state.subs)
                self.assertNotIn("randomlegacysub", state.subs)

                # 5. --audit-live
                code, audit_out, _ = self._run_cli(["--audit-live", "--agent"])
                self.assertEqual(code, 0)
                audit_data = json.loads(audit_out)
                self.assertEqual(audit_data["username"], "test_curator")
                self.assertGreater(audit_data["audit"]["subreddit_count"], 0)

                # 6. --snapshot & --list-snapshots
                code, snap_out, _ = self._run_cli(["--snapshot", "--agent"])
                self.assertEqual(code, 0)
                snap_id = json.loads(snap_out)["snapshot_id"]
                self.assertTrue(snap_id.startswith("snapshot_"))

                code, list_out, _ = self._run_cli(["--list-snapshots", "--agent"])
                self.assertEqual(code, 0)
                snaps_list = json.loads(list_out)
                self.assertGreaterEqual(snaps_list["count"], 3)

                # 7. --rollback to the very first snapshot (oldest one has RandomLegacySub + KeepMeSub)
                oldest_id = snaps_list["snapshots"][-1]["snapshot_id"]
                code, rb_out, _ = self._run_cli(["--rollback", oldest_id, "--agent"])
                self.assertEqual(code, 0)
                rb_data = json.loads(rb_out)
                self.assertTrue(rb_data["success"])
                self.assertIn("randomlegacysub", state.subs)
                self.assertIn("keepmesub", state.subs)

    def test_studio_ui_and_launch_browser_dispatch(self) -> None:
        with mock.patch("reddit_curator.run_studio") as mock_studio:
            code, _, _ = self._run_cli(["studio", "--studio-port", "8555", "--no-open-browser"])
            self.assertEqual(code, 0)
            mock_studio.assert_called_once_with(
                host="127.0.0.1", port=8555, cdp_port=9222, open_browser=False
            )

            mock_studio.reset_mock()
            code, _, _ = self._run_cli(["ui"])
            self.assertEqual(code, 0)
            mock_studio.assert_called_once_with(
                host="127.0.0.1", port=8420, cdp_port=9222, open_browser=True
            )

            mock_studio.reset_mock()
            code, _, _ = self._run_cli(["--studio", "--studio-port", "8499"])
            self.assertEqual(code, 0)
            mock_studio.assert_called_once_with(
                host="127.0.0.1", port=8499, cdp_port=9222, open_browser=True
            )

        fake_launch = {
            "launched": True,
            "reused_existing": False,
            "browser_id": "chromium",
            "command": ["/usr/bin/chromium"],
            "pid": 12345,
            "port": 9222,
            "cdp_http_url": "http://127.0.0.1:9222",
            "user_data_dir": "/tmp/prof",
        }
        with mock.patch("reddit_curator.BrowserLauncher.launch", return_value=fake_launch) as m_launch:
            code, out_json, _ = self._run_cli(
                ["--launch-browser", "--browser", "chromium", "--agent"]
            )
            self.assertEqual(code, 0)
            m_launch.assert_called_once_with(port=9222, preferred="chromium")
            self.assertEqual(json.loads(out_json)["browser_id"], "chromium")


class TestInteractiveWizard(unittest.TestCase):
    """Deterministic walkthrough tests for rcurator.cli_wizard."""

    def test_wizard_preset_save_and_offline_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            out_manifest = tmp / "wizard_preset.json"
            answers = iter(
                [
                    "1",                    # Mode 1: Preset
                    "40,35,25",             # Golden ratio
                    "systems_engineering",  # Preset key
                    str(out_manifest),      # Save path
                    "1",                    # Offline/live dry-run
                ]
            )
            stream = io.StringIO()
            closed_port = _find_closed_port()
            res = run_wizard(
                input_fn=lambda _: next(answers),
                output_stream=stream,
                snapshots_dir=tmp / "snaps",
                cdp_port=closed_port,
            )
            self.assertEqual(res["mode"], "1")
            self.assertEqual(res["action"], "dry_run")
            self.assertTrue(out_manifest.is_file())
            self.assertEqual(res["sync_result"]["mode"], "offline_dry_run")
            text = stream.getvalue()
            self.assertIn("Systems Engineering & Infrastructure", text)
            self.assertIn("Offline Dry-Run Preview", text)

    def test_wizard_auto_curate_and_mixer_and_cli_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            ws = tmp / "repo"
            ws.mkdir()
            (ws / "Cargo.toml").write_text('[package]\nname = "tinykv"\n', encoding="utf-8")
            out_auto = tmp / "auto.json"

            answers_auto = iter(
                [
                    "2",                     # Mode 2: Auto-Curate
                    "40,35,25",              # Ratio
                    "15",                    # Tier-1 count
                    str(ws),                 # Workspace path
                    "",                      # Skip resume
                    "",                      # Skip GitHub
                    "rust storage engines",  # Query
                    str(out_auto),           # Save path
                    "3",                     # Exit
                ]
            )
            stream = io.StringIO()
            res_auto = run_wizard(
                input_fn=lambda _: next(answers_auto),
                output_stream=stream,
                snapshots_dir=tmp / "snaps",
            )
            self.assertEqual(res_auto["mode"], "2")
            self.assertEqual(res_auto["action"], "exit")
            self.assertTrue(out_auto.is_file())
            self.assertIn("Top Terms", stream.getvalue())
            self.assertIn("Diet Health Summary", stream.getvalue())

            # Mode 3: Mixer via CLI `wizard` dispatch
            answers_mix = iter(
                [
                    "3",
                    "40,35,25",
                    "16",
                    "systems_engineering:0.6,rust_and_compilers:0.4",
                    "skip",
                    "3",
                ]
            )
            with mock.patch("reddit_curator.run_wizard") as m_wiz:
                reddit_curator.main(["wizard"])
                m_wiz.assert_called_once()

            stream_mix = io.StringIO()
            res_mix = run_wizard(
                input_fn=lambda _: next(answers_mix),
                output_stream=stream_mix,
                snapshots_dir=tmp / "snaps",
            )
            self.assertEqual(res_mix["mode"], "3")
            self.assertIn("Mixed Diet:", res_mix["manifest"]["name"])

    def test_github_miner_cli_and_legacy_curator_methods(self) -> None:
        from rcurator.sources.types import SignalEvidence

        fake_ev = [
            SignalEvidence(
                term="rust",
                raw_term="Rust",
                weight=3.2,
                source_type="github",
                source_detail="octocat/tinykv",
                signal_kind="language",
            ),
            SignalEvidence(
                term="ebpf",
                raw_term="eBPF",
                weight=2.8,
                source_type="github",
                source_detail="octocat/ebpf-probe",
                signal_kind="keyword",
            ),
        ]
        stdout = io.StringIO()
        with mock.patch("reddit_curator.GitHubMiner") as m_gh, contextlib.redirect_stdout(stdout):
            m_gh.return_value.mine.return_value = fake_ev
            reddit_curator.main(["--from-github", "octocat", "--github-token", "tok123", "--agent"])
        m_gh.assert_called_once_with(token="tok123")
        payload = json.loads(stdout.getvalue())
        self.assertIn("github", payload["manifest"]["curation_provenance"]["sources_used"])

        # Verify legacy RedditCurator.subscribe_priming_subreddits works seamlessly
        state = _FakeRedditState()
        curator = reddit_curator.RedditCurator(preset_name="systems_engineering", dry_run=True)
        session = reddit_curator.RedditSession(
            username="test_curator",
            access_token="tok_secret",
            target_id="tab_1",
            ws_url="ws://127.0.0.1:9222/devtools/page/tab_1",
        )
        with mock.patch("reddit_curator.RedditAPIClient", side_effect=state.make_api_client):
            primed = curator.subscribe_priming_subreddits(session)
        self.assertEqual(primed, curator.get_priming_subreddits())


if __name__ == "__main__":
    unittest.main()
