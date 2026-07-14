from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

import fetch_bayern_wayback as wb  # noqa: E402


def _event(date: str, description: str,
           known: tuple[wb.NormId, ...]) -> wb.ChangeEvent:
    parsed = wb.parse_descriptor(description, known)
    return wb.ChangeEvent(
        jurabk="Example",
        date=date,
        seq=1,
        description=description,
        changes=parsed.changes,
        unspecified=parsed.unspecified,
    )


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        (
            "Art. 9, 52, 121, 123 geänd.",
            [("Art. 9", wb.OP_MODIFY), ("Art. 52", wb.OP_MODIFY),
             ("Art. 121", wb.OP_MODIFY), ("Art. 123", wb.OP_MODIFY)],
        ),
        (
            "Art. 5 und 8 geänd.",
            [("Art. 5", wb.OP_MODIFY), ("Art. 8", wb.OP_MODIFY)],
        ),
        (
            "Art. 112 bis 114 neu gef., 115, 116 aufgeh",
            [("Art. 112", wb.OP_REPLACE), ("Art. 113", wb.OP_REPLACE),
             ("Art. 114", wb.OP_REPLACE), ("Art. 115", wb.OP_REPEAL),
             ("Art. 116", wb.OP_REPEAL)],
        ),
        (
            "IÜ, Art. 19 geänd., Art. 23b eingef.",
            [("Art. 19", wb.OP_MODIFY), ("Art. 23b", wb.OP_ADD)],
        ),
        (
            "Art. 9 Abs. 2, 3 und 4 geänd.",
            [("Art. 9", wb.OP_MODIFY)],
        ),
        (
            "§§ 23, 30 geänd.",
            [("§ 23", wb.OP_MODIFY), ("§ 30", wb.OP_MODIFY)],
        ),
        (
            "Art, 7 geänd.",
            [("Art. 7", wb.OP_MODIFY)],
        ),
    ],
)
def test_ffn_descriptor_parser_preserves_targets_and_operations(
        description: str, expected: list[tuple[str, str]]) -> None:
    known = tuple(
        [wb.NormId("Art.", nr) for nr in
         ("5", "7", "8", "9", "19", "23b", "52", "112", "113",
          "114", "115", "116", "121", "123")]
        + [wb.NormId("§", nr) for nr in ("23", "30")]
    )
    parsed = wb.parse_descriptor(description, known)
    assert [(norm.label, operation)
            for norm, operation in parsed.changes] == expected
    assert parsed.unspecified is False


def test_range_and_coordinated_explicit_article_are_one_operation() -> None:
    known = tuple(wb.NormId("Art.", f"78{letter}")
                  for letter in "abcdefghijkl") + (wb.NormId("Art.", "96a"),)
    parsed = wb.parse_descriptor(
        "IÜ aufgeh., Art. 78a- 78l und Art. 96a geänd.", known)
    assert [norm.nr for norm, _ in parsed.changes] == [
        *(f"78{letter}" for letter in "abcdefghijkl"), "96a"]
    assert {operation for _, operation in parsed.changes} == {wb.OP_MODIFY}


@pytest.mark.parametrize("description", ["", "IÜ aufgeh.",
                                          "Siebter Teil, Abschnitt I, Ia geänd."])
def test_blank_or_structural_descriptor_is_not_a_wildcard(
        description: str) -> None:
    parsed = wb.parse_descriptor(description, (wb.NormId("Art.", "1"),))
    assert parsed.changes == ()
    assert parsed.unspecified is False


def test_only_explicit_mehrfach_descriptor_is_a_wildcard() -> None:
    parsed = wb.parse_descriptor(
        "IÜ aufgeh., mehrfach geänd.", (wb.NormId("Art.", "1"),))
    assert parsed.changes == ()
    assert parsed.unspecified is True


def test_load_events_uses_only_ffn_target_descriptors(tmp_path: Path) -> None:
    rows = [
        {"jurabk": "DVAsyl", "date": "2020-06-17", "seq": 2,
         "source": "ffn", "description": "§§ 23, 30 geänd."},
        {"jurabk": "DVAsyl", "date": "2020-06-17", "seq": 4,
         "source": "xml",
         "description": "V zur Änderung der Asyldurchführungsverordnung"},
    ]
    (tmp_path / "versions.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    events = wb._load_events(
        tmp_path, {"DVAsyl": [wb.NormId("§", "23"), wb.NormId("§", "30")]})
    assert len(events["DVAsyl"]) == 1
    assert [(norm.label, operation)
            for norm, operation in events["DVAsyl"][0].changes] == [
                ("§ 23", wb.OP_MODIFY), ("§ 30", wb.OP_MODIFY)]


