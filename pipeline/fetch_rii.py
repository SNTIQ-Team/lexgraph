"""Fetch corpus-relevant federal case law from Rechtsprechung im Internet.

Rechtsprechung im Internet (RII, BMJV/BfJ) publishes one rolling RSS feed
for each federal supreme court and the Bundespatentgericht.  Every RSS GUID
has an official ZIP containing the current XML record:

    https://www.rechtsprechung-im-internet.de/jportal/docs/feed/
        bsjrs-{court}.xml
    https://www.rechtsprechung-im-internet.de/jportal/docs/bsjrs/{guid}.zip

RSS is only a rollover window, so this fetcher is cumulative.  It merges new
rows with the latest successful RII snapshot by decision id.  ZIPs are cached
under data/cache/rii/ because a GUID's linked document can later gain richer
metadata without being emitted by the feed again.

Only decisions whose official <norm> metadata names an act in the latest GII
corpus are retained.  Matching uses abbreviation aliases with real word
boundaries; in particular SGB slugs match both Arabic and Roman book numbers.

Output:
    data/snapshots/rii/<date>/decisions.jsonl

Usage:
    python3 pipeline/fetch_rii.py
    python3 pipeline/fetch_rii.py --courts bsg --limit 25

The selected feeds and every selected ZIP must be fetched or parsed completely
before the snapshot is atomically replaced.  A failed/empty run therefore
cannot overwrite the last good cumulative snapshot.
"""
from __future__ import annotations

import argparse
import io
import os
import re
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import requests

from common import ROOT, Http, read_jsonl, write_jsonl


BASE = "https://www.rechtsprechung-im-internet.de"
FEED_TMPL = BASE + "/jportal/docs/feed/bsjrs-{court}.xml"
ZIP_TMPL = BASE + "/jportal/docs/bsjrs/{guid}.zip"
CACHE = ROOT / "data" / "cache" / "rii"
CACHE_MAX_AGE = 7 * 24 * 60 * 60
SNAPSHOTS = ROOT / "data" / "snapshots"
SOURCE = "Rechtsprechung im Internet (BMJV/BfJ)"

# CLI key -> XML abbreviation, full display name.
COURTS: dict[str, tuple[str, str]] = {
    "bverfg": ("BVerfG", "Bundesverfassungsgericht"),
    "bgh": ("BGH", "Bundesgerichtshof"),
    "bverwg": ("BVerwG", "Bundesverwaltungsgericht"),
    "bfh": ("BFH", "Bundesfinanzhof"),
    "bag": ("BAG", "Bundesarbeitsgericht"),
    "bsg": ("BSG", "Bundessozialgericht"),
    "bpatg": ("BPatG", "Bundespatentgericht"),
}

GUID_RE = re.compile(r"jb-[A-Za-z0-9._-]+$")
YEAR_SUFFIX_RE = re.compile(r"(?:[_\s](?:19|20)\d{2})$")
DOCTYPE_RE = re.compile(br"<!DOCTYPE\b.*?>", re.IGNORECASE | re.DOTALL)
ENTITY_RE = re.compile(br"<!ENTITY\b", re.IGNORECASE)
# A citation token may be a real lettered norm ("§ 10f") or use German
# following-section shorthand ("§ 10 f.", "§§ 330ff"). Keep the latter in
# the match so it cannot be mistaken for the nonexistent lettered norm 330f.
_SECTION_TOKEN = (
    r"\d+(?:(?:[\s\u00a0]*f{1,2}\.|ff)|[a-z])?"
)
SECTION_RE = re.compile(
    rf"(?:§{{1,2}}|Art\.?)[\s\u00a0]*"
    rf"(?P<nums>{_SECTION_TOKEN}"
    rf"(?:[\s\u00a0]*(?:,|und|bis|[-–—])"
    rf"[\s\u00a0]*{_SECTION_TOKEN})*)",
    re.IGNORECASE,
)
SECTION_TOKEN_RE = re.compile(
    r"(?P<number>\d+)(?P<tail>(?:[\s\u00a0]*f{1,2}\.|ff)|[a-z])?",
    re.IGNORECASE,
)
FOLLOWING_RE = re.compile(
    r"[\s\u00a0]*(?:f{1,2}\.|ff)$", re.IGNORECASE)
