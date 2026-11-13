"""Velocity-weighted Golden Ratio constrained optimizer and Tier-2 custom feed router."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from rcurator.engine.ontology import (
    CATEGORIES,
    OntologyRepository,
    SubredditRecord,
    sanitize_feed_name,
)

EPS: float = 1e-9
MU_JS: float = 16.0
MU_DOM: float = 30.0
DOMINANCE_SOFT_CAP: float = 0.25
DOMINANCE_HARD_CAP: float = 0.50
RELEVANCE_RETENTION: float = 0.95
MAX_CUSTOM_FEED_SUBS: int = 100


@dataclass(frozen=True)
class CandidateScore:
    """Candidate subreddit prepared for Golden Ratio optimization."""

    record: SubredditRecord
    final_relevance: float
    pillar_norm_relevance: float
    lexical_score: float
    pagerank_score: float

    @property
    def name(self) -> str:
        return self.record.name

    @property
    def category(self) -> str:
        return self.record.category

    @property
    def velocity(self) -> float:
        return self.record.velocity


@dataclass
class OptimizationResult:
    """Outputs of GoldenRatioOptimizer including Tier-1 Following and Tier-2 Custom Feeds."""

    tier1_subreddits: list[CandidateScore]
    tier2_overflow: list[CandidateScore]
    custom_feeds: list[dict[str, Any]]
    target_simplex: dict[str, float]
    achieved_simplex: dict[str, float]
    tvd_drift: float
    fidelity_score: float
    max_dominance: float
    total_velocity: float
    greedy_relevance_sum: float
    final_relevance_sum: float


def normalize_target_ratio(
    target_ratio: tuple[float, float, float] | list[float],
) -> dict[str, float]:
    """Normalize (aes, cog, dom) ratio to a valid probability simplex."""
    if len(target_ratio) != 3:
        raise ValueError("target_ratio must have exactly 3 entries (aes, cog, dom)")
    vals = [max(0.0, float(x)) for x in target_ratio]
    total = sum(vals)
    if total <= 0.0:
        return {
            "aesthetic_leisure": 0.40,
            "cognitive_curiosity": 0.35,
            "domain_priming": 0.25,
        }
    return {
        "aesthetic_leisure": vals[0] / total,
        "cognitive_curiosity": vals[1] / total,
        "domain_priming": vals[2] / total,
    }


def compute_velocity_simplex(
    selected: list[CandidateScore],
) -> tuple[dict[str, float], float, float]:
    """Compute velocity distribution P_v(X), total velocity V(X), and max dominance."""
    if not selected:
        return (
            {"aesthetic_leisure": 0.0, "cognitive_curiosity": 0.0, "domain_priming": 0.0},
            0.0,
            0.0,
        )
    cat_vel: dict[str, float] = {c: 0.0 for c in CATEGORIES}
    total_v = 0.0
    max_v = 0.0
    for item in selected:
        v = item.velocity
        cat_vel[item.category] = cat_vel.get(item.category, 0.0) + v
        total_v += v
        if v > max_v:
            max_v = v

    denom = max(total_v, EPS)
    simplex = {c: cat_vel.get(c, 0.0) / denom for c in CATEGORIES}
    max_dom = max_v / denom
    return simplex, total_v, max_dom


def total_variation_drift(
    p_vel: dict[str, float], tau: dict[str, float]
) -> float:
    """Simplex-bounded Total Variation Distance in [0, 1] (#9 §1)."""
    tvd = 0.5 * sum(abs(p_vel.get(c, 0.0) - tau.get(c, 0.0)) for c in CATEGORIES)
    return max(0.0, min(1.0, tvd))


def jensen_shannon_divergence(
    p_vel: dict[str, float], tau: dict[str, float]
) -> float:
    """Jensen-Shannon divergence D_JS(P_v || tau) in [0, 1] using base-2 log."""
    js = 0.0
    for c in CATEGORIES:
        p = max(0.0, p_vel.get(c, 0.0))
        q = max(0.0, tau.get(c, 0.0))
        m = 0.5 * (p + q)
        if m <= 0.0:
            continue
        if p > 0.0:
            js += 0.5 * p * math.log2(p / m)
        if q > 0.0:
            js += 0.5 * q * math.log2(q / m)
    return max(0.0, min(1.0, js))


def _evaluate_objective(
    selected: list[CandidateScore], tau: dict[str, float]
) -> float:
    """Compute J(X) = sum R_tilde - mu_JS * D_JS - mu_dom * sum max(0, share - 0.25)^2."""
    if not selected:
        return -1e9
    rel_sum = sum(s.pillar_norm_relevance for s in selected)
    p_vel, total_v, _ = compute_velocity_simplex(selected)
    js = jensen_shannon_divergence(p_vel, tau)
    denom = max(total_v, EPS)
    dom_pen = sum(
        max(0.0, (s.velocity / denom) - DOMINANCE_SOFT_CAP) ** 2 for s in selected
    )
    return rel_sum - MU_JS * js - MU_DOM * dom_pen


class GoldenRatioOptimizer:
    """Deterministic constrained optimizer for Tier-1 Following + Tier-2 Custom Feeds."""

    def __init__(self, ontology: OntologyRepository) -> None:
        self.ontology = ontology

    def optimize(
        self,
        fused_relevance: dict[str, float],
        lexical_scores: dict[str, float],
        ppr_scores: dict[str, float],
        target_ratio: tuple[float, float, float] | list[float] = (40, 35, 25),
        tier1_count: int = 18,
    ) -> OptimizationResult:
        if int(tier1_count) < 3:
            raise ValueError(f"tier1_count must be >= 3, got {tier1_count}")

        tau = normalize_target_ratio(target_ratio)
        all_records = self.ontology.all_subreddits()
        k_target = min(int(tier1_count), len(all_records))
        if k_target < 3:
            raise ValueError(
                f"Ontology has only {len(all_records)} subreddits; need at least 3"
            )

        # Compute per-pillar max relevance for fair pillar normalization
        max_rel_by_cat: dict[str, float] = {c: 0.0 for c in CATEGORIES}
        for rec in all_records:
            rel = max(0.0, float(fused_relevance.get(rec.name, 0.0)))
            if rel > max_rel_by_cat.get(rec.category, 0.0):
                max_rel_by_cat[rec.category] = rel

        candidates: list[CandidateScore] = []
        by_cat: dict[str, list[CandidateScore]] = {c: [] for c in CATEGORIES}
        for rec in all_records:
            rel = max(0.0, float(fused_relevance.get(rec.name, 0.0)))
            denom = max(max_rel_by_cat.get(rec.category, 0.0), EPS)
            cand = CandidateScore(
                record=rec,
                final_relevance=rel,
                pillar_norm_relevance=rel / denom,
                lexical_score=float(lexical_scores.get(rec.name, 0.0)),
                pagerank_score=float(ppr_scores.get(rec.name, 0.0)),
            )
            candidates.append(cand)
            if rec.category in by_cat:
                by_cat[rec.category].append(cand)

        for cat in CATEGORIES:
            by_cat[cat].sort(
                key=lambda x: (-x.pillar_norm_relevance, x.velocity, x.name)
            )

        # 1. Seed m_seed = max(1, min(2, floor(K/3))) moderate-velocity seeds per pillar
        m_seed = max(1, min(2, k_target // 3))
        selected = self._select_initial_seeds(by_cat, m_seed, k_target)
        selected_names = {s.name for s in selected}

        # 2. Greedy marginal-gain expansion up to k_target (incremental O(1) per trial)
        remaining = [c for c in candidates if c.name not in selected_names]
        remaining.sort(key=lambda x: (-x.pillar_norm_relevance, x.velocity, x.name))

        cur_cat_vel: dict[str, float] = {c: 0.0 for c in CATEGORIES}
        cur_total_v = 0.0
        cur_rel_sum = 0.0
        cur_max_v = 0.0
        for s in selected:
            cur_cat_vel[s.category] = cur_cat_vel.get(s.category, 0.0) + s.velocity
            cur_total_v += s.velocity
            cur_rel_sum += s.pillar_norm_relevance
            if s.velocity > cur_max_v:
                cur_max_v = s.velocity

        while len(selected) < k_target and remaining:
            cur_dom = cur_max_v / max(cur_total_v, EPS)
            best_cand: CandidateScore | None = None
            best_obj = -1e18
            best_feasible = False
            best_trial_dom = 1e9

            for cand in remaining:
                v_c = cand.velocity
                new_total_v = cur_total_v + v_c
                new_max_v = max(cur_max_v, v_c)
                trial_dom = new_max_v / max(new_total_v, EPS)
                is_feasible = trial_dom <= DOMINANCE_HARD_CAP + 1e-12

                # O(1) objective evaluation
                p_vel = {
                    c: (
                        cur_cat_vel[c] + (v_c if cand.category == c else 0.0)
                    ) / max(new_total_v, EPS)
                    for c in CATEGORIES
                }
                js = jensen_shannon_divergence(p_vel, tau)
                denom = max(new_total_v, EPS)
                dom_pen = max(0.0, (v_c / denom) - DOMINANCE_SOFT_CAP) ** 2
                for s in selected:
                    dom_pen += max(0.0, (s.velocity / denom) - DOMINANCE_SOFT_CAP) ** 2
                obj = (
                    (cur_rel_sum + cand.pillar_norm_relevance)
                    - MU_JS * js
                    - MU_DOM * dom_pen
                )

                if is_feasible and not best_feasible:
                    best_feasible = True
                    best_obj = obj
                    best_cand = cand
                    best_trial_dom = trial_dom
                elif is_feasible == best_feasible:
                    if not is_feasible and cur_dom > DOMINANCE_HARD_CAP:
                        if best_cand is None or (-trial_dom, obj) > (
                            -best_trial_dom,
                            best_obj,
                        ):
                            best_obj = obj
                            best_cand = cand
                            best_trial_dom = trial_dom
                    else:
                        if best_cand is None or obj > best_obj + 1e-12:
                            best_obj = obj
                            best_cand = cand
                            best_trial_dom = trial_dom

            if best_cand is None:
                break
            selected.append(best_cand)
            selected_names.add(best_cand.name)
            cur_cat_vel[best_cand.category] = (
                cur_cat_vel.get(best_cand.category, 0.0) + best_cand.velocity
            )
            cur_total_v += best_cand.velocity
            cur_rel_sum += best_cand.pillar_norm_relevance
            if best_cand.velocity > cur_max_v:
                cur_max_v = best_cand.velocity
            remaining = [c for c in remaining if c.name != best_cand.name]

        greedy_rel_sum = cur_rel_sum

        # 3. Deterministic 1-swap steepest-ascent local search minimizing TVD Drift
        selected = self._local_search_1swap(
            selected=selected,
            all_candidates=candidates,
            tau=tau,
            min_per_pillar=1 if k_target < 6 else m_seed,
            min_relevance_sum=RELEVANCE_RETENTION * greedy_rel_sum,
        )

        # Sort Tier 1 cleanly by category then relevance descending
        cat_order = {c: idx for idx, c in enumerate(CATEGORIES)}
        selected.sort(
            key=lambda x: (
                cat_order.get(x.category, 99),
                -x.final_relevance,
                x.name,
            )
        )

        p_vel, total_v, max_dom = compute_velocity_simplex(selected)
        tvd = total_variation_drift(p_vel, tau)
        fidelity = max(0.0, min(100.0, 100.0 * (1.0 - tvd)))

        # 4. Route Tier-1 domain_priming + high-relevance overflow into 1-3 custom feeds
        tier1_names = {s.name for s in selected}
        tier1_dom = [s for s in selected if s.category == "domain_priming"]
        overflow_pool = [
            c
            for c in by_cat.get("domain_priming", [])
            if c.name not in tier1_names
        ]
        # Keep overflow candidates that are genuinely relevant (or top 12 if signal is sparse)
        max_dom_rel = max((c.final_relevance for c in by_cat.get("domain_priming", [])), default=0.0)
        tier2_overflow = [
            c
            for idx, c in enumerate(overflow_pool)
            if (max_dom_rel > 0.0 and c.final_relevance >= 0.30 * max_dom_rel and idx < 18)
            or idx < 8
        ][:18]

        custom_feeds = self._build_custom_feeds(tier1_dom + tier2_overflow)

        return OptimizationResult(
            tier1_subreddits=selected,
            tier2_overflow=tier2_overflow,
            custom_feeds=custom_feeds,
            target_simplex=tau,
            achieved_simplex=p_vel,
            tvd_drift=tvd,
            fidelity_score=fidelity,
            max_dominance=max_dom,
            total_velocity=total_v,
            greedy_relevance_sum=greedy_rel_sum,
            final_relevance_sum=sum(s.pillar_norm_relevance for s in selected),
        )

    def _select_initial_seeds(
        self,
        by_cat: dict[str, list[CandidateScore]],
        m_seed: int,
        k_target: int,
    ) -> list[CandidateScore]:
        """Select m_seed moderate-velocity high-relevance seeds per pillar."""
        seeds: list[CandidateScore] = []
        used: set[str] = set()

        # Median velocity per pillar to guard against single extreme-velocity seed
        med_vel: dict[str, float] = {}
        for cat in CATEGORIES:
            vels = sorted(c.velocity for c in by_cat.get(cat, []))
            med_vel[cat] = vels[len(vels) // 2] if vels else 25.0

        for cat in CATEGORIES:
            pool = by_cat.get(cat, [])
            if not pool:
                continue
            # Prefer moderate velocity among top relevance candidates for small-K stability
            vel_cap = max(35.0, 2.5 * med_vel[cat])
            moderate = [c for c in pool if c.velocity <= vel_cap]
            chosen_pool = moderate if len(moderate) >= m_seed else pool
            for cand in chosen_pool[:m_seed]:
                if cand.name not in used and len(seeds) < k_target:
                    seeds.append(cand)
                    used.add(cand.name)

        # Repair hard dominance cap if any seed violates > 50% share
        for _ in range(10):
            _, total_v, max_dom = compute_velocity_simplex(seeds)
            if max_dom <= DOMINANCE_HARD_CAP + 1e-12 or total_v <= 0.0:
                break
            dominant = max(seeds, key=lambda s: s.velocity)
            dom_idx = seeds.index(dominant)
            cat_pool = [
                c
                for c in by_cat.get(dominant.category, [])
                if c.name not in used and c.velocity < dominant.velocity
            ]
            if not cat_pool:
                break
            # Pick highest-relevance replacement that reduces dominance (or satisfies cap)
            replacement = None
            for cand in cat_pool:
                trial = list(seeds)
                trial[dom_idx] = cand
                _, _, t_dom = compute_velocity_simplex(trial)
                if t_dom <= DOMINANCE_HARD_CAP + 1e-12:
                    replacement = cand
                    break
            if replacement is None:
                replacement = min(cat_pool[:10], key=lambda c: c.velocity)
            used.discard(dominant.name)
            seeds[dom_idx] = replacement
            used.add(replacement.name)

        return seeds

    def _local_search_1swap(
        self,
        selected: list[CandidateScore],
        all_candidates: list[CandidateScore],
        tau: dict[str, float],
        min_per_pillar: int,
        min_relevance_sum: float,
    ) -> list[CandidateScore]:
        """Deterministic 1-swap steepest-ascent search minimizing TVD Drift."""
        cur = list(selected)
        cur_names = {s.name for s in cur}

        tau_aes = tau["aesthetic_leisure"]
        tau_cog = tau["cognitive_curiosity"]
        tau_dom = tau["domain_priming"]

        for _ in range(50):
            cur_cat_vel = {c: 0.0 for c in CATEGORIES}
            cat_counts = {c: 0 for c in CATEGORIES}
            cur_total_v = 0.0
            cur_rel_sum = 0.0
            for s in cur:
                cur_cat_vel[s.category] += s.velocity
                cat_counts[s.category] += 1
                cur_total_v += s.velocity
                cur_rel_sum += s.pillar_norm_relevance

            # Top-2 velocities so max_v without leaving item is O(1)
            sorted_v = sorted((s.velocity for s in cur), reverse=True)
            top1_v = sorted_v[0] if sorted_v else 0.0
            top2_v = sorted_v[1] if len(sorted_v) > 1 else 0.0

            cur_denom = max(cur_total_v, EPS)
            cur_tvd = 0.5 * (
                abs(cur_cat_vel["aesthetic_leisure"] / cur_denom - tau_aes)
                + abs(cur_cat_vel["cognitive_curiosity"] / cur_denom - tau_cog)
                + abs(cur_cat_vel["domain_priming"] / cur_denom - tau_dom)
            )
            cur_dom = top1_v / cur_denom
            cur_dom_pen = sum(
                max(0.0, (s.velocity / cur_denom) - DOMINANCE_SOFT_CAP) ** 2
                for s in cur
            )
            cur_score = (
                cur_tvd + 0.05 * cur_dom_pen - 1e-4 * cur_rel_sum,
                cur_dom,
                -cur_rel_sum,
            )

            best_swap: tuple[int, CandidateScore] | None = None
            best_score = cur_score

            outside = [c for c in all_candidates if c.name not in cur_names]
            for out_idx, leaving in enumerate(cur):
                v_l = leaving.velocity
                cat_l = leaving.category
                rel_l = leaving.pillar_norm_relevance
                rem_max_v = top2_v if v_l == top1_v else top1_v
                base_aes = cur_cat_vel["aesthetic_leisure"] - (
                    v_l if cat_l == "aesthetic_leisure" else 0.0
                )
                base_cog = cur_cat_vel["cognitive_curiosity"] - (
                    v_l if cat_l == "cognitive_curiosity" else 0.0
                )
                base_dom = cur_cat_vel["domain_priming"] - (
                    v_l if cat_l == "domain_priming" else 0.0
                )
                base_total_v = cur_total_v - v_l
                base_rel_sum = cur_rel_sum - rel_l
                can_leave_cat = cat_counts.get(cat_l, 0) - 1 >= min_per_pillar

                for entering in outside:
                    cat_e = entering.category
                    if cat_e != cat_l and not can_leave_cat:
                        continue

                    t_rel_sum = base_rel_sum + entering.pillar_norm_relevance
                    if t_rel_sum + 1e-12 < min_relevance_sum:
                        continue

                    v_e = entering.velocity
                    t_total_v = base_total_v + v_e
                    t_denom = max(t_total_v, EPS)
                    t_max_v = max(rem_max_v, v_e)
                    t_dom = t_max_v / t_denom

                    if cur_dom <= DOMINANCE_HARD_CAP + 1e-12:
                        if t_dom > DOMINANCE_HARD_CAP + 1e-12:
                            continue
                    elif t_dom >= cur_dom:
                        continue

                    t_aes = base_aes + (v_e if cat_e == "aesthetic_leisure" else 0.0)
                    t_cog = base_cog + (v_e if cat_e == "cognitive_curiosity" else 0.0)
                    t_dom_v = base_dom + (v_e if cat_e == "domain_priming" else 0.0)
                    t_tvd = 0.5 * (
                        abs(t_aes / t_denom - tau_aes)
                        + abs(t_cog / t_denom - tau_cog)
                        + abs(t_dom_v / t_denom - tau_dom)
                    )

                    if not (
                        t_tvd < cur_tvd - 1e-9 or cur_dom > DOMINANCE_HARD_CAP + 1e-12
                    ):
                        continue

                    t_dom_pen = max(0.0, (v_e / t_denom) - DOMINANCE_SOFT_CAP) ** 2
                    for j, s in enumerate(cur):
                        if j != out_idx:
                            t_dom_pen += (
                                max(0.0, (s.velocity / t_denom) - DOMINANCE_SOFT_CAP) ** 2
                            )

                    t_score = (
                        t_tvd + 0.05 * t_dom_pen - 1e-4 * t_rel_sum,
                        t_dom,
                        -t_rel_sum,
                    )
                    if t_score < best_score:
                        best_score = t_score
                        best_swap = (out_idx, entering)

            if best_swap is None:
                break

            idx, entering = best_swap
            cur_names.discard(cur[idx].name)
            cur[idx] = entering
            cur_names.add(entering.name)

        return cur

    def _build_custom_feeds(
        self, domain_candidates: list[CandidateScore]
    ) -> list[dict[str, Any]]:
        """Route Tier-1 domain_priming + overflow candidates into 1-3 private feeds."""
        if not domain_candidates:
            return []

        deduped: list[CandidateScore] = []
        seen: set[str] = set()
        for c in sorted(
            domain_candidates, key=lambda x: (-x.final_relevance, x.name)
        ):
            low = c.name.lower()
            if low not in seen:
                seen.add(low)
                deduped.append(c)

        grouped: dict[str, list[CandidateScore]] = defaultdict(list)
        for c in deduped:
            primary_dom = c.record.domains[0] if c.record.domains else "domain_focus"
            grouped[primary_dom].append(c)

        # Keep top 3 thematic groups by total relevance; merge remainder into top group
        ranked_domains = sorted(
            grouped.keys(),
            key=lambda d: (-sum(x.final_relevance for x in grouped[d]), d),
        )
        top_domains = ranked_domains[:3]
        if len(ranked_domains) > 3:
            primary_bucket = top_domains[0]
            for extra_dom in ranked_domains[3:]:
                grouped[primary_bucket].extend(grouped[extra_dom])

        feeds: list[dict[str, Any]] = []
        used_feed_names: set[str] = set()

        for dom in top_domains:
            items = sorted(
                grouped[dom], key=lambda x: (-x.final_relevance, x.name)
            )
            sub_names = [x.name for x in items]
            base_name = sanitize_feed_name(dom, fallback="domain_focus")
            display_title = dom.replace("_", " ").title()

            # Partition into <= 100 subreddit chunks per Reddit API constraint
            chunks = [
                sub_names[i : i + MAX_CUSTOM_FEED_SUBS]
                for i in range(0, len(sub_names), MAX_CUSTOM_FEED_SUBS)
            ]
            for chunk_idx, chunk in enumerate(chunks, start=1):
                if chunk_idx == 1 and base_name not in used_feed_names:
                    fname = base_name
                else:
                    suffix = f"_{chunk_idx}"
                    fname = sanitize_feed_name(
                        f"{base_name[: 50 - len(suffix)]}{suffix}"
                    )
                    while fname in used_feed_names:
                        chunk_idx += 1
                        suffix = f"_{chunk_idx}"
                        fname = sanitize_feed_name(
                            f"{base_name[: 50 - len(suffix)]}{suffix}"
                        )
                used_feed_names.add(fname)
                feeds.append(
                    {
                        "feed_name": fname,
                        "display_name": (
                            display_title
                            if chunk_idx == 1
                            else f"{display_title} ({chunk_idx})"
                        ),
                        "description_md": (
                            f"Private distraction-free domain feed for {display_title.lower()} "
                            f"({len(chunk)} high-signal communities)."
                        ),
                        "icon_name": "programming",
                        "visibility": "private",
                        "subreddits": chunk,
                    }
                )

        return feeds
