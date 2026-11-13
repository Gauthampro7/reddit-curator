"""Convex preset mixer across the 17 domain presets with graph proximity & GR optimization."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from rcurator.engine.explain import build_manifest_with_provenance
from rcurator.engine.graph import ANCHOR_EPSILON, BipartitePPRScorer
from rcurator.engine.lexical import LexicalResult, LexicalScorer, QueryProfile
from rcurator.engine.ontology import OntologyRepository, normalize_manifest
from rcurator.engine.optimizer import GoldenRatioOptimizer

DEFAULT_PRESETS_DIR = Path(__file__).resolve().parents[1] / "presets"
EPS = 1e-9


class PresetMixer:
    """Computes convex combinations of curated presets and re-optimizes via GoldenRatioOptimizer."""

    def __init__(
        self,
        ontology: OntologyRepository | None = None,
        presets_dir: Path | None = None,
    ) -> None:
        self.ontology = ontology or OntologyRepository.load()
        self.presets_dir = Path(presets_dir) if presets_dir is not None else DEFAULT_PRESETS_DIR

    def _resolve_preset_path(self, preset_name: str) -> Path:
        raw = str(preset_name).strip()
        if not raw:
            raise ValueError("Preset name cannot be empty")
        if raw.endswith(".json"):
            raw = raw[:-5]
        if "/" in raw or "\\" in raw or ".." in raw:
            raise ValueError(f"Invalid preset name: {preset_name!r}")

        base_dir = self.presets_dir.resolve()
        candidate = (base_dir / f"{raw}.json").resolve()
        if not candidate.is_relative_to(base_dir):
            raise ValueError(f"Preset path escapes presets directory: {preset_name!r}")
        if not candidate.is_file():
            raise FileNotFoundError(f"Preset not found: {raw!r} in {base_dir}")
        return candidate

    def mix(
        self,
        weights: dict[str, float],
        target_ratio: tuple[int, int, int] = (40, 35, 25),
        tier1_count: int = 18,
    ) -> dict[str, Any]:
        if not weights:
            raise ValueError("weights must contain at least one preset")

        positive: dict[str, float] = {}
        for k, v in weights.items():
            val = float(v)
            if val > 0.0:
                clean_k = str(k).strip()
                if clean_k.endswith(".json"):
                    clean_k = clean_k[:-5]
                positive[clean_k] = positive.get(clean_k, 0.0) + val

        total_w = sum(positive.values())
        if total_w <= 0.0:
            raise ValueError("Total preset mix weight must be > 0")

        norm_weights = {k: v / total_w for k, v in sorted(positive.items())}

        # Load and normalize each preset
        preset_manifests: dict[str, dict[str, Any]] = {}
        for pname in norm_weights:
            path = self._resolve_preset_path(pname)
            raw_data = json.loads(path.read_text(encoding="utf-8"))
            preset_manifests[pname] = normalize_manifest(raw_data, self.ontology)

        # Direct convex membership scores & per-preset attribution
        direct_scores: dict[str, float] = defaultdict(float)
        per_sub_preset_breakdown: dict[str, dict[str, float]] = defaultdict(dict)

        for pname, pw in norm_weights.items():
            manifest = preset_manifests[pname]
            sub_ranks: dict[str, float] = {}

            for idx, item in enumerate(manifest.get("anchor_subreddits", [])):
                sname = self.ontology.canonical_name(item["name"])
                rank_w = 1.0 / (1.0 + 0.04 * idx)
                sub_ranks[sname] = max(sub_ranks.get(sname, 0.0), rank_w)

            for idx, item in enumerate(manifest.get("domain_priming_subreddits", [])):
                sname = self.ontology.canonical_name(item["name"])
                rank_w = 1.0 / (1.0 + 0.04 * idx)
                sub_ranks[sname] = max(sub_ranks.get(sname, 0.0), rank_w)

            for feed in manifest.get("custom_feeds", []):
                for idx, sraw in enumerate(feed.get("subreddits", [])):
                    sname = self.ontology.canonical_name(str(sraw))
                    rank_w = 0.65 / (1.0 + 0.04 * idx)
                    sub_ranks[sname] = max(sub_ranks.get(sname, 0.0), rank_w)

            for sname, rw in sub_ranks.items():
                contrib = pw * rw
                direct_scores[sname] += contrib
                per_sub_preset_breakdown[sname][pname] = round(contrib, 6)

        max_direct = max(direct_scores.values()) if direct_scores else 0.0
        direct_norm = {
            r.name: direct_scores.get(r.name, 0.0) / max(max_direct, EPS)
            for r in self.ontology.all_subreddits()
        }

        # Seed Personalized PageRank with ontology concepts + direct preset membership
        lexical_scorer = LexicalScorer(self.ontology)
        synthetic_evidences: list[dict[str, Any]] = []
        for pname, pw in norm_weights.items():
            for sname in per_sub_preset_breakdown:
                if pname in per_sub_preset_breakdown[sname]:
                    rec = self.ontology.get(sname)
                    if rec is None:
                        continue
                    for term in (*rec.ecosystems[:4], *rec.keywords[:4], *rec.domains[:2]):
                        synthetic_evidences.append(
                            {
                                "term": term,
                                "raw_term": term,
                                "weight": pw * per_sub_preset_breakdown[sname][pname],
                                "source_type": "query",
                                "source_detail": f"preset:{pname}",
                                "signal_kind": "keyword",
                            }
                        )

        lex_res = lexical_scorer.score(synthetic_evidences)
        # Override lexical_scores inside PPR seed with convex direct_norm so preset members anchor PPR
        blended_lex_seed = {
            sname: 0.75 * direct_norm.get(sname, 0.0)
            + 0.25 * lex_res.lexical_scores.get(sname, 0.0)
            for sname in direct_norm
        }
        seeded_lex_res = LexicalResult(
            query_profile=lex_res.query_profile,
            bm25f_norm=lex_res.bm25f_norm,
            tfidf_norm=lex_res.tfidf_norm,
            lexical_scores=blended_lex_seed,
            matched_terms=lex_res.matched_terms,
            source_breakdown=lex_res.source_breakdown,
            source_artifacts=lex_res.source_artifacts,
        )

        ppr_scorer = BipartitePPRScorer(self.ontology, lexical_scorer)
        graph_res = ppr_scorer.score(seeded_lex_res)

        # Fused mix relevance combining convex preset membership + graph proximity
        fused_mix_relevance: dict[str, float] = {}
        for rec in self.ontology.all_subreddits():
            rho = rec.signal_density
            eps_c = (
                ANCHOR_EPSILON * rho
                if rec.category in ("aesthetic_leisure", "cognitive_curiosity")
                else 0.0
            )
            d_score = direct_norm.get(rec.name, 0.0)
            p_score = graph_res.ppr_norm.get(rec.name, 0.0)
            fused_mix_relevance[rec.name] = (0.65 * d_score + 0.35 * p_score + eps_c) * (
                0.70 + 0.30 * rho
            )

        optimizer = GoldenRatioOptimizer(self.ontology)
        opt_res = optimizer.optimize(
            fused_relevance=fused_mix_relevance,
            lexical_scores=direct_norm,
            ppr_scores=graph_res.ppr_norm,
            target_ratio=target_ratio,
            tier1_count=tier1_count,
        )

        mix_label = " + ".join(
            f"{pname} ({pw * 100:.0f}%)" for pname, pw in norm_weights.items()
        )
        top_terms_override = [
            {
                "term": f"preset:{pname}",
                "weight": round(pw, 6),
                "sources": ["preset_mixer"],
            }
            for pname, pw in sorted(norm_weights.items(), key=lambda x: (-x[1], x[0]))
        ]

        routing_notes: dict[str, str] = {}
        for cand in opt_res.tier1_subreddits + opt_res.tier2_overflow:
            bd = per_sub_preset_breakdown.get(cand.name, {})
            if bd:
                presets_str = ", ".join(
                    f"{p} ({w:.2f})" for p, w in sorted(bd.items(), key=lambda x: (-x[1], x[0]))
                )
                routing_notes[cand.name] = (
                    f"Blended from [{presets_str}] via convex preset mix and Golden Ratio velocity optimization."
                )
            else:
                bridges = graph_res.graph_bridges.get(cand.name, [])
                hint = bridges[0] if bridges else "ontology graph proximity"
                routing_notes[cand.name] = (
                    f"Discovered as cross-preset bridge via {hint} to optimize Golden Ratio velocity balance."
                )

        return build_manifest_with_provenance(
            ontology=self.ontology,
            opt=opt_res,
            lexical_res=seeded_lex_res,
            graph_res=graph_res,
            name=f"Mixed Diet: {mix_label}",
            description=f"Convex blend of presets [{mix_label}] balanced via Golden Ratio optimizer.",
            sources_used_override=["preset_mixer"],
            top_terms_override=top_terms_override,
            custom_source_breakdown=dict(per_sub_preset_breakdown),
            custom_routing_notes=routing_notes,
        )