MAX_ZIP_BYTES = 64 * 1024 * 1024
MAX_XML_BYTES = 128 * 1024 * 1024

ROMAN = {
    1: "I", 2: "II", 3: "III", 4: "IV", 5: "V", 6: "VI",
    7: "VII", 8: "VIII", 9: "IX", 10: "X", 11: "XI",
    12: "XII", 13: "XIII", 14: "XIV",
}

# Current court metadata does not always use the GII header abbreviation.
# These are aliases, not replacements: the exact jurabk remains authoritative.
KNOWN_ALIASES: dict[str, tuple[str, ...]] = {
    "asylvfg_1992": ("AsylG", "AsylVfG"),
    "aufenthg_2004": ("AufenthG",),
    "freiz_gg_eu_2004": ("FreizügG/EU", "FreizG/EU"),
    "sgb_9_2018": ("SGB 9", "SGB IX"),
    "stag": ("StAG", "RuStAG"),
    "waffg_2002": ("WaffG",),
}


@dataclass(frozen=True)
class FeedItem:
    court_key: str
    guid: str
    link: str
    title: str
    description: str


@dataclass(frozen=True)
class CorpusAct:
    slug: str
    jurabk: str
    act_id: str
    patterns: tuple[re.Pattern[str], ...]


@dataclass(frozen=True)
class AliasHit:
    start: int
    end: int
    act: CorpusAct


def clean_text(value: str | None) -> str:
    """Collapse XML formatting whitespace without changing its words."""
    return " ".join((value or "").replace("\u00a0", " ").split())


def element_text(root: ET.Element, name: str) -> str:
    el = root.find(name)
    return clean_text("".join(el.itertext())) if el is not None else ""


def parse_xml(data: bytes, *, label: str) -> ET.Element:
    """Parse XML while explicitly disabling the document's external DTD.

    ElementTree does not fetch external DTDs itself, but RII records declare
    one.  Removing that declaration makes the no-network property explicit.
    Entity declarations are rejected rather than expanded.
    """
    if ENTITY_RE.search(data):
        raise ValueError(f"{label}: entity declarations are not allowed")
    data = DOCTYPE_RE.sub(b"", data, count=1)
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError(f"{label}: invalid XML: {exc}") from exc


def latest_file(source: str, filename: str) -> Path | None:
    """Newest successful snapshot file, ignoring incomplete directories."""
    base = SNAPSHOTS / source
    if not base.is_dir():
        return None
    for folder in sorted((p for p in base.iterdir() if p.is_dir()), reverse=True):
        candidate = folder / filename
        if candidate.is_file() and candidate.stat().st_size:
            return candidate
    return None


def alias_pattern(alias: str) -> re.Pattern[str]:
    escaped = re.escape(clean_text(alias))
    # Official metadata varies between "SGB III" and "SGBIII", and may put
    # whitespace around the slash in FreizügG/EU.
    escaped = escaped.replace(r"\ ", r"\s*").replace("/", r"\s*/\s*")
    return re.compile(rf"(?<![\w]){escaped}(?![\w])", re.IGNORECASE)


