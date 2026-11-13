# `reddit-curator` v2.0 — Deterministic Algorithms & Mathematical Foundations

> **Design Mandate:** Zero third-party AI or LLM APIs. Every extraction, scoring, graph propagation, constrained optimization, auditing, and preset-mixing pipeline in `reddit-curator` executes locally in deterministic Python using classical Information Retrieval (IR), Natural Language Processing (NLP), graph theory, and combinatorial optimization.

---

## 1. Why Deterministic Classical IR Beats External LLMs Here

| Property | External LLM API | `reddit-curator` Classical Engine |
| :--- | :--- | :--- |
| **Determinism & Reproducibility** | Stochastic temperature sampling; model weights silently drift over time | Bit-for-bit reproducible given the same inputs and `curator_data/subreddit_ontology.json` |
| **Privacy & Data Sovereignty** | Uploads personal resumes, local repositories, and private dotfiles to remote servers | 100% offline local execution; PII scrubbed from provenance logs |
| **Latency & Cost** | Seconds per call, API keys, token billing, rate-limit failures | Sub-10 ms local scoring across 292 communities and 1,415 directed edges |
| **Explainability** | Post-hoc rationalizations that cannot prove mathematical attribution | Exact per-term BM25F contribution, PPR graph bridge paths, and velocity impression math |
| **Constraint Satisfaction** | Fails reliably at numerical simplex constraints (e.g., velocity-weighted 40/35/25) | Guaranteed Total Variation bound, dominance cap $\le 50\%$, and Tier-2 overflow routing |

---

## 2. Knowledge Graph & Ontology Representation

The deterministic knowledge base in `curator_data/subreddit_ontology.json` (`schema: "reddit-curator.ontology.v1"`) models curated Reddit communities as a heterogeneous bipartite + co-occurrence graph:

$$\mathcal{G} = \left(\mathcal{V}_{\text{sub}} \cup \mathcal{V}_{\text{concept}},\; \mathcal{E}_{\text{cs}} \cup \mathcal{E}_{\text{ss}}\right)$$

* **Subreddit Nodes ($\mathcal{V}_{\text{sub}}$, $|\mathcal{V}_{\text{sub}}| = 292$):**
  * `aesthetic_leisure`: $56$ communities (median velocity $35.0$ posts/day, range $[4.0, 140.0]$)
  * `cognitive_curiosity`: $46$ communities (median velocity $17.0$ posts/day, range $[5.0, 160.0]$)
  * `domain_priming`: $190$ communities across $17$ technical/academic domains (median velocity $20.0$ posts/day, range $[4.0, 180.0]$)
* **Subreddit Co-Occurrence Edges ($\mathcal{E}_{\text{ss}}$):** $1{,}415$ directed `related_subreddits` links ($3 \le \deg^+(s) \le 6$, $\min_s \deg^-(s) \ge 2$, mean out-degree $4.85$), symmetrized into undirected random-walk transitions.
* **Subreddit Metadata:** Each node $s \in \mathcal{V}_{\text{sub}}$ carries canonical casing (`name`), Golden Ratio pillar (`category`), domain tags (`domains`), post velocity (`estimated_daily_posts` $v_s$), quality prior (`signal_density` $\rho_s \in (0, 1]$), and weighted textual fields (`keywords`, `ecosystems`, `file_indicators`, `description`).

---

## 3. Stage 1 — Technical Lexer & Ontology Prefix Trie (`rcurator/nlp/tokenizer.py`)

Standard NLP tokenizers strip punctuation (`C++` $\to$ `C`, `.NET` $\to$ `NET`, `node.js` $\to$ `node` + `js`), destroying technical specificity. `TechnicalTokenizer` preserves domain tokens in $O(|T|)$ linear time:

