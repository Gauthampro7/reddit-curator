"""Shared signal evidence data contract across all miners and the curation engine."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class SignalEvidence:
    """Normalized signal extracted from a workspace, GitHub profile, resume, or query."""

    term: str
    raw_term: str
    weight: float
    source_type: str
    source_detail: str
    signal_kind: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize evidence into a JSON-compatible dictionary."""
        return {
            "term": self.term,
            "raw_term": self.raw_term,
            "weight": float(self.weight),
            "source_type": self.source_type,
            "source_detail": self.source_detail,
            "signal_kind": self.signal_kind,
        }

    @classmethod
    def from_any(cls, obj: Any) -> SignalEvidence:
        """Construct a SignalEvidence from an instance, Mapping, or duck-typed object."""
        if isinstance(obj, cls):
            return obj
        if isinstance(obj, Mapping):
            return cls(
                term=str(obj["term"]),
                raw_term=str(obj.get("raw_term", obj["term"])),
                weight=float(obj["weight"]),
                source_type=str(obj["source_type"]),
                source_detail=str(obj.get("source_detail", "")),
                signal_kind=str(obj.get("signal_kind", "keyword")),
            )
        return cls(
            term=str(getattr(obj, "term")),
            raw_term=str(getattr(obj, "raw_term", getattr(obj, "term"))),
            weight=float(getattr(obj, "weight")),
            source_type=str(getattr(obj, "source_type")),
            source_detail=str(getattr(obj, "source_detail", "")),
            signal_kind=str(getattr(obj, "signal_kind", "keyword")),
        )
