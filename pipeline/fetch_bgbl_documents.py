"""Capture official BGBl amendment documents referenced by the GII corpus.

This is the primary-source document layer behind Lexgraph's independently
computed history. It joins the latest curated Gesetze-im-Internet snapshot
with the latest Bundestag DIP snapshot by BGBl year and issue.

For every joined issue the canonical recht.bund.de ``VO.html`` page and its
``regelungstext.pdf`` are captured. The advertised MD5 is checked before any
metadata snapshot is published. PDF and extracted text are stored once in a
content-addressed object store; the reproducible snapshot contains metadata
and object references.

The article splitter is deliberately descriptive. It preserves complete
article text and identifies the article containing ``Inkrafttreten`` but does
not infer effective dates for individual amendment instructions. DIP's raw
``inkrafttreten`` entries are retained separately as official evidence.

Output:
    data/bgbl_documents/objects/<pdf-sha256>.pdf
    data/bgbl_documents/texts/<text-sha256>.txt
    data/snapshots/bgbl_documents/<date>/documents.jsonl

Usage:
    python3 pipeline/fetch_bgbl_documents.py [--limit 40] [--refresh]
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from pypdf import PdfReader

from common import Http, ROOT, latest_snapshot, read_jsonl, snapshot_dir


STORE = ROOT / "data" / "bgbl_documents"
PDF_OBJECTS = STORE / "objects"
TEXT_OBJECTS = STORE / "texts"

# GII examples:
#   zuletzt geändert durch Art. 3 G v. 23.4.2026 I Nr. 112
#   zuletzt geändert durch Art. 11 Abs. 17 G v. 16.4.2026 I Nr. 107
# ``I Nr.`` is intentionally required: that is the stable BGBl issue join.
GII_BGBL_RE = re.compile(
    r"Art\.\s*"
    r"(?P<article>\d+[a-z]?(?:\s+Abs\.\s*\d+[a-z]?)?)\s+"
    r"(?P<instrument>[A-ZÄÖÜ][\wÄÖÜäöüß-]*)\s+v\.\s*"
    r"(?P<day>\d{1,2})\.(?P<month>\d{1,2})\.(?P<year>\d{4})\s+"
    r"I\s+Nr\.\s*(?P<issue>\d+)",
    flags=re.UNICODE,
)

# Requiring a standalone line prevents references such as "nach Artikel 7
# der Verordnung ..." from splitting a section in the middle of its text.
ARTICLE_RE = re.compile(
    r"(?m)^[ \t]*Artikel[ \t]+(?P<number>\d+[a-z]?|[IVXLCDM]+)[ \t]*$",
    flags=re.IGNORECASE,
)
ENTRY_RE = re.compile(r"(?im)^[ \t]*Inkrafttreten(?:\s+und\s+[^\n]+)?[ \t]*$")


class SourceIntegrityError(RuntimeError):
    """Official source metadata and downloaded bytes do not agree."""


def _compact(value: object) -> str:
    return " ".join(str(value or "").split())


def _iso_german_date(value: str | None) -> str | None:
    raw = _compact(value)
    if not raw:
        return None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _issue(value: object) -> str:
    raw = str(value or "").strip()
    if not raw.isdigit():
        raise ValueError(f"invalid BGBl issue: {value!r}")
    return str(int(raw))


def canonical_urls(year: object, issue: object) -> dict[str, str]:
    """Return stable official URLs, rejecting non-numeric path components."""
    year_s = str(year or "").strip()
    if not (year_s.isdigit() and len(year_s) == 4):
        raise ValueError(f"invalid BGBl year: {year!r}")
    issue_s = _issue(issue)
    base = f"https://www.recht.bund.de/bgbl/1/{year_s}/{issue_s}"
    return {
        "eli": f"https://www.recht.bund.de/eli/bund/bgbl-1/{year_s}/{issue_s}",
        "html": f"{base}/VO.html",
        "pdf": f"{base}/regelungstext.pdf?__blob=publicationFile&v=1",
    }


def parse_gii_references(acts: list[dict]) -> dict[tuple[str, str], list[dict]]:
    """Map ``(year, issue)`` to the corpus acts/articles citing that issue."""
    joined: dict[tuple[str, str], list[dict]] = {}
    for act in acts:
        stand = str(act.get("stand") or "")
        for match in GII_BGBL_RE.finditer(stand):
            year = match.group("year")
            issue = _issue(match.group("issue"))
            row = {
                "slug": act.get("slug"),
                "jurabk": act.get("jurabk"),
                "article": _compact(match.group("article")),
                "instrument_kind": match.group("instrument"),
                "amendment_date": (
                    f"{year}-{int(match.group('month')):02d}-"
                    f"{int(match.group('day')):02d}"
                ),
                "citation": _compact(match.group(0)),
            }
            bucket = joined.setdefault((year, issue), [])
            identity = (row["slug"], row["article"], row["citation"])
            if not any((item.get("slug"), item.get("article"),
                        item.get("citation")) == identity for item in bucket):
                bucket.append(row)
    for refs in joined.values():
        refs.sort(key=lambda row: (str(row.get("jurabk") or "").casefold(),
                                   str(row.get("article") or "")))
    return joined


def select_documents(acts: list[dict], procedures: list[dict]) -> list[dict]:
    """Join the latest official GII and DIP snapshots by BGBl year/issue."""
    references = parse_gii_references(acts)
    selected: dict[tuple[str, str], dict] = {}
    for procedure in procedures:
        for promulgation in procedure.get("verkuendung") or []:
            try:
                key = (str(promulgation.get("jahrgang") or "").strip(),
                       _issue(promulgation.get("heftnummer")))
            except ValueError:
                continue
            refs = references.get(key)
            if not refs:
                continue
            urls = canonical_urls(*key)
            row = {
                "document_id": f"bgbl-1-{key[0]}-{key[1]}",
                "year": key[0],
                "issue": key[1],
                "title": _compact(procedure.get("titel")),
                "eli": str(promulgation.get("pdf_url") or urls["eli"]),
                "official_html_url": urls["html"],
                "official_pdf_url": urls["pdf"],
                "publication_date": promulgation.get("verkuendungsdatum"),
                "execution_date": promulgation.get("ausfertigungsdatum"),
                "procedure_id": str(procedure.get("id") or ""),
                "procedure_status": procedure.get("beratungsstand"),
                "procedure_updated_at": procedure.get("aktualisiert"),
                # Retained verbatim; not mapped to articles by this step.
                "dip_entry_into_force": procedure.get("inkrafttreten") or [],
                "referenced_corpus_acts": refs,
            }
            old = selected.get(key)
            # DIP should expose one procedure per promulgation. If it ever
            # emits duplicates, prefer the most recently updated record.
            if old is None or str(row["procedure_updated_at"] or "") > str(
                    old.get("procedure_updated_at") or ""):
                selected[key] = row
    rows = list(selected.values())
    rows.sort(key=lambda row: (
        str(row.get("publication_date") or ""), int(row["year"]),
        int(row["issue"])), reverse=True)
    return rows


def parse_vo_html(html: bytes | str, requested_url: str) -> dict:
    """Parse one official recht.bund.de promulgation page."""
    soup = BeautifulSoup(html, "html.parser")
    canonical = soup.find("link", rel="canonical")
    canonical_url = (canonical.get("href") if canonical else None) or requested_url
    title_node = soup.select_one("h1#introH") or soup.find("h1")
    title = _compact(title_node.get_text(" ", strip=True) if title_node else "")

    fields: dict[str, str] = {}
    for item in soup.select('[role="listitem"]'):
        label = item.find("strong")
        if label is None:
            continue
        label_text = _compact(label.get_text(" ", strip=True))
        key = label_text.rstrip(":")
        value = _compact(item.get_text(" ", strip=True).removeprefix(label_text))
        if key and value:
            fields[key] = value

    link = soup.find("a", href=re.compile(r"regelungstext\.pdf", re.I))
    if link is None:
        raise SourceIntegrityError("official page has no Regelungstext PDF")
    pdf_url = urljoin(canonical_url, str(link.get("href") or ""))
    parsed = urlparse(pdf_url)
    if parsed.scheme != "https" or parsed.netloc != "www.recht.bund.de":
        raise SourceIntegrityError(f"unexpected official PDF host: {pdf_url}")

    md5_node = soup.select_one(".c-tooltip__content--hash")
    advertised_md5 = _compact(md5_node.get_text(strip=True) if md5_node else "")
    if advertised_md5 and not re.fullmatch(r"[0-9a-fA-F]{32}", advertised_md5):
        raise SourceIntegrityError(f"invalid advertised MD5: {advertised_md5!r}")

    eli_link = soup.find("a", href=re.compile(r"/eli/bund/", re.I))
    eli = str(eli_link.get("href")) if eli_link else None
    return {
        "title": title,
        "canonical_url": canonical_url,
        "eli": eli,
        "pdf_url": pdf_url,
        "advertised_md5": advertised_md5.lower() or None,
        "publication_date": _iso_german_date(fields.get(
            "Veröffentlichungsdatum")),
        "execution_date": _iso_german_date(fields.get("Ausfertigungsdatum")),
        "official_fields": fields,
    }


def verify_advertised_md5(payload: bytes, advertised: str | None) -> str:
    """Return computed MD5 or fail closed when an advertised digest differs."""
    computed = hashlib.md5(payload).hexdigest()  # noqa: S324 — source checksum
    if advertised and computed.casefold() != advertised.casefold():
        raise SourceIntegrityError(
            f"official PDF MD5 mismatch: advertised {advertised}, got {computed}")
    return computed


def extract_pdf_text(payload: bytes) -> tuple[str, int]:
    """Extract every page in reading order using pypdf."""
    try:
        reader = PdfReader(io.BytesIO(payload))
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
    except Exception as exc:  # pypdf exposes several parser exception classes
        raise SourceIntegrityError(f"cannot extract official PDF: {exc}") from exc
    text = "\n\f\n".join(pages).strip() + "\n"
    if not text.strip():
        raise SourceIntegrityError("official PDF yielded no text")
    return text, len(reader.pages)


def parse_article_sections(text: str) -> tuple[list[dict], dict | None]:
    """Split full BGBl text at standalone article headings.

    ``entry_into_force`` is the unmodified article section containing the
    standalone heading ``Inkrafttreten``. No dates or scopes are inferred.
    """
    matches = list(ARTICLE_RE.finditer(text))
    sections: list[dict] = []
    entry: dict | None = None
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = text[match.start():end].strip()
        lines = block.splitlines()
        body_lines = lines[1:]
        heading = ""
        for line in body_lines:
            if line.strip():
                heading = _compact(line)
                break
        section = {
            "article": _compact(match.group("number")),
            "heading": heading or None,
            "text": block,
        }
        sections.append(section)
        if entry is None and ENTRY_RE.search(block):
            entry = dict(section)
            entry["effective_dates_inferred"] = False
    return sections, entry


def _atomic_bytes(path: Path, payload: bytes) -> None:
    """Create or validate an immutable CAS object atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    expected = hashlib.sha256(payload).hexdigest()
    if path.exists():
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise SourceIntegrityError(f"corrupt CAS object: {path}")
        return
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _atomic_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                      for row in rows).encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _cas_path(relative: object, expected_root: Path) -> Path:
    raw = str(relative or "")
    path = (ROOT / raw).resolve()
    try:
        path.relative_to(expected_root.resolve())
    except ValueError as exc:
        raise SourceIntegrityError(f"object path escapes CAS: {raw}") from exc
    return path


