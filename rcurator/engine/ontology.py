"""Ontology repository, manifest normalizer, and standalone technical tokenizer fallback."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

CATEGORIES: tuple[str, str, str] = (
    "aesthetic_leisure",
    "cognitive_curiosity",
    "domain_priming",
)

# Frozen canonical alias map matching #9 §3 (identical to Slice 2 TechnicalTokenizer).
CANONICAL_ALIASES: dict[str, str] = {
    "nodejs": "node.js",
    "nextjs": "next.js",
    "vuejs": "vue.js",
    "nuxtjs": "nuxt.js",
    "threejs": "three.js",
    "d3js": "d3.js",
    "dotnet": ".net",
    "csharp": "c#",
    "fsharp": "f#",
    "cpp": "c++",
    "cxx": "c++",
    "llamacpp": "llama.cpp",
    "k8s": "kubernetes",
    "k3s": "kubernetes",
    "golang": "go",
    "postgres": "postgresql",
    "py": "python",
    "rs": "rust",
    "ts": "typescript",
    "js": "javascript",
    "tf": "tensorflow",
    "sklearn": "scikit-learn",
}

PROTECTED_SHORT_SYMBOLS: frozenset[str] = frozenset(
    {
        "c",
        "r",
        "go",
        "ml",
        "ai",
        "os",
        "db",
        "ir",
        "vm",
        "ui",
        "ux",
        "io",
        "3d",
        "2d",
        "ar",
        "vr",
        "qa",
        "ci",
        "cd",
        "fp",
        "hw",
        "sw",
        "dl",
        "rl",
    }
)

STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "being",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "doing",
        "done",
        "for",
        "from",
        "had",
        "has",
        "have",
        "having",
        "he",
        "her",
        "here",
        "hers",
        "him",
        "his",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "just",
        "may",
        "me",
        "might",
        "more",
        "most",
        "my",
        "no",
        "nor",
        "not",
        "of",
        "on",
        "only",
        "or",
        "other",
        "our",
        "out",
        "over",
        "own",
        "same",
        "she",
        "should",
        "so",
        "some",
        "such",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "to",
        "too",
        "under",
        "until",
        "up",
        "using",
        "use",
        "used",
        "very",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
        # Code / resume boilerplate
        "true",
        "false",
        "null",
        "none",
        "self",
        "return",
        "import",
        "def",
        "class",
        "const",
        "let",
        "var",
        "function",
        "public",
        "private",
        "static",
        "void",
        "etc",
        "via",
        "based",
        "built",
        "build",
        "building",
        "developed",
        "work",
        "worked",
        "working",
        "project",
        "projects",
        "experience",
        "summary",
        "skills",
    }
)

_TOKEN_EXTRACT_RE = re.compile(
    r"""
    (?<![a-zA-Z0-9_])
    (?:
        \.[a-zA-Z0-9][a-zA-Z0-9_.-]*
        | [a-zA-Z0-9][a-zA-Z0-9_.-]*<[a-zA-Z0-9_,\s]+>
        | [a-zA-Z0-9][a-zA-Z0-9_.&+/-]*(?:\+{2}(?:\d{2})?|\#)
        | [a-zA-Z0-9][a-zA-Z0-9_.&+/-]*
    )
    """,
    re.VERBOSE,
)

_CAMEL_SPLIT_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_FEED_NAME_SANITIZE_RE = re.compile(r"[^a-z0-9_]+")
_VALID_FEED_NAME_RE = re.compile(r"^[a-z0-9_]{1,50}$")

DEFAULT_ONTOLOGY_PATH = (
    Path(__file__).resolve().parents[2] / "curator_data" / "subreddit_ontology.json"
)


def _builtin_canonicalize_token(token: str) -> str:
    """Canonicalize a technical token while preserving punctuated identifiers (#9 §3)."""
    cleaned = token.strip().strip("\"'`()[]{},;:!?")
    if not cleaned:
        return ""

    # Strip trailing sentence dots (e.g. "node.js." -> "node.js") while keeping internal dots
    # and leading dots (.net, .py, .bpf.c).
    while len(cleaned) > 1 and cleaned.endswith("."):
        cleaned = cleaned[:-1]

    # Collapse spaces inside generic brackets like span<t>
    cleaned = re.sub(r"\s+", "", cleaned)
    lowered = cleaned.lower()

    if lowered in CANONICAL_ALIASES:
        return CANONICAL_ALIASES[lowered]

    return lowered


_SLICE2_CANONICALIZER: Any = None
_SLICE2_CHECKED: bool = False


def canonicalize_token(token: str) -> str:
    """Use Slice 2 canonicalizer when available, else built-in identical fallback."""
    global _SLICE2_CANONICALIZER, _SLICE2_CHECKED
    if not _SLICE2_CHECKED:
        _SLICE2_CHECKED = True
        try:
            from rcurator.nlp.tokenizer import canonicalize_token as slice2_canon

            _SLICE2_CANONICALIZER = slice2_canon
        except ImportError:
            _SLICE2_CANONICALIZER = None

    if _SLICE2_CANONICALIZER is not None:
        result = _SLICE2_CANONICALIZER(token)
        if result:
            return CANONICAL_ALIASES.get(result, result)
        return result
    return _builtin_canonicalize_token(token)


def _split_subtokens(raw_token: str, canonical: str) -> list[str]:
    """Emit CamelCase / snake_case / hyphen / slash / dot sub-tokens for dual emission."""
    parts: list[str] = []
    # Split CamelCase on raw form first
    camel_parts = _CAMEL_SPLIT_RE.split(raw_token)
    candidates = camel_parts if len(camel_parts) > 1 else [raw_token]

    for seg in candidates:
        for piece in re.split(r"[_/\-.]+", seg):
            sub = _builtin_canonicalize_token(piece)
            if not sub or sub == canonical:
                continue
            if len(sub) < 2 and sub not in PROTECTED_SHORT_SYMBOLS:
                continue
            if sub in STOPWORDS:
                continue
            parts.append(sub)
    return parts


def tokenize_technical_text(
    text: str,
    known_phrases: Iterable[str] = (),
    include_subtokens: bool = True,
    max_ngram: int = 3,
) -> list[tuple[str, float]]:
    """Deterministic technical lexer emitting weighted tokens and ontology phrases."""
    if not text or not text.strip():
        return []

    primary_tokens: list[str] = []
    emitted: list[tuple[str, float]] = []

    for match in _TOKEN_EXTRACT_RE.finditer(text):
        raw = match.group(0)
        canon = canonicalize_token(raw)
        if not canon:
            continue

        # Strip trailing '-' or '/' artifacts
        canon = canon.strip("-/")
        if not canon:
            continue
        canon = CANONICAL_ALIASES.get(canon, canon)

        if canon in STOPWORDS:
            continue
        if len(canon) < 2 and canon not in PROTECTED_SHORT_SYMBOLS:
            continue

        primary_tokens.append(canon)
        emitted.append((canon, 1.0))

        if include_subtokens:
            for sub in _split_subtokens(raw, canon):
                emitted.append((sub, 0.5))

    # Extract n-gram phrases (specificity multiplier 1 + 0.35 * (n - 1))
    n_toks = len(primary_tokens)
    if max_ngram >= 2 and n_toks >= 2:
        if isinstance(known_phrases, frozenset):
            phrase_set = known_phrases
        elif known_phrases:
            phrase_set = frozenset(
                " ".join(canonicalize_token(w) for w in p.split() if canonicalize_token(w))
                for p in known_phrases
                if p and " " in p
            ) - {""}
        else:
            phrase_set = frozenset()

        for n in range(2, min(max_ngram, n_toks) + 1):
            mult = 1.0 + 0.35 * (n - 1)
            for idx in range(n_toks - n + 1):
                gram = " ".join(primary_tokens[idx : idx + n])
                if not phrase_set or gram in phrase_set:
                    emitted.append((gram, mult))

    return emitted


def sanitize_feed_name(raw_name: str, fallback: str = "domain_focus") -> str:
    """Sanitize a custom feed name to match Reddit's ^[a-z0-9_]{1,50}$ constraint."""
    cleaned = _FEED_NAME_SANITIZE_RE.sub("_", raw_name.strip().lower()).strip("_")
    cleaned = re.sub(r"_+", "_", cleaned)
    if not cleaned:
        cleaned = fallback
    cleaned = cleaned[:50].strip("_")
    if not _VALID_FEED_NAME_RE.match(cleaned):
        return fallback[:50]
    return cleaned


@dataclass(frozen=True)
class SubredditRecord:
    """Canonical representation of one subreddit in the curation ontology."""

    name: str
    category: str
    domains: tuple[str, ...] = ()
    estimated_daily_posts: float = 20.0
    signal_density: float = 0.85
    keywords: tuple[str, ...] = ()
    ecosystems: tuple[str, ...] = ()
    file_indicators: tuple[str, ...] = ()
    related_subreddits: tuple[str, ...] = ()
    description: str = ""
    is_online_candidate: bool = False
    subscribers: int = 0

    @property
    def velocity(self) -> float:
        return max(0.5, float(self.estimated_daily_posts))

    @classmethod
    def from_dict(cls, key_name: str, payload: dict[str, Any]) -> SubredditRecord:
        name = str(payload.get("name") or key_name).strip()
        raw_cat = str(payload.get("category") or "domain_priming").strip()
        if raw_cat == "high_signal_systems_priming":
            raw_cat = "domain_priming"
        return cls(
            name=name,
            category=raw_cat,
            domains=tuple(str(d) for d in payload.get("domains", []) if d),
            estimated_daily_posts=max(
                0.5, float(payload.get("estimated_daily_posts", 20.0) or 20.0)
            ),
            signal_density=max(
                0.0, min(1.0, float(payload.get("signal_density", 0.85) or 0.85))
            ),
            keywords=tuple(str(k) for k in payload.get("keywords", []) if k),
            ecosystems=tuple(str(e) for e in payload.get("ecosystems", []) if e),
            file_indicators=tuple(
                str(f) for f in payload.get("file_indicators", []) if f
            ),
            related_subreddits=tuple(
                str(r) for r in payload.get("related_subreddits", []) if r
            ),
            description=str(payload.get("description") or "").strip(),
            is_online_candidate=bool(payload.get("is_online_candidate", False)),
            subscribers=int(payload.get("subscribers", 0) or 0),
        )


class OntologyRepository:
    """In-memory repository of canonical subreddit records and multi-word phrases."""

    def __init__(
        self,
        records: Sequence[SubredditRecord],
        domains: Sequence[str] = (),
        categories: Sequence[str] = CATEGORIES,
        schema: str = "reddit-curator.ontology.v1",
    ) -> None:
        self.schema = schema
        self.categories: tuple[str, ...] = tuple(categories) or CATEGORIES
        self._records: dict[str, SubredditRecord] = {}
        self._by_lower: dict[str, SubredditRecord] = {}

        discovered_domains: list[str] = list(domains)
        seen_domains = set(discovered_domains)

        for rec in records:
            self._records[rec.name] = rec
            self._by_lower[rec.name.lower()] = rec
            for dom in rec.domains:
                if dom not in seen_domains:
                    seen_domains.add(dom)
                    discovered_domains.append(dom)

        self.domains: tuple[str, ...] = tuple(discovered_domains)
        self.known_phrases: frozenset[str] = self._build_known_phrases()

    def _build_known_phrases(self) -> frozenset[str]:
        phrases: set[str] = set()
        for rec in self._records.values():
            for item in (*rec.keywords, *rec.ecosystems, *rec.file_indicators):
                if " " in item:
                    norm = " ".join(
                        canonicalize_token(w)
                        for w in item.split()
                        if canonicalize_token(w)
                    )
                    if " " in norm:
                        phrases.add(norm)
        return frozenset(phrases)

    @classmethod
    def load(cls, path: Path | None = None) -> OntologyRepository:
        """Load ontology from JSON (`dict[str, dict]` or `list[dict]` fixture)."""
        target = Path(path) if path is not None else DEFAULT_ONTOLOGY_PATH
        raw = json.loads(target.read_text(encoding="utf-8"))

        records: list[SubredditRecord] = []
        domains: Sequence[str] = ()
        categories: Sequence[str] = CATEGORIES
        schema = "reddit-curator.ontology.v1"

        if isinstance(raw, list):
            for entry in raw:
                if isinstance(entry, dict) and entry.get("name"):
                    records.append(SubredditRecord.from_dict(entry["name"], entry))
        elif isinstance(raw, dict):
            schema = str(raw.get("schema", schema))
            domains = raw.get("domains", ())
            categories = raw.get("categories", CATEGORIES)
            subs = raw.get("subreddits", {})
            if isinstance(subs, dict):
                for key_name, entry in subs.items():
                    if isinstance(entry, dict):
                        records.append(SubredditRecord.from_dict(key_name, entry))
            elif isinstance(subs, list):
                for entry in subs:
                    if isinstance(entry, dict) and entry.get("name"):
                        records.append(SubredditRecord.from_dict(entry["name"], entry))
        else:
            raise ValueError(f"Unsupported ontology JSON structure in {target}")

        return cls(records=records, domains=domains, categories=categories, schema=schema)

    def __len__(self) -> int:
        return len(self._records)

    def __contains__(self, name: str) -> bool:
        return name.lower() in self._by_lower

    def get(self, name: str) -> SubredditRecord | None:
        return self._by_lower.get(name.strip().lower())

    def canonical_name(self, name: str) -> str:
        rec = self.get(name)
        return rec.name if rec is not None else name.strip()

    def all_subreddits(self) -> list[SubredditRecord]:
        return list(self._records.values())

    def by_category(self, category: str) -> list[SubredditRecord]:
        return [r for r in self._records.values() if r.category == category]

    def extend_with(self, extra_records: Sequence[SubredditRecord]) -> OntologyRepository:
        """Return a new OntologyRepository adding non-duplicate extra records."""
        merged = list(self._records.values())
        seen_lower = set(self._by_lower.keys())
        for rec in extra_records:
            low = rec.name.lower()
            if low not in seen_lower:
                seen_lower.add(low)
                merged.append(rec)
        return OntologyRepository(
            records=merged,
            domains=self.domains,
            categories=self.categories,
            schema=self.schema,
        )


def _normalize_ratio_dict(raw_ratio: Any) -> dict[str, float]:
    """Normalize legacy or current golden_ratio_distribution dictionaries."""
    if not isinstance(raw_ratio, dict):
        return {
            "aesthetic_leisure_target_pct": 40,
            "cognitive_curiosity_target_pct": 35,
            "domain_priming_target_pct": 25,
        }

    aes = raw_ratio.get(
        "aesthetic_leisure_target_pct",
        raw_ratio.get(
            "aesthetic_leisure_pct",
            raw_ratio.get("aesthetic_leisure", raw_ratio.get("aesthetic", 40)),
        ),
    )
    cog = raw_ratio.get(
        "cognitive_curiosity_target_pct",
        raw_ratio.get(
            "cognitive_curiosity_pct",
            raw_ratio.get("cognitive_curiosity", raw_ratio.get("cognitive", 35)),
        ),
    )
    dom = raw_ratio.get(
        "domain_priming_target_pct",
        raw_ratio.get(
            "domain_priming_pct",
            raw_ratio.get(
                "high_signal_systems_priming_target_pct",
                raw_ratio.get(
                    "systems_priming_pct",
                    raw_ratio.get("domain_priming", raw_ratio.get("domain", 25)),
                ),
            ),
        ),
    )
    return {
        "aesthetic_leisure_target_pct": aes,
        "cognitive_curiosity_target_pct": cog,
        "domain_priming_target_pct": dom,
    }


def _normalize_sub_entry(
    item: Any,
    default_category: str,
    ontology: OntologyRepository | None,
) -> dict[str, Any] | None:
    if isinstance(item, str):
        raw_name = item.strip()
        raw_cat = default_category
        desc = ""
    elif isinstance(item, dict) and item.get("name"):
        raw_name = str(item["name"]).strip()
        raw_cat = str(item.get("category") or default_category).strip()
        desc = str(item.get("description") or "").strip()
    else:
        return None

    if not raw_name:
        return None

    if raw_cat == "high_signal_systems_priming":
        raw_cat = "domain_priming"

    rec = ontology.get(raw_name) if ontology is not None else None
    canon_name = rec.name if rec is not None else raw_name
    final_cat = rec.category if rec is not None else raw_cat
    if not desc and rec is not None:
        desc = rec.description

    return {
        "name": canon_name,
        "category": final_cat,
        "description": desc,
    }


def normalize_manifest(
    data: dict[str, Any], ontology: OntologyRepository | None = None
) -> dict[str, Any]:
    """Normalize v1/v2 or legacy curation_manifest.json into canonical manifest schema (#9 §3)."""
    if ontology is None and DEFAULT_ONTOLOGY_PATH.is_file():
        ontology = OntologyRepository.load(DEFAULT_ONTOLOGY_PATH)

    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    raw_ratio = (
        data.get("golden_ratio_distribution")
        or metadata.get("cognitive_distribution")
        or metadata.get("golden_ratio_distribution")
        or {}
    )

    anchors_raw = data.get("anchor_subreddits") or []
    anchors: list[dict[str, Any]] = []
    seen_anchors: set[str] = set()
    for item in anchors_raw:
        entry = _normalize_sub_entry(item, "aesthetic_leisure", ontology)
        if entry and entry["name"].lower() not in seen_anchors:
            seen_anchors.add(entry["name"].lower())
            anchors.append(entry)

    priming_raw = (
        data.get("domain_priming_subreddits")
        or data.get("tier1_systems_subreddits")
        or []
    )
    priming: list[dict[str, Any]] = []
    seen_priming: set[str] = set()
    for item in priming_raw:
        entry = _normalize_sub_entry(item, "domain_priming", ontology)
        if entry and entry["name"].lower() not in seen_priming:
            seen_priming.add(entry["name"].lower())
            priming.append(entry)

    feeds_out: list[dict[str, Any]] = []
    for feed in data.get("custom_feeds") or []:
        if not isinstance(feed, dict):
            continue
        raw_fname = str(feed.get("feed_name") or feed.get("name") or "domain_focus")
        fname = sanitize_feed_name(raw_fname)
        canon_subs: list[str] = []
        seen_feed_subs: set[str] = set()
        for sub in feed.get("subreddits") or []:
            sub_name = str(sub.get("name") if isinstance(sub, dict) else sub).strip()
            if not sub_name:
                continue
            cname = ontology.canonical_name(sub_name) if ontology is not None else sub_name
            if cname.lower() not in seen_feed_subs:
                seen_feed_subs.add(cname.lower())
                canon_subs.append(cname)
        feeds_out.append(
            {
                "feed_name": fname,
                "display_name": str(feed.get("display_name") or fname.replace("_", " ").title()),
                "description_md": str(
                    feed.get("description_md")
                    or feed.get("description")
                    or "Curated domain focus feed."
                ),
                "icon_name": str(feed.get("icon_name") or "programming"),
                "visibility": str(feed.get("visibility") or "private"),
                "subreddits": canon_subs,
            }
        )

    out: dict[str, Any] = {
        "version": str(data.get("version") or "1.0.0"),
        "name": str(data.get("name") or metadata.get("name") or "Curated Feed Manifest"),
        "description": str(
            data.get("description")
            or metadata.get("description")
            or "Balanced Reddit cognitive curation manifest."
        ),
        "feed_mode": str(data.get("feed_mode") or "following"),
        "golden_ratio_distribution": _normalize_ratio_dict(raw_ratio),
        "anchor_subreddits": anchors,
        "domain_priming_subreddits": priming,
        "custom_feeds": feeds_out,
    }

    if isinstance(data.get("curation_provenance"), dict):
        prov = dict(data["curation_provenance"])
        if isinstance(prov.get("explanations"), dict) and ontology is not None:
            norm_exp: dict[str, Any] = {}
            for k, v in prov["explanations"].items():
                ck = ontology.canonical_name(str(k))
                if isinstance(v, dict):
                    item = dict(v)
                    item["name"] = ck
                    if item.get("category") == "high_signal_systems_priming":
                        item["category"] = "domain_priming"
                    norm_exp[ck] = item
                else:
                    norm_exp[ck] = v
            prov["explanations"] = norm_exp
        out["curation_provenance"] = prov

    return out
