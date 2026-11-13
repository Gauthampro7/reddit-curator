"""Deterministic curation engine package for reddit-curator v2.0."""

from rcurator.engine.ontology import (
    CANONICAL_ALIASES,
    CATEGORIES,
    OntologyRepository,
    SubredditRecord,
    canonicalize_token,
    normalize_manifest,
    tokenize_technical_text,
)
from rcurator.engine.pipeline import AutoPresetGenerator

__all__ = [
    "AutoPresetGenerator",
    "CANONICAL_ALIASES",
    "CATEGORIES",
    "OntologyRepository",
    "SubredditRecord",
    "canonicalize_token",
    "normalize_manifest",
    "tokenize_technical_text",
]
