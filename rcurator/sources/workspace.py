"""Safe local workspace signal scanner with symlink, secret, and resource guards."""

from __future__ import annotations

import ast
import fnmatch
import json
import math
import os
import re
import stat
import sys
import tomllib
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

from rcurator.nlp.tokenizer import (
    TechnicalTokenizer,
    canonicalize_token,
    load_ontology_phrases,
)
from rcurator.sources.types import SignalEvidence

MAX_FILE_BYTES = 512 * 1024
MAX_FILES = 5000

SYSTEM_SENSITIVE_DIRS: tuple[Path, ...] = tuple(
    Path(p) for p in ("/etc", "/proc", "/sys", "/dev", "/run", "/root", "/var", "/boot")
)

HOME_SENSITIVE_REL_DIRS: tuple[str, ...] = (
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

SKIP_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "target",
        "dist",
        "build",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        "vendor",
        ".curator_cache",
        ".curator_snapshots",
    }
)

SECRET_FILENAME_PATTERNS: tuple[str, ...] = (
    "cookies.json",
    "*cookie*",
    ".env",
    ".env.*",
    "*secret*",
    "*token*",
    "*credential*",
    "*.pem",
    "*.key",
    "*.p12",
    "id_rsa*",
    "id_ed25519*",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "*.sqlite*",
    "*.db",
)

BINARY_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".ico",
        ".bmp",
        ".tiff",
        ".tif",
        ".pdf",
        ".zip",
        ".tar",
        ".gz",
        ".xz",
        ".bz2",
        ".zst",
        ".7z",
        ".wasm",
        ".so",
        ".dll",
        ".exe",
        ".o",
        ".a",
        ".pyc",
        ".pyo",
        ".class",
        ".jar",
        ".ttf",
        ".otf",
        ".woff",
        ".woff2",
        ".mp3",
        ".mp4",
        ".mov",
        ".avi",
        ".mkv",
        ".flac",
        ".wav",
        ".blend",
        ".stl",
        ".fbx",
        ".gltf",
        ".parquet",
        ".gguf",
        ".safetensors",
        ".pcap",
        ".elf",
        ".bin",
    }
)

EXTENSION_LANGUAGE_MAP: dict[str, str] = {
    ".py": "python",
    ".rs": "rust",
    ".go": "go",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cpp": "c++",
    ".cc": "c++",
    ".cxx": "c++",
    ".hpp": "c++",
    ".c": "c",
    ".h": "c",
    ".cs": "c#",
    ".fs": "f#",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".zig": "zig",
    ".hs": "haskell",
    ".ml": "ocaml",
    ".mli": "ocaml",
    ".nix": "nix",
    ".lua": "lua",
    ".sh": "bash",
    ".sql": "sql",
    ".tf": "terraform",
    ".rb": "ruby",
    ".jl": "julia",
    ".r": "r",
    ".rmd": "r",
    ".sv": "systemverilog",
    ".vhd": "vhdl",
    ".vhdl": "vhdl",
    ".cu": "cuda",
    ".glsl": "glsl",
    ".wgsl": "wgsl",
    ".hlsl": "hlsl",
    ".svelte": "svelte",
    ".vue": "vue.js",
    ".clj": "clojure",
    ".cljs": "clojure",
    ".ex": "elixir",
    ".exs": "elixir",
    ".erl": "erlang",
    ".scala": "scala",
    ".lean": "lean",
    ".agda": "agda",
    ".tex": "latex",
    ".typ": "typst",
}

