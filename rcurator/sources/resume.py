"""3-tier local resume parser (pypdf -> pdftotext -> pure-Python zlib/ObjStm/ToUnicode)."""

from __future__ import annotations

import importlib
import io
import os
import re
import shutil
import subprocess
import zlib
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from rcurator.nlp.tokenizer import TechnicalTokenizer, load_ontology_phrases
from rcurator.sources.types import SignalEvidence
from rcurator.sources.workspace import scrub_url_userinfo

MAX_RESUME_BYTES = 15 * 1024 * 1024  # 15 MB cap
MAX_BFRANGE_ENTRIES = 4096
SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({".pdf", ".md", ".markdown", ".txt", ".tex"})

SECTION_MULTIPLIERS: dict[str, float] = {
    "skills": 3.5,
    "projects": 2.8,
    "research": 2.8,
    "experience": 2.4,
    "education": 1.8,
    "certifications": 1.6,
    "summary": 1.5,
    "general": 1.0,
}

_SECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "skills",
        re.compile(
            r"^(?:#{1,6}\s*)?(?:technical\s+skills|core\s+skills|skills|technologies|tech\s+stack|core\s+competencies|tools\s*(?:&|and)\s*technologies)\s*:?\s*(.*)$",
            re.IGNORECASE,
        ),
    ),
    (
        "research",
        re.compile(
            r"^(?:#{1,6}\s*)?(?:research(?:\s+experience|\s+interests)?|publications)\s*:?\s*(.*)$",
            re.IGNORECASE,
        ),
    ),
    (
        "projects",
        re.compile(
            r"^(?:#{1,6}\s*)?(?:selected\s+projects|academic\s+projects|personal\s+projects|projects|open\s+source)\s*:?\s*(.*)$",
            re.IGNORECASE,
        ),
    ),
    (
        "experience",
        re.compile(
            r"^(?:#{1,6}\s*)?(?:work\s+experience|professional\s+experience|experience|employment(?:\s+history)?|internships)\s*:?\s*(.*)$",
            re.IGNORECASE,
        ),
    ),
    (
        "education",
        re.compile(
            r"^(?:#{1,6}\s*)?(?:education|academic\s+background|coursework)\s*:?\s*(.*)$",
            re.IGNORECASE,
        ),
    ),
    (
        "certifications",
        re.compile(
            r"^(?:#{1,6}\s*)?(?:certifications|certificates|licenses(?:\s*(?:&|and)\s*certifications)?|awards)\s*:?\s*(.*)$",
            re.IGNORECASE,
        ),
    ),
    (
        "summary",
        re.compile(
            r"^(?:#{1,6}\s*)?(?:professional\s+summary|executive\s+summary|summary|profile|objective|about\s+me)\s*:?\s*(.*)$",
            re.IGNORECASE,
        ),
    ),
)

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(
    r"(?<!\w)(?:\+?\d{1,3}[-.\s]?)?(?:\(\d{3}\)|\d{3})[-.\s]\d{3}[-.\s]\d{4}(?!\w)"
    r"|(?<!\w)\+\d{1,3}[\s-]?\d{7,12}(?!\w)"
)
_PII_LABEL_LINE_RE = re.compile(
    r"^\s*(?:email|e-mail|phone|tel|telephone|mobile|cell|address|street)\s*:.*$",
    re.IGNORECASE,
)

# LaTeX light cleanup regexes
_TEX_COMMENT_RE = re.compile(r"(?<!\\)%.*$", re.MULTILINE)
_TEX_ENV_RE = re.compile(r"\\(?:begin|end)\*?\{[^}]*\}(?:\[[^\]]*\])?")
_TEX_SECTION_RE = re.compile(r"\\(?:section|subsection|subsubsection)\*?\{([^}]*)\}")
_TEX_HREF_RE = re.compile(r"\\href\{[^}]*\}\{([^}]*)\}")
_TEX_CMD_ARG_RE = re.compile(r"\\[a-zA-Z]+\*?\{([^}]*)\}")
_TEX_BARE_CMD_RE = re.compile(r"\\[a-zA-Z]+\*?")

# Pure-Python PDF regexes
_OBJ_HEADER_RE = re.compile(rb"(\d+)\s+(\d+)\s+obj\b")
_INT_ENTRY_RE = re.compile(rb"/([A-Za-z0-9_+.-]+)\s+(\d+)(?!\s+\d+\s+R)")
_REF_ENTRY_RE = re.compile(rb"/([A-Za-z0-9_+.-]+)\s+(\d+)\s+(\d+)\s+R")
_NAME_ENTRY_RE = re.compile(rb"/([A-Za-z0-9_+.-]+)\s+/([A-Za-z0-9_+.-]+)")
_NUMBER_TOKEN_RE = re.compile(rb"^[+-]?(?:\d+\.?\d*|\.\d+)$")