def aliases_for_act(row: dict) -> set[str]:
    slug = clean_text(str(row.get("slug") or "")).lower()
    jurabk = clean_text(str(row.get("jurabk") or ""))
    aliases = {jurabk, YEAR_SUFFIX_RE.sub("", jurabk)}

    slug_base = YEAR_SUFFIX_RE.sub("", slug)
    aliases.add(slug_base.replace("_", " "))
    aliases.add(slug_base.replace("_", ""))
    aliases.update(KNOWN_ALIASES.get(slug, ()))

    sgb = re.fullmatch(r"sgb_(\d+)(?:_(?:19|20)\d{2})?", slug)
    if sgb:
        book = int(sgb.group(1))
        aliases.update((f"SGB {book}", f"SGB{book}"))
        if book in ROMAN:
            aliases.update((f"SGB {ROMAN[book]}", f"SGB{ROMAN[book]}"))

    return {
        a for a in (clean_text(v) for v in aliases)
        if len(re.sub(r"\W", "", a, flags=re.UNICODE)) >= 2
    }


def load_corpus() -> list[CorpusAct]:
    path = latest_file("gii", "acts.jsonl")
    if path is None:
        raise RuntimeError("no non-empty GII acts.jsonl snapshot found")

    acts: list[CorpusAct] = []
    for row in read_jsonl(path):
        slug = clean_text(str(row.get("slug") or "")).lower()
        jurabk = clean_text(str(row.get("jurabk") or ""))
        if not slug or not jurabk:
            continue
        aliases = sorted(aliases_for_act(row), key=len, reverse=True)
        acts.append(CorpusAct(
            slug=slug,
            jurabk=jurabk,
            # Must exactly mirror tools/build_web_data.py's public act id;
            # source slugs differ in at least one real case (stag / RuStAG).
            act_id="fed_" + re.sub(
                r"[^a-z0-9]+", "_", jurabk.lower()).strip("_"),
            patterns=tuple(alias_pattern(a) for a in aliases),
        ))
    if not acts:
        raise RuntimeError(f"GII corpus is empty: {path}")
    print(f"[gii] {len(acts)} acts from {path.parent.name}")
    return acts


def fetch_bytes(http: Http, url: str, *, label: str, timeout: int) -> bytes:
    response = http.get(url, timeout=timeout)
    if response.status_code != 200:
        raise RuntimeError(f"{label}: HTTP {response.status_code} for {url}")
    if not response.content:
        raise RuntimeError(f"{label}: empty response from {url}")
    return response.content


def canonical_link(guid: str) -> str:
    return (BASE + "/jportal/portal/page/bsjrsprod?showdoccase=1&"
            f"doc.id={guid}#focuspoint")


def parse_feed(data: bytes, court_key: str) -> list[FeedItem]:
    root = parse_xml(data, label=f"{court_key} feed")
    raw_items = root.findall("./channel/item")
    if not raw_items:
        raise ValueError(f"{court_key} feed contains zero items")

    rows: list[FeedItem] = []
    seen: set[str] = set()
    for pos, item in enumerate(raw_items, 1):
        guid = clean_text(item.findtext("guid"))
        if not GUID_RE.fullmatch(guid):
            raise ValueError(f"{court_key} feed item {pos}: invalid/missing GUID")
        if guid in seen:
            continue
        seen.add(guid)
        link = clean_text(item.findtext("link"))
        if not link.startswith(BASE + "/"):
            link = canonical_link(guid)
        rows.append(FeedItem(
            court_key=court_key,
            guid=guid,
            link=link,
            title=clean_text(item.findtext("title")),
            description=clean_text(item.findtext("description")),
        ))
    if not rows:
        raise ValueError(f"{court_key} feed contains zero unique GUIDs")
    return rows


def read_zip_xml(blob: bytes, guid: str) -> bytes:
    if not blob or len(blob) > MAX_ZIP_BYTES:
        raise ValueError(f"{guid}: invalid ZIP size {len(blob)}")
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            members = [i for i in archive.infolist()
                       if not i.is_dir() and i.filename.lower().endswith(".xml")]
            if len(members) != 1:
                raise ValueError(
                    f"{guid}: expected one XML member, found {len(members)}")
            member = members[0]
            if member.file_size > MAX_XML_BYTES:
                raise ValueError(
                    f"{guid}: XML is too large ({member.file_size} bytes)")
            return archive.read(member)
    except (zipfile.BadZipFile, RuntimeError) as exc:
        raise ValueError(f"{guid}: invalid ZIP: {exc}") from exc


def atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def get_decision_xml(http: Http, guid: str) -> tuple[bytes, bool]:
    """Return XML and whether it came from an already-valid cache entry."""
    cache_path = CACHE / f"{guid}.zip"
    cached_xml: bytes | None = None
    if cache_path.is_file():
        try:
            cached_xml = read_zip_xml(cache_path.read_bytes(), guid)
            # ZIP-valid but XML-corrupt is no cache hit.
            parse_xml(cached_xml, label=guid)
            if time.time() - cache_path.stat().st_mtime < CACHE_MAX_AGE:
                return cached_xml, True
        except (OSError, ValueError) as exc:
            cached_xml = None
            print(f"[cache] {guid}: corrupt ({exc}); refetching", file=sys.stderr)

    try:
        blob = fetch_bytes(http, ZIP_TMPL.format(guid=guid),
                           label=guid, timeout=120)
    except (RuntimeError, requests.RequestException):
        if cached_xml is not None:
            print(f"[cache] {guid}: refresh failed; using valid stale ZIP",
                  file=sys.stderr)
            return cached_xml, True
        raise
    xml = read_zip_xml(blob, guid)  # validate before replacing cached bytes
    parse_xml(xml, label=guid)
    atomic_bytes(cache_path, blob)
    return xml, False


def find_alias_hits(excerpt: str, acts: list[CorpusAct]) -> list[AliasHit]:
    raw: list[AliasHit] = []
    for act in acts:
        for pattern in act.patterns:
            raw.extend(AliasHit(m.start(), m.end(), act)
                       for m in pattern.finditer(excerpt))

    # Prefer the longest alias when a base alias overlaps a year-qualified one.
    chosen: list[AliasHit] = []
    for hit in sorted(raw, key=lambda h: (h.start, -(h.end - h.start))):
        overlaps_same_act = any(
            old.act.slug == hit.act.slug
            and hit.start < old.end and old.start < hit.end
            for old in chosen
        )
        if not overlaps_same_act:
            chosen.append(hit)
    return sorted(chosen, key=lambda h: (h.start, h.end, h.act.slug))


def section_matches(text: str) -> list[tuple[int, list[str]]]:
    matches: list[tuple[int, list[str]]] = []
    for match in SECTION_RE.finditer(text):
        paras = []
        for token in SECTION_TOKEN_RE.finditer(match.group("nums")):
            number = token.group("number")
            tail = token.group("tail") or ""
            # f./ff. and compact ff mean "and following", not a letter
            # suffix. The schema links exact norms, so retain the cited base
            # norm. A bare f remains the real norm § 10f.
            paras.append(number if FOLLOWING_RE.fullmatch(tail)
                         else number + tail.lower())
        matches.append((match.start(), paras))
    return matches


def unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    return [v for v in values if not (v in seen or seen.add(v))]


def norm_segments(excerpt: str, acts: list[CorpusAct]) -> list[str]:
    """Split a RII <norm> list without breaking "§§ 3, 4 SGB 2".

    A comma starts a new citation only when the next token is another section/
    article marker or a known corpus-act alias. Semicolons always split. This
    prevents a bare trailing corpus name (for example `..., § 63 BVerfGG,
    SGB 5`) from inheriting every unrelated section number before it.
    """
    cuts = [0]
    for match in re.finditer(r"[,;]", excerpt):
        if match.group() == ";":
            cuts.append(match.end())
            continue
        suffix = excerpt[match.end():]
        stripped = suffix.lstrip()
        starts_section = bool(re.match(r"(?:§{1,2}|Art\.?)\s*\d",
                                       stripped, re.IGNORECASE))
        starts_act = any(hit.start == 0
                         for hit in find_alias_hits(stripped, acts))
        if starts_section or starts_act:
            cuts.append(match.end())
    cuts.append(len(excerpt))
    return [excerpt[cuts[i]:cuts[i + 1]].strip(" ,;")
            for i in range(len(cuts) - 1)
            if excerpt[cuts[i]:cuts[i + 1]].strip(" ,;")]


