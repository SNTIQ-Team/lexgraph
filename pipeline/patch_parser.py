"""Parse German amendment commands (Änderungsbefehle) into
PatchInstruction dicts (docs/VISION.md).

German amendment law is a patch language with a small, stable grammar:

    Artikel 1
    Änderung des Aufenthaltsgesetzes
    Das Aufenthaltsgesetz ... wird wie folgt geändert:
    1. In der Inhaltsübersicht wird ... die folgende Angabe eingefügt: „…"
    2. Nach § 104c wird der folgende § 104d eingefügt: „…"
    Artikel 2
    Inkrafttreten
    Dieses Gesetz tritt am Tag nach der Verkündung in Kraft.

This module is pure text -> data; callers attach lifecycle fields
(status, procedure, dates). Unrecognized commands are still emitted with
operation="other" and the raw text — silence is worse than coarseness.
"""
from __future__ import annotations

import hashlib
import re

# ---------------------------------------------------------------- targets

ORDINAL_BOOK = {
    "erste": 1, "zweite": 2, "dritte": 3, "vierte": 4, "fünfte": 5,
    "sechste": 6, "siebte": 7, "siebente": 7, "achte": 8, "neunte": 9,
    "zehnte": 10, "elfte": 11, "zwölfte": 12, "dreizehnte": 13,
    "vierzehnte": 14,
}

# leading whitespace allowed: pdftotext -layout centers BR-Drucksachen
# headings ("            Artikel 1"); inline refs stay excluded by the $
ARTIKEL_RE = re.compile(r"^\s*Artikel\s+(\d+[a-z]?)\s*$", re.M)
QUOTE_RE = re.compile(r"[„‚]([^“”‘’]*)[“”‘’]", re.S)


def _clean(s: str) -> str:
    return " ".join(s.split())


# curated needles: official short names as word-boundary regexes.
# resolve_act once matched "des Düngegesetzes" to AufenthG because the
# generic stem of "Gesetz über den Aufenthalt…" degraded to the bare
# word "gesetz" — never derive needles that loose again.
NEEDLES = {
    "GG": r"grundgesetz",
    "BGB": r"bürgerlichen?\s+gesetzbuch",
    "StGB": r"strafgesetzbuch",
    "StPO": r"strafprozess?ordnung",
    "EStG": r"einkommensteuergesetz",
    "SGG": r"sozialgerichtsgesetz",
    "BAföG": r"bundesausbildungsförderungsgesetz|bafög",
    "AsylbLG": r"asylbewerberleistungsgesetz",
    "AsylVfG 1992": r"asylgesetz|asylverfahrensgesetz",
    "AufenthG 2004": r"aufenthaltsgesetz",
    "FreizügG/EU 2004": r"freizügigkeitsgesetz",
    "AZRG": r"azr-gesetz|ausländerzentralregistergesetz",
    "RuStAG": r"staatsangehörigkeitsgesetz",
    "VwGO": r"verwaltungsgerichtsordnung",
    "VwVfG": r"verwaltungsverfahrensgesetz",
    "OZG": r"onlinezugangsgesetz",
    "WaffG 2002": r"waffengesetz",
    "BEG": r"bundesentschädigungsgesetz",
    "IDNrG": r"identifikationsnummerngesetz",
    "RBEG 2021": r"regelbedarfs-?ermittlungsgesetz",
    "UkraineAufenthFGV": r"ukraine-?aufenthalts?-?fortgeltung",
    "UkraineAufenthÜV": r"ukraine-?aufenthalts?-?überführung",
}


def _needle_for(a: dict) -> str | None:
    if a["jurabk"] in NEEDLES:
        return NEEDLES[a["jurabk"]]
    # safe generic: single compound word ending in -gesetz/-ordnung/-buch
    first = ((a.get("long_title") or "").split() or [""])[0].lower()
    if len(first) > 10 and re.search(r"(gesetz|ordnung|buch)$", first):
        return re.escape(first)
    return None


