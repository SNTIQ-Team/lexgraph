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
MODELS_URL = "https://openrouter.ai/api/v1/models"
# Free-tier slugs churn over time; when the whole curated list fails, the
# live /models catalog is queried for any remaining :free chat model.
FALLBACK_MODELS = [
    # ordered by measured nightly RELIABILITY, not raw benchmark rank:
    # nemotron-super is the only slug that consistently finishes the full
    # digest prompt on the free tier (2026-07: gpt-oss-120b rotated out
    # with 404, qwen3-next rate-limits with 429, hy3 reasons for >400 s)
    "nvidia/nemotron-3-super-120b-a12b:free",
    "google/gemma-4-31b-it:free",
    "tencent/hy3:free",
    "qwen/qwen3-next-80b-a3b-instruct:free",
    "meta-llama/llama-3.3-70b-instruct:free",
]


def discover_free_models(limit: int = 5) -> list[str]:
    """Last-resort dynamic discovery of :free chat models (largest first)."""
    try:
        rsp = requests.get(MODELS_URL, timeout=30)
        rsp.raise_for_status()
        rows = rsp.json().get("data", [])
    except Exception:  # noqa: BLE001 — discovery is best-effort
        return []
    free = [m for m in rows
            if ":free" in (m.get("id") or "")
            and m.get("pricing", {}).get("prompt") == "0"
            and m.get("pricing", {}).get("completion") == "0"
            and "coder" not in m["id"] and "code" not in m["id"]]
    free.sort(key=lambda m: -(m.get("context_length") or 0))
    return [m["id"] for m in free[:limit]]
PERIODS = ("year", "month", "upcoming")
LANGS = ("de", "en", "ru", "ua")

SYSTEM_PROMPT = (
    "You write a neutral, factual digest of German legislative activity "
    "for a public legal-information website. You are given pre-computed "
    "facts as JSON. You must ONLY restate those facts in plain language — "
    "never invent names, numbers, dates or any specifics that are not in "
    "the facts. BE CONCRETE: name the specific laws by their abbreviation "
    "(AsylbLG, SGB II, AufenthG …), the concrete court decisions by court "
    "and docket number, and concrete dates from the facts. Generic filler "
    "like 'multiple publications', 'various changes' or 'several notices' "
    "is forbidden when the facts name the items — pick the 2-4 most "
    "relevant concrete items instead. Hedge everything about the future "
    "(\"voraussichtlich\" / \"likely\"). Give NO legal advice. Respond "
    "with STRICT JSON, no markdown, exactly this shape: "
    '{"year":{"de":str,"en":str,"ru":str,"ua":str},'
    '"month":{"de":str,"en":str,"ru":str,"ua":str},'
    '"upcoming":{"de":str,"en":str,"ru":str,"ua":str}} '
    "with 5-8 sentences (roughly 100-170 words) per value: 'year' = what "
    "changed over the last 12 months, 'month' = the last 30 days, "
    "'upcoming' = what is scheduled or likely next. NO REPETITION between "
    "periods: mention each notable item exactly once, in the single period "
    "it fits best — an event from the last 30 days belongs in 'month' and "
    "must NOT be retold in 'year'; 'year' covers the broader arc beyond "
    "the current month. Go deep, not wide: pick the most consequential "
    "items and explain in one clause WHY each matters for the reader "
    "(which group is affected, what changes for them), citing the exact "
    "§§, dates and docket numbers from the facts. 'de' is German, 'en' "
    "English, 'ru' Russian, 'ua' Ukrainian. Write native-quality prose in "
    "each language — no words from other languages mixed in."
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
                           "title": (e["title"] or "")[:90]}
                          for e in month_ev[:14]],
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
            {"act": a["jurabk"], "title": (a.get("title") or "")[:90],
             "last_change": a["last_change"]} for a in changed[:8]],
        "published_patches_total": patches.get("published", 0),
        "court_decisions": [
            {"court": d.get("court_short"), "az": d.get("az"),
             "date": d.get("date"), "title": (d.get("title") or "")[:90]}
            for d in decisions[:8]],
    }

    fut_ev = sorted((e for e in feed if e["time"] > today),
                    key=lambda e: e["time"])
    upcoming = {
        "today": today,
        "scheduled_events": [{"date": e["time"], "kind": e["kind"],
                              "title": (e["title"] or "")[:90]}
                             for e in fut_ev[:14]],
        "acts_with_scheduled_changes": [
            {"act": a["jurabk"], "title": (a.get("title") or "")[:90],
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


# A period shorter than this is terse filler ("multiple laws were
# published") — reject it so the chain moves to a model that elaborates.
_MIN_PERIOD_CHARS = 400

# Appended to the user message: models weigh the final instruction heavier
# than the system prompt, and the terse ones need the numeric floor spelled
# out right next to the data.
_LENGTH_REMINDER = (
    "\n\nIMPORTANT: every one of the 12 string values must contain 5-8 full "
    "sentences (at least 450 characters). For the most relevant items "
    "explain WHO is affected and WHAT changes for them. Responses with "
    "short summary paragraphs will be rejected."
)


def _user_message(facts: dict) -> str:
    return json.dumps(facts, ensure_ascii=False) + _LENGTH_REMINDER


def _valid(digest: dict) -> bool:
    """All 3 periods × 4 languages present as substantial strings."""
    for p in PERIODS:
        block = digest.get(p)
        if not isinstance(block, dict):
            return False
        for lang in LANGS:
            v = block.get(lang)
            if not isinstance(v, str) or len(v.strip()) < _MIN_PERIOD_CHARS:
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
                       "content": _user_message(facts)}],
                  "response_format": {"type": "json_object"}},
            timeout=120)
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


GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
              "{model}:generateContent")


def ask_gemini(key: str, model: str, facts: dict) -> dict | None:
    """One native Gemini API attempt (official free tier — markedly better
    multilingual prose than the OpenRouter free slugs). Same validation
    contract as ask()."""
    try:
        resp = requests.post(
            GEMINI_URL.format(model=model),
            params={"key": key},
            json={"systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
                  "contents": [{"role": "user", "parts": [
                      {"text": _user_message(facts)}]}],
                  "generationConfig": {
                      "responseMimeType": "application/json",
                      "temperature": 0.4}},
            timeout=120)
    except requests.RequestException as exc:
        print(f"  [warn] gemini/{model}: {exc}")
        return None
    if resp.status_code != 200:
        print(f"  [warn] gemini/{model}: HTTP {resp.status_code}")
        return None
    try:
        content = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    except (ValueError, KeyError, IndexError, TypeError):
        print(f"  [warn] gemini/{model}: malformed API response")
        return None
    raw = _parse_json(content)
    if raw is None or not _valid(raw):
        print(f"  [warn] gemini/{model}: invalid digest JSON")
        return None
    return {p: {lg: raw[p][lg].strip() for lg in LANGS} for p in PERIODS}


def ask_g4f(facts: dict) -> tuple[dict | None, str]:
    """gpt4free (g4f) — community provider-rotating free access. Tried
    between Gemini and the OpenRouter chain whenever the package is
    installed. Providers churn and violate upstream ToS at their own risk —
    every failure just falls through to the next tier."""
    try:
        from g4f.client import Client  # heavy optional dependency
    except ImportError:
        return None, ""
    # Best first, but each capped so a hanging provider can't eat the run.
    # Reasoning/large models tend to give the richest digest; the terse ones
    # get rejected by _valid's length floor and fall through anyway.
    candidates = [m for m in (os.environ.get("G4F_MODEL", ""),
                              "gpt-5", "gpt-4o") if m]
    seen: set[str] = set()
    for gm in candidates:
        if gm in seen:
            continue
        seen.add(gm)
        try:
            rsp = Client().chat.completions.create(
                model=gm,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",
                     "content": _user_message(facts)}],
                timeout=75)
            content = rsp.choices[0].message.content
        except Exception as exc:  # noqa: BLE001 — g4f raises anything
            print(f"  [warn] g4f/{gm}: {type(exc).__name__}: {str(exc)[:70]}")
            continue
        raw = _parse_json(content)
        if raw is not None and _valid(raw):
            return ({p: {lg: raw[p][lg].strip() for lg in LANGS}
                     for p in PERIODS}, f"g4f/{gm}")
        print(f"  [warn] g4f/{gm}: invalid digest JSON")
    return None, ""


def _write(periods: dict, model: str) -> int:
    f = WEB / "digest.json"
    f.write_text(json.dumps(
        {"generated_at": datetime.now(timezone.utc).isoformat(
             timespec="seconds"),
         "model": model, "llm": True, "periods": periods},
        ensure_ascii=False, separators=(",", ":")))
    print(f"digest -> {f}   (model: {model})")
    print(f"  {'digest.json':16} {f.stat().st_size / 1024:8.1f} KB")
    return 0


def main() -> int:
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()

    # Strictly one generation per day (the nightly retrieval run) — a
    # manual refresh.sh re-run must not burn provider quota or churn the
    # published text. Override with --force.
    existing = WEB / "digest.json"
    if "--force" not in sys.argv and existing.is_file():
        try:
            prev_day = (json.loads(existing.read_text(encoding="utf-8"))
                        .get("generated_at") or "")[:10]
        except (ValueError, OSError):
            prev_day = ""
        if prev_day == datetime.now(timezone.utc).date().isoformat():
            print(f"digest already generated today ({prev_day}) — "
                  "skipping (--force to regenerate)")
            return 0

    facts = build_facts(read("summary", {}), read("feed", []),
                        read("wiki", []), read("decisions", []))

    # Provider tiers, best prose first:
    # 1. Gemini (official free tier) when a key is present
    if gemini_key:
        for gm in [os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
                   "gemini-2.0-flash"]:
            periods = ask_gemini(gemini_key, gm, facts)
            if periods:
                return _write(periods, f"google/{gm}")

    # 2. gpt4free, when installed
    periods, model = ask_g4f(facts)
    if periods:
        return _write(periods, model)

    # 3. OpenRouter free slugs
    if not key:
        print("[warn] no provider produced a digest and no "
              "OPENROUTER_API_KEY — digest.json unchanged")
        return 1
    models = [os.environ["OPENROUTER_MODEL"]] \
        if os.environ.get("OPENROUTER_MODEL") else FALLBACK_MODELS
    for model in models:
        periods = ask(key, model, facts)
        if periods:
            return _write(periods, model)
    # the curated slugs may all have rotated out of the free tier —
    # ask the live catalog for whatever :free chat models exist today
    discovered = [m for m in discover_free_models() if m not in models]
    print(f"[warn] curated models failed — trying discovered: "
          f"{', '.join(discovered) or 'none'}")
    for model in discovered:
        periods = ask(key, model, facts)
        if periods:
            return _write(periods, model)

    print("[warn] no model produced a valid digest — digest.json unchanged")
    return 1


if __name__ == "__main__":
    sys.exit(main())