def effects_for_norm(excerpt: str, acts: list[CorpusAct]) -> list[dict]:
    """Map one exact <norm> excerpt to corpus effects."""
    by_slug: dict[str, tuple[CorpusAct, list[str]]] = {}
    for segment in norm_segments(excerpt, acts):
        hits = find_alias_hits(segment, acts)
        if not hits:
            continue
        paras = [p for _, group in section_matches(segment) for p in group]
        for hit in hits:
            act, known = by_slug.setdefault(hit.act.slug, (hit.act, []))
            known.extend(paras)

    return [
        {
            "act_id": act.act_id,
            "jurabk": act.jurabk,
            "paras": unique(paras),
            # RII's official <norm> metadata establishes a citation/backlink,
            # not whether the court interpreted or applied the provision.
            "kind": "cited",
            "note": excerpt,
        }
        for act, paras in by_slug.values()
    ]


def iso_date(raw: str, *, guid: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if len(digits) != 8:
        raise ValueError(f"{guid}: invalid decision date {raw!r}")
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"


def strip_outer_parentheses(value: str) -> str:
    value = clean_text(value)
    if len(value) > 2 and value.startswith("(") and value.endswith(")"):
        return value[1:-1].strip()
    return value


def parse_decision(xml: bytes, item: FeedItem,
                   acts: list[CorpusAct]) -> dict | None:
    root = parse_xml(xml, label=item.guid)
    doknr = clean_text(root.findtext("doknr"))
    if not doknr or not re.fullmatch(r"[A-Za-z0-9._-]+", doknr):
        raise ValueError(f"{item.guid}: invalid/missing doknr")

    court_short, court_name = COURTS[item.court_key]
    xml_court = clean_text(root.findtext("gertyp"))
    if xml_court and xml_court.casefold() != court_short.casefold():
        raise ValueError(
            f"{item.guid}: court mismatch ({xml_court!r} != {court_short!r})")

    norm_excerpts = [clean_text("".join(node.itertext()))
                     for node in root.findall("norm")]
    norm_excerpts = [n for n in norm_excerpts if n]
    effects = [effect for excerpt in norm_excerpts
               for effect in effects_for_norm(excerpt, acts)]
    if not effects:
        return None

    title_line = strip_outer_parentheses(element_text(root, "titelzeile"))
    lead = element_text(root, "leitsatz")
    other = element_text(root, "sonstosatz")
    official_summary = clean_text(" ".join(v for v in (lead, other) if v))
    title = title_line or item.description or official_summary or item.title
    summary = official_summary or item.description or title
    if not title:
        title = f"{court_short}, {clean_text(root.findtext('doktyp'))} {doknr}"
    if not summary:
        summary = title

    return {
        "id": f"rii-{doknr.lower()}",
        "court": court_name,
        "court_short": court_short,
        "level": court_short,
        "az": clean_text(root.findtext("aktenzeichen")),
        "date": iso_date(clean_text(root.findtext("entsch-datum")),
                         guid=item.guid),
        "kind": clean_text(root.findtext("doktyp")) or "Entscheidung",
        "proc": "",
        "juris": "DE",
        "title": title,
        "summary": {"de": summary},
        "outcome": None,
        "effects": effects,
        "related": [],
        "quote": None,
        "url": item.link,
        "source": SOURCE,
        "text": None,
    }


def load_previous() -> tuple[Path | None, dict[str, dict]]:
    path = latest_file("rii", "decisions.jsonl")
    if path is None:
        return None, {}
    rows: dict[str, dict] = {}
    for row in read_jsonl(path):
        decision_id = row.get("id")
        if decision_id:
            rows[str(decision_id)] = row
    return path, rows


def atomic_jsonl(path: Path, rows: list[dict]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(fd)
    tmp = Path(raw_tmp)
    try:
        count = write_jsonl(tmp, rows)
        os.replace(tmp, path)
        return count
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def parse_courts(raw: str | None, parser: argparse.ArgumentParser) -> list[str]:
    if raw is None:
        return list(COURTS)
    selected = unique([v.strip().lower() for v in raw.split(",") if v.strip()])
    unknown = sorted(set(selected) - COURTS.keys())
    if unknown:
        parser.error(
            f"unknown courts: {', '.join(unknown)}; choices: "
            f"{', '.join(COURTS)}")
    if not selected:
        parser.error("--courts must select at least one court")
    return selected


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch corpus-relevant federal RII decisions")
    parser.add_argument(
        "--courts",
        help="comma list: bverfg,bgh,bverwg,bfh,bag,bsg,bpatg (default: all)")
    parser.add_argument(
        "--limit", type=positive_int,
        help="testing: process at most N newest RSS items per selected court")
    args = parser.parse_args()
    selected = parse_courts(args.courts, parser)

    try:
        acts = load_corpus()
    except (OSError, ValueError, RuntimeError, requests.RequestException) as exc:
        print(f"[fatal] {exc}", file=sys.stderr)
        return 1

    http = Http(delay=0.5, retries=4)
    feed_items: list[FeedItem] = []
    try:
        # Validate every selected feed before touching snapshot output.
        for court_key in selected:
            url = FEED_TMPL.format(court=court_key)
            data = fetch_bytes(http, url, label=f"{court_key} feed", timeout=60)
            items = parse_feed(data, court_key)
            total = len(items)
            if args.limit:
                items = items[:args.limit]
            print(f"[feed] {COURTS[court_key][0]:>6}: "
                  f"{len(items)}/{total} items")
            feed_items.extend(items)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"[fatal] incomplete feed retrieval: {exc}", file=sys.stderr)
        return 1

    if not feed_items:
        print("[fatal] selected feeds yielded zero items", file=sys.stderr)
        return 1

    fresh: dict[str, dict] = {}
    cached = downloaded = skipped = 0
    try:
        for pos, item in enumerate(feed_items, 1):
            xml, was_cached = get_decision_xml(http, item.guid)
            row = parse_decision(xml, item, acts)
            cached += int(was_cached)
            downloaded += int(not was_cached)
            if row is None:
                skipped += 1
            else:
                fresh[row["id"]] = row
            if pos % 25 == 0 or pos == len(feed_items):
                print(f"[docs] {pos}/{len(feed_items)}; "
                      f"matched {len(fresh)}")
    except (OSError, ValueError, RuntimeError, requests.RequestException,
            zipfile.BadZipFile) as exc:
        print(f"[fatal] incomplete decision retrieval: {exc}", file=sys.stderr)
        print("[safe] snapshot not touched", file=sys.stderr)
        return 1

    previous_path, merged = load_previous()
    if not fresh:
        if previous_path is not None:
            print("[warn] zero new corpus matches; previous snapshot left "
                  f"untouched: {previous_path}", file=sys.stderr)
            return 0
        print("[fatal] zero corpus matches and no previous snapshot; "
              "nothing written", file=sys.stderr)
        return 1

    previous_count = len(merged)
    merged.update(fresh)
    rows = sorted(merged.values(),
                  key=lambda row: (row.get("date") or "", row.get("id") or ""),
                  reverse=True)
    output = SNAPSHOTS / "rii" / date.today().isoformat() / "decisions.jsonl"
    count = atomic_jsonl(output, rows)

    print(f"\n{len(feed_items)} RSS items: {downloaded} downloaded, "
          f"{cached} cached, {skipped} outside corpus")
    print(f"{len(fresh)} matched decisions; cumulative "
          f"{previous_count} -> {count}")
    print(f"  -> {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
