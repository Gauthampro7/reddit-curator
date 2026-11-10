# reddit-curator

> Automated Cognitive Information Diet & Feed Curator for Reddit via Chrome DevTools Protocol (CDP).

`reddit-curator` turns Reddit into a passive learning and productivity engine. It uses the principles of **passive marketing** and the **availability heuristic** to ambiently prime your subconscious with technical domain concepts, mental models, and craftsmanship during everyday browsing breaks—without algorithmic clickbait, outrage, or study burnout.

No Reddit developer API keys or script apps required. It attaches directly to an active browser session via CDP.

---

## Features

* **Passive Cognitive Priming:** Enforces a **Golden Ratio** (40% Aesthetic/Leisure, 35% Cognitive Curiosity, 25% Target Domain Priming) to maintain genuine enjoyment while conditioning mental models.
* **Zero Credential Configuration:** Connects to an existing, authenticated browser session via Chrome DevTools Protocol (port 9222) and automatically rehydrates session tokens.
* **One-Shot & Agent-Driven:** Run standalone via CLI (`--one-shot`) or drive programmatically via AI agents using structured JSON output (`--agent`).
* **Domain Presets Included:** Ships with ready-to-use manifests for:
  * `systems_engineering` (Low-level systems, compilers, database internals, kernel, Linux)
  * `machine_learning` (Model architectures, local inference, quantization, research papers)
  * `cognitive_science` (Decision theory, behavioral economics, social psychology, epistemology)
* **Custom Feed (Multireddit) Synchronization:** Automatically creates and updates private custom feeds with idempotent `PUT` upserting and lowercased URL routing.
* **Following Feed Lock:** Automatically switches and locks your feed to the **Following** tab (`#feed-tab-2`), cutting off location-based algorithmic clickbait and meme spam.

---

## Quickstart

### 1. Prerequisites & Dependencies

Python 3.10+ is required. Install dependencies:

```bash
pip install -r requirements.txt
```

### 2. Launch Chrome with Remote Debugging

Launch Google Chrome (or Chromium / Brave) with remote debugging enabled on port `9222`:

```bash
google-chrome --remote-debugging-port=9222 --user-data-dir="$HOME/.config/chrome-automation" https://www.reddit.com
```

Log in to your Reddit account in that window (or import your cookies).

### 3. Run the Curator

#### Option A: One-Shot Curation with a Preset
Apply a curated preset in a single command:

```bash
# View available presets
python3 reddit_curator.py --list-presets

# Apply Systems Engineering preset
python3 reddit_curator.py --preset systems_engineering --one-shot
```

#### Option B: Curation with a Custom Manifest
Copy the template and define your own target communities:

```bash
cp manifest.example.json manifest.json
# Edit manifest.json to match your goals
python3 reddit_curator.py --manifest manifest.json --one-shot
```

#### Option C: Verification & Audit
Audit your live account subscriptions and custom feed health without making mutations:

```bash
python3 reddit_curator.py --verify
```

---

## CLI Reference

| Flag | Type | Description |
| :--- | :--- | :--- |
| `--one-shot` | Flag | Execute end-to-end curation, subscription, custom feed sync, and feed lock. |
| `--preset <name>` | String | Use a preset from `presets/` (`systems_engineering`, `machine_learning`, `cognitive_science`). |
| `--manifest <path>` | Path | Path to custom manifest JSON file. |
| `--verify` | Flag | Audit current subscriptions and custom feeds without making any changes. |
| `--dry-run` | Flag | Preview mutations and API endpoints without executing them. |
| `--agent` | Flag | Machine-readable mode: suppresses logs and emits pure JSON to `stdout`. |
| `--list-presets` | Flag | Print list of available presets and exit. |
| `--port <port>` | Int | Chrome DevTools Protocol remote debugging port (default: `9222`). |

---

## Manifest Format

Manifests define your target information diet declaratively:

```json
{
  "version": "1.0.0",
  "name": "My Custom Curation",
  "feed_mode": "following",
  "anchor_subreddits": [
    {"name": "unixporn", "category": "aesthetic_leisure"},
    {"name": "linux", "category": "aesthetic_leisure"},
    {"name": "AskHistorians", "category": "cognitive_curiosity"},
    {"name": "cogsci", "category": "cognitive_curiosity"}
  ],
  "domain_priming_subreddits": [
    {"name": "databasedevelopment", "category": "domain_priming"},
    {"name": "Compilers", "category": "domain_priming"},
    {"name": "kernel", "category": "domain_priming"}
  ],
  "custom_feeds": [
    {
      "feed_name": "systems_core",
      "display_name": "Systems Deep Dive",
      "visibility": "private",
      "subreddits": [
        "databasedevelopment",
        "Compilers",
        "kernel"
      ]
    }
  ]
}
```

---

## Methodology & Cognitive Strategy

For an in-depth breakdown of the psychology behind ambient priming, context interleaving, and the Availability Heuristic, read [`TECHNIQUE.md`](TECHNIQUE.md).

---

## License

MIT
