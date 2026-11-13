"""Comprehensive unit & integration tests for Slice 3 Deterministic Curation Engine."""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from rcurator.analytics import FeedHealthAuditor
from rcurator.engine.graph import BipartitePPRScorer
from rcurator.engine.lexical import LexicalScorer, build_query_vector
from rcurator.engine.ontology import (
    CATEGORIES,
    OntologyRepository,
    canonicalize_token,
    normalize_manifest,
    tokenize_technical_text,
)
from rcurator.engine.optimizer import (
    DOMINANCE_HARD_CAP,
    GoldenRatioOptimizer,
    total_variation_drift,
)
from rcurator.engine.pipeline import AutoPresetGenerator
from rcurator.mixer import PresetMixer

REPO_ROOT = Path(__file__).resolve().parents[1]
ONTOLOGY_PATH = REPO_ROOT / "curator_data" / "subreddit_ontology.json"
PRESETS_DIR = REPO_ROOT / "presets"
FEED_NAME_RE = re.compile(r"^[a-z0-9_]{1,50}$")


class TestOntologyAndNormalization(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ontology = OntologyRepository.load(ONTOLOGY_PATH)

    def test_live_ontology_loads_292_subreddits(self) -> None:
        self.assertEqual(len(self.ontology), 292)
        self.assertEqual(len(self.ontology.domains), 17)
        self.assertEqual(self.ontology.canonical_name("askscience"), "AskScience")
        self.assertEqual(self.ontology.canonical_name("LOCALLLAMA"), "LocalLLaMA")

    def test_load_supports_list_fixture(self) -> None:
        payload = [
            {
                "name": "CustomSub",
                "category": "domain_priming",
                "domains": ["systems_engineering"],
                "estimated_daily_posts": 12.0,
                "signal_density": 0.9,
                "keywords": ["raft"],
                "ecosystems": ["rust"],
                "file_indicators": [".rs"],
                "related_subreddits": [],
                "description": "Custom fixture sub",
            }
        ]
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir) / "fixture_ont.json"
            tmp_path.write_text(json.dumps(payload), encoding="utf-8")
            repo = OntologyRepository.load(tmp_path)
            self.assertEqual(len(repo), 1)
            self.assertIn("customsub", repo)
            self.assertEqual(repo.canonical_name("customsub"), "CustomSub")

    def test_punctuated_tokens_and_aliases_preserved(self) -> None:
        preserved = [
            ".net",
            "async/await",
            "c#",
            "c++",
            "c++20",
            "c++23",
            "ci/cd",
            "core.async",
            "data.table",
            "gnu/linux",
            "http/3",
            "lazy.nvim",
            "llama.cpp",
            "m&a",
            "node.js",
            "next.js",
            "vue.js",
            "nuxt.js",
            "d3.js",
            "three.js",
            "pub/sub",
            "sdr++",
            "span<t>",
            "tcp/ip",
            "io_uring",
            "scikit-learn",
        ]
        for tok in preserved:
            self.assertEqual(canonicalize_token(tok), tok, f"Failed to preserve {tok}")

        self.assertEqual(canonicalize_token("k8s"), "kubernetes")
        self.assertEqual(canonicalize_token("golang"), "go")
        self.assertEqual(canonicalize_token("postgres"), "postgresql")
        self.assertEqual(canonicalize_token("nodejs"), "node.js")
        self.assertEqual(canonicalize_token("llamacpp"), "llama.cpp")

        toks = dict(tokenize_technical_text("Using node.js, llama.cpp, C++20, and .NET."))
        self.assertIn("node.js", toks)
        self.assertIn("llama.cpp", toks)
        self.assertIn("c++20", toks)
        self.assertIn(".net", toks)

    def test_legacy_curation_manifest_normalization(self) -> None:
        legacy = {
            "version": "0.9",
            "name": "Legacy Diet",
            "metadata": {
                "cognitive_distribution": {
                    "aesthetic_leisure_pct": 40,
                    "cognitive_curiosity_pct": 35,
                    "systems_priming_pct": 25,
                }
            },
            "anchor_subreddits": [
                {"name": "unixporn", "category": "aesthetic_leisure"},
                {"name": "askscience", "category": "cognitive_curiosity"},
            ],
            "tier1_systems_subreddits": [
                {
                    "name": "databasedevelopment",
                    "category": "high_signal_systems_priming",
                },
                "postgresql",
            ],
            "custom_feeds": [
                {
                    "feed_name": "Systems-Core!",
                    "subreddits": ["kernel", "experienceddevs"],
                }
            ],
        }
        norm = normalize_manifest(legacy, self.ontology)
        self.assertIn("golden_ratio_distribution", norm)
        self.assertEqual(
            norm["golden_ratio_distribution"]["domain_priming_target_pct"], 25
        )
        self.assertEqual(
            [s["name"] for s in norm["anchor_subreddits"]],
            ["unixporn", "AskScience"],
        )
        self.assertEqual(
            [s["name"] for s in norm["domain_priming_subreddits"]],
            ["databasedevelopment", "PostgreSQL"],
        )
        self.assertTrue(
            all(
                s["category"] == "domain_priming"
                for s in norm["domain_priming_subreddits"]
            )
        )
        self.assertEqual(norm["custom_feeds"][0]["feed_name"], "systems_core")
        self.assertEqual(
            norm["custom_feeds"][0]["subreddits"], ["kernel", "ExperiencedDevs"]
        )


