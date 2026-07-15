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


def _zip_bytes(text: str = "official") -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("regelung.xml", f"<akomaNtoso>{text}</akomaNtoso>")
    return out.getvalue()


class FakeResponse:
    def __init__(self, status_code: int, body: bytes = b"", data=None,
                 declared_size: int | None = None):
        self.status_code = status_code
        self._body = body
        self._data = data
        size = len(body) if declared_size is None else declared_size
        self.headers = {"Content-Length": str(size)} if status_code == 200 \
            else {}
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


class MappingHttp:
    def __init__(self):
        self.queries: list[str] = []

    def get(self, _url: str, **kwargs):
        abbreviation = kwargs["params"]["abbreviation"]
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
