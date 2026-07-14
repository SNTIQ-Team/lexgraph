"""Fetch consolidated Bavarian law HEAD + amendment back-history from
gesetze-bayern.de (BAYERN.RECHT, Bayerische Staatskanzlei).

VERIFIED endpoints (2026-07-06, robots fully permissive "Allow: /"):
  /Content/Document/ffn   one ~1.1 MB HTML page: the complete BayRS
      systematic register. Act rows are <td>BayRS-Nr</td><td><a
      href="KEY">title</a></td>; the rows after each act carry the
      Fortfuehrungsnachweis amendment chain, numbered like
      "1) Art. 1 neu gefasst ... (§ 2 G v. 10.09.2007, S. 634)".
  /Content/Zip/<KEY>      application/zip with bayportalnorm/<KEY>.xml
      (DOCTYPE byrecht-norm, C.H.Beck DTD): builddate attribute,
      inkraft/ausserkraft, einzelnorm per Art./§ (para.nr, para.titel,
      jurAbsatz), aenderungsverlauf, typed verweis.* cross-references.

Doc keys are NOT the official abbreviations (AufnG -> "BayAsylAufnG";
/Content/Document/AufnG is 404). Keys are resolved from the ffn
register by matching the "( ... - AufnG)" abbreviation in the title.
EU references in the corpus XML are encoded as verweis.norm with
ersatz dokids "EU_RL_*", "EWG_VO_*", "EWG_DSGVO", "EUGRCharta*"; the
DTD's verweis.eurl/verweis.euvo elements are also handled when present.
They occur in norm bodies (enbez = "Art. N"/"§ N") and in fn.def
implementation footnotes on aenderungsverlauf entries (enbez =
"aenderungsverlauf:<seq>", joinable against versions.jsonl).

Output (data/snapshots/bayern_recht/<date>/):
    acts.jsonl     {key, jurabk, long_title, bayrs_nr, builddate,
                    inkraft, norm_count}
    norms.jsonl    {key, jurabk, enbez, titel, text}
    versions.jsonl {jurabk, seq, date, gvbl_citation, description,
                    source: "ffn" | "xml"}
    eu_refs.jsonl  {jurabk, enbez, ref_type, target}

Usage:
    python3 pipeline/fetch_bayern_recht.py [--abbrs AufnG,DVAsyl]
"""
from __future__ import annotations

import argparse
import copy
import io
import re
import sys
import xml.etree.ElementTree as ET
import zipfile

from bs4 import BeautifulSoup

from common import Http, snapshot_dir, write_jsonl

BASE = "https://www.gesetze-bayern.de"
FFN_URL = BASE + "/Content/Document/ffn"
ZIP_URL = BASE + "/Content/Zip/{key}"

# practice corpus (asylum/social): official abbr -> candidates as they
# appear in ffn register titles, tried in order
CORPUS = {
    # Land constitutional layer (kept in the same official BayRS feed so the
    # hierarchy never has to present a constitution-shaped placeholder).
    "BayVerf": ("BayVerf",
                 "Verfassung des Freistaates Bayern in der Fassung"),
    "AufnG": ("AufnG",),
    "DVAsyl": ("DVAsyl",),
    "ZustVAuslR": ("ZustVAuslR",),
    "BayIntG": ("BayIntG",),
    "AGSG": ("AGSG",),
    "BayVwVfG": ("BayVwVfG",),
    "AGVwGO": ("AGVwGO",),
    "VwZVG": ("VwZVG",),
    "LStVG": ("LStVG",),
    "BayPAG": ("BayPAG", "PAG"),
    "BayEUG": ("BayEUG", "EUG"),
}

DATE_RE = re.compile(r"v(?:om)?\.?\s+(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})")
EU_DOKID_RE = re.compile(r"^(?:EU_|EWG_|EG_|EUGR)")


def flat(el: ET.Element) -> str:
    """Flatten an XML subtree to whitespace-normalized plain text."""
    txt = ET.tostring(el, encoding="unicode", method="text")
    return " ".join(txt.split())


def iso_date(text: str, first: bool = False) -> str:
    """D.M.YYYY date in text (after 'v.'/'vom') as ISO, or ''.
    ffn citation strings want the LAST date (default); XML
    aenderungsverlauf bodies want the FIRST — nested parentheticals like
    '(GVBl. S. 282, geänd. durch G v. 23.6.2016 …)' cite amendments OF
    the amending act, and taking the last date shifted 5 rows by up to
    12 years (confirmed against the independent ffn chain)."""
    hits = list(DATE_RE.finditer(text))
    if not hits:
        return ""
    d, m, y = hits[0].groups() if first else hits[-1].groups()
    return f"{y}-{int(m):02d}-{int(d):02d}"


# --------------------------------------------------------------- ffn


