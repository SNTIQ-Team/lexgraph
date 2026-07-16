"""Fetch the current consolidated HEAD of the practice corpus from
gesetze-im-internet.de (GII).

GII is seed_only (verified 2026-07-06): current Fassung only, one <norm>
element per §, builddate + BJNE doknr as cheap change detectors, public
domain (§ 5 UrhG). Daily snapshots of this fetch build forward history.

Output (data/snapshots/gii/<date>/):
    catalog.jsonl one metadata-only row for every official GII TOC item
                  {id, abbrev, title, url}
    acts.jsonl    one row per act  {slug, jurabk, long_title, builddate,
                                    doknr, stand, norm_count}
    norms.jsonl   one row per §    {slug, jurabk, enbez, titel, text,
                                    doknr, gliederung}

Usage:
    python3 pipeline/fetch_gii.py [--slugs asylblg,aufenthg_2004]
"""
from __future__ import annotations

import argparse
import io
import re
import sys
import xml.etree.ElementTree as ET
import zipfile

from common import Http, snapshot_dir, write_jsonl

TOC_URL = "https://www.gesetze-im-internet.de/gii-toc.xml"

# practice corpus (from the retired downloader's mapping) — GII slugs
CORPUS = {
    # constitution & civil/criminal backbone
    "gg", "bgb", "stgb", "stpo", "estg",
    # social law
    "sgb_1", "sgb_2", "sgb_3", "sgb_4", "sgb_5", "sgb_6", "sgb_7",
    "sgb_8", "sgb_9_2018", "sgb_10", "sgb_11", "sgb_12", "sgb_14",
    "sgg", "rbeg_2021", "baf_g", "asylblg",
    "wogg", "bkgg_1996", "uhvorschg",          # housing/child/maintenance
    "beeg",                                    # Elterngeld/Elternzeit
    "bvfg",                                    # Spätaussiedler/Vertriebene
    # work & protection (practical core for migrant workers)
    "agg", "milog", "kschg", "entgfg", "arbzg", "gewschg",
    # migration & asylum
    "asylvfg_1992", "aufenthg_2004", "freiz_gg_eu_2004", "azrg",
    "stag",                                    # citizenship (StAG)
    "ukraineaufenthfgv", "ukraineaufenth_v",
    "aufenthv", "beschv_2013", "intv",         # residence/work/integration regs
    # procedure
    "vwgo", "vwvfg", "ozg",
    "zpo",                                     # PKH lives in §§ 114-127 ZPO
    "berathig",                                # Beratungshilfe (out-of-court aid)
    # misc from practice
    "waffg_2002", "beg", "idnrg",
}

# Broad practice expansion, verified against the official GII TOC/XML on
# 2026-07-16.  Keep this named bundle separate so a future edit cannot
# silently shrink the deep full-text corpus back to the original seed set.
PRACTICE_EXPANSION_2026_07 = {
    # migration, asylum, integration, identity & civil status
    "asylzbv_2026", "azrg-dv", "aknv", "einbtestv", "bqfg", "deuf_v",
    "dbv", "bmg", "pstg", "pstv", "pa_g_1986", "pauswg", "vwdg",
    "vwdg-dv", "viszg", "hauslg",
    # benefits, social administration, housing & accessibility
    "algiiv_2008", "grsidav", "sozhidav_2019", "svev", "de_v",
    "beitrvv", "wogv", "wofg", "wobindg", "woflv", "wovermrg", "wbvg",
    "woeigg", "bgg", "bitv_2_0", "pflegezg", "fpfzg", "kkg",
    "beratungsg",
    # family and personal status
    "famfg", "famgkg", "versausglg", "btog", "aug_2011", "adwirkg",
    "nam_ndg", "sbgg", "muschg_2018",
    # employment and workplace protection
    "arbgg", "burlg", "tzbfg", "a_g", "betrvg", "tvg", "bbig_2005",
    "arbschg", "jarbschg", "aentg_2009", "schwarzarbg_2004", "nachwg",
    "hinschg", "entgtranspg", "betravg", "asig",
    # courts, enforcement, information rights and general procedure
    "bverfgg", "gvg", "gkg_2004", "rvg", "jveg", "owig_1968", "vwvg",
    "vwzg_2005", "ifg", "bdsg_2018", "fgo", "inso", "ao_1977", "bgbeg",
}

CORPUS |= PRACTICE_EXPANSION_2026_07


def strip_tags(el: ET.Element) -> str:
    """Flatten a textdaten subtree to plain text, keeping structure hints."""
    txt = ET.tostring(el, encoding="unicode", method="text")
    return re.sub(r"[ \t]+", " ", txt).strip()