DOTFILE_INDICATORS: dict[str, tuple[str, ...]] = {
    "hyprland.conf": ("hyprland", "hyprland.conf", "linux"),
    "hyprlock.conf": ("hyprland", "hyprlock.conf", "linux"),
    "hypridle.conf": ("hyprland", "hypridle.conf", "linux"),
    "waybar.css": ("waybar", "waybar.css", "linux"),
    "polybar.ini": ("polybar", "polybar.ini", "linux"),
    "init.lua": ("neovim", "init.lua", "lua"),
    "lazy-lock.json": ("neovim", "lazy.nvim"),
    ".tmux.conf": ("tmux", ".tmux.conf"),
    ".xresources": ("ricing", ".xresources", "linux"),
    ".zshrc": ("zsh", ".zshrc"),
    ".bashrc": ("bash", ".bashrc"),
    ".vimrc": ("vim", ".vimrc"),
    "flake.nix": ("nix", "nixos", "flake.nix"),
    "configuration.nix": ("nixos", "configuration.nix"),
    "alacritty.toml": ("alacritty", "linux"),
    "kitty.conf": ("kitty", "linux"),
}

_URL_USERINFO_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s@]+@")
_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?")
_CMAKE_FIND_RE = re.compile(r"find_package\(\s*([A-Za-z0-9_+-]+)", re.IGNORECASE)
_CMAKE_STD_RE = re.compile(r"CMAKE_CXX_STANDARD\s+(17|20|23)", re.IGNORECASE)
_RUST_USE_RE = re.compile(r"^\s*(?:pub\s+)?use\s+([A-Za-z_][A-Za-z0-9_]*)(?:::|;)", re.MULTILINE)
_GO_SINGLE_IMPORT_RE = re.compile(r'^\s*import\s+(?:[A-Za-z_][A-Za-z0-9_]*\s+)?"([^"]+)"', re.MULTILINE)
_GO_BLOCK_IMPORT_RE = re.compile(r"import\s*\((.*?)\)", re.DOTALL)
_GO_QUOTED_RE = re.compile(r'"([^"]+)"')
_CPP_INCLUDE_RE = re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]', re.MULTILINE)
_JS_IMPORT_RE = re.compile(
    r"""(?:import\s+.*?\s+from\s+|import\s+|require\(\s*)['"]([^'"]+)['"]""",
    re.MULTILINE,
)
_MD_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", re.MULTILINE)

_RUST_STD_CRATES = frozenset({"std", "core", "alloc", "crate", "self", "super"})
_GO_STD_PREFIXES = frozenset(
    {
        "archive",
        "bufio",
        "bytes",
        "cmp",
        "compress",
        "container",
        "context",
        "crypto",
        "database",
        "debug",
        "embed",
        "encoding",
        "errors",
        "expvar",
        "flag",
        "fmt",
        "go",
        "hash",
        "html",
        "image",
        "index",
        "io",
        "iter",
        "log",
        "maps",
        "math",
        "mime",
        "net",
        "os",
        "path",
        "plugin",
        "reflect",
        "regexp",
        "runtime",
        "slices",
        "sort",
        "strconv",
        "strings",
        "sync",
        "syscall",
        "testing",
        "text",
        "time",
        "unicode",
        "unsafe",
    }
)
_C_CPP_STD_HEADERS = frozenset(
    {
        "algorithm",
        "any",
        "array",
        "assert.h",
        "atomic",
        "bit",
        "bitset",
        "cassert",
        "cctype",
        "cerrno",
        "chrono",
        "climits",
        "cmath",
        "compare",
        "complex",
        "concepts",
        "condition_variable",
        "coroutine",
        "cstdint",
        "cstdio",
        "cstdlib",
        "cstring",
        "ctime",
        "ctype.h",
        "deque",
        "errno.h",
        "exception",
        "filesystem",
        "format",
        "forward_list",
        "fstream",
        "functional",
        "future",
        "initializer_list",
        "iomanip",
        "ios",
        "iosfwd",
        "iostream",
        "istream",
        "iterator",
        "limits",
        "limits.h",
        "list",
        "locale",
        "map",
        "math.h",
        "memory",
        "mutex",
        "new",
        "numbers",
        "numeric",
        "optional",
        "ostream",
        "queue",
        "random",
        "ranges",
        "ratio",
        "regex",
        "set",
        "shared_mutex",
        "signal.h",
        "span",
        "sstream",
        "stack",
        "stdbool.h",
        "stddef.h",
        "stdexcept",
        "stdint.h",
        "stdio.h",
        "stdlib.h",
        "string",
        "string.h",
        "string_view",
        "system_error",
        "thread",
        "time.h",
        "tuple",
        "type_traits",
        "typeinfo",
        "unistd.h",
        "unordered_map",
        "unordered_set",
        "utility",
        "valarray",
        "variant",
        "vector",
    }
)
_NODE_BUILTINS = frozenset(
    {
        "assert",
        "buffer",
        "child_process",
        "cluster",
        "crypto",
        "dgram",
        "dns",
        "events",
        "fs",
        "http",
        "http2",
        "https",
        "module",
        "net",
        "os",
        "path",
        "perf_hooks",
        "process",
        "querystring",
        "readline",
        "stream",
        "timers",
        "tls",
        "tty",
        "url",
        "util",
        "v8",
        "vm",
        "worker_threads",
        "zlib",
    }
)
_JS_PACKAGE_ALIASES = {
    "next": "next.js",
    "vue": "vue.js",
    "nuxt": "nuxt.js",
    "three": "three.js",
    "d3": "d3.js",
    "tailwindcss": "tailwind",
    "@tanstack/react-query": "tanstack-query",
}


