"""Ontology repository, manifest normalizer, and validation helpers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from rcurator.nlp.tokenizer import (
    ALL_STOPWORDS,
    CANONICAL_ALIASES,
    PROTECTED_SHORT_SYMBOLS,
    canonicalize_token,
    tokenize_technical_text,
)

CATEGORIES: tuple[str, str, str] = (
    "aesthetic_leisure",
    "cognitive_curiosity",
    "domain_priming",
)

STOPWORDS: frozenset[str] = ALL_STOPWORDS

_FEED_NAME_SANITIZE_RE = re.compile(r"[^a-z0-9_]+")
_VALID_FEED_NAME_RE = re.compile(r"^[a-z0-9_]{1,50}$")

DEFAULT_ONTOLOGY_PATH = (
    Path(__file__).resolve().parents[2] / "curator_data" / "subreddit_ontology.json"
)


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


def _strip_r_prefix(raw_name: str) -> str:
    """Strip leading r/ or /r/ and trailing slashes from a subreddit name."""
    name = raw_name.strip()
    low = name.lower()
    if low.startswith("/r/"):
        name = name[3:]
    elif low.startswith("r/"):
        name = name[2:]
    return name.strip().strip("/")


def _normalize_sub_entry(
    item: Any,
    default_category: str,
    ontology: OntologyRepository | None,
) -> dict[str, Any] | None:
    explicit_cat: str | None = None
    if isinstance(item, str):
        raw_name = _strip_r_prefix(item)
        desc = ""
    elif isinstance(item, dict) and item.get("name"):
        raw_name = _strip_r_prefix(str(item["name"]))
        raw_explicit = str(item.get("category") or "").strip()
        if raw_explicit == "high_signal_systems_priming":
            raw_explicit = "domain_priming"
        if raw_explicit in CATEGORIES:
            explicit_cat = raw_explicit
        desc = str(item.get("description") or "").strip()
    else:
        return None

    if not raw_name:
        return None

    fallback_cat = (
        default_category if default_category in CATEGORIES else "domain_priming"
    )
    rec = ontology.get(raw_name) if ontology is not None else None
    canon_name = rec.name if rec is not None else raw_name
    if explicit_cat is not None:
        final_cat = explicit_cat
    elif rec is not None and rec.category in CATEGORIES:
        final_cat = rec.category
    else:
        final_cat = fallback_cat
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
    if not isinstance(data, dict):
        raise ValueError("Manifest payload must be a JSON object")

    if ontology is None and DEFAULT_ONTOLOGY_PATH.is_file():
        ontology = OntologyRepository.load(DEFAULT_ONTOLOGY_PATH)

    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    raw_ratio = (
        data.get("golden_ratio_distribution")
        or metadata.get("cognitive_distribution")
        or metadata.get("golden_ratio_distribution")
        or {}
    )

    anchors_raw = data.get("anchor_subreddits")
    if not isinstance(anchors_raw, list):
        anchors_raw = []
    anchors: list[dict[str, Any]] = []
    seen_tier1: set[str] = set()
    for item in anchors_raw:
        entry = _normalize_sub_entry(item, "aesthetic_leisure", ontology)
        if entry and entry["name"].lower() not in seen_tier1:
            seen_tier1.add(entry["name"].lower())
            anchors.append(entry)

    priming_raw = data.get("domain_priming_subreddits")
    if priming_raw is None:
        priming_raw = data.get("tier1_systems_subreddits")
    if not isinstance(priming_raw, list):
        priming_raw = []
    priming: list[dict[str, Any]] = []
    for item in priming_raw:
        entry = _normalize_sub_entry(item, "domain_priming", ontology)
        if entry and entry["name"].lower() not in seen_tier1:
            seen_tier1.add(entry["name"].lower())
            priming.append(entry)

    feeds_raw = data.get("custom_feeds")
    if not isinstance(feeds_raw, list):
        feeds_raw = []
    feeds_out: list[dict[str, Any]] = []
    used_feed_names: set[str] = set()
    for feed in feeds_raw:
        if not isinstance(feed, dict):
            continue
        raw_fname = str(feed.get("feed_name") or feed.get("name") or "domain_focus")
        base_fname = sanitize_feed_name(raw_fname)
        fname = base_fname
        suffix_idx = 2
        while fname in used_feed_names:
            suffix = f"_{suffix_idx}"
            fname = sanitize_feed_name(f"{base_fname[: 50 - len(suffix)]}{suffix}")
            suffix_idx += 1
        used_feed_names.add(fname)

        subs_raw = feed.get("subreddits")
        if not isinstance(subs_raw, list):
            subs_raw = []
        canon_subs: list[str] = []
        seen_feed_subs: set[str] = set()
        for sub in subs_raw:
            raw_sub = sub.get("name") if isinstance(sub, dict) else sub
            if raw_sub is None or isinstance(raw_sub, (dict, list, bool)):
                continue
            sub_name = _strip_r_prefix(str(raw_sub))
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
                ck = ontology.canonical_name(_strip_r_prefix(str(k)))
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


def validate_manifest(
    data: Any, ontology: OntologyRepository | None = None
) -> dict[str, Any]:
    """Validate a raw or normalized manifest and return structured errors, warnings, and summary."""
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(data, dict):
        return {
            "valid": False,
            "errors": ["Manifest root must be a JSON object."],
            "warnings": [],
            "normalized": None,
            "summary": {},
        }

    if ontology is None and DEFAULT_ONTOLOGY_PATH.is_file():
        ontology = OntologyRepository.load(DEFAULT_ONTOLOGY_PATH)

    if "anchor_subreddits" in data and data["anchor_subreddits"] is not None:
        if not isinstance(data["anchor_subreddits"], list):
            errors.append("'anchor_subreddits' must be a list.")
    if "domain_priming_subreddits" in data and data["domain_priming_subreddits"] is not None:
        if not isinstance(data["domain_priming_subreddits"], list):
            errors.append("'domain_priming_subreddits' must be a list.")
    elif "tier1_systems_subreddits" in data and data["tier1_systems_subreddits"] is not None:
        if not isinstance(data["tier1_systems_subreddits"], list):
            errors.append("'tier1_systems_subreddits' must be a list.")
    if "custom_feeds" in data and data["custom_feeds"] is not None:
        if not isinstance(data["custom_feeds"], list):
            errors.append("'custom_feeds' must be a list.")

    # Inspect raw tier-1 items for malformed entries, invalid categories, and duplicates
    seen_raw_t1: set[str] = set()
    for field_name in ("anchor_subreddits", "domain_priming_subreddits", "tier1_systems_subreddits"):
        raw_list = data.get(field_name)
        if not isinstance(raw_list, list):
            continue
        for idx, item in enumerate(raw_list):
            if isinstance(item, str):
                sname = _strip_r_prefix(item)
                if not sname:
                    errors.append(f"{field_name}[{idx}] is empty.")
                    continue
            elif isinstance(item, dict) and item.get("name"):
                sname = _strip_r_prefix(str(item["name"]))
                if not sname:
                    errors.append(f"{field_name}[{idx}].name is empty.")
                    continue
                raw_cat = str(item.get("category") or "").strip()
                if (
                    raw_cat
                    and raw_cat != "high_signal_systems_priming"
                    and raw_cat not in CATEGORIES
                ):
                    warnings.append(
                        f"{field_name}[{idx}] (r/{sname}) has unknown category '{raw_cat}'; falling back."
                    )
            else:
                errors.append(f"{field_name}[{idx}] must be a subreddit string or object with 'name'.")
                continue

            low = sname.lower()
            if low in seen_raw_t1:
                warnings.append(f"Duplicate Tier 1 subreddit r/{sname} in {field_name}; deduplicated.")
            else:
                seen_raw_t1.add(low)

    # Inspect raw custom_feeds
    raw_feeds = data.get("custom_feeds")
    if isinstance(raw_feeds, list):
        seen_raw_fnames: set[str] = set()
        for idx, feed in enumerate(raw_feeds):
            if not isinstance(feed, dict):
                errors.append(f"custom_feeds[{idx}] must be a JSON object.")
                continue
            subs_val = feed.get("subreddits")
            if subs_val is not None and not isinstance(subs_val, list):
                errors.append(f"custom_feeds[{idx}].subreddits must be a list.")
            elif not subs_val:
                warnings.append(f"custom_feeds[{idx}] has no subreddits.")
            elif len(subs_val) > 100:
                warnings.append(
                    f"custom_feeds[{idx}] has {len(subs_val)} subreddits (>100 Reddit limit; will partition on sync)."
                )
            raw_fn = str(feed.get("feed_name") or feed.get("name") or "domain_focus")
            san = sanitize_feed_name(raw_fn)
            if san in seen_raw_fnames:
                warnings.append(f"Colliding custom feed name '{san}' will be disambiguated.")
            seen_raw_fnames.add(san)

    norm = normalize_manifest(data, ontology)

    # Validate ratio numbers
    ratio = norm.get("golden_ratio_distribution", {})
    ratio_vals: list[float] = []
    for k in (
        "aesthetic_leisure_target_pct",
        "cognitive_curiosity_target_pct",
        "domain_priming_target_pct",
    ):
        try:
            val = float(ratio.get(k, 0.0))
        except (TypeError, ValueError):
            errors.append(f"golden_ratio_distribution.{k} must be numeric.")
            val = 0.0
        if val < 0.0:
            errors.append(f"golden_ratio_distribution.{k} must be non-negative.")
        ratio_vals.append(val)
    ratio_sum = sum(ratio_vals)
    if ratio_sum <= 0.0:
        errors.append("golden_ratio_distribution target percentages must sum to > 0.")
    elif abs(ratio_sum - 100.0) > 1.0:
        warnings.append(
            f"golden_ratio_distribution percentages sum to {ratio_sum:.1f} instead of 100."
        )

    tier1 = norm["anchor_subreddits"] + norm["domain_priming_subreddits"]
    if not tier1:
        errors.append("Manifest contains no Tier 1 subreddits.")

    pillar_counts: dict[str, int] = {c: 0 for c in CATEGORIES}
    for entry in tier1:
        cat = entry.get("category")
        if cat in pillar_counts:
            pillar_counts[cat] += 1
    for cat, cnt in pillar_counts.items():
        if tier1 and cnt == 0:
            warnings.append(f"Missing '{cat}' pillar in Tier 1.")

    unrecognized: list[str] = []
    if ontology is not None:
        seen_unrec: set[str] = set()
        for entry in tier1:
            n = entry["name"]
            if ontology.get(n) is None and n.lower() not in seen_unrec:
                seen_unrec.add(n.lower())
                unrecognized.append(n)
        for feed in norm["custom_feeds"]:
            for n in feed["subreddits"]:
                if ontology.get(n) is None and n.lower() not in seen_unrec:
                    seen_unrec.add(n.lower())
                    unrecognized.append(n)
        if unrecognized:
            warnings.append(
                f"{len(unrecognized)} unrecognized subreddit(s) not in ontology: {', '.join(unrecognized[:5])}."
            )

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "normalized": norm,
        "summary": {
            "tier1_count": len(tier1),
            "anchor_count": len(norm["anchor_subreddits"]),
            "domain_priming_count": len(norm["domain_priming_subreddits"]),
            "custom_feed_count": len(norm["custom_feeds"]),
            "pillar_counts": pillar_counts,
            "unrecognized_subreddits": unrecognized,
        },
    }