def resolve_act(heading: str, acts: list[dict]) -> str | None:
    """Match an Artikel heading like 'Änderung des Zwölften Buches
    Sozialgesetzbuch' or 'Änderung des Aufenthaltsgesetzes' to a GII
    jurabk from the corpus list [{jurabk, long_title}, ...].
    Word-boundary matching only; None when nothing safe matches."""
    h = _clean(heading).lower()
    m = re.search(r"(\w+)n?\s+buch(?:es)?\s+(?:des\s+)?sozialgesetzbuch",
                  h)
    if m:
        n = ORDINAL_BOOK.get(m.group(1).rstrip("sn"))
        if n:
            for a in acts:
                if re.fullmatch(rf"SGB {n}( \d{{4}})?", a["jurabk"]):
                    return a["jurabk"]
    best = None
    for a in acts:
        needle = _needle_for(a)
        if not needle:
            continue
        m = re.search(rf"\b(?:{needle})(?:es|e|s|n)?\b", h)
        if m and (best is None or m.end() - m.start() > best[0]):
            best = (m.end() - m.start(), a["jurabk"])
    if best:
        return best[1]
    for a in acts:                              # plain jurabk mention
        jb = a["jurabk"].lower()
        if len(jb) >= 4 and re.search(rf"\b{re.escape(jb)}\b", h):
            return a["jurabk"]
    return None


def _ref(text: str) -> dict:
    """Pull the normative address out of one command."""
    t = text[:300]
    ref = {}
    m = re.search(r"§§?\s*(\d+\s*[a-z]?)\b", t)
    if m:
        ref["para"] = m.group(1).replace(" ", "")
    m = re.search(r"Absatz\s+(\d+[a-z]?)", t)
    if m:
        ref["absatz"] = m.group(1)
    m = re.search(r"Satz\s+(\d+)", t)
    if m:
        ref["satz"] = m.group(1)
    m = re.search(r"Nummer\s+(\d+[a-z]?)", t)
    if m:
        ref["nummer"] = m.group(1)
    m = re.search(r"Buchstabe\s+([a-z])\b", t)
    if m:
        ref["buchstabe"] = m.group(1)
    if re.search(r"Inhaltsübersicht|Inhaltsverzeichnis", t):
        ref["toc"] = True
    return ref


# operation tests, first match wins — 'wie folgt geändert' (a container
# of sub-commands) must run before the leaf verbs it contains
_OPS = [
    ("modify",   r"(?:wird|werden) wie folgt geändert"),
    ("replace",  r"durch (?:die Wörter|das Wort|die Angabe|die Wortfolge|"
                 r"die Bezeichnung|folgende|die folgenden?) .{0,400}?ersetzt"),
    ("replace",  r"(?:wird|werden) (?:.{0,80}? )?wie folgt gefasst|"
                 r"erhält folgende Fassung|erhalten folgende Fassung"),
    ("repeal",   r"(?:wird|werden|ist|sind) aufgehoben"),
    ("delete",   r"(?:wird|werden) gestrichen"),
    ("insert",   r"(?:wird|werden) .{0,240}?(?:eingefügt|angefügt|"
                 r"vorangestellt)"),
    ("renumber", r"(?:wird|werden) .{0,80}?(?:umnummeriert|"
                 r"(?:der|die|das) (?:neue[rn]?\s+)?§§?\s*\d+[a-z]?)"),
]


def classify(cmd: str) -> str:
    c = _clean(cmd)
    for op, pat in _OPS:
        if re.search(pat, c, re.S):
            return op
    return "other"


