# Passive Cognitive Priming: Feed Curation Technique

> **Objective:** Engineer an ambient information diet that subconsciously primes mental models and technical vocabulary during routine leisure breaks, leveraging passive marketing principles.

---

## 1. Executive Concept

Cognitive load theory shows that intense deliberate study ("System 2" thinking) rapidly depletes mental energy and causes cognitive avoidance. Conversely, the human brain continuously ingests ambient environmental cues ("System 1" heuristics), updating default problem-solving reflexes without conscious effort.

This technique repurposes **passive marketing**: just as repeated exposure to brand visuals builds intuitive recall without deliberate study, controlled exposure to high-signal domain topics builds technical fluency and intuitive problem-solving instincts.

When unexpected design questions, interviews, or architecture reviews arise, relevant terminology, trade-offs, and failure modes are already top-of-mind via Kahneman & Tversky's **Availability Heuristic**.

---

## 2. Core Pillars

### 2.1 The Availability Heuristic (Kahneman & Tversky, 1973)
People assess probability, frequency, and solutions based on how easily examples are retrieved from memory:
* If ambient feeds are full of generic controversy, clickbait, or meme dumps, the retrieval index is cluttered with noise.
* If feeds consistently present real-world discussions (e.g., query execution plans, cache-coherence, memory-mapped I/O, or statistical fallacies), those patterns become the subconscious default during technical analysis.

### 2.2 Context Interleaving (Kornell & Bjork, 2008)
Continuous, uninterrupted study of a single subject produces mental fatigue and shallow retention. **Interleaving** distinct disciplines (e.g., visual craftsmanship, rigorous history, and systems internals) forces cognitive re-indexing, cementing concepts into long-term semantic memory.

### 2.3 The Golden Ratio Distribution
To prevent feed aversion (where a feed feels like homework and is abandoned for cheap distractions), configure your subscriptions according to the tripartite **Golden Ratio**:

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                        THE GOLDEN RATIO FEED DISTRIBUTION                              │
│                                                                                        │
│   ┌──────────────────────────┬──────────────────────────┬───────────────────────────┐  │
│   │   Aesthetic / Leisure    │  Cognitive / Deep Dive   │  Target Domain Priming    │  │
│   │  40% Feed Impressions    │  35% Feed Impressions    │  25% Feed Impressions     │  │
│   │  (High Post Velocity)    │  (Medium Post Velocity)  │  (Focused Signal Density) │  │
│   │  [Flow & Craftsmanship]  │  [Mental Models & Sci]   │  [Core Learning Subject]  │  │
│   └──────────────────────────┴──────────────────────────┴───────────────────────────┘  │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

1. **40% Aesthetic & Leisure (The Hook):** High-velocity craft and visual communities (e.g. desktop ricing, terminal tools). Sustains genuine relaxation so opening the feed remains enjoyable.
2. **35% Cognitive Depth & Curiosity (Lateral Thinking):** High-signal, strictly-moderated communities (e.g. cognitive science, economic history, epistemology, long-form journalism). Fosters analytical patience and intellectual rigor.
3. **25% Target Domain Priming (The Professional Edge):** Focused technical communities mapped directly to your current project or career focus (e.g. database engines, compilers, cloud-native orchestration).

---

## 3. Two-Tiered Architecture

High-volume subreddits (such as general language or framework communities with 400k+ subscribers) can drown out small, niche, high-signal communities. To resolve this:

1. **Tier 1 (Main Following Feed):**
   * Combines aesthetic anchors, cognitive curiosity, and lower-volume, high-density domain communities.
   * Locked to the **Following** feed (`sort=BEST&feed=following`) to bypass algorithmic location recommendations.
2. **Tier 2 (Dedicated Custom Feed / Multireddit):**
   * A private custom feed (e.g., `/user/{username}/m/systems_core`) housing higher-volume technical communities.
   * Used for intentional 1-click deep immersion sprints without polluting the casual ambient feed.

---

## 4. Implementation Rules

1. **Bypass Algorithmic Recommendations:** Ensure your Reddit Account Settings have Location Customization set to *"No location specified"* and default to the **Following** tab.
2. **Filter Out Low-Signal Communities:** Mute or unfollow high-volume regional gossip, memes, and drama communities.
3. **Maintain the Ratio:** Keep total subscribed communities balanced so no single high-volume subreddit exceeds 50% of incoming posts.
