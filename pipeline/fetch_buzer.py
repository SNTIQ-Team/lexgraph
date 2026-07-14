"""Fetch per-act amendment chronology (2006+) from buzer.de.

buzer is the verified back-history source: every version of nearly all
Bundesrecht since 2006, each mapped to its amending act and BGBl citation
(robots.txt and Impressum permit this; /s2.htm full-text search is
disallowed and NOT used). Tier: non-authoritative convenience — every
record is marked source=buzer and must never outrank official sources.

Fetched per corpus act (log level only — one request per act):
  /<Abk>.htm            act index -> internal act id
  /gesetz/<id>/l.htm    the per-act "git log": every version with
                        in-force date, amending act, synopsis links
Plus once:
  /v.htm                promulgated-but-not-yet-in-force changes
                        (next ~100 days) -> anticipation events

Output (data/snapshots/buzer/<date>/):
    versions.jsonl   {jurabk, act_id, date, title, synopsis_url}
    upcoming.jsonl   {date, title, url}
"""
from __future__ import annotations

import re
import sys

from bs4 import BeautifulSoup

from common import Http, latest_snapshot, read_jsonl, snapshot_dir, write_jsonl

BASE = "https://www.buzer.de"


def soup_get(http: Http, path: str) -> BeautifulSoup | None:
    r = http.get(f"{BASE}{path}", timeout=45)
    if r.status_code != 200:
        return None
    return BeautifulSoup(r.text, "html.parser")


def _norm_abk(s: str) -> str:
    return re.sub(r"[\s./_-]", "", s).lower()


ROMAN = {1: "I", 2: "II", 3: "III", 4: "IV", 5: "V", 6: "VI", 7: "VII",
         8: "VIII", 9: "IX", 10: "X", 11: "XI", 12: "XII", 13: "XIII",
         14: "XIV"}
# statutory renames: GII keeps the historic jurabk, buzer files under the
# current abbreviation
RENAMED = {"AsylVfG": "AsylG", "RuStAG": "StAG"}


def _candidates(jurabk: str) -> list[str]:
    """Buzer-notation candidates for a GII jurabk, best first.
    Observed patterns: SGB_I (roman + underscore), FreizuegG-EU
    (transliterated, slash->dash), RBEG_2021 (underscore year),
    AufenthG (year dropped), AsylG (renamed act)."""
    out = []

    def add(c):
        c = c.strip()
        if c and c not in out:
            out.append(c)

    base = re.sub(r"\s+(19|20)\d{2}$", "", jurabk).strip()
    m = re.match(r"^SGB\s*(\d+)$", base)
    if m and int(m.group(1)) in ROMAN:
        add(f"SGB_{ROMAN[int(m.group(1))]}")
    add(RENAMED.get(base, ""))
    add(base.replace(" ", ""))
    add(jurabk.replace(" ", "_"))
    tr = (base.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
              .replace("ß", "ss").replace("/", "-").replace(" ", ""))
    add(tr)
    return out


def _id_if_named(page: BeautifulSoup, want: str) -> int | None:
    """Act id from a page, but only if the page demonstrably IS that act:
    buzer names it in <h1 class="t"> (act + search-direct-hit pages) or
    leads <title> with the abbreviation. The cookie-fed "Verlauf" sidebar
    (div#history) links the previously visited act\'s l.htm and once made
    every unresolved jurabk inherit its predecessor\'s id — strip it."""
    for div in page.find_all(class_="history"):
        div.decompose()
    a = page.find("a", href=re.compile(r"/gesetz/(\d+)/l\.htm"))
    if not a:
        return None
    h1 = page.find("h1", class_="t")
    title = page.find("title")
    named = (want in _norm_abk(h1.get_text()) if h1 else False) or \
            (title is not None
             and _norm_abk(title.get_text()).startswith(want))
    if not named:
        return None
    return int(re.search(r"/gesetz/(\d+)/", a["href"]).group(1))


