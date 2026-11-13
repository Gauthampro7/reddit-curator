"""Comprehensive unit & integration tests for Slice 3 Deterministic Curation Engine."""

from __future__ import annotations

import json
import re
import tempfile
import tomllib
import unittest
from pathlib import Path

import setuptools

from rcurator.analytics import FeedHealthAuditor
from rcurator.engine.graph import BipartitePPRScorer
from rcurator.engine.lexical import LexicalScorer, build_query_vector
from rcurator.engine.ontology import (
    ALL_STOPWORDS,
    CATEGORIES,
    PROTECTED_SHORT_SYMBOLS,
    OntologyRepository,
    canonicalize_token,
    normalize_manifest,
    tokenize_technical_text,
    validate_manifest,
)
from rcurator.engine.optimizer import (
    DOMINANCE_HARD_CAP,
    GoldenRatioOptimizer,
    total_variation_drift,
)
from rcurator.engine.pipeline import AutoPresetGenerator, _build_online_records
from rcurator.mixer import PresetMixer
from rcurator.nlp import tokenizer as nlp_tokenizer

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

    def test_manifest_normalization_preserves_explicit_category_and_hardens_inputs(self) -> None:
        raw = {
            "name": "Hardened Manifest",
            "anchor_subreddits": [
                {"name": "r/unixporn", "category": "aesthetic_leisure"},
                {"name": "/r/askscience/", "category": "cognitive_curiosity"},
            ],
            "domain_priming_subreddits": [
                {"name": "r/neuroscience", "category": "domain_priming"},
                {"name": "UnixPorn", "category": "domain_priming"},
                {"name": "r/kernel", "category": "invalid_category_xyz"},
            ],
            "custom_feeds": [
                {"feed_name": "Systems Focus", "subreddits": ["r/rust", "kernel"]},
                {"feed_name": "Systems-Focus", "subreddits": ["cpp"]},
            ],
        }
        norm = normalize_manifest(raw, self.ontology)
        self.assertEqual(
            [s["name"] for s in norm["anchor_subreddits"]],
            ["unixporn", "AskScience"],
        )
        # neuroscience preserves explicit valid domain_priming; unixporn deduped (anchor wins);
        # kernel falls back to ontology category ("domain_priming") on invalid category
        priming_by_name = {s["name"]: s["category"] for s in norm["domain_priming_subreddits"]}
        self.assertEqual(priming_by_name, {"neuroscience": "domain_priming", "kernel": "domain_priming"})
        self.assertEqual(
            [f["feed_name"] for f in norm["custom_feeds"]],
            ["systems_focus", "systems_focus_2"],
        )
        self.assertEqual(norm["custom_feeds"][0]["subreddits"], ["rust", "kernel"])

        # Non-list field guards do not iterate characters of strings
        malformed = {
            "anchor_subreddits": "unixporn",
            "domain_priming_subreddits": 123,
            "custom_feeds": {"feed_name": "bad"},
        }
        norm_bad = normalize_manifest(malformed, self.ontology)
        self.assertEqual(norm_bad["anchor_subreddits"], [])
        self.assertEqual(norm_bad["domain_priming_subreddits"], [])
        self.assertEqual(norm_bad["custom_feeds"], [])

    def test_validate_manifest_reports_errors_warnings_and_summary(self) -> None:
        valid_preset = json.loads((PRESETS_DIR / "cognitive_science.json").read_text(encoding="utf-8"))
        res_ok = validate_manifest(valid_preset, self.ontology)
        self.assertTrue(res_ok["valid"])
        self.assertEqual(res_ok["errors"], [])
        self.assertEqual(res_ok["summary"]["pillar_counts"]["domain_priming"], 8)

        res_bad = validate_manifest({"anchor_subreddits": "not-a-list"}, self.ontology)
        self.assertFalse(res_bad["valid"])
        self.assertTrue(any("anchor_subreddits" in e for e in res_bad["errors"]))

        res_warn = validate_manifest(
            {
                "anchor_subreddits": [{"name": "unixporn", "category": "aesthetic_leisure"}],
                "domain_priming_subreddits": [
                    {"name": "unixporn", "category": "domain_priming"},
                    {"name": "brand_new_unrecognized_sub_xyz", "category": "domain_priming"},
                ],
                "custom_feeds": [
                    {"feed_name": "dup_feed", "subreddits": ["rust"]},
                    {"feed_name": "dup-feed", "subreddits": ["python"]},
                ],
            },
            self.ontology,
        )
        self.assertTrue(res_warn["valid"])
        self.assertTrue(any("Duplicate" in w for w in res_warn["warnings"]))
        self.assertTrue(any("Colliding" in w for w in res_warn["warnings"]))
        self.assertTrue(any("unrecognized" in w for w in res_warn["warnings"]))

    def test_tokenizer_unified_between_nlp_and_engine(self) -> None:
        self.assertIs(PROTECTED_SHORT_SYMBOLS, nlp_tokenizer.PROTECTED_SHORT_SYMBOLS)
        self.assertIs(ALL_STOPWORDS, nlp_tokenizer.ALL_STOPWORDS)
        self.assertIs(tokenize_technical_text, nlp_tokenizer.tokenize_technical_text)

    def test_pyproject_pep420_packaging_discovers_presets_and_curator_data(self) -> None:
        pyproject_data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        find_cfg = pyproject_data["tool"]["setuptools"]["packages"]["find"]
        pkg_data = pyproject_data["tool"]["setuptools"]["package-data"]
        self.assertTrue(find_cfg.get("namespaces"))
        self.assertIn("curator_data*", find_cfg["include"])
        self.assertIn("presets*", find_cfg["include"])
        self.assertEqual(pkg_data["curator_data"], ["*.json"])
        self.assertEqual(pkg_data["presets"], ["*.json"])

        discovered = setuptools.PEP420PackageFinder.find(
            where=str(REPO_ROOT),
            include=find_cfg["include"],
        )
        self.assertIn("curator_data", discovered)
        self.assertIn("presets", discovered)
        self.assertIn("rcurator", discovered)
        self.assertIn("rcurator.studio.static", discovered)


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
        self.assertEqual(feeds[0]["display_name"], "Systems Engineering")
        self.assertEqual(feeds[1]["display_name"], "Systems Engineering (2)")

    def test_custom_feed_display_name_numbering_on_sanitized_collision(self) -> None:
        from rcurator.engine.ontology import SubredditRecord
        from rcurator.engine.optimizer import CandidateScore

        opt = GoldenRatioOptimizer(self.ontology)
        cands = [
            CandidateScore(
                record=SubredditRecord(
                    name="sub_alpha",
                    category="domain_priming",
                    domains=("foo-bar",),
                ),
                final_relevance=2.0,
                pillar_norm_relevance=1.0,
                lexical_score=1.0,
                pagerank_score=1.0,
            ),
            CandidateScore(
                record=SubredditRecord(
                    name="sub_beta",
                    category="domain_priming",
                    domains=("foo_bar",),
                ),
                final_relevance=1.0,
                pillar_norm_relevance=1.0,
                lexical_score=1.0,
                pagerank_score=1.0,
            ),
        ]
        feeds = opt._build_custom_feeds(cands)
        self.assertEqual(len(feeds), 2)
        self.assertEqual([f["feed_name"] for f in feeds], ["foo_bar", "foo_bar_2"])
        self.assertEqual(feeds[0]["display_name"], "Foo-Bar")
        self.assertEqual(feeds[1]["display_name"], "Foo Bar")

    def test_online_records_filter_non_ontology_bigrams(self) -> None:
        records = _build_online_records(
            [
                {
                    "name": "r/TypeTheoryLab",
                    "subscribers": 5000,
                    "title": "Arbitrary words here",
                    "public_description": "type theory discussions and random adjacent phrases",
                }
            ],
            self.ontology,
        )
        self.assertEqual(len(records), 1)
        kw = set(records[0].keywords)
        self.assertIn("type theory", kw)
        self.assertNotIn("arbitrary words", kw)
        self.assertNotIn("random adjacent", kw)


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
            for cat in CATEGORIES:
                self.assertGreaterEqual(
                    report["pillar_counts"][cat],
                    1,
                    f"{pfile.name} missing pillar {cat}",
                )
            self.assertGreaterEqual(
                report["golden_ratio_fidelity_score"],
                75.0,
                f"{pfile.name} low fidelity {report['golden_ratio_fidelity_score']}",
            )
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

        all_unrec = self.auditor.audit_live(["not_a_real_sub_1", "not_a_real_sub_2"])
        self.assertLessEqual(all_unrec["golden_ratio_fidelity_score"], 0.0)
        self.assertEqual(all_unrec["composite_health_score"], 0.0)

        # Explicit manifest category overrides ontology category in audit_manifest, not audit_live
        explicit_manifest = {
            "anchor_subreddits": [{"name": "unixporn", "category": "aesthetic_leisure"}],
            "domain_priming_subreddits": [{"name": "neuroscience", "category": "domain_priming"}],
        }
        rep_manifest = self.auditor.audit_manifest(explicit_manifest)
        self.assertEqual(rep_manifest["pillar_counts"]["domain_priming"], 1)
        rep_live = self.auditor.audit_live(["unixporn", "neuroscience"])
        self.assertEqual(rep_live["pillar_counts"]["cognitive_curiosity"], 1)
        self.assertEqual(rep_live["pillar_counts"]["domain_priming"], 0)

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
