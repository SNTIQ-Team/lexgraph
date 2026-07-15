#!/usr/bin/env python3
"""Update the persistent state/history of explicitly watched procedures.

Inputs are immutable official-source snapshots (DIP and EUR-Lex).  Unchanged
checks update ``last_checked`` without bloating the history.  Terminal items
remain as an auditable archive but leave the active polling set; for EU items,
the fetcher marks terminal only after OJ publication *and* a persisted review
of final Article 2 against the tracked proposal, not merely political agreement.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))

from common import latest_snapshot, read_jsonl  # noqa: E402

WATCHLIST = ROOT / "data" / "procedure_watchlist.json"
STATE = ROOT / "data" / "procedure_watch_state.json"
HISTORY = ROOT / "data" / "procedure_watch_history.jsonl"

DIP_TERMINAL_STATUSES = {
    "Verkündet",
    "Abgelehnt",
    "Für erledigt erklärt",
    "Einbringung abgelehnt",
}


def _read_rows(source: str, filename: str) -> list[dict]:
    snapshot = latest_snapshot(source)
    path = snapshot / filename if snapshot else None
    return list(read_jsonl(path)) if path and path.is_file() else []


def _dip_row(watch_key: str, config: dict, row: dict) -> dict:
    status = str(row.get("beratungsstand") or "?")
    return {
        "id": watch_key,
        "watch_id": config.get("id") or watch_key,
        "source": "DIP",
        "jurisdiction": config.get("jurisdiction") or "DE",
        "procedure": watch_key,
        "gesta": row.get("gesta"),
        "title": row.get("titel") or watch_key,
        "status": status,
        "stage": status,
        "date": row.get("datum"),
        "updated": row.get("aktualisiert"),
        "url": f"https://dip.bundestag.de/vorgang/_/{watch_key}",
        "promulgation": row.get("verkuendung") or [],
        "entry_into_force": row.get("inkrafttreten") or [],
        "adopted_celexes": [],
        "official_journal": [],
        "terminal": status in DIP_TERMINAL_STATUSES,
    }


def _eu_row(watch_key: str, config: dict, row: dict) -> dict:
    return {
        "id": watch_key,
        "watch_id": config.get("id") or watch_key,
        "source": "EUR-Lex",
        "jurisdiction": config.get("jurisdiction") or "EU",
        "procedure": row.get("procedure") or config.get("procedure"),
        "gesta": None,
        "title": row.get("title") or config.get("procedure") or watch_key,
        "status": row.get("status") or "?",
        "stage": row.get("stage") or row.get("status") or "?",
        "date": row.get("date"),
        "updated": row.get("updated"),
        "url": row.get("url") or config.get("official_url"),
        "promulgation": [],
        "entry_into_force": [],
        "adopted_celexes": row.get("adopted_celexes") or [],
        "official_journal": row.get("official_journal") or [],
        "events": row.get("events") or [],
        "publication_detected": bool(row.get("publication_detected")),
        "awaiting_final_review": bool(row.get("awaiting_final_review")),
        "final_text_review": row.get("final_text_review"),
        "terminal": bool(row.get("terminal")),
    }


def _fingerprint(row: dict) -> str:
    fields = {key: row.get(key) for key in (
        "status", "stage", "date", "updated", "title", "terminal",
        "active", "tracking_state", "last_observed_status",
        "last_observed_stage", "last_observed_updated",
        "promulgation", "entry_into_force", "adopted_celexes",
        "official_journal", "events",
        "publication_detected", "awaiting_final_review", "final_text_review",
    )}
    return json.dumps(fields, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _event_id(key: str, old: dict | None, current: dict) -> str:
    transition = json.dumps({
        "id": key,
        # Distinguish a later recurrence of the same A -> B transition while
        # remaining stable across a retry from the exact same persisted state.
        "from_changed_at": (old or {}).get("last_changed"),
        "from": _fingerprint(old or {}),
        "to": _fingerprint(current),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "watch-" + hashlib.sha256(transition.encode("utf-8")).hexdigest()[:24]


def _write_json_atomic(path: Path, payload: object) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(path)


def update_watch_state(watchlist_path: Path, state_path: Path,
                       history_path: Path, dip_rows: list[dict],
                       eu_rows: list[dict], now: str) -> dict:
    watchlist = json.loads(watchlist_path.read_text(encoding="utf-8"))
    configs = watchlist.get("procedures") or {}
    previous = json.loads(state_path.read_text(encoding="utf-8")) \
        if state_path.is_file() else {"procedures": {}}
    previous_rows = previous.get("procedures") or {}
    dip = {str(row.get("id")): row for row in dip_rows}
    eu = {str(row.get("id")): row for row in eu_rows}
    state_rows: dict[str, dict] = {}
    history_additions: list[dict] = []
    existing_history = (list(read_jsonl(history_path))
                        if history_path.is_file() else [])
    history_by_id = {str(event.get("event_id")): event
                     for event in existing_history if event.get("event_id")}

    for key, config in configs.items():
        key = str(key)
        old = previous_rows.get(key)
        # Once an item reaches a terminal/archive state, retain its immutable
        # final observation.  It stays visible but no longer accrues checks.
        if old and not old.get("active", True):
            state_rows[key] = old
            continue
        source = str(config.get("source") or "DIP").casefold()
        raw = eu.get(key) if source == "eur-lex" else dip.get(key)
        if raw is None:
            current = {
                "id": key,
                "watch_id": (old or {}).get("watch_id") or
                            config.get("id") or key,
                "source": (old or {}).get("source") or
                          config.get("source") or "DIP",
                "jurisdiction": (old or {}).get("jurisdiction") or
                                config.get("jurisdiction"),
                "procedure": (old or {}).get("procedure") or
                             config.get("procedure") or key,
                "gesta": (old or {}).get("gesta"),
                "title": (old or {}).get("title") or
                         config.get("procedure") or key,
                "status": "Not found in latest official snapshot",
                "stage": "source_missing",
                "date": (old or {}).get("date"),
                "updated": None,
                "url": (old or {}).get("url") or config.get("official_url"),
                "terminal": False,
                "first_seen": (old or {}).get("first_seen") or now,
                "source_missing_since": (
                    (old or {}).get("source_missing_since")
                    if (old or {}).get("tracking_state") == "source_missing"
                    else now),
            }
            previous_observed = old or {}
            if previous_observed.get("tracking_state") == "source_missing":
                current["last_observed_status"] = \
                    previous_observed.get("last_observed_status")
                current["last_observed_stage"] = \
                    previous_observed.get("last_observed_stage")
                current["last_observed_updated"] = \
                    previous_observed.get("last_observed_updated")
                current["last_observed_at"] = \
                    previous_observed.get("last_observed_at")
            elif old:
                current["last_observed_status"] = old.get("status")
                current["last_observed_stage"] = old.get("stage")
                current["last_observed_updated"] = old.get("updated")
                current["last_observed_at"] = old.get("last_checked")
            monitor = bool(config.get("monitor", True))
            current["active"] = monitor
            current["tracking_state"] = (
                "source_missing" if monitor else "archived")
            current["last_checked"] = now
        else:
            current = (_eu_row(key, config, raw) if source == "eur-lex"
                       else _dip_row(key, config, raw))
            monitor = bool(config.get("monitor", True))
            current["active"] = monitor and not current["terminal"]
            current["tracking_state"] = (
                "pending_final_review"
                if current["active"] and current.get("awaiting_final_review") else
                "active" if current["active"] else
                "terminal" if current["terminal"] else "archived")
            current["first_seen"] = (old or {}).get("first_seen") or now
            current["last_checked"] = now
            current["last_observed_status"] = current.get("status")
            current["last_observed_stage"] = current.get("stage")
            current["last_observed_updated"] = current.get("updated")
            current["last_observed_at"] = now
            if not current["active"]:
                current["monitoring_stopped_at"] = now

        changed = old is None or _fingerprint(old) != _fingerprint(current)
        if changed:
            event_id = _event_id(key, old, current)
            prior_event = history_by_id.get(event_id)
            current["last_changed"] = (
                prior_event.get("observed_at") if prior_event else now)
            event = {
                "event_id": event_id,
                "observed_at": now,
                "id": key,
                "watch_id": current.get("watch_id"),
                "source": current.get("source"),
                "event": (
                    "first_seen" if old is None else
                    "source_missing"
                    if current.get("tracking_state") == "source_missing" else
                    "source_restored"
                    if old.get("tracking_state") == "source_missing" else
                    "terminal" if current.get("terminal") else
                    "status_changed"),
                "from_status": old.get("status") if old else None,
                "to_status": current.get("status"),
                "from_stage": old.get("stage") if old else None,
                "stage": current.get("stage"),
                "official_updated": current.get("updated"),
                "active": current.get("active"),
                "terminal": current.get("terminal"),
                "url": current.get("url"),
                "adopted_celexes": current.get("adopted_celexes") or [],
                "official_journal": current.get("official_journal") or [],
            }
            if prior_event is None:
                history_additions.append(event)
                history_by_id[event_id] = event
        else:
            current["last_changed"] = old.get("last_changed")
        state_rows[key] = current

    payload = {"schema_version": 1, "checked_at": now,
               "procedures": state_rows}
    state_path.parent.mkdir(parents=True, exist_ok=True)
    # History first: a crash before the state replacement is safe because the
    # stable transition id is deduplicated on the next run.  Writing state
    # first could permanently lose the corresponding audit event.
    if history_additions:
        with history_path.open("a", encoding="utf-8") as handle:
            for row in history_additions:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    _write_json_atomic(state_path, payload)
    return {"state": payload, "changes": history_additions}


def main() -> int:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    result = update_watch_state(
        WATCHLIST, STATE, HISTORY,
        _read_rows("dip", "vorgaenge.jsonl"),
        _read_rows("eu_watch", "procedures.jsonl"), now)
    active = sum(bool(row.get("active"))
                 for row in result["state"]["procedures"].values())
    print(f"procedure-watch: {len(result['state']['procedures'])} total / "
          f"{active} active / {len(result['changes'])} changed -> {STATE}")
    for row in result["changes"]:
        print(f"  {row['id']}: {row['event']} -> {row['to_status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