class TestLexicalAndGraphScoring(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ontology = OntologyRepository.load(ONTOLOGY_PATH)
        cls.lexical = LexicalScorer(cls.ontology)
        cls.ppr = BipartitePPRScorer(cls.ontology, cls.lexical)

    def test_eq1_multi_source_synergy_vector(self) -> None:
        evidences = [
            {"term": "rust", "weight": 2.0, "source_type": "workspace"},
            {"term": "rust", "weight": 2.0, "source_type": "resume"},
            {"term": "rust", "weight": 2.0, "source_type": "github"},
            {"term": "python", "weight": 2.0, "source_type": "workspace"},
        ]
        qp = build_query_vector(evidences)
        # rust corroborated across 3 sources should receive synergy 1 + 0.25*(3-1) = 1.5x
        self.assertIn("rust", qp.weights)
        self.assertAlmostEqual(
            qp.weights["rust"], (1.0 + 1.0 + 0.9) * 1.5, places=6
        )
        self.assertAlmostEqual(qp.weights["python"], 1.0, places=6)

    def test_ppr_activates_non_lexically_mentioned_related_subreddits(self) -> None:
        ev = [
            {
                "term": "pyarrow",
                "weight": 3.5,
                "source_type": "workspace",
                "source_detail": "pyproject.toml",
            },
            {
                "term": "parquet",
                "weight": 3.2,
                "source_type": "workspace",
                "source_detail": "etl.py",
            },
        ]
        lex_res = self.lexical.score(ev)
        ppr_res = self.ppr.score(lex_res)

        # duckdb lexically matches pyarrow & parquet; databasedevelopment does NOT
        self.assertGreater(lex_res.lexical_scores["duckdb"], 0.8)
        self.assertEqual(lex_res.lexical_scores["databasedevelopment"], 0.0)

        # PPR must propagate activation to databasedevelopment and explain the bridge
        self.assertGreater(ppr_res.ppr_norm["databasedevelopment"], 0.03)
        self.assertFalse(ppr_res.cold_start)
        bridges = ppr_res.graph_bridges["databasedevelopment"]
        self.assertTrue(any("duckdb" in b for b in bridges), f"Unexpected bridges: {bridges}")

    def test_cold_start_zero_evidence_uses_quality_prior(self) -> None:
        lex_res = self.lexical.score([])
        ppr_res = self.ppr.score(lex_res)

        self.assertTrue(ppr_res.cold_start)
        self.assertEqual(sum(lex_res.lexical_scores.values()), 0.0)
        self.assertGreater(max(ppr_res.ppr_norm.values()), 0.99)
        self.assertTrue(all(v > 0.0 for v in ppr_res.ppr_raw.values()))


class TestGoldenRatioOptimizerAndPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ontology = OntologyRepository.load(ONTOLOGY_PATH)
        cls.generator = AutoPresetGenerator(cls.ontology)

    def test_multi_source_systems_ml_cogsci_ricing_profile(self) -> None:
        evidences = [
            {"term": "rust", "weight": 3.5, "source_type": "workspace", "source_detail": "Cargo.toml"},
            {"term": "rocksdb", "weight": 3.0, "source_type": "workspace", "source_detail": "Cargo.toml"},
            {"term": "ebpf", "weight": 3.2, "source_type": "github", "source_detail": "repo:kernel-probe"},
            {"term": "pytorch", "weight": 3.5, "source_type": "resume", "source_detail": "Skills"},
            {"term": "llama.cpp", "weight": 3.0, "source_type": "resume", "source_detail": "Projects"},
            {"term": "hyprland", "weight": 2.8, "source_type": "workspace", "source_detail": "hyprland.conf"},
            {"term": "predictive processing", "weight": 2.5, "source_type": "query", "source_detail": "query"},
        ]
        manifest = self.generator.generate(
            evidences, name="Systems ML CogSci Diet", tier1_count=18
        )

        # V1 & V2 keys present
        for key in (
            "version",
            "name",
            "description",
            "feed_mode",
            "golden_ratio_distribution",
            "anchor_subreddits",
            "domain_priming_subreddits",
            "custom_feeds",
            "curation_provenance",
        ):
            self.assertIn(key, manifest)

        tier1 = manifest["anchor_subreddits"] + manifest["domain_priming_subreddits"]
        self.assertEqual(len(tier1), 18)

        prov = manifest["curation_provenance"]
        self.assertEqual(
            prov["algorithm"], "bm25f_tfidf_bipartite_ppr_velocity_gr_v2"
        )
        self.assertEqual(
            sorted(prov["sources_used"]), ["github", "query", "resume", "workspace"]
        )

        health = prov["diet_health"]
        self.assertLessEqual(health["golden_ratio_tvd_drift"], 0.08)
        self.assertGreaterEqual(health["golden_ratio_fidelity_score"], 92.0)
        self.assertLessEqual(health["max_subreddit_dominance_pct"], 50.0)

        # Tier 2 custom feeds: 1-3 valid private feeds <= 100 subs each
        feeds = manifest["custom_feeds"]
        self.assertGreaterEqual(len(feeds), 1)
        self.assertLessEqual(len(feeds), 3)
        all_t2_subs = set()
        for f in feeds:
            self.assertRegex(f["feed_name"], FEED_NAME_RE)
            self.assertEqual(f["visibility"], "private")
            self.assertLessEqual(len(f["subreddits"]), 100)
            all_t2_subs.update(f["subreddits"])

        # High-velocity LocalLLaMA (180/day) should be routed to Tier 2 custom feed
        tier1_names = {s["name"] for s in tier1}
        self.assertIn("LocalLLaMA", all_t2_subs)
        self.assertNotIn("LocalLLaMA", tier1_names)

        # Explanations schema check
        explanations = prov["explanations"]
        for sname in tier1_names:
            exp = explanations[sname]
            self.assertIn(exp["tier"], ("tier1_following", "both"))
            self.assertIn("matched_terms", exp)
            self.assertIn("graph_bridges", exp)
            self.assertIn("routing_reason", exp)

    def test_small_k_tier1_count_3_and_invalid_k_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.generator.generate([], tier1_count=2)

        manifest = self.generator.generate(
            [{"term": "ebpf", "weight": 3.0, "source_type": "query"}], tier1_count=3
        )
        tier1 = manifest["anchor_subreddits"] + manifest["domain_priming_subreddits"]
        self.assertEqual(len(tier1), 3)
        cats = {s["category"] for s in tier1}
        self.assertEqual(cats, set(CATEGORIES))
        health = manifest["curation_provenance"]["diet_health"]
        self.assertLessEqual(health["max_subreddit_dominance_pct"], 50.0)
        self.assertGreaterEqual(health["golden_ratio_tvd_drift"], 0.0)
        self.assertLessEqual(health["golden_ratio_tvd_drift"], 1.0)

    def test_cold_start_pipeline_stability(self) -> None:
        manifest = self.generator.generate([], tier1_count=18)
        health = manifest["curation_provenance"]["diet_health"]
        self.assertEqual(manifest["curation_provenance"]["sources_used"], [])
        self.assertLessEqual(health["golden_ratio_tvd_drift"], 0.05)
        self.assertLessEqual(health["max_subreddit_dominance_pct"], 50.0)

    def test_online_candidates_filtering_and_enrichment(self) -> None:
        online = [
            {
                "name": "nsfw_bad_sub",
                "over18": True,
                "subscribers": 500000,
                "public_description": "rust systems kernel",
            },
            {
                "name": "tiny_sub",
                "over18": False,
                "subscribers": 400,
                "public_description": "rust systems kernel",
            },
            {
                "name": "ZigKernelDev",
                "over18": False,
                "subscribers": 18000,
                "title": "Zig Kernel and Bare Metal Systems",
                "public_description": "zig osdev kernel bare metal systems",
            },
        ]
        manifest = self.generator.generate(
            [{"term": "zig", "weight": 4.0, "source_type": "workspace"}],
            tier1_count=18,
            online_candidates=online,
        )
        all_names = {
            s["name"]
            for s in manifest["anchor_subreddits"] + manifest["domain_priming_subreddits"]
        }
        for f in manifest["custom_feeds"]:
            all_names.update(f["subreddits"])

        self.assertNotIn("nsfw_bad_sub", all_names)
        self.assertNotIn("tiny_sub", all_names)
        self.assertIn("ZigKernelDev", all_names)

    def test_dominance_cap_enforced_under_high_velocity_inputs(self) -> None:
        opt = GoldenRatioOptimizer(self.ontology)
        lex = {r.name: 0.0 for r in self.ontology.all_subreddits()}
        ppr = {r.name: 0.0 for r in self.ontology.all_subreddits()}
        # Heavily spike LocalLLaMA (180 posts/day) and unixporn (95 posts/day)
        fused = {r.name: 0.01 for r in self.ontology.all_subreddits()}
        fused["LocalLLaMA"] = 10.0
        fused["unixporn"] = 8.0
        res = opt.optimize(fused, lex, ppr, tier1_count=6)
        self.assertLessEqual(res.max_dominance, DOMINANCE_HARD_CAP + 1e-9)
        self.assertGreaterEqual(
            res.final_relevance_sum + 1e-9, 0.95 * res.greedy_relevance_sum
        )

    def test_custom_feed_partitioning_respects_100_sub_limit(self) -> None:
        from rcurator.engine.ontology import SubredditRecord
        from rcurator.engine.optimizer import CandidateScore

        opt = GoldenRatioOptimizer(self.ontology)
        cands = [
            CandidateScore(
                record=SubredditRecord(
                    name=f"domsub_{i}",
                    category="domain_priming",
                    domains=("systems_engineering",),
                ),
                final_relevance=1.0 - i * 0.001,
                pillar_norm_relevance=1.0,
                lexical_score=1.0,
                pagerank_score=1.0,
            )
            for i in range(105)
        ]
        feeds = opt._build_custom_feeds(cands)
        self.assertEqual(len(feeds), 2)
        self.assertEqual(len(feeds[0]["subreddits"]), 100)
        self.assertEqual(len(feeds[1]["subreddits"]), 5)
        for f in feeds:
            self.assertRegex(f["feed_name"], FEED_NAME_RE)


class TestAuditorAndPresetMixer(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ontology = OntologyRepository.load(ONTOLOGY_PATH)
        cls.auditor = FeedHealthAuditor(cls.ontology)
        cls.mixer = PresetMixer(cls.ontology, PRESETS_DIR)

    def test_all_17_presets_audit_cleanly(self) -> None:
        preset_files = sorted(PRESETS_DIR.glob("*.json"))
        self.assertEqual(len(preset_files), 17)
        for pfile in preset_files:
            data = json.loads(pfile.read_text(encoding="utf-8"))
            report = self.auditor.audit_manifest(data)
            self.assertEqual(report["unrecognized_subreddits"], [], pfile.name)
            self.assertGreaterEqual(report["golden_ratio_tvd_drift"], 0.0)
            self.assertLessEqual(report["golden_ratio_tvd_drift"], 1.0)
            self.assertGreaterEqual(report["golden_ratio_fidelity_score"], 0.0)
            self.assertLessEqual(report["golden_ratio_fidelity_score"], 100.0)
            self.assertGreaterEqual(report["shannon_domain_entropy_norm"], 0.0)
            self.assertLessEqual(report["shannon_domain_entropy_norm"], 1.0)
            self.assertGreaterEqual(report["gini_velocity_concentration"], 0.0)
            self.assertLessEqual(report["gini_velocity_concentration"], 1.0)
            self.assertGreaterEqual(report["composite_health_score"], 0.0)
            self.assertLessEqual(report["composite_health_score"], 100.0)

    def test_simplex_bounds_on_extreme_single_pillar_inputs(self) -> None:
        # 100% domain_priming feed vs (40, 35, 25) target
        report_dom = self.auditor.audit_live(
            ["databasedevelopment", "LocalLLaMA", "kernel"], target_ratio=(40, 35, 25)
        )
        self.assertAlmostEqual(report_dom["golden_ratio_tvd_drift"], 0.75, places=6)
        self.assertAlmostEqual(report_dom["golden_ratio_fidelity_score"], 25.0, places=2)
        self.assertGreaterEqual(report_dom["golden_ratio_tvd_drift"], 0.0)
        self.assertLessEqual(report_dom["golden_ratio_tvd_drift"], 1.0)

        # 100% aesthetic_leisure feed
        report_aes = self.auditor.audit_live(
            ["unixporn", "linux"], target_ratio=(40, 35, 25)
        )
        self.assertAlmostEqual(report_aes["golden_ratio_tvd_drift"], 0.60, places=6)
        self.assertAlmostEqual(report_aes["golden_ratio_fidelity_score"], 40.0, places=2)

        # TVD helper direct bound check
        self.assertAlmostEqual(
            total_variation_drift(
                {"aesthetic_leisure": 1.0, "cognitive_curiosity": 0.0, "domain_priming": 0.0},
                {"aesthetic_leisure": 0.0, "cognitive_curiosity": 0.0, "domain_priming": 1.0},
            ),
            1.0,
        )

    def test_audit_live_handles_unrecognized_and_empty(self) -> None:
        empty = self.auditor.audit_live([])
        self.assertEqual(empty["subreddit_count"], 0)
        self.assertEqual(empty["golden_ratio_tvd_drift"], 1.0)
        self.assertEqual(empty["composite_health_score"], 0.0)

        mixed = self.auditor.audit_live(["unixporn", "not_a_real_sub_xyz", "askscience"])
        self.assertEqual(mixed["unrecognized_subreddits"], ["not_a_real_sub_xyz"])
        self.assertEqual(mixed["subreddit_count"], 3)
        self.assertGreaterEqual(mixed["golden_ratio_tvd_drift"], 0.0)
        self.assertLessEqual(mixed["golden_ratio_tvd_drift"], 1.0)

    def test_preset_mixer_convex_blend_and_validation(self) -> None:
        mixed = self.mixer.mix(
            {"systems_engineering": 0.6, "machine_learning": 0.4},
            target_ratio=(40, 35, 25),
            tier1_count=18,
        )
        prov = mixed["curation_provenance"]
        self.assertEqual(prov["sources_used"], ["preset_mixer"])
        health = prov["diet_health"]
        self.assertLessEqual(health["golden_ratio_tvd_drift"], 0.05)
        self.assertGreaterEqual(health["golden_ratio_fidelity_score"], 95.0)
        self.assertLessEqual(health["max_subreddit_dominance_pct"], 50.0)
        self.assertGreaterEqual(len(mixed["custom_feeds"]), 1)

        with self.assertRaises(ValueError):
            self.mixer.mix({})
        with self.assertRaises(ValueError):
            self.mixer.mix({"../curator_data/subreddit_ontology": 1.0})
        with self.assertRaises(FileNotFoundError):
            self.mixer.mix({"nonexistent_preset_xyz": 1.0})


if __name__ == "__main__":
    unittest.main()
