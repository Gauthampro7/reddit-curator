"""Unit tests for Slice 2: TechnicalTokenizer and multi-source Signal Miners."""

from __future__ import annotations

import base64
import json
import math
import os
import stat
import tempfile
import unittest
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from rcurator.nlp.tokenizer import (
    CANONICAL_ALIASES,
    TechnicalTokenizer,
    canonicalize_token,
    load_ontology_phrases,
)
from rcurator.sources.github import GitHubMiner, compute_repo_weight
from rcurator.sources.resume import (
    EncryptedPDFError,
    MAX_BFRANGE_ENTRIES,
    MAX_RESUME_BYTES,
    ResumeParser,
    _parse_tounicode_cmap,
)
from rcurator.sources.types import SignalEvidence
from rcurator.sources.workspace import (
    WorkspaceScanner,
    is_secret_filename,
    is_sensitive_path,
)


def _build_synthetic_pdf(objects_body: list[tuple[int, bytes]], trailer_extra: bytes = b"") -> bytes:
    """Construct a valid minimal PDF byte stream with accurate xref offsets."""
    buf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}
    max_obj = 0
    for obj_num, body in objects_body:
        max_obj = max(max_obj, obj_num)
        offsets[obj_num] = len(buf)
        buf.extend(f"{obj_num} 0 obj\n".encode("ascii"))
        buf.extend(body)
        if not body.endswith(b"\n"):
            buf.extend(b"\n")
        buf.extend(b"endobj\n")

    xref_offset = len(buf)
    buf.extend(f"xref\n0 {max_obj + 1}\n".encode("ascii"))
    buf.extend(b"0000000000 65535 f \n")
    for i in range(1, max_obj + 1):
        off = offsets.get(i, 0)
        flag = "n" if i in offsets else "f"
        buf.extend(f"{off:010d} 00000 {flag} \n".encode("ascii"))
    buf.extend(
        f"trailer\n<< /Size {max_obj + 1} /Root 1 0 R {trailer_extra.decode('latin-1')}>>\n".encode(
            "latin-1"
        )
    )
    buf.extend(f"startxref\n{xref_offset}\n%%EOF\n".encode("ascii"))
    return bytes(buf)


def _apply_png_up_predictor(raw: bytes, columns: int) -> bytes:
    """Encode raw bytes with PNG Up predictor (filter byte 2 per row) for test fixtures."""
    pad = (-len(raw)) % columns
    padded = raw + (b"\x00" * pad)
    out = bytearray()
    prev = bytearray(columns)
    for off in range(0, len(padded), columns):
        row = padded[off : off + columns]
        out.append(2)  # PNG Up filter
        for i in range(columns):
            out.append((row[i] - prev[i]) & 0xFF)
        prev = bytearray(row)
    return bytes(out)


class _FakeHTTPResponse:
    def __init__(self, status_code: int, payload: object = None, headers: dict[str, str] | None = None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self) -> object:
        return self._payload


class TestTechnicalTokenizer(unittest.TestCase):
    def test_preserves_all_ontology_punctuated_tokens(self) -> None:
        specials = [
            ".net",
            "async/await",
            "c#",
            "f#",
            "c++",
            "c++20",
            "c++23",
            "ci/cd",
            "core.async",
            "data.table",
            "gnu/linux",
            "http/3",
            "lazy.nvim",
            "llama.cpp",
            "m&a",
            "node.js",
            "next.js",
            "vue.js",
            "nuxt.js",
            "d3.js",
            "three.js",
            "pub/sub",
            "sdr++",
            "span<t>",
            "tcp/ip",
            "io_uring",
            "scikit-learn",
            "x86_64",
            "aarch64",
            "ebpf",
            "risc-v",
            "zero-copy",
            "lsm-tree",
            "b-tree",
        ]
        tok = TechnicalTokenizer(load_ontology_phrases())
        sentence = "Stack: " + ", ".join(s.upper() for s in specials) + "."
        terms = {term for term, _ in tok.tokenize(sentence)}
        for expected in specials:
            self.assertIn(expected, terms, f"Missing preserved token: {expected}")

    def test_canonical_aliases_and_edge_punctuation(self) -> None:
        for alias, target in CANONICAL_ALIASES.items():
            self.assertEqual(canonicalize_token(alias), target)
            self.assertEqual(canonicalize_token(f"({alias.upper()}),"), target)
        self.assertEqual(canonicalize_token("  Node.JS. "), "node.js")
        self.assertEqual(canonicalize_token("llama.cpp!"), "llama.cpp")

    def test_camelcase_snakecase_and_short_protected_tokens(self) -> None:
        tok = TechnicalTokenizer()
        pairs = tok.tokenize("FastAPI DuckDB async_io_engine C R Go ML AI OS DB IR VM the def return")
        weights: dict[str, list[float]] = {}
        for t, w in pairs:
            weights.setdefault(t, []).append(w)

        self.assertIn(1.0, weights["fastapi"])
        self.assertIn(0.5, weights["fast"])
        self.assertIn(0.5, weights["api"])
        self.assertIn(1.0, weights["duckdb"])
        self.assertIn(0.5, weights["duck"])
        self.assertIn(0.5, weights["db"])
        self.assertIn(1.0, weights["async_io_engine"])
        self.assertIn(0.5, weights["engine"])
        for short_tok in ("c", "r", "go", "ml", "ai", "os", "db", "ir", "vm"):
            self.assertIn(short_tok, weights)
        for sw in ("the", "def", "return"):
            self.assertNotIn(sw, weights)

    def test_shingles_and_trie_phrase_gamma(self) -> None:
        tok = TechnicalTokenizer(["machine learning", "tiling window manager", "type theory"])
        out = tok.tokenize("I study type theory and tiling window manager with machine learning")
        out_map: dict[str, list[float]] = {}
        for t, w in out:
            out_map.setdefault(t, []).append(w)

        self.assertIn(1.35, out_map["machine learning"])
        self.assertIn(1.7, out_map["tiling window manager"])
        self.assertIn(1.35, out_map["type theory"])
        # Bigram & trigram shingles present
        self.assertIn("type theory", out_map)
        self.assertIn("machine learning", out_map)


