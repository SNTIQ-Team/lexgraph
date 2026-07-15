"""Deterministic evidence analysis for explicitly watched procedures.

This module deliberately does not call a model or the network.  It turns the
latest persisted official observations, their transition history and curated
document-role records into a stable, auditable presentation layer.  Forecasts
are stage-based estimates, never facts, and cannot turn a recommendation or a
political agreement into an adopted act.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime


def _urls(*values: object) -> list[str]:
    result: list[str] = []
    for value in values:
        if isinstance(value, str) and value.startswith(("https://", "http://")):
            if value not in result:
                result.append(value)
        elif isinstance(value, Iterable) and not isinstance(value, (str, bytes, dict)):
            for item in value:
                if isinstance(item, str) and item.startswith(("https://", "http://")):
                    if item not in result:
                        result.append(item)
    return result


def _check(check_id: str, kind: str, label: str, status: str,
           detail: str, *sources: object) -> dict:
    return {
        "id": check_id,
        "kind": kind,
        "label": label,
        "status": status,
        "detail": detail,
        "source_urls": _urls(*sources),
    }


def _factor(kind: str, label: str, detail: str, *sources: object) -> dict:
    return {
        "kind": kind,
        "label": label,
        "detail": detail,
        "source_urls": _urls(*sources),
    }


def _fact(fact_id: str, kind: str, statement: str, status: str,
          *sources: object) -> dict:
    return {
        "id": fact_id,
        "kind": kind,
        "statement": statement,
        "status": status,
        "source_urls": _urls(*sources),
    }


def _inference(inference_id: str, kind: str, statement: str,
               confidence: str, basis: list[str], *sources: object) -> dict:
    return {
        "id": inference_id,
        "kind": kind,
        "statement": statement,
        "confidence": confidence,
        "basis": basis,
        "source_urls": _urls(*sources),
    }


def _forecast(outcome: str, band: str, _minimum: int, _maximum: int,
              confidence: str, summary: str) -> dict:
    return {
        "outcome": outcome,
        "likelihood": {
            "band": band,
            # The rule set is not statistically calibrated.  Keep the schema
            # compatible with a future calibrated model without displaying a
            # made-up exact percentage today.
            "minimum": None,
            "maximum": None,
        },
        "confidence": confidence,
        "summary": summary,
        "not_a_fact": True,
    }


def _milestone(kind: str, label: str, description: str, status: str,
               conditions: list[str], *sources: object) -> dict:
    return {
        "kind": kind,
        "label": label,
        "description": description,
        "status": status,
        "conditions": conditions,
        "source_urls": _urls(*sources),
    }


def _chronology(row: dict, history: list[dict], fate: dict | None) -> list[dict]:
    events: list[dict] = []

    def add(date: object, label: object, kind: str, status: str,
            *sources: object) -> None:
        if not date or not label:
            return
        item = {
            "date": str(date),
            "label": str(label),
            "kind": kind,
            "status": status,
            "source_urls": _urls(*sources),
        }
        identity = (item["date"], item["label"], item["kind"])
        if not any((old["date"], old["label"], old["kind"]) == identity
                   for old in events):
            events.append(item)

    official_url = row.get("official_url") or row.get("url")
    for event in row.get("events") or []:
        if not isinstance(event, dict):
            continue
        add(event.get("date"), event.get("title") or event.get("stage"),
            "official_event", "verified", event.get("url"), official_url)
    council = row.get("council_development") or {}
    if isinstance(council, dict):
        add(council.get("date"), council.get("title") or council.get("stage"),
            "official_event", "verified", council.get("url"))
    for position in row.get("positions") or []:
        if not isinstance(position, dict):
            continue
        document = position.get("document") or {}
        document_number = document.get("number") if isinstance(document, dict) else None
        label = str(position.get("stage") or "Amtliche Verfahrensposition")
        if document_number and not str(document_number).startswith("bundestag-"):
            label += f" · {document_number}"
        validations = position.get("content_validations") or []
        evidence_status = (
            "validated" if validations and
            all(item.get("passed") for item in validations
                if isinstance(item, dict)) else "verified")
        add(position.get("date"), label, "official_event", evidence_status,
            document.get("url") if isinstance(document, dict) else None)
    for event in history:
        if not isinstance(event, dict):
            continue
        old = event.get("from_stage") or event.get("from_status")
        new = event.get("stage") or event.get("to_status")
        label = (f"{old} → {new}" if old and new and old != new else
                 str(new or event.get("event") or "Status check"))
        add(event.get("official_updated") or event.get("observed_at"), label,
            "watch_transition", "observed", event.get("url"))
    for publication in row.get("promulgation") or []:
        if isinstance(publication, dict):
            add(publication.get("verkuendungsdatum") or row.get("date"),
                publication.get("fundstelle") or "Promulgation",
                "promulgation", "verified",
                publication.get("pdf_url"), official_url)
    for effective in row.get("entry_into_force") or []:
        if isinstance(effective, dict):
            add(effective.get("datum"), "Entry into force",
                "entry_into_force", "verified", official_url)
    if fate:
        for document in fate.get("document_chain") or []:
            if isinstance(document, dict):
                add(document.get("date"),
                    f"{document.get('document')}: {document.get('finding')}",
                    "document_chain", "validated", document.get("url"),
                    document.get("vote_url"))
    events.sort(key=lambda event: (event["date"], event["kind"], event["label"]))
    return events


def _source_analysis(row: dict, config: dict) -> tuple[list[dict], list[dict]]:
    url = row.get("official_url") or row.get("url")
    missing = row.get("tracking_state") == "source_missing"
    stale = bool(row.get("source_stale"))
    facts = [_fact(
        "official_status", "official_observation",
        (f"Amtlicher Status: {row.get('status')}; Stadium: {row.get('stage')}."
         if not missing and not stale else
         "Die jüngste EUR-Lex-Abfrage ist fehlgeschlagen; angezeigt wird die letzte bestätigte amtliche Beobachtung."
         if stale else
         "Der Eintrag fehlt im jüngsten amtlichen Snapshot; der vorherige "
         "Status wird nicht als aktuelle Beobachtung ausgegeben."),
        "unverified" if missing or stale else "verified", url)]
    checks = [_check(
        "official_source_available", "source", "Amtliche Quelle verfügbar",
        "failed" if missing else "pending" if stale else "passed",
        ("Der beobachtete Vorgang fehlt derzeit in der amtlichen Quelle."
         if missing else
         "Die letzte bestätigte Beobachtung wird mit einer Stale-Warnung weitergeführt."
         if stale else
         "Status und Stadium stammen aus dem jüngsten amtlichen Snapshot."),
        url)]
    scope_source = config.get("scope_source")
    checks.append(_check(
        "scope_source_linked", "source", "Quelle für den beschriebenen Regelungsumfang",
        "passed" if scope_source else "pending",
        ("Der beschriebene Entwurfsumfang ist mit einer amtlichen Vorlage verknüpft."
         if scope_source else
         "Für die Inhaltsbeschreibung ist noch keine gesonderte amtliche Vorlage verknüpft."),
        scope_source))
    return facts, checks


def _analyse_eu(row: dict, config: dict, checks: list[dict],
                facts: list[dict]) -> tuple[dict, list[dict], dict, list[dict], list[str]]:
    official_url = row.get("official_url") or row.get("url")
    proposal_url = config.get("proposal_url") or config.get("scope_source")
    stage = str(row.get("stage") or "")
    status = str(row.get("status") or "")
    stage_folded = stage.casefold()
    adopted = list(row.get("adopted_celexes") or [])
    journal = list(row.get("official_journal") or [])
    review = row.get("final_text_review") or config.get("final_text_review") or {}
    review_passed = bool(
        isinstance(review, dict) and review.get("status") == "passed" and
        review.get("article_2_compared") is True)
    preparing_agreement = "preparation for a political agreement" in stage_folded
    political_agreement = ("political agreement" in stage_folded and
                           not preparing_agreement)
    council_signal = preparing_agreement or political_agreement

    checks.extend([
        _check("proposal_identified", "role", "Kommissionsvorschlag identifiziert",
               "passed" if config.get("celex_proposal") and proposal_url else "pending",
               "Der Vorschlag ist als Entwurf gekennzeichnet und über seine CELEX-Nummer verknüpft.",
               proposal_url),
        _check("political_agreement_not_adoption", "role",
               "Politische Einigung von Annahme getrennt",
               "passed" if council_signal else "not_applicable",
               "Auch die Vorbereitung einer politischen Einigung oder eine politische Einigung selbst sind weder formelle Ratsannahme noch geltendes Recht.",
               config.get("council_register_url"), official_url),
        _check("adopted_act_identified", "transition", "Angenommenen Rechtsakt identifiziert",
               "passed" if adopted else "pending",
               ("Ein finaler CELEX-Rechtsakt ist erfasst." if adopted else
                "Noch kein finaler CELEX-Rechtsakt in der amtlichen Verfahrensakte."),
               official_url),
        _check("official_journal_publication", "transition", "Veröffentlichung im Amtsblatt",
               "passed" if journal else "pending",
               ("Eine Amtsblattfundstelle ist erfasst." if journal else
                "Eine Amtsblattfundstelle ist noch nicht erfasst."), official_url),
        _check("final_article_2_review", "final_text", "Finalen Artikel 2 abgeglichen",
               "passed" if review_passed else "pending",
               ("Der endgültige Artikel 2 wurde gegen den Vorschlag abgeglichen."
                if review_passed else
                "Der endgültige Artikel 2 kann erst nach Vorliegen des angenommenen und veröffentlichten Textes abgeglichen werden."),
               proposal_url, official_url),
    ])
    if preparing_agreement:
        facts.append(_fact(
            "council_prepares_political_agreement", "procedural_signal",
            "Die Coreper-Tagesordnung vom 10. Juli 2026 bezeichnet den Punkt nur als Vorbereitung einer politischen Einigung; eine formelle Ratsannahme ist damit nicht belegt.",
            "verified", config.get("council_register_url"), official_url))
    elif political_agreement:
        facts.append(_fact(
            "council_political_agreement", "procedural_signal",
            "Im Ratsregister ist eine politische Einigung dokumentiert; der EUR-Lex-Vorgang ist weiterhin nicht als endgültig abgeschlossen belegt.",
            "verified", config.get("council_register_url"), official_url))

    factors: list[dict] = []
    warnings = ["Der Prognosekorridor ist eine regelbasierte Einschätzung und kein amtlicher Verfahrensstatus."]
    if row.get("source_stale"):
        factors.append(_factor(
            "uncertainty", "EUR-Lex-Abfrage vorübergehend fehlgeschlagen",
            "Die Prognose nutzt die letzte bestätigte amtliche Beobachtung und wird nach einer erfolgreichen Abfrage neu bewertet.",
            official_url))
        warnings.append(str(row.get("retrieval_warning") or
                            "Die aktuelle EUR-Lex-Beobachtung ist als veraltet markiert."))
    if preparing_agreement:
        factors.append(_factor(
            "supports", "Vorbereitung einer politischen Einigung",
            "Die Behandlung im Coreper ist ein starkes Fortgangssignal, aber noch keine dokumentierte Einigung oder Annahme.",
            config.get("council_register_url")))
    elif political_agreement:
        factors.append(_factor(
            "supports", "Politische Einigung im Rat",
            "Die im Ratsregister dokumentierte Einigung ist ein starkes Signal für die spätere formelle Annahme.",
            config.get("council_register_url")))
    if status.casefold() == "ongoing" or not adopted:
        factors.append(_factor(
            "uncertainty", "Vorgang noch offen",
            "EUR-Lex weist noch keinen angenommenen finalen Rechtsakt mit CELEX aus.",
            official_url))
    if not journal:
        factors.append(_factor(
            "uncertainty", "Keine Amtsblattfundstelle",
            "Ohne Amtsblattfundstelle ist weder Veröffentlichung noch Inkrafttreten belegt.",
            official_url))
    if review_passed and row.get("terminal"):
        forecast = _forecast(
            "final_text_validated", "completed", 100, 100, "high",
            "Der angenommene Text wurde veröffentlicht und der überwachte Artikel 2 gegen den Vorschlag geprüft.")
        next_milestone = _milestone(
            "monitoring_complete", "Überwachung abgeschlossen",
            "Keine weitere häufige Abfrage; der validierte Abschluss bleibt archiviert.",
            "completed", [], official_url)
    elif row.get("awaiting_final_review") or journal:
        forecast = _forecast(
            "final_text_review_pending", "high", 95, 100, "high",
            "Der Rechtsakt ist veröffentlicht; offen ist die inhaltliche Gegenprüfung des endgültigen Artikels 2.")
        next_milestone = _milestone(
            "final_text_review", "Endfassung gegenprüfen",
            "Artikel 2 des veröffentlichten Rechtsakts wird mit COM(2026) 345 verglichen.",
            "pending", ["finalen CELEX erfassen", "Artikel 2 vergleichen",
                        "Abweichungen dokumentieren"], proposal_url, official_url)
    elif adopted:
        forecast = _forecast(
            "official_journal_publication_likely", "high", 85, 98, "medium",
            "Eine formelle Annahme ist erfasst; als Nächstes werden Amtsblattfundstelle und Endfassung erwartet.")
        next_milestone = _milestone(
            "official_journal_publication", "Veröffentlichung im Amtsblatt",
            "Die Fundstelle und der finale CELEX müssen erfasst werden.", "expected",
            ["Amtsblattfundstelle veröffentlichen"], official_url)
    elif council_signal:
        forecast = _forecast(
            "extension_likely_exact_article_2_uncertain", "high", 0, 0,
            "medium_low",
            "Die Verlängerung des vorübergehenden Schutzes erscheint nach dem fortgeschrittenen Ratsstadium wahrscheinlich. Ob Artikel 2 unverändert bleibt, ist ohne angenommenen Endtext weiterhin offen.")
        next_milestone = _milestone(
            "political_agreement_or_council_adoption", "Dokumentierte Einigung und formelle Ratsannahme",
            "Erwartet wird zunächst ein belegtes Beratungsergebnis und danach ein förmlicher Ratsbeschluss; erst der Endtext zeigt die Fassung von Artikel 2.",
            "expected", ["Beratungsergebnis veröffentlichen", "Ratsannahme",
                         "finalen Text veröffentlichen"],
            config.get("council_register_url"), official_url)
    else:
        forecast = _forecast(
            "council_deliberation_continues", "moderate", 45, 70, "low",
            "Weitere Ratsberatung ist plausibel; die amtlichen Daten tragen noch keine belastbare Annahmeprognose.")
        next_milestone = _milestone(
            "council_position", "Nächste dokumentierte Ratsposition",
            "Erwartet wird ein neues Rats- oder Vorbereitungsgremium-Dokument.",
            "expected", ["neuen Ratsstand veröffentlichen"], official_url)
    inferences = [_inference(
        "stage_based_eu_forecast", "procedural_forecast",
        forecast["summary"], forecast["confidence"],
        ["latest_official_stage", "adopted_celex_presence",
         "official_journal_presence", "final_text_review_state"],
        config.get("council_register_url"), official_url),
        _inference(
            "article_2_wording_uncertain", "content_forecast",
            "Aus dem Ratsstadium lässt sich die endgültige Reichweite des geschlechtsneutral formulierten Artikels 2 nicht ableiten.",
            "low", ["no_adopted_celex", "no_official_journal_text",
                    "final_article_2_review_pending"], proposal_url, official_url)]
    return forecast, factors, next_milestone, inferences, warnings


def _analyse_dip_active(row: dict, config: dict, checks: list[dict],
                        facts: list[dict], as_of: str | None
                        ) -> tuple[dict, list[dict], dict, list[dict], list[str]]:
    official_url = row.get("official_url") or row.get("url")
    stage = str(row.get("stage") or row.get("status") or "")
    folded = stage.casefold()
    initiators = list(row.get("initiators") or [])
    approval = list(row.get("approval_requirements") or [])
    positions = [item for item in row.get("positions") or []
                 if isinstance(item, dict)]
    position_stages = [str(item.get("stage") or "").casefold()
                       for item in positions]
    first_reading = any("1. beratung" in value for value in position_stages)
    hearing = any(
        "anhörung" in str(item.get("stage") or "").casefold() and
        any(validation.get("passed") for validation in
            item.get("content_validations") or []
            if isinstance(validation, dict))
        for item in positions)
    br_objections = any(
        item.get("chamber") == "BR" and
        ("empfehlungen" in str(item.get("stage") or "").casefold() or
         "stellungnahme" in str(item.get("abstract") or "").casefold())
        for item in positions)
    committee_position = any(
        item.get("chamber") == "BT" and
        "beschlussempfehlung" in str(item.get("stage") or "").casefold()
        for item in positions)
    dated_positions = [str(item.get("date")) for item in positions
                       if item.get("date")]
    latest_position_date = max(dated_positions, default=None)
    days_without_position: int | None = None
    if latest_position_date and as_of:
        try:
            observed_day = date.fromisoformat(latest_position_date[:10])
            as_of_day = datetime.fromisoformat(
                str(as_of).replace("Z", "+00:00")).date()
            days_without_position = max(0, (as_of_day - observed_day).days)
        except ValueError:
            days_without_position = None
    government = any("bundesregierung" in str(item).casefold()
                     for item in initiators)
    recommendation = "beschlussempfehlung" in folded or committee_position
    plenary = any(word in folded for word in ("verabschiedet", "zugestimmt"))
    promulgated = bool(row.get("promulgation")) or "verkündet" in folded

    checks.extend([
        _check("draft_not_current_law", "role", "Entwurf von geltendem Recht getrennt",
               "passed" if config.get("draft_only") else "not_applicable",
               "Der Inhalt wird bis zur Verkündung ausdrücklich als Entwurfsstand behandelt.",
               config.get("scope_source"), official_url),
        _check("committee_recommendation_identified", "transition",
               "Ausschussfassung identifiziert",
               "passed" if recommendation or plenary or promulgated else "pending",
               ("Eine Ausschuss- oder spätere Fassung ist belegt." if recommendation or plenary or promulgated else
                "Eine Beschlussempfehlung des federführenden Ausschusses ist noch nicht in der überwachten Akte belegt."),
               official_url),
        _check("plenary_resolution_identified", "transition", "Plenarbeschluss identifiziert",
               "passed" if plenary or promulgated else "pending",
               ("Ein Plenarbeschluss ist belegt." if plenary or promulgated else
                "Ein Plenarbeschluss ist noch nicht belegt; eine Ausschussempfehlung wäre allein kein Gesetz."),
               official_url),
        _check("promulgated_text_identified", "final_text", "Verkündeten Gesetzestext identifiziert",
               "passed" if promulgated else "pending",
               ("Eine Verkündung ist belegt." if promulgated else
                "Ohne BGBl-Fundstelle werden Entwurfsaussagen nicht als geltendes Recht ausgegeben."),
               official_url),
        _check("current_law_effect_checked", "current_law", "Auswirkung im geltenden Recht geprüft",
               "pending" if not promulgated else "passed",
               ("Nach Verkündung muss der konsolidierte Normtext gegen die Endfassung geprüft werden."
                if not promulgated else "Die Verkündung kann nun gegen den konsolidierten Normtext geprüft werden."),
               official_url),
    ])
    validation_failures: list[str] = []
    for position in positions:
        document = position.get("document") or {}
        for validation in position.get("content_validations") or []:
            if not isinstance(validation, dict):
                continue
            retrieval = validation.get("retrieval_status")
            passed = bool(validation.get("passed"))
            status = "passed" if passed else (
                "pending" if retrieval != "fetched" else "failed")
            checks.append(_check(
                str(validation.get("id") or "official_content_check"),
                "final_text" if validation.get("kind") == "operative_text" else "source",
                str(validation.get("label") or "Amtlichen Inhalt geprüft"),
                status,
                str(validation.get("finding") or
                    "Die konfigurierten Merkmale wurden im amtlichen Dokument geprüft."),
                validation.get("source_url"), document.get("url")))
            if passed and validation.get("finding"):
                facts.append(_fact(
                    str(validation.get("id") or "content_validation"),
                    str(validation.get("kind") or "official_content"),
                    str(validation.get("finding")), "verified",
                    validation.get("source_url"), document.get("url")))
            elif not passed:
                validation_failures.append(str(validation.get("label") or
                                               validation.get("id")))
    if config.get("id") == "ukraine-rechtskreiswechsel":
        operative = next((check for check in checks
                          if check["id"] == "ukraine-cutoff-operative-text"), None)
        checks.append(_check(
            "dip_summary_not_operative_rule", "role",
            "DIP-Kurzfassung nicht mit Tatbestand verwechselt",
            "passed" if operative and operative["status"] == "passed" else "pending",
            "Die DIP-Kurzfassung spricht verkürzt von Einreise; maßgeblich für die beobachtete Entwurfsfassung sind die Regelungen zur ersten §-24-Erlaubnis beziehungsweise entsprechenden Fiktionsbescheinigung.",
            official_url, config.get("scope_source")))
    factors: list[dict] = []
    if government:
        factors.append(_factor(
            "supports", "Initiative der Bundesregierung",
            "Ein Regierungsentwurf hat regelmäßig einen organisierten Verfahrenspfad; das garantiert weder Mehrheit noch unveränderte Annahme.",
            official_url))
    if "koalitionsvertrag" in str(row.get("abstract") or "").casefold():
        factors.append(_factor(
            "supports", "Vorhaben im Koalitionsvertrag erwähnt",
            "Die amtliche DIP-Zusammenfassung nennt eine Koalitionsvereinbarung als Bezug des Entwurfs.",
            official_url))
    if first_reading:
        factors.append(_factor(
            "supports", "Erste Beratung und Ausschussüberweisung erfolgt",
            "Der Bundestag hat den Entwurf beraten und an die zuständigen Ausschüsse überwiesen.",
            *[item.get("document", {}).get("url") for item in positions
              if "1. beratung" in str(item.get("stage") or "").casefold()]))
    if hearing:
        factors.append(_factor(
            "supports", "Öffentliche Anhörung durchgeführt",
            "Der federführende Ausschuss hat am 23. Februar 2026 Sachverständige angehört.",
            *[item.get("document", {}).get("url") for item in positions
              if "anhörung" in str(item.get("stage") or "").casefold()]))
    if approval:
        factors.append(_factor(
            "against", "Zustimmungsbedürftigkeit angegeben",
            "Laut Entwurf ist die Zustimmung des Bundesrates erforderlich; damit bleibt nach dem Bundestag eine zusätzliche Hürde.",
            official_url))
    if br_objections:
        factors.append(_factor(
            "against", "Änderungsforderungen des Bundesrates",
            "Die amtliche Dokumentkette enthält Empfehlungen und eine Stellungnahme des Bundesrates; die Endfassung ist damit politisch und inhaltlich offen.",
            *[item.get("document", {}).get("url") for item in positions
              if item.get("chamber") == "BR"]))
    if first_reading and hearing and not committee_position:
        factors.append(_factor(
            "against", "Noch keine Beschlussempfehlung nach der Anhörung",
            "Trotz erster Beratung und Anhörung ist in der aktuellen DIP-Kette noch keine Ausschussfassung für die zweite und dritte Beratung erfasst.",
            official_url))
    if (days_without_position is not None and days_without_position > 90 and
            not committee_position):
        factors.append(_factor(
            "against", f"{days_without_position} Tage ohne neue DIP-Verfahrensposition",
            "Seit der jüngsten amtlich erfassten Position ist mehr als ein Vierteljahr vergangen, ohne dass eine Beschlussempfehlung in der DIP-Kette erscheint.",
            official_url))
    factors.append(_factor(
        "uncertainty", "Endfassung fehlt",
        "Ausschussänderungen, Plenarentscheidung und verkündeter Wortlaut liegen noch nicht vollständig vor.",
        official_url))

    if "überwiesen" in folded:
        forecast = _forecast(
            "progress_toward_committee_recommendation_likely", "moderate",
            0, 0, "medium_low",
            "Regierungsinitiative, Koalitionsbezug, erste Beratung und Anhörung sprechen für einen Fortgang. Zustimmungsbedürftigkeit, Bundesratsforderungen und die weiterhin fehlende Ausschussempfehlung begrenzen die Prognose; Annahme und Wortlaut bleiben offen.")
        next_milestone = _milestone(
            "committee_recommendation", "Ausschussberatung und Beschlussempfehlung",
            "Entscheidend ist die Ausschussfassung, nicht allein der ursprüngliche Regierungsentwurf.",
            "expected", ["Ausschussberatung", "Beschlussempfehlung veröffentlichen"],
            official_url)
    elif recommendation:
        forecast = _forecast(
            "plenary_adoption_likely", "high", 70, 90, "medium",
            "Eine Ausschussempfehlung erhöht die Wahrscheinlichkeit einer Plenarentscheidung; sie ist selbst noch kein Gesetz.")
        next_milestone = _milestone(
            "plenary_resolution", "Plenarentscheidung des Bundestages",
            "Die angenommene Fassung muss aus dem Plenarbeschluss bestimmt werden.",
            "expected", ["Plenarabstimmung", "angenommene Fassung erfassen"], official_url)
    elif plenary and not promulgated:
        forecast = _forecast(
            "promulgation_after_further_steps_likely", "high", 75, 95,
            "medium",
            "Der Bundestag hat entschieden; Bundesratsbehandlung, Ausfertigung und Verkündung bleiben je nach Verfahren offen.")
        next_milestone = _milestone(
            "federal_council_or_promulgation", "Weitere Verfahrensschritte und Verkündung",
            "Erst die BGBl-Fundstelle belegt den endgültigen Gesetzeswortlaut.",
            "expected", ["Bundesratsrolle klären", "BGBl-Fundstelle erfassen"], official_url)
    elif promulgated:
        forecast = _forecast(
            "promulgated", "completed", 100, 100, "high",
            "Das Gesetz ist verkündet; offen bleibt nur die Konsolidierungs- und Inhaltskontrolle.")
        next_milestone = _milestone(
            "current_law_validation", "Konsolidierten Normtext prüfen",
            "Die verkündeten Änderungen werden gegen die geltenden Normfassungen geprüft.",
            "pending", ["Konsolidierung abwarten", "Normen vergleichen"], official_url)
    else:
        forecast = _forecast(
            "further_deliberation_uncertain", "moderate", 35, 65, "low",
            "Der amtliche Status reicht für eine engere Prognose noch nicht aus.")
        next_milestone = _milestone(
            "next_official_stage", "Nächste amtliche Verfahrensstufe",
            "Die nächste Statusänderung wird aus DIP übernommen.", "expected",
            ["amtliche Statusänderung"], official_url)
    inferences = [_inference(
        "stage_based_dip_forecast", "procedural_forecast", forecast["summary"],
        forecast["confidence"],
        ["latest_DIP_stage", "initiative", "approval_requirement",
         "official_position_chain", "operative_text_checks",
         "final_text_presence"], official_url)]
    warnings = [
        "Der Prognosekorridor beruht auf Verfahrenssignalen; politische Mehrheiten, Termine und unveröffentlichte Verhandlungen werden nicht erfunden.",
        "Bis zur Verkündung bleibt der überwachte Regelungsumfang ein Entwurf und kann geändert werden.",
    ]
    if validation_failures:
        warnings.append("Noch nicht validierte amtliche Inhaltsprüfungen: " +
                        ", ".join(validation_failures) + ".")
    return forecast, factors, next_milestone, inferences, warnings


def _analyse_retrospective(row: dict, config: dict, fate: dict,
                           checks: list[dict], facts: list[dict]) -> tuple[dict, list[dict], dict, list[dict], list[str]]:
    chain = [item for item in fate.get("document_chain") or []
             if isinstance(item, dict)]
    roles = {str(item.get("role")) for item in chain}
    required_roles = {
        "government_draft", "bundesrat_recommendation",
        "committee_recommendation", "plenary_resolution", "promulgated_law",
    }
    missing_roles = sorted(required_roles - roles)
    chain_complete = not missing_roles
    validation = fate.get("validation") or {}
    current_passed = bool(validation.get("passed"))
    disposition = {str(item.get("role")): str(item.get("disposition"))
                   for item in chain}
    transition_valid = (
        disposition.get("bundesrat_recommendation") == "recommended_not_adopted" and
        disposition.get("committee_recommendation") == "omitted" and
        disposition.get("plenary_resolution") == "adopted_in_committee_version" and
        disposition.get("promulgated_law") == "promulgated_without_proposal")
    chain_urls = [item.get("url") for item in chain]
    checks.extend([
        _check("document_roles_complete", "chain", "Dokumentrollen vollständig",
               "passed" if chain_complete else "failed",
               ("Entwurf, Bundesratsempfehlung, Ausschussfassung, Plenarbeschluss und Verkündung sind getrennt erfasst."
                if chain_complete else
                "Fehlende Dokumentrollen: " + ", ".join(missing_roles)), chain_urls),
        _check("role_transition_consistent", "transition", "Übernahmeweg der Änderung geprüft",
               "passed" if transition_valid else "failed",
               ("Die Empfehlung wurde in der Ausschussfassung ausgelassen, diese Fassung beschlossen und ohne die Empfehlung verkündet."
                if transition_valid else
                "Die dokumentierten Dispositionen belegen den behaupteten Übernahmeweg noch nicht vollständig."), chain_urls),
        _check("promulgated_text_checked", "final_text", "Verkündeten Text geprüft",
               "passed" if "promulgated_law" in roles else "failed",
               "Die behauptete Nichtübernahme wird am verkündeten Gesetz geprüft.", chain_urls),
        _check("consolidated_current_law_checked", "current_law", "Geltendes Recht mechanisch geprüft",
               "passed" if current_passed else "failed",
               ("Alle deklarierten Kontrollen im aktuellen Lexgraph-Korpus sind bestanden."
                if current_passed else
                "Mindestens eine deklarierte Kontrolle des aktuellen Normtexts ist nicht bestanden."),
               [source.get("url") for source in fate.get("current_sources") or []
                if isinstance(source, dict)]),
    ])
    conclusion = str(fate.get("conclusion") or fate.get("claim") or "")
    facts.append(_fact(
        "retrospective_conclusion", "validated_document_chain", conclusion,
        "verified" if chain_complete and transition_valid and current_passed else "unverified",
        chain_urls))
    fully_validated = chain_complete and transition_valid and current_passed
    forecast = _forecast(
        "retrospective_validation_complete" if fully_validated else
        "retrospective_validation_incomplete",
        "completed" if fully_validated else "low",
        100 if fully_validated else 0, 100 if fully_validated else 40,
        "high" if fully_validated else "low",
        ("Die Dokumentkette und das geltende Recht bestätigen rückblickend, dass die Zwölfmonatsregel nicht Gesetz wurde."
         if fully_validated else
         "Die rückblickende Behauptung ist noch nicht durch eine vollständige Dokument- und Normkette abgesichert."))
    next_milestone = _milestone(
        "validation_complete" if fully_validated else "complete_validation",
        "Validierung abgeschlossen" if fully_validated else "Validierung vervollständigen",
        ("Der Vorgang bleibt mit seiner Beweiskette archiviert." if fully_validated else
         "Fehlende Rollen oder Normprüfungen müssen ergänzt werden."),
        "completed" if fully_validated else "pending",
        [] if fully_validated else ["Dokumentrollen ergänzen", "Normprüfungen bestehen"],
        chain_urls)
    factors = [
        _factor("supports", "Vollständige amtliche Dokumentkette",
                "Die Rollen vom Regierungsentwurf bis zum BGBl sind getrennt dokumentiert.",
                chain_urls),
        _factor("supports" if current_passed else "against",
                "Abgleich mit geltendem Recht",
                "Der aktuelle StAG- und VwGO-Korpus bestätigt die deklarierte Schlussfolgerung."
                if current_passed else "Der aktuelle Normabgleich ist nicht vollständig bestanden.",
                [source.get("url") for source in fate.get("current_sources") or []
                 if isinstance(source, dict)]),
    ]
    inference = _inference(
        "retrospective_fate", "document_chain_inference", forecast["summary"],
        forecast["confidence"],
        ["document_roles", "recorded_dispositions", "promulgated_text",
         "current_law_checks"], chain_urls)
    warnings = ([] if fully_validated else
                ["Die Schlussfolgerung darf bis zum Schließen der fehlenden Prüfungen nicht als validiert ausgegeben werden."])
    return forecast, factors, next_milestone, [inference], warnings


def analyse_procedure(row: dict, config: dict, history: list[dict],
                      amendment_records: list[dict], as_of: str | None) -> dict:
    """Return the stable analysis object embedded in one watch row."""
    facts, checks = _source_analysis(row, config)
    validation_ids = set(config.get("validation_ids") or [])
    fate = next((record for record in amendment_records
                 if isinstance(record, dict) and
                 (str(record.get("id")) in validation_ids or
                  str(record.get("procedure_id")) == str(row.get("id")))), None)

    if fate:
        forecast, factors, milestone, inferences, warnings = \
            _analyse_retrospective(row, config, fate, checks, facts)
    elif row.get("tracking_state") == "source_missing":
        forecast = _forecast(
            "forecast_suspended_source_missing", "low", 0, 100, "low",
            "Die Prognose ist ausgesetzt, bis der Vorgang wieder in einem aktuellen amtlichen Snapshot erscheint.")
        factors = [_factor(
            "uncertainty", "Amtliche Beobachtung fehlt",
            "Ein alter Status wird nicht als gegenwärtiger Stand fortgeschrieben.",
            row.get("official_url") or row.get("url"))]
        milestone = _milestone(
            "official_source_restored", "Amtliche Quelle wieder verfügbar",
            "Erst danach wird die stufenbasierte Prognose neu berechnet.",
            "pending", ["Vorgang im nächsten Snapshot wiederfinden"],
            row.get("official_url") or row.get("url"))
        inferences = []
        warnings = ["Keine belastbare Prognose bei fehlender aktueller Quelle."]
    elif str(row.get("source") or config.get("source")).casefold() == "eur-lex":
        forecast, factors, milestone, inferences, warnings = \
            _analyse_eu(row, config, checks, facts)
    else:
        forecast, factors, milestone, inferences, warnings = \
            _analyse_dip_active(row, config, checks, facts,
                                as_of or row.get("last_checked"))

    return {
        "schema_version": 1,
        "as_of": as_of or row.get("last_checked"),
        "method": "deterministic_official_evidence",
        "summary": forecast["summary"],
        "facts": facts,
        "inferences": inferences,
        "forecast": forecast,
        "factors": factors,
        "next_milestone": milestone,
        "checks": checks,
        "chronology": _chronology(row, history, fate),
        "warnings": warnings,
    }
