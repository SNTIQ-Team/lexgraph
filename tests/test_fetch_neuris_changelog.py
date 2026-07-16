from __future__ import annotations

import hashlib
import io
import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

import fetch_neuris_changelog as neuris  # noqa: E402


WORK = "eli/bund/bgbl-1/2004/s1950"
URL_1 = ("https://testphase.rechtsinformationen.bund.de/v1/legislation/"
         f"{WORK}/2026-07-01/1/deu/2026-06-03.zip")
URL_2 = ("https://testphase.rechtsinformationen.bund.de/v1/legislation/"
         f"{WORK}/2026-07-02/1/deu/2026-06-04.zip")
URL_XML = ("https://testphase.rechtsinformationen.bund.de/v1/legislation/"
           f"{WORK}/2026-07-03/1/deu/2026-06-05.xml")
URL_HTML = ("https://testphase.rechtsinformationen.bund.de/v1/legislation/"
            f"{WORK}/2026-07-04/1/deu/2026-06-06.html")


def _zip_bytes(text: str = "official") -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("regelung.xml", f"<akomaNtoso>{text}</akomaNtoso>")
    return out.getvalue()


def _xml_bytes(text: str = "official") -> bytes:
    return (f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<akomaNtoso><doc>{text}</doc></akomaNtoso>').encode()


def _html_bytes(text: str = "official") -> bytes:
    return (f'<!doctype html><html><body><main>{text}</main></body></html>') \
        .encode()


class FakeResponse:
    def __init__(self, status_code: int, body: bytes = b"", data=None,
                 declared_size: int | None = None,
                 content_type: str | None = None):
        self.status_code = status_code
        self._body = body
        self._data = data
        size = len(body) if declared_size is None else declared_size
        self.headers = {"Content-Length": str(size)} if status_code == 200 \
            else {}
        if content_type and status_code == 200:
            self.headers["Content-Type"] = content_type
        self.closed = False

    def json(self):
        return self._data

    def iter_content(self, chunk_size: int):
        for start in range(0, len(self._body), chunk_size):
            yield self._body[start:start + chunk_size]

    def close(self):
        self.closed = True


class VanishingHttp:
    """Serve each ZIP once, then behave like the retired NeuRIS URL."""

    def __init__(self, bodies: dict[str, bytes]):
        self.bodies = dict(bodies)
        self.calls: list[str] = []

    def get(self, url: str, **_kwargs):
        self.calls.append(url)
        body = self.bodies.pop(url, None)
        return FakeResponse(200, body) if body is not None \
            else FakeResponse(404)


class ForbiddenHttp:
    def get(self, url: str, **_kwargs):  # pragma: no cover - assertion path
        raise AssertionError(f"unexpected HTTP request: {url}")


class StaticHttp:
    def __init__(self, responses: dict[str, FakeResponse]):
        self.responses = dict(responses)
        self.calls: list[str] = []

    def get(self, url: str, **_kwargs):
        self.calls.append(url)
        return self.responses.get(url, FakeResponse(404))


class MappingHttp:
    def __init__(self):
        self.queries: list[str] = []

    def get(self, _url: str, **kwargs):
        assert "abbreviation" not in kwargs["params"]
        search_term = kwargs["params"]["searchTerm"]
        assert search_term.startswith('"') and search_term.endswith('"')
        abbreviation = search_term[1:-1]
        self.queries.append(abbreviation)
        member = []
        if abbreviation == "AsylG":
            member = [{"item": {
                "abbreviation": "AsylG",
                "exampleOfWork": {
                    "legislationIdentifier": "eli/bund/bgbl-1/1992/s1126",
                },
            }}]
        return FakeResponse(200, data={"member": member})


def _mapping() -> dict:
    return {
        "schema_version": 1,
        "by_work": {
            WORK: {"slug": "aufenthg_2004", "jurabk": "AufenthG 2004",
                   "query_abbreviation": "AufenthG"},
        },
    }


def _event(url: str = URL_1, fetched_at: str = "2026-07-15T12:00:00+00:00"):
    return neuris.to_event(url, "consolidation_changed", fetched_at)


def test_ephemeral_url_is_captured_before_archive_commit(tmp_path: Path):
    body = _zip_bytes("before it vanished")
    http = VanishingHttp({URL_1: body})
    archive = tmp_path / "neuris_archive.jsonl"
    objects = tmp_path / "neuris_objects"

    events, budget = neuris.capture_events(
        [_event()], http, _mapping(), archive_path=archive, objects=objects,
        max_downloads=4, max_bytes=1024 * 1024)
    assert not archive.exists(), "capture must finish before ledger mutation"
    added, upgraded, total = neuris.update_archive(archive, events)

    digest = hashlib.sha256(body).hexdigest()
    assert (added, upgraded, total) == (1, 0, 1)
    assert budget.downloads == 1
    assert events[0]["capture_status"] == "captured"
    assert events[0]["content_sha256"] == digest
    assert events[0]["content_bytes"] == len(body)
    assert (objects / f"{digest}.zip").read_bytes() == body
    row = json.loads(archive.read_text(encoding="utf-8"))
    assert row["content_sha256"] == digest
    assert row["capture_status"] == "captured"
    assert row["time"] == "2026-07-15T12:00:00+00:00"
    assert row["point_in_time"] == "2026-07-01"
    assert row["legal_effect"] == "not_asserted"

    # The source object has disappeared after the changelog response, while
    # the hash-addressed capture committed above remains available.
    assert http.get(URL_1).status_code == 404
    assert (objects / f"{digest}.zip").is_file()


def test_second_run_reuses_verified_cas_without_network(tmp_path: Path):
    body = _zip_bytes("idempotent")
    archive = tmp_path / "archive.jsonl"
    objects = tmp_path / "objects"
    first, _ = neuris.capture_events(
        [_event()], VanishingHttp({URL_1: body}), _mapping(),
        archive_path=archive, objects=objects)
    neuris.update_archive(archive, first)

    second, budget = neuris.capture_events(
        [_event(fetched_at="2026-07-16T12:00:00+00:00")],
        ForbiddenHttp(), _mapping(), archive_path=archive, objects=objects)
    added, upgraded, total = neuris.update_archive(archive, second)

    assert budget.downloads == 0
    assert second[0]["capture_status"] == "captured"
    assert second[0]["capture_reused"] is True
    assert (added, upgraded, total) == (0, 0, 1)
    assert len(list(objects.glob("*.zip"))) == 1


def test_download_and_byte_caps_are_hard_guards(tmp_path: Path):
    body = _zip_bytes("bounded")
    http = VanishingHttp({URL_1: body, URL_2: body})
    events, budget = neuris.capture_events(
        [_event(URL_1), _event(URL_2)], http, _mapping(),
        archive_path=tmp_path / "archive.jsonl",
        objects=tmp_path / "objects", max_downloads=1,
        max_bytes=1024 * 1024)
    assert [row["capture_status"] for row in events] == [
        "captured", "limit_downloads"]
    assert budget.downloads == 1
    assert http.calls == [URL_1]

    oversized = VanishingHttp({URL_1: body})
    limited, limited_budget = neuris.capture_events(
        [_event()], oversized, _mapping(),
        archive_path=tmp_path / "empty.jsonl",
        objects=tmp_path / "objects-2", max_downloads=1,
        max_bytes=len(body) - 1)
    assert limited[0]["capture_status"] == "limit_bytes"
    assert limited[0]["content_sha256"] is None
    assert limited_budget.downloads == 1
    assert not list((tmp_path / "objects-2").glob("*.zip")) \
        if (tmp_path / "objects-2").exists() else True


def test_unmapped_changed_event_and_deleted_tombstone_stay_metadata_only(
        tmp_path: Path):
    deleted = neuris.to_event(
        URL_2, "consolidation_deleted", "2026-07-15T12:00:00+00:00")
    rows, budget = neuris.capture_events(
        [_event(), deleted], ForbiddenHttp(), {"by_work": {}},
        archive_path=tmp_path / "archive.jsonl",
        objects=tmp_path / "objects")

    assert budget.downloads == 0
    assert rows[0]["capture_status"] == "metadata_only_unmapped"
    assert rows[0]["content_sha256"] is None
    assert rows[1]["capture_status"] == "tombstone"
    assert rows[1]["kind"] == "consolidation_deleted"
    assert rows[1]["content_url"] == URL_2


def test_asyl_and_sgb_aliases_are_explicit_not_fuzzy():
    assert neuris.abbreviation_candidates({
        "slug": "asylvfg_1992", "jurabk": "AsylVfG 1992",
    })[0] == "AsylG"
    assert neuris.abbreviation_candidates({
        "slug": "sgb_2", "jurabk": "SGB 2",
    })[0] == "SGB II"

    ambiguous = {
        "member": [
            {"item": {"abbreviation": "SGB II", "exampleOfWork": {
                "legislationIdentifier": "eli/bund/bgbl-1/2003/s2954"}}},
            {"item": {"abbreviation": "SGB II", "exampleOfWork": {
                "legislationIdentifier": "eli/bund/bgbl-1/2004/s1"}}},
        ],
    }
    assert neuris._exact_work_from_search(ambiguous, "SGB II") is None


def test_curated_work_mapping_is_persisted_and_unresolved_is_throttled(
        tmp_path: Path):
    path = tmp_path / "neuris_work_map.json"
    acts = [
        {"slug": "asylvfg_1992", "jurabk": "AsylVfG 1992"},
        {"slug": "sgb_2", "jurabk": "SGB 2"},
    ]
    now = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    http = MappingHttp()
    state = neuris.resolve_curated_work_map(http, acts, now=now, path=path)

    assert state["by_slug"]["asylvfg_1992"]["eli_work"] == \
        "eli/bund/bgbl-1/1992/s1126"
    assert state["by_work"]["eli/bund/bgbl-1/1992/s1126"]["jurabk"] == \
        "AsylVfG 1992"
    assert state["unresolved"]["sgb_2"]["queries"][0] == "SGB II"
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 1

    # A second run inside the seven-day retry window uses both the resolved
    # entry and the persisted miss without issuing 51-ish repeated searches.
    again = neuris.resolve_curated_work_map(
        ForbiddenHttp(), acts, now=now, path=path)
    assert again["by_work"] == state["by_work"]


def test_legacy_archive_is_migrated_without_losing_tombstones(tmp_path: Path):
    archive = tmp_path / "archive.jsonl"
    old_changed = {
        "event_id": "old-change", "kind": "consolidation_changed",
        "source": "neuris_changelog", "time": "2026-07-01",
        "point_in_time": "2026-07-01", "content_url": URL_1,
        "fetched_at": "2026-07-15T12:00:00+00:00",
    }
    old_deleted = {
        "event_id": "old-delete", "kind": "consolidation_deleted",
        "source": "neuris_changelog", "time": "2026-07-02",
        "point_in_time": "2026-07-02", "content_url": URL_2,
        "fetched_at": "2026-07-15T12:00:00+00:00",
    }
    archive.write_text(
        json.dumps(old_changed) + "\n" + json.dumps(old_deleted) + "\n",
        encoding="utf-8")

    added, upgraded, total = neuris.update_archive(archive, [])
    rows = [json.loads(line) for line in archive.read_text().splitlines()]
    assert (added, upgraded, total) == (0, 0, 2)
    assert rows[0]["time"] == "2026-07-15T12:00:00+00:00"
    assert rows[0]["legacy_source_time"] == "2026-07-01"
    assert rows[0]["capture_status"] == \
        "legacy_metadata_only_not_captured"
    assert rows[0]["content_sha256"] is None
    assert rows[1]["capture_status"] == "tombstone"
    assert rows[1]["event_id"] == "old-delete"


def test_archive_backfill_is_bounded_resumable_and_records_dates_and_media(
        tmp_path: Path):
    archive = tmp_path / "archive.jsonl"
    objects = tmp_path / "objects"
    stamp_1 = "2026-07-16T10:00:00+00:00"
    stamp_2 = "2026-07-16T11:00:00+00:00"
    source_rows = [
        _event(URL_1, "2026-07-10T01:00:00+00:00"),
        _event(URL_XML, "2026-07-10T02:00:00+00:00"),
        _event(URL_HTML, "2026-07-10T03:00:00+00:00"),
    ]
    # Exercise the legacy migration boundary as well as capture: a historical
    # PIT must not survive as an asserted legal-effective date.
    for row in source_rows:
        row["time"] = row["point_in_time"]
        row["legal_effect"] = "revises_consolidation"
        row.pop("date_basis", None)
    neuris.atomic_write_jsonl(archive, source_rows)

    zip_body = _zip_bytes("zip")
    xml_body = _xml_bytes("xml")
    first_http = StaticHttp({
        URL_1: FakeResponse(200, zip_body, content_type="application/zip"),
        URL_XML: FakeResponse(
            200, xml_body, content_type="application/akn+xml; charset=utf-8"),
    })
    first, first_budget = neuris.backfill_archive(
        first_http, archive_path=archive, objects=objects, limit=2,
        max_downloads=2, max_bytes=1024 * 1024, captured_at=stamp_1)

    rows = [json.loads(line) for line in archive.read_text().splitlines()]
    assert first_http.calls == [URL_1, URL_XML]
    assert first["attempted"] == 2
    assert first["captured"] == 2
    assert first["remaining"] == 1
    assert first_budget.downloads == 2
    for row in rows[:2]:
        assert row["capture_status"] == "captured"
        assert row["captured_at"] == stamp_1
        assert row["capture_attempted_at"] == stamp_1
        assert row["capture_attempts"] == 1
        assert row["content_source_url"] == row["content_url"]
        assert row["legal_effect"] == "not_asserted"
        assert row["date_basis"] == \
            "retrieval_observation_and_eli_identifiers_not_legal_effect"
        assert row["time"] == row["fetched_at"]
        assert "effective_at" not in row
    assert rows[0]["content_media_type"] == "application/zip"
    assert rows[1]["content_media_type"] == "application/xml"
    assert rows[1]["content_sha256"] == hashlib.sha256(xml_body).hexdigest()
    assert rows[1]["content_bytes"] == len(xml_body)
    assert (objects / f"{rows[1]['content_sha256']}.xml").read_bytes() == \
        xml_body
    assert rows[2]["capture_status"] == \
        "legacy_metadata_only_not_captured"

    html_body = _html_bytes("html")
    second_http = StaticHttp({
        URL_HTML: FakeResponse(200, html_body, content_type="text/html"),
    })
    second, second_budget = neuris.backfill_archive(
        second_http, archive_path=archive, objects=objects, limit=2,
        max_downloads=2, max_bytes=1024 * 1024, captured_at=stamp_2)

    rows = [json.loads(line) for line in archive.read_text().splitlines()]
    assert second_http.calls == [URL_HTML]
    assert second["cached"] == 2
    assert second["attempted"] == 1
    assert second["captured"] == 1
    assert second["remaining"] == 0
    assert second_budget.downloads == 1
    assert rows[2]["content_media_type"] == "text/html"
    assert rows[2]["captured_at"] == stamp_2
    assert rows[2]["capture_attempted_at"] == stamp_2
    assert (objects / f"{rows[2]['content_sha256']}.html").read_bytes() == \
        html_body


def test_archive_backfill_failure_does_not_starve_unattempted_rows(
        tmp_path: Path):
    archive = tmp_path / "archive.jsonl"
    objects = tmp_path / "objects"
    neuris.atomic_write_jsonl(archive, [_event(URL_1), _event(URL_2)])

    first_http = StaticHttp({URL_1: FakeResponse(404)})
    first, _ = neuris.backfill_archive(
        first_http, archive_path=archive, objects=objects, limit=1,
        captured_at="2026-07-16T10:00:00+00:00")
    rows = [json.loads(line) for line in archive.read_text().splitlines()]
    assert first_http.calls == [URL_1]
    assert first["failed"] == 1
    assert rows[0]["capture_status"] == "http_404"
    assert rows[0]["capture_attempts"] == 1

    body = _zip_bytes("second row")
    second_http = StaticHttp({URL_2: FakeResponse(200, body)})
    second, _ = neuris.backfill_archive(
        second_http, archive_path=archive, objects=objects, limit=1,
        captured_at="2026-07-16T11:00:00+00:00")
    rows = [json.loads(line) for line in archive.read_text().splitlines()]
    assert second_http.calls == [URL_2]
    assert second["captured"] == 1
    assert rows[0]["capture_status"] == "http_404"
    assert rows[1]["capture_status"] == "captured"


def test_archive_backfill_prioritises_exact_curated_work_mapping(
        tmp_path: Path):
    archive = tmp_path / "archive.jsonl"
    objects = tmp_path / "objects"
    general = _event(URL_1, "2026-07-09T01:00:00+00:00")
    curated = _event(URL_2, "2026-07-10T01:00:00+00:00")
    curated["eli_work"] = "eli/bund/bgbl-1/2005/s1218"
    neuris.atomic_write_jsonl(archive, [general, curated])
    body = _zip_bytes("curated first")
    http = StaticHttp({URL_2: FakeResponse(200, body)})

    stats, _ = neuris.backfill_archive(
        http, archive_path=archive, objects=objects, limit=1,
        work_map={"by_work": {
            "eli/bund/bgbl-1/2005/s1218": {
                "slug": "example", "jurabk": "ExampleG",
            },
        }}, captured_at="2026-07-16T12:00:00+00:00")

    rows = [json.loads(line) for line in archive.read_text().splitlines()]
    assert stats["captured"] == 1
    assert http.calls == [URL_2]
    assert rows[0].get("content_sha256") is None
    assert rows[1]["mapped_slug"] == "example"
    assert rows[1]["mapped_jurabk"] == "ExampleG"


def test_archive_backfill_rejects_non_official_source_without_http(
        tmp_path: Path):
    archive = tmp_path / "archive.jsonl"
    row = _event()
    row["content_url"] = "https://example.org/v1/legislation/object.zip"
    neuris.atomic_write_jsonl(archive, [row])

    stats, budget = neuris.backfill_archive(
        ForbiddenHttp(), archive_path=archive, objects=tmp_path / "objects",
        limit=1, captured_at="2026-07-16T12:00:00+00:00")
    stored = json.loads(archive.read_text())
    assert stats["attempted"] == 1
    assert stats["failed"] == 1
    assert budget.downloads == 0
    assert stored["capture_status"] == "invalid_source_url"
    assert stored["capture_attempted_at"] == "2026-07-16T12:00:00+00:00"