class TestSignalEvidenceTypes(unittest.TestCase):
    def test_to_dict_and_from_any(self) -> None:
        ev = SignalEvidence(
            term="rust",
            raw_term="Rust",
            weight=3.5,
            source_type="workspace",
            source_detail="workspace:Cargo.toml",
            signal_kind="language",
        )
        as_dict = ev.to_dict()
        self.assertEqual(as_dict["term"], "rust")
        self.assertEqual(SignalEvidence.from_any(ev), ev)
        self.assertEqual(SignalEvidence.from_any(as_dict), ev)

        class _Duck:
            term = "rust"
            raw_term = "Rust"
            weight = 3.5
            source_type = "workspace"
            source_detail = "workspace:Cargo.toml"
            signal_kind = "language"

        self.assertEqual(SignalEvidence.from_any(_Duck()), ev)


class TestWorkspaceScanner(unittest.TestCase):
    def test_manifests_imports_dotfiles_and_extensions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text(
                '[project]\nname = "demo"\ndependencies = ["fastapi>=0.110", "numpy"]\n',
                encoding="utf-8",
            )
            (root / "Cargo.toml").write_text(
                '[package]\nname = "x"\n[dependencies]\ntokio = "1.0"\nserde = "1.0"\n',
                encoding="utf-8",
            )
            (root / "package.json").write_text(
                json.dumps({"dependencies": {"next": "15.0.0", "three": "0.170.0"}}),
                encoding="utf-8",
            )
            (root / "go.mod").write_text(
                "module example.com/app\nrequire github.com/gin-gonic/gin v1.9.1\n",
                encoding="utf-8",
            )
            (root / "CMakeLists.txt").write_text(
                "set(CMAKE_CXX_STANDARD 20)\nfind_package(Vulkan REQUIRED)\n",
                encoding="utf-8",
            )
            (root / "Dockerfile").write_text("FROM python:3.12-slim\n", encoding="utf-8")
            (root / "deployment.yaml").write_text(
                "apiVersion: apps/v1\nkind: Deployment\n", encoding="utf-8"
            )
            (root / "hyprland.conf").write_text("monitor=,preferred,auto,1\n", encoding="utf-8")
            (root / "main.py").write_text(
                "import os\nimport json\nimport polars\nfrom pydantic import BaseModel\n",
                encoding="utf-8",
            )
            (root / "helper.py").write_text("import sys\n", encoding="utf-8")
            (root / "lib.rs").write_text("use std::collections::HashMap;\nuse axum::Router;\n", encoding="utf-8")
            (root / "server.go").write_text('import (\n\t"fmt"\n\t"github.com/spf13/cobra"\n)\n', encoding="utf-8")
            (root / "ring.cpp").write_text(
                "#include <vector>\n#include <linux/io_uring.h>\n", encoding="utf-8"
            )
            (root / "app.ts").write_text(
                'import fs from "fs";\nimport { z } from "zod";\n', encoding="utf-8"
            )
            (root / "README.md").write_text(
                "# Distributed Consensus Lab\nUsing https://alice:s3cr3t@git.example.com/repo and eBPF.\n",
                encoding="utf-8",
            )

            evidences = WorkspaceScanner().scan(root)
            by_term: dict[str, list[SignalEvidence]] = {}
            for ev in evidences:
                by_term.setdefault(ev.term, []).append(ev)

            # Manifests w=3.5
            self.assertTrue(any(e.weight == 3.5 for e in by_term["fastapi"]))
            self.assertTrue(any(e.weight == 3.5 for e in by_term["tokio"]))
            self.assertTrue(any(e.weight == 3.5 for e in by_term["next.js"]))
            self.assertTrue(any(e.weight == 3.5 for e in by_term["gin"]))
            self.assertTrue(any(e.weight == 3.5 for e in by_term["c++20"]))
            self.assertTrue(any(e.weight == 3.5 for e in by_term["vulkan"]))

            # Containers / K8s / Dotfiles w=2.8
            self.assertTrue(any(e.weight == 2.8 for e in by_term["docker"]))
            self.assertTrue(any(e.weight == 2.8 for e in by_term["kubernetes"]))
            self.assertTrue(any(e.weight == 2.8 for e in by_term["hyprland"]))

            # Code imports w=2.0 (stdlib filtered!)
            self.assertTrue(any(e.weight == 2.0 for e in by_term["polars"]))
            self.assertTrue(any(e.weight == 2.0 for e in by_term["axum"]))
            self.assertTrue(any(e.weight == 2.0 for e in by_term["cobra"]))
            self.assertTrue(any(e.weight == 2.0 for e in by_term["io_uring"]))
            self.assertTrue(any(e.weight == 2.0 for e in by_term["zod"]))
            self.assertNotIn("os", by_term)
            self.assertNotIn("fmt", by_term)
            self.assertNotIn("vector", by_term)

            # Log-dampened extension (.py appears twice)
            expected_py_w = round(2.2 * (1.0 + math.log(2)), 4)
            self.assertTrue(any(math.isclose(e.weight, expected_py_w, rel_tol=1e-4) for e in by_term[".py"]))

            # Markdown heading 1.8x (1.5 * 1.8 = 2.7; 2-word ontology phrase = 2.7 * 1.35 = 3.645) and URL userinfo scrubbed
            self.assertTrue(any(math.isclose(e.weight, 2.7, rel_tol=1e-4) for e in by_term["distributed"]))
            self.assertTrue(any(math.isclose(e.weight, 3.645, rel_tol=1e-4) for e in by_term["distributed consensus"]))
            self.assertNotIn("s3cr3t", by_term)
            self.assertIn("ebpf", by_term)

    def test_secret_denylist_symlink_loop_binary_and_home_blocklist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp)
            ssh_dir = fake_home / ".ssh"
            ssh_dir.mkdir()
            (ssh_dir / "config").write_text("Host secret_ssh_host\n", encoding="utf-8")

            proj = fake_home / "project"
            proj.mkdir()
            # Secret files that must NEVER be read
            secret_names = [
                "cookies.json",
                "my_cookie_jar.txt",
                ".env",
                ".env.production",
                "app_secrets.yaml",
                "oauth_token.json",
                "aws_credentials.txt",
                "server.pem",
                "private.key",
                "cert.p12",
                "id_rsa",
                "id_ed25519.pub",
                ".netrc",
                ".npmrc",
                ".pypirc",
                "state.sqlite",
                "cache.db",
            ]
            for name in secret_names:
                (proj / name).write_text(f"unique_leak_marker_{name.replace('.', '_')}", encoding="utf-8")

            # Binary file with null byte
            (proj / "blob.txt").write_bytes(b"binary_leak_marker\x00\x01\x02")

            # Symlink loop
            loop_dir = proj / "sub"
            loop_dir.mkdir()
            (loop_dir / "loop").symlink_to(proj)
            (proj / "good.py").write_text("import scipy\n", encoding="utf-8")

            with mock.patch.dict(os.environ, {"HOME": str(fake_home)}):
                scanner = WorkspaceScanner()
                # Scanning $HOME directly skips ~/.ssh without crashing
                home_evs = scanner.scan(fake_home)
                home_terms = {e.term for e in home_evs}
                self.assertIn("scipy", home_terms)
                self.assertNotIn("secret_ssh_host", home_terms)
                self.assertNotIn("binary_leak_marker", home_terms)
                for t in home_terms:
                    self.assertNotIn("unique_leak_marker", t)

                # Direct scan of ~/.ssh or /etc is refused
                with self.assertRaises(PermissionError):
                    scanner.scan(ssh_dir)
                with self.assertRaises(PermissionError):
                    scanner.scan(Path("/etc"))

    def test_fedora_atomic_var_home_allowed_while_system_and_ssh_blocked(self) -> None:
        fake_var_home = Path("/var/home/alice")
        with mock.patch("pathlib.Path.home", return_value=fake_var_home):
            self.assertFalse(is_sensitive_path(Path("/var/home/alice/repos/myapp")))
            self.assertTrue(is_sensitive_path(Path("/var/home/alice/.ssh")))
            self.assertTrue(is_sensitive_path(Path("/var/home/alice/.config/reddit-curator")))
            self.assertTrue(is_sensitive_path(Path("/var/log")))
            self.assertTrue(is_sensitive_path(Path("/var/lib")))
            self.assertTrue(is_sensitive_path(Path("/etc")))

    def test_secret_glob_allows_source_code_and_blocks_secrets(self) -> None:
        self.assertFalse(is_secret_filename("tokenizer.py"))
        self.assertFalse(is_secret_filename("token_bucket.rs"))
        self.assertFalse(is_secret_filename("secret_handshake.ts"))
        self.assertTrue(is_secret_filename("api.token"))
        self.assertTrue(is_secret_filename("oauth_token.json"))
        self.assertTrue(is_secret_filename("app_secrets.yaml"))
        self.assertTrue(is_secret_filename(".env"))
        self.assertTrue(is_secret_filename(".env.local"))
        self.assertTrue(is_secret_filename("cookies.json"))
        self.assertTrue(is_secret_filename("aws_credentials.txt"))
        self.assertTrue(is_secret_filename("server.pem"))
        self.assertTrue(is_secret_filename("private.key"))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tokenizer.py").write_text("import regex\n", encoding="utf-8")
            (root / "api.token").write_text("import leaked_token_pkg\n", encoding="utf-8")
            terms = {e.term for e in WorkspaceScanner().scan(root)}
            self.assertIn("regex", terms)
            self.assertNotIn("leaked_token_pkg", terms)

    def test_js_subpaths_github_workflows_and_python_stopword_imports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wf = root / ".github" / "workflows"
            wf.mkdir(parents=True)
            (wf / "ci.yml").write_text("name: CI\non: [push]\n", encoding="utf-8")
            (root / "page.tsx").write_text(
                'import Image from "next/image";\n'
                'import { ReactQueryDevtools } from "@tanstack/react-query/devtools";\n'
                'import fs from "fs/promises";\n',
                encoding="utf-8",
            )
            (root / "app.py").write_text(
                "import utils\nimport main\nimport config\nfrom helpers import run\nimport httpx\n",
                encoding="utf-8",
            )
            terms = {e.term for e in WorkspaceScanner().scan(root)}
            self.assertIn("ci/cd", terms)
            self.assertIn("github-actions", terms)
            self.assertIn("next.js", terms)
            self.assertIn("tanstack-query", terms)
            self.assertNotIn("fs", terms)
            self.assertIn("httpx", terms)
            self.assertNotIn("utils", terms)
            self.assertNotIn("main", terms)
            self.assertNotIn("config", terms)
            self.assertNotIn("helpers", terms)