def test_empty_cdx_response_is_not_persisted(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wb, "CACHE", tmp_path)
    monkeypatch.setattr(
        wb, "http_get",
        lambda *_args, **_kwargs: json.dumps([["original", "timestamp"]]))
    assert wb.cdx_act("Example", offline=False) == {}
    assert not (tmp_path / "cdx-act" / "Example.json").exists()


def test_corrupt_cached_cdx_entries_are_not_scheduled_offline(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wb, "CACHE", tmp_path)
    cache = tmp_path / "cdx-act"
    cache.mkdir()
    (cache / "Example.json").write_text(json.dumps({
        "Example-1": ["junk", "20200102030405"],
        "WrongAct-1": ["20210102030405"],
    }), encoding="utf-8")
    assert wb.cdx_act("Example", offline=True) == {
        "Example-1": ["20200102030405"]}


def test_partial_fresh_cdx_is_unioned_with_existing_cache(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wb, "CACHE", tmp_path)
    cache = tmp_path / "cdx-act"
    cache.mkdir()
    path = cache / "Example.json"
    path.write_text(json.dumps({
        "Example-1": ["20190102030405", "20200102030405"]}),
        encoding="utf-8")
    fresh = [
        ["original", "timestamp"],
        ["https://www.gesetze-bayern.de/Content/Document/Example-1",
         "20200102030405"],
        ["https://www.gesetze-bayern.de/Content/Document/Example-1",
         "20210102030405"],
    ]
    monkeypatch.setattr(
        wb, "http_get", lambda *_args, **_kwargs: json.dumps(fresh))
    expected = {"Example-1": [
        "20190102030405", "20200102030405", "20210102030405"]}
    assert wb.cdx_act("Example", offline=False) == expected
    assert json.loads(path.read_text(encoding="utf-8")) == expected


def test_offline_cache_access_never_calls_network(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wb, "CACHE", tmp_path)

    def forbidden_network(*_args, **_kwargs):
        raise AssertionError("offline mode attempted network access")

    monkeypatch.setattr(wb, "http_get", forbidden_network)
    assert wb.cdx_act("Example", offline=True) == {}
    assert wb.fetch_capture(
        "Example-1", "20200102030405", offline=True) is None


def test_legacy_per_document_cdx_cache_is_preserved(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wb, "CACHE", tmp_path)
    legacy = tmp_path / "cdx"
    legacy.mkdir()
    (legacy / "Example-23.json").write_text(json.dumps([
        "20190102030405", "20220102030405", "not-a-timestamp",
    ]), encoding="utf-8")
    assert wb.legacy_document_cdx_index("Example") == {
        "Example-23": ["20190102030405", "20220102030405"]}


def test_explicit_empty_or_unknown_act_filter_is_rejected() -> None:
    acts = {"PAG": "PAG", "AufnG": "BayAsylAufnG"}
    with pytest.raises(ValueError, match="at least one"):
        wb._select_acts(acts, set())
    with pytest.raises(ValueError, match="unknown.*NoSuchAct"):
        wb._select_acts(acts, {"NoSuchAct"})
    assert wb._select_acts(acts, {"PAG"}) == {"PAG": "PAG"}
    assert wb._select_acts(acts, {"BayAsylAufnG"}) == {
        "AufnG": "BayAsylAufnG"}


def test_distinct_transition_is_assigned_once_to_the_only_compatible_event() -> None:
    norm = wb.NormId("Art.", "1")
    other = wb.NormId("Art.", "2")
    captures = [
        wb.Capture("Example-1", "20190102000000", "2019-01-01", "old"),
        wb.Capture("Example-1", "20200102000000", "2019-01-01", "old"),
        wb.Capture("Example-1", "20221218000000", "2022-12-16", "new"),
        wb.Capture("Example-1", "20230101000000", "2022-12-16", "new"),
    ]
    states = wb.build_states(captures)
    events = [
        _event("2020-01-01", "Art. 2 geänd.", (norm, other)),
        _event("2022-12-09", "Art. 1 geänd.", (norm, other)),
    ]
    candidates, stats = wb.assign_document_transitions(norm, states, events)
    assert len(states) == 2
    assert len(candidates) == 1
    assert candidates[0].row["date"] == "2022-12-09"
    assert candidates[0].row["old"] == "old"
    assert candidates[0].row["new"] == "new"
    assert stats["assigned"] == 1