def _validate_cached(row: dict) -> bool:
    try:
        pdf = _cas_path(row.get("pdf_object"), PDF_OBJECTS)
        text = _cas_path(row.get("text_object"), TEXT_OBJECTS)
        if not pdf.is_file() or not text.is_file():
            return False
        return (hashlib.sha256(pdf.read_bytes()).hexdigest() == row.get("sha256")
                and hashlib.sha256(text.read_bytes()).hexdigest() ==
                row.get("text_sha256"))
    except (OSError, SourceIntegrityError):
        return False


def load_cached_documents() -> dict[str, dict]:
    latest = latest_snapshot("bgbl_documents")
    path = latest / "documents.jsonl" if latest else None
    if path is None or not path.is_file():
        return {}
    rows = {}
    for row in read_jsonl(path):
        if row.get("document_id") and _validate_cached(row):
            rows[str(row["document_id"])] = row
    return rows


def _same_official_dates(candidate: dict, parsed: dict) -> None:
    """Reject a join if the official HTML contradicts DIP's issue dates."""
    for key in ("publication_date", "execution_date"):
        dip_value = candidate.get(key)
        page_value = parsed.get(key)
        if dip_value and page_value and dip_value != page_value:
            raise SourceIntegrityError(
                f"{candidate['document_id']} {key} mismatch: "
                f"DIP {dip_value}, recht.bund.de {page_value}")