class TestGitHubMiner(unittest.TestCase):
    def test_budget_etag_304_rate_limit_and_stale_fallback(self) -> None:
        now = datetime.now(timezone.utc)
        recent = (now - timedelta(days=2)).isoformat()
        old = (now - timedelta(days=365)).isoformat()

        repos_payload = [
            {
                "name": "fast-rust",
                "full_name": "octo/fast-rust",
                "language": "Rust",
                "topics": ["ebpf", "io_uring"],
                "description": "Zero-copy kernel packet processor",
                "stargazers_count": 120,
                "fork": False,
                "pushed_at": recent,
            },
            {
                "name": "ml-notes",
                "full_name": "octo/ml-notes",
                "language": "Python",
                "topics": ["pytorch"],
                "description": "LLM experiments",
                "stargazers_count": 20,
                "fork": False,
                "pushed_at": recent,
            },
            {
                "name": "old-fork",
                "full_name": "octo/old-fork",
                "language": "Go",
                "topics": ["k8s"],
                "description": "Forked repo",
                "stargazers_count": 5,
                "fork": True,
                "pushed_at": old,
            },
            {
                "name": "web-ui",
                "full_name": "octo/web-ui",
                "language": "TypeScript",
                "topics": ["nextjs"],
                "description": "Frontend dashboard",
                "stargazers_count": 10,
                "fork": False,
                "pushed_at": recent,
            },
            {
                "name": "extra-repo",
                "full_name": "octo/extra-repo",
                "language": "C++",
                "topics": [],
                "description": "Should not fetch 4th readme",
                "stargazers_count": 1,
                "fork": False,
                "pushed_at": old,
            },
        ]
        starred_payload = [
            {
                "name": "neovim",
                "full_name": "neovim/neovim",
                "language": "Lua",
                "topics": ["lazy.nvim"],
                "description": "Vim-fork focused on extensibility",
                "stargazers_count": 80000,
                "fork": False,
                "pushed_at": recent,
            }
        ]
        readme_b64 = base64.b64encode(b"# Fast Rust\nBuilt with tokio and llama.cpp.\n").decode("ascii")

        calls: list[tuple[str, dict[str, str]]] = []
        mode = {"phase": "warm"}

        def fake_get(url: str, headers: dict[str, str] | None = None, timeout: float = 10.0) -> _FakeHTTPResponse:
            hdrs = headers or {}
            calls.append((url, hdrs))
            if mode["phase"] == "rate_limited":
                return _FakeHTTPResponse(429, {"message": "rate limit"}, {"X-RateLimit-Remaining": "0"})
            if mode["phase"] == "revalidate":
                if "If-None-Match" in hdrs:
                    return _FakeHTTPResponse(304, None, {"X-RateLimit-Remaining": "50"})
            if mode["phase"] == "low_quota":
                low_hdr = {"X-RateLimit-Remaining": "8"}
                if url.endswith("/users/octo"):
                    return _FakeHTTPResponse(200, {"bio": "Systems engineer", "company": "Acme"}, low_hdr)
                if "/repos?" in url:
                    return _FakeHTTPResponse(200, repos_payload, low_hdr)
                if "/starred?" in url:
                    return _FakeHTTPResponse(200, [], low_hdr)
                raise AssertionError(f"README fetched despite low quota: {url}")

            ok_hdr = {"ETag": '"v1"', "X-RateLimit-Remaining": "55"}
            if url.endswith("/users/octo"):
                return _FakeHTTPResponse(200, {"bio": "Systems & C++20 engineer", "company": ""}, ok_hdr)
            if "/repos?" in url:
                return _FakeHTTPResponse(200, repos_payload, ok_hdr)
            if "/starred?" in url:
                return _FakeHTTPResponse(200, starred_payload, ok_hdr)
            if url.endswith("/readme"):
                return _FakeHTTPResponse(200, {"encoding": "base64", "content": readme_b64}, ok_hdr)
            return _FakeHTTPResponse(404, {})

        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / ".curator_cache"
            session = mock.Mock()
            session.get.side_effect = fake_get

            miner = GitHubMiner(token="ghp_secret_must_never_be_cached", cache_dir=cache_dir, session=session)
            evs = miner.mine("octo")

            # <= 6 GETs: 3 base + 3 non-fork READMEs
            self.assertLessEqual(miner.last_request_count, 6)
            self.assertEqual(miner.last_request_count, 6)
            terms = {e.term for e in evs}
            self.assertIn("rust", terms)
            self.assertIn("ebpf", terms)
            self.assertIn("next.js", terms)
            self.assertIn("kubernetes", terms)  # k8s alias
            self.assertIn("llama.cpp", terms)  # from README

            # Verify permissions & zero token leakage on disk
            self.assertEqual(stat.S_IMODE(cache_dir.stat().st_mode), 0o700)
            cache_files = list(cache_dir.glob("gh_*.json"))
            self.assertEqual(len(cache_files), 6)
            for cf in cache_files:
                self.assertEqual(stat.S_IMODE(cf.stat().st_mode), 0o600)
                self.assertNotIn("ghp_secret_must_never_be_cached", cf.read_text(encoding="utf-8"))

            # Verify weight ordering: recent non-fork > old fork, starred discounted 0.35x
            w_non_fork = compute_repo_weight(repos_payload[0], now=now, starred=False)
            w_fork = compute_repo_weight(repos_payload[2], now=now, starred=False)
            self.assertGreater(w_non_fork, w_fork * 4.0)
            w_starred = compute_repo_weight(repos_payload[0], now=now, starred=True)
            self.assertAlmostEqual(w_starred, 0.35 * w_non_fork, places=6)

            # Second run: 304 Not Modified sends If-None-Match and reuses cache
            calls.clear()
            mode["phase"] = "revalidate"
            evs_304 = miner.mine("octo")
            self.assertTrue(all("If-None-Match" in hdrs for _, hdrs in calls))
            self.assertEqual({e.term for e in evs_304}, terms)

            # Third run: 429 falls back cleanly to stale cache
            mode["phase"] = "rate_limited"
            evs_429 = miner.mine("octo")
            self.assertEqual({e.term for e in evs_429}, terms)

            # Fresh cache with low quota (<=10) skips README calls
            low_cache = Path(tmp) / ".low_cache"
            low_miner = GitHubMiner(cache_dir=low_cache, session=session)
            calls.clear()
            mode["phase"] = "low_quota"
            low_miner.mine("octo")
            self.assertEqual(low_miner.last_request_count, 3)
            self.assertFalse(any(u.endswith("/readme") for u, _ in calls))

    def test_404_and_403_without_cache_surface_errors_and_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session_404 = mock.Mock()
            session_404.get.return_value = _FakeHTTPResponse(404, {"message": "Not Found"})
            miner_404 = GitHubMiner(cache_dir=Path(tmp) / "c404", session=session_404)
            with self.assertRaises(RuntimeError) as ctx_404:
                miner_404.mine("nonexistent-user-xyz")
            self.assertIn("404", str(ctx_404.exception))
            self.assertEqual(miner_404.last_request_count, 1)
            self.assertTrue(any("404" in w for w in miner_404.last_warnings))

            session_403 = mock.Mock()
            session_403.get.return_value = _FakeHTTPResponse(403, {"message": "rate limit"})
            miner_403 = GitHubMiner(cache_dir=Path(tmp) / "c403", session=session_403)
            with self.assertRaises(RuntimeError) as ctx_403:
                miner_403.mine("octo")
            self.assertIn("403", str(ctx_403.exception))
            self.assertTrue(any("403" in w for w in miner_403.last_warnings))

            # Optional /readme 404 records warning without failing mine()
            def partial_get(url: str, headers=None, timeout: float = 10.0):
                if url.endswith("/users/octo"):
                    return _FakeHTTPResponse(200, {"bio": "Rust dev"}, {"X-RateLimit-Remaining": "50"})
                if "/repos?" in url:
                    return _FakeHTTPResponse(
                        200,
                        [{"name": "proj", "language": "Rust", "fork": False}],
                        {"X-RateLimit-Remaining": "50"},
                    )
                if "/starred?" in url:
                    return _FakeHTTPResponse(200, [], {"X-RateLimit-Remaining": "50"})
                return _FakeHTTPResponse(404, {})

            session_partial = mock.Mock()
            session_partial.get.side_effect = partial_get
            miner_ok = GitHubMiner(cache_dir=Path(tmp) / "cpart", session=session_partial)
            evs = miner_ok.mine("octo")
            self.assertTrue(any(e.term == "rust" for e in evs))
            self.assertTrue(any("404" in w for w in miner_ok.last_warnings))