def test_candidate_keeps_complete_norm_text() -> None:
    norm = wb.NormId("Art.", "1")
    old_text = "a" * 5001
    new_text = "b" * 6001
    states = wb.build_states([
        wb.Capture("Example-1", "20190102000000", "2019-01-01", old_text),
        wb.Capture("Example-1", "20200102000000", "2020-01-01", new_text),
    ])
    candidates, _ = wb.assign_document_transitions(
        norm, states, [_event("2019-12-01", "Art. 1 geänd.", (norm,))])
    assert len(candidates) == 1
    assert candidates[0].row["old"] == old_text
    assert candidates[0].row["new"] == new_text


def test_sparse_transition_with_two_compatible_events_is_omitted() -> None:
    norm = wb.NormId("Art.", "1")
    states = wb.build_states([
        wb.Capture("Example-1", "20190102000000", "2019-01-01", "old"),
        wb.Capture("Example-1", "20230102000000", "2023-01-01", "new"),
    ])
    events = [
        _event("2020-01-01", "Art. 1 geänd.", (norm,)),
        _event("2022-01-01", "Art. 1 neu gef.", (norm,)),
    ]
    candidates, stats = wb.assign_document_transitions(norm, states, events)
    assert candidates == []
    assert stats["ambiguous"] == 1


def test_one_unspecified_event_is_allowed_but_two_are_ambiguous() -> None:
    norm = wb.NormId("Art.", "1")
    states = wb.build_states([
        wb.Capture("Example-1", "20190102000000", "2019-01-01", "old"),
        wb.Capture("Example-1", "20230102000000", "2023-01-01", "new"),
    ])
    one = [_event("2020-01-01", "mehrfach geänd.", (norm,))]
    candidates, _ = wb.assign_document_transitions(norm, states, one)
    assert len(candidates) == 1
    assert candidates[0].row["confidence"] == "unique-unspecified"

    two = [*one, _event("2022-01-01", "mehrfach geänd.", (norm,))]
    candidates, stats = wb.assign_document_transitions(norm, states, two)
    assert candidates == []
    assert stats["ambiguous"] == 1


def test_equal_declared_validity_dates_do_not_fall_back_to_capture_time() -> None:
    norm = wb.NormId("Art.", "1")
    states = wb.build_states([
        wb.Capture("Example-1", "20190102000000", "2020-01-01", "old"),
        wb.Capture("Example-1", "20220102000000", "2020-01-01", "new"),
    ])
    candidates, stats = wb.assign_document_transitions(
        norm, states, [_event("2021-01-01", "Art. 1 geänd.", (norm,))])
    assert candidates == []
    assert stats["invalid_window"] == 1


def test_missing_validity_uses_last_old_and_first_new_capture_bounds() -> None:
    norm = wb.NormId("Art.", "1")
    states = wb.build_states([
        wb.Capture("Example-1", "20190102000000", None, "old"),
        wb.Capture("Example-1", "20210102000000", None, "old"),
        wb.Capture("Example-1", "20230102000000", None, "new"),
    ])
    events = [
        _event("2020-01-01", "Art. 1 geänd.", (norm,)),
        _event("2022-01-01", "Art. 1 geänd.", (norm,)),
    ]
    candidates, _ = wb.assign_document_transitions(norm, states, events)
    assert len(candidates) == 1
    assert candidates[0].row["date"] == "2022-01-01"
    assert candidates[0].row["old_capture"] == "20210102000000"
    assert candidates[0].row["new_capture"] == "20230102000000"