def scrub_url_userinfo(text: str) -> str:
    """Remove embedded user:password@ credentials from URLs."""
    return _URL_USERINFO_RE.sub(r"\1", text)


def _sensitive_dirs() -> tuple[Path, ...]:
    home = Path.home().resolve()
    home_dirs = tuple((home / rel).resolve() for rel in HOME_SENSITIVE_REL_DIRS)
    return SYSTEM_SENSITIVE_DIRS + home_dirs


def is_sensitive_path(path: Path, blocked_dirs: tuple[Path, ...] | None = None) -> bool:
    """Return True if path is equal to or inside a sensitive system or $HOME directory."""
    resolved = path.resolve()
    dirs = blocked_dirs if blocked_dirs is not None else _sensitive_dirs()
    for blocked in dirs:
        if resolved == blocked or resolved.is_relative_to(blocked):
            return True
    return False


def is_secret_filename(filename: str) -> bool:
    """Return True if filename matches the strict secret/credential denylist."""
    low = filename.lower()
    return any(fnmatch.fnmatch(low, pattern) for pattern in SECRET_FILENAME_PATTERNS)


def _read_text_safe(fpath: Path) -> str | None:
    """Read up to MAX_FILE_BYTES as UTF-8 text; return None if binary or unreadable."""
    try:
        with fpath.open("rb") as fh:
            raw = fh.read(MAX_FILE_BYTES)
    except OSError:
        return None
    if b"\x00" in raw:
        return None
    return scrub_url_userinfo(raw.decode("utf-8", errors="replace"))