def parse_law(xml_bytes: bytes, slug: str) -> tuple[dict, list[dict]]:
    root = ET.fromstring(xml_bytes)
    builddate = root.get("builddate", "")
    doknr = root.get("doknr", "")
    act = {"slug": slug, "builddate": builddate, "doknr": doknr,
           "jurabk": "", "long_title": "", "stand": "", "norm_count": 0}
    norms = []
    gliederung = ""
    for norm in root.findall("norm"):
        meta = norm.find("metadaten")
        if meta is None:
            continue
        jurabk = (meta.findtext("jurabk") or "").strip()
        enbez = (meta.findtext("enbez") or "").strip()
        titel = (meta.findtext("titel") or "").strip()
        glied = meta.find("gliederungseinheit")
        if glied is not None:
            gliederung = " ".join(filter(None, (
                (glied.findtext("gliederungsbez") or "").strip(),
                (glied.findtext("gliederungstitel") or "").strip())))
        if not act["jurabk"] and jurabk:
            act["jurabk"] = jurabk
            act["long_title"] = (meta.findtext("langue") or "").strip()
            for st in meta.findall(".//standangabe"):
                k = (st.findtext("standtyp") or "").strip()
                v = (st.findtext("standkommentar") or "").strip()
                if k and v:
                    act["stand"] = (act["stand"] + " | " if act["stand"]
                                    else "") + f"{k}: {v}"
        if not enbez:                       # act header / TOC pseudo-norms
            continue
        body = norm.find("textdaten/text/Content")
        text = strip_tags(body) if body is not None else ""
        norms.append({"slug": slug, "jurabk": jurabk or act["jurabk"],
                      "enbez": enbez, "titel": titel, "text": text,
                      "doknr": norm.get("doknr", ""),
                      "gliederung": gliederung})
    act["norm_count"] = len(norms)
    return act, norms


def parse_toc(xml_bytes: bytes) -> tuple[dict[str, str], list[dict]]:
    """Parse GII's official master TOC without fetching any act text.

    The TOC exposes only a title and the stable ``/<slug>/xml.zip`` path.
    Consequently ``abbrev`` is deliberately the official GII path token, not
    a guessed printed ``JurAbk``.  Curated rows are enriched with their real
    ``jurabk`` later by ``build_web_data.py`` after their XML has been parsed.
    """
    toc = ET.fromstring(xml_bytes)
    links: dict[str, str] = {}
    catalog: list[dict] = []
    for item in toc.findall("item"):
        title = " ".join((item.findtext("title") or "").split())
        link = (item.findtext("link") or "").strip().replace(
            "http://", "https://", 1)
        match = re.search(r"/([^/?]+)/xml\.zip(?:$|\?)", link)
        if not match or not title:
            continue
        slug = match.group(1).lower()
        if slug in links:  # stable path is the identity; keep source-first
            continue
        xml_url = link.split("?", 1)[0]
        links[slug] = xml_url
        catalog.append({
            "id": f"gii:{slug}",
            "abbrev": slug,
            "title": title,
            "url": xml_url.removesuffix("xml.zip"),
        })
    return links, catalog


def require_complete_corpus(wanted: set[str], links: dict[str, str]) -> None:
    """Refuse a default refresh if GII no longer exposes a configured act.

    A transiently incomplete TOC or a renamed slug must not silently replace a
    complete production generation with fewer laws.  Explicit ``--slugs``
    probes remain intentionally partial and are handled separately in
    :func:`main`.
    """
    missing = sorted(wanted - links.keys())
    if missing:
        raise RuntimeError(
            "configured corpus missing from the official GII index: "
            + ", ".join(missing))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slugs", help="comma list; default: practice corpus")
    args = ap.parse_args()
    wanted = set(args.slugs.split(",")) if args.slugs else set(CORPUS)

    http = Http(delay=0.4)
    print("[toc] fetching index …")
    links, catalog = parse_toc(http.get(TOC_URL, timeout=60).content)
    print(f"[toc] {len(links)} laws in the federal index")

    missing = sorted(wanted - links.keys())
    if args.slugs:
        if missing:
            print(f"[warn] not in GII index: {missing}")
    else:
        try:
            require_complete_corpus(wanted, links)
        except RuntimeError as exc:
            print(f"[error] {exc}", file=sys.stderr)
            return 1

    acts, norms = [], []
    for slug in sorted(wanted & links.keys()):
        r = http.get(links[slug], timeout=90)
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            xml_name = next(n for n in z.namelist() if n.endswith(".xml"))
            act, ns = parse_law(z.read(xml_name), slug)
        acts.append(act)
        norms.extend(ns)
        print(f"  {act['jurabk'] or slug:>16}  {act['norm_count']:4} §§  "
              f"build {act['builddate'][:8]}")

    out = snapshot_dir("gii")
    write_jsonl(out / "catalog.jsonl", catalog)
    write_jsonl(out / "acts.jsonl", acts)
    write_jsonl(out / "norms.jsonl", norms)
    print(f"\n{len(acts)} acts, {len(norms)} norms -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
