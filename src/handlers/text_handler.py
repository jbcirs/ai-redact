"""Native (Kind B) handler for plain-text formats.

Redacts .txt/.md/.log/.json/.yaml/.yml/.xml/.html/.htm files in place
(format-preserving): the file is treated as one text stream, every matched
span is replaced with common.redaction_text(), and everything else is
carried over byte-for-byte where the encoding allows. For HTML, href/src
attribute values (mailto:, tel:, ...) are additionally scanned so link
targets can't leak data the visible text no longer shows.

Contract: docs/plans/handler-spec.md (Kind B).
"""

from __future__ import annotations

import html
import pathlib
import re
import sys
import urllib.parse

try:
    from handlers.common import UnsupportedFormatError, redaction_text
except ImportError:  # direct execution (smoke test): put src/ on the path
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    from handlers.common import UnsupportedFormatError, redaction_text

SUPPORTED_EXTENSIONS = {".txt", ".md", ".log", ".json", ".yaml", ".yml",
                        ".xml", ".html", ".htm"}

_HTML_EXTENSIONS = {".html", ".htm"}

# href/src attribute values (double-quoted, single-quoted, or bare).
_ATTR_RE = re.compile(
    r"""\b(?:href|src)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>"']+))""",
    re.IGNORECASE)


def _read_text(path: pathlib.Path) -> tuple[str, list]:
    """Decode a file as UTF-8, falling back to latin-1 with a report note.

    Latin-1 can decode any byte sequence, so this never raises for
    encoding reasons — but the fallback is noted because the redacted
    output is then written as UTF-8 (the redaction block character does
    not exist in latin-1), which re-encodes any non-ASCII bytes.
    """
    data = path.read_bytes()
    try:
        return data.decode("utf-8"), []
    except UnicodeDecodeError:
        return data.decode("latin-1"), [
            f"{path.name}: not valid UTF-8 — decoded as latin-1; "
            "output written as UTF-8 (non-ASCII bytes re-encoded)"]


def _decoded_variants(text: str) -> list:
    """The text plus its HTML-entity- and percent-decoded forms.

    Entity/percent encoding defeats plain regexes (john&#64;example.com,
    mailto:john%40example.com), so HTML is scanned in every decoding.
    """
    variants = [text]
    for v in (html.unescape(text), urllib.parse.unquote(text),
              urllib.parse.unquote(html.unescape(text))):
        if v not in variants:
            variants.append(v)
    return variants


def _collect_matches(text: str, matcher, is_html: bool) -> list:
    """All (category, matched_string) pairs to redact, longest first.

    For HTML the matcher runs over the raw text AND its decoded variants,
    plus each href/src attribute value in isolation — catching identifiers
    a full-document scan can miss (entity/percent encoding, or a value
    flush against quote characters or URI punctuation).
    """
    if not is_html:
        return matcher(text)  # already deduplicated, longest first
    found = dict.fromkeys(matcher(text))
    for variant in _decoded_variants(text):
        for hit in matcher(variant):
            found.setdefault(hit)
        for m in _ATTR_RE.finditer(variant):
            value = m.group(1) or m.group(2) or m.group(3) or ""
            for hit in matcher(value):
                found.setdefault(hit)
    return sorted(found, key=lambda cs: -len(cs[1]))


# Named entities html.escape/serializers commonly produce; numeric forms
# are generated per character in _encoded_span_re.
_NAMED_ENTITIES = {"&": "&amp;", "<": "&lt;", ">": "&gt;",
                   '"': "&quot;", "'": "&apos;"}


def _encoded_span_re(match: str) -> re.Pattern:
    """Regex matching `match` with each character in ANY encoding.

    Per character it accepts: the raw character, its named entity (&amp;),
    decimal (&#64;) and hex (&#x40;) numeric entities, and its
    percent-encoded form (%40, upper or lower hex). This is a superset of
    the three prescribed forms (raw, html.escape(match),
    urllib.parse.quote(match)) — numeric entities are included because
    verification decodes them, so replacement must remove them too.
    """
    parts = []
    for ch in match:
        o = ord(ch)
        alts = []
        if ch in _NAMED_ENTITIES:
            alts.append(_NAMED_ENTITIES[ch])
        alts.append(f"&#0*{o};")
        alts.append(f"&#[xX]0*(?:{o:x}|{o:X});")
        quoted = urllib.parse.quote(ch, safe="")
        if quoted != ch:
            alts.append(re.escape(quoted))
            alts.append(re.escape(quoted.lower()))
        alts.append(re.escape(ch))
        parts.append("(?:" + "|".join(dict.fromkeys(alts)) + ")")
    return re.compile("".join(parts))


