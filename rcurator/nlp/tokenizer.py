"""Deterministic technical lexer, canonicalizer, and ontology phrase matcher."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path

CANONICAL_ALIASES: dict[str, str] = {
    "nodejs": "node.js",
    "nextjs": "next.js",
    "vuejs": "vue.js",
    "nuxtjs": "nuxt.js",
    "threejs": "three.js",
    "d3js": "d3.js",
    "dotnet": ".net",
    "csharp": "c#",
    "fsharp": "f#",
    "cpp": "c++",
    "cxx": "c++",
    "llamacpp": "llama.cpp",
    "k8s": "kubernetes",
    "k3s": "kubernetes",
    "golang": "go",
    "postgres": "postgresql",
    "py": "python",
    "rs": "rust",
    "ts": "typescript",
    "js": "javascript",
    "tf": "tensorflow",
    "sklearn": "scikit-learn",
}

PROTECTED_SHORT_SYMBOLS: frozenset[str] = frozenset(
    {
        "c",
        "r",
        "go",
        "ml",
        "ai",
        "os",
        "db",
        "ir",
        "vm",
        "ui",
        "ux",
        "ci",
        "cd",
        "qa",
        "io",
        "ip",
        "3d",
        "2d",
        "ar",
        "vr",
        "fp",
        "hw",
        "sw",
        "dl",
        "rl",
        "c#",
        "f#",
    }
)

PROTECTED_SHORT_TOKENS: frozenset[str] = PROTECTED_SHORT_SYMBOLS

PROTECTED_PUNCTUATED_TOKENS: frozenset[str] = frozenset(
    {
        ".net",
        "async/await",
        "c#",
        "f#",
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
        "x86_64",
        "aarch64",
        "ebpf",
        "risc-v",
        "zero-copy",
        "lsm-tree",
        "b-tree",
        "a/b",
    }
)

ENGLISH_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "about",
        "above",
        "after",
        "again",
        "against",
        "all",
        "am",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "because",
        "been",
        "before",
        "being",
        "below",
        "between",
        "both",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "doing",
        "down",
        "during",
        "each",
        "few",
        "for",
        "from",
        "further",
        "had",
        "has",
        "have",
        "having",
        "he",
        "her",
        "here",
        "hers",
        "herself",
        "him",
        "himself",
        "his",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "itself",
        "just",
        "may",
        "me",
        "might",
        "more",
        "most",
        "my",
        "myself",
        "no",
        "nor",
        "not",
        "now",
        "of",
        "off",
        "on",
        "once",
        "only",
        "or",
        "other",
        "our",
        "ours",
        "ourselves",
        "out",
        "over",
        "own",
        "same",
        "she",
        "should",
        "so",
        "some",
        "such",
        "than",
        "that",
        "the",
        "their",
        "theirs",
        "them",
        "themselves",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "to",
        "too",
        "under",
        "until",
        "up",
        "very",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
        "yours",
        "yourself",
        "yourselves",
    }
)

CODE_BOILERPLATE_STOPWORDS: frozenset[str] = frozenset(
    {
        "args",
        "based",
        "bool",
        "break",
        "build",
        "building",
        "built",
        "case",
        "class",
        "cls",
        "com",
        "config",
        "const",
        "continue",
        "crate",
        "def",
        "default",
        "define",
        "developed",
        "dict",
        "elif",
        "else",
        "endif",
        "enum",
        "etc",
        "except",
        "experience",
        "export",
        "extends",
        "false",
        "final",
        "finally",
        "fixme",
        "float",
        "fn",
        "function",
        "helper",
        "helpers",
        "ifdef",
        "ifndef",
        "impl",
        "implements",
        "import",
        "include",
        "init",
        "instanceof",
        "int",
        "kwargs",
        "let",
        "lib",
        "list",
        "main",
        "mod",
        "namespace",
        "none",
        "null",
        "nullptr",
        "org",
        "override",
        "package",
        "pass",
        "pragma",
        "print",
        "private",
        "project",
        "projects",
        "protected",
        "pub",
        "public",
        "raise",
        "require",
        "return",
        "self",
        "set",
        "sizeof",
        "skills",
        "src",
        "static",
        "str",
        "struct",
        "summary",
        "switch",
        "todo",
        "true",
        "try",
        "tuple",
        "typedef",
        "typename",
        "typeof",
        "undefined",
        "use",
        "used",
        "using",
        "utils",
        "var",
        "via",
        "void",
        "work",
        "worked",
        "working",
        "www",
    }
)

ALL_STOPWORDS: frozenset[str] = ENGLISH_STOPWORDS | CODE_BOILERPLATE_STOPWORDS

_EDGE_STRIP_CHARS = " \t\r\n\"'`()[]{},;:!?"
_URL_SCHEME_RE = re.compile(r"https?://(?:[^/\s@]+@)?", re.IGNORECASE)
_RAW_TOKEN_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*<[A-Za-z0-9_,]+>"
    r"|\.[A-Za-z0-9_]+(?:[./-][A-Za-z0-9_]+)*"
    r"|[A-Za-z0-9_]+(?:[+][+][0-9]*|[#])?(?:[./&-][A-Za-z0-9_]+(?:[+][+][0-9]*|[#])?)*"
)
_CAMEL_SPLIT_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_PURE_NUMBER_RE = re.compile(r"^\d+(?:\.\d+)*$")
_TOKEN_EXTRACT_RE = re.compile(
    r"""
    (?<![a-zA-Z0-9_])
    (?:
        \.[a-zA-Z0-9][a-zA-Z0-9_.-]*
        | [a-zA-Z0-9][a-zA-Z0-9_.-]*<[a-zA-Z0-9_,\s]+>
        | [a-zA-Z0-9][a-zA-Z0-9_.&+/-]*(?:\+{2}(?:\d{2})?|\#)
        | [a-zA-Z0-9][a-zA-Z0-9_.&+/-]*
    )
    """,
    re.VERBOSE,
)


def load_ontology_phrases(ontology_path: Path | None = None) -> list[str]:
    """Load technical terms and multi-word phrases from subreddit_ontology.json."""
    path = ontology_path
    if path is None:
        path = Path(__file__).resolve().parents[2] / "curator_data" / "subreddit_ontology.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    subreddits = data.get("subreddits", {})
    entries = subreddits.values() if isinstance(subreddits, dict) else subreddits
    if not isinstance(entries, Iterable):
        return []

    phrases: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for field in ("keywords", "ecosystems", "file_indicators", "domains"):
            values = entry.get(field, [])
            if not isinstance(values, list):
                continue
            for item in values:
                text = str(item).strip().lower()
                if text and text not in seen:
                    seen.add(text)
                    phrases.append(text)
    return phrases


def canonicalize_token(token: str) -> str:
    """Normalize a single technical token while preserving significant punctuation."""
    cleaned = token.strip(_EDGE_STRIP_CHARS)
    while cleaned.endswith(".") and len(cleaned) > 1:
        cleaned = cleaned[:-1]
    if cleaned.startswith("<") and ">" not in cleaned:
        cleaned = cleaned.lstrip("<")
    if cleaned.endswith(">") and "<" not in cleaned:
        cleaned = cleaned.rstrip(">")
    if "<" in cleaned and ">" in cleaned:
        cleaned = re.sub(r"\s+", "", cleaned)
    cleaned = cleaned.strip(_EDGE_STRIP_CHARS).lower()
    if not cleaned:
        return ""
    return CANONICAL_ALIASES.get(cleaned, cleaned)


def _is_valid_emitted_token(tok: str) -> bool:
    """Return True if a canonicalized token should be kept as a signal."""
    if not tok or tok in ALL_STOPWORDS:
        return False
    if _PURE_NUMBER_RE.match(tok):
        return False
    if tok in PROTECTED_SHORT_TOKENS or tok in PROTECTED_PUNCTUATED_TOKENS:
        return True
    if len(tok) <= 1:
        return False
    return len(tok) >= 2


def _split_camel_and_snake(raw: str) -> list[str]:
    """Extract sub-tokens from CamelCase, snake_case, and hyphenated identifiers."""
    parts: list[str] = []
    for chunk in re.split(r"[_-]+", raw):
        if not chunk:
            continue
        camel_parts = _CAMEL_SPLIT_RE.split(chunk)
        parts.extend(p for p in camel_parts if p)
    return parts


class _TrieNode:
    __slots__ = ("children", "phrase")

    def __init__(self) -> None:
        self.children: dict[str, _TrieNode] = {}
        self.phrase: str | None = None


class TechnicalTokenizer:
    """Deterministic technical tokenizer with alias folding, sub-tokens, shingles, and trie phrases."""

    def __init__(self, known_phrases: Iterable[str] = ()) -> None:
        self._trie_root = _TrieNode()
        self._protected_slash: set[str] = {
            t for t in PROTECTED_PUNCTUATED_TOKENS if "/" in t
        }
        self._phrase_prefix_words: set[str] = set()
        for raw_phrase in known_phrases:
            self._register_phrase(str(raw_phrase))

    def _register_phrase(self, raw_phrase: str) -> None:
        words = [canonicalize_token(w) for w in raw_phrase.strip().split()]
        words = [w for w in words if w]
        if not words:
            return
        for w in words:
            if "/" in w:
                self._protected_slash.add(w)
                self._phrase_prefix_words.add(w)
        if len(words) < 2:
            return
        node = self._trie_root
        for word in words:
            node = node.children.setdefault(word, _TrieNode())
        node.phrase = " ".join(words)

    def _extract_word_stream(self, text: str) -> list[tuple[str, str]]:
        """Return ordered (raw_piece, canonical_piece) pairs preserving punctuated technical tokens."""
        scrubbed = _URL_SCHEME_RE.sub("", text)
        stream: list[tuple[str, str]] = []
        for match in _RAW_TOKEN_RE.finditer(scrubbed):
            raw = match.group(0)
            canon = canonicalize_token(raw)
            if not canon:
                continue
            if "/" in canon and canon not in self._protected_slash and canon not in PROTECTED_PUNCTUATED_TOKENS:
                segments = [s for s in raw.split("/") if s]
                for seg in segments:
                    seg_canon = canonicalize_token(seg)
                    if seg_canon:
                        stream.append((seg, seg_canon))
            else:
                stream.append((raw, canon))
        return stream

    def _match_trie_phrases(self, words: list[str]) -> list[tuple[str, float]]:
        """Find longest ontology phrase matches starting at each position in the word stream."""
        matched: list[tuple[str, float]] = []
        total = len(words)
        for idx in range(total):
            node = self._trie_root
            longest_phrase: str | None = None
            longest_len = 0
            pos = idx
            while pos < total:
                child = node.children.get(words[pos])
                if child is None:
                    break
                node = child
                pos += 1
                if node.phrase is not None:
                    longest_phrase = node.phrase
                    longest_len = pos - idx
            if longest_phrase is not None and longest_len >= 2:
                gamma = 1.0 + 0.35 * (longest_len - 1)
                matched.append((longest_phrase, round(gamma, 4)))
        return matched

    def tokenize(self, text: str) -> list[tuple[str, float]]:
        """Tokenize text into weighted (canonical_term, weight) tuples."""
        if not text:
            return []

        stream = self._extract_word_stream(text)
        if not stream:
            return []

        results: list[tuple[str, float]] = []
        unigram_stream: list[str] = []

        for raw, canon in stream:
            if not _is_valid_emitted_token(canon):
                continue

            results.append((canon, 1.0))
            unigram_stream.append(canon)

            # Dual-emission for CamelCase / snake_case / hyphenated tokens at 0.5x weight
            if raw not in PROTECTED_PUNCTUATED_TOKENS and canon not in PROTECTED_PUNCTUATED_TOKENS:
                sub_parts = _split_camel_and_snake(raw)
                if len(sub_parts) > 1:
                    seen_subs: set[str] = {canon}
                    for part in sub_parts:
                        sub_canon = canonicalize_token(part)
                        if sub_canon not in seen_subs and _is_valid_emitted_token(sub_canon):
                            seen_subs.add(sub_canon)
                            results.append((sub_canon, 0.5))

        # n=2 and n=3 shingles over filtered primary unigrams
        n_uni = len(unigram_stream)
        for i in range(n_uni - 1):
            bigram = f"{unigram_stream[i]} {unigram_stream[i + 1]}"
            results.append((bigram, 0.75))
            if i + 2 < n_uni:
                trigram = f"{bigram} {unigram_stream[i + 2]}"
                results.append((trigram, 0.5))

        # Prefix-trie ontology multi-word phrase matching over unfiltered canonical word stream
        phrase_words = [canon for _, canon in stream]
        results.extend(self._match_trie_phrases(phrase_words))

        return results


def _split_subtokens(raw_token: str, canonical: str) -> list[str]:
    """Emit CamelCase / snake_case / hyphen / slash / dot sub-tokens for dual emission."""
    parts: list[str] = []
    camel_parts = _CAMEL_SPLIT_RE.split(raw_token)
    candidates = camel_parts if len(camel_parts) > 1 else [raw_token]

    for seg in candidates:
        for piece in re.split(r"[_/\-.]+", seg):
            sub = canonicalize_token(piece)
            if not sub or sub == canonical:
                continue
            if len(sub) < 2 and sub not in PROTECTED_SHORT_SYMBOLS:
                continue
            if sub in ALL_STOPWORDS:
                continue
            parts.append(sub)
    return parts


def tokenize_technical_text(
    text: str,
    known_phrases: Iterable[str] = (),
    include_subtokens: bool = True,
    max_ngram: int = 3,
) -> list[tuple[str, float]]:
    """Deterministic technical lexer emitting weighted tokens and ontology phrases."""
    if not text or not text.strip():
        return []

    primary_tokens: list[str] = []
    emitted: list[tuple[str, float]] = []

    for match in _TOKEN_EXTRACT_RE.finditer(text):
        raw = match.group(0)
        canon = canonicalize_token(raw)
        if not canon:
            continue

        canon = canon.strip("-/")
        if not canon:
            continue
        canon = CANONICAL_ALIASES.get(canon, canon)

        if canon in ALL_STOPWORDS:
            continue
        if len(canon) < 2 and canon not in PROTECTED_SHORT_SYMBOLS:
            continue

        primary_tokens.append(canon)
        emitted.append((canon, 1.0))

        if include_subtokens:
            for sub in _split_subtokens(raw, canon):
                emitted.append((sub, 0.5))

    n_toks = len(primary_tokens)
    if max_ngram >= 2 and n_toks >= 2:
        if isinstance(known_phrases, frozenset):
            phrase_set = known_phrases
        elif known_phrases:
            phrase_set = frozenset(
                " ".join(canonicalize_token(w) for w in p.split() if canonicalize_token(w))
                for p in known_phrases
                if p and " " in p
            ) - {""}
        else:
            phrase_set = frozenset()

        for n in range(2, min(max_ngram, n_toks) + 1):
            mult = 1.0 + 0.35 * (n - 1)
            for idx in range(n_toks - n + 1):
                gram = " ".join(primary_tokens[idx : idx + n])
                if not phrase_set or gram in phrase_set:
                    emitted.append((gram, mult))

    return emitted
