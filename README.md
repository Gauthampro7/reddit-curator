# reddit-curator

> **Deterministic Cognitive Information Diet Engine, Multi-Source Auto-Curator, Local Web Studio & CDP Sync Suite for Reddit.**

`reddit-curator` v2.0 transforms Reddit into a structured, passive cognitive-priming engine without third-party AI/LLM APIs or Reddit developer app credentials. It combines **classical Information Retrieval (BM25F + TF-IDF)**, **Bipartite Personalized PageRank** over a **292-community knowledge graph**, and a **Velocity-Weighted Golden Ratio Constrained Optimizer** to engineer balanced information diets—then synchronizes subscriptions, private Custom Feeds, snapshots, and the **Following** feed lock directly via Chrome DevTools Protocol (CDP).

---

## Key Capabilities

* **Zero External AI / Zero Extra Pip Dependencies:** Runs out-of-the-box on Python 3.10+ (PEP 668 / Fedora 45 friendly) using only Python stdlib + `requests` + `websockets`.
* **Multi-Source Deterministic Auto-Preset Generator:**
  * **Local Workspace Scanner (`--from-workspace`):** Extracts signals from dependency manifests (`pyproject.toml`, `Cargo.toml`, `package.json`, `go.mod`, `CMakeLists.txt`), containers/K8s/dotfiles, code imports (Python `ast`, Rust, Go, C/C++, TS/JS), file extensions, and READMEs while guarding against symlinks and secrets.
  * **GitHub Profile & Repo Miner (`--from-github`):** Mines bio, repositories (recency-decayed & star-weighted), topics, and READMEs within a $\le 6$-request unauthenticated budget backed by ETag disk caching.
  * **3-Tier Local Resume Parser (`--from-resume`):** Parses `.pdf`, `.md`, `.txt`, and `.tex` locally (`pypdf` $\to$ Poppler `pdftotext` $\to$ built-in pure-Python `zlib`/CMap/kerning fallback) with section multipliers (`Skills`, `Projects`/`Research`, `Experience`, `Education`).
  * **Free-Text Query & Convex Preset Mixer (`--query`, `--mix`):** Synthesize or blend multiple presets via convex weights while preserving velocity balance.
* **Velocity-Weighted Golden Ratio Optimizer (40 / 35 / 25):** Optimizes **expected daily impression share** across `aesthetic_leisure` (40%), `cognitive_curiosity` (35%), and `domain_priming` (25%), enforcing a $50\%$ single-subreddit dominance cap and routing high-velocity technical communities into Tier-2 private `custom_feeds`.
* **Information Diet Health Auditor (`--audit`, `--audit-live`):** Computes Total Variation Drift $\Delta_{\text{GR}}$, Golden Ratio Fidelity Score ($0\text{–}100$), normalized Shannon domain entropy, Gini velocity concentration, and actionable rebalancing suggestions.
* **Hardened CDP Runtime, Snapshots & Rollback:** Correlated WebSocket JSON-RPC, open-Shadow-DOM **Following** feed lock, full `/subreddits/mine/subscriber` pagination, rate-limit backoff, timestamped `.curator_snapshots/` backups, `--diff` previews, opt-in `--prune`, and 1-command `--rollback`.
* **Local Web Studio (`studio` / `ui`):** Zero-build, dark-themed local dashboard on `127.0.0.1:8420` protected by `Host`/`Origin` checks, `X-Curator-Token` CSRF protection, and traversal/sensitive-dir guards.

---

## Quickstart

### 1. Prerequisites

Python 3.10+ with `requests` and `websockets` (already installed on standard environments or via `pip install -r requirements.txt`):

```bash
pip install -r requirements.txt
```

### 2. Launch the Local Web Studio (Recommended)

Start the browser-based curation dashboard, preset explorer, resume uploader, diet health charts, and sync console:

```bash
python3 reddit_curator.py studio
# Equivalent flag invocation:
python3 reddit_curator.py --studio
```

Open `http://127.0.0.1:8420` (opens automatically unless `--no-open-browser` is passed).

### 3. Launch a CDP-Enabled Browser (Fedora / Linux RPM & Flatpak)

Auto-detect and launch Chrome, Chromium, or Brave (including Flatpak `com.brave.Browser` / `com.google.Chrome`) with isolated remote debugging on port `9222`:

```bash
python3 reddit_curator.py --launch-browser
```

Log in to Reddit in that browser window once.

### 4. Apply a Curated Preset or Generate One Automatically

