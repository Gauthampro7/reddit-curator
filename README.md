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
* **Local Web Studio (`studio` / `ui`):** Zero-build, dark-themed local dashboard on `127.0.0.1:8420` featuring **Easy Mode (default 3-step setup)**, **Advanced Mode (5-tab IR/graph lab)**, **Open Custom JSON** import, an **About & Philosophy** research explorer, and `Host`/`Origin`/CSRF/traversal security guards.

---

## Philosophy: Reverse Passive Marketing & Controlled Engagement

Commercial social platforms shape subconscious attention through **mere exposure** and **incidental exposure** while people assume ads and algorithms do not affect them (**third-person effect**). Meanwhile, rigid app blockers and "digital detoxes" routinely trigger **psychological reactance**, **ironic rebound**, or null well-being gains.

`reddit-curator` flips passive marketing inward: instead of banning leisure feeds or surrendering to algorithmic recommendations, it engineers **controlled engagement** via:
1. **Following Feed Lock:** Deterministic `sort=BEST&feed=following` navigation bypassing engagement-maximizing home feeds.
2. **Velocity-Weighted 40 / 35 / 25 Golden Ratio:** Interleaves `40% Aesthetic & Leisure`, `35% Cognitive Curiosity`, and `25% Domain Priming` by expected daily post velocity so casual breaks stay enjoyable while reinforcing mental availability.
3. **Tier-2 Deep-Focus Custom Feeds:** Routes high-volume technical megasubs into private multireddits so they never drown out smaller, high-signal communities.

Read the full methodology and 17 peer-reviewed citations in [`TECHNIQUE.md`](TECHNIQUE.md) or inside Web Studio's **About & Philosophy** view.

---

## Quickstart

### 1. Prerequisites & Editable Install

Python 3.10+ with `requests` and `websockets` (Fedora / PEP 668 friendly):

```bash
pip install -r requirements.txt
# Optional editable install exposing the `reddit-curator` command from this repo checkout
# (keeps repo-root `presets/` and `curator_data/` directly accessible alongside `rcurator`):
pip install -e .
```

### 2. Inspect Environment & Safe Quickstart

Running `python3 reddit_curator.py` with **zero arguments** safely prints a Quickstart guide and grouped `--help` without mutating your Reddit account or requiring CDP. Run `doctor` anytime to check Python/dependencies, ontology & presets, detected RPM/Flatpak browsers, CDP port reachability, Reddit auth, and PDF extractors:

```bash
# Safe zero-arg Quickstart + grouped help
python3 reddit_curator.py

# Environment & CDP/auth diagnostics (human or JSON)
python3 reddit_curator.py doctor
python3 reddit_curator.py doctor --port 9222 --agent
```

### 3. Launch the Local Web Studio (Recommended)

Start the browser-based curation dashboard, **Easy Mode** 3-step setup (default) or **Advanced Mode** 5-tab lab, **Open Custom JSON** manifest importer, **About & Philosophy** explorer, diet health charts, and sync console:

```bash
python3 reddit_curator.py studio
# Equivalent flag invocation:
python3 reddit_curator.py --studio
```

Open `http://127.0.0.1:8420` (opens automatically unless `--no-open-browser` is passed). In **Easy Mode**, pick one of the 17 topic packs (or click **Open Custom JSON**), inspect the 3-pillar breakdown, and either run an offline **Preview Dry-Run** or connect your browser to apply changes. Toggle **Advanced** in the top bar for multi-source auto-curation (workspace, GitHub, resume PDF), BM25F/PPR explainability, convex preset mixing, entropy audits, and snapshot rollback.

### 4. Launch a CDP-Enabled Browser (Fedora / Linux RPM & Flatpak)

Auto-detect and launch Chrome, Chromium, or Brave (including Flatpak `com.brave.Browser` / `com.google.Chrome`) with isolated remote debugging on port `9222`:

```bash
python3 reddit_curator.py --launch-browser
```

Log in to Reddit in that browser window once.

### 5. Apply a Curated Preset or Generate One Automatically

```bash
# List all 17 built-in domain presets (or filter with --query)
python3 reddit_curator.py --list-presets
python3 reddit_curator.py --list-presets --query rust

# Inspect a preset, compare two presets side-by-side, or validate a manifest
python3 reddit_curator.py --preset-info systems_engineering
python3 reddit_curator.py --compare-presets systems_engineering,rust_and_compilers
python3 reddit_curator.py --preset systems_engineering --validate

# Export normalized preset JSON without syncing
python3 reddit_curator.py --preset systems_engineering --output out.json

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

### 6. Audit, Diff, Snapshot & Rollback

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
| `sync` *(default)* | Standard CLI execution mode when flags are provided (bare zero-arg `reddit_curator.py` prints Quickstart + help safely). |
| `doctor` | Run read-only environment, browser, CDP, Reddit auth, and PDF extractor diagnostics. |
| `studio` / `ui` | Start the local Web Studio HTTP server (`127.0.0.1:8420`). |
| `wizard` | Launch the interactive guided terminal curation wizard. |

### Flags

| Flag | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `--port` | `int` | `9222` | Chrome DevTools Protocol remote debugging port. |
| `--manifest` | `Path` | — | Path to a custom v1/v2 manifest JSON file. |
| `--preset` | `str` | — | Load a built-in preset by name from `presets/`. |
| `--list-presets` | `bool` | `false` | Print all available presets (optionally filtered by `--query`) and exit. |
| `--preset-info` | `str` | — | Display full subreddits, custom feeds, and Diet Health audit for a preset. |
| `--compare-presets` | `str` | — | Compare two comma-separated presets (`A,B`) side-by-side. |
| `--validate` | `bool` | `false` | Validate `--manifest` or `--preset` schema and Diet Health. |
| `--version` | `bool` | — | Show package version and exit. |
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

## Offline Evaluation & College Demo (Zero Reddit Login Needed)

Evaluators can inspect the entire methodology, curation engine, Web Studio UI, and automated test suite completely offline with no Reddit credentials or live browser required:

```bash
# 1. Run the full offline unit & integration test suite (zero external test runner required)
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v

# 2. Explore all 17 built-in presets and run an offline Golden Ratio / entropy audit
python3 reddit_curator.py --list-presets
python3 reddit_curator.py --preset systems_engineering --audit

# 3. Preview an offline dry-run plan and multi-source deterministic manifest generation
python3 reddit_curator.py --preset systems_engineering --dry-run
python3 reddit_curator.py --from-workspace . --query "systems distributed databases" --audit

# 4. Launch Web Studio to inspect Easy/Advanced modes, Open Custom JSON,
#    offline Dry-Run preview, and the About & Philosophy 17-source explorer
python3 reddit_curator.py studio
```

* [`TECHNIQUE.md`](TECHNIQUE.md) — Reverse Passive Marketing, Controlled Engagement, 40/35/25 Golden Ratio & 17 verified peer-reviewed citations
* [`ALGORITHMS.md`](ALGORITHMS.md) — Formal mathematical specifications for tokenization, BM25F/TF-IDF, Bipartite PPR, velocity optimization, and auditing
* [`ARCHITECTURE.md`](ARCHITECTURE.md) — Modular package design, CDP/OAuth hardening, Mermaid diagrams, and Web Studio security model

---

## License

[MIT](LICENSE) — Copyright (c) 2026 Gautham Saiju