def redact_file(input_path, output_path, matcher, dry_run: bool,
                options: dict) -> dict:
    """Scan (and unless dry_run, write) a redacted copy of a text file."""
    input_path = pathlib.Path(input_path)
    ext = input_path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise UnsupportedFormatError(
            f"text_handler does not support '{ext}' files "
            f"(supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}).")

    text, notes = _read_text(input_path)
    is_html = ext in _HTML_EXTENSIONS

    results = {
        "counts": {},
        "matches": [],
        "unit_label": "line",
        "unit_count": len(text.splitlines()),
        "notes": notes,
    }

    # Longest first, so shorter hits that are substrings of longer ones
    # never leave fragments behind. Every occurrence of each matched
    # string is replaced, not just the span the matcher happened to see.
    working = text
    for category, matched in _collect_matches(text, matcher, is_html):
        if is_html:
            # Also erase entity/percent-encoded spellings of the match.
            pattern = _encoded_span_re(matched)
            first = pattern.search(working)
            if not first:
                continue  # subsumed by a longer, already-redacted match
            line_no = working.count("\n", 0, first.start()) + 1
            working, n = pattern.subn(redaction_text(matched), working)
        else:
            n = working.count(matched)
            if n == 0:
                continue  # subsumed by a longer, already-redacted match
            line_no = working.count("\n", 0, working.find(matched)) + 1
            working = working.replace(matched, redaction_text(matched))
        results["counts"][category] = results["counts"].get(category, 0) + n
        results["matches"].append((f"line {line_no}", category, matched, True))

    if not dry_run:
        pathlib.Path(output_path).write_bytes(working.encode("utf-8"))
    return results


def verify_file(output_path, matcher, options: dict) -> dict:
    """Re-read the written output and re-run the matcher over everything.

    Returns {category: remaining_count} — {} proves the sensitive text is
    actually gone from the file. HTML attribute values and entity/percent
    decodings are re-scanned too (same construction as redact_file).
    """
    output_path = pathlib.Path(output_path)
    text, _ = _read_text(output_path)
    is_html = output_path.suffix.lower() in _HTML_EXTENSIONS
    remaining = {}
    for category, _matched in _collect_matches(text, matcher, is_html):
        remaining[category] = remaining.get(category, 0) + 1
    return remaining


# ---------------------------------------------------------------------------
# Standalone smoke test (see handler-spec.md): trivial email matcher only.
# ---------------------------------------------------------------------------
def _smoke_test() -> int:
    import tempfile

    email_re = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

    def matcher(text):
        hits = dict.fromkeys(("email", m.group(0))
                             for m in email_re.finditer(text))
        return sorted(hits, key=lambda cs: -len(cs[1]))

    planted = "planted.email@example.com"
    fixtures = {
        "sample.txt": (f"Report for Casey Plantedname\n"
                       f"Email: {planted}\nPhone: (555) 010-9999\n"
                       f"Balance: $12,345.67\n"),
        "sample.html": (f"<html><body><p>Balance $12,345.67</p>\n"
                        f'<a href="mailto:{planted}">contact</a>\n'
                        f"</body></html>\n"),
        # Entity- and percent-encoded plantings: only findable via the
        # decoded-variant scan, only removable via encoded-form replacement.
        "encoded.html": ("<html><body><p>Balance $12,345.67</p>\n"
                         '<a href="mailto:planted.email%40example.com">e</a>\n'
                         "<p>planted.email&#64;example.com</p>\n"
                         "</body></html>\n"),
    }

    with tempfile.TemporaryDirectory() as td:
        for name, content in fixtures.items():
            src = pathlib.Path(td) / name
            dst = pathlib.Path(td) / ("redacted_" + name)
            src.write_text(content, encoding="utf-8")

            dry = redact_file(src, dst, matcher, dry_run=True, options={})
            if dst.exists():
                print(f"FAIL: {name}: dry_run wrote an output file")
                return 1
            if dry["counts"].get("email", 0) < 1:
                print(f"FAIL: {name}: dry_run found no planted email")
                return 1

            res = redact_file(src, dst, matcher, dry_run=False, options={})
            out = dst.read_text(encoding="utf-8")
            for form in (out, html.unescape(out), urllib.parse.unquote(out)):
                if planted in form:
                    print(f"FAIL: {name}: planted email survived redaction")
                    return 1
            if "$12,345.67" not in out:
                print(f"FAIL: {name}: financial string was not preserved")
                return 1
            if res["unit_label"] != "line" or not res["matches"]:
                print(f"FAIL: {name}: bad result schema: {res}")
                return 1
            if verify_file(dst, matcher, {}) != {}:
                print(f"FAIL: {name}: verify_file found remaining matches")
                return 1
            print(f"ok: {name}: {res['counts']} across "
                  f"{res['unit_count']} lines; verify clean")
    print("text_handler smoke test PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(_smoke_test())