1. **Punctuated Symbol Preservation:** Preserves internal `.`, `/`, `&`, `<...>`, leading `.` in `.net`, and trailing `++`, `++20`, `++23`, `#` so tokens such as `.net`, `async/await`, `c#`, `c++`, `c++20`, `c++23`, `ci/cd`, `core.async`, `data.table`, `gnu/linux`, `http/3`, `lazy.nvim`, `llama.cpp`, `m&a`, `node.js`, `next.js`, `vue.js`, `nuxt.js`, `d3.js`, `three.js`, `pub/sub`, `sdr++`, `span<t>`, `tcp/ip`, `io_uring`, and `scikit-learn` remain intact.
2. **Canonical Alias Table:** Maps surface variants to canonical ontology tokens without stripping preserved dots:
   * `nodejs` $\to$ `node.js`, `nextjs` $\to$ `next.js`, `vuejs` $\to$ `vue.js`, `nuxtjs` $\to$ `nuxt.js`, `threejs` $\to$ `three.js`, `d3js` $\to$ `d3.js`
   * `dotnet` $\to$ `.net`, `csharp` $\to$ `c#`, `fsharp` $\to$ `f#`, `cpp`/`cxx` $\to$ `c++`, `llamacpp` $\to$ `llama.cpp`
   * `k8s`/`k3s` $\to$ `kubernetes`, `golang` $\to$ `go`, `postgres` $\to$ `postgresql`, `py` $\to$ `python`, `rs` $\to$ `rust`, `ts` $\to$ `typescript`, `js` $\to$ `javascript`, `tf` $\to$ `tensorflow`, `sklearn` $\to$ `scikit-learn`
3. **Dual Identifier Emission & Protected Short Tokens:** Emits each full normalized identifier at weight $1.0$ plus CamelCase/snake_case sub-tokens at weight $0.5$ (e.g., `FastAPI` $\to$ `fastapi` ($1.0$), `fast` ($0.5$), `api` ($0.5$)). Filters English and boilerplate code stopwords while protecting short technical identifiers $\{ \text{c}, \text{r}, \text{go}, \text{ml}, \text{ai}, \text{os}, \text{db}, \text{ir}, \text{vm} \}$.
4. **N-Grams & Prefix Trie Phrase Matching:** Generates $n \in \{1, 2, 3\}$ contiguous token shingles and matches multi-word ontology phrases via a compiled Prefix Trie with length specificity multiplier:

$$\gamma_{\text{phrase}}(n) = 1 + 0.35(n - 1)$$

**Time Complexity:** $O(|T| \cdot L_{\max})$ where $|T|$ is token count and $L_{\max} \le 4$ is maximum Trie phrase depth.

---

## 4. Stage 2 — Multi-Source Deterministic Signal Miners (`rcurator/sources/`)

Every miner emits structured `SignalEvidence(term, raw_term, weight, source_type, source_detail, signal_kind)` records:

### 4.1 Local Workspace Scanner (`workspace.py`)

Traverses directories safely (`os.walk(..., followlinks=False)` with `(st_dev, st_ino)` symlink cycle guard, $512\text{ KB}$ per-file and $5{,}000$-file caps, skipping binary `\x00` files, build directories, and a strict secret denylist covering `cookies.json`, `.env*`, `*secret*`, `*token*`, `*credential*`, `*.pem`, `*.key`, `id_rsa*`, `id_ed25519*`, `.netrc`, `.npmrc`, `.pypirc`, `*.sqlite*`, `*.db`).

| Artifact Class | Extraction Mechanism | Base Weight $w(e)$ |
| :--- | :--- | :---: |
| **Dependency Manifests** | `pyproject.toml` (`tomllib`), `requirements*.txt`, `Cargo.toml`, `package.json`, `go.mod`, `CMakeLists.txt` | $3.5$ |
| **Containers, K8s & Dotfiles** | `Containerfile`, `Dockerfile`, `docker-compose.yml`, Kubernetes YAML, `hyprland`/`sway`/`i3`/`waybar`/`kitty`/`neovim`/`tmux` configs | $2.8$ |
| **File Extensions** | Log-dampened frequency per extension: $2.2 \cdot (1 + \ln(\text{count}))$ | $\ge 2.2$ |
| **Source Code Imports** | Python `ast` (filtering `sys.stdlib_module_names`), Rust `use`, Go `import`, C/C++ `#include`, TS/JS `import` | $2.0$ |
| **README & Documentation** | Markdown parser (`1.8x` multiplier on `#` headings) | $1.5$ ($2.7$ headings) |

### 4.2 GitHub Profile & Repository Miner (`github.py`)

Respects GitHub's unauthenticated $60\text{ req/hr}$ ceiling with a $\le 6$ GET request budget (`/users/{u}`, `/users/{u}/repos?per_page=100&sort=pushed`, `/users/{u}/starred?per_page=100`, and READMEs for at most top $K \le 3$ non-fork repositories when `X-RateLimit-Remaining > 10`), backed by `If-None-Match` ETag disk caching (`304 Not Modified` consumes zero quota). Each owned repository $r$ pushed $\Delta d_r$ days ago receives weight:

$$\omega(r) = \left(\mathbb{I}_{\neg\text{fork}} + 0.25\,\mathbb{I}_{\text{fork}}\right) \cdot \exp\!\left(-\frac{\ln 2}{365}\,\Delta d_r\right) \cdot \left(1 + 0.35\,\ln(1 + \text{stars}_r)\right)$$

Starred repositories contribute at $0.35\times$ decay weight.

### 4.3 3-Tier Local Resume PDF & Document Parser (`resume.py`)

Supports `.pdf`, `.md`, `.txt`, and `.tex` up to $15\text{ MB}$ in memory without touching disk:

1. **Tier 1:** Optional `pypdf` if available in the environment.
2. **Tier 2:** Native Poppler `/usr/bin/pdftotext -layout -enc UTF-8 - -` (`shell=False`, `timeout=10`, piped over `stdin`/`stdout`).
3. **Tier 3 (Pure-Python Fallback):** Built-in `zlib` PDF stream decoder supporting `/FlateDecode` (including PNG `/Predictor` row unfiltering), `/Type /ObjStm` compressed object streams, `/ToUnicode` CMaps (`beginbfchar`/`beginbfrange` UTF-16BE), `BT..ET` text matrices (`Td`/`TD`/`Tm`/`T*`), `TJ` kerning array spacing (`offset < -150` emits word space so adjacent tokens like `"Python"` and `"C++"` never concatenate), octal/hex escapes, and explicit `EncryptedPDFError` rejection on `/Encrypt`.

Extracted lines are weighted by detected resume section: `Skills` ($3.5\times$), `Projects`/`Research` ($2.8\times$), `Experience` ($2.4\times$), `Education` ($1.8\times$), `Certifications` ($1.6\times$), and `Summary` ($1.5\times$), with emails, phone numbers, and URL userinfo scrubbed before provenance output.

---

## 5. Stage 3 — Multi-Source Synergy Query Vector (`rcurator/engine/pipeline.py`)

Within each active evidence source $\sigma \in \mathcal{S} \subseteq \{\text{workspace}, \text{resume}, \text{github}, \text{query}\}$, repeated occurrences of term $t$ are sublinearly saturated while preserving peak signal importance:

$$\tilde{u}_\sigma(t) = \ln\!\left(1 + \sum_{e \in E_{\sigma, t}} w(e)\right) \cdot \max_{e \in E_{\sigma, t}} w(e), \qquad \hat{u}_\sigma(t) = \frac{\tilde{u}_\sigma(t)}{\max\!\left(\max_{t'} \tilde{u}_\sigma(t'),\; 10^{-9}\right)}$$

Let $\nu(t) = \left|\{\sigma \in \mathcal{S} : \hat{u}_\sigma(t) > 0\}\right|$ be the number of corroborating sources for term $t$. With frozen source priors $\alpha_{\text{workspace}} = 1.0$, $\alpha_{\text{resume}} = 1.0$, $\alpha_{\text{github}} = 0.9$, $\alpha_{\text{query}} = 1.1$, and cross-source synergy $\beta_{\text{syn}} = 0.25$:

$$q(t) = \left(\sum_{\sigma \in \mathcal{S}} \alpha_\sigma \,\hat{u}_\sigma(t)\right) \cdot \left(1 + \beta_{\text{syn}}\,(\nu(t) - 1)\right)$$

Terms corroborated across multiple independent sources (e.g., `rust` appearing in local `Cargo.toml`, GitHub repositories, and a Resume Skills section) receive a super-linear consensus boost.

---

## 6. Stage 4 — Fielded BM25F + Sublinear TF-IDF Hybrid (`rcurator/engine/lexical.py`)

Each subreddit document $s \in \mathcal{V}_{\text{sub}}$ is partitioned into $6$ structured fields $f$ with field weights $w_f$ and length-normalization parameters $b_f$:

| Field $f$ | Field Weight $w_f$ | Length Norm $b_f$ | Role |
| :--- | :---: | :---: | :--- |
| `name` | $4.0$ | $0.40$ | Exact subreddit name match (e.g., `duckdb`, `FastAPI`, `rust`) |
| `ecosystems` | $3.5$ | $0.40$ | Libraries, compilers, runtimes (`pyarrow`, `tokio`, `llvm`) |
| `file_indicators` | $3.2$ | $0.40$ | Configs & file extensions (`Cargo.toml`, `.rs`, `hyprland.conf`) |
| `keywords` | $2.5$ | $0.75$ | Technical unigrams, bigrams, and concepts (`lsm tree`, `mvcc`) |
| `domains` | $2.0$ | $0.40$ | Canonical domain tags (`systems_engineering`, `rust_and_compilers`) |
| `description` | $1.0$ | $0.75$ | One-line community summary |