def capture_document(http: Http, candidate: dict) -> dict:
    """Download, verify, extract and persist one selected document."""
    response = http.get(candidate["official_html_url"], timeout=90)
    response.raise_for_status()
    parsed = parse_vo_html(response.content, candidate["official_html_url"])
    _same_official_dates(candidate, parsed)

    expected_path = urlparse(candidate["official_pdf_url"]).path
    actual_path = urlparse(parsed["pdf_url"]).path
    if expected_path != actual_path:
        raise SourceIntegrityError(
            f"unexpected PDF path for {candidate['document_id']}: {actual_path}")
    pdf_response = http.get(parsed["pdf_url"], timeout=240)
    pdf_response.raise_for_status()
    payload = pdf_response.content
    if not payload.startswith(b"%PDF-"):
        raise SourceIntegrityError(
            f"{candidate['document_id']} did not return a PDF")

    md5 = verify_advertised_md5(payload, parsed["advertised_md5"])
    sha256 = hashlib.sha256(payload).hexdigest()
    pdf_path = PDF_OBJECTS / f"{sha256}.pdf"
    _atomic_bytes(pdf_path, payload)

    text, page_count = extract_pdf_text(payload)
    text_payload = text.encode("utf-8")
    text_sha256 = hashlib.sha256(text_payload).hexdigest()
    text_path = TEXT_OBJECTS / f"{text_sha256}.txt"
    _atomic_bytes(text_path, text_payload)
    articles, entry = parse_article_sections(text)

    row = dict(candidate)
    row.update({
        "title": parsed["title"] or candidate.get("title"),
        "eli": parsed["eli"] or candidate.get("eli"),
        "official_html_url": parsed["canonical_url"],
        "official_pdf_url": parsed["pdf_url"],
        "sha256": sha256,
        "md5": md5,
        "advertised_md5": parsed["advertised_md5"],
        "integrity_verified": bool(parsed["advertised_md5"]),
        "pdf_bytes": len(payload),
        "page_count": page_count,
        "pdf_object": str(pdf_path.relative_to(ROOT)),
        "text_sha256": text_sha256,
        "text_bytes": len(text_payload),
        "text_object": str(text_path.relative_to(ROOT)),
        "articles": articles,
        "entry_into_force": entry,
        "effective_dates_inferred": False,
        "official_fields": parsed["official_fields"],
        "captured_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "source": "recht.bund.de+dip+gii",
    })
    return row