class EncryptedPDFError(Exception):
    """Raised when attempting to parse an encrypted or password-protected PDF."""


def scrub_pii(text: str) -> str:
    """Remove emails, phone numbers, URL userinfo, and labeled contact lines from text."""
    cleaned_lines: list[str] = []
    for line in text.splitlines():
        if _PII_LABEL_LINE_RE.match(line):
            continue
        line = scrub_url_userinfo(line)
        line = _EMAIL_RE.sub(" ", line)
        line = _PHONE_RE.sub(" ", line)
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines)


def _strip_latex(text: str) -> str:
    text = _TEX_COMMENT_RE.sub("", text)
    text = _TEX_ENV_RE.sub(" ", text)
    text = _TEX_SECTION_RE.sub(r"\n\1\n", text)
    text = _TEX_HREF_RE.sub(r"\1", text)
    for _ in range(3):
        text = _TEX_CMD_ARG_RE.sub(r"\1", text)
    text = _TEX_BARE_CMD_RE.sub(" ", text)
    return text.replace("{", " ").replace("}", " ")


def _find_matching_dict_end(buf: bytes, start: int) -> int | None:
    """Given buf[start:start+2] == b'<<', return index immediately after matching '>>'."""
    n = len(buf)
    depth = 0
    i = start
    while i < n - 1:
        two = buf[i : i + 2]
        if two == b"<<":
            depth += 1
            i += 2
            continue
        if two == b">>":
            depth -= 1
            i += 2
            if depth == 0:
                return i
            continue
        ch = buf[i]
        if ch == ord("("):
            # Skip PDF literal string
            paren_depth = 1
            i += 1
            while i < n and paren_depth > 0:
                if buf[i] == ord("\\"):
                    i += 2
                    continue
                if buf[i] == ord("("):
                    paren_depth += 1
                elif buf[i] == ord(")"):
                    paren_depth -= 1
                i += 1
            continue
        i += 1
    return None