```bash
# List all 17 built-in domain presets
python3 reddit_curator.py --list-presets

# Preview a preset diff without mutating your account (works offline too)
python3 reddit_curator.py --preset systems_engineering --dry-run

# Apply a preset end-to-end (snapshot + anchor & priming subscribe + custom feeds + Following lock)
python3 reddit_curator.py --preset systems_engineering --one-shot

# Generate a deterministic manifest from your local code, GitHub, and resume PDF
python3 reddit_curator.py \
  --from-workspace ~/projects/my-repo \
  --from-github octocat \
  --from-resume ~/Documents/resume.pdf \
  --ratio 40,35,25 \
  --output generated_manifest.json

# Blend presets via Convex Mixer and audit diet health
python3 reddit_curator.py --mix systems_engineering:0.6,rust_and_compilers:0.4 --audit

# Interactive guided terminal wizard
python3 reddit_curator.py wizard
```

### 5. Audit, Diff, Snapshot & Rollback

```bash
# Audit live Reddit subscriptions against the Golden Ratio
python3 reddit_curator.py --audit-live

# Preview exact subscription and custom-feed changes before syncing
python3 reddit_curator.py --preset rust_and_compilers --diff

# Sync while safely pruning unmanaged subreddits (preserving allowlisted ones)
python3 reddit_curator.py --preset rust_and_compilers --one-shot --prune --keep Fedora,unixporn

# Create a manual snapshot, list snapshots, or roll back to the latest snapshot
python3 reddit_curator.py --snapshot
python3 reddit_curator.py --list-snapshots
python3 reddit_curator.py --rollback latest
```

---

## Preset Library (17 Domains & 292-Subreddit Knowledge Graph)

Every preset in [`presets/`](presets/) is validated against [`manifest.example.json`](manifest.example.json) and linked to [`curator_data/subreddit_ontology.json`](curator_data/subreddit_ontology.json) (`292` vetted communities: `56` `aesthetic_leisure`, `46` `cognitive_curiosity`, `190` `domain_priming`, `1,415` directed graph edges).

| Preset Key | Title | Primary Focus | Custom Feed |
| :--- | :--- | :--- | :--- |
| `systems_engineering` | Systems Engineering & Infrastructure | Linux kernel, eBPF, storage engines, PostgreSQL, DuckDB, Podman | `systems_core` |
| `machine_learning` | Machine Learning & AI Engineering | Deep learning, open-weights LLM inference, NLP, MLOps, MLIR/LLVM | `ai_research` |
| `cognitive_science` | Cognitive Science & Decision Theory | Cognitive science, behavioral economics, neuroscience, rationality | `cog_science` |
| `cybersecurity_infosec` | Cybersecurity, Reverse Engineering & InfoSec | Netsec, Ghidra/binary RE, blue/red team, AppSec, DFIR, crypto | `infosec_core` |
| `rust_and_compilers` | Rust, Compilers & Programming Language Theory | Rust ownership, LLVM/MLIR, PLT, Zig, Haskell, OCaml, WebAssembly | `rust_compilers` |
| `data_engineering_analytics` | Data Engineering, Lakehouses & Analytics | DuckDB, Polars, PostgreSQL, ClickHouse, dbt, Spark, Kafka | `data_lakehouse` |
| `distributed_cloud_native` | Distributed Systems & Cloud-Native Infrastructure | Kubernetes, Podman, SRE, Terraform/OpenTofu, OpenTelemetry, Go | `cloud_native_ops` |
| `frontend_ui_craft` | Frontend Architecture, UI Craft & Design Systems | TypeScript, CSS Grid, React, Svelte, Design Systems, WCAG a11y | `ui_engineering` |
| `embedded_hardware_fpga` | Embedded Systems, Hardware & FPGA Design | Bare-metal RTOS, SystemVerilog FPGAs, RISC-V, PCBs, DSP, robotics | `hardware_silicon` |
| `algorithms_mathematics` | Algorithms, Theoretical CS & Mathematics | Algorithms, competitive programming, statistics, crypto, Quantum | `math_algo_theory` |
| `quant_finance_economics` | Quantitative Finance, Econometrics & Markets | Stochastic pricing, systematic backtesting, econometrics, equity | `quant_research` |
| `game_engines_graphics` | Game Engines, Real-Time Graphics & Shaders | Vulkan, path tracing, GLSL/WGSL shaders, Bevy, Godot, C++ | `graphics_engines` |
| `linux_ricing_opensource` | Linux Ricing, Terminal Craft & Open Source | Fedora, NixOS, Arch, Hyprland, Neovim, CLI, Podman, self-hosting | `linux_craft` |
| `biotech_computational_sci` | Biotech, Bioinformatics & Computational Science | Bioinformatics, genomics, computational chemistry, Julia, R | `comp_bio_sci` |
| `product_entrepreneurship` | Product Craft, Engineering Leadership & Startups | B2B SaaS, engineering management, product discovery, docs-as-code | `product_builders` |
| `academic_research_epistemology` | Academic Research, Epistemology & Polymath Inquiry | Historiography, philosophy of science, epistemology, linguistics | `scholarly_inquiry` |
| `fullstack_web_systems` | Fullstack Web Systems & API Architecture | FastAPI, Python, TypeScript, Next.js, PostgreSQL, Elixir, HTMX | `fullstack_core` |

