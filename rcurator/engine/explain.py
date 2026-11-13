"""Manifest builder and per-subreddit explainability / provenance generator."""

from __future__ import annotations

from typing import Any

from rcurator.engine.graph import GraphResult
from rcurator.engine.lexical import LexicalResult
from rcurator.engine.ontology import CATEGORIES, OntologyRepository
from rcurator.engine.optimizer import OptimizationResult

ALGORITHM_ID = "bm25f_tfidf_bipartite_ppr_velocity_gr_v2"
EPS = 1e-9


def build_manifest_with_provenance(
    *,
    ontology: OntologyRepository,
    opt: OptimizationResult,
    lexical_res: LexicalResult | None,
    graph_res: GraphResult | None,
    name: str,
    description: str,
    sources_used_override: list[str] | None = None,
    top_terms_override: list[dict[str, Any]] | None = None,
    custom_source_breakdown: dict[str, dict[str, float]] | None = None,
    custom_routing_notes: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Assemble v1/v2 compatible manifest with complete curation_provenance (#9 §5)."""
    tier1_subs = opt.tier1_subreddits
    tier1_names = {s.name for s in tier1_subs}

    feed_by_sub: dict[str, list[str]] = {}
    tier2_names: set[str] = set()
    for feed in opt.custom_feeds:
        fname = feed["feed_name"]
        for sname in feed.get("subreddits", []):
            tier2_names.add(sname)
            feed_by_sub.setdefault(sname, []).append(fname)

    anchors = [
        {
            "name": s.name,
            "category": s.category,
            "description": s.record.description,
        }
        for s in tier1_subs
        if s.category in ("aesthetic_leisure", "cognitive_curiosity")
    ]
    priming = [
        {
            "name": s.name,
            "category": s.category,
            "description": s.record.description,
        }
        for s in tier1_subs
        if s.category == "domain_priming"
    ]

    target_pct = {
        "aesthetic_leisure_target_pct": round(
            opt.target_simplex["aesthetic_leisure"] * 100.0, 2
        ),
        "cognitive_curiosity_target_pct": round(
            opt.target_simplex["cognitive_curiosity"] * 100.0, 2
        ),
        "domain_priming_target_pct": round(
            opt.target_simplex["domain_priming"] * 100.0, 2
        ),
    }

    achieved_pct = {
        c: round(opt.achieved_simplex.get(c, 0.0) * 100.0, 2) for c in CATEGORIES
    }

    total_tier1_vel = max(opt.total_velocity, EPS)
    candidate_map = {c.name: c for c in tier1_subs}
    for c in opt.tier2_overflow:
        candidate_map.setdefault(c.name, c)

    explanations: dict[str, dict[str, Any]] = {}
    ordered_names = [s.name for s in tier1_subs] + [
        s.name for s in opt.tier2_overflow if s.name not in tier1_names
    ]

    for sname in ordered_names:
        cand = candidate_map[sname]
        in_t1 = sname in tier1_names
        in_t2 = sname in tier2_names
        if in_t1 and in_t2:
            tier = "both"
        elif in_t1:
            tier = "tier1_following"
        else:
            tier = "tier2_custom_feed"

        imp_share = 100.0 * (cand.velocity / total_tier1_vel) if in_t1 else 0.0
        matched = (
            lexical_res.matched_terms.get(sname, []) if lexical_res is not None else []
        )
        if custom_source_breakdown and sname in custom_source_breakdown:
            src_bd = custom_source_breakdown[sname]
        elif lexical_res is not None:
            src_bd = lexical_res.source_breakdown.get(sname, {})
        else:
            src_bd = {}

        src_arts = (
            lexical_res.source_artifacts.get(sname, [])
            if lexical_res is not None
            else []
        )
        bridges = (
            graph_res.graph_bridges.get(sname, []) if graph_res is not None else []
        )

        if custom_routing_notes and sname in custom_routing_notes:
            reason = custom_routing_notes[sname]
        else:
            reason = _build_routing_reason(
                cand=cand,
                tier=tier,
                imp_share=imp_share,
                feed_names=feed_by_sub.get(sname, []),
                matched=matched,
                bridges=bridges,
                cold_start=(graph_res.cold_start if graph_res else False),
            )

        explanations[sname] = {
            "name": sname,
            "category": cand.category,
            "tier": tier,
            "final_relevance": round(cand.final_relevance, 6),
            "lexical_score": round(cand.lexical_score, 6),
            "pagerank_score": round(cand.pagerank_score, 6),
            "signal_density": round(cand.record.signal_density, 4),
            "estimated_daily_posts": round(cand.velocity, 2),
            "impression_share_pct": round(imp_share, 2),
            "matched_terms": matched,
            "source_breakdown": src_bd,
            "source_artifacts": src_arts,
            "graph_bridges": bridges,
            "routing_reason": reason,
        }

    manifest: dict[str, Any] = {
        "version": "2.0.0",
        "name": name,
        "description": description,
        "feed_mode": "following",
        "golden_ratio_distribution": target_pct,
        "anchor_subreddits": anchors,
        "domain_priming_subreddits": priming,
        "custom_feeds": opt.custom_feeds,
    }

    from rcurator.analytics import FeedHealthAuditor

    auditor = FeedHealthAuditor(ontology)
    diet_health = auditor.audit_manifest(manifest)

    sources_used = (
        list(sources_used_override)
        if sources_used_override is not None
        else (list(lexical_res.query_profile.sources_used) if lexical_res else [])
    )
    top_terms = (
        list(top_terms_override)
        if top_terms_override is not None
        else (list(lexical_res.query_profile.top_terms) if lexical_res else [])
    )

    manifest["curation_provenance"] = {
        "algorithm": ALGORITHM_ID,
        "sources_used": sources_used,
        "top_extracted_terms": top_terms,
        "achieved_velocity_distribution_pct": achieved_pct,
        "diet_health": diet_health,
        "explanations": explanations,
    }
    return manifest


def _build_routing_reason(
    *,
    cand: Any,
    tier: str,
    imp_share: float,
    feed_names: list[str],
    matched: list[dict[str, Any]],
    bridges: list[str],
    cold_start: bool,
) -> str:
    if cold_start:
        if tier == "tier2_custom_feed":
            return (
                f"Cold-start quality prior routed r/{cand.name} ({cand.velocity:.0f} posts/day) "
                f"to Tier 2 custom feed {', '.join(feed_names)}."
            )
        return (
            f"Cold-start baseline selected r/{cand.name} ({cand.category}) by signal density "
            f"{cand.record.signal_density:.2f} ({imp_share:.1f}% Tier 1 share)."
        )

    signal_hint = (
        f"matched {', '.join(m['term'] for m in matched[:3])}"
        if matched
        else (f"activated via graph bridge ({bridges[0]})" if bridges else "quality-weighted pillar fit")
    )

    if tier == "both":
        return (
            f"Selected for Tier 1 Following feed ({cand.category}, {imp_share:.1f}% impression share) "
            f"and Tier 2 custom feed [{', '.join(feed_names)}]; {signal_hint}."
        )
    if tier == "tier2_custom_feed":
        return (
            f"High-relevance domain_priming candidate ({cand.velocity:.0f} posts/day) routed to "
            f"Tier 2 custom feed [{', '.join(feed_names)}] to preserve Tier 1 Golden Ratio balance; {signal_hint}."
        )
    return (
        f"Selected as Tier 1 Following anchor ({cand.category}, {imp_share:.1f}% impression share); {signal_hint}."
    )