def _paeth_predictor(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _unfilter_png_predictor(
    raw: bytes, columns: int = 1, colors: int = 1, bpc: int = 8
) -> bytes:
    """Undo PNG row predictors (10..15) applied by FlateDecode DecodeParms."""
    bpp = max(1, (colors * bpc + 7) // 8)
    row_len = max(1, (columns * colors * bpc + 7) // 8)
    stride = row_len + 1
    if len(raw) % stride != 0:
        return raw

    out = bytearray()
    prev = bytearray(row_len)
    for offset in range(0, len(raw), stride):
        ftype = raw[offset]
        row = raw[offset + 1 : offset + stride]
        recon = bytearray(row_len)
        if ftype == 0:  # None
            recon[:] = row
        elif ftype == 1:  # Sub
            for i in range(row_len):
                left = recon[i - bpp] if i >= bpp else 0
                recon[i] = (row[i] + left) & 0xFF
        elif ftype == 2:  # Up
            for i in range(row_len):
                recon[i] = (row[i] + prev[i]) & 0xFF
        elif ftype == 3:  # Average
            for i in range(row_len):
                left = recon[i - bpp] if i >= bpp else 0
                recon[i] = (row[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:  # Paeth
            for i in range(row_len):
                left = recon[i - bpp] if i >= bpp else 0
                up = prev[i]
                up_left = prev[i - bpp] if i >= bpp else 0
                recon[i] = (row[i] + _paeth_predictor(left, up, up_left)) & 0xFF
        else:
            recon[:] = row
        out.extend(recon)
        prev = recon
    return bytes(out)


def _decompress_stream(dict_bytes: bytes | None, stream_bytes: bytes) -> bytes:
    """Decompress PDF stream supporting /FlateDecode and PNG /Predictor."""
    if not dict_bytes:
        return stream_bytes
    if b"/FlateDecode" not in dict_bytes and b"/Fl" not in dict_bytes:
        return stream_bytes

    data = stream_bytes
    try:
        data = zlib.decompress(stream_bytes)
    except zlib.error:
        try:
            data = zlib.decompress(stream_bytes, -zlib.MAX_WBITS)
        except zlib.error:
            return stream_bytes

    pred_m = re.search(rb"/Predictor\s+(\d+)", dict_bytes)
    if pred_m:
        predictor = int(pred_m.group(1))
        if predictor >= 10:
            cols_m = re.search(rb"/Columns\s+(\d+)", dict_bytes)
            colors_m = re.search(rb"/Colors\s+(\d+)", dict_bytes)
            bpc_m = re.search(rb"/BitsPerComponent\s+(\d+)", dict_bytes)
            cols = int(cols_m.group(1)) if cols_m else 1
            colors = int(colors_m.group(1)) if colors_m else 1
            bpc = int(bpc_m.group(1)) if bpc_m else 8
            data = _unfilter_png_predictor(data, columns=cols, colors=colors, bpc=bpc)
    return data


def _parse_pdf_objects(
    data: bytes,
) -> tuple[dict[tuple[int, int], tuple[bytes | None, bytes | None, bytes]], bool]:
    """Parse indirect PDF objects (including stream boundaries) and detect /Encrypt."""
    objects: dict[tuple[int, int], tuple[bytes | None, bytes | None, bytes]] = {}
    has_encrypt = False

    # Check explicit trailer dictionaries for /Encrypt
    for tm in re.finditer(rb"\btrailer\s*<<", data):
        end = _find_matching_dict_end(data, tm.end() - 2)
        if end is not None:
            trailer_dict = data[tm.end() - 2 : end]
            if re.search(rb"/Encrypt\b", trailer_dict):
                has_encrypt = True

    pos = 0
    n = len(data)
    while pos < n:
        m = _OBJ_HEADER_RE.search(data, pos)
        if not m:
            break
        obj_num = int(m.group(1))
        gen_num = int(m.group(2))
        cursor = m.end()
        while cursor < n and data[cursor] in b" \t\r\n":
            cursor += 1

        dict_bytes: bytes | None = None
        if cursor + 1 < n and data[cursor : cursor + 2] == b"<<":
            dict_end = _find_matching_dict_end(data, cursor)
            if dict_end is not None:
                dict_bytes = data[cursor:dict_end]
                if re.search(rb"/Encrypt\b", dict_bytes):
                    has_encrypt = True
                cursor = dict_end

        while cursor < n and data[cursor] in b" \t\r\n":
            cursor += 1

        if data[cursor : cursor + 6] == b"stream":
            s_start = cursor + 6
            if s_start < n and data[s_start : s_start + 2] == b"\r\n":
                s_start += 2
            elif s_start < n and data[s_start : s_start + 1] in (b"\r", b"\n"):
                s_start += 1

            stream_bytes: bytes | None = None
            if dict_bytes is not None:
                len_m = re.search(rb"/Length\s+(\d+)(?!\s+\d+\s+R)", dict_bytes)
                if len_m:
                    declared_len = int(len_m.group(1))
                    cand_end = s_start + declared_len
                    after = data[cand_end : cand_end + 16].lstrip(b"\r\n ")
                    if after.startswith(b"endstream"):
                        stream_bytes = data[s_start:cand_end]
                        end_pos = data.find(b"endobj", cand_end)
                        pos = (end_pos + 6) if end_pos != -1 else (cand_end + 9)

            if stream_bytes is None:
                endstream_m = re.search(rb"\r?\n?endstream\b", data[s_start:])
                if endstream_m:
                    s_end = s_start + endstream_m.start()
                    stream_bytes = data[s_start:s_end]
                    end_pos = data.find(b"endobj", s_start + endstream_m.end())
                    pos = (end_pos + 6) if end_pos != -1 else (s_start + endstream_m.end())
                else:
                    pos = s_start

            objects[(obj_num, gen_num)] = (dict_bytes, stream_bytes, dict_bytes or b"")
        else:
            end_pos = data.find(b"endobj", cursor)
            if end_pos == -1:
                body = data[cursor : min(n, cursor + 4096)]
                pos = n
            else:
                body = data[cursor:end_pos]
                pos = end_pos + 6
            if dict_bytes is None and b"<<" in body:
                idx = body.find(b"<<")
                d_end = _find_matching_dict_end(body, idx)
                if d_end is not None:
                    dict_bytes = body[idx:d_end]
                    if re.search(rb"/Encrypt\b", dict_bytes):
                        has_encrypt = True
            objects[(obj_num, gen_num)] = (dict_bytes, None, body)

    return objects, has_encrypt


def _unpack_object_streams(
    objects: dict[tuple[int, int], tuple[bytes | None, bytes | None, bytes]],
) -> None:
    """Unpack /Type /ObjStm compressed object streams into objects dict."""
    objstm_items = [
        (key, val)
        for key, val in list(objects.items())
        if val[0] is not None and val[1] is not None and re.search(rb"/Type\s*/ObjStm\b", val[0])
    ]
    for _, (dict_bytes, stream_bytes, _) in objstm_items:
        assert dict_bytes is not None and stream_bytes is not None
        raw = _decompress_stream(dict_bytes, stream_bytes)
        n_m = re.search(rb"/N\s+(\d+)", dict_bytes)
        first_m = re.search(rb"/First\s+(\d+)", dict_bytes)
        if not n_m or not first_m:
            continue
        count = int(n_m.group(1))
        first = int(first_m.group(1))
        if first > len(raw):
            continue
        header_nums = [int(x) for x in re.findall(rb"\d+", raw[:first])]
        if len(header_nums) < 2 * count:
            continue
        pairs = [(header_nums[2 * i], header_nums[2 * i + 1]) for i in range(count)]
        for i, (emb_id, rel_off) in enumerate(pairs):
            start = first + rel_off
            end = first + pairs[i + 1][1] if i + 1 < count else len(raw)
            if start < 0 or end > len(raw) or start >= end:
                continue
            chunk = raw[start:end].strip()
            emb_dict: bytes | None = None
            if chunk.startswith(b"<<"):
                d_end = _find_matching_dict_end(chunk, 0)
                if d_end is not None:
                    emb_dict = chunk[:d_end]
            objects[(emb_id, 0)] = (emb_dict, None, chunk)


def _decode_utf16be_hex(hex_str: str) -> str:
    cleaned = re.sub(r"\s+", "", hex_str)
    if len(cleaned) % 2 == 1:
        cleaned += "0"
    if not cleaned:
        return ""
    raw = bytes.fromhex(cleaned)
    if len(raw) == 1:
        return chr(raw[0])
    if len(raw) % 2 == 1:
        raw = b"\x00" + raw
    return raw.decode("utf-16-be", errors="replace")


def _parse_tounicode_cmap(cmap_bytes: bytes) -> dict[tuple[int, int], str]:
    """Parse PDF /ToUnicode CMap beginbfchar and beginbfrange mappings."""
    table: dict[tuple[int, int], str] = {}
    text = cmap_bytes.decode("latin-1", errors="replace")

    for block in re.findall(r"beginbfchar(.*?)endbfchar", text, re.DOTALL):
        for src_hex, dst_hex in re.findall(r"<([0-9A-Fa-f\s]+)>\s*<([0-9A-Fa-f\s]+)>", block):
            src_clean = re.sub(r"\s+", "", src_hex)
            if not src_clean:
                continue
            byte_len = max(1, (len(src_clean) + 1) // 2)
            code = int(src_clean, 16)
            table[(code, byte_len)] = _decode_utf16be_hex(dst_hex)

    for block in re.findall(r"beginbfrange(.*?)endbfrange", text, re.DOTALL):
        # Array form: <start> <end> [<d1> <d2> ...]
        for start_hex, end_hex, arr_body in re.findall(
            r"<([0-9A-Fa-f\s]+)>\s*<([0-9A-Fa-f\s]+)>\s*\[(.*?)\]", block, re.DOTALL
        ):
            start_clean = re.sub(r"\s+", "", start_hex)
            end_clean = re.sub(r"\s+", "", end_hex)
            if not start_clean or not end_clean:
                continue
            byte_len = max(1, (len(start_clean) + 1) // 2)
            start_code = int(start_clean, 16)
            end_code = int(end_clean, 16)
            dsts = re.findall(r"<([0-9A-Fa-f\s]+)>", arr_body)
            for idx, dst_hex in enumerate(dsts[:MAX_BFRANGE_ENTRIES]):
                code = start_code + idx
                if code > end_code:
                    break
                table[(code, byte_len)] = _decode_utf16be_hex(dst_hex)

        # Incrementing scalar form: <start> <end> <dst_start>
        for start_hex, end_hex, dst_hex in re.findall(
            r"<([0-9A-Fa-f\s]+)>\s*<([0-9A-Fa-f\s]+)>\s*<([0-9A-Fa-f\s]+)>", block
        ):
            start_clean = re.sub(r"\s+", "", start_hex)
            end_clean = re.sub(r"\s+", "", end_hex)
            dst_clean = re.sub(r"\s+", "", dst_hex)
            if not start_clean or not end_clean or not dst_clean:
                continue
            byte_len = max(1, (len(start_clean) + 1) // 2)
            start_code = int(start_clean, 16)
            end_code = int(end_clean, 16)
            if end_code < start_code:
                continue
            dst_val = int(dst_clean, 16)
            dst_hex_len = max(4, len(dst_clean) + (len(dst_clean) % 2))
            span = min(end_code - start_code + 1, MAX_BFRANGE_ENTRIES)
            for offset in range(span):
                cur_hex = f"{dst_val + offset:0{dst_hex_len}x}"
                table[(start_code + offset, byte_len)] = _decode_utf16be_hex(cur_hex)

    return table


def _collect_font_cmaps(
    objects: dict[tuple[int, int], tuple[bytes | None, bytes | None, bytes]],
) -> tuple[dict[str, dict[tuple[int, int], str]], dict[tuple[int, int], str]]:
    """Map PDF font names (e.g. 'F1') and global fallback to /ToUnicode CMap tables."""
    obj_cmaps: dict[tuple[int, int], dict[tuple[int, int], str]] = {}
    fallback_cmap: dict[tuple[int, int], str] = {}

    for key, (dict_bytes, _, body) in objects.items():
        source = dict_bytes or body
        m = re.search(rb"/ToUnicode\s+(\d+)\s+(\d+)\s+R", source)
        if not m:
            continue
        ref = (int(m.group(1)), int(m.group(2)))
        target = objects.get(ref)
        if target is None or target[1] is None:
            continue
        cmap_raw = _decompress_stream(target[0], target[1])
        cmap = _parse_tounicode_cmap(cmap_raw)
        if cmap:
            obj_cmaps[key] = cmap
            fallback_cmap.update(cmap)

    font_name_cmaps: dict[str, dict[tuple[int, int], str]] = {}
    for _, (dict_bytes, _, body) in objects.items():
        source = dict_bytes or body
        if b"/Font" not in source:
            continue
        for fname_b, obj_b, gen_b in re.findall(rb"/([A-Za-z0-9_+.-]+)\s+(\d+)\s+(\d+)\s+R", source):
            ref = (int(obj_b), int(gen_b))
            if ref in obj_cmaps:
                font_name_cmaps[fname_b.decode("latin-1", errors="ignore")] = obj_cmaps[ref]

    return font_name_cmaps, fallback_cmap


def _parse_pdf_literal_string(buf: bytes, start: int) -> tuple[bytes, int]:
    """Parse PDF literal string starting at '(' and return (decoded_bytes, next_index)."""
    out = bytearray()
    n = len(buf)
    i = start + 1
    depth = 1
    escapes = {
        ord("n"): ord("\n"),
        ord("r"): ord("\r"),
        ord("t"): ord("\t"),
        ord("b"): ord("\b"),
        ord("f"): ord("\f"),
        ord("("): ord("("),
        ord(")"): ord(")"),
        ord("\\"): ord("\\"),
    }
    while i < n and depth > 0:
        ch = buf[i]
        if ch == ord("\\"):
            i += 1
            if i >= n:
                break
            esc = buf[i]
            if esc in escapes:
                out.append(escapes[esc])
                i += 1
            elif esc in (ord("\r"), ord("\n")):
                if esc == ord("\r") and i + 1 < n and buf[i + 1] == ord("\n"):
                    i += 2
                else:
                    i += 1
            elif ord("0") <= esc <= ord("7"):
                oct_digits = bytes([esc])
                for _ in range(2):
                    if i + 1 < n and ord("0") <= buf[i + 1] <= ord("7"):
                        i += 1
                        oct_digits += bytes([buf[i]])
                    else:
                        break
                out.append(int(oct_digits, 8) & 0xFF)
                i += 1
            else:
                out.append(esc)
                i += 1
        elif ch == ord("("):
            depth += 1
            out.append(ch)
            i += 1
        elif ch == ord(")"):
            depth -= 1
            if depth > 0:
                out.append(ch)
            i += 1
        else:
            out.append(ch)
            i += 1
    return bytes(out), i


def _parse_pdf_hex_string(buf: bytes, start: int) -> tuple[bytes, int]:
    """Parse PDF hex string starting at '<' and return (raw_bytes, next_index)."""
    end = buf.find(b">", start + 1)
    if end == -1:
        return b"", len(buf)
    hex_chars = re.sub(rb"\s+", b"", buf[start + 1 : end])
    if len(hex_chars) % 2 == 1:
        hex_chars += b"0"
    try:
        raw = bytes.fromhex(hex_chars.decode("ascii"))
    except ValueError:
        raw = b""
    return raw, end + 1


def _decode_pdf_bytes(raw: bytes, cmap: dict[tuple[int, int], str]) -> str:
    """Decode PDF string bytes using CMap if present, else UTF-16BE BOM or Latin-1/UTF-8."""
    if not raw:
        return ""
    if cmap:
        has_2byte = any(bl == 2 for _, bl in cmap)
        has_1byte = any(bl == 1 for _, bl in cmap)
        chars: list[str] = []
        i = 0
        n = len(raw)
        while i < n:
            if has_2byte and i + 1 < n:
                code2 = (raw[i] << 8) | raw[i + 1]
                mapped = cmap.get((code2, 2))
                if mapped is not None:
                    chars.append(mapped)
                    i += 2
                    continue
            if has_1byte:
                mapped1 = cmap.get((raw[i], 1))
                if mapped1 is not None:
                    chars.append(mapped1)
                    i += 1
                    continue
            if has_2byte and not has_1byte and i + 1 < n:
                code2 = (raw[i] << 8) | raw[i + 1]
                if 0x20 <= code2 <= 0x7E:
                    chars.append(chr(code2))
                i += 2
                continue
            b = raw[i]
            if b >= 0x20 or b in (0x09, 0x0A, 0x0D):
                chars.append(chr(b))
            i += 1
        return "".join(chars)

    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be", errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", errors="replace")


def _tokenize_content_stream(stream: bytes) -> list[tuple[str, Any]]:
    """Lex a PDF content stream into typed tokens for text operator evaluation."""
    tokens: list[tuple[str, Any]] = []
    n = len(stream)
    i = 0
    ws = b" \t\r\n\x00\x0c"
    delims = b"()<>[]{}/%"

    while i < n:
        ch = stream[i]
        if ch in ws:
            i += 1
            continue
        if ch == ord("%"):
            nl = stream.find(b"\n", i)
            i = n if nl == -1 else nl + 1
            continue
        if ch == ord("("):
            raw, i = _parse_pdf_literal_string(stream, i)
            tokens.append(("str", raw))
            continue
        if ch == ord("<"):
            if i + 1 < n and stream[i + 1] == ord("<"):
                d_end = _find_matching_dict_end(stream, i)
                i = d_end if d_end is not None else i + 2
                continue
            raw, i = _parse_pdf_hex_string(stream, i)
            tokens.append(("str", raw))
            continue
        if ch == ord("["):
            tokens.append(("arr_start", "["))
            i += 1
            continue
        if ch == ord("]"):
            tokens.append(("arr_end", "]"))
            i += 1
            continue
        if ch == ord("/"):
            j = i + 1
            while j < n and stream[j] not in ws and stream[j] not in delims:
                j += 1
            tokens.append(("name", stream[i + 1 : j].decode("latin-1", errors="ignore")))
            i = j
            continue

        j = i
        while j < n and stream[j] not in ws and stream[j] not in delims:
            j += 1
        word = stream[i:j]
        i = j
        if _NUMBER_TOKEN_RE.match(word):
            tokens.append(("num", float(word)))
        else:
            tokens.append(("op", word.decode("latin-1", errors="ignore")))

    return tokens


def _extract_text_from_content_stream(
    stream: bytes,
    font_cmaps: dict[str, dict[tuple[int, int], str]],
    fallback_cmap: dict[tuple[int, int], str],
) -> str:
    """Interpret PDF text operators (BT/ET, Tf, Td/TD/Tm/T*, Tj/TJ) into UTF-8 text."""
    tokens = _tokenize_content_stream(stream)
    out: list[str] = []
    stack: list[tuple[str, Any]] = []
    in_array = False
    array_items: list[tuple[str, Any]] = []
    current_font = ""
    prev_x: float | None = None
    prev_y: float | None = None

    def active_cmap() -> dict[tuple[int, int], str]:
        return font_cmaps.get(current_font, fallback_cmap)

    def ensure_newline() -> None:
        if out and not out[-1].endswith("\n"):
            out.append("\n")

    def ensure_space() -> None:
        if out and not out[-1].endswith((" ", "\n", "\t")):
            out.append(" ")

    for kind, val in tokens:
        if kind == "arr_start":
            in_array = True
            array_items = []
            continue
        if kind == "arr_end":
            in_array = False
            stack.append(("arr", array_items))
            continue
        if in_array:
            array_items.append((kind, val))
            continue
        if kind != "op":
            stack.append((kind, val))
            continue

        op = val
        if op == "BT":
            prev_x = None
            prev_y = None
        elif op == "ET":
            ensure_newline()
        elif op == "Tf":
            if len(stack) >= 2 and stack[-2][0] == "name":
                current_font = str(stack[-2][1])
        elif op == "T*":
            ensure_newline()
        elif op in ("Td", "TD"):
            if len(stack) >= 2 and stack[-2][0] == "num" and stack[-1][0] == "num":
                tx = float(stack[-2][1])
                ty = float(stack[-1][1])
                if abs(ty) > 1.0:
                    ensure_newline()
                elif tx > 15.0:
                    ensure_space()
        elif op == "Tm":
            if len(stack) >= 6 and all(t[0] == "num" for t in stack[-6:]):
                e = float(stack[-2][1])
                f = float(stack[-1][1])
                if prev_y is not None and abs(f - prev_y) > 1.0:
                    ensure_newline()
                elif prev_x is not None and (e - prev_x) > 15.0:
                    ensure_space()
                prev_x, prev_y = e, f
        elif op == "Tj":
            if stack and stack[-1][0] == "str":
                out.append(_decode_pdf_bytes(stack[-1][1], active_cmap()))
        elif op == "'":
            ensure_newline()
            if stack and stack[-1][0] == "str":
                out.append(_decode_pdf_bytes(stack[-1][1], active_cmap()))
        elif op == '"':
            ensure_newline()
            if stack and stack[-1][0] == "str":
                out.append(_decode_pdf_bytes(stack[-1][1], active_cmap()))
        elif op == "TJ":
            if stack and stack[-1][0] == "arr":
                cmap = active_cmap()
                for item_kind, item_val in stack[-1][1]:
                    if item_kind == "str":
                        out.append(_decode_pdf_bytes(item_val, cmap))
                    elif item_kind == "num" and float(item_val) < -150.0:
                        ensure_space()
        stack.clear()

    return "".join(out)


def _collect_page_content_streams(
    objects: dict[tuple[int, int], tuple[bytes | None, bytes | None, bytes]],
) -> list[bytes]:
    """Collect decompressed content streams in page order, falling back to text-like streams."""
    page_streams: list[bytes] = []
    seen_refs: set[tuple[int, int]] = set()

    for _, (dict_bytes, _, body) in sorted(objects.items()):
        source = dict_bytes or body
        if not re.search(rb"/Type\s*/Page\b(?!s)", source):
            continue
        arr_m = re.search(rb"/Contents\s*\[(.*?)\]", source, re.DOTALL)
        refs: list[tuple[int, int]] = []
        if arr_m:
            for ob, gb in re.findall(rb"(\d+)\s+(\d+)\s+R", arr_m.group(1)):
                refs.append((int(ob), int(gb)))
        else:
            single_m = re.search(rb"/Contents\s+(\d+)\s+(\d+)\s+R", source)
            if single_m:
                refs.append((int(single_m.group(1)), int(single_m.group(2))))

        for ref in refs:
            if ref in seen_refs:
                continue
            obj = objects.get(ref)
            if obj is None or obj[1] is None:
                continue
            seen_refs.add(ref)
            page_streams.append(_decompress_stream(obj[0], obj[1]))

    if page_streams:
        return page_streams

    # Fallback for minimal synthetic PDFs lacking explicit /Type /Page /Contents links
    for _, (dict_bytes, stream_bytes, _) in sorted(objects.items()):
        if stream_bytes is None:
            continue
        if dict_bytes and re.search(rb"/Type\s*/(?:ObjStm|XRef)\b", dict_bytes):
            continue
        decompressed = _decompress_stream(dict_bytes, stream_bytes)
        if b"begincmap" in decompressed:
            continue
        if b"Tj" in decompressed or b"TJ" in decompressed or b"BT" in decompressed:
            page_streams.append(decompressed)

    return page_streams


class ResumeParser:
    """3-tier local resume parser with section weighting and PII scrubbing."""

    def __init__(self, known_phrases: Iterable[str] | None = None) -> None:
        phrases = list(known_phrases) if known_phrases is not None else load_ontology_phrases()
        self._tokenizer = TechnicalTokenizer(phrases)

    def extract_pdf_text_pure(self, data: bytes) -> str:
        """Pure-Python zlib/ObjStm/ToUnicode PDF text extractor."""
        if len(data) > MAX_RESUME_BYTES:
            raise ValueError(f"Resume exceeds 15 MB limit ({len(data)} bytes)")

        objects, has_encrypt = _parse_pdf_objects(data)
        if has_encrypt:
            raise EncryptedPDFError("Encrypted PDF files cannot be parsed")

        _unpack_object_streams(objects)
        font_cmaps, fallback_cmap = _collect_font_cmaps(objects)
        streams = _collect_page_content_streams(objects)
        pages = [
            _extract_text_from_content_stream(stream, font_cmaps, fallback_cmap)
            for stream in streams
        ]
        return "\n".join(p.strip() for p in pages if p.strip())

    def _extract_pdf_text(self, data: bytes) -> str:
        """3-tier PDF extraction: (a) optional pypdf, (b) pdftotext, (c) pure-Python."""
        # Tier (a): optional pypdf
        try:
            pypdf = importlib.import_module("pypdf")
            reader = pypdf.PdfReader(io.BytesIO(data))
            if getattr(reader, "is_encrypted", False):
                raise EncryptedPDFError("Encrypted PDF files cannot be parsed")
            chunks = [page.extract_text() or "" for page in reader.pages]
            text = "\n".join(c for c in chunks if c.strip())
            if text.strip():
                return text
        except ImportError:
            pass
        except EncryptedPDFError:
            raise
        except Exception:
            pass

        # Tier (b): native pdftotext via PATH or /usr/bin/pdftotext (zero temp files)
        pdftotext_bin = shutil.which("pdftotext") or "/usr/bin/pdftotext"
        if pdftotext_bin and os.path.isfile(pdftotext_bin) and os.access(pdftotext_bin, os.X_OK):
            try:
                proc = subprocess.run(
                    [pdftotext_bin, "-layout", "-enc", "UTF-8", "-", "-"],
                    input=data,
                    capture_output=True,
                    shell=False,
                    timeout=10,
                )
                stderr_low = proc.stderr.decode("utf-8", errors="ignore").lower()
                if "encrypted" in stderr_low or "password" in stderr_low:
                    raise EncryptedPDFError("Encrypted PDF files cannot be parsed")
                if proc.returncode == 0:
                    decoded = proc.stdout.decode("utf-8", errors="replace")
                    if decoded.strip():
                        return decoded
            except EncryptedPDFError:
                raise
            except (OSError, subprocess.SubprocessError):
                pass

        # Tier (c): built-in pure-Python parser
        return self.extract_pdf_text_pure(data)

    def parse_file(self, path: Path | str) -> list[SignalEvidence]:
        """Parse a resume file (.pdf, .md, .txt, .tex) up to 15 MB into SignalEvidence."""
        target = Path(path)
        if not target.is_file():
            raise FileNotFoundError(f"Resume file not found: {target}")
        size = target.stat().st_size
        if size > MAX_RESUME_BYTES:
            raise ValueError(f"Resume file exceeds 15 MB limit ({size} bytes)")
        ext = target.suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            raise ValueError(f"Unsupported resume format: {ext}")
        data = target.read_bytes()
        return self.parse_bytes(data, filename=target.name)

    def parse_bytes(self, data: bytes, filename: str = "resume.pdf") -> list[SignalEvidence]:
        """Parse in-memory resume bytes into section-weighted, PII-scrubbed SignalEvidence."""
        if len(data) > MAX_RESUME_BYTES:
            raise ValueError(f"Resume exceeds 15 MB limit ({len(data)} bytes)")

        safe_name = Path(filename).name or "resume.pdf"
        ext = Path(safe_name).suffix.lower()
        if ext and ext not in SUPPORTED_EXTENSIONS:
            raise ValueError(f"Unsupported resume format: {ext}")

        is_pdf = ext == ".pdf" or data.lstrip().startswith(b"%PDF-")
        if is_pdf:
            raw_text = self._extract_pdf_text(data)
        else:
            raw_text = data.decode("utf-8", errors="replace")
            if ext == ".tex":
                raw_text = _strip_latex(raw_text)

        cleaned = scrub_pii(raw_text)
        return self._extract_sectioned_evidences(cleaned, safe_name)

    def _extract_sectioned_evidences(
        self, text: str, safe_name: str
    ) -> list[SignalEvidence]:
        """Detect resume sections, apply multipliers, and emit SignalEvidence."""
        section_buffers: dict[str, list[str]] = {}
        current_section = "general"

        for raw_line in text.splitlines():
            stripped = raw_line.strip()
            if not stripped:
                continue
            matched_section: str | None = None
            remainder = ""
            for sec_name, pattern in _SECTION_PATTERNS:
                m = pattern.match(stripped)
                if m:
                    matched_section = sec_name
                    remainder = (m.group(1) or "").strip()
                    break

            if matched_section is not None:
                current_section = matched_section
                if remainder:
                    section_buffers.setdefault(current_section, []).append(remainder)
            else:
                section_buffers.setdefault(current_section, []).append(stripped)

        evidences: list[SignalEvidence] = []
        for sec_name, lines in section_buffers.items():
            mult = SECTION_MULTIPLIERS.get(sec_name, 1.0)
            detail = f"resume:{safe_name}#section={sec_name}"
            sec_text = "\n".join(lines)
            for term, tok_w in self._tokenizer.tokenize(sec_text):
                evidences.append(
                    SignalEvidence(
                        term=term,
                        raw_term=term,
                        weight=round(mult * tok_w, 4),
                        source_type="resume",
                        source_detail=detail,
                        signal_kind="keyword",
                    )
                )
        return evidences