def act_id_for(http: Http, jurabk: str) -> int | None:
    """Resolve a GII jurabk to buzer's internal act id — VERIFIED: the page
    we land on must actually name the abbreviation, otherwise the s1 search
    silently hands us the first vaguely similar act (observed: 'WaffG 2002'
    resolving to the VwVfG page)."""
    for cand in _candidates(jurabk):
        want = _norm_abk(cand)
        s = soup_get(http, f"/{cand}.htm")
        if s is not None:
            aid = _id_if_named(s, want)
            if aid:
                return aid
        # exact search hits render the act page inline (h1.t + l.htm link)
        s = soup_get(http, f"/s1.htm?g={cand}")
        if s is None:
            continue
        aid = _id_if_named(s, want)
        if aid:
            return aid
        for hit in s.find_all("a", class_="ltg", href=True)[:4]:
            if want not in _norm_abk(hit.get_text()):
                continue
            sub = soup_get(http, hit["href"].replace(BASE, ""))
            if sub is not None:
                aid = _id_if_named(sub, want)
                if aid:
                    return aid
    return None


def versions_of(http: Http, jurabk: str, act_id: int) -> list[dict]:
    s = soup_get(http, f"/gesetz/{act_id}/l.htm")
    if s is None:
        return []
    out = []
    for a in s.find_all("a", href=re.compile(rf"/gesetz/{act_id}/v\d+-")):
        m = re.search(r"v(\d+)-(\d{4}-\d{2}-\d{2})\.htm", a["href"])
        if not m:
            continue
        # the row text around the link names the amending act
        row = a.find_parent(["tr", "li", "p"])
        title = " ".join((row.get_text(" ", strip=True) if row
                          else a.get_text(" ", strip=True)).split())[:400]
        out.append({"jurabk": jurabk, "act_id": act_id,
                    "date": m.group(2), "title": title,
                    "synopsis_url": f"{BASE}/gesetz/{act_id}/"
                                    f"v{m.group(1)}-{m.group(2)}.htm"})
    # dedupe by (date, url)
    seen, uniq = set(), []
    for v in out:
        k = (v["date"], v["synopsis_url"])
        if k not in seen:
            seen.add(k)
            uniq.append(v)
    return uniq


def upcoming(http: Http) -> list[dict]:
    """Changes entering force in the next ~100 days (/v.htm). Each row
    links the TARGET act's version log as /gesetz/<id>/l.htm#m<ddmmyyyy>
    — target act id and in-force date live in the anchor itself, so no
    fragile rowspan/date-cell tracking is needed."""
    s = soup_get(http, "/v.htm")
    if s is None:
        return []
    out, seen = [], set()
    for a in s.find_all("a", href=re.compile(r"/gesetz/\d+/l\.htm#m\d{8}")):
        m = re.search(r"/gesetz/(\d+)/l\.htm#m(\d{8})", a["href"])
        aid, d8 = int(m.group(1)), m.group(2)
        date = f"{d8[4:]}-{d8[2:4]}-{d8[:2]}"
        if (aid, date) in seen:
            continue
        seen.add((aid, date))
        td = a.find_parent("td")
        ltg = td.find("a", class_="ltg") if td else None
        title = " ".join(((ltg or a).get_text(" ", strip=True)).split())
        out.append({"date": date, "act_id": aid, "title": title[:400],
                    "url": f"{BASE}/gesetz/{aid}/l.htm"})
    return out


def main() -> int:
    gii = latest_snapshot("gii")
    if not gii:
        print("run fetch_gii.py first", file=sys.stderr)
        return 1
    acts = list(read_jsonl(gii / "acts.jsonl"))
    http = Http(delay=0.9)                       # extra polite: private site
    from http.cookiejar import DefaultCookiePolicy
    http.s.cookies.set_policy(                   # no session -> no "Verlauf"
        DefaultCookiePolicy(allowed_domains=[]))  # sidebar contamination

    versions, misses = [], []
    for a in acts:
        jurabk = (a.get("jurabk") or "").strip()
        if not jurabk:
            continue
        aid = act_id_for(http, jurabk)
        if not aid:
            misses.append(jurabk)
            continue
        vs = versions_of(http, jurabk, aid)
        versions.extend(vs)
        print(f"  {jurabk:>18}  act {aid:<6} {len(vs):3} versions")
    up = upcoming(http)

    out = snapshot_dir("buzer")
    write_jsonl(out / "versions.jsonl", versions)
    write_jsonl(out / "upcoming.jsonl", up)
    print(f"\n{len(versions)} versions, {len(up)} upcoming changes -> {out}")
    if misses:
        print(f"unresolved jurabk: {misses}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