def parse_ffn(html: str) -> list[dict]:
    """Register in document order: doc_key, bayrs_nr, title, chain."""
    soup = BeautifulSoup(html, "html.parser")
    register: list[dict] = []
    current: dict | None = None
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) != 2:
            continue
        a = tds[1].find("a")
        href = (a.get("href") or "") if a else ""
        if a and href and "/" not in href:
            current = {"doc_key": href,
                       "bayrs_nr": tds[0].get_text(strip=True),
                       "title": " ".join(a.get_text().split()),
                       "chain": []}
            register.append(current)
            continue
        if current is None or tds[0].get_text(strip=True):
            continue                        # section header / stray row
        txt = " ".join(tds[1].get_text().split())
        m = re.match(r"^(\d+)\)\s*(.*)$", txt)
        if m:
            current["chain"].append(
                parse_chain_entry(int(m.group(1)), m.group(2)))
    return register


def parse_chain_entry(seq: int, rest: str) -> dict:
    """'Art. 1 neu gefasst (§ 2 G v. 10.09.2007, S. 634)' -> fields."""
    cit, desc = "", rest.strip()
    m = re.search(r"\(([^()]*)\)\s*$", desc)
    if m:
        cit = m.group(1).strip()
        desc = desc[:m.start()].strip()
    return {"seq": seq, "description": desc, "gvbl_citation": cit,
            "date": iso_date(cit)}


def resolve_key(register: list[dict], candidates: tuple[str, ...],
                ) -> dict | None:
    """Find the register entry whose title carries the abbreviation."""
    for cand in candidates:
        esc = re.escape(cand)
        # tier 1: abbr right before ')' — "(Aufnahmegesetz - AufnG)"
        # tier 2: abbr anywhere on a word boundary
        for pat in (rf"(?<![A-Za-z0-9]){esc}\)",
                    rf"(?<![A-Za-z0-9]){esc}(?![A-Za-z0-9])"):
            hits = [r for r in register if re.search(pat, r["title"])]
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                print(f"[warn] {cand}: {len(hits)} ambiguous ffn hits: "
                      f"{[h['doc_key'] for h in hits]}")
    return None


# --------------------------------------------------------------- xml


def eu_ref_type(dokid: str) -> str:
    if "_RL_" in dokid:
        return "eurl"
    if "_VO_" in dokid or "DSGVO" in dokid:
        return "euvo"
    return "eu"


def eu_refs_of(el: ET.Element) -> list[tuple[str, str]]:
    """(ref_type, target) pairs for EU references under el."""
    refs: list[tuple[str, str]] = []
    for node in el.iter():
        if node.tag == "verweis.eurl":
            nr = (node.get("v.rl-eu-nr")
                  or f"{node.get('v.rl-jahr', '')}/{node.get('v.rl-nr', '')}"
                  .strip("/")) or flat(node)
            refs.append(("eurl", nr))
        elif node.tag == "verweis.euvo":
            nr = (node.get("v.vo-eu-nr")
                  or f"{node.get('v.vo-jahr', '')}/{node.get('v.vo-nr', '')}"
                  .strip("/")) or flat(node)
            refs.append(("euvo", nr))
        elif node.tag == "verweis.norm":
            for sub in node.iter():
                dokid = sub.get("ersatz") or ""
                if EU_DOKID_RE.match(dokid):
                    refs.append((eu_ref_type(dokid), dokid))
    return refs


def parse_act_xml(xml_bytes: bytes, key: str, abbr: str,
                  ) -> tuple[dict, list[dict], list[dict], list[dict]]:
    """-> (act, norms, xml_versions, eu_refs) for one bayportalnorm doc."""
    root = ET.fromstring(xml_bytes)
    jurabk = (root.findtext("kopf/angaben.versabh/amtlicheAbk")
              or "").strip() or abbr
    bayrs = (root.findtext(".//gliederungsNr.BayRS") or "").strip()
    bayrs = re.sub(r"^BayRS\s*", "", bayrs)
    act = {"key": key, "jurabk": jurabk, "long_title": "",
           "bayrs_nr": bayrs, "builddate": root.get("builddate", ""),
           "inkraft": (root.findtext(".//inkraft") or "").strip(),
           "norm_count": 0}

    norms, refs = [], []
    seen_refs: set[tuple[str, str]] = set()
    for en in root.iter("einzelnorm"):
        enbez = " ".join((en.findtext("para.nr") or "").split())
        tit = en.find("para.titel")
        parts = [flat(c) for c in en
                 if c.tag not in ("para.nr", "para.titel")]
        norms.append({"key": key, "jurabk": jurabk, "enbez": enbez,
                      "titel": flat(tit) if tit is not None else "",
                      "text": "\n".join(p for p in parts if p)})
        for ref_type, target in eu_refs_of(en):
            if (enbez, target) not in seen_refs:
                seen_refs.add((enbez, target))
                refs.append({"jurabk": jurabk, "enbez": enbez,
                             "ref_type": ref_type, "target": target})
    act["norm_count"] = len(norms)

    versions = []
    av = root.find(".//aenderungsverlauf")
    for li in (av.findall(".//li") if av is not None else []):
        sym = (li.findtext("symbol") or "").strip()
        seq = int(sym.rstrip(".")) if sym.rstrip(".").isdigit() else 0
        # implementation footnotes carry the EU refs of this amendment
        enbez = f"aenderungsverlauf:{seq}"
        for ref_type, target in eu_refs_of(li):
            if (enbez, target) not in seen_refs:
                seen_refs.add((enbez, target))
                refs.append({"jurabk": jurabk, "enbez": enbez,
                             "ref_type": ref_type, "target": target})
        clean = copy.deepcopy(li)           # drop footnote text from body
        for node in clean.iter():
            for child in list(node):
                if child.tag != "fn.call":
                    continue
                tail = child.tail or ""     # keep tail (holds "vom <date>")
                kids = list(node)
                i = kids.index(child)
                if i > 0:
                    kids[i - 1].tail = (kids[i - 1].tail or "") + tail
                else:
                    node.text = (node.text or "") + tail
                node.remove(child)
        body = flat(clean)
        if sym and body.startswith(sym):
            body = body[len(sym):].strip()
        m = re.search(r"\((GVBl\.[^(),]*)", body)
        cit = m.group(1).strip() if m else ""
        date = iso_date(body, first=True)
        desc = body.split("(GVBl", 1)[0].strip()
        versions.append({"jurabk": jurabk, "seq": seq, "date": date,
                         "gvbl_citation": cit, "description": desc,
                         "source": "xml"})
    return act, norms, versions, refs


