"""Multi-source synergy query construction and Fielded BM25F + TF-IDF lexical scorer."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Sequence

from rcurator.engine.ontology import (
    OntologyRepository,
    SubredditRecord,
    canonicalize_token,
    tokenize_technical_text,
)

SOURCE_PRIORS: dict[str, float] = {
    "workspace": 1.0,
    "resume": 1.0,
    "github": 0.9,
    "query": 1.1,
}
BETA_SYN: float = 0.25

FIELD_WEIGHTS: dict[str, float] = {
    "name": 4.0,
    "ecosystems": 3.5,
    "file_indicators": 3.2,
    "keywords": 2.5,
    "domains": 2.0,
    "description": 1.0,
}

FIELD_B: dict[str, float] = {
    "name": 0.4,
    "ecosystems": 0.4,
    "file_indicators": 0.4,
    "keywords": 0.4,
    "domains": 0.4,
    "description": 0.75,
}

BM25F_K1: float = 1.5
EPS: float = 1e-9


@dataclass(frozen=True)
class QueryProfile:
    """Fused multi-source query vector with per-term provenance."""

    weights: dict[str, float]
    sources_used: tuple[str, ...]
    term_sources: dict[str, tuple[str, ...]]
    term_source_shares: dict[str, dict[str, float]]
    term_artifacts: dict[str, tuple[str, ...]]
    top_terms: list[dict[str, Any]]


@dataclass
class LexicalResult:
    """Outputs of the Fielded BM25F + TF-IDF hybrid scorer."""

    query_profile: QueryProfile
    bm25f_norm: dict[str, float]
    tfidf_norm: dict[str, float]
    lexical_scores: dict[str, float]
    matched_terms: dict[str, list[dict[str, Any]]]
    source_breakdown: dict[str, dict[str, float]]
    source_artifacts: dict[str, list[str]]


def _extract_evidence_fields(item: Any) -> tuple[str, float, str, str]:
    """Read (term, weight, source_type, source_detail) from SignalEvidence or dict."""
    if isinstance(item, dict):
        term = str(item.get("term") or item.get("raw_term") or "")
        weight = float(item.get("weight", 1.0) or 0.0)
        source_type = str(item.get("source_type") or "query").strip().lower()
        source_detail = str(item.get("source_detail") or "").strip()
    else:
        term = str(getattr(item, "term", "") or getattr(item, "raw_term", "") or "")
        weight = float(getattr(item, "weight", 1.0) or 0.0)
        source_type = str(getattr(item, "source_type", "query") or "query").strip().lower()
        source_detail = str(getattr(item, "source_detail", "") or "").strip()
    return term, max(0.0, weight), source_type, source_detail


def _normalize_evidence_terms(
    raw_term: str, known_phrases: frozenset[str]
) -> list[tuple[str, float]]:
    """Normalize an evidence term into canonical primary term + optional phrase subtokens."""
    stripped = raw_term.strip()
    if not stripped:
        return []

    if " " not in stripped:
        canon = canonicalize_token(stripped)
        if not canon:
            return []
        return [(canon, 1.0)]

    words = [canonicalize_token(w) for w in stripped.split() if canonicalize_token(w)]
    if not words:
        return []
    joined = " ".join(words)
    out: list[tuple[str, float]] = [(joined, 1.0)]
    if joined in known_phrases or len(words) > 1:
        for w in words:
            if w != joined and len(w) >= 2:
                out.append((w, 0.5))
    return out


def build_query_vector(
    evidences: Sequence[Any], known_phrases: frozenset[str] = frozenset()
) -> QueryProfile:
    """Build Eq. 1 multi-source synergy query vector (#9 §2)."""
    # source -> term -> list[weight]
    source_term_weights: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    term_artifacts_map: dict[str, set[str]] = defaultdict(set)
    seen_sources: list[str] = []
    seen_sources_set: set[str] = set()

    for ev in evidences:
        raw_term, weight, source_type, source_detail = _extract_evidence_fields(ev)
        if weight <= 0.0 or not raw_term.strip():
            continue

        if source_type not in seen_sources_set:
            seen_sources_set.add(source_type)
            seen_sources.append(source_type)

        for term, mult in _normalize_evidence_terms(raw_term, known_phrases):
            eff_w = weight * mult
            if eff_w > 0.0:
                source_term_weights[source_type][term].append(eff_w)
                if source_detail:
                    term_artifacts_map[term].add(source_detail)

    # Normalize per-source vector u_hat_sigma(t)
    u_hat: dict[str, dict[str, float]] = {}
    all_terms: set[str] = set()
    for src, term_map in source_term_weights.items():
        raw_u: dict[str, float] = {}
        max_u = 0.0
        for term, weights in term_map.items():
            val = math.log1p(sum(weights)) * max(weights)
            raw_u[term] = val
            all_terms.add(term)
            if val > max_u:
                max_u = val
        denom = max(max_u, EPS)
        u_hat[src] = {t: v / denom for t, v in raw_u.items()}

    # Cross-source synergy fusion q(t)
    q_weights: dict[str, float] = {}
    term_sources: dict[str, tuple[str, ...]] = {}
    term_source_shares: dict[str, dict[str, float]] = {}

    for term in sorted(all_terms):
        corroborating = [src for src in seen_sources if u_hat.get(src, {}).get(term, 0.0) > 0.0]
        if not corroborating:
            continue
        nu = len(corroborating)
        synergy = 1.0 + BETA_SYN * (nu - 1)

        src_contribs: dict[str, float] = {}
        total_base = 0.0
        for src in corroborating:
            alpha_s = SOURCE_PRIORS.get(src, 1.0)
            part = alpha_s * u_hat[src][term]
            src_contribs[src] = part * synergy
            total_base += part

        q_weights[term] = total_base * synergy
        term_sources[term] = tuple(sorted(corroborating))
        term_source_shares[term] = src_contribs

    top_terms = [
        {
            "term": t,
            "weight": round(q_weights[t], 6),
            "sources": list(term_sources[t]),
        }
        for t in sorted(q_weights, key=lambda x: (-q_weights[x], x))[:25]
    ]

    return QueryProfile(
        weights=q_weights,
        sources_used=tuple(sorted(seen_sources)),
        term_sources=term_sources,
        term_source_shares=term_source_shares,
        term_artifacts={t: tuple(sorted(v)) for t, v in term_artifacts_map.items()},
        top_terms=top_terms,
    )


def _index_field_items(
    items: Sequence[str], known_phrases: frozenset[str]
) -> dict[str, float]:
    """Tokenize structured field items (keywords, ecosystems, file_indicators, domains)."""
    tf: dict[str, float] = defaultdict(float)
    for item in items:
        raw = item.strip()
        if not raw:
            continue
        if " " in raw:
            words = [canonicalize_token(w) for w in raw.split() if canonicalize_token(w)]
            phrase = " ".join(words)
            if phrase:
                tf[phrase] += 1.0
            for w in words:
                if w != phrase and len(w) >= 2:
                    tf[w] += 0.5
        else:
            canon = canonicalize_token(raw)
            if canon:
                tf[canon] += 1.0
            # Emit subtokens for compound items like systems_engineering or FastAPI
            for tok, w in tokenize_technical_text(
                raw, known_phrases=known_phrases, include_subtokens=True, max_ngram=1
            ):
                if tok != canon:
                    tf[tok] += 0.5 * w
    return dict(tf)


class LexicalScorer:
    """Pre-indexed Fielded BM25F + Sublinear TF-IDF hybrid scorer."""

    def __init__(self, ontology: OntologyRepository) -> None:
        self.ontology = ontology
        self.records: list[SubredditRecord] = ontology.all_subreddits()
        self.n_docs = len(self.records)

        # sub_name -> field -> term -> tf
        self.doc_fields: dict[str, dict[str, dict[str, float]]] = {}
        self.field_lengths: dict[str, dict[str, float]] = {}
        self.avg_field_len: dict[str, float] = {f: 0.0 for f in FIELD_WEIGHTS}

        # Combined field-weighted TF for TF-IDF & pseudo-TF for BM25F
        self.doc_pseudo_tf: dict[str, dict[str, float]] = {}
        self.doc_tfidf_vec: dict[str, dict[str, float]] = {}
        self.doc_tfidf_norm: dict[str, float] = {}
        self.idf: dict[str, float] = {}

        self._build_index()

    def _build_index(self) -> None:
        known_phrases = self.ontology.known_phrases
        df: dict[str, int] = defaultdict(int)

        for rec in self.records:
            name_tf = _index_field_items([rec.name], known_phrases)
            eco_tf = _index_field_items(rec.ecosystems, known_phrases)
            file_tf = _index_field_items(rec.file_indicators, known_phrases)
            kw_tf = _index_field_items(rec.keywords, known_phrases)
            dom_tf = _index_field_items(rec.domains, known_phrases)

            desc_tf: dict[str, float] = defaultdict(float)
            for tok, w in tokenize_technical_text(
                rec.description,
                known_phrases=known_phrases,
                include_subtokens=True,
                max_ngram=3,
            ):
                desc_tf[tok] += w

            fields = {
                "name": name_tf,
                "ecosystems": eco_tf,
                "file_indicators": file_tf,
                "keywords": kw_tf,
                "domains": dom_tf,
                "description": dict(desc_tf),
            }
            self.doc_fields[rec.name] = fields
            lengths = {f: sum(tmap.values()) for f, tmap in fields.items()}
            self.field_lengths[rec.name] = lengths
            for f, length in lengths.items():
                self.avg_field_len[f] += length

            doc_terms: set[str] = set()
            for tmap in fields.values():
                doc_terms.update(tmap.keys())
            for t in doc_terms:
                df[t] += 1

        n = max(1, self.n_docs)
        for f in FIELD_WEIGHTS:
            self.avg_field_len[f] = max(self.avg_field_len[f] / n, EPS)

        for term, freq in df.items():
            self.idf[term] = math.log(1.0 + (n - freq + 0.5) / (freq + 0.5))

        for rec in self.records:
            fields = self.doc_fields[rec.name]
            lengths = self.field_lengths[rec.name]
            pseudo_tf: dict[str, float] = defaultdict(float)
            raw_tf_weighted: dict[str, float] = defaultdict(float)

            for f, w_f in FIELD_WEIGHTS.items():
                b_f = FIELD_B[f]
                avg_len = self.avg_field_len[f]
                len_norm = 1.0 - b_f + b_f * (lengths[f] / avg_len)
                len_norm = max(len_norm, EPS)
                for t, tf_val in fields[f].items():
                    pseudo_tf[t] += w_f * (tf_val / len_norm)
                    raw_tf_weighted[t] += w_f * tf_val

            self.doc_pseudo_tf[rec.name] = dict(pseudo_tf)

            tfidf_vec: dict[str, float] = {}
            sq_sum = 0.0
            for t, tf_val in raw_tf_weighted.items():
                if tf_val <= 0.0:
                    continue
                w = math.log1p(tf_val) * self.idf.get(t, 0.0)
                if w > 0.0:
                    tfidf_vec[t] = w
                    sq_sum += w * w
            self.doc_tfidf_vec[rec.name] = tfidf_vec
            self.doc_tfidf_norm[rec.name] = max(math.sqrt(sq_sum), EPS)

    def score(self, evidences: Sequence[Any]) -> LexicalResult:
        """Score all ontology subreddits against multi-source evidences."""
        q_profile = build_query_vector(evidences, self.ontology.known_phrases)
        q = q_profile.weights

        bm25_raw: dict[str, float] = {}
        tfidf_raw: dict[str, float] = {}
        bm25_term_contribs: dict[str, dict[str, float]] = {}
        tfidf_term_contribs: dict[str, dict[str, float]] = {}

        if not q:
            zeros = {r.name: 0.0 for r in self.records}
            return LexicalResult(
                query_profile=q_profile,
                bm25f_norm=dict(zeros),
                tfidf_norm=dict(zeros),
                lexical_scores=dict(zeros),
                matched_terms={r.name: [] for r in self.records},
                source_breakdown={r.name: {} for r in self.records},
                source_artifacts={r.name: [] for r in self.records},
            )

        q_tfidf_sq = 0.0
        q_tfidf_vec: dict[str, float] = {}
        for t, q_w in q.items():
            idf_t = self.idf.get(t, 0.0)
            if idf_t > 0.0:
                val = q_w * idf_t
                q_tfidf_vec[t] = val
                q_tfidf_sq += val * val
        q_tfidf_norm = max(math.sqrt(q_tfidf_sq), EPS)

        max_bm25 = 0.0
        max_tfidf = 0.0

        for rec in self.records:
            sname = rec.name
            pseudo = self.doc_pseudo_tf[sname]
            d_vec = self.doc_tfidf_vec[sname]
            d_norm = self.doc_tfidf_norm[sname]

            b_sum = 0.0
            t_sum = 0.0
            b_contribs: dict[str, float] = {}
            t_contribs: dict[str, float] = {}

            for term, q_w in q.items():
                ptf = pseudo.get(term, 0.0)
                if ptf > 0.0:
                    idf_t = self.idf.get(term, 0.0)
                    b_part = (
                        q_w
                        * idf_t
                        * (ptf * (BM25F_K1 + 1.0))
                        / (ptf + BM25F_K1)
                    )
                    if b_part > 0.0:
                        b_sum += b_part
                        b_contribs[term] = b_part

                d_w = d_vec.get(term, 0.0)
                if d_w > 0.0:
                    t_part = (q_tfidf_vec.get(term, 0.0) * d_w) / (
                        q_tfidf_norm * d_norm
                    )
                    if t_part > 0.0:
                        t_sum += t_part
                        t_contribs[term] = t_part

            bm25_raw[sname] = b_sum
            tfidf_raw[sname] = t_sum
            bm25_term_contribs[sname] = b_contribs
            tfidf_term_contribs[sname] = t_contribs
            if b_sum > max_bm25:
                max_bm25 = b_sum
            if t_sum > max_tfidf:
                max_tfidf = t_sum

        bm25_denom = max(max_bm25, EPS)
        tfidf_denom = max(max_tfidf, EPS)

        bm25_norm: dict[str, float] = {}
        tfidf_norm: dict[str, float] = {}
        lexical_scores: dict[str, float] = {}
        matched_terms: dict[str, list[dict[str, Any]]] = {}
        source_breakdown: dict[str, dict[str, float]] = {}
        source_artifacts: dict[str, list[str]] = {}

        for rec in self.records:
            sname = rec.name
            b_n = bm25_raw[sname] / bm25_denom
            t_n = tfidf_raw[sname] / tfidf_denom
            bm25_norm[sname] = b_n
            tfidf_norm[sname] = t_n
            lexical_scores[sname] = 0.70 * b_n + 0.30 * t_n

            b_c = bm25_term_contribs[sname]
            t_c = tfidf_term_contribs[sname]
            terms_hit = set(b_c.keys()) | set(t_c.keys())

            term_list: list[dict[str, Any]] = []
            src_totals: dict[str, float] = defaultdict(float)
            artifacts: set[str] = set()

            for term in terms_hit:
                contrib = (0.70 * (b_c.get(term, 0.0) / bm25_denom)) + (
                    0.30 * (t_c.get(term, 0.0) / tfidf_denom)
                )
                term_list.append(
                    {"term": term, "contribution": round(contrib, 6)}
                )

                shares = q_profile.term_source_shares.get(term, {})
                total_share = sum(shares.values())
                if total_share > 0.0:
                    for src, s_val in shares.items():
                        src_totals[src] += contrib * (s_val / total_share)

                for art in q_profile.term_artifacts.get(term, ()):
                    artifacts.add(art)

            term_list.sort(key=lambda x: (-x["contribution"], x["term"]))
            matched_terms[sname] = term_list[:15]
            source_breakdown[sname] = {
                k: round(v, 6) for k, v in sorted(src_totals.items())
            }
            source_artifacts[sname] = sorted(artifacts)[:10]

        return LexicalResult(
            query_profile=q_profile,
            bm25f_norm=bm25_norm,
            tfidf_norm=tfidf_norm,
            lexical_scores=lexical_scores,
            matched_terms=matched_terms,
            source_breakdown=source_breakdown,
            source_artifacts=source_artifacts,
        )