def test_empty_sides_require_explicit_insertion_or_repeal() -> None:
    norm = wb.NormId("Art.", "23b")
    state = wb.build_states([
        wb.Capture("Example-23b", "20170720000000", "2017-07-20", "text")
    ])

    modified, modified_stats = wb.assign_document_transitions(
        norm, state, [_event("2017-07-12", "Art. 23b geänd.", (norm,))])
    assert modified == []
    assert modified_stats["single_state"] == 1

    inserted, _ = wb.assign_document_transitions(
        norm, state, [_event("2017-07-12", "Art. 23b eingef.", (norm,))])
    assert len(inserted) == 1
    assert inserted[0].row["old"] == ""
    assert inserted[0].row["new"] == "text"
    assert inserted[0].row["operation"] == wb.OP_ADD

    repealed, _ = wb.assign_document_transitions(
        norm, state, [_event("2018-01-01", "Art. 23b aufgeh.", (norm,))])
    assert len(repealed) == 1
    assert repealed[0].row["old"] == "text"
    assert repealed[0].row["new"] == ""
    assert repealed[0].row["operation"] == wb.OP_REPEAL


def test_transition_collision_drops_all_assignments() -> None:
    base = {
        "jurabk": "Example", "date": "2020-01-01", "para": "Art. 1",
        "old": "a", "new": "b",
    }
    candidates = [
        wb.Candidate("same-transition", {**base, "event_id": "one"}),
        wb.Candidate("same-transition",
                     {**base, "date": "2021-01-01", "event_id": "two"}),
    ]
    rows, dropped = wb.unique_candidates(candidates)
    assert rows == []
    assert dropped == 2


def test_repeated_textual_transition_in_distinct_epochs_is_preserved() -> None:
    norm = wb.NormId("Art.", "1")
    states = wb.build_states([
        wb.Capture("Example-1", "20180102000000", "2018-01-01", "A"),
        wb.Capture("Example-1", "20190102000000", "2019-01-01", "B"),
        wb.Capture("Example-1", "20200102000000", "2020-01-01", "A"),
        wb.Capture("Example-1", "20210102000000", "2021-01-01", "B"),
    ])
    events = [
        _event("2018-12-01", "Art. 1 geänd.", (norm,)),
        _event("2019-12-01", "Art. 1 geänd.", (norm,)),
        _event("2020-12-01", "Art. 1 geänd.", (norm,)),
    ]
    candidates, _ = wb.assign_document_transitions(norm, states, events)
    assert len(candidates) == 3
    assert candidates[0].row["old"] == candidates[2].row["old"] == "A"
    assert candidates[0].row["new"] == candidates[2].row["new"] == "B"
    assert candidates[0].transition_id != candidates[2].transition_id
    rows, dropped = wb.unique_candidates(candidates)
    assert len(rows) == 3
    assert dropped == 0


def test_repeated_repeal_of_restored_text_has_distinct_epoch_identity() -> None:
    norm = wb.NormId("Art.", "1")
    states = wb.build_states([
        wb.Capture("Example-1", "20180102000000", "2018-01-01", "A"),
        wb.Capture("Example-1", "20200102000000", "2020-01-01", "B"),
        wb.Capture("Example-1", "20220102000000", "2022-01-01", "A"),
    ])
    events = [
        _event("2019-01-01", "Art. 1 aufgeh.", (norm,)),
        _event("2023-01-01", "Art. 1 aufgeh.", (norm,)),
    ]
    candidates, _ = wb.assign_document_transitions(norm, states, events)
    repeals = [candidate for candidate in candidates
               if candidate.row["operation"] == wb.OP_REPEAL]
    assert len(repeals) == 2
    assert repeals[0].transition_id != repeals[1].transition_id
    rows, dropped = wb.unique_candidates(repeals)
    assert len(rows) == 2
    assert dropped == 0


def test_explicit_output_replaces_only_candidate_target(tmp_path: Path) -> None:
    canonical = tmp_path / "data" / "by_diffs.jsonl"
    canonical.parent.mkdir()
    canonical.write_text("canonical-sentinel\n", encoding="utf-8")
    target = tmp_path / "candidate.jsonl"
    target.write_text("old-candidate\n", encoding="utf-8")
    rows = [{"jurabk": "Example", "date": "2020-01-01",
             "para": "Art. 1", "old": "a", "new": "b"}]

    wb.write_output(str(target), rows)

    assert canonical.read_text(encoding="utf-8") == "canonical-sentinel\n"
    assert [json.loads(line) for line in target.read_text(
        encoding="utf-8").splitlines()] == rows
    assert not list(tmp_path.glob(".candidate.jsonl.tmp-*"))
