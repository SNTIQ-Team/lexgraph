"""Generate the LLM digest (web/data/digest.json): a short multilingual
summary of legislative activity for the SNTIQ frontend. The FACTS are the
honest part — composed deterministically here from the exported web data;
the OpenRouter model only phrases them and must not invent specifics.

    OPENROUTER_API_KEY=sk-or-… python3 tools/build_digest.py

Reads (all produced by tools/build_web_data.py):
    web/data/summary.json      patch counts by status
    web/data/feed.json         merged event stream (past + scheduled)
    web/data/wiki.json         act index (last_change / next_change)
    web/data/decisions.json    curated court decisions

Writes:
    web/data/digest.json       {generated_at, model, llm, periods:
                                {year|month|upcoming: {de, en, ru, ua}}}

Without OPENROUTER_API_KEY the script skips gracefully (exit 0) and never
touches an existing digest.json — the old digest stays served. Model:
OPENROUTER_MODEL if set, else a list of free-tier fallbacks tried in order
(404 model-not-found / 429 rate-limit / invalid output → next model).
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web" / "data"

API_URL = "https://openrouter.ai/api/v1/chat/completions"
FALLBACK_MODELS = [
    "deepseek/deepseek-chat-v3-0324:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "google/gemini-2.0-flash-exp:free",
    "mistralai/mistral-small-3.1-24b-instruct:free",
]
PERIODS = ("year", "month", "upcoming")
LANGS = ("de", "en", "ru", "ua")

SYSTEM_PROMPT = (
    "You write a neutral, factual digest of German legislative activity "
    "for a public legal-information website. You are given pre-computed "
    "facts as JSON. You must ONLY restate those facts in plain language — "
    "never invent names, numbers, dates or any specifics that are not in "
    "the facts. Hedge everything about the future (\"voraussichtlich\" / "
    "\"likely\"). Give NO legal advice. Respond with STRICT JSON, no "
    "markdown, exactly this shape: "
    '{"year":{"de":str,"en":str,"ru":str,"ua":str},'
    '"month":{"de":str,"en":str,"ru":str,"ua":str},'
    '"upcoming":{"de":str,"en":str,"ru":str,"ua":str}} '
    "with 2-4 plain-language sentences per value: 'year' = what changed "
    "over the last 12 months, 'month' = the last 30 days, 'upcoming' = "
    "what is scheduled or likely next. 'de' is German, 'en' English, "
    "'ru' Russian, 'ua' Ukrainian."
)


def read(name: str, fallback):
    """One exported web/data JSON file; missing files must not crash the
    digest (the facts just get thinner)."""
    f = WEB / f"{name}.json"
    if not f.is_file():
        return fallback
    return json.loads(f.read_text(encoding="utf-8"))


# ----------------------------------------------------------------- facts
def build_facts(summary: dict, feed: list[dict], wiki: list[dict],
                decisions: list[dict]) -> dict:
    """Deterministic, compact facts — the only ground truth the model may
    phrase. feed.json is newest first; dates are ISO, so string compares
    order correctly."""
    today = date.today().isoformat()
    d30 = (date.today() - timedelta(days=30)).isoformat()
    d365 = (date.today() - timedelta(days=365)).isoformat()
    patches = summary.get("patches") or {}

    month_ev = [e for e in feed if d30 <= e["time"] <= today]
    month_counts: dict[str, int] = {}
    for e in month_ev:
        k = f"{e['kind']} [{e['source']}]"
        month_counts[k] = month_counts.get(k, 0) + 1
    month = {
        "window": f"{d30} .. {today}",
        "event_counts_by_kind_source": month_counts,
        "recent_events": [{"date": e["time"], "kind": e["kind"],
                           "title": (e["title"] or "")[:110]}
                          for e in month_ev[:25]],
    }

    year_ev = [e for e in feed if d365 <= e["time"] <= today]
    year_counts: dict[str, int] = {}
    for e in year_ev:
        year_counts[e["kind"]] = year_counts.get(e["kind"], 0) + 1
    changed = sorted((a for a in wiki if a.get("last_change")),
                     key=lambda a: a["last_change"], reverse=True)
    year = {
        "window": f"{d365} .. {today}",
        "event_counts_by_kind": year_counts,
        "recently_changed_acts": [
            {"act": a["jurabk"], "title": (a.get("title") or "")[:110],
             "last_change": a["last_change"]} for a in changed[:12]],
        "published_patches_total": patches.get("published", 0),
        "court_decisions": [
            {"court": d.get("court_short"), "az": d.get("az"),
             "date": d.get("date"), "title": (d.get("title") or "")[:110]}
            for d in decisions[:15]],
    }

    fut_ev = sorted((e for e in feed if e["time"] > today),
                    key=lambda e: e["time"])
    upcoming = {
        "today": today,
        "scheduled_events": [{"date": e["time"], "kind": e["kind"],
                              "title": (e["title"] or "")[:110]}
                             for e in fut_ev[:25]],
        "acts_with_scheduled_changes": [
            {"act": a["jurabk"], "title": (a.get("title") or "")[:110],
             "next_change": a["next_change"]}
            for a in sorted((a for a in wiki
                             if (a.get("next_change") or "") > today),
                            key=lambda a: a["next_change"])[:15]],
        "pending_patches": (patches.get("proposed", 0)
                            + patches.get("adopted", 0)),
    }
    return {"today": today, "month": month, "year": year,
            "upcoming": upcoming}


# ------------------------------------------------------------------- llm
def _parse_json(text: str) -> dict | None:
    """Parse model output defensively: strict JSON is requested via
    response_format, but some models wrap it in ```json fences anyway."""
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    for cand in (t, t[t.find("{"):t.rfind("}") + 1]):
        try:
            obj = json.loads(cand)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _valid(digest: dict) -> bool:
    """All 3 periods × 4 languages present as non-empty strings."""
    for p in PERIODS:
        block = digest.get(p)
        if not isinstance(block, dict):
            return False
        for lang in LANGS:
            v = block.get(lang)
            if not isinstance(v, str) or not v.strip():
                return False
    return True


def ask(key: str, model: str, facts: dict) -> dict | None:
    """One OpenRouter attempt. Returns the validated periods dict, or None
    (HTTP error, unparseable output, missing leaf) so the caller moves on
    to the next model."""
    try:
        resp = requests.post(
            API_URL,
            headers={"Authorization": f"Bearer {key}",
                     "HTTP-Referer": "https://sntiq.com",
                     "X-Title": "SNTIQ Lexgraph"},
            json={"model": model,
                  "messages": [
                      {"role": "system", "content": SYSTEM_PROMPT},
                      {"role": "user",
                       "content": json.dumps(facts, ensure_ascii=False)}],
                  "response_format": {"type": "json_object"}},
            timeout=180)
    except requests.RequestException as exc:
        print(f"  [warn] {model}: {exc}")
        return None
    if resp.status_code != 200:            # 404 model gone, 429 rate limit
        print(f"  [warn] {model}: HTTP {resp.status_code}")
        return None
    try:
        content = resp.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        print(f"  [warn] {model}: malformed API response")
        return None
    raw = _parse_json(content)
    if raw is None or not _valid(raw):
        print(f"  [warn] {model}: invalid digest JSON")
        return None
    return {p: {lg: raw[p][lg].strip() for lg in LANGS} for p in PERIODS}


def main() -> int:
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        print("digest skipped — no OPENROUTER_API_KEY "
              "(existing digest.json kept)")
        return 0

    facts = build_facts(read("summary", {}), read("feed", []),
                        read("wiki", []), read("decisions", []))

    models = [os.environ["OPENROUTER_MODEL"]] \
        if os.environ.get("OPENROUTER_MODEL") else FALLBACK_MODELS
    periods = None
    for model in models:
        periods = ask(key, model, facts)
        if periods:
            break
    if not periods:
        print("[warn] no model produced a valid digest — "
              "digest.json unchanged")
        return 1

    out = {"generated_at": datetime.now(timezone.utc).isoformat(
               timespec="seconds"),
           "model": model, "llm": True, "periods": periods}
    f = WEB / "digest.json"
    f.write_text(json.dumps(out, ensure_ascii=False,
                            separators=(",", ":")))
    print(f"digest -> {f}   (model: {model})")
    print(f"  {'digest.json':16} {f.stat().st_size / 1024:8.1f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