class WorkspaceScanner:
    """Deterministic multi-language workspace scanner emitting SignalEvidence."""

    def __init__(self, known_phrases: Iterable[str] | None = None) -> None:
        phrases = list(known_phrases) if known_phrases is not None else load_ontology_phrases()
        self._tokenizer = TechnicalTokenizer(phrases)

    def scan(self, path: Path | str) -> list[SignalEvidence]:
        """Scan a local directory safely and return extracted SignalEvidence items."""
        target = Path(path)
        if target.is_symlink():
            raise PermissionError(f"Refusing to scan symlink root: {target}")
        resolved = target.resolve()
        blocked_dirs = _sensitive_dirs()
        if is_sensitive_path(resolved, blocked_dirs):
            raise PermissionError(f"Refusing to scan sensitive directory: {resolved}")
        if not resolved.exists():
            raise FileNotFoundError(f"Workspace path does not exist: {resolved}")
        if not resolved.is_dir():
            raise ValueError(f"Workspace path is not a directory: {resolved}")

        evidences: list[SignalEvidence] = []
        ext_counts: Counter[str] = Counter()
        visited_inodes: set[tuple[int, int]] = set()

        root_stat = os.lstat(resolved)
        visited_inodes.add((root_stat.st_dev, root_stat.st_ino))
        files_seen = 0

        for dirpath, dirnames, filenames in os.walk(resolved, topdown=True, followlinks=False):
            kept_dirs: list[str] = []
            for dname in sorted(dirnames):
                if dname in SKIP_DIR_NAMES or dname.startswith(".curator_"):
                    continue
                dpath = Path(dirpath) / dname
                try:
                    st = os.lstat(dpath)
                except OSError:
                    continue
                if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                    continue
                if is_sensitive_path(dpath, blocked_dirs):
                    continue
                inode = (st.st_dev, st.st_ino)
                if inode in visited_inodes:
                    continue
                visited_inodes.add(inode)
                kept_dirs.append(dname)
            dirnames[:] = kept_dirs

            for fname in sorted(filenames):
                if files_seen >= MAX_FILES:
                    break
                fpath = Path(dirpath) / fname
                try:
                    st = os.lstat(fpath)
                except OSError:
                    continue
                if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                    continue
                if is_secret_filename(fname):
                    continue

                files_seen += 1
                rel_path = scrub_url_userinfo(fpath.relative_to(resolved).as_posix())
                self._record_extension(fname, ext_counts)
                self._process_file(fpath, fname, rel_path, evidences)

            if files_seen >= MAX_FILES:
                break

        self._emit_extension_evidences(ext_counts, evidences)
        return evidences

    @staticmethod
    def _record_extension(fname: str, ext_counts: Counter[str]) -> None:
        low = fname.lower()
        if low.endswith(".bpf.c"):
            ext_counts[".bpf.c"] += 1
            return
        if low.endswith(".d.ts"):
            ext_counts[".d.ts"] += 1
            return
        if low.endswith(".spec.ts"):
            ext_counts[".spec.ts"] += 1
            return
        suffix = Path(low).suffix
        if suffix:
            ext_counts[suffix] += 1

    @staticmethod
    def _emit_extension_evidences(
        ext_counts: Counter[str], evidences: list[SignalEvidence]
    ) -> None:
        for ext, count in sorted(ext_counts.items()):
            weight = round(2.2 * (1.0 + math.log(count)), 4)
            evidences.append(
                SignalEvidence(
                    term=ext,
                    raw_term=ext,
                    weight=weight,
                    source_type="workspace",
                    source_detail=f"ext:{ext}(n={count})",
                    signal_kind="file_indicator",
                )
            )
            lang = EXTENSION_LANGUAGE_MAP.get(ext)
            if lang:
                evidences.append(
                    SignalEvidence(
                        term=lang,
                        raw_term=ext,
                        weight=weight,
                        source_type="workspace",
                        source_detail=f"ext:{ext}(n={count})",
                        signal_kind="language",
                    )
                )

    def _process_file(
        self,
        fpath: Path,
        fname: str,
        rel_path: str,
        evidences: list[SignalEvidence],
    ) -> None:
        low_name = fname.lower()
        low_rel = rel_path.lower()
        detail = f"workspace:{rel_path}"

        # Dotfiles / ricing path indicators (even before binary check)
        self._extract_dotfile_signals(low_name, low_rel, detail, evidences)

        if Path(low_name).suffix in BINARY_EXTENSIONS:
            return

        if low_name == "pyproject.toml":
            text = _read_text_safe(fpath)
            if text is not None:
                self._extract_pyproject(text, detail, evidences)
            return

        if low_name.startswith("requirements") and low_name.endswith(".txt"):
            text = _read_text_safe(fpath)
            if text is not None:
                self._extract_requirements(text, detail, evidences)
            return

        if low_name == "cargo.toml":
            text = _read_text_safe(fpath)
            if text is not None:
                self._extract_cargo(text, detail, evidences)
            return

        if low_name == "package.json":
            text = _read_text_safe(fpath)
            if text is not None:
                self._extract_package_json(text, detail, evidences)
            return

        if low_name == "go.mod":
            text = _read_text_safe(fpath)
            if text is not None:
                self._extract_go_mod(text, detail, evidences)
            return

        if low_name == "cmakelists.txt":
            text = _read_text_safe(fpath)
            if text is not None:
                self._extract_cmake(text, detail, evidences)
            return

        if (
            low_name in {"containerfile", "dockerfile"}
            or low_name.startswith("dockerfile.")
            or low_name.endswith(".container")
        ):
            text = _read_text_safe(fpath)
            if text is not None:
                self._extract_containerfile(text, low_name, detail, evidences)
            return

        if low_name in {
            "docker-compose.yml",
            "docker-compose.yaml",
            "compose.yml",
            "compose.yaml",
        }:
            self._emit(evidences, "docker", low_name, 2.8, detail, "ecosystem")
            self._emit(evidences, "docker-compose", low_name, 2.8, detail, "ecosystem")
            self._emit(evidences, low_name, low_name, 2.8, detail, "file_indicator")
            return

        if low_name.endswith((".yml", ".yaml")):
            text = _read_text_safe(fpath)
            if text is not None:
                self._extract_yaml_infra(text, low_name, detail, evidences)
            return

        suffix = Path(low_name).suffix
        if suffix == ".py":
            text = _read_text_safe(fpath)
            if text is not None:
                self._extract_python_imports(text, detail, evidences)
            return

        if suffix == ".rs":
            text = _read_text_safe(fpath)
            if text is not None:
                self._extract_rust_imports(text, detail, evidences)
            return

        if suffix == ".go":
            text = _read_text_safe(fpath)
            if text is not None:
                self._extract_go_imports(text, detail, evidences)
            return

        if suffix in {".c", ".h", ".cpp", ".cc", ".cxx", ".hpp"}:
            text = _read_text_safe(fpath)
            if text is not None:
                self._extract_cpp_includes(text, detail, evidences)
            return

        if suffix in {".ts", ".tsx", ".js", ".jsx", ".mjs"}:
            text = _read_text_safe(fpath)
            if text is not None:
                self._extract_js_imports(text, detail, evidences)
            return

        if suffix in {".md", ".markdown", ".rst", ".txt"} or low_name.startswith("readme"):
            text = _read_text_safe(fpath)
            if text is not None:
                self._extract_markdown_doc(text, detail, evidences)

    @staticmethod
    def _emit(
        evidences: list[SignalEvidence],
        term: str,
        raw_term: str,
        weight: float,
        detail: str,
        kind: str,
    ) -> None:
        canon = canonicalize_token(term)
        if not canon:
            return
        evidences.append(
            SignalEvidence(
                term=canon,
                raw_term=raw_term,
                weight=round(weight, 4),
                source_type="workspace",
                source_detail=detail,
                signal_kind=kind,
            )
        )

    def _extract_dotfile_signals(
        self, low_name: str, low_rel: str, detail: str, evidences: list[SignalEvidence]
    ) -> None:
        if low_name in DOTFILE_INDICATORS:
            for term in DOTFILE_INDICATORS[low_name]:
                kind = "file_indicator" if "." in term and term == low_name else "ecosystem"
                self._emit(evidences, term, low_name, 2.8, detail, kind)

        for tool in ("hyprland", "sway", "i3", "waybar", "kitty", "nvim", "neovim", "tmux"):
            if f"/{tool}/" in f"/{low_rel}" or low_name.startswith(tool):
                mapped = "neovim" if tool == "nvim" else tool
                self._emit(evidences, mapped, low_rel, 2.8, detail, "ecosystem")
        if "lazy.nvim" in low_rel:
            self._emit(evidences, "lazy.nvim", "lazy.nvim", 2.8, detail, "ecosystem")

    def _extract_pyproject(
        self, text: str, detail: str, evidences: list[SignalEvidence]
    ) -> None:
        self._emit(evidences, "pyproject.toml", "pyproject.toml", 3.5, detail, "file_indicator")
        self._emit(evidences, "python", "pyproject.toml", 3.5, detail, "language")
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return

        deps: list[str] = []
        project = data.get("project", {})
        if isinstance(project, dict):
            raw_deps = project.get("dependencies", [])
            if isinstance(raw_deps, list):
                deps.extend(str(d) for d in raw_deps)
            opt = project.get("optional-dependencies", {})
            if isinstance(opt, dict):
                for group in opt.values():
                    if isinstance(group, list):
                        deps.extend(str(d) for d in group)

        build_sys = data.get("build-system", {})
        if isinstance(build_sys, dict):
            reqs = build_sys.get("requires", [])
            if isinstance(reqs, list):
                deps.extend(str(r) for r in reqs)

        poetry = data.get("tool", {}).get("poetry", {})
        if isinstance(poetry, dict):
            for section in ("dependencies", "dev-dependencies"):
                table = poetry.get(section, {})
                if isinstance(table, dict):
                    deps.extend(str(k) for k in table.keys() if str(k).lower() != "python")

        seen: set[str] = set()
        for dep in deps:
            m = _REQ_NAME_RE.match(dep)
            if not m:
                continue
            pkg = canonicalize_token(m.group(1))
            if pkg and pkg not in seen:
                seen.add(pkg)
                self._emit(evidences, pkg, m.group(1), 3.5, detail, "ecosystem")

    def _extract_requirements(
        self, text: str, detail: str, evidences: list[SignalEvidence]
    ) -> None:
        self._emit(evidences, "requirements.txt", "requirements.txt", 3.5, detail, "file_indicator")
        self._emit(evidences, "python", "requirements.txt", 3.5, detail, "language")
        seen: set[str] = set()
        for line in text.splitlines():
            stripped = line.split("#", 1)[0].strip()
            if not stripped or stripped.startswith(("-", "git+", "http://", "https://")):
                continue
            m = _REQ_NAME_RE.match(stripped)
            if not m:
                continue
            pkg = canonicalize_token(m.group(1))
            if pkg and pkg not in seen:
                seen.add(pkg)
                self._emit(evidences, pkg, m.group(1), 3.5, detail, "ecosystem")

    def _extract_cargo(
        self, text: str, detail: str, evidences: list[SignalEvidence]
    ) -> None:
        self._emit(evidences, "cargo.toml", "Cargo.toml", 3.5, detail, "file_indicator")
        self._emit(evidences, "rust", "Cargo.toml", 3.5, detail, "language")
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return
        seen: set[str] = set()
        for sec in ("dependencies", "dev-dependencies", "build-dependencies"):
            table = data.get(sec, {})
            if isinstance(table, dict):
                for crate in table.keys():
                    pkg = canonicalize_token(str(crate))
                    if pkg and pkg not in seen:
                        seen.add(pkg)
                        self._emit(evidences, pkg, str(crate), 3.5, detail, "ecosystem")

    def _extract_package_json(
        self, text: str, detail: str, evidences: list[SignalEvidence]
    ) -> None:
        self._emit(evidences, "package.json", "package.json", 3.5, detail, "file_indicator")
        self._emit(evidences, "node.js", "package.json", 3.5, detail, "ecosystem")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return
        if not isinstance(data, dict):
            return
        seen: set[str] = set()
        for sec in ("dependencies", "devDependencies", "peerDependencies"):
            table = data.get(sec, {})
            if isinstance(table, dict):
                for raw_pkg in table.keys():
                    raw_s = str(raw_pkg)
                    mapped = _JS_PACKAGE_ALIASES.get(raw_s.lower(), raw_s)
                    if mapped.startswith("@") and "/" in mapped:
                        mapped = mapped.split("/", 1)[1]
                    pkg = canonicalize_token(mapped)
                    if pkg and pkg not in seen:
                        seen.add(pkg)
                        self._emit(evidences, pkg, raw_s, 3.5, detail, "ecosystem")

    def _extract_go_mod(
        self, text: str, detail: str, evidences: list[SignalEvidence]
    ) -> None:
        self._emit(evidences, "go.mod", "go.mod", 3.5, detail, "file_indicator")
        self._emit(evidences, "go", "go.mod", 3.5, detail, "language")
        seen: set[str] = set()
        in_require = False
        for line in text.splitlines():
            clean = line.split("//", 1)[0].strip()
            if not clean:
                continue
            if clean.startswith("require ("):
                in_require = True
                continue
            if in_require and clean == ")":
                in_require = False
                continue
            if clean.startswith("require "):
                clean = clean[len("require "):].strip()
            elif not in_require:
                continue
            mod_path = clean.split()[0]
            parts = [p for p in mod_path.split("/") if p and not re.match(r"^v\d+$", p)]
            if parts:
                pkg = canonicalize_token(parts[-1])
                if pkg and pkg not in seen:
                    seen.add(pkg)
                    self._emit(evidences, pkg, mod_path, 3.5, detail, "ecosystem")

    def _extract_cmake(
        self, text: str, detail: str, evidences: list[SignalEvidence]
    ) -> None:
        self._emit(evidences, "cmakelists.txt", "CMakeLists.txt", 3.5, detail, "file_indicator")
        self._emit(evidences, "cmake", "CMakeLists.txt", 3.5, detail, "ecosystem")
        self._emit(evidences, "c++", "CMakeLists.txt", 3.5, detail, "language")
        for m in _CMAKE_STD_RE.finditer(text):
            self._emit(evidences, f"c++{m.group(1)}", m.group(0), 3.5, detail, "language")
        seen: set[str] = set()
        for m in _CMAKE_FIND_RE.finditer(text):
            pkg = canonicalize_token(m.group(1))
            if pkg and pkg not in seen:
                seen.add(pkg)
                self._emit(evidences, pkg, m.group(1), 3.5, detail, "ecosystem")

    def _extract_containerfile(
        self, text: str, low_name: str, detail: str, evidences: list[SignalEvidence]
    ) -> None:
        self._emit(evidences, "docker", low_name, 2.8, detail, "ecosystem")
        self._emit(evidences, "containers", low_name, 2.8, detail, "ecosystem")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.upper().startswith("FROM "):
                parts = stripped.split()
                if len(parts) >= 2:
                    image = parts[1].split(":", 1)[0].split("@", 1)[0]
                    base = image.rsplit("/", 1)[-1]
                    if base and base.lower() != "scratch":
                        self._emit(evidences, base, parts[1], 2.8, detail, "ecosystem")

    def _extract_yaml_infra(
        self, text: str, low_name: str, detail: str, evidences: list[SignalEvidence]
    ) -> None:
        k8s_names = {"deployment.yaml", "kustomization.yaml", "chart.yaml", "helmfile.yaml"}
        has_k8s_keys = ("apiVersion:" in text and "kind:" in text)
        if low_name in k8s_names or has_k8s_keys:
            self._emit(evidences, "kubernetes", low_name, 2.8, detail, "ecosystem")
            self._emit(evidences, low_name, low_name, 2.8, detail, "file_indicator")
        elif low_name in {".gitlab-ci.yml", "ci.yml", "cloudbuild.yaml"}:
            self._emit(evidences, "ci/cd", low_name, 2.8, detail, "ecosystem")

    def _extract_python_imports(
        self, text: str, detail: str, evidences: list[SignalEvidence]
    ) -> None:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return
        stdlib = sys.stdlib_module_names
        seen: set[str] = set()
        for node in ast.walk(tree):
            mod_name: str | None = None
            if isinstance(node, ast.Import):
                for alias in node.names:
                    mod_name = alias.name.split(".", 1)[0]
                    if mod_name and mod_name not in stdlib and mod_name not in seen:
                        seen.add(mod_name)
                        self._emit(evidences, mod_name, alias.name, 2.0, detail, "ecosystem")
            elif isinstance(node, ast.ImportFrom):
                if node.level > 0 or not node.module:
                    continue
                mod_name = node.module.split(".", 1)[0]
                if mod_name and mod_name not in stdlib and mod_name not in seen:
                    seen.add(mod_name)
                    self._emit(evidences, mod_name, node.module, 2.0, detail, "ecosystem")

    def _extract_rust_imports(
        self, text: str, detail: str, evidences: list[SignalEvidence]
    ) -> None:
        seen: set[str] = set()
        for m in _RUST_USE_RE.finditer(text):
            crate = m.group(1)
            if crate.lower() in _RUST_STD_CRATES:
                continue
            if crate not in seen:
                seen.add(crate)
                self._emit(evidences, crate, crate, 2.0, detail, "ecosystem")

    def _extract_go_imports(
        self, text: str, detail: str, evidences: list[SignalEvidence]
    ) -> None:
        paths: list[str] = _GO_SINGLE_IMPORT_RE.findall(text)
        for block in _GO_BLOCK_IMPORT_RE.findall(text):
            paths.extend(_GO_QUOTED_RE.findall(block))
        seen: set[str] = set()
        for mod_path in paths:
            top = mod_path.split("/", 1)[0]
            if top in _GO_STD_PREFIXES and "." not in top:
                continue
            parts = [p for p in mod_path.split("/") if p and not re.match(r"^v\d+$", p)]
            if not parts:
                continue
            pkg = parts[-1]
            if pkg not in seen:
                seen.add(pkg)
                self._emit(evidences, pkg, mod_path, 2.0, detail, "ecosystem")

    def _extract_cpp_includes(
        self, text: str, detail: str, evidences: list[SignalEvidence]
    ) -> None:
        seen: set[str] = set()
        for m in _CPP_INCLUDE_RE.finditer(text):
            hdr = m.group(1).strip()
            low_hdr = hdr.lower()
            if low_hdr in _C_CPP_STD_HEADERS:
                continue
            if "io_uring" in low_hdr:
                term = "io_uring"
            elif "cuda" in low_hdr:
                term = "cuda"
            elif "vulkan" in low_hdr:
                term = "vulkan"
            elif "sqlite" in low_hdr:
                term = "sqlite"
            else:
                term = low_hdr.split("/", 1)[0].removesuffix(".h").removesuffix(".hpp")
            if term and term not in seen:
                seen.add(term)
                self._emit(evidences, term, hdr, 2.0, detail, "ecosystem")

    def _extract_js_imports(
        self, text: str, detail: str, evidences: list[SignalEvidence]
    ) -> None:
        seen: set[str] = set()
        for m in _JS_IMPORT_RE.finditer(text):
            spec = m.group(1).strip()
            if not spec or spec.startswith((".", "/", "node:")):
                continue
            if spec in _NODE_BUILTINS:
                continue
            mapped = _JS_PACKAGE_ALIASES.get(spec.lower())
            if mapped is None:
                if spec.startswith("@") and "/" in spec:
                    mapped = spec.split("/", 2)[1]
                else:
                    mapped = spec.split("/", 1)[0]
            if mapped and mapped not in seen:
                seen.add(mapped)
                self._emit(evidences, mapped, spec, 2.0, detail, "ecosystem")

    def _extract_markdown_doc(
        self, text: str, detail: str, evidences: list[SignalEvidence]
    ) -> None:
        term_weights: dict[str, float] = {}
        heading_spans: list[tuple[int, int]] = []
        for hm in _MD_HEADING_RE.finditer(text):
            heading_spans.append(hm.span())
            for term, tok_w in self._tokenizer.tokenize(hm.group(1)):
                term_weights[term] = max(term_weights.get(term, 0.0), 1.5 * 1.8 * tok_w)

        body_parts: list[str] = []
        last = 0
        for start, end in heading_spans:
            if start > last:
                body_parts.append(text[last:start])
            last = end
        if last < len(text):
            body_parts.append(text[last:])
        body_text = "\n".join(body_parts)
        for term, tok_w in self._tokenizer.tokenize(body_text):
            term_weights[term] = max(term_weights.get(term, 0.0), 1.5 * tok_w)

        for term, weight in term_weights.items():
            evidences.append(
                SignalEvidence(
                    term=term,
                    raw_term=term,
                    weight=round(weight, 4),
                    source_type="workspace",
                    source_detail=detail,
                    signal_kind="keyword",
                )
            )