def parse_command(cmd: str) -> dict:
    """One numbered Änderungsbefehl -> partial PatchInstruction."""
    c = _clean(cmd)
    op = classify(c)
    quotes = [q.strip() for q in QUOTE_RE.findall(cmd)]
    old, new = None, None
    m = re.search(
        r"(?:die Wörter|das Wort|die Angabe|die Wortfolge|die Bezeichnung)"
        r"\s*[„‚](.+?)[“”’]\s*(?:wird|werden)?\s*durch\s*"
        r"(?:die Wörter|das Wort|die Angabe|die Wortfolge|die Bezeichnung)?"
        r"\s*[„‚](.+?)[“”’]", cmd, re.S)
    if m:
        old, new = _clean(m.group(1)), _clean(m.group(2))
    elif op in ("replace", "insert") and quotes:
        new = "\n".join(quotes)
    raw = cmd.strip()
    return {
        "operation": op,
        "ref": _ref(c),
        "old_text_constraint": old,
        "new_text": new,
        # The command is primary evidence for the target/operation parser.
        # Never truncate it: large recasts routinely exceed the former
        # 800-character UI-era cap.  Preserve line structure and attach a
        # stable digest so downstream exports can prove byte identity.
        "raw": raw,
        "raw_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
    }


def _split_items(body: str) -> list[str]:
    """Split an Artikel body on top-level '1. ' item numbers at line
    starts; a body without numbering is a single command. Two guards
    against phantom items: the number must continue the 1,2,3… sequence
    (a quoted "31. Juli 2025" at a line break is a date, not item 31),
    and it must not sit inside a „…" quotation."""
    spans = [m.span() for m in QUOTE_RE.finditer(body)]
    cuts, expect = [], 1
    for m in re.finditer(r"(?m)^\s{0,3}(\d{1,2})\.\s+", body):
        if int(m.group(1)) != expect:
            continue
        if any(a < m.start() < b for a, b in spans):
            continue
        cuts.append(m)
        expect += 1
    if not cuts:
        return [body] if _clean(body) else []
    out = []
    for i, m in enumerate(cuts):
        end = cuts[i + 1].start() if i + 1 < len(cuts) else len(body)
        out.append(body[m.end():end])
    return out


def parse_inkrafttreten(body: str) -> dict:
    b = _clean(body)
    m = re.search(r"tritt\s+am\s+(\d{1,2})\.\s*"
                  r"(Januar|Februar|März|April|Mai|Juni|Juli|August|"
                  r"September|Oktober|November|Dezember)\s+(\d{4})", b)
    if m:
        mon = ("Januar Februar März April Mai Juni Juli August September "
               "Oktober November Dezember").split().index(m.group(2)) + 1
        return {"valid_from": f"{m.group(3)}-{mon:02d}-{int(m.group(1)):02d}",
                "mode": "fixed_date"}
    if re.search(r"tritt\s+am\s+Tag\s+nach\s+der\s+Verkündung", b):
        return {"valid_from": None, "mode": "day_after_promulgation"}
    if re.search(r"tritt\s+.{0,40}?in\s+Kraft", b):
        return {"valid_from": None, "mode": "other", "raw": b[:200]}
    return {}


def parse_bill(text: str, acts: list[dict]) -> dict:
    """Full Drucksache/Gesetz text -> {patches: [...], inkrafttreten: {}}.
    Only the normative part is parsed: everything from the first
    'Artikel 1' heading to 'Begründung' (the explanatory memorandum
    repeats commands in prose and must not be mistaken for law)."""
    cut = re.search(r"(?m)^\s*Begründung\s*$", text)
    norm = text[:cut.start()] if cut else text
    heads = list(ARTIKEL_RE.finditer(norm))
    patches, ikt = [], {}
    for i, h in enumerate(heads):
        body = norm[h.end():heads[i + 1].start() if i + 1 < len(heads)
                    else len(norm)]
        first_lines = _clean(body[:250])
        if re.search(r"Inkrafttreten|tritt .{0,60}?in Kraft", first_lines):
            ikt = parse_inkrafttreten(body) or ikt
            continue
        jurabk = resolve_act(first_lines, acts)
        for n, item in enumerate(_split_items(body), 1):
            p = parse_command(item)
            p["artikel"] = h.group(1)
            p["item"] = n
            p["target_act"] = jurabk
            patches.append(p)
    return {"patches": patches, "inkrafttreten": ikt}
