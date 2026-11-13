"""Information diet health auditor and rebalancing recommender."""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Sequence

from rcurator.engine.ontology import CATEGORIES, OntologyRepository, normalize_manifest
from rcurator.engine.optimizer import normalize_target_ratio, total_variation_drift

EPS: float = 1e-9
DEFAULT_UNRECOGNIZED_VELOCITY: float = 15.0
DEFAULT_UNRECOGNIZED_SIGNAL_DENSITY: float = 0.50


def _compute_gini(values: Sequence[float]) -> float:
    """Compute Gini concentration coefficient in [0.0, 1.0]."""
    n = len(values)
    if n <= 1:
        return 0.0
    sorted_vals = sorted(max(0.0, float(v)) for v in values)
    total = sum(sorted_vals)
    if total <= 0.0:
        return 0.0
    weighted_sum = sum((2 * (i + 1) - n - 1) * v for i, v in enumerate(sorted_vals))
    gini = weighted_sum / (n * total)
    return max(0.0, min(1.0, gini))


class FeedHealthAuditor:
    """Deterministic auditor measuring Golden Ratio drift, entropy, Gini, and dominance."""

    def __init__(self, ontology: OntologyRepository | None = None) -> None:
        self.ontology = ontology or OntologyRepository.load()

    def audit_manifest(self, manifest: dict[str, Any]) -> dict[str, Any]:
        """Audit a v1/v2 or legacy curation manifest."""
        norm = normalize_manifest(manifest, self.ontology)
        ratio_dict = norm.get("golden_ratio_distribution", {})
        target_ratio = (
            float(ratio_dict.get("aesthetic_leisure_target_pct", 40)),
            float(ratio_dict.get("cognitive_curiosity_target_pct", 35)),
            float(ratio_dict.get("domain_priming_target_pct", 25)),
        )

        entries: list[tuple[str, str | None]] = []
        seen: set[str] = set()
        for item in norm.get("anchor_subreddits", []) + norm.get(
            "domain_priming_subreddits", []
        ):
            name = str(item.get("name", "")).strip()
            cat = str(item.get("category", "")).strip() or None
            if name and name.lower() not in seen:
                seen.add(name.lower())
                entries.append((name, cat))

        return self._evaluate_entries(entries, target_ratio=target_ratio)

    def audit_live(
        self,
        subscribed_names: Sequence[str],
        target_ratio: tuple[float, float, float] | list[float] = (40, 35, 25),
    ) -> dict[str, Any]:
        """Audit a live list of subscribed subreddit names."""
        entries: list[tuple[str, str | None]] = []
        seen: set[str] = set()
        for raw in subscribed_names:
            name = str(raw).strip()
            if name and name.lower() not in seen:
                seen.add(name.lower())
                entries.append((name, None))
        return self._evaluate_entries(entries, target_ratio=target_ratio)

    def _evaluate_entries(
        self,
        entries: Sequence[tuple[str, str | None]],
        target_ratio: tuple[float, float, float] | list[float],
    ) -> dict[str, Any]:
        tau = normalize_target_ratio(target_ratio)
        target_pct = {c: round(tau[c] * 100.0, 2) for c in CATEGORIES}

        if not entries:
            return {
                "subreddit_count": 0,
                "total_daily_velocity": 0.0,
                "target_distribution_pct": target_pct,
                "actual_velocity_distribution_pct": {c: 0.0 for c in CATEGORIES},
                "pillar_counts": {c: 0 for c in CATEGORIES},
                "golden_ratio_tvd_drift": 1.0,
                "golden_ratio_fidelity_score": 0.0,
                "shannon_domain_entropy_norm": 0.0,
                "gini_velocity_concentration": 0.0,
                "max_subreddit_dominance_pct": 0.0,
                "dominant_subreddit": None,
                "mean_signal_density": 0.0,
                "composite_health_score": 0.0,
                "unrecognized_subreddits": [],
                "recommendations": [
                    "Feed is empty. Generate or apply a preset manifest to populate Tier 1 subscriptions."
                ],
                "per_subreddit_shares": [],
            }

        items: list[dict[str, Any]] = []
        unrecognized: list[str] = []
        pillar_counts: dict[str, int] = {c: 0 for c in CATEGORIES}
        pillar_vel: dict[str, float] = {c: 0.0 for c in CATEGORIES}
        domain_weights: dict[str, float] = defaultdict(float)

        total_velocity = 0.0
        weighted_signal_sum = 0.0

        for raw_name, fallback_cat in entries:
            rec = self.ontology.get(raw_name)
            if rec is not None:
                cname = rec.name
                cat = rec.category
                vel = rec.velocity
                sig = rec.signal_density
                domains = rec.domains
            else:
                unrecognized.append(raw_name)
                cname = raw_name
                cat = fallback_cat if fallback_cat in CATEGORIES else "unrecognized"
                vel = DEFAULT_UNRECOGNIZED_VELOCITY
                sig = DEFAULT_UNRECOGNIZED_SIGNAL_DENSITY
                domains = ()

            total_velocity += vel
            weighted_signal_sum += vel * sig
            if cat in CATEGORIES:
                pillar_counts[cat] += 1
                pillar_vel[cat] += vel

            if domains:
                per_dom = vel / len(domains)
                for d in domains:
                    domain_weights[d] += per_dom

            items.append(
                {
                    "name": cname,
                    "category": cat,
                    "estimated_daily_posts": vel,
                    "signal_density": sig,
                }
            )

        denom_total = max(total_velocity, EPS)
        per_subreddit_shares: list[dict[str, Any]] = []
        max_dom_pct = 0.0
        dominant_sub: str | None = None

        for item in items:
            share_pct = 100.0 * (item["estimated_daily_posts"] / denom_total)
            if share_pct > max_dom_pct:
                max_dom_pct = share_pct
                dominant_sub = item["name"]
            per_subreddit_shares.append(
                {
                    "name": item["name"],
                    "category": item["category"],
                    "estimated_daily_posts": round(item["estimated_daily_posts"], 2),
                    "impression_share_pct": round(share_pct, 2),
                    "signal_density": round(item["signal_density"], 4),
                }
            )

        per_subreddit_shares.sort(
            key=lambda x: (-x["impression_share_pct"], x["name"])
        )

        # Simplex distribution over the 3 canonical pillars
        pillar_vel_total = sum(pillar_vel.values())
        if pillar_vel_total > 0.0:
            p_vel = {c: pillar_vel[c] / pillar_vel_total for c in CATEGORIES}
            actual_pct = {c: round(p_vel[c] * 100.0, 2) for c in CATEGORIES}
            tvd = total_variation_drift(p_vel, tau)
            fidelity = max(0.0, min(100.0, 100.0 * (1.0 - tvd)))
        else:
            p_vel = {c: 0.0 for c in CATEGORIES}
            actual_pct = {c: 0.0 for c in CATEGORIES}
            tvd = 1.0
            fidelity = 0.0

        # Normalized Shannon domain entropy across ontology domains
        n_domains = max(len(self.ontology.domains), len(domain_weights))
        dom_total = sum(domain_weights.values())
        if n_domains > 1 and dom_total > 0.0:
            entropy = 0.0
            for w in domain_weights.values():
                if w > 0.0:
                    p = w / dom_total
                    entropy -= p * math.log(p)
            shannon_norm = max(0.0, min(1.0, entropy / math.log(n_domains)))
        else:
            shannon_norm = 0.0

        gini = _compute_gini([x["estimated_daily_posts"] for x in items])
        mean_signal = max(0.0, min(1.0, weighted_signal_sum / denom_total))

        # Composite health score in [0, 100]
        dominance_health = max(0.0, 100.0 * (1.0 - max(0.0, max_dom_pct - 25.0) / 75.0))
        gini_health = 100.0 * (1.0 - gini)
        balance_health = 0.60 * dominance_health + 0.40 * gini_health
        unknown_penalty = min(15.0, 3.0 * len(unrecognized))

        composite = (
            0.50 * fidelity
            + 0.25 * (100.0 * mean_signal)
            + 0.15 * balance_health
            + 0.10 * (100.0 * shannon_norm)
            - unknown_penalty
        )
        composite = max(0.0, min(100.0, composite))

        recommendations = self._build_recommendations(
            items=items,
            actual_pct=actual_pct,
            target_pct=target_pct,
            pillar_counts=pillar_counts,
            tvd=tvd,
            max_dom_pct=max_dom_pct,
            dominant_sub=dominant_sub,
            shannon_norm=shannon_norm,
            unrecognized=unrecognized,
        )

        return {
            "subreddit_count": len(items),
            "total_daily_velocity": round(total_velocity, 2),
            "target_distribution_pct": target_pct,
            "actual_velocity_distribution_pct": actual_pct,
            "pillar_counts": pillar_counts,
            "golden_ratio_tvd_drift": round(tvd, 6),
            "golden_ratio_fidelity_score": round(fidelity, 2),
            "shannon_domain_entropy_norm": round(shannon_norm, 6),
            "gini_velocity_concentration": round(gini, 6),
            "max_subreddit_dominance_pct": round(max_dom_pct, 2),
            "dominant_subreddit": dominant_sub,
            "mean_signal_density": round(mean_signal, 4),
            "composite_health_score": round(composite, 2),
            "unrecognized_subreddits": unrecognized,
            "recommendations": recommendations,
            "per_subreddit_shares": per_subreddit_shares,
        }

    def _build_recommendations(
        self,
        items: list[dict[str, Any]],
        actual_pct: dict[str, float],
        target_pct: dict[str, float],
        pillar_counts: dict[str, int],
        tvd: float,
        max_dom_pct: float,
        dominant_sub: str | None,
        shannon_norm: float,
        unrecognized: list[str],
    ) -> list[str]:
        recs: list[str] = []

        for cat in CATEGORIES:
            if pillar_counts.get(cat, 0) == 0:
                recs.append(
                    f"Missing '{cat}' pillar in Tier 1; add moderate-velocity '{cat}' communities to restore cognitive balance."
                )

        if max_dom_pct > 25.0 and dominant_sub:
            recs.append(
                f"r/{dominant_sub} accounts for {max_dom_pct:.1f}% of daily post velocity (>25% cap); consider moving it to a Tier 2 custom feed."
            )

        dom_diff = actual_pct["domain_priming"] - target_pct["domain_priming"]
        if dom_diff > 6.0:
            dom_subs = [x for x in items if x["category"] == "domain_priming"]
            if dom_subs:
                top_dom = max(dom_subs, key=lambda x: x["estimated_daily_posts"])
                recs.append(
                    f"domain_priming velocity is +{dom_diff:.1f}% above target; offload high-velocity r/{top_dom['name']} ({top_dom['estimated_daily_posts']:.0f} posts/day) to a private custom feed."
                )

        aes_diff = actual_pct["aesthetic_leisure"] - target_pct["aesthetic_leisure"]
        if aes_diff < -6.0:
            recs.append(
                f"aesthetic_leisure velocity is {aes_diff:.1f}% below target; add a healthy aesthetic anchor (e.g. unixporn, Fedora, or commandline)."
            )

        cog_diff = actual_pct["cognitive_curiosity"] - target_pct["cognitive_curiosity"]
        if cog_diff < -6.0:
            recs.append(
                f"cognitive_curiosity velocity is {cog_diff:.1f}% below target; add reflective communities (e.g. AskHistorians, DepthHub, or cogsci)."
            )

        if unrecognized:
            sample = ", ".join(f"r/{u}" for u in unrecognized[:5])
            recs.append(
                f"{len(unrecognized)} subreddit(s) not in curated ontology ({sample}); velocity/signal defaults were estimated."
            )

        if shannon_norm < 0.35 and len(items) >= 6:
            recs.append(
                f"Domain entropy is narrow ({shannon_norm:.2f}); broaden cognitive_curiosity or adjacent technical domains."
            )

        if not recs and tvd <= 0.08:
            recs.append(
                "Information diet is well-balanced across all three Golden Ratio pillars with healthy velocity dispersion."
            )

        return recs