Using smoothed Robertson–Sparck Jones inverse document frequency over $N = |\mathcal{V}_{\text{sub}}|$ communities where $n(t)$ is the document frequency of term $t$:

$$\text{IDF}(t) = \ln\!\left(1 + \frac{N - n(t) + 0.5}{n(t) + 0.5}\right)$$

The pseudo-term frequency of term $t$ in subreddit $s$ across fields is:

$$\widetilde{\text{tf}}(t, s) = \sum_{f} \frac{w_f \cdot \text{tf}_{f}(t, s)}{(1 - b_f) + b_f \cdot \frac{|d_{s,f}|}{\text{avgdl}_f}}$$

With saturation $k_1 = 1.5$, fielded BM25F and sublinear cosine TF-IDF scores are computed and max-normalized with $10^{-9}$ zero-division guards:

$$S_{\text{BM25F}}(q, s) = \sum_{t \in q} q(t)\,\text{IDF}(t)\,\frac{\widetilde{\text{tf}}(t, s) \cdot (k_1 + 1)}{\widetilde{\text{tf}}(t, s) + k_1}$$

$$S_{\text{TFIDF}}(q, s) = \frac{\sum_{t \in q} q(t)\,(1 + \ln \widetilde{\text{tf}}(t, s))\,\text{IDF}(t)^2}{\|q\|_2 \cdot \|d_s\|_2 + 10^{-9}}$$

$$S_{\text{lex}}(s) = 0.70\,\widehat{S}_{\text{BM25F}}(q, s) + 0.30\,\widehat{S}_{\text{TFIDF}}(q, s)$$

**Time Complexity:** $O(|q| \cdot \bar{n})$ using inverted field postings, where $|q| \ll 10^3$ and $N = 292$.

---

## 7. Stage 5 — Bipartite Personalized PageRank (`rcurator/engine/graph.py`)

Pure lexical matching misses communities that share deep conceptual neighborhoods without exact surface keyword overlap (e.g., matching `duckdb` and `rust` should surface `Polars`, `databasedevelopment`, and `Compilers`). We construct a bipartite Concept–Subreddit + Subreddit Co-Occurrence graph:

* **Concept–Subreddit Edge Weight:** $W_{c,s} = w_f \cdot \text{IDF}(c) \cdot \rho_s$ when concept $c$ appears in field $f$ of subreddit $s$.
* **Subreddit–Subreddit Edge Weight:** Symmetrized `related_subreddits` adjacency $W_{s,s'} = W_{s',s} = \rho_s \cdot \rho_{s'}$.
* **Row-Stochastic Transition Matrix $P$:** $P_{u,v} = W_{u,v} / \sum_{v'} W_{u,v'}$.
* **Teleportation Vector $p$:** Allocates $0.55$ probability mass proportionally to matched query concepts $q(c)\,\text{IDF}(c)$ and $0.45$ proportionally to lexically matched subreddits $S_{\text{lex}}(s)$.
  * **Cold-Start Guard:** If total matched concept and lexical mass is $0$, $p$ falls back to quality-weighted subreddit priors $p_s = \rho_s / \sum_{s' \in \mathcal{V}_{\text{sub}}} \rho_{s'}$ ($p_c = 0$).

Power iteration runs with damping $\alpha = 0.85$ (redistributing any dangling node mass proportionally to $p$):

$$\pi^{(k+1)} = (1 - \alpha)\,p + \alpha \left(P^\top \pi^{(k)} + \left(\sum_{u \in \mathcal{D}} \pi^{(k)}_u\right) p\right)$$

converging when $\|\pi^{(k+1)} - \pi^{(k)}\|_1 < 10^{-7}$ or $k = 50$.

### Relevance Fusion & Pillar Anchor Prior

Let $\widehat{\pi}_{\text{sub}}(s) = \pi_{\text{sub}}(s) / \max(\max_{s'} \pi_{\text{sub}}(s'), 10^{-9})$ and $\epsilon_c > 0$ be a small baseline prior for `aesthetic_leisure` and `cognitive_curiosity` communities so high-quality leisure/curiosity anchors retain baseline visibility even for narrow technical queries. The composite subreddit relevance is:

$$R(s) = \left(0.60\,\widehat{S}_{\text{lex}}(s) + 0.40\,\widehat{\pi}_{\text{sub}}(s) + \epsilon_c\right)\,(0.70 + 0.30\,\rho_s)$$

---

## 8. Stage 6 — Velocity-Weighted Golden Ratio Constrained Optimizer (`rcurator/engine/optimizer.py`)

Subscribing purely by relevance fails because high-velocity communities (e.g., $180$ posts/day) drown out niche, dense communities ($5$ posts/day). We optimize **expected daily impression share** rather than raw subreddit counts.

Let $v_s = \max(0.5, \text{estimated\_daily\_posts}(s))$, total Tier-1 velocity $V(X) = \sum_{s \in X} v_s$, and achieved impression share distribution over the three Golden Ratio pillars $c \in \{\text{aes}, \text{cog}, \text{dom}\}$:

$$P_{v, c}(X) = \frac{\sum_{s \in X_c} v_s}{V(X)}$$

Given target simplex $\tau = (\tau_{\text{aes}}, \tau_{\text{cog}}, \tau_{\text{dom}})$ (default $(0.40, 0.35, 0.25)$) and Tier-1 size budget $K \in [3, 22]$ (default $18$), we normalize relevance within each pillar ($\tilde{R}_c(s) = R(s) / \max_{s' \in \mathcal{V}_c} R(s')$) so pillars compete on equal footing and solve:

$$\max_{X \subseteq \mathcal{V}_{\text{sub}},\; |X| = K} \;\; \mathcal{J}(X) = \sum_{s \in X} \tilde{R}_c(s) - \mu_{\text{JS}}\, D_{\text{JS}}\!\left(P_v(X) \,\|\, \tau\right) - \mu_{\text{dom}} \sum_{s \in X} \max\!\left(0,\; \frac{v_s}{V(X)} - 0.25\right)^2$$

subject to:

$$|X_c| \ge \max\!\left(1, \min(2, \lfloor K / 3 \rfloor)\right) \;\;\forall c, \qquad \max_{s \in X} \frac{v_s}{V(X)} \le 0.50$$

where $D_{\text{JS}}(P \,\|\, \tau) = \frac{1}{2} D_{\text{KL}}(P \,\|\, M) + \frac{1}{2} D_{\text{KL}}(\tau \,\|\, M)$ with $M = \frac{1}{2}(P + \tau)$.

### 3-Phase Deterministic Solver

1. **Moderate-Velocity Seeding:** Selects $m_{\text{seed}} = \max(1, \min(2, \lfloor K/3 \rfloor))$ high-relevance, moderate-velocity seeds per pillar to initialize $X$ near $\tau$.
2. **Greedy Marginal-Gain Expansion:** Iteratively adds $s^* = \arg\max_{s \notin X} \mathcal{J}(X \cup \{s\})$ respecting the $50\%$ single-subreddit dominance cap.
3. **1-Swap Steepest-Ascent Refinement:** Evaluates candidate swaps $(s_{\text{out}} \in X, s_{\text{in}} \notin X)$ that strictly reduce **Total Variation Drift**:

$$\Delta_{\text{GR}}(X) = \frac{1}{2} \sum_{c \in \{\text{aes}, \text{cog}, \text{dom}\}} \left| P_{v, c}(X) - \tau_c \right| \in [0, 1]$$

while preserving $\ge 95\%$ of pre-refinement cumulative relevance and maintaining $\max_s v_s / V(X) \le 0.50$.

### Tier-2 Custom Feed Overflow Routing

High-relevance `domain_priming` communities that are excluded or swapped out of Tier 1 due to high daily post velocity (e.g., `LocalLLaMA` at $180$ posts/day or `Python` at $115$ posts/day) are **automatically routed to Tier-2 private `custom_feeds`** (grouped by primary domain into $1\text{–}3$ thematic feeds of $\le 100$ subreddits each) alongside Tier-1 `domain_priming` communities—preserving both ambient Following feed balance and deep-dive sprint access.

---

## 9. Stage 7 — Diet Health Auditor & Convex Preset Mixer

### 9.1 Information Diet Health Metrics (`rcurator/analytics.py`)

* **Total Variation Drift & Golden Ratio Fidelity:**

$$\Delta_{\text{GR}}(X) = \frac{1}{2} \sum_{c \in \{\text{aes}, \text{cog}, \text{dom}\}} \left| P_{v, c}(X) - \tau_c \right| \in [0, 1], \qquad \text{Fidelity}(X) = 100 \cdot \left(1 - \Delta_{\text{GR}}(X)\right) \in [0, 100]$$

* **Normalized Shannon Domain Entropy:** Over domain distribution $p_d$ ($D = 17$ domains):

$$H_{\text{norm}}(X) = \frac{-\sum_{d=1}^{D} p_d \ln p_d}{\ln D} \in [0, 1]$$

* **Gini Velocity Concentration:** Over sorted post velocities $v_{(1)} \le \dots \le v_{(n)}$:

$$G(X) = \frac{\sum_{i=1}^{n} (2i - n - 1)\,v_{(i)}}{n \sum_{i=1}^{n} v_{(i)}} \in [0, 1]$$

* **Velocity-Weighted Mean Signal Quality:** $\bar{\rho}_v(X) = \sum_{s \in X} \frac{v_s}{V(X)} \rho_s \in (0, 1]$.
* **Composite Health Score ($0\text{–}100$):** Combines Fidelity, normalized entropy, $(1 - G(X))$, dominance penalty when $\max_s v_s / V(X) > 0.35$, and $\bar{\rho}_v(X)$, emitting concrete add/move-to-custom-feed prescriptions.

### 9.2 Convex Preset Mixer (`rcurator/mixer.py`)

Given preset blend specification $\{(M_i, w_i)\}_{i=1}^m$ with normalized convex weights $\sum_i w_i = 1$, candidate subreddits across the union of presets and their `related_subreddits` neighbors are scored by convex rank + graph proximity and re-optimized through `GoldenRatioOptimizer` to produce a new velocity-balanced manifest.

---

## 10. Worked End-to-End Example

Consider scanning a systems/database workspace containing `Cargo.toml` (`tokio`, `serde`), `pyproject.toml` (`duckdb`, `pyarrow`), and `.c` / `ebpf` files (`io_uring`, `xdp`), with `--ratio 40,35,25` and `tier1_count=18`:

1. **Tokenization & Mining:** `WorkspaceScanner` extracts `cargo.toml` ($3.5$), `rust` ($3.5$), `tokio` ($3.5$), `duckdb` ($3.5$), `pyarrow` ($3.5$), `io_uring` ($2.0$), `ebpf` ($2.8$).
2. **Synergy Vector:** Single-source max-normalized weights yield $q(\text{rust}) = 1.00$, $q(\text{duckdb}) = 1.00$, $q(\text{ebpf}) = 0.80$, $q(\text{io\_uring}) = 0.57$.
3. **Lexical + Bipartite PPR:**
   * `rust` ($v=78.0$, $\rho=0.91$) scores highest on `name` + `ecosystems` (`tokio`, `serde`, `Cargo.toml`).
   * `duckdb` ($v=14.0$, $\rho=0.93$) and `databasedevelopment` ($v=5.0$, $\rho=0.96$) score high on `duckdb`/`pyarrow` and `related_subreddits` edges.
   * `ebpf` ($v=5.0$, $\rho=0.95$) and `kernel` ($v=6.0$, $\rho=0.94$) surge via `io_uring`/`xdp` and co-occurrence with `systems`.
   * PPR spreading activation bridges `rust` + `duckdb` to `Polars` ($v=8.0$, $\rho=0.92$).
4. **Golden Ratio Velocity Optimization:**
   * Putting `rust` ($78.0$ posts/day) in Tier 1 alongside low-velocity `ebpf` ($5.0$) and `duckdb` ($14.0$) would push `domain_priming` past $60\%$ of impressions.
   * The optimizer therefore places moderate/low-velocity `databasedevelopment` ($5.0$), `ebpf` ($5.0$), `kernel` ($6.0$), `Polars` ($8.0$), and `duckdb` ($14.0$) in **Tier 1 (`domain_priming_subreddits`, total $38.0$ posts/day $\approx 25\%$)**, balances them with `aesthetic_leisure` anchors (e.g., `Fedora` $55.0$, `commandline` $22.0$ — $\approx 40\%$) and `cognitive_curiosity` anchors (e.g., `AskHistorians` $55.0$, `DepthHub` $6.0$ — $\approx 35\%$), achieving $\Delta_{\text{GR}} < 0.05$ (**Fidelity $> 95\%$**).
   * High-velocity `rust` ($78.0$ posts/day) is automatically routed to **Tier 2 (`custom_feeds`)** for focused coding sprints without polluting the ambient Following ratio.