# -------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--abbrs", help="comma list; default: practice corpus")
    args = ap.parse_args()
    wanted = (args.abbrs.split(",") if args.abbrs else sorted(CORPUS))
    bad = [a for a in wanted if a not in CORPUS]
    if bad:
        print(f"[err] unknown abbrs (not in corpus map): {bad}")
        return 1

    http = Http(delay=0.8)
    http.s.headers["User-Agent"] = (
        "SNTIQ-lexgraph/0.1 (research; deless500@gmail.com)")

    print("[ffn] fetching BayRS register …")
    r = http.get(FFN_URL, timeout=120)
    register = parse_ffn(r.text) if r.status_code == 200 else []
    if not register:
        print(f"[err] ffn register empty/unfetchable (HTTP "
              f"{r.status_code}) — refusing to overwrite snapshot",
              file=sys.stderr)
        return 1
    n_chain = sum(len(r["chain"]) for r in register)
    print(f"[ffn] {len(register)} acts, {n_chain} amendment entries")

    resolved, unresolved = {}, []
    for abbr in wanted:
        entry = resolve_key(register, CORPUS[abbr])
        if entry is None:
            unresolved.append(abbr)
            print(f"[warn] unresolved in ffn register: {abbr}")
        else:
            resolved[abbr] = entry
            print(f"  {abbr:>10} -> {entry['doc_key']:<14} "
                  f"BayRS {entry['bayrs_nr']:<12} "
                  f"({len(entry['chain'])} ffn amendments)")

    acts, norms, versions, eu_refs = [], [], [], []
    fetched, failed = [], []
    for abbr, entry in resolved.items():
        key = entry["doc_key"]
        try:
            r = http.get(ZIP_URL.format(key=key), timeout=90)
            r.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                name = next(n for n in z.namelist() if n.endswith(".xml"))
                act, ns, xv, refs = parse_act_xml(z.read(name), key, abbr)
        except Exception as exc:            # noqa: BLE001 — count & go on
            failed.append(abbr)
            print(f"[err] {abbr} ({key}): {exc}")
            continue
        act["long_title"] = re.sub(
            r"\s*(?:in der Fassung der Bekanntmachung\s+)?"
            r"vom\s+\d{1,2}\.\s.*$", "", entry["title"])
        act["bayrs_nr"] = act["bayrs_nr"] or entry["bayrs_nr"]
        for c in entry["chain"]:
            versions.append({"jurabk": act["jurabk"], **c, "source": "ffn"})
        acts.append(act)
        norms.extend(ns)
        versions.extend(xv)
        eu_refs.extend(refs)
        fetched.append(abbr)
        print(f"  {act['jurabk']:>10}  {act['norm_count']:4} norms  "
              f"{len(entry['chain']):2} ffn / {len(xv):2} xml versions  "
              f"{len(refs):2} EU refs  build {act['builddate']}")

    if not acts or unresolved or failed:
        print("[err] incomplete Bavarian corpus — refusing to overwrite "
              f"snapshot (unresolved={unresolved or '-'}, "
              f"failed={failed or '-'})", file=sys.stderr)
        return 1
    out = snapshot_dir("bayern_recht")
    n_a = write_jsonl(out / "acts.jsonl", acts)
    n_n = write_jsonl(out / "norms.jsonl", norms)
    n_v = write_jsonl(out / "versions.jsonl", versions)
    n_e = write_jsonl(out / "eu_refs.jsonl", eu_refs)
    print(f"\nfetched {len(fetched)}/{len(wanted)} acts"
          f" | unresolved {unresolved or '-'} | failed {failed or '-'}")
    print(f"{n_a} acts, {n_n} norms, {n_v} versions, {n_e} EU refs -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