class TestResumeParser(unittest.TestCase):
    def test_pure_pdf_flatedecode_kerning_escapes_and_linebreaks(self) -> None:
        stream_txt = (
            b"BT\n"
            b"/F1 12 Tf\n"
            b"1 0 0 1 72 720 Tm\n"
            b"[(Python) -220 (C++)] TJ\n"
            b"0 -18 Td\n"
            b"(\\101\\111) Tj\n"
            b"T*\n"
            b"<6e6f64652e6a73> Tj\n"
            b"ET\n"
        )
        compressed = zlib.compress(stream_txt)
        pdf = _build_synthetic_pdf(
            [
                (1, b"<< /Type /Catalog /Pages 2 0 R >>"),
                (2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"),
                (3, b"<< /Type /Page /Parent 2 0 R /Contents 4 0 R >>"),
                (4, f"<< /Length {len(compressed)} /Filter /FlateDecode >>\nstream\n".encode("ascii") + compressed + b"\nendstream"),
            ]
        )
        parser = ResumeParser()
        extracted = parser.extract_pdf_text_pure(pdf)
        self.assertIn("Python C++", extracted)
        self.assertNotIn("PythonC++", extracted)
        self.assertIn("AI", extracted)
        self.assertIn("node.js", extracted)

    def test_pure_pdf_objstm_png_predictor_and_tounicode_cmap(self) -> None:
        # CMap maps glyph <0001> -> 'R', <0002> -> 'u', <0003> -> 's', <0004> -> 't' via bfchar & bfrange
        cmap_data = (
            b"/CIDInit /ProcSet findresource begin\n"
            b"begincmap\n"
            b"1 beginbfchar\n"
            b"<0001> <0052>\n"
            b"endbfchar\n"
            b"1 beginbfrange\n"
            b"<0002> <0004> <0075>\n"
            b"endbfrange\n"
            b"endcmap\n"
        )
        # Wait: 'u'=0x75, 'v'=0x76, 'w'=0x77, whereas 's'=0x73, 't'=0x74! Let's map <0002>->'u', <0003>-><0004> via array form for 's','t'
        cmap_data = (
            b"/CIDInit /ProcSet findresource begin\n"
            b"begincmap\n"
            b"1 beginbfchar\n"
            b"<0001> <0052>\n"
            b"endbfchar\n"
            b"1 beginbfrange\n"
            b"<0002> <0004> [<0075> <0073> <0074>]\n"
            b"endbfrange\n"
            b"1 beginbfrange\n"
            b"<0010> <0012> <0041>\n"
            b"endbfrange\n"
            b"endcmap\n"
        )
        cmap_comp = zlib.compress(cmap_data)

        # Embedded objects inside /Type /ObjStm (obj 10 = Font dict, obj 11 = Page dict)
        emb_10 = b"<< /Type /Font /Subtype /Type0 /ToUnicode 5 0 R >>"
        emb_11 = (
            b"<< /Type /Page /Parent 2 0 R "
            b"/Resources << /Font << /F1 10 0 R >> >> "
            b"/Contents 6 0 R >>"
        )
        header = f"10 0 11 {len(emb_10) + 1} ".encode("ascii")
        objstm_raw = header + emb_10 + b" " + emb_11
        # Encode ObjStm with PNG Up predictor (Predictor=12, Columns=8)
        objstm_pred = _apply_png_up_predictor(objstm_raw, columns=8)
        objstm_comp = zlib.compress(objstm_pred)

        # Content stream writes <0001000200030004> ("Rust") and <001000110012> ("ABC")
        content_raw = b"BT /F1 12 Tf <0001000200030004> Tj 0 -14 Td <001000110012> Tj ET"
        content_comp = zlib.compress(content_raw)

        pdf = _build_synthetic_pdf(
            [
                (1, b"<< /Type /Catalog /Pages 2 0 R >>"),
                (2, b"<< /Type /Pages /Kids [11 0 R] /Count 1 >>"),
                (5, f"<< /Length {len(cmap_comp)} /Filter /FlateDecode >>\nstream\n".encode("ascii") + cmap_comp + b"\nendstream"),
                (6, f"<< /Length {len(content_comp)} /Filter /FlateDecode >>\nstream\n".encode("ascii") + content_comp + b"\nendstream"),
                (
                    7,
                    f"<< /Type /ObjStm /N 2 /First {len(header)} /Length {len(objstm_comp)} "
                    f"/Filter /FlateDecode /DecodeParms << /Predictor 12 /Columns 8 >> >>\nstream\n".encode("ascii")
                    + objstm_comp
                    + b"\nendstream",
                ),
            ]
        )
        extracted = ResumeParser().extract_pdf_text_pure(pdf)
        self.assertIn("Rust", extracted)
        self.assertIn("ABC", extracted)

    def test_encrypted_pdf_raises_error(self) -> None:
        encrypted_pdf = _build_synthetic_pdf(
            [(1, b"<< /Type /Catalog >>"), (9, b"<< /Filter /Standard /V 2 >>")],
            trailer_extra=b"/Encrypt 9 0 R ",
        )
        parser = ResumeParser()
        with self.assertRaises(EncryptedPDFError):
            parser.extract_pdf_text_pure(encrypted_pdf)
        with self.assertRaises(EncryptedPDFError):
            parser.parse_bytes(encrypted_pdf, filename="secret_resume.pdf")

    def test_parse_bytes_sections_pii_scrubbing_and_size_cap(self) -> None:
        resume_md = (
            "Email: candidate@example.com\n"
            "Phone: +1 (415) 555-0199\n"
            "Portfolio: https://user:secretpass@portfolio.example.dev/projects\n"
            "\n"
            "## Summary\n"
            "Systems programmer focused on compilers.\n"
            "\n"
            "## Skills\n"
            "Rust, C++20, eBPF, llama.cpp\n"
            "\n"
            "## Projects\n"
            "Built io_uring storage engine.\n"
            "\n"
            "## Experience\n"
            "Engineered Kubernetes telemetry pipelines.\n"
            "\n"
            "## Education\n"
            "B.S. Computer Science\n"
            "\n"
            "## Certifications\n"
            "CKA\n"
        ).encode("utf-8")

        parser = ResumeParser()
        evs = parser.parse_bytes(resume_md, filename="resume.md")
        weights_by_term: dict[str, float] = {}
        for ev in evs:
            weights_by_term[ev.term] = max(weights_by_term.get(ev.term, 0.0), ev.weight)
            self.assertNotIn("candidate@example.com", ev.source_detail)
            self.assertNotIn("secretpass", ev.source_detail)

        self.assertEqual(weights_by_term["rust"], 3.5)
        self.assertEqual(weights_by_term["c++20"], 3.5)
        self.assertEqual(weights_by_term["llama.cpp"], 3.5)
        self.assertEqual(weights_by_term["io_uring"], 2.8)
        self.assertEqual(weights_by_term["kubernetes"], 2.4)
        self.assertEqual(weights_by_term["compilers"], 1.5)
        self.assertEqual(weights_by_term["cka"], 1.6)
        self.assertNotIn("candidate", weights_by_term)
        self.assertNotIn("secretpass", weights_by_term)
        self.assertNotIn("415", weights_by_term)

        # Size limit (>15 MB)
        oversized = b"x" * (MAX_RESUME_BYTES + 1)
        with self.assertRaises(ValueError):
            parser.parse_bytes(oversized, filename="huge.txt")

    def test_parse_bytes_pdf_pipeline_fallback_to_pure_python(self) -> None:
        stream_txt = b"BT /F1 12 Tf (Skills:) Tj 0 -16 Td (Kubernetes PostgreSQL) Tj ET"
        comp = zlib.compress(stream_txt)
        pdf = _build_synthetic_pdf(
            [
                (1, b"<< /Type /Catalog /Pages 2 0 R >>"),
                (2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"),
                (3, b"<< /Type /Page /Parent 2 0 R /Contents 4 0 R >>"),
                (4, f"<< /Length {len(comp)} /Filter /FlateDecode >>\nstream\n".encode("ascii") + comp + b"\nendstream"),
            ]
        )
        parser = ResumeParser()
        with mock.patch("rcurator.sources.resume.os.path.isfile", return_value=False):
            evs = parser.parse_bytes(pdf, filename="cv.pdf")
        terms = {e.term: e.weight for e in evs}
        self.assertEqual(terms.get("kubernetes"), 3.5)
        self.assertEqual(terms.get("postgresql"), 3.5)

    def test_parse_tex_file_and_512kb_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tex_path = Path(tmp) / "resume.tex"
            tex_path.write_text(
                "\\section{Skills}\n\\textbf{Rust}, \\href{https://u:p@example.com}{Wayland}, C++23\n",
                encoding="utf-8",
            )
            evs = ResumeParser().parse_file(tex_path)
            terms = {e.term: e.weight for e in evs}
            self.assertEqual(terms.get("rust"), 3.5)
            self.assertEqual(terms.get("wayland"), 3.5)
            self.assertEqual(terms.get("c++23"), 3.5)

            # 512 KB cap in WorkspaceScanner: token after 512 KB is truncated
            big_md = Path(tmp) / "NOTES.md"
            big_md.write_text(("rust " * 120000) + "\nunreachable_tail_marker\n", encoding="utf-8")
            ws_terms = {e.term for e in WorkspaceScanner().scan(Path(tmp))}
            self.assertIn("rust", ws_terms)
            self.assertNotIn("unreachable_tail_marker", ws_terms)

    def test_cmap_giant_bfrange_is_capped_at_4096(self) -> None:
        cmap = (
            b"/CIDInit /ProcSet findresource begin\n"
            b"begincmap\n"
            b"1 beginbfrange\n"
            b"<0000> <FFFFFFFF> <0020>\n"
            b"endbfrange\n"
            b"endcmap\n"
        )
        table = _parse_tounicode_cmap(cmap)
        self.assertLessEqual(len(table), MAX_BFRANGE_ENTRIES)
        self.assertEqual(len(table), MAX_BFRANGE_ENTRIES)

    def test_pdftotext_uses_shutil_which_and_defers_pure_pdf_parse(self) -> None:
        fake_pdf = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n"
        parser = ResumeParser()
        fake_proc = mock.Mock(returncode=0, stdout=b"Skills\nRust C++20\n", stderr=b"")
        with (
            mock.patch("rcurator.sources.resume.shutil.which", return_value="/custom/bin/pdftotext") as m_which,
            mock.patch("rcurator.sources.resume.os.path.isfile", return_value=True),
            mock.patch("rcurator.sources.resume.os.access", return_value=True),
            mock.patch("rcurator.sources.resume.subprocess.run", return_value=fake_proc) as m_run,
            mock.patch("rcurator.sources.resume._parse_pdf_objects") as m_pure,
        ):
            evs = parser.parse_bytes(fake_pdf, filename="cv.pdf")
        m_which.assert_called_once_with("pdftotext")
        self.assertEqual(m_run.call_args[0][0][0], "/custom/bin/pdftotext")
        m_pure.assert_not_called()
        self.assertTrue(any(e.term == "rust" for e in evs))

    def test_strip_latex_removes_begin_end_env_names(self) -> None:
        tex = (
            "\\begin{document}\n"
            "\\section{Skills}\n"
            "\\begin{itemize}\n"
            "\\item Rust and eBPF\n"
            "\\end{itemize}\n"
            "\\end{document}\n"
        ).encode("utf-8")
        evs = ResumeParser().parse_bytes(tex, filename="cv.tex")
        terms = {e.term for e in evs}
        self.assertIn("rust", terms)
        self.assertIn("ebpf", terms)
        self.assertNotIn("document", terms)
        self.assertNotIn("itemize", terms)


if __name__ == "__main__":
    unittest.main()
