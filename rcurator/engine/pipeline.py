"""End-to-end deterministic AutoPresetGenerator pipeline."""

from __future__ import annotations

from typing import Any, Sequence

from rcurator.engine.explain import build_manifest_with_provenance
from rcurator.engine.graph import BipartitePPRScorer
from rcurator.engine.lexical import LexicalScorer
from rcurator.engine.ontology import (
    OntologyRepository,
    SubredditRecord,
    tokenize_technical_text,
)
from rcurator.engine.optimizer import GoldenRatioOptimizer

MIN_ONLINE_SUBSCRIBERS = 1500


def _normalize_evidences(
    evidences: Sequence[Any], known_phrases: frozenset[str]
) -> list[Any]:
    """Accept SignalEvidence objects, dicts, or raw query strings uniformly."""
    out: list[Any] = []
    for item in evidences:
        if isinstance(item, str):
            for tok, mult in tokenize_technical_text(
                item, known_phrases=known_phrases, include_subtokens=True, max_ngram=3
            ):
                out.append(
                    {
                        "term": tok,
                        "raw_term": tok,
                        "weight": 1.0 * mult,
                        "source_type": "query",
                        "source_detail": "query_text",
                        "signal_kind": "keyword",
                    }
                )
        else:
            out.append(item)
    return out


def _build_online_records(
    online_candidates: Sequence[dict[str, Any]] | None,
    ontology: OntologyRepository,
) -> list[SubredditRecord]:
    """Filter online Reddit search candidates (SFW, >= 1500 subscribers) into records."""
    if not online_candidates:
        return []

    extra: list[SubredditRecord] = []
    seen: set[str] = set()
    for raw in online_candidates:
        if not isinstance(raw, dict):
            continue
        if bool(raw.get("over18", False)):
            continue
        subs = int(raw.get("subscribers", 0) or 0)
        if subs < MIN_ONLINE_SUBSCRIBERS:
            continue

        raw_name = str(raw.get("name") or raw.get("display_name") or "").strip()
        if raw_name.startswith("/r/"):
            raw_name = raw_name[3:]
        elif raw_name.startswith("r/"):
            raw_name = raw_name[2:]
        raw_name = raw_name.strip("/")
        if not raw_name or raw_name in ontology or raw_name.lower() in seen:
            continue
        seen.add(raw_name.lower())

        title = str(raw.get("title") or "").strip()
        pub_desc = str(raw.get("public_description") or "").strip()
        combined_desc = f"{title}. {pub_desc}".strip(". ") or f"Online discovered r/{raw_name}"
        tokens = [
            tok
            for tok, _ in tokenize_technical_text(
                combined_desc, include_subtokens=False, max_ngram=2
            )
        ]
        kw = tuple(dict.fromkeys(tokens))[:12]
        est_vel = float(
            raw.get("estimated_daily_posts")
            or min(60.0, max(5.0, subs / 5000.0))
        )
        sig_density = float(raw.get("signal_density") or 0.76)

        extra.append(
            SubredditRecord(
                name=raw_name,
                category="domain_priming",
                domains=("domain_discovery",),
                estimated_daily_posts=max(0.5, est_vel),
                signal_density=max(0.1, min(1.0, sig_density)),
                keywords=kw,
                ecosystems=(),
                file_indicators=(),
                related_subreddits=(),
                description=combined_desc[:240],
                is_online_candidate=True,
                subscribers=subs,
            )
        )
    return extra


class AutoPresetGenerator:
    """Orchestrates lexical scoring, bipartite PPR, Golden Ratio optimization, and provenance."""

    def __init__(self, ontology: OntologyRepository | None = None) -> None:
        self.ontology = ontology or OntologyRepository.load()

    def generate(
        self,
        evidences: Sequence[Any],
        name: str = "Auto-Generated Curation",
        target_ratio: tuple[int, int, int] = (40, 35, 25),
        tier1_count: int = 18,
        online_candidates: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        extra_records = _build_online_records(online_candidates, self.ontology)
        working_ont = (
            self.ontology.extend_with(extra_records)
            if extra_records
            else self.ontology
        )

        norm_evidences = _normalize_evidences(evidences, working_ont.known_phrases)
        lexical_scorer = LexicalScorer(working_ont)
        lexical_res = lexical_scorer.score(norm_evidences)

        ppr_scorer = BipartitePPRScorer(working_ont, lexical_scorer)
        graph_res = ppr_scorer.score(lexical_res)

        optimizer = GoldenRatioOptimizer(working_ont)
        opt_res = optimizer.optimize(
            fused_relevance=graph_res.fused_relevance,
            lexical_scores=lexical_res.lexical_scores,
            ppr_scores=graph_res.ppr_norm,
            target_ratio=target_ratio,
            tier1_count=tier1_count,
        )

        sources_str = (
            ", ".join(lexical_res.query_profile.sources_used)
            if lexical_res.query_profile.sources_used
            else "cold-start quality baseline"
        )
        description = (
            f"Deterministically curated Golden Ratio diet ({sources_str}) "
            f"optimizing velocity-weighted impression balance across {len(opt_res.tier1_subreddits)} Tier 1 communities."
        )

        return build_manifest_with_provenance(
            ontology=working_ont,
            opt=opt_res,
            lexical_res=lexical_res,
            graph_res=graph_res,
            name=name,
            description=description,
        )