---

## CLI Reference

### Positional Subcommands

| Command | Description |
| :--- | :--- |
| `sync` *(default)* | Standard CLI execution mode (preserved when invoking flags directly, e.g., `--preset ... --one-shot`). |
| `studio` / `ui` | Start the local Web Studio HTTP server (`127.0.0.1:8420`). |
| `wizard` | Launch the interactive guided terminal curation wizard. |

### Flags

| Flag | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `--port` | `int` | `9222` | Chrome DevTools Protocol remote debugging port. |
| `--manifest` | `Path` | — | Path to a custom v1/v2 manifest JSON file. |
| `--preset` | `str` | — | Load a built-in preset by name from `presets/`. |
| `--list-presets` | `bool` | `false` | Print all available presets and exit. |
| `--one-shot` | `bool` | `false` | Execute full snapshot, subscription sync, custom feed upsert, and Following lock. |
| `--agent` | `bool` | `false` | Suppress logs and emit structured JSON to `stdout`. |
| `--dry-run` | `bool` | `false` | Preview mutations without modifying Reddit (supports offline dry-run if CDP is closed). |
| `--verify` | `bool` | `false` | Audit live account subscriptions, custom feeds, and Following tab without mutating. |
| `--from-workspace` | `Path` | — | Mine signals deterministically from a local project/workspace directory. |
| `--from-github` | `str` | — | Mine signals from a GitHub username's public profile, repos, and stars. |
| `--github-token` | `str` | — | Optional GitHub PAT (or set `GITHUB_TOKEN`) to raise API rate limits. |
| `--from-resume` | `Path` | — | Parse a local `.pdf`, `.md`, `.txt`, or `.tex` resume into weighted skill signals. |
| `--query` | `str` | — | Free-text technical/research topic query for deterministic curation. |
| `--mix` | `str` | — | Blend presets with convex weights (e.g., `systems_engineering:0.6,rust_and_compilers:0.4`). |
| `--ratio` | `str` | `"40,35,25"` | Target Golden Ratio impression percentages (`aesthetic,cognitive,domain`). |
| `--tier1-count` | `int` | `18` | Target number of Tier-1 communities in the Following feed ($\ge 3$). |
| `--discover-online` | `bool` | `false` | Optionally enrich ontology candidates via Reddit `/subreddits/search`. |
| `--output` | `Path` | — | Write generated or mixed manifest JSON to disk. |
| `--audit` | `bool` | `false` | Run Diet Health audit on the selected/generated manifest. |
| `--audit-live` | `bool` | `false` | Fetch live Reddit subscriptions via CDP and run Diet Health audit. |
| `--diff` | `bool` | `false` | Compute and display subscription/custom-feed diff against live Reddit state. |
| `--prune` | `bool` | `false` | Opt-in removal of unmanaged subscriptions during sync (auto-creates pre-sync snapshot). |
| `--keep` | `str` | `""` | Comma-separated subreddit allowlist preserved even when `--prune` is enabled. |
| `--snapshot` | `bool` | `false` | Capture a timestamped backup snapshot in `.curator_snapshots/` and exit. |
| `--list-snapshots` | `bool` | `false` | List saved backup snapshots from `.curator_snapshots/`. |
| `--rollback` | `str` | — | Restore subscriptions and custom feeds from `"latest"` or a specific snapshot ID. |
| `--launch-browser` | `bool` | `false` | Detect and launch Chrome/Chromium/Brave (RPM or Flatpak) with CDP enabled. |
| `--browser` | `str` | — | Preferred browser binary or Flatpak ID for `--launch-browser`. |
| `--studio` | `bool` | `false` | Start the local Web Studio HTTP server. |
| `--studio-port` | `int` | `8420` | TCP port for the local Web Studio server (bound strictly to `127.0.0.1`). |
| `--no-open-browser` | `bool` | `false` | Do not auto-open the Web Studio URL in your browser on startup. |

---

## Testing & Further Reading

Run the deterministic test suite with zero external test runner requirements:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

* [`TECHNIQUE.md`](TECHNIQUE.md) — Cognitive psychology foundations (Availability Heuristic, Context Interleaving, Golden Ratio)
* [`ALGORITHMS.md`](ALGORITHMS.md) — Formal mathematical specifications for tokenization, BM25F/TF-IDF, Bipartite PPR, velocity optimization, and auditing
* [`ARCHITECTURE.md`](ARCHITECTURE.md) — Modular package design, CDP/OAuth hardening, Mermaid diagrams, and Web Studio security model

---

## License

MIT