def reuse_document(cached: dict, candidate: dict) -> dict:
    """Reuse immutable bytes while refreshing only official join metadata."""
    row = dict(cached)
    for key in (
        "publication_date", "execution_date", "procedure_id", "procedure_status",
        "procedure_updated_at", "dip_entry_into_force", "referenced_corpus_acts",
    ):
        row[key] = candidate.get(key)
    return row


def cumulative_documents(cached: dict[str, dict],
                         refreshed: list[dict]) -> list[dict]:
    """Keep captured final documents after GII's latest citation moves on.

    GII describes only the current consolidation.  If tomorrow's ``stand``
    cites a newer amending issue, dropping yesterday's BGBl metadata would
    make an already accepted legal transition disappear from Lexgraph.  The
    CAS and this snapshot are therefore cumulative; a refreshed document wins
    by stable ``document_id`` while older integrity-checked rows remain.
    """
    rows = dict(cached)
    for row in refreshed:
        document_id = str(row.get("document_id") or "")
        if not document_id:
            raise SourceIntegrityError("document has no stable id")
        rows[document_id] = row
    return sorted(rows.values(), key=lambda row: (
        str(row.get("publication_date") or ""),
        int(str(row.get("year") or "0")),
        int(str(row.get("issue") or "0")),
        str(row.get("document_id") or "")), reverse=True)


def _latest_rows(source: str, name: str) -> list[dict]:
    latest = latest_snapshot(source)
    path = latest / name if latest else None
    if path is None or not path.is_file():
        raise FileNotFoundError(f"no {source}/{name} snapshot")
    return list(read_jsonl(path))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=40,
                        help="maximum official documents per run (1..200)")
    parser.add_argument("--refresh", action="store_true",
                        help="re-download even when verified CAS objects exist")
    parser.add_argument("--delay", type=float, default=0.8,
                        help="minimum delay between official requests")
    args = parser.parse_args()
    if not 1 <= args.limit <= 200:
        parser.error("--limit must be between 1 and 200")
    if args.delay < 0.2:
        parser.error("--delay must be at least 0.2 seconds")

    acts = _latest_rows("gii", "acts.jsonl")
    procedures = _latest_rows("dip", "vorgaenge.jsonl")
    candidates = select_documents(acts, procedures)[:args.limit]
    historical = load_cached_documents()
    reusable = {} if args.refresh else historical
    http = Http(delay=args.delay, retries=4)

    refreshed: list[dict] = []
    for index, candidate in enumerate(candidates, start=1):
        previous = reusable.get(candidate["document_id"])
        if previous:
            row = reuse_document(previous, candidate)
            action = "cached"
        else:
            row = capture_document(http, candidate)
            action = "captured"
        refreshed.append(row)
        print(f"[{index:02d}/{len(candidates):02d}] {action:8} "
              f"BGBl I {row['year']} Nr. {row['issue']}  "
              f"{len(row.get('articles') or [])} articles")

    # Only a fully successful batch replaces today's metadata snapshot. CAS
    # objects written before a later failure are harmless and reusable.  The
    # new snapshot remains cumulative so accepted history cannot disappear
    # when a later GII consolidation cites a newer BGBl issue.
    rows = cumulative_documents(historical, refreshed)
    out = snapshot_dir("bgbl_documents")
    _atomic_jsonl(out / "documents.jsonl", rows)
    print(f"\n{len(refreshed)} current joins; {len(rows)} cumulative verified "
          f"official documents -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
