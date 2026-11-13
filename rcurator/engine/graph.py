"""Bipartite Concept-Subreddit + related_subreddits Personalized PageRank scorer."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from rcurator.engine.lexical import FIELD_WEIGHTS, LexicalResult, LexicalScorer
from rcurator.engine.ontology import OntologyRepository

PPR_ALPHA: float = 0.85
PPR_TOL: float = 1e-7
PPR_MAX_ITERS: int = 50
CONCEPT_TELEPORT_SHARE: float = 0.55
SUB_TELEPORT_SHARE: float = 0.45
ANCHOR_EPSILON: float = 0.05
EPS: float = 1e-9


@dataclass
class GraphResult:
    """Personalized PageRank scores, fused relevance R(s), and bridge explanations."""

    ppr_raw: dict[str, float]
    ppr_norm: dict[str, float]
    fused_relevance: dict[str, float]
    graph_bridges: dict[str, list[str]]
    iterations: int
    l1_delta: float
    cold_start: bool


class BipartitePPRScorer:
    """Heterogeneous Concept-Subreddit + related_subreddits Personalized PageRank."""

    def __init__(
        self, ontology: OntologyRepository, lexical_scorer: LexicalScorer
    ) -> None:
        self.ontology = ontology
        self.lexical_scorer = lexical_scorer

        self.sub_names: list[str] = sorted(r.name for r in ontology.all_subreddits())
        self.sub_index: dict[str, int] = {name: i for i, name in enumerate(self.sub_names)}

        # Nodes: [0 .. n_sub-1] are subreddits, [n_sub .. n_total-1] are concepts
        self.concept_names: list[str] = []
        self.concept_index: dict[str, int] = {}

        # Adjacency: node_idx -> list[(neighbor_idx, weight)]
        self.adj: list[list[tuple[int, float]]] = []
        self.out_sum: list[float] = []
        # Metadata for bridge attribution
        self.concept_to_subs: dict[str, list[tuple[str, float]]] = defaultdict(list)
        self.sub_to_concepts: dict[str, list[tuple[str, float]]] = defaultdict(list)
        self.related_edges: dict[str, list[tuple[str, float]]] = defaultdict(list)

        self._build_graph()

    def _build_graph(self) -> None:
        n_sub = len(self.sub_names)
        idf = self.lexical_scorer.idf

        # Collect concept-subreddit edges from structured ontology fields
        sub_concept_weights: dict[tuple[str, str], float] = defaultdict(float)
        structured_fields = ("name", "ecosystems", "file_indicators", "keywords", "domains")

        for sname in self.sub_names:
            rec = self.ontology.get(sname)
            if rec is None:
                continue
            rho = max(0.05, rec.signal_density)
            fields = self.lexical_scorer.doc_fields.get(sname, {})
            for fname in structured_fields:
                w_f = FIELD_WEIGHTS[fname]
                for concept, tf_val in fields.get(fname, {}).items():
                    if tf_val <= 0.0:
                        continue
                    c_idf = idf.get(concept, 1.0)
                    # Weight scales with field importance, term specificity, and quality prior
                    edge_w = w_f * c_idf * rho * min(2.0, tf_val)
                    if edge_w > 0.0:
                        sub_concept_weights[(sname, concept)] += edge_w

        all_concepts = sorted({c for _, c in sub_concept_weights.keys()})
        self.concept_names = all_concepts
        self.concept_index = {c: n_sub + i for i, c in enumerate(all_concepts)}

        n_total = n_sub + len(all_concepts)
        adj_maps: list[dict[int, float]] = [defaultdict(float) for _ in range(n_total)]

        concept_weights_list: list[float] = []
        for (sname, concept), w in sub_concept_weights.items():
            s_idx = self.sub_index[sname]
            c_idx = self.concept_index[concept]
            adj_maps[s_idx][c_idx] += w
            adj_maps[c_idx][s_idx] += w
            self.concept_to_subs[concept].append((sname, w))
            self.sub_to_concepts[sname].append((concept, w))
            concept_weights_list.append(w)

        if concept_weights_list:
            sorted_cw = sorted(concept_weights_list)
            median_cw = sorted_cw[len(sorted_cw) // 2]
        else:
            median_cw = 5.0

        # Symmetrized related_subreddits edges
        seen_pairs: set[tuple[str, str]] = set()
        for sname in self.sub_names:
            rec = self.ontology.get(sname)
            if rec is None:
                continue
            for rel_raw in rec.related_subreddits:
                rel_rec = self.ontology.get(rel_raw)
                if rel_rec is None or rel_rec.name == sname:
                    continue
                pair = tuple(sorted((sname, rel_rec.name)))
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                avg_rho = 0.5 * (max(0.05, rec.signal_density) + max(0.05, rel_rec.signal_density))
                w_rel = max(4.0, 1.5 * median_cw) * avg_rho
                u_idx = self.sub_index[pair[0]]
                v_idx = self.sub_index[pair[1]]
                adj_maps[u_idx][v_idx] += w_rel
                adj_maps[v_idx][u_idx] += w_rel
                self.related_edges[pair[0]].append((pair[1], w_rel))
                self.related_edges[pair[1]].append((pair[0], w_rel))

        self.adj = [sorted(m.items()) for m in adj_maps]
        self.out_sum = [sum(w for _, w in nbrs) for nbrs in self.adj]

    def score(self, lexical_res: LexicalResult) -> GraphResult:
        """Run Personalized PageRank and compute fused relevance R(s) + graph bridges."""
        n_sub = len(self.sub_names)
        n_total = n_sub + len(self.concept_names)
        if n_sub == 0:
            return GraphResult(
                ppr_raw={},
                ppr_norm={},
                fused_relevance={},
                graph_bridges={},
                iterations=0,
                l1_delta=0.0,
                cold_start=True,
            )

        q_weights = lexical_res.query_profile.weights
        idf = self.lexical_scorer.idf

        concept_masses: dict[int, float] = {}
        for term, q_w in q_weights.items():
            c_idx = self.concept_index.get(term)
            if c_idx is not None and q_w > 0.0:
                concept_masses[c_idx] = q_w * idf.get(term, 1.0)

        sub_masses: dict[int, float] = {}
        for sname, lex_val in lexical_res.lexical_scores.items():
            if lex_val > 0.0 and sname in self.sub_index:
                sub_masses[self.sub_index[sname]] = lex_val

        total_c_mass = sum(concept_masses.values())
        total_s_mass = sum(sub_masses.values())
        cold_start = (total_c_mass <= 0.0) and (total_s_mass <= 0.0)

        teleport = [0.0] * n_total
        if cold_start:
            rho_sum = 0.0
            rhos = []
            for sname in self.sub_names:
                rec = self.ontology.get(sname)
                rho = max(0.05, rec.signal_density if rec else 0.5)
                rhos.append(rho)
                rho_sum += rho
            denom = max(rho_sum, EPS)
            for i, rho in enumerate(rhos):
                teleport[i] = rho / denom
        else:
            if total_c_mass > 0.0 and total_s_mass > 0.0:
                c_share = CONCEPT_TELEPORT_SHARE
                s_share = SUB_TELEPORT_SHARE
            elif total_c_mass > 0.0:
                c_share = 1.0
                s_share = 0.0
            else:
                c_share = 0.0
                s_share = 1.0

            if total_c_mass > 0.0:
                for c_idx, m in concept_masses.items():
                    teleport[c_idx] = c_share * (m / total_c_mass)
            if total_s_mass > 0.0:
                for s_idx, m in sub_masses.items():
                    teleport[s_idx] = s_share * (m / total_s_mass)

        # Power iteration with dangling mass redistribution
        pi = list(teleport)
        iters = 0
        delta = 0.0

        for it in range(1, PPR_MAX_ITERS + 1):
            iters = it
            next_pi = [(1.0 - PPR_ALPHA) * p for p in teleport]
            dangling_mass = 0.0

            for u in range(n_total):
                pi_u = pi[u]
                if pi_u <= 0.0:
                    continue
                deg = self.out_sum[u]
                if deg <= 0.0:
                    dangling_mass += pi_u
                    continue
                scale = PPR_ALPHA * pi_u / deg
                for v, w in self.adj[u]:
                    next_pi[v] += scale * w

            if dangling_mass > 0.0:
                d_scale = PPR_ALPHA * dangling_mass
                for i in range(n_total):
                    if teleport[i] > 0.0:
                        next_pi[i] += d_scale * teleport[i]

            delta = sum(abs(next_pi[i] - pi[i]) for i in range(n_total))
            pi = next_pi
            if delta < PPR_TOL:
                break

        ppr_raw: dict[str, float] = {
            sname: pi[self.sub_index[sname]] for sname in self.sub_names
        }
        max_ppr = max(ppr_raw.values()) if ppr_raw else 0.0
        ppr_denom = max(max_ppr, EPS)
        ppr_norm: dict[str, float] = {
            sname: val / ppr_denom for sname, val in ppr_raw.items()
        }

        # Fused relevance R(s) (#7 §2.3.3)
        fused_relevance: dict[str, float] = {}
        for sname in self.sub_names:
            rec = self.ontology.get(sname)
            rho = rec.signal_density if rec else 0.8
            cat = rec.category if rec else "domain_priming"
            eps_c = (
                ANCHOR_EPSILON * rho
                if cat in ("aesthetic_leisure", "cognitive_curiosity")
                else 0.0
            )
            lex_s = lexical_res.lexical_scores.get(sname, 0.0)
            ppr_s = ppr_norm.get(sname, 0.0)
            fused_relevance[sname] = (0.60 * lex_s + 0.40 * ppr_s + eps_c) * (
                0.70 + 0.30 * rho
            )

        graph_bridges = self._attribute_bridges(
            pi=pi,
            lexical_res=lexical_res,
            q_weights=q_weights,
            cold_start=cold_start,
        )

        return GraphResult(
            ppr_raw=ppr_raw,
            ppr_norm=ppr_norm,
            fused_relevance=fused_relevance,
            graph_bridges=graph_bridges,
            iterations=iters,
            l1_delta=delta,
            cold_start=cold_start,
        )

    def _attribute_bridges(
        self,
        pi: list[float],
        lexical_res: LexicalResult,
        q_weights: dict[str, float],
        cold_start: bool,
    ) -> dict[str, list[str]]:
        """Identify top graph-bridge paths explaining why PPR activated each subreddit."""
        if cold_start:
            return {sname: ["cold_start_quality_prior"] for sname in self.sub_names}

        n_sub = len(self.sub_names)
        lex_scores = lexical_res.lexical_scores
        matched_terms = lexical_res.matched_terms

        # Precompute top query term per matched subreddit for crisp 2-hop labels
        sub_top_term: dict[str, str] = {}
        for sname, terms in matched_terms.items():
            if terms:
                sub_top_term[sname] = terms[0]["term"]

        bridges_out: dict[str, list[str]] = {}
        for sname in self.sub_names:
            s_idx = self.sub_index[sname]
            scored_paths: dict[str, float] = {}

            # 1. Direct related_subreddits flow u -> s
            for rel_name, w_rel in self.related_edges.get(sname, ()):
                u_idx = self.sub_index[rel_name]
                deg_u = self.out_sum[u_idx]
                if deg_u <= 0.0:
                    continue
                flow = pi[u_idx] * (w_rel / deg_u)
                if flow <= 0.0:
                    continue
                if lex_scores.get(rel_name, 0.0) > 0.0 and rel_name in sub_top_term:
                    label = f"{sub_top_term[rel_name]} -> r/{rel_name} -> r/{sname} (related_subreddits)"
                else:
                    label = f"r/{rel_name} -> r/{sname} (related_subreddits)"
                scored_paths[label] = max(scored_paths.get(label, 0.0), flow)

            # 2. Concept bridges: query/sub -> concept -> s
            for concept, w_cs in self.sub_to_concepts.get(sname, ()):
                c_idx = self.concept_index.get(concept)
                if c_idx is None:
                    continue
                deg_c = self.out_sum[c_idx]
                if deg_c <= 0.0:
                    continue
                flow = pi[c_idx] * (w_cs / deg_c)
                if flow <= 0.0:
                    continue
                if q_weights.get(concept, 0.0) > 0.0:
                    label = f"concept:{concept} -> r/{sname}"
                else:
                    # Find strongest neighbor sharing this concept
                    best_u = ""
                    best_u_score = -1.0
                    for cand_u, w_cu in self.concept_to_subs.get(concept, ()):
                        if cand_u == sname:
                            continue
                        u_strength = (lex_scores.get(cand_u, 0.0) + pi[self.sub_index[cand_u]]) * w_cu
                        if u_strength > best_u_score:
                            best_u_score = u_strength
                            best_u = cand_u
                    if best_u:
                        label = f"r/{best_u} -> concept:{concept} -> r/{sname}"
                    else:
                        label = f"concept:{concept} -> r/{sname}"
                scored_paths[label] = max(scored_paths.get(label, 0.0), flow)

            ranked = sorted(scored_paths.items(), key=lambda x: (-x[1], x[0]))
            bridges_out[sname] = [lbl for lbl, _ in ranked[:5]]

        return bridges_out
